"""
Normalization and validation layer.

Turns the format-specific records produced by `extract` into a small, stable
set of events.  Nothing downstream of this module knows what a `problemactions`
record looks like.

Two event kinds are produced:

* `attempt` -- a student submitted or checked a specific question.  Carries a
  score when CAT-SOOP recorded one.
* `activity` -- a student was present on a page (viewed it, saved a draft,
  revealed an answer).  No question, no score.

Validation flags bad records rather than dropping them, so a data-quality
problem shows up as a number on the dashboard instead of silently biasing a
metric (design plan, NFR Reliability).
"""

import datetime

#: Actions that carry per-question results.
_ATTEMPT_ACTIONS = frozenset({"submit", "check"})
#: Actions that only prove presence.
_ACTIVITY_ACTIONS = frozenset({"view", "save", "viewanswer"})

_TS_FORMATS = ("%Y-%m-%d:%H:%M:%S.%f", "%Y-%m-%d:%H:%M:%S")


class Event:
    """One normalized student event."""

    __slots__ = (
        "course",
        "username",
        "path",
        "qname",
        "kind",
        "action",
        "score",
        "ts",
        "impersonated",
    )

    def __init__(
        self,
        course,
        username,
        path,
        kind,
        action,
        ts,
        qname=None,
        score=None,
        impersonated=False,
    ):
        self.course = course
        self.username = username
        self.path = path          # tuple, e.g. ('ml101', 'assignments', 'hw01')
        self.kind = kind          # 'attempt' | 'activity'
        self.action = action
        self.ts = ts              # float, unix seconds
        self.qname = qname
        self.score = score        # float in [0, 1], or None
        self.impersonated = impersonated

    def __repr__(self):
        return "<Event %s %s %s %s %s score=%s>" % (
            self.kind,
            self.username,
            "/".join(self.path),
            self.qname or "-",
            self.action,
            self.score,
        )


class Anomaly:
    """A record that could not be normalized cleanly.  Counted, never hidden."""

    __slots__ = ("course", "username", "path", "kind", "detail", "ts")

    def __init__(self, course, username, path, kind, detail, ts=None):
        self.course = course
        self.username = username
        self.path = path
        self.kind = kind
        self.detail = detail
        self.ts = ts


def parse_timestamp(value):
    """
    CAT-SOOP writes `'YYYY-MM-DD:HH:MM:SS.ffffff'`.  Returns unix seconds, or
    None if the value is not a timestamp we recognise.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    for fmt in _TS_FORMATS:
        try:
            return datetime.datetime.strptime(value, fmt).timestamp()
        except ValueError:
            continue
    # `question_info` stores a bare float in a string.
    try:
        return float(value)
    except ValueError:
        return None


def normalize_score(raw):
    """
    CAT-SOOP scores are `True`/`False` for pass-fail question types and a float
    in [0, 1] for partial credit.  `None` means "recorded, not yet graded"
    (an async checker submission), which is distinct from zero.
    """
    if raw is None:
        return None
    if isinstance(raw, bool):
        return 1.0 if raw else 0.0
    if isinstance(raw, (int, float)):
        value = float(raw)
        if value != value:  # NaN
            return None
        return min(1.0, max(0.0, value))
    return None


def real_submitter(user_info):
    """
    Returns the username actually driving the request.

    When staff use CAT-SOOP's impersonation (`?as=`), `user_info` describes the
    *impersonated* student but carries a `real_user` block naming the staff
    member.  Those submissions are an instructor testing a problem, not a
    student learning, and counting them silently corrupts every per-student
    metric -- so they are marked here and filtered by default.
    """
    if not isinstance(user_info, dict):
        return None
    real = user_info.get("real_user")
    if isinstance(real, dict):
        return real.get("username")
    return None


def normalize_actions(course, username, path, records):
    """
    Convert raw `problemactions` records into events.

    Returns `(events, anomalies)`.
    """
    events = []
    anomalies = []
    path = tuple(path)

    for rec in records:
        if not isinstance(rec, dict) or rec.get("__malformed__"):
            anomalies.append(
                Anomaly(course, username, path, "unreadable_record", repr(rec)[:200])
            )
            continue

        action = rec.get("action")
        ts = parse_timestamp(rec.get("timestamp"))
        if ts is None:
            anomalies.append(
                Anomaly(
                    course, username, path, "bad_timestamp",
                    repr(rec.get("timestamp"))[:120],
                )
            )
            continue

        real = real_submitter(rec.get("user_info"))
        impersonated = bool(real) and real != username

        if action in _ATTEMPT_ACTIONS:
            names = rec.get("names") or []
            scores = rec.get("scores") or {}
            if not isinstance(names, (list, tuple)):
                anomalies.append(
                    Anomaly(course, username, path, "bad_names", repr(names)[:120], ts)
                )
                continue
            if not names:
                anomalies.append(
                    Anomaly(course, username, path, "attempt_without_question", action, ts)
                )
                continue
            for qname in names:
                events.append(
                    Event(
                        course, username, path, "attempt", action, ts,
                        qname=qname,
                        score=normalize_score(
                            scores.get(qname) if isinstance(scores, dict) else None
                        ),
                        impersonated=impersonated,
                    )
                )
        elif action in _ACTIVITY_ACTIONS:
            events.append(
                Event(
                    course, username, path, "activity", action, ts,
                    impersonated=impersonated,
                )
            )
        elif action is None:
            anomalies.append(
                Anomaly(course, username, path, "missing_action", repr(rec)[:160], ts)
            )
        # Anything else (lock/unlock/grade/revert) is staff bookkeeping and is
        # deliberately not an event.

    return events, anomalies


def normalize_state(course, username, path, state):
    """
    Convert one `problemstate` record into per-question current standing.

    `problemstate` is CAT-SOOP's own authoritative view of where a student
    currently stands: `nsubmits_used` is the attempt count the gating logic
    itself uses, and `scores` is the score that counts.  Deriving these by
    replaying `problemactions` would be a reimplementation that could drift
    from CAT-SOOP's behaviour, so we read them directly instead.

    Returns `(rows, anomalies)` where each row is a dict keyed by question.
    """
    rows = []
    anomalies = []
    path = tuple(path)
    if not isinstance(state, dict):
        return rows, anomalies

    scores = state.get("scores") or {}
    nsubmits = state.get("nsubmits_used") or {}
    last_times = state.get("last_submit_times") or {}
    last_action = state.get("last_action") or {}

    if not isinstance(scores, dict):
        anomalies.append(
            Anomaly(course, username, path, "bad_state_scores", repr(scores)[:120])
        )
        return rows, anomalies

    for qname in set(scores) | set(nsubmits):
        rows.append(
            {
                "qname": qname,
                "score": normalize_score(scores.get(qname)),
                "nsubmits": int(nsubmits.get(qname) or 0),
                "last_ts": parse_timestamp(last_times.get(qname)),
                "last_action": last_action.get(qname),
            }
        )
    return rows, anomalies
