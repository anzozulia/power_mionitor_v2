# Power Monitor v2 — what was built, how, and where it stands (2026-10-04)

Snapshot of the repo at HEAD `0d76e29`, taken before Phase 6. The facts below cite repo paths.

## 1. What was built

A Django 5.2 + PostgreSQL 18 system that runs as six Compose services:

| Service | Role |
|---|---|
| `db` | `postgres:18.6-trixie` |
| `backup` | `backup.sh` loop: nightly `pg_dump -Fc`, keep 14, catch-up, health check (dump < 26 h old) |
| `migrate` | one-shot `manage.py release`: migrations, then admin account synced from env |
| `web` | gunicorn gthread (2×4): admin panel + `/hb` heartbeat endpoint + `/healthz` |
| `worker` | one process, three threads: detection (5 s), Telegram I/O (outbox relay + chart lifecycle), watchdog (exit 70 → Docker restart) |
| `caddy` (prod) | TLS proxy; the only service publishing ports |

**Core design (the brief's KD1–KD4, implemented faithfully):**
- **One stored timeline** (`power_interval`, PostgreSQL exclusion constraint against overlaps) drives alerts, the chart, the daily totals and the outage list. Raw heartbeats are not stored at all.
- **Transitions** take a per-location row lock (`SELECT … FOR UPDATE` on `location_state`), then one conditional UPDATE; the outbox row is written in the same transaction. No network I/O inside a transition.
- **Outbox** with `pending → sending → sent | uncertain | expired | dropped`; possibly-delivered sends are never re-sent; per-bot backoff without sleeping; late alerts get an event-time prefix; expiry after 6 h with an ops notice.
- **Single active worker** via a PostgreSQL advisory-lock lease (HELD / STANDBY / DB_DOWN), reacquired in-process; outbox claims fenced by the lease.
- **Server downtime = not monitored**: a "lapse carve" rewrites gaps > 15 s as `not_monitored`, sends one ops gap notice; fresh detection windows after worker/web starts.
- **Weekly chart**: Pillow + bundled Inter 4.1, 1280×1000, deterministic (golden PNG tests incl. DST days); condition-based lifecycle (post silently → pin → refresh every 15 min → midnight roll-over → unpin/retire), no timers.
- **Ops self-alerts**: 10 ops notice kinds to an optional env-configured ops chat (without it: worker log + an admin banner).
- **History tools**: remove a false outage, reset history, restore drill (`post_restore`, `history_fingerprint`).
- **Security**: fail-closed config, login throttle (5 fails/60 s → 429 for 5 min), write-only bot tokens, device key reveal only by POST with `no-store`, HMAC-protected key regeneration, log redaction across app/gunicorn/Caddy, strict CSP.

**Size:** 12.1k lines of app Python (≈⅓ docstrings/comments), 41.3k lines of tests (1,281 test functions, 2,108 collected, all passing, 99% coverage on the gated modules). Admin frontend: 15 templates (673 lines) + one hand-written 287-line CSS file, **zero JavaScript**.

**Stack (pinned):** Python 3.14, Django 5.2.17, psycopg 3.3.6, gunicorn 26.2, WhiteNoise 6.12, requests 2.34, Pillow 12.3; uv lockfile with `exclude-newer = 7 days`; in-house Telegram client on `requests`; no Celery/Redis/DRF/`contrib.admin`/Node.

## 2. How it was built

- GSD 1.15.0, **quality** profile (Opus for every role, effort xhigh), yolo mode, standard granularity, parallel worktree executors; every optional gate on: research, plan-check, verifier, Nyquist, deep code review, ASVS L2 security audit, UI phase + UI review.
- 2026-09-30 → 2026-10-03: **347 commits, 45 plans, 23 waves, 58 worktree merges** across 5 phases (P1 11 plans, P2 10, P3 10, P4 8, P5 6).
- On top of core GSD: per-wave **adversarial audits** (3 skeptics per lens, majority vote), with every accepted finding fixed test-first (RED → GREEN evidence).
- `.planning/` is **gitignored** (`commit_docs: false`), so the design record (CONTEXT, UI-SPECs, reviews, UAT) exists only on this machine. Back it up.

## 3. Where it stands — nothing is formally closed

- **Every phase stopped at `human_needed`** (0 failed must-haves) and human verification was deferred each time. `/gsd-verify-work` and `/gsd-transition` never ran, which is why STATE shows 0% / 0 completed phases, ROADMAP shows `[ ]`, all 49 requirements show "Pending", and KD1–KD7 still read "— Pending".
- **30 UAT items are pending** (`.planning/phases/0N-*/0N-UAT.md`). Most need the production VPS, real devices or real Telegram (DoD 1–4 and 7 drills, restore drill, chart design approval, cross-bot unpin, etc.). A few are maintainer decisions (AR-03-01, IN-05, refined D-04).
- **Time-sensitive:** the first live DST change is **2026-10-25**; the chart "goes live only after /gsd-verify-work 03"; real subscriber channels should be connected only after Phase 2 is verified; production backups start after the next deploy (`backup.sh --dump-now` right after it). There is no recorded evidence that the production deploy has happened.
- **Stale state:** `milestone.lock` held by a dead pid (session 12321); `state.json` drift (`milestone: null`, phases 1–4 "in_progress"). `/gsd-health` or the next `/gsd-verify-work` should reconcile.
- **Gaps before `/gsd-complete-milestone`:** Phase 1 has no `01-SECURITY.md` (`/gsd-secure-phase 1`) and its `01-VALIDATION.md` is still draft (`/gsd-validate-phase 1`); DoD 7 (fresh VPS ≤ 30 min) must be re-run at milestone end.
- **Open deferred review findings:** P3 WR-01 (accepted risk awaiting maintainer confirmation), P3 IN-05 (caption decision), P5 WR-01 (future-dated dump name stops scheduled dumps while health stays green — v2 backlog), P5 IN-04/IN-07/IN-09. Accepted risks AR-02-01…AR-05-04, including "a failing backup sends no ops notice" (D-12).
- **Dev machine:** the data volume is ~96% full and Docker Desktop hung under parallel executors (WINDOWS.md). Free space before Phase 6.

**Path to close the milestone:** `/gsd-verify-work 01…05` (Phase 4/5 visual UAT items become moot — superseded by Phase 6) → transitions → `/gsd-secure-phase 1` + `/gsd-validate-phase 1` → `/gsd-audit-milestone` → `/gsd-complete-milestone`. The backend UAT (DoD drills, DST) is independent of Phase 6 and should not wait for it.

## 4. Deviations from the kickoff brief worth knowing

| Item | As built |
|---|---|
| DATA-01 heartbeat retention | No heartbeats stored at all, so there is nothing to prune (stronger than asked) |
| OPS-01 ops chat | Optional; without it notices go to the worker log + an admin banner |
| OPS-06 backups | A failing backup sends no ops notice (only health status + log) |
| LOC-04 delete | Soft tombstone (history, token and key kept; no undelete) |
| After a restore | Every location restarts as "waiting"; an outage in progress at server loss gets no ON alert (accepted D-13) |
| Extra services | A one-shot `migrate` service and a long-running `backup` container |
| **Admin UI** | **Stricter than the brief**: no JS, system fonts, a CSP that allows only one stylesheet and same-origin images (no `script-src`, `font-src` or `connect-src`), one ≤300-line CSS file, frozen after Phase 4. The brief only banned **third-party** runtime assets. These were GSD agent defaults (`01-UI-SPEC.md` rows sourced "default"), never maintainer decisions — see `PHASE-6-BRIEF.md` §7 |

## 5. Honest assessment

**Strong:** invariants enforced by the database itself; one lock-order rule; lease-fenced outbox; time injected everywhere; races tested on real PostgreSQL; deterministic chart goldens; thorough secret redaction and supply-chain pinning; extensive runbooks (README, 15 sections).

**Heavy for a ≤20-location, one-admin tool:** 12k app lines with 3.4× as much test code; `chart/lifecycle.py` alone is 1,280 lines; module docstrings full of audit IDs; engine logic as raw SQL constants; UI copy pinned verbatim across hundreds of assertions; `test_css.py` implements a CSS-cascade engine to test one red border. Not broken, but every future change pays this cost. Phase 6 is a good moment to stop pinning presentation in tests (see `TEST-STRATEGY.md`).
