# CAT-SOOP Learning Analytics & Instructor Dashboard

**Team:** Swayam Borate · Chaitanya Sharma · Parth Dembla · Kavya Lavti · Harsh Jamgaonkar
**Project Instructor:** Manu Awasthi
**Repository:** `catsoop/analytics/` (pipeline) · `catsoop/test/test_analytics.py` (tests)

---

## 1. What this project is

CAT-SOOP already delivers content and auto-grades exercises for Digital Systems
(~250 students). Every student action leaves a trace on disk, but there is no
aggregated, instructor-facing view of it. This project closes that visibility
gap.

**Deviation from the original design plan, and why.** The design plan proposed a
*separate* system: a standalone ETL service, a FastAPI backend, and a React
front end running beside CAT-SOOP. We deliver the same layered pipeline **inside
the CAT-SOOP server**, served at `<course>/analytics`.

| | Design plan | Delivered | Rationale |
|---|---|---|---|
| Deployment | 3 services | 1 (CAT-SOOP itself) | No second service to operate, authenticate, or keep in sync |
| Auth | New RBAC layer in FastAPI | CAT-SOOP's own permission model | Re-implementing auth is a security risk; the `admin` permission already exists |
| Freshness | Daily batch only | Daily batch **or** live | Incremental extraction made live sync cheap enough (§6) |
| Store | PostgreSQL/SQLite | SQLite | Portable SQL, no server; migration is a connection-string change |

Every other element of the plan — layer separation, read-only source access,
normalized events, validation-not-dropping, precomputed snapshots, course-scoped
schema — is retained.

---

## 2. Findings from the CAT-SOOP codebase

The design plan (§4.3, §10) stated the log schema could not be finalised from
documentation and had to be established empirically. We did that. **Every finding
below is cited to a file and line and is re-checkable with one command.**

### F1 — CAT-SOOP has no SQL database
Storage is flat files under `<data_root>/_logs/`. Layout:
`_logs/_courses/<course>/<username>/<path…>/<logname>.log`
→ `catsoop/cslog.py:166` (`get_log_filename`)

### F2 — Only four log streams exist
`problemstate`, `problemactions`, `problemgrades`, `groups` (plus `random_seed`,
`question_info`). Verify:
```bash
grep -rhoE '"(problemstate|problemactions|problemgrades|groups)"' --include=*.py catsoop/ | sort | uniq -c
```

### F3 — Record framing is `[8-byte LE length][payload][8-byte LE length]`
→ `catsoop/cslog.py:285`. The **trailing** copy of the length is what lets
CAT-SOOP seek backwards for `most_recent`.
**Consequence we exploited:** a reader can checkpoint a byte offset and resume.
This is the single fact that makes incremental extraction — and therefore live
dashboards — possible.

### F4 — Page views ARE logged, but the flag is OFF by default
→ `catsoop/__HANDLERS__/default/default.py:435`
```python
if _get(context, "cs_log_page_views", False, bool):
    log_action(context, {})
```
The design plan listed this as an **unresolved open question** (§10: "Uncertainty
whether page/module view events are logged at all"). **Resolved: they are, gated
on `cs_log_page_views`, default `False`.** Without it there is no activity or
time-on-task data — only submissions. We enable it in the CAT-SOOP config.

### F5 — Staff impersonation silently corrupts per-student metrics
When staff use `?as=<student>` to test a problem, CAT-SOOP writes the submission
into **that student's** log, with a `real_user` block naming the staff member.
→ `catsoop/auth.py:179-188`

This is **not in the design plan**. It is the most consequential finding:
naively counting these makes instructor testing indistinguishable from student
learning. In our own seed data, **75 of 76 events were staff-impersonated** — a
naive pipeline would have reported a fully active cohort that had done nothing.
Handled by `normalize.real_submitter()`; excluded by default, with a UI toggle
and a visible excluded-count.

### F6 — `problemstate` is authoritative for scores and attempts
`nsubmits_used` is the counter CAT-SOOP's own gating logic uses; `scores` is the
score that counts. Replaying `problemactions` to re-derive them would be a
re-implementation that could drift from CAT-SOOP's behaviour. We read them
directly.

### F7 — Course structure is free
`question_info` logs record `{qname: {qtype, csq_npoints, …}}` per module,
written when a page renders. No page needs re-executing to build the Exercise
table.

### F8 — Two integration traps (found by failure, then fixed)
- `print()` inside an imported module goes to the **server's stdout, not the
  page**. CAT-SOOP rebinds `print` only inside the `<python>` block's own
  namespace. Fixed by writing to `cs___WEBOUT`.
- `setup.py` carries a **hardcoded package list** (`setup.py:188`). A new
  subpackage is invisible to the installed server until registered there.

---

## 3. Architecture

```
CAT-SOOP logs ──► extract ──► normalize ──► store ──► engine ──► page
  (read-only)      (format)    (validate)   (SQLite)  (metrics)  (HTML)
                       ▲
        the ONLY format-aware module
```

| Module | Lines | Responsibility |
|---|---|---|
| `extract.py` | 254 | **Only** module aware of CAT-SOOP's on-disk format. Incremental, byte-offset resumable |
| `normalize.py` | 262 | Raw records → `Event`/`Anomaly`. Score + timestamp normalization, impersonation detection |
| `store.py` | 285 | SQLite schema, loader, per-log sync cursors |
| `sync.py` | 244 | Orchestration: cron, CLI, or inline. Idempotent |
| `engine.py` | 630 | Course/module/question/student metrics; difficulty + at-risk heuristics |
| `render.py` | 411 | Inline-SVG charts. No CDN dependency |
| `page.py` | 501 | Access control, sync policy, page assembly |
| `__main__.py` | 87 | CLI entry point |
| `test_analytics.py` | 426 | Ground-truth validation |
| **Total** | **3,121** | |

**Maintainability guarantee (NFR):** a change to CAT-SOOP's storage format
requires a fix in `extract.py` and nowhere else.

**Database:** SQLite 3.46, single file at
`<cs_data_root>/_logs/_analytics/analytics.db`, WAL mode.
Tables: `event`, `state`, `exercise`, `module`, `student`, `cursor`, `snapshot`,
`anomaly`, `course`, `meta`. Every table is keyed by `course_id` (FR15).
The store is a **derived cache** — delete it and the next sync rebuilds it.

---

## 4. Access control (FR13)

Gated on CAT-SOOP's `admin` permission, **server-side, before any metric is
computed**. Hiding a menu link is not a gate.

| Role | Result | Mechanism |
|---|---|---|
| Admin / Instructor | Full dashboard | holds `admin` |
| TA | Blocked | holds `whdw`, not `admin` |
| Student / Guest | Blocked | blocked by release date before page code runs |
| Admin impersonating a student | Blocked | impersonation drops `admin` |

Configurable per course via `cs_analytics_staff_permission` (set to `"whdw"` to
include TAs). Verified live over HTTP — see §8.

---

## 5. Metric definitions (so they cannot be disputed)

- **Mastery threshold** = 0.999. A partially-credited answer is not counted as
  complete.
- **Completion** = mastered (student, question) pairs ÷ (students × exercises).
- **Reached** a module = produced ≥1 non-impersonated event there.
- **Completed** a module = every question in it at ≥ mastery.
- **Difficulty index** =
  `0.50·(1 − mean_score) + 0.30·fail_rate + 0.20·min(1, (mean_attempts−1)/4)`
  Weights are a stated judgement call; all three components are displayed beside
  the index so the weighting can be disagreed with.
- **At risk** = inactive ≥14 days, OR completion < ½ cohort median, OR mean
  score > 1 SD below cohort mean. Reasons are listed individually, because
  "stopped showing up" and "showing up and struggling" need different responses.
  "Never started" is reported separately rather than scored against the cohort.
- **Time on task** = sum of gaps between a student's consecutive logged events
  on a page, discarding gaps > 15 min. **A lower bound**, labelled as such on the
  dashboard — CAT-SOOP logs events, not a heartbeat, so reading before the first
  action is invisible.

---

## 6. Measured performance (§8 of the plan: evaluated, not assumed)

Synthetic cohorts, 12 modules × 8 questions, Linux filesystem:

| | 250 students | 2,000 students |
|---|---|---|
| Events | 45,170 | 357,330 |
| Log files on disk | 3,378 | 26,052 |
| Initial full sync | 1.16 s | 10.24 s |
| **Incremental sync (no change)** | **0.12 s** | **0.97 s** |
| **Incremental sync (1 new record)** | **0.14 s** | **1.02 s** |
| All dashboard metrics | 0.34 s | 2.95 s |
| Store size | 8 MB | 64 MB |

**Why sync stays flat:** an unchanged log is never opened. A no-op sync costs one
`stat` per file.

**Honest ceiling:** metric computation is what grows. Live sync is comfortable to
~1,000 students; beyond that, set `cs_analytics_sync_mode = "cached"` and rely on
the daily job. The `snapshot` table is where per-module rollups would be
materialised to push that further.

**Two optimisations made after profiling** (not guessed):
1. `course_overview` recomputed the student table 4× per render → per-request
   memoisation, invalidated on sync. 2,000-student render: 4.09 s → 2.95 s.
2. `activity_series` and `time_on_task` moved from Python loops to SQL
   aggregation.

---

## 7. Work division

Five members, two phases. Each member owns one module per phase, so nobody is
idle and nobody blocks everyone.

### Phase 1 — Pipeline foundation *(Report #1)*

| Member | Component | Deliverable |
|---|---|---|
| **Swayam** | Codebase investigation + `extract.py` | Findings F1–F8; incremental byte-offset extractor |
| **Harsh** | `normalize.py` | Event/Anomaly model; score + timestamp normalization; impersonation detection (F5) |
| **Parth** | `store.py` | SQLite schema, loader, sync cursors, WAL |
| **Chaitanya** | `sync.py` + `__main__.py` | Orchestration, idempotency, CLI, cron job |
| **Kavya** | `test_analytics.py` | Ground-truth personas; extraction + normalization test layers |

**Phase 1 exit criteria:** logs → normalized events in a queryable store;
`sync` idempotent; extraction and normalization tests green.

### Phase 2 — Metrics, dashboard, scale *(Report #2)*

| Member | Component | Deliverable |
|---|---|---|
| **Kavya** | `engine.py` core | FR1–FR6: overview, module progress, score/attempt distributions |
| **Parth** | `engine.py` heuristics | FR7/FR8 difficulty, FR10 at-risk, FR9 trends |
| **Harsh** | `render.py` | Charts, accessible palette, light/dark, tooltips |
| **Swayam** | `page.py` | Access control (FR13), sync policy, page assembly |
| **Chaitanya** | Benchmarking | Scalability runs at 250/2,000; profiling; optimisation |

**Phase 2 exit criteria:** dashboard live at `<course>/analytics`; access control
verified per role over HTTP; performance measured at ≥2 cohort sizes.

---

## 8. Test & QA evidence

```bash
python -m pytest catsoop/test/test_analytics.py -v
```

**Result: 18 tests, 18 passed, 0 failed.**

| Suite | Tests | Covers |
|---|---|---|
| `TestNormalize` | 5 | Score/timestamp normalization, impersonation, malformed records, staff-action filtering |
| `TestIncrementalExtraction` | 2 | Byte-offset resume; truncated-log reset |
| `TestGroundTruth` | 8 | Four scripted personas with known-correct expected output |
| `TestAccessControl` | 3 | Permission gate; no metric leakage when denied |

**Ground-truth method (plan §8.1).** Four synthetic personas with deliberately
distinct behaviour — *ace* (all correct, one try), *grinder* (correct after many
tries), *struggler* (repeated failure), *ghost* (never starts, but is used as an
impersonation test subject). Because each persona's behaviour is scripted, the
correct analytics output is known in advance, so a failure means the pipeline is
wrong, not the data.

**Live HTTP verification** (not just unit tests):

| Check | Result |
|---|---|
| Admin loads dashboard | HTTP 200, all 9 sections render |
| TA | Blocked, 0 data elements |
| Student / Guest | Blocked before page code runs |
| Admin impersonating student | Blocked |
| Student identities in non-admin responses | **none** |
| Live sync on page load | "picked up 1 new event just now" |

---

## 9. Defects found in QA

| # | Defect | Severity | Root cause | Status |
|---|---|---|---|---|
| D1 | Dashboard rendered empty despite HTTP 200 | Blocker | `print()` in an imported module writes to server stdout; CAT-SOOP rebinds `print` only in the block's namespace | **Fixed** — write to `cs___WEBOUT` |
| D2 | `catsoop.analytics` not importable by the installed server | Blocker | `setup.py:188` hardcodes the package list | **Fixed** — registered + reinstalled |
| D3 | Permission gate appeared to reject a valid Admin | Major | Masked by D1 — the denial HTML was going to stdout | **Fixed with D1**; gate re-verified per role |
| D4 | `store.set_cursor` called with mismatched positional args | Major | Kwarg patch applied over a positional signature | **Fixed** — call site corrected |
| D5 | No-op sync took 1.45 s | Performance | `question_info` re-decoded for every module each run | **Fixed** — mtime-cursored; 1.45 s → 0.46 s |
| D6 | Student table computed 4× per page render | Performance | `course_overview` → `at_risk` → `student_table` chain | **Fixed** — per-request memoisation + invalidation on sync |
| D7 | Memoisation could serve stale data | Major (latent) | Cache survived a sync on a held connection | **Fixed** — `sync_course` clears the memo; regression test added |
| D8 | Dark-mode palette failed accessibility validation | Minor | Lightness band + CVD separation below threshold | **Fixed** — re-stepped; passes all-pairs CVD in both modes |
| D9 | Difficulty band conveyed by colour alone | Minor (a11y) | Status colour with no text | **Fixed** — band now carries a word (`▲ hard`) |

**Open defects: none.** All nine are closed and covered by tests or live checks.

---

## 10. Known limitations (stated, not hidden)

1. **Time on task is a lower bound** — gaps between logged events, capped at 15 min.
2. **Encrypted log paths unsupported** — `extract.check_readable()` raises a clear
   error rather than returning wrong numbers.
3. **Course structure appears only after first render** — a module nobody has
   opened has no `question_info` log yet.
4. **Difficulty weights are a judgement call** — components shown so they can be
   re-weighted.
5. **Live sync ceiling ~1,000 students** — measured, see §6.

---

## 11. Reproducing every claim in this document

```bash
# Findings F2, F3, F4, F5 — cited lines
grep -n 'cs_log_page_views' catsoop/__HANDLERS__/default/default.py     # F4 → 435
grep -n 'def get_log_filename' catsoop/cslog.py                         # F1 → 166
grep -n 'struct.unpack("<Q"' catsoop/cslog.py                           # F3 → 285
grep -n 'real_user' catsoop/auth.py                                     # F5 → 179

# Tests (§8)
python -m pytest catsoop/test/test_analytics.py -v

# Pipeline + database (§3)
python -m catsoop.analytics status
python -m catsoop.analytics pending ml101
# inspect the store (sqlite3 CLI optional -- this needs no extra tool):
python -c "import catsoop.analytics.store as S; c=S.connect(readonly=True); \
print([r[0] for r in c.execute(\"select name from sqlite_master where type='table' order by name\")])"

# Access control (§4) — server must be running
curl -s localhost:7667/ml101/analytics             | grep -c Gradebook   # admin: 1
curl -s 'localhost:7667/ml101/analytics?as_role=TA' | grep -c Gradebook  # TA:    0
```

## 12. Daily batch

```
17 3 * * *  /path/to/python -m catsoop.analytics sync >> /var/log/catsoop-analytics.log 2>&1
```

Idempotent and safe to run while the server serves (WAL mode).
