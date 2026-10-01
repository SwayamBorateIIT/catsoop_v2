"""
Analytics data store.

A single SQLite database, separate from CAT-SOOP's own data, holding normalized
events plus precomputed daily snapshots.  CAT-SOOP's log files are never
queried by the engine or the dashboard -- only by `extract`, and only to fill
this store (design plan, §3.2 and §6).

Why SQLite rather than PostgreSQL: the whole point of putting this inside the
CAT-SOOP server is to avoid operating a second service.  The schema is plain
portable SQL with no SQLite-specific types, so moving to PostgreSQL later is a
connection change, not a redesign (design plan, §4.4).

The store is opened WAL-mode so the dashboard can read it while a sync writes.
"""

import os
import sqlite3
import time

from .. import base_context

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS course (
    course_id     TEXT PRIMARY KEY,
    last_sync     REAL,
    sync_seconds  REAL,
    event_count   INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS student (
    course_id TEXT NOT NULL,
    username  TEXT NOT NULL,
    name      TEXT,
    email     TEXT,
    role      TEXT,
    PRIMARY KEY (course_id, username)
);

CREATE TABLE IF NOT EXISTS module (
    course_id TEXT NOT NULL,
    path      TEXT NOT NULL,
    PRIMARY KEY (course_id, path)
);

CREATE TABLE IF NOT EXISTS exercise (
    course_id    TEXT NOT NULL,
    path         TEXT NOT NULL,
    qname        TEXT NOT NULL,
    qtype        TEXT,
    npoints      REAL DEFAULT 1,
    display_name TEXT,
    PRIMARY KEY (course_id, path, qname)
);

CREATE TABLE IF NOT EXISTS event (
    course_id    TEXT NOT NULL,
    username     TEXT NOT NULL,
    path         TEXT NOT NULL,
    qname        TEXT,
    kind         TEXT NOT NULL,
    action       TEXT,
    score        REAL,
    ts           REAL NOT NULL,
    impersonated INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS state (
    course_id TEXT NOT NULL,
    username  TEXT NOT NULL,
    path      TEXT NOT NULL,
    qname     TEXT NOT NULL,
    score     REAL,
    nsubmits  INTEGER DEFAULT 0,
    last_ts   REAL,
    PRIMARY KEY (course_id, username, path, qname)
);

CREATE TABLE IF NOT EXISTS cursor (
    course_id TEXT NOT NULL,
    username  TEXT NOT NULL,
    path      TEXT NOT NULL,
    logname   TEXT NOT NULL,
    offset    INTEGER NOT NULL DEFAULT 0,
    mtime     REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (course_id, username, path, logname)
);

CREATE TABLE IF NOT EXISTS anomaly (
    course_id TEXT NOT NULL,
    username  TEXT,
    path      TEXT,
    kind      TEXT NOT NULL,
    detail    TEXT,
    ts        REAL,
    seen_at   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshot (
    course_id TEXT NOT NULL,
    day       TEXT NOT NULL,
    key       TEXT NOT NULL,
    value     REAL,
    PRIMARY KEY (course_id, day, key)
);

-- An event is uniquely identified by who did what, where, and exactly when.
-- CAT-SOOP timestamps carry microseconds, so two distinct actions cannot
-- collide; a collision therefore means the same record was read twice, which
-- is what happens if two syncs overlap.  `qname` is coalesced because activity
-- events carry no question, and SQLite treats NULLs as distinct in an index.
CREATE UNIQUE INDEX IF NOT EXISTS event_natural_key ON event
    (course_id, username, path, kind, action, ts, COALESCE(qname, ''));

CREATE INDEX IF NOT EXISTS event_course_ts   ON event (course_id, ts);
CREATE INDEX IF NOT EXISTS event_course_q    ON event (course_id, path, qname);
CREATE INDEX IF NOT EXISTS event_course_user ON event (course_id, username, ts);
CREATE INDEX IF NOT EXISTS state_course_path ON state (course_id, path);
CREATE INDEX IF NOT EXISTS snapshot_course   ON snapshot (course_id, key, day);
"""


def db_path():
    return os.path.join(base_context.cs_data_root, "_logs", "_analytics", "analytics.db")


def _dedupe_events(conn):
    """
    Remove duplicate events left behind by overlapping syncs, keeping the
    earliest row of each set.

    Needed once, on stores created before the unique index existed: the index
    cannot be built while duplicates are present.
    """
    have_index = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'index'"
        " AND name = 'event_natural_key'"
    ).fetchone()
    if have_index:
        return 0
    removed = conn.execute(
        "DELETE FROM event WHERE rowid NOT IN ("
        "  SELECT MIN(rowid) FROM event"
        "  GROUP BY course_id, username, path, kind, action, ts,"
        "           COALESCE(qname, '')"
        ")"
    ).rowcount
    conn.commit()
    return max(0, removed)


def connect(readonly=False):
    """Open the analytics store, creating it if necessary."""
    path = db_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fresh = not os.path.exists(path)
    conn = sqlite3.connect(path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    if not readonly:
        # Existing stores may hold duplicates from before the unique index
        # existed; they must go before the index can be created.
        try:
            _dedupe_events(conn)
        except sqlite3.Error:
            pass
        conn.executescript(_SCHEMA)
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        conn.commit()
    elif fresh:
        conn.executescript(_SCHEMA)
        conn.commit()
    return conn


def encode_path(path):
    """Path lists are stored as `/`-joined strings; `/` cannot occur in a segment."""
    return "/".join(path)


def decode_path(text):
    return tuple(text.split("/")) if text else ()


# ------------------------------------------------------------------- cursors


def get_cursors(conn, course):
    rows = conn.execute(
        "SELECT username, path, logname, offset, mtime FROM cursor WHERE course_id = ?",
        (course,),
    ).fetchall()
    return {(r["username"], r["path"], r["logname"]): (r["offset"], r["mtime"]) for r in rows}


def set_cursor(conn, course, username, path, logname, offset, mtime):
    conn.execute(
        "INSERT OR REPLACE INTO cursor (course_id, username, path, logname, offset, mtime)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (course, username, encode_path(path), logname, offset, mtime),
    )


# -------------------------------------------------------------------- writes


def drop_events(conn, course, username, path):
    """Discard events for one log, used when a log was truncated or rebuilt."""
    conn.execute(
        "DELETE FROM event WHERE course_id = ? AND username = ? AND path = ?",
        (course, username, encode_path(path)),
    )


def insert_events(conn, events):
    """
    Insert events, ignoring any that are already present.

    `OR IGNORE` against the natural-key index makes this safe to call twice
    with the same records, which is the second line of defence behind the sync
    lock: even if two syncs overlap, the store cannot end up double-counting.
    """
    conn.executemany(
        "INSERT OR IGNORE INTO event (course_id, username, path, qname, kind,"
        " action, score, ts, impersonated) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                e.course, e.username, encode_path(e.path), e.qname, e.kind,
                e.action, e.score, e.ts, 1 if e.impersonated else 0,
            )
            for e in events
        ],
    )


def upsert_state(conn, course, username, path, rows):
    p = encode_path(path)
    conn.executemany(
        "INSERT OR REPLACE INTO state (course_id, username, path, qname, score,"
        " nsubmits, last_ts) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (course, username, p, r["qname"], r["score"], r["nsubmits"], r["last_ts"])
            for r in rows
        ],
    )


def upsert_students(conn, course, roster):
    conn.executemany(
        "INSERT OR REPLACE INTO student (course_id, username, name, email, role)"
        " VALUES (?, ?, ?, ?, ?)",
        [
            (course, u, i.get("name"), i.get("email"), i.get("role"))
            for u, i in roster.items()
        ],
    )


def upsert_structure(conn, course, question_info):
    modules = [(course, encode_path(p)) for p in question_info]
    conn.executemany(
        "INSERT OR REPLACE INTO module (course_id, path) VALUES (?, ?)", modules
    )
    rows = []
    for path, questions in question_info.items():
        p = encode_path(path)
        for qname, info in questions.items():
            rows.append(
                (course, p, qname, info["qtype"], info["npoints"], info["display_name"])
            )
    conn.executemany(
        "INSERT OR REPLACE INTO exercise (course_id, path, qname, qtype, npoints,"
        " display_name) VALUES (?, ?, ?, ?, ?, ?)",
        rows,
    )


def record_anomalies(conn, anomalies):
    now = time.time()
    conn.executemany(
        "INSERT INTO anomaly (course_id, username, path, kind, detail, ts, seen_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (a.course, a.username, encode_path(a.path or ()), a.kind, a.detail, a.ts, now)
            for a in anomalies
        ],
    )


def finish_sync(conn, course, seconds):
    count = conn.execute(
        "SELECT COUNT(*) AS n FROM event WHERE course_id = ?", (course,)
    ).fetchone()["n"]
    conn.execute(
        "INSERT OR REPLACE INTO course (course_id, last_sync, sync_seconds, event_count)"
        " VALUES (?, ?, ?, ?)",
        (course, time.time(), seconds, count),
    )


def last_sync(conn, course):
    row = conn.execute(
        "SELECT last_sync, sync_seconds, event_count FROM course WHERE course_id = ?",
        (course,),
    ).fetchone()
    if not row:
        return None
    return {
        "last_sync": row["last_sync"],
        "sync_seconds": row["sync_seconds"],
        "event_count": row["event_count"],
    }


def write_snapshot(conn, course, day, values):
    conn.executemany(
        "INSERT OR REPLACE INTO snapshot (course_id, day, key, value) VALUES (?, ?, ?, ?)",
        [(course, day, k, v) for k, v in values.items()],
    )