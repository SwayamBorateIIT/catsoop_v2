"""
Extraction layer for CAT-SOOP learning analytics.

This is the *only* module that knows how CAT-SOOP stores data on disk.
Everything downstream of `normalize` operates on plain `Event` tuples and is
insulated from the log format entirely, so a change to CAT-SOOP's storage
requires a fix here and nowhere else.

Format notes (verified against a live 19.0.6 data root, not assumed):

* Logs live at `<data_root>/_logs/_courses/<course>/<user>/<path...>/<log>.log`.
* Each record is framed `[8-byte LE length][payload][8-byte LE length]`.  The
  trailing copy of the length is what lets CAT-SOOP seek backwards for
  `most_recent`; it also means a reader can checkpoint a byte offset and
  resume, which is what makes incremental extraction possible here.
* `payload` is decoded by `cslog.unprep`, which transparently handles the
  optional LZMA compression.
* `problemactions` is append-only (one record per student action).
* `problemstate` is overwritten in place (exactly one current record).
"""

import os
import struct

from .. import base_context, cslog

_HEADER = struct.Struct("<Q")

#: Records whose `action` we care about.  Anything else in `problemactions`
#: (lock/unlock/grade/revert) is staff bookkeeping, not student learning.
STUDENT_ACTIONS = frozenset({"view", "submit", "check", "save", "viewanswer"})


class ExtractionError(Exception):
    """Raised when the data root cannot be read in the way this layer expects."""


def data_root():
    return base_context.cs_data_root


def _courses_root():
    return os.path.join(data_root(), "_logs", "_courses")


def encryption_active():
    """True when log paths on disk are encrypted (names are unreadable)."""
    return getattr(cslog, "ENCRYPT_KEY", None) is not None


def check_readable():
    """Fail loudly, early, if this deployment stores logs in a shape we cannot walk."""
    if encryption_active():
        raise ExtractionError(
            "This CAT-SOOP deployment encrypts log paths, so the analytics "
            "extractor cannot discover users and modules by walking the data "
            "root.  Extraction for encrypted deployments needs a roster-driven "
            "path enumeration; see analytics/extract.py."
        )
    if not os.path.isdir(_courses_root()):
        raise ExtractionError("no _logs/_courses directory under %s" % data_root())


# ---------------------------------------------------------------- discovery


def list_courses():
    """Every course that has produced at least one log record."""
    root = _courses_root()
    if not os.path.isdir(root):
        return []
    return sorted(
        d
        for d in os.listdir(root)
        if os.path.isdir(os.path.join(root, d)) and not d.startswith("_")
    )


def list_log_users(course):
    """Usernames that appear in `course`'s log tree (i.e. have done something)."""
    root = os.path.join(_courses_root(), course)
    if not os.path.isdir(root):
        return []
    return sorted(
        d
        for d in os.listdir(root)
        if os.path.isdir(os.path.join(root, d)) and not d.startswith("_")
    )


def list_user_paths(course, username, logname):
    """
    Every page path under `course` for which `username` has a `logname` log.

    Yields path lists in CAT-SOOP's own form, e.g. `['ml101', 'assignments',
    'hw01']`, which is what `cslog` functions expect.
    """
    root = os.path.join(_courses_root(), course, username)
    target = "%s.log" % logname
    if not os.path.isdir(root):
        return
    for dirpath, _dirnames, filenames in os.walk(root):
        if target not in filenames:
            continue
        rel = os.path.relpath(dirpath, root)
        parts = [] if rel == "." else rel.split(os.sep)
        yield [course] + parts


# ------------------------------------------------------------ roster / structure


def read_roster(course):
    """
    The course roster, read from `__USERS__/*.py` the same way CAT-SOOP itself
    reads it: execute the file and keep its module-level names.

    Returns `{username: {"name":…, "email":…, "role":…, "permissions":[…]}}`.
    """
    course_dir = os.path.join(data_root(), "courses", course, "__USERS__")
    out = {}
    if not os.path.isdir(course_dir):
        return out
    for fn in sorted(os.listdir(course_dir)):
        if not fn.endswith(".py") or fn.startswith("_"):
            continue
        uname = fn[:-3]
        ns = {}
        try:
            with open(os.path.join(course_dir, fn)) as f:
                exec(f.read(), ns)
        except Exception:
            # A broken user file is a data-quality anomaly, not a crash.
            out[uname] = {"name": uname, "email": "", "role": None, "broken": True}
            continue
        out[uname] = {
            "name": ns.get("name", uname),
            "email": ns.get("email", ""),
            "role": ns.get("role"),
            "permissions": ns.get("permissions"),
            "broken": False,
        }
    return out


def read_one_question_info(path):
    """
    Course structure for a single module path, from the `question_info` log
    CAT-SOOP writes when it renders that page.

    Returns `{qname: {"qtype":…, "npoints":…, "display_name":…}}`.
    """
    rec = cslog.most_recent("_question_info", list(path), "question_info", None)
    if not rec:
        return {}
    raw = rec.get("questions")
    if isinstance(raw, str):
        try:
            raw = eval(raw, {"__builtins__": {}}, {})
        except Exception:
            return {}
    if not isinstance(raw, dict):
        return {}
    out = {}
    for qname, info in raw.items():
        if not isinstance(info, dict):
            continue
        out[qname] = {
            "qtype": info.get("qtype", "unknown"),
            "npoints": info.get("csq_npoints", 1),
            "display_name": info.get("csq_display_name", qname),
        }
    return out


def read_question_info(course):
    """
    Course structure, as CAT-SOOP recorded it while rendering pages.

    Returns `{tuple(path): {qname: {"qtype":…, "npoints":…, "display_name":…}}}`.
    This is the Exercise table of the design plan's schema, and it comes for
    free -- no page needs to be re-executed to build it.
    """
    out = {}
    for path in list_user_paths(course, "_question_info", "question_info"):
        qs = read_one_question_info(path)
        if qs:
            out[tuple(path)] = qs
    return out


# ------------------------------------------------------------ record reading


def read_actions_since(username, path, offset=0):
    """
    Read `problemactions` records for `username` at `path`, starting at byte
    `offset`.

    Returns `(records, new_offset, reset)`.  `reset` is True when the file
    turned out to be shorter than `offset` -- meaning it was truncated,
    deleted, or rebuilt -- in which case the caller should discard anything it
    had previously derived from this log and start over from the records
    returned here.

    Only whole records advance the offset, so a record being appended
    concurrently is simply picked up on the next run rather than read torn.
    """
    fname = cslog.get_log_filename(username, list(path), "problemactions")
    try:
        size = os.path.getsize(fname)
    except OSError:
        return [], 0, offset > 0

    reset = False
    if offset > size:
        offset, reset = 0, True
    if offset == size:
        return [], offset, reset

    records = []
    pos = offset
    with open(fname, "rb") as f:
        f.seek(offset)
        while True:
            head = f.read(8)
            if len(head) < 8:
                break
            (length,) = _HEADER.unpack(head)
            payload = f.read(length)
            if len(payload) < length:
                break  # torn write; stop and leave `pos` before it
            if len(f.read(8)) < 8:
                break
            try:
                records.append(cslog.unprep(payload))
            except Exception:
                records.append({"__malformed__": True, "__offset__": pos})
            pos = f.tell()
    return records, pos, reset


def read_state(username, path):
    """The single current `problemstate` record, or None."""
    return cslog.most_recent(username, list(path), "problemstate", None)


def log_mtime(username, path, logname):
    try:
        return os.path.getmtime(
            cslog.get_log_filename(username, list(path), logname)
        )
    except OSError:
        return 0.0
