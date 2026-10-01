"""
Analytics engine: course-, module-, question-, and student-level metrics.

Every function here reads the analytics store and nothing else -- never
CAT-SOOP's log files (design plan, §3.2).  Aggregation is pushed into SQL
wherever it can be, so cost scales with the size of the result rather than the
size of the cohort.

Two conventions used throughout:

* `mastery` is the score at or above which a question counts as done.  It
  defaults to 0.999 so that a partially-credited answer is not silently
  counted as complete.
* Staff submissions made while impersonating a student are excluded by default.
  They are CAT-SOOP's `?as=` feature, used for testing problems, and counting
  them inflates every metric for whichever students were used as test subjects.
"""

import time

MASTERY = 0.999
#: Gap beyond which two consecutive events are treated as separate sittings
#: rather than continuous work.  15 minutes.
IDLE_CUTOFF = 900.0
ACTIVE_WINDOW_DAYS = 7
STALE_DAYS = 14


def _imp(include_impersonated):
    return "" if include_impersonated else " AND impersonated = 0"


def _median(values):
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    n = len(vals)
    mid = n // 2
    return vals[mid] if n % 2 else (vals[mid - 1] + vals[mid]) / 2.0


def _mean(values):
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


def _stdev(values):
    vals = [v for v in values if v is not None]
    if len(vals) < 2:
        return 0.0
    mu = sum(vals) / len(vals)
    return (sum((v - mu) ** 2 for v in vals) / (len(vals) - 1)) ** 0.5


# --------------------------------------------------------------- the cohort


def students(conn, course):
    """
    The cohort: everyone with the Student role on the roster, plus anyone who
    has produced events but is not on the roster (so nobody silently vanishes
    from the numbers).  Guests are excluded per the design plan, §1.3.
    """
    return _memo(conn, ("students", course), lambda: _students(conn, course))


def _students(conn, course):
    rows = conn.execute(
        "SELECT username, name, email, role FROM student WHERE course_id = ?",
        (course,),
    ).fetchall()
    out = {}
    for r in rows:
        role = (r["role"] or "").strip()
        if role and role.lower() in ("guest", "admin", "instructor", "ta"):
            continue
        out[r["username"]] = {
            "username": r["username"],
            "name": r["name"] or r["username"],
            "email": r["email"] or "",
            "role": role or "Student",
            "on_roster": True,
        }
    seen = conn.execute(
        "SELECT DISTINCT username FROM event WHERE course_id = ?", (course,)
    ).fetchall()
    roster_roles = {r["username"]: (r["role"] or "") for r in rows}
    for r in seen:
        u = r["username"]
        if u in out:
            continue
        if roster_roles.get(u, "").lower() in ("guest", "admin", "instructor", "ta"):
            continue
        if u in roster_roles:
            continue
        out[u] = {
            "username": u, "name": u, "email": "", "role": "unknown",
            "on_roster": False,
        }
    return out


def _memo(conn, key, build):
    """
    Cache a derived table on the connection for the life of one request.

    Several metrics are built from the same intermediate results -- the
    at-risk list is derived from the student table, and the course overview
    quotes the at-risk count -- so without this a single dashboard render
    recomputes the student table four times.  The cache lives on the
    connection object, so it lasts exactly as long as the request that opened
    it and can never serve one request's numbers to another.
    """
    cache = getattr(conn, "_csa_memo", None)
    if cache is None:
        cache = {}
        try:
            conn._csa_memo = cache
        except AttributeError:
            return build()
    if key not in cache:
        cache[key] = build()
    return cache[key]


_EXCLUDED_ROLES = ("guest", "admin", "instructor", "ta")


def _excluded_usernames(conn, course):
    """
    Roster accounts that are not part of the cohort (staff and guests).

    Returned so aggregation can be pushed into SQL: the exclusion list is the
    size of the staff, not of the class, so `NOT IN` stays cheap as the cohort
    grows.
    """
    rows = conn.execute(
        "SELECT username, role FROM student WHERE course_id = ?", (course,)
    ).fetchall()
    return [r["username"] for r in rows
            if (r["role"] or "").strip().lower() in _EXCLUDED_ROLES]


def _cohort_sql(conn, course):
    """Returns `(sql_fragment, params)` restricting a query to the cohort."""
    excluded = _excluded_usernames(conn, course)
    if not excluded:
        return "", []
    placeholders = ",".join("?" * len(excluded))
    return " AND username NOT IN (%s)" % placeholders, excluded


def exercises(conn, course):
    return _memo(conn, ("exercises", course), lambda: _exercises(conn, course))


def _exercises(conn, course):
    rows = conn.execute(
        "SELECT path, qname, qtype, npoints, display_name FROM exercise"
        " WHERE course_id = ? ORDER BY path, qname",
        (course,),
    ).fetchall()
    return [dict(r) for r in rows]


def graded_modules(conn, course):
    """Module paths that actually contain questions.  Pages with none are not progress."""
    rows = conn.execute(
        "SELECT DISTINCT path FROM exercise WHERE course_id = ? ORDER BY path",
        (course,),
    ).fetchall()
    return [r["path"] for r in rows]


# ------------------------------------------------------------------ FR1


def course_overview(conn, course, include_impersonated=False, mastery=MASTERY):
    cohort = students(conn, course)
    usernames = set(cohort)
    n_students = len(usernames)
    ex = exercises(conn, course)
    n_exercises = len(ex)

    cutoff = time.time() - ACTIVE_WINDOW_DAYS * 86400
    active = {
        r["username"]
        for r in conn.execute(
            "SELECT DISTINCT username FROM event WHERE course_id = ? AND ts >= ?"
            + _imp(include_impersonated),
            (course, cutoff),
        )
        if r["username"] in usernames
    }

    state_rows = conn.execute(
        "SELECT username, path, qname, score, nsubmits FROM state WHERE course_id = ?",
        (course,),
    ).fetchall()
    state_rows = [r for r in state_rows if r["username"] in usernames]

    scores = [r["score"] for r in state_rows if r["score"] is not None]
    mastered = sum(1 for r in state_rows if (r["score"] or 0) >= mastery)
    attempts = sum(r["nsubmits"] or 0 for r in state_rows)

    denom = n_students * n_exercises
    excluded = conn.execute(
        "SELECT COUNT(*) AS n FROM event WHERE course_id = ? AND impersonated = 1",
        (course,),
    ).fetchone()["n"]

    return {
        "total_students": n_students,
        "active_students": len(active),
        "total_exercises": n_exercises,
        "total_attempts": attempts,
        "attempted_pairs": len(state_rows),
        "mastered_pairs": mastered,
        "completion": (mastered / denom) if denom else None,
        "mean_score": _mean(scores),
        "median_score": _median(scores),
        "impersonated_events": excluded,
        "at_risk": len(at_risk(conn, course, include_impersonated, mastery)),
    }


# ------------------------------------------------------------------ FR2


def module_progress(conn, course, include_impersonated=False, mastery=MASTERY):
    """Per module: how many students reached it, and how many finished it."""
    cohort = set(students(conn, course))
    if not cohort:
        return []

    per_module_q = {}
    for e in exercises(conn, course):
        per_module_q.setdefault(e["path"], set()).add(e["qname"])

    reached = {}
    for r in conn.execute(
        "SELECT DISTINCT path, username FROM event WHERE course_id = ?"
        + _imp(include_impersonated),
        (course,),
    ):
        if r["username"] in cohort:
            reached.setdefault(r["path"], set()).add(r["username"])

    mastered = {}
    for r in conn.execute(
        "SELECT path, username, qname FROM state WHERE course_id = ? AND score >= ?",
        (course, mastery),
    ):
        if r["username"] in cohort:
            mastered.setdefault((r["path"], r["username"]), set()).add(r["qname"])

    out = []
    for path in sorted(per_module_q):
        qs = per_module_q[path]
        got = reached.get(path, set())
        done = sum(
            1 for u in cohort if mastered.get((path, u), set()) >= qs
        )
        out.append(
            {
                "path": path,
                "questions": len(qs),
                "reached": len(got),
                "completed": done,
                "reach_pct": len(got) / len(cohort),
                "completion_pct": done / len(cohort),
            }
        )
    return out


# ------------------------------------------------------------ FR7 / FR8


def question_difficulty(conn, course, include_impersonated=False, mastery=MASTERY):
    """
    Per-question difficulty.

    The heuristic combines three independent signals, because no one of them
    is sufficient on its own: a question everyone eventually gets right after
    six tries is hard even though its mean score is 1.0, and a question with a
    low mean score but one attempt each may simply be one nobody tried twice.

        difficulty = 0.50 * (1 - mean_score)
                   + 0.30 * fail_rate
                   + 0.20 * min(1, (mean_attempts - 1) / 4)

    Weights are a judgement call, stated here rather than buried, and the
    component values are shown alongside the index on the dashboard so an
    instructor can disagree with the weighting and still read the evidence.
    """
    cohort = set(students(conn, course))
    rows = conn.execute(
        "SELECT path, qname, username, score, nsubmits FROM state WHERE course_id = ?",
        (course,),
    ).fetchall()
    meta = {(e["path"], e["qname"]): e for e in exercises(conn, course)}

    grouped = {}
    for r in rows:
        if r["username"] not in cohort:
            continue
        grouped.setdefault((r["path"], r["qname"]), []).append(r)

    out = []
    for key, rs in grouped.items():
        scores = [r["score"] for r in rs if r["score"] is not None]
        attempts = [r["nsubmits"] or 0 for r in rs]
        mean_score = _mean(scores)
        if mean_score is None:
            continue
        fail_rate = sum(1 for s in scores if s < mastery) / len(scores)
        mean_attempts = _mean(attempts) or 0.0
        difficulty = (
            0.50 * (1 - mean_score)
            + 0.30 * fail_rate
            + 0.20 * min(1.0, max(0.0, (mean_attempts - 1) / 4.0))
        )
        info = meta.get(key, {})
        out.append(
            {
                "path": key[0],
                "qname": key[1],
                "display_name": info.get("display_name") or key[1],
                "qtype": info.get("qtype") or "unknown",
                "n_students": len(rs),
                "mean_score": mean_score,
                "fail_rate": fail_rate,
                "mean_attempts": mean_attempts,
                "difficulty": difficulty,
            }
        )
    out.sort(key=lambda d: -d["difficulty"])
    return out


# ------------------------------------------------- question discrimination


#: Fraction of the cohort in each comparison group.  0.5 means "top half
#: against bottom half", which is the easiest version to explain and needs no
#: statistics beyond counting.  Classical item analysis often uses 0.27.
DISCRIM_GROUP = 0.5


def question_discrimination(conn, course, include_impersonated=False,
                            mastery=MASTERY):
    """
    Does each question separate stronger students from weaker ones?

    Difficulty alone cannot tell you whether a question is *working*.  A
    question everybody passes and a question everybody fails are both useless
    for telling students apart, however different their difficulty scores look.

    The measure is the classical discrimination index, computed the simple way:
    rank students by their overall mean score, split into a top group and a
    bottom group, and subtract.

        D = (top group who got it right) - (bottom group who got it right)

    Read it as:

        D >= 0.40   excellent    strong students pass, weak ones do not
        0.20-0.39   acceptable
        0.00-0.19   weak         barely separates anyone
        D < 0       suspect      *stronger* students do worse, which usually
                                 means a mis-keyed answer or misleading wording

    A negative D is the useful alarm, and the reason this is worth having
    alongside difficulty: it points at questions that are not merely hard but
    probably wrong.
    """
    cohort = set(students(conn, course))
    if not cohort:
        return []

    rows = conn.execute(
        "SELECT username, path, qname, score FROM state"
        " WHERE course_id = ? AND score IS NOT NULL",
        (course,),
    ).fetchall()
    rows = [r for r in rows if r["username"] in cohort]
    if not rows:
        return []

    # Each student's overall standing, used only for ranking.
    per_student = {}
    for r in rows:
        per_student.setdefault(r["username"], []).append(r["score"])
    ranked = sorted(
        per_student,
        key=lambda u: sum(per_student[u]) / len(per_student[u]),
        reverse=True,
    )
    # Floor division, so the two groups can never overlap.  With an odd number
    # of students the middle one is left out of both rather than counted in
    # each, which would compare a student against themselves.
    group_n = int(len(ranked) * DISCRIM_GROUP)
    if group_n < 2:
        return []           # too few students for the comparison to mean much
    top, bottom = set(ranked[:group_n]), set(ranked[-group_n:])
    assert not (top & bottom), "comparison groups must be disjoint"

    meta = {(e["path"], e["qname"]): e for e in exercises(conn, course)}
    grouped = {}
    for r in rows:
        grouped.setdefault((r["path"], r["qname"]), []).append(r)

    out = []
    for key, rs in grouped.items():
        t = [r for r in rs if r["username"] in top]
        b = [r for r in rs if r["username"] in bottom]
        if not t or not b:
            continue        # not attempted by both groups; nothing to compare
        t_pass = sum(1 for r in t if r["score"] >= mastery) / len(t)
        b_pass = sum(1 for r in b if r["score"] >= mastery) / len(b)
        info = meta.get(key, {})
        out.append(
            {
                "path": key[0],
                "qname": key[1],
                "qtype": info.get("qtype") or "unknown",
                "top_pass": t_pass,
                "bottom_pass": b_pass,
                "discrimination": t_pass - b_pass,
                "n_top": len(t),
                "n_bottom": len(b),
                "group_size": group_n,
            }
        )
    # Worst first: a negative value is the thing worth looking at.
    out.sort(key=lambda d: d["discrimination"])
    return out


def discrimination_band(value):
    """(label, status-colour-token) for a discrimination value."""
    if value is None:
        return "no data", "var(--ink-soft)"
    if value < 0:
        return "suspect", "var(--critical)"
    if value < 0.20:
        return "weak", "var(--serious)"
    if value < 0.40:
        return "acceptable", "var(--series-1)"
    return "excellent", "var(--good)"


# ------------------------------------------------------------------ FR6


def score_distribution(conn, course, bins=10, mastery=MASTERY):
    """Histogram of per-student mean score across everything they attempted."""
    cohort = set(students(conn, course))
    per_student = {}
    for r in conn.execute(
        "SELECT username, score FROM state WHERE course_id = ? AND score IS NOT NULL",
        (course,),
    ):
        if r["username"] in cohort:
            per_student.setdefault(r["username"], []).append(r["score"])

    counts = [0] * bins
    for u, scores in per_student.items():
        m = sum(scores) / len(scores)
        ix = min(bins - 1, int(m * bins))
        counts[ix] += 1
    return {
        "bins": bins,
        "counts": counts,
        "n": len(per_student),
        "labels": [
            "%d–%d%%" % (i * 100 // bins, (i + 1) * 100 // bins) for i in range(bins)
        ],
    }


# ------------------------------------------------------------------ FR5


def attempt_distribution(conn, course):
    """How many (student, question) pairs took 1, 2, 3, … attempts."""
    cohort = set(students(conn, course))
    buckets = {}
    for r in conn.execute(
        "SELECT username, nsubmits FROM state WHERE course_id = ?", (course,)
    ):
        if r["username"] not in cohort:
            continue
        n = r["nsubmits"] or 0
        if n <= 0:
            continue
        key = min(n, 6)
        buckets[key] = buckets.get(key, 0) + 1
    return [
        {"attempts": k, "label": "6+" if k == 6 else str(k), "count": buckets.get(k, 0)}
        for k in range(1, 7)
    ]


# ------------------------------------------------------------------ FR9


def activity_series(conn, course, days=30, include_impersonated=False):
    """
    Daily attempt counts and distinct active students, for the trend view.

    Grouped in SQL rather than in Python: at cohort scale this query returns
    one row per day instead of one row per event, which is the difference
    between reading thirty rows and reading a few hundred thousand.
    """
    cutoff = time.time() - days * 86400
    cohort_sql, cohort_params = _cohort_sql(conn, course)
    rows = conn.execute(
        "SELECT date(ts, 'unixepoch', 'localtime') AS day,"
        " SUM(CASE WHEN kind = 'attempt' THEN 1 ELSE 0 END) AS attempts,"
        " COUNT(DISTINCT username) AS active"
        " FROM event WHERE course_id = ? AND ts >= ?"
        + _imp(include_impersonated) + cohort_sql
        + " GROUP BY day",
        [course, cutoff] + cohort_params,
    ).fetchall()
    by_day = {r["day"]: (r["attempts"] or 0, r["active"] or 0) for r in rows}

    out = []
    for i in range(days - 1, -1, -1):
        day = time.strftime("%Y-%m-%d", time.localtime(time.time() - i * 86400))
        attempts, active = by_day.get(day, (0, 0))
        out.append({"day": day, "attempts": attempts, "active": active})
    return out


# ------------------------------------------------- time on task (approximate)


def time_on_task(conn, course, include_impersonated=False):
    """
    Median time each module takes, measured from the gaps between a student's
    consecutive logged events on that module.

    This is a real measurement, not an estimate from attempt counts -- but it
    is still a lower bound, and the dashboard says so.  CAT-SOOP logs discrete
    events, not a heartbeat, so a student who opens a page, reads for twenty
    minutes and then submits once contributes a single gap; and any gap longer
    than IDLE_CUTOFF is treated as the student having walked away, and dropped.
    Time therefore under-reports reading and over-reports nothing.

    It is only meaningful at all because `cs_log_page_views = True` puts page
    views in the log; with that flag off, the only events are submissions and
    this measures inter-submission time instead.
    """
    cohort_sql, cohort_params = _cohort_sql(conn, course)
    # The gap computation and the per-(student, module) sum both happen in
    # SQLite; Python only takes the median across students, so this reads one
    # row per (module, student) rather than one per event.
    rows = conn.execute(
        "SELECT path, total FROM ("
        "  SELECT path, username, SUM(gap) AS total FROM ("
        "    SELECT path, username, ts - LAG(ts) OVER"
        "      (PARTITION BY username, path ORDER BY ts) AS gap"
        "    FROM event WHERE course_id = ?"
        + _imp(include_impersonated) + cohort_sql +
        "  ) WHERE gap IS NOT NULL AND gap > 0 AND gap <= ?"
        "  GROUP BY path, username"
        ")",
        [course] + cohort_params + [IDLE_CUTOFF],
    ).fetchall()

    per_module = {}
    for r in rows:
        per_module.setdefault(r["path"], []).append(r["total"])

    return [
        {
            "path": path,
            "students": len(vals),
            "median_seconds": _median(vals) or 0.0,
            "total_seconds": sum(vals),
        }
        for path, vals in sorted(per_module.items())
    ]


# ------------------------------------------------------------------ FR3


def student_table(conn, course, include_impersonated=False, mastery=MASTERY):
    return _memo(
        conn,
        ("student_table", course, include_impersonated, mastery),
        lambda: _student_table(conn, course, include_impersonated, mastery),
    )


def _student_table(conn, course, include_impersonated=False, mastery=MASTERY):
    cohort = students(conn, course)
    if not cohort:
        return []
    n_exercises = len(exercises(conn, course))

    agg = {}
    for r in conn.execute(
        "SELECT username, path, qname, score, nsubmits, last_ts FROM state"
        " WHERE course_id = ?",
        (course,),
    ):
        u = r["username"]
        if u not in cohort:
            continue
        a = agg.setdefault(u, {"scores": [], "attempts": 0, "mastered": 0,
                               "modules": set(), "last": None})
        if r["score"] is not None:
            a["scores"].append(r["score"])
            if r["score"] >= mastery:
                a["mastered"] += 1
        a["attempts"] += r["nsubmits"] or 0
        a["modules"].add(r["path"])
        if r["last_ts"] and (a["last"] is None or r["last_ts"] > a["last"]):
            a["last"] = r["last_ts"]

    last_seen = {}
    for r in conn.execute(
        "SELECT username, MAX(ts) AS t FROM event WHERE course_id = ?"
        + _imp(include_impersonated) + " GROUP BY username",
        (course,),
    ):
        last_seen[r["username"]] = r["t"]

    out = []
    for u, info in cohort.items():
        a = agg.get(u, {"scores": [], "attempts": 0, "mastered": 0,
                        "modules": set(), "last": None})
        seen = last_seen.get(u) or a["last"]
        out.append(
            {
                "username": u,
                "name": info["name"],
                "email": info["email"],
                "on_roster": info["on_roster"],
                "attempted": len(a["scores"]),
                "mastered": a["mastered"],
                "attempts": a["attempts"],
                "modules": len(a["modules"]),
                "mean_score": _mean(a["scores"]),
                "completion": (a["mastered"] / n_exercises) if n_exercises else None,
                "last_seen": seen,
            }
        )
    out.sort(key=lambda d: (d["completion"] or 0, d["mean_score"] or 0))
    return out


# ------------------------------------------------------------------ FR10


def at_risk(conn, course, include_impersonated=False, mastery=MASTERY):
    """
    Students whose activity, completion, or scores trail the cohort.

    Each flag is reported with its reason, because "at risk" as a bare label is
    not actionable -- an instructor needs to know whether a student has stopped
    showing up or is showing up and struggling, since those need different
    interventions.  A student with no recorded activity at all is reported
    separately as "never started" rather than being scored against the cohort.
    """
    return _memo(
        conn,
        ("at_risk", course, include_impersonated, mastery),
        lambda: _at_risk(conn, course, include_impersonated, mastery),
    )


def _at_risk(conn, course, include_impersonated=False, mastery=MASTERY):
    table = student_table(conn, course, include_impersonated, mastery)
    if not table:
        return []

    started = [s for s in table if s["attempts"] > 0]
    completions = [s["completion"] or 0 for s in started]
    scores = [s["mean_score"] for s in started if s["mean_score"] is not None]
    med_completion = _median(completions) or 0.0
    mean_score = _mean(scores) or 0.0
    sd_score = _stdev(scores)
    now = time.time()
    stale_cutoff = now - STALE_DAYS * 86400

    flagged = []
    for s in table:
        reasons = []
        if s["attempts"] == 0:
            reasons.append("never started")
        else:
            if s["last_seen"] and s["last_seen"] < stale_cutoff:
                days = int((now - s["last_seen"]) / 86400)
                reasons.append("inactive %d days" % days)
            if med_completion > 0 and (s["completion"] or 0) < 0.5 * med_completion:
                reasons.append(
                    "completion %.0f%% vs cohort median %.0f%%"
                    % ((s["completion"] or 0) * 100, med_completion * 100)
                )
            if (
                sd_score > 0
                and s["mean_score"] is not None
                and s["mean_score"] < mean_score - sd_score
            ):
                reasons.append(
                    "mean score %.0f%% (cohort %.0f%%)"
                    % (s["mean_score"] * 100, mean_score * 100)
                )
        if reasons:
            row = dict(s)
            row["reasons"] = reasons
            flagged.append(row)

    flagged.sort(key=lambda d: (-len(d["reasons"]), d["completion"] or 0))
    return flagged


# ------------------------------------------------------- data quality / trend


def anomaly_summary(conn, course, limit=10):
    rows = conn.execute(
        "SELECT kind, COUNT(*) AS n, MAX(seen_at) AS latest FROM anomaly"
        " WHERE course_id = ? GROUP BY kind ORDER BY n DESC LIMIT ?",
        (course, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def snapshot_series(conn, course, key, days=30):
    rows = conn.execute(
        "SELECT day, value FROM snapshot WHERE course_id = ? AND key = ?"
        " ORDER BY day DESC LIMIT ?",
        (course, key, days),
    ).fetchall()
    return [{"day": r["day"], "value": r["value"]} for r in reversed(rows)]