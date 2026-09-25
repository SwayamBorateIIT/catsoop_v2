# CAT-SOOP Learning Analytics

An instructor-facing analytics dashboard that runs **inside** the CAT-SOOP
server, reachable at `<course>/analytics`.

CAT-SOOP's data root is only ever **read**, never written.

## Quick start

Add two files to a course:

```
data/courses/<course>/analytics/preload.py
data/courses/<course>/analytics/content.catsoop
```

`content.catsoop`:

```
<python>
import catsoop.analytics.page as _analytics
print(_analytics.render_dashboard(globals()))
</python>
```

`preload.py`:

```python
cs_long_name = "Analytics Dashboard"
cs_release_date = "9999-01-01:00:00"   # keeps it out of the student course index
cs_analytics_staff_permission = "admin"
cs_analytics_sync_mode = "auto"
cs_analytics_inline_budget = 200
```

Then add a link to `cs_top_menu`, and turn on page-view logging in your
CAT-SOOP config so activity and time-on-task have data to work from:

```python
cs_log_page_views = True     # off by default in CAT-SOOP
```

## Layers

| Module | Responsibility |
|---|---|
| `extract.py` | **The only module that knows CAT-SOOP's on-disk log format.** Incremental, byte-offset resumable. |
| `normalize.py` | Raw records → `Event` / `Anomaly`. Score and timestamp normalization; impersonation detection. |
| `store.py` | SQLite schema, loader, and per-log sync cursors. |
| `sync.py` | Orchestration. Runs from cron, the CLI, or inline from the page. |
| `engine.py` | Course / module / question / student metrics, difficulty and at-risk heuristics. |
| `render.py` | Inline-SVG charts and tables. No CDN dependency. |
| `page.py` | Access control, sync policy, page assembly. |

A change to CAT-SOOP's storage format requires a fix in `extract.py` and
nowhere else.

## Command line

```
python -m catsoop.analytics sync            # every course
python -m catsoop.analytics sync ml101      # one course
python -m catsoop.analytics status          # what the store holds
python -m catsoop.analytics pending ml101   # changed logs; reads nothing
```

Daily batch:

```
17 3 * * *  /path/to/python -m catsoop.analytics sync >> /var/log/catsoop-analytics.log 2>&1
```

`sync` is idempotent and safe to run while the web server is serving (the
store is WAL-mode).

## Sync policy

Because an unchanged log is never opened, a sync that finds nothing new costs
one `stat` per log file. So the page can afford to sync on load:

* `auto` (default) — sync inline when fewer than `cs_analytics_inline_budget`
  logs have changed; otherwise serve the last batch and say so.
* `inline` — always sync on load.
* `cached` — never sync on load; rely on the scheduled job.

Every view states when the data was last refreshed, so a cached view is never
mistaken for a live one.

## Two findings that shaped the design

**Page views are logged, but the flag is off by default.** CAT-SOOP writes a
`view` action into `problemactions` at `__HANDLERS__/default/default.py:435`,
gated on `cs_log_page_views`, which defaults to `False`. Without it there is no
activity or time-on-task data at all — only submissions.

**Staff impersonation pollutes per-student metrics.** When staff use `?as=` to
test a problem, CAT-SOOP records the submission under the *student's* log with
a `real_user` block naming the staff member. `normalize.real_submitter` detects
this and the dashboard excludes such events by default, with a toggle to
include them and a count of how many were excluded.

## Measured performance

Synthetic cohorts, 12 modules × 8 questions, Linux filesystem:

| | 250 students | 2 000 students |
|---|---|---|
| events | 45 000 | 357 000 |
| initial full sync | 1.2 s | 10.2 s |
| incremental sync (no changes) | 0.12 s | 0.97 s |
| incremental sync (1 new record) | 0.14 s | 1.02 s |
| all dashboard metrics | 0.34 s | 2.95 s |
| store size | 8 MB | 64 MB |

Sync cost stays flat with cohort size because unchanged logs are never opened.
Metric computation is the part that grows: it is comfortable inline to roughly
1 000 students, beyond which `cs_analytics_sync_mode = "cached"` plus the daily
job is the right configuration. The `snapshot` table is where per-module and
per-question rollups would be materialized to push that ceiling further.

## Limitations, stated rather than hidden

* **Time on task is a lower bound.** It is measured from gaps between logged
  events, discarding gaps over 15 minutes. Reading before the first action is
  invisible. CAT-SOOP logs events, not a heartbeat.
* **Encrypted log paths are not supported.** `extract.check_readable()` raises
  a clear error rather than returning wrong numbers. Supporting them needs
  roster-driven path enumeration instead of a filesystem walk.
* **Course structure comes from rendered pages.** A module nobody has opened
  has no `question_info` log yet, so it does not appear until first render.
* **Difficulty weights are a judgement call** (50/30/20 over mean score,
  failure rate, attempts). The components are shown beside the index so the
  weighting can be disagreed with.

## Tests

```
python -m pytest catsoop/test/test_analytics.py
```

Ground-truth validation: four synthetic personas with deliberately distinct,
known behaviour (completes everything, needs many attempts, stops early, never
starts), whose correct analytics output is known in advance.
