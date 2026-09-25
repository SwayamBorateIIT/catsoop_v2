"""
Synchronization: read CAT-SOOP's logs, normalize, load into the analytics store.

This is the "ETL service" of the design plan, collapsed into a function that
can be called three ways:

* from cron / a scheduler, for the daily batch (design plan, FR14);
* from the CLI, `python -m catsoop.analytics sync`;
* inline from the dashboard page, when the amount of new data is small enough
  to absorb in a page load.

Incrementality is what makes the third option viable.  Every log file has a
stored `(offset, mtime)` cursor; a file whose mtime is unchanged is not opened
at all, and a file that grew is read only from its previous offset.  A sync
that finds nothing new therefore costs one `stat` per log file and no parsing,
which is what keeps the dashboard responsive as a course grows.

Sync is idempotent: running it twice in a row produces the same store, because
the second run reads zero records.
"""

import time

from . import extract, normalize, store


class SyncReport:
    def __init__(self, course):
        self.course = course
        self.logs_seen = 0
        self.logs_read = 0
        self.records = 0
        self.events = 0
        self.anomalies = 0
        self.resets = 0
        self.seconds = 0.0
        self.error = None

    def as_dict(self):
        return {
            "course": self.course,
            "logs_seen": self.logs_seen,
            "logs_read": self.logs_read,
            "records": self.records,
            "events": self.events,
            "anomalies": self.anomalies,
            "resets": self.resets,
            "seconds": round(self.seconds, 3),
            "error": self.error,
        }

    def __repr__(self):
        return "<SyncReport %s: %d/%d logs, %d events, %d anomalies in %.2fs>" % (
            self.course, self.logs_read, self.logs_seen, self.events,
            self.anomalies, self.seconds,
        )


def count_pending(course):
    """
    How many logs have changed since the last sync, without reading any of them.

    Costs one `stat` per log file.  The dashboard uses this to decide whether
    to sync inline or serve the cached numbers.
    """
    extract.check_readable()
    conn = store.connect()
    try:
        cursors = store.get_cursors(conn, course)
    finally:
        conn.close()

    pending = 0
    for username in extract.list_log_users(course):
        if username.startswith("_"):
            continue
        for path in extract.list_user_paths(course, username, "problemactions"):
            key = (username, store.encode_path(path), "problemactions")
            known = cursors.get(key, (0, 0.0))
            if extract.log_mtime(username, path, "problemactions") > known[1]:
                pending += 1
    return pending


def sync_course(course, conn=None):
    """Bring the analytics store up to date for one course.  Returns a SyncReport."""
    report = SyncReport(course)
    started = time.time()
    own_conn = conn is None
    try:
        extract.check_readable()
        conn = conn or store.connect()
        # A sync changes what the engine's per-request caches describe, so any
        # memo on this connection is stale the moment new records land.
        if hasattr(conn, "_csa_memo"):
            conn._csa_memo.clear()

        cursors = store.get_cursors(conn, course)

        # --- roster and course structure ------------------------------------
        # The roster is a handful of small files, so it is re-read every time.
        # `question_info` is not: decoding every module's copy costs about as
        # much as the entire rest of a no-op sync, so each one is cursored by
        # mtime and only re-read when the page behind it was re-rendered.
        roster = extract.read_roster(course)
        if roster:
            store.upsert_students(conn, course, roster)
        report.logs_read += _sync_structure(conn, course, cursors, report)
        all_anomalies = []

        for username in extract.list_log_users(course):
            if username.startswith("_"):
                continue

            # --- problemactions: append-only, resumed from a byte offset ----
            for path in extract.list_user_paths(course, username, "problemactions"):
                report.logs_seen += 1
                encoded = store.encode_path(path)
                key = (username, encoded, "problemactions")
                offset, known_mtime = cursors.get(key, (0, 0.0))
                mtime = extract.log_mtime(username, path, "problemactions")
                if mtime <= known_mtime:
                    continue  # untouched since last sync; never opened

                report.logs_read += 1
                records, new_offset, reset = extract.read_actions_since(
                    username, path, offset
                )
                if reset:
                    report.resets += 1
                    store.drop_events(conn, course, username, path)
                    all_anomalies.append(
                        normalize.Anomaly(
                            course, username, tuple(path), "log_reset",
                            "log shrank below stored offset; events rebuilt",
                            time.time(),
                        )
                    )
                report.records += len(records)
                events, anomalies = normalize.normalize_actions(
                    course, username, path, records
                )
                if events:
                    store.insert_events(conn, events)
                    report.events += len(events)
                all_anomalies.extend(anomalies)
                store.set_cursor(
                    conn, course, username, path, "problemactions", new_offset, mtime
                )

            # --- problemstate: overwritten, so re-read whenever it moved ----
            for path in extract.list_user_paths(course, username, "problemstate"):
                report.logs_seen += 1
                encoded = store.encode_path(path)
                key = (username, encoded, "problemstate")
                _, known_mtime = cursors.get(key, (0, 0.0))
                mtime = extract.log_mtime(username, path, "problemstate")
                if mtime <= known_mtime:
                    continue

                report.logs_read += 1
                state = extract.read_state(username, path)
                rows, anomalies = normalize.normalize_state(
                    course, username, path, state
                )
                if rows:
                    store.upsert_state(conn, course, username, path, rows)
                all_anomalies.extend(anomalies)
                store.set_cursor(
                    conn, course, username, path, "problemstate", 0, mtime
                )

        if all_anomalies:
            store.record_anomalies(conn, all_anomalies)
            report.anomalies = len(all_anomalies)

        report.seconds = time.time() - started
        store.finish_sync(conn, course, report.seconds)
        _write_daily_snapshot(conn, course)
        conn.commit()
    except Exception as exc:  # a failed sync must not take the dashboard down
        report.error = "%s: %s" % (type(exc).__name__, exc)
        report.seconds = time.time() - started
    finally:
        if own_conn and conn is not None:
            conn.close()
    return report


def _sync_structure(conn, course, cursors, report):
    """
    Refresh the module/exercise tables from the `question_info` logs, skipping
    any whose file has not changed since the last sync.  Returns the number of
    logs actually opened.
    """
    read = 0
    changed = {}
    for path in extract.list_user_paths(course, "_question_info", "question_info"):
        report.logs_seen += 1
        key = ("_question_info", store.encode_path(path), "question_info")
        _, known_mtime = cursors.get(key, (0, 0.0))
        mtime = extract.log_mtime("_question_info", path, "question_info")
        if mtime <= known_mtime:
            continue
        read += 1
        info = extract.read_one_question_info(path)
        if info:
            changed[tuple(path)] = info
        store.set_cursor(
            conn, course, "_question_info", path, "question_info", 0, mtime
        )
    if changed:
        store.upsert_structure(conn, course, changed)
    return read

def _write_daily_snapshot(conn, course):
    """
    Persist today's course-level rollup so trend views never recompute history
    (design plan, FR9/FR12).  Re-running on the same day overwrites that day.
    """
    from . import engine

    day = time.strftime("%Y-%m-%d")
    m = engine.course_overview(conn, course)
    store.write_snapshot(
        conn,
        course,
        day,
        {
            "students_active": m["active_students"],
            "students_total": m["total_students"],
            "mean_score": m["mean_score"] or 0.0,
            "completion": m["completion"] or 0.0,
            "attempts": m["total_attempts"],
        },
    )


def sync_all():
    """Sync every course that has logs.  This is what the daily job runs."""
    reports = []
    for course in extract.list_courses():
        reports.append(sync_course(course))
    return reports
