"""
Command-line entry point for the analytics pipeline.

    python -m catsoop.analytics sync            # every course
    python -m catsoop.analytics sync ml101      # one course
    python -m catsoop.analytics status          # what the store holds
    python -m catsoop.analytics pending ml101   # changed logs, reads nothing

Reading the logs as tables, straight from disk rather than from the store:

    python -m catsoop.analytics table ml101              # one row per student
    python -m catsoop.analytics table ml101 riya         # that student's history
    python -m catsoop.analytics table ml101 riya --state # where she stands now
    python -m catsoop.analytics table ml101 riya --csv > riya.csv
    python -m catsoop.analytics table ml101 riya --md

Options: --state (current standing), --no-views (submissions only),
--no-staff (exclude staff impersonation), --csv, --md, --limit N

`sync` is what a scheduler runs for the daily batch, e.g.

    17 3 * * *  /path/to/python -m catsoop.analytics sync >> /var/log/catsoop-analytics.log 2>&1

It is safe to run concurrently with the web server (the store is WAL-mode) and
safe to run twice (a second run reads no records).
"""

import sys
import time

from . import engine, extract, store, sync, tabulate


def _cmd_sync(args):
    courses = args or extract.list_courses()
    failed = False
    for course in courses:
        report = sync.sync_course(course, lock_timeout=sync.BATCH_LOCK_TIMEOUT)
        print(report)
        if report.error:
            failed = True
            print("  ERROR: %s" % report.error, file=sys.stderr)
    return 1 if failed else 0


def _cmd_pending(args):
    for course in args or extract.list_courses():
        print("%-20s %d changed log(s)" % (course, sync.count_pending(course)))
    return 0


def _cmd_status(args):
    conn = store.connect()
    try:
        print("store: %s" % store.db_path())
        for course in args or extract.list_courses():
            meta = store.last_sync(conn, course)
            if not meta or not meta["last_sync"]:
                print("%-20s never synced" % course)
                continue
            m = engine.course_overview(conn, course)
            print(
                "%-20s synced %s (%.2fs) | %d events | %d students | "
                "completion %s | at risk %d"
                % (
                    course,
                    time.strftime("%Y-%m-%d %H:%M", time.localtime(meta["last_sync"])),
                    meta["sync_seconds"] or 0.0,
                    meta["event_count"] or 0,
                    m["total_students"],
                    "—" if m["completion"] is None else "%.1f%%" % (m["completion"] * 100),
                    m["at_risk"],
                )
            )
    finally:
        conn.close()
    return 0


def _cmd_table(args):
    """
    Print a student's logs, or the roster, as a table.

    Reads the log files directly, not the analytics store, so the output can
    be used to check the store rather than merely repeating it.
    """
    flags = {a for a in args if a.startswith("-")}
    positional = [a for a in args if not a.startswith("-")]
    if not positional:
        print("usage: table <course> [student] [options]", file=sys.stderr)
        return 2

    course = positional[0]
    student = positional[1] if len(positional) > 1 else None
    fmt = "csv" if "--csv" in flags else "md" if "--md" in flags else "text"

    limit = None
    for a in args:
        if a.startswith("--limit"):
            try:
                limit = int(a.split("=", 1)[1]) if "=" in a else int(
                    args[args.index(a) + 1]
                )
            except (ValueError, IndexError):
                print("--limit needs a number", file=sys.stderr)
                return 2

    try:
        if student is None:
            rows = tabulate.roster(
                course, include_staff="--no-staff" not in flags
            )
            title = "%s: roster" % course
        elif "--state" in flags:
            rows = tabulate.state(course, student)
            title = "%s / %s: current standing" % (course, student)
        else:
            rows = tabulate.events(
                course, student,
                include_views="--no-views" not in flags,
                include_staff="--no-staff" not in flags,
            )
            title = "%s / %s: activity history" % (course, student)
    except extract.ExtractionError as exc:
        print("extraction error: %s" % exc, file=sys.stderr)
        return 1

    if limit:
        rows = rows[:limit]
    if fmt == "text":
        print(title)
        print("=" * len(title))
    print(tabulate.render(rows, fmt))
    if fmt == "text":
        print()
        print("%d row%s" % (len(rows), "" if len(rows) == 1 else "s"))
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__.strip())
        return 0
    command, args = argv[0], argv[1:]
    handlers = {
        "sync": _cmd_sync,
        "pending": _cmd_pending,
        "status": _cmd_status,
        "table": _cmd_table,
    }
    if command not in handlers:
        print("unknown command: %s (try --help)" % command, file=sys.stderr)
        return 2
    try:
        return handlers[command](args)
    except extract.ExtractionError as exc:
        print("extraction error: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())