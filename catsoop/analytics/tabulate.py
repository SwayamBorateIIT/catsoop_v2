"""
Lay out each student's saved logs as a table.

This is the inspection layer that sits *before* the dashboard.  CAT-SOOP keeps
student activity as length-prefixed binary records in nested folders, which is
efficient but impossible to read.  This module turns those records into plain
rows and columns, so you can see what a student actually did and check any
dashboard number against the thing it came from.

It deliberately reads **the log files**, not the analytics database.  Reading
the database would only echo what the dashboard already believes; reading the
logs independently means the two can be compared, which is what catches an
extraction bug rather than hiding it.

Three views:

* `events`  -- one row per action.  The full history: every page view, every
               submission, in order.
* `state`   -- one row per question.  Where the student stands right now,
               taken from CAT-SOOP's own `problemstate` record.
* `roster`  -- one row per student.  A summary, for finding who to look at.

The roster deliberately shows `submissions`, `attempts` and `staff` side by
side, because they can legitimately disagree: `submissions` counts entries in
the append-only action history, `attempts` is CAT-SOOP's own counter from the
overwritten `problemstate`, and `staff` counts submissions a staff member made
while impersonating.  Seeing all three is how you tell a real discrepancy from
an explained one.

Each renders as an aligned text table, CSV, or Markdown.
"""

import csv
import io
import time

from . import extract, normalize


# --------------------------------------------------------------- gathering


def student_modules(course, username):
    """Every module path this student has any activity log for."""
    seen = {}
    for logname in ("problemactions", "problemstate"):
        for path in extract.list_user_paths(course, username, logname):
            seen[tuple(path)] = True
    return sorted(seen, key=lambda p: "/".join(p))


def events(course, username, include_views=True, include_staff=True):
    """
    One row per recorded action, oldest first.

    Set `include_views=False` to see only submissions, and
    `include_staff=False` to drop actions a staff member made while
    impersonating this student.
    """
    rows = []
    for path in student_modules(course, username):
        records, _offset, _reset = extract.read_actions_since(username, path, 0)
        evs, anomalies = normalize.normalize_actions(
            course, username, path, records
        )
        for e in evs:
            if e.kind == "activity" and not include_views:
                continue
            if e.impersonated and not include_staff:
                continue
            rows.append(
                {
                    "when": _when(e.ts),
                    "module": _module(e.path),
                    "question": e.qname or "",
                    "action": e.action,
                    # An activity event has no question, so it has no score;
                    # "not graded" would wrongly imply one is pending.
                    "score": _score(e.score) if e.kind == "attempt" else "",
                    "by": "staff" if e.impersonated else "student",
                    "_ts": e.ts,
                }
            )
        for a in anomalies:
            rows.append(
                {
                    "when": _when(a.ts) if a.ts else "?",
                    "module": _module(a.path),
                    "question": "",
                    "action": "UNREADABLE: %s" % a.kind,
                    "score": "",
                    "by": "",
                    "_ts": a.ts or 0,
                }
            )
    rows.sort(key=lambda r: r["_ts"])
    for r in rows:
        r.pop("_ts", None)
    return rows


def state(course, username):
    """
    One row per question: where this student currently stands.

    Taken from `problemstate`, which is CAT-SOOP's own authoritative record --
    `nsubmits_used` is the counter its "you have N tries left" logic uses, so
    these numbers are what the student was actually shown.
    """
    structure = extract.read_question_info(course)
    rows = []
    for path in student_modules(course, username):
        raw = extract.read_state(username, path)
        if not raw:
            continue
        srows, _anomalies = normalize.normalize_state(
            course, username, path, raw
        )
        qmeta = structure.get(tuple(path), {})
        for r in srows:
            info = qmeta.get(r["qname"], {})
            rows.append(
                {
                    "module": _module(path),
                    "question": r["qname"],
                    "type": info.get("qtype", "?"),
                    "score": _score(r["score"]),
                    "attempts": r["nsubmits"],
                    "last action": r["last_action"] or "",
                    "last submitted": _when(r["last_ts"]) if r["last_ts"] else "",
                }
            )
    rows.sort(key=lambda r: (r["module"], r["question"]))
    return rows


def roster(course, include_staff=False):
    """
    One row per student: a summary, to find who is worth looking at.

    Reads every student's logs, so it is the slowest of the three views.
    """
    people = extract.read_roster(course)
    log_users = set(extract.list_log_users(course))
    rows = []
    for uname in sorted(set(people) | log_users):
        if uname.startswith("_"):
            continue
        info = people.get(uname, {})
        role = (info.get("role") or "unknown").strip()
        evs = events(course, uname, include_staff=True)
        srows = state(course, uname)
        submits = [e for e in evs if e["action"] in ("submit", "check")]
        by_staff = [e for e in submits if e["by"] == "staff"]
        if not include_staff:
            submits = [e for e in submits if e["by"] != "staff"]
            evs = [e for e in evs if e["by"] != "staff"]
        rows.append(
            {
                "username": uname,
                "name": info.get("name") or uname,
                "role": role or "unknown",
                "modules": len({r["module"] for r in srows}),
                "questions": len(srows),
                "submissions": len(submits),
                # `attempts` is CAT-SOOP's own counter from problemstate.  It
                # can be lower than `submissions`, because problemstate is
                # overwritten while the action history is only appended to.
                # The `staff` column usually accounts for the difference.
                "attempts": sum(r["attempts"] for r in srows),
                "staff": len(by_staff),
                "last seen": evs[-1]["when"] if evs else "never",
            }
        )
    return rows


# --------------------------------------------------------------- formatting


def _when(ts):
    if not ts:
        return ""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def _module(path):
    """Drop the course prefix: ('ml101','assignments','hw01') -> assignments/hw01."""
    parts = list(path)
    if len(parts) > 1:
        parts = parts[1:]
    return "/".join(parts) or "(course root)"


def _score(value):
    if value is None:
        return "not graded"
    if value in (0.0, 1.0):
        return "correct" if value else "wrong"
    return "%.0f%%" % (value * 100)


def as_text(rows, columns=None, max_width=40):
    """An aligned plain-text table.  Returns '(no rows)' when empty."""
    if not rows:
        return "(no rows)"
    columns = columns or list(rows[0].keys())

    def cell(r, c):
        v = str(r.get(c, ""))
        return v if len(v) <= max_width else v[: max_width - 1] + "…"

    widths = {
        c: max(len(c), max((len(cell(r, c)) for r in rows), default=0))
        for c in columns
    }
    line = "  ".join(c.upper().ljust(widths[c]) for c in columns).rstrip()
    rule = "  ".join("-" * widths[c] for c in columns)
    out = [line, rule]
    for r in rows:
        out.append(
            "  ".join(cell(r, c).ljust(widths[c]) for c in columns).rstrip()
        )
    return "\n".join(out)


def as_csv(rows, columns=None):
    if not rows:
        return ""
    columns = columns or list(rows[0].keys())
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


def as_markdown(rows, columns=None):
    if not rows:
        return "_(no rows)_"
    columns = columns or list(rows[0].keys())
    out = ["| " + " | ".join(columns) + " |",
           "|" + "|".join("---" for _ in columns) + "|"]
    for r in rows:
        out.append(
            "| " + " | ".join(str(r.get(c, "")).replace("|", "\\|")
                              for c in columns) + " |"
        )
    return "\n".join(out)


RENDERERS = {"text": as_text, "csv": as_csv, "md": as_markdown}


def render(rows, fmt="text", columns=None):
    if fmt not in RENDERERS:
        raise ValueError("unknown format %r; try one of %s"
                         % (fmt, ", ".join(sorted(RENDERERS))))
    return RENDERERS[fmt](rows, columns)