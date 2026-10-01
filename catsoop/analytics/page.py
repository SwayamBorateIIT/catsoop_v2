"""
The instructor-facing dashboard page.

A course gets a dashboard by dropping a three-line `content.catsoop` into
`<course>/analytics/` that calls `render(globals())`.  Everything else --
access control, sync policy, layout -- lives here, so every course gets the
same dashboard and a fix lands everywhere at once.

Access control
--------------
The gate is the `admin` permission, which CAT-SOOP grants to the Admin and
Instructor roles and withholds from TAs, students and guests.  It is enforced
here, server-side, before any metric is computed -- not by hiding a menu link.
A course that wants its TAs included can set `cs_analytics_staff_permission`.

Sync policy
-----------
The design this implements calls for a daily batch so that dashboard views
never re-parse raw logs.  Incremental extraction makes a stronger policy
possible: because an unchanged log is never opened, a sync that finds nothing
new costs one `stat` per log file.  So the page syncs inline when the pending
work is small, and falls back to serving the last batch when it is not:

    cs_analytics_sync_mode     "auto" (default) | "inline" | "cached"
    cs_analytics_inline_budget  max changed logs to absorb in a page load (200)

In every mode the page states when the numbers were last refreshed, so a
cached view is never mistaken for a live one.
"""

import time

from . import engine, render, store, sync
from .extract import ExtractionError

STAFF_PERMISSION_DEFAULT = "admin"
INLINE_BUDGET_DEFAULT = 200


def _cfg(ctx, name, default):
    value = ctx.get(name, default)
    return default if value is None else value


def _url(ctx, **params):
    """This page's URL with query parameters replaced."""
    base = "%s/%s" % (
        ctx.get("cs_url_root", "").rstrip("/"),
        "/".join(ctx.get("cs_path_info", [])),
    )
    keep = {}
    form = ctx.get("cs_form", {}) or {}
    for k in ("staff", "as", "as_role"):
        if form.get(k):
            keep[k] = form[k]
    keep.update({k: v for k, v in params.items() if v is not None})
    for k, v in list(keep.items()):
        if v is False:
            keep.pop(k)
    if not keep:
        return base
    return base + "?" + "&".join("%s=%s" % (k, v) for k, v in keep.items())


# ------------------------------------------------------------------- gating


def is_staff(ctx):
    perms = set((ctx.get("cs_user_info") or {}).get("permissions", []))
    needed = _cfg(ctx, "cs_analytics_staff_permission", STAFF_PERMISSION_DEFAULT)
    return needed in perms


def _denied(ctx):
    name = (ctx.get("cs_user_info") or {}).get("username", "you")
    return (
        render.STYLE
        + '<div class="csa"><div class="csa-panel">'
        "<h3>Not available</h3>"
        '<p class="csa-note">The analytics dashboard is restricted to course '
        "staff. This page is gated on the <code>%s</code> permission, which "
        "the account <b>%s</b> does not hold.</p></div></div>"
        % (
            render.esc(_cfg(ctx, "cs_analytics_staff_permission", STAFF_PERMISSION_DEFAULT)),
            render.esc(name),
        )
    )


# --------------------------------------------------------------- sync policy


def _refresh(ctx, course):
    """
    Decide whether to sync now, do it, and describe what happened.

    Returns `(report_or_None, explanation, pending)`.
    """
    mode = _cfg(ctx, "cs_analytics_sync_mode", "auto")
    budget = int(_cfg(ctx, "cs_analytics_inline_budget", INLINE_BUDGET_DEFAULT))
    forced = (ctx.get("cs_form", {}) or {}).get("sync") == "1"

    if mode == "cached" and not forced:
        return None, "Serving the last completed batch (sync mode: cached).", None

    try:
        pending = sync.count_pending(course)
    except ExtractionError as exc:
        return None, "Cannot read the data root: %s" % exc, None

    if pending == 0 and not forced:
        return None, None, 0
    if mode == "inline" or forced or pending <= budget:
        return sync.sync_course(course), None, pending
    return (
        None,
        "%d logs have changed since the last sync -- more than the inline "
        "budget of %d, so this view is from the last batch." % (pending, budget),
        pending,
    )


# ------------------------------------------------------------------ sections


def _header(ctx, course, meta, report, explanation, include_staff):
    bits = []
    if meta and meta.get("last_sync"):
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(meta["last_sync"]))
        freshness = "Data as of <b>%s</b> (%s)" % (
            render.esc(when), render.esc(render.ago(meta["last_sync"]))
        )
        if report and getattr(report, "skipped", False):
            pass  # the banner says why below; do not claim a refresh happened
        elif report and report.events:
            freshness += " &middot; picked up %d new event%s just now" % (
                report.events, "" if report.events == 1 else "s"
            )
        elif report:
            freshness += " &middot; refreshed just now"
        bits.append(freshness)
    else:
        bits.append("<b>No data yet.</b> Nothing has been synced for this course.")

    if explanation:
        bits.append(render.esc(explanation))
    if report and getattr(report, "skipped", False):
        bits.append(
            "Another refresh is already running, so this view is from the last "
            "completed one."
        )
    if report and report.error:
        bits.append("Sync failed: <b>%s</b>" % render.esc(report.error))
    if report and report.anomalies:
        bits.append(
            "%d record%s could not be read cleanly and %s flagged below."
            % (report.anomalies, "" if report.anomalies == 1 else "s",
               "was" if report.anomalies == 1 else "were")
        )

    links = [
        '<a class="csa-link" href="%s">Refresh now</a>' % render.esc(_url(ctx, sync="1"))
    ]
    if include_staff:
        links.append(
            '<a class="csa-link" href="%s">Exclude staff test submissions</a>'
            % render.esc(_url(ctx, staff=None))
        )
    else:
        links.append(
            '<a class="csa-link" href="%s">Include staff test submissions</a>'
            % render.esc(_url(ctx, staff="1"))
        )

    return '<div class="csa-banner">%s<br>%s</div>' % (
        " &middot; ".join(bits), " &middot; ".join(links)
    )


def _overview(m, include_staff):
    return render.tiles(
        [
            ("Students", m["total_students"],
             "%d active this week" % m["active_students"]),
            ("Completion", render.pct(m["completion"], 1),
             "%d of %d question-slots done"
             % (m["mastered_pairs"], m["total_students"] * m["total_exercises"])),
            ("Mean score", render.pct(m["mean_score"], 1),
             "median %s" % render.pct(m["median_score"], 0)),
            ("Attempts", m["total_attempts"],
             "across %d question-slots tried" % m["attempted_pairs"]),
            ("Need attention", m["at_risk"],
             "see breakdown below"),
            ("Exercises", m["total_exercises"],
             "%s staff test events %s"
             % (m["impersonated_events"], "included" if include_staff else "excluded")),
        ]
    )


def _module_section(rows, total_students):
    if not rows:
        return render.panel(
            "Module progress", "No graded modules have been rendered yet.", ""
        )
    data = []
    for r in rows:
        data.append(
            {
                "label": render.short_path(r["path"]),
                "reached": r["reached"],
                "reached_label": r["reached"],
                "completed": r["completed"],
                "completed_label": r["completed"],
            }
        )
    chart = render.grouped_bars(
        data,
        [
            ("reached", "reached", "var(--series-1)"),
            ("completed", "completed", "var(--series-3)"),
        ],
        value_max=total_students or None,
    )
    return render.panel(
        "Module progress",
        "How many of the %d students opened each module, and how many have "
        "every question in it fully correct." % total_students,
        chart,
    )


def _difficulty_section(rows, limit=12):
    if not rows:
        return render.panel("Question difficulty", "No graded attempts yet.", "")
    head = (
        '<div class="csa-scroll"><table><tr>'
        "<th>Question</th><th>Module</th><th>Type</th>"
        '<th class="n">Students</th><th class="n">Mean score</th>'
        '<th class="n">Fail rate</th><th class="n">Mean tries</th>'
        "<th>Difficulty</th></tr>"
    )
    body = []
    for r in rows[:limit]:
        body.append(
            "<tr><td><code>%s</code></td><td>%s</td><td>%s</td>"
            '<td class="n">%d</td><td class="n">%s</td><td class="n">%s</td>'
            '<td class="n">%s</td><td style="min-width:170px">%s</td></tr>'
            % (
                render.esc(r["qname"]),
                render.esc(render.short_path(r["path"])),
                render.esc(r["qtype"]),
                r["n_students"],
                render.pct(r["mean_score"]),
                render.pct(r["fail_rate"]),
                render.num(r["mean_attempts"], 1),
                render.status_bar(
                    r["difficulty"], "difficulty index %.2f" % r["difficulty"]
                ),
            )
        )
    return render.panel(
        "Hardest questions",
        "Ranked by a heuristic combining mean score (50%), failure rate (30%) "
        "and attempts used (20%). The components are shown so you can weigh "
        "them differently than the index does.",
        head + "".join(body) + "</table></div>",
    )


def _at_risk_section(rows, limit=25):
    if not rows:
        return render.panel(
            "Students needing attention",
            "No student currently trails the cohort on activity, completion "
            "or score.",
            "",
        )
    head = (
        '<div class="csa-scroll"><table><tr><th>Student</th>'
        '<th class="n">Completion</th><th class="n">Mean score</th>'
        '<th class="n">Attempts</th><th>Last seen</th><th>Why flagged</th>'
        "</tr>"
    )
    body = []
    for r in rows[:limit]:
        chips = "".join(
            '<span class="csa-chip warn">%s</span>' % render.esc(x) for x in r["reasons"]
        )
        body.append(
            "<tr><td>%s<br><span style='font-size:12px;opacity:.7'>%s</span></td>"
            '<td class="n">%s</td><td class="n">%s</td><td class="n">%d</td>'
            "<td>%s</td><td>%s</td></tr>"
            % (
                render.esc(r["name"]), render.esc(r["username"]),
                render.pct(r["completion"]), render.pct(r["mean_score"]),
                r["attempts"], render.esc(render.ago(r["last_seen"])), chips,
            )
        )
    return render.panel(
        "Students needing attention",
        "Flagged for inactivity (%d+ days), completion below half the cohort "
        "median, or a mean score more than one standard deviation below the "
        "cohort. Reasons are listed because they call for different responses."
        % engine.STALE_DAYS,
        head + "".join(body) + "</table></div>",
    )


def _time_section(rows, page_views_on, limit=12):
    if not rows:
        body = '<p class="csa-note">No timed activity recorded yet.</p>'
    else:
        head = (
            '<div class="csa-scroll"><table><tr><th>Module</th>'
            '<th class="n">Students timed</th><th class="n">Median time</th>'
            '<th class="n">Total time</th></tr>'
        )
        body = head + "".join(
            "<tr><td>%s</td><td class=\"n\">%d</td><td class=\"n\">%s</td>"
            '<td class="n">%s</td></tr>'
            % (
                render.esc(render.short_path(r["path"])), r["students"],
                render.esc(render.duration(r["median_seconds"])),
                render.esc(render.duration(r["total_seconds"])),
            )
            for r in sorted(rows, key=lambda d: -d["median_seconds"])[:limit]
        ) + "</table></div>"

    caveat = (
        "Measured from the gaps between a student's consecutive logged events "
        "on a page, discarding any gap over %d minutes as the student having "
        "stepped away. It is a lower bound: CAT-SOOP logs events, not a "
        "heartbeat, so time spent reading before the first action is invisible."
        % int(engine.IDLE_CUTOFF // 60)
    )
    if not page_views_on:
        caveat += (
            "  cs_log_page_views is currently off, so only submissions are "
            "logged and this measures inter-submission time, not time on page."
        )
    return render.panel("Time on task (lower bound)", caveat, body)


def _student_section(rows, limit=200):
    if not rows:
        return render.panel("Gradebook", "No students on the roster yet.", "")
    head = (
        '<div class="csa-scroll"><table><tr><th>Student</th>'
        "<th>Completion</th>"
        '<th class="n">Mastered</th><th class="n">Tried</th>'
        '<th class="n">Attempts</th><th class="n">Mean score</th>'
        "<th>Last seen</th></tr>"
    )
    body = []
    for r in rows[:limit]:
        flag = (
            ' <span class="csa-chip">not on roster</span>'
            if not r["on_roster"] else ""
        )
        body.append(
            "<tr><td>%s%s<br><span style='font-size:12px;opacity:.7'>%s</span></td>"
            '<td style="min-width:110px">%s</td>'
            '<td class="n">%d</td><td class="n">%d</td><td class="n">%d</td>'
            '<td class="n">%s</td><td>%s</td></tr>'
            % (
                render.esc(r["name"]), flag, render.esc(r["username"]),
                render.inline_bar(r["completion"]),
                r["mastered"], r["attempted"], r["attempts"],
                render.pct(r["mean_score"]), render.esc(render.ago(r["last_seen"])),
            )
        )
    return render.panel(
        "Gradebook",
        "Every student, weakest first. 'Mastered' counts questions fully "
        "correct; 'tried' counts questions with any recorded score.",
        head + "".join(body) + "</table></div>",
    )


def _anomaly_section(rows):
    if not rows:
        return ""
    body = (
        "<table><tr><th>Kind</th><th class=\"n\">Count</th><th>Most recent</th></tr>"
        + "".join(
            '<tr><td><code>%s</code></td><td class="n">%d</td><td>%s</td></tr>'
            % (render.esc(r["kind"]), r["n"], render.esc(render.ago(r["latest"])))
            for r in rows
        )
        + "</table>"
    )
    return render.panel(
        "Data quality",
        "Records that could not be normalized. They are counted here rather "
        "than dropped silently, so a log-format problem shows up as a number "
        "instead of a quietly wrong metric.",
        body,
    )


# --------------------------------------------------------------------- entry


def render_dashboard(ctx):
    """Build the dashboard HTML for the course in `ctx`.  Returns a string."""
    if not is_staff(ctx):
        return _denied(ctx)

    course = ctx.get("cs_course")
    if not course:
        return render.STYLE + (
            '<div class="csa"><div class="csa-panel"><h3>No course</h3>'
            '<p class="csa-note">This page is not inside a course.</p>'
            "</div></div>"
        )

    include_staff = (ctx.get("cs_form", {}) or {}).get("staff") == "1"
    report, explanation, _pending = _refresh(ctx, course)

    try:
        conn = store.connect()
    except Exception as exc:
        return render.STYLE + (
            '<div class="csa"><div class="csa-panel"><h3>Analytics store '
            'unavailable</h3><p class="csa-note">%s</p></div></div>'
            % render.esc(exc)
        )

    try:
        meta = store.last_sync(conn, course)
        overview = engine.course_overview(conn, course, include_staff)
        modules = engine.module_progress(conn, course, include_staff)
        difficulty = engine.question_difficulty(conn, course, include_staff)
        dist = engine.score_distribution(conn, course)
        attempts = engine.attempt_distribution(conn, course)
        activity = engine.activity_series(conn, course, 30, include_staff)
        timing = engine.time_on_task(conn, course, include_staff)
        table = engine.student_table(conn, course, include_staff)
        risk = engine.at_risk(conn, course, include_staff)
        anomalies = engine.anomaly_summary(conn, course)
    finally:
        conn.close()

    page_views_on = bool(ctx.get("cs_log_page_views", False))

    parts = [
        render.STYLE,
        '<div class="csa">',
        _header(ctx, course, meta, report, explanation, include_staff),
        _overview(overview, include_staff),
        _module_section(modules, overview["total_students"]),
        render.panel(
            "Score distribution",
            "Students grouped by their mean score across every question they "
            "have attempted.",
            render.column_chart(dist["labels"], dist["counts"], "var(--series-1)"),
        ),
        render.panel(
            "Attempts per question",
            "How many tries each solved question-slot took. A long tail here "
            "usually means a question is unclear rather than hard.",
            render.column_chart(
                [a["label"] for a in attempts],
                [a["count"] for a in attempts],
                "var(--series-2)",
            ),
        ),
        render.panel(
            "Activity, last 30 days",
            "Graded attempts per day. Use it to see whether the class is "
            "working steadily or only at deadlines.",
            render.sparkline_area(
                [a["attempts"] for a in activity],
                [a["day"] for a in activity],
                "var(--series-1)",
            ),
        ),
        _difficulty_section(difficulty),
        _at_risk_section(risk),
        _time_section(timing, page_views_on),
        _student_section(table),
        _anomaly_section(anomalies),
        "</div>",
    ]
    return "".join(parts)


def emit(ctx):
    """
    Write the dashboard into the page from a `<python>` block.

    CAT-SOOP captures a `<python>` block's output by rebinding `print` inside
    the block's own namespace, which does *not* reach `print` calls made by an
    imported module like this one -- those go to the server's stdout and vanish
    from the page.  So the output is written to `cs___WEBOUT` directly, which
    is the file-like object that rebound `print` writes to.

    A page can equally well do `print(render_dashboard(globals()))` itself,
    since there `print` is the rebound one.
    """
    html = render_dashboard(ctx)
    out = ctx.get("cs___WEBOUT")
    if out is not None:
        out.write(html)
    else:
        print(html)
    return html