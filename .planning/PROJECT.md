# Power Monitor

## What This Is

Power Monitor is a small self-hosted service for one admin. It tracks whether mains power is on at up to ~20 locations (homes and offices in Ukraine with scheduled and emergency blackouts), using heartbeats from a cheap mains-powered device at each location. Each location has its own Telegram channel. There, subscribers get an alert when power goes OFF and when it comes back ON, and they see a pinned weekly chart of when power was on and off that updates itself. It rebuilds a system that worked but was fragile. The goal is the same product, made correct and dependable, plus two small features: daily totals and ops self-alerts.

## Core Value

Subscribers get a timely, correct OFF/ON alert for every real outage and never a false, missed or duplicate one, plus a chart they can trust.

## Requirements

### Validated

(None yet — ship to validate)

### Active

Each requirement is user-visible or observable and testable. Context › How it works defines the terms.

#### Locations & admin (LOC)

- [ ] **LOC-01**: Admin can log in to the web admin panel with the single admin account defined in the environment; changing its username or password there and restarting changes the login and never leaves a second account.
- [ ] **LOC-02**: Admin can create a location with name, heartbeat period, grace period, Telegram bot token, chat/channel ID and language (uk/en/ru).
- [ ] **LOC-03**: Admin sees each location's current status (on / off / maintenance / waiting for first heartbeat), its last heartbeat time, and whether its alert delivery is healthy.
- [ ] **LOC-04**: Admin can edit and delete a location; deleting stops its alerts and unpins its chart.
- [ ] **LOC-05**: Admin sees device setup instructions for a location: heartbeat URL, device key, and copy-paste examples (curl, router cron line).
- [ ] **LOC-06**: Admin can regenerate a location's device key; the old key stops working immediately.
- [ ] **LOC-07**: Admin can send a Telegram test message to check a location's bot token and chat.
- [ ] **LOC-08**: Admin can toggle maintenance mode (no OFF detection or alerts; the chart shows the period as not monitored, not as an outage).
- [ ] **LOC-09**: Admin can toggle router-reconnect grace per location.
- [ ] **LOC-10**: Admin can turn alerts off for a location without affecting its chart.

#### Heartbeats (HB)

- [ ] **HB-01**: A device can report "alive" with one HTTP request authenticated by its location key, which a router cron job can send with curl/wget.
- [ ] **HB-02**: Requests with a missing or unknown key are rejected and never change any state.
- [ ] **HB-03**: A device gets its heartbeat response within 1 s whether or not Telegram is reachable (no outbound network I/O in the request path).

#### Power state (MON)

- [ ] **MON-01**: A location's first heartbeat starts monitoring with status ON and sends no alert.
- [ ] **MON-02**: A location is marked OFF when no heartbeat has arrived for period + grace (+ router-reconnect grace when it applies); the outage start is recorded as the last heartbeat time.
- [ ] **MON-03**: A location is marked ON at the first heartbeat after an outage; outage duration = restore time − outage start.
- [ ] **MON-04**: Each outage produces exactly one OFF and one ON transition, even with concurrent heartbeats, overlapping checks, or a second worker process started by mistake.
- [ ] **MON-05**: Server downtime is never reported as a power outage: after a restart every location gets a fresh detection window, the downtime is recorded as "not monitored", outages that begin during the downtime and continue after it are still alerted within one detection window, and an outage already in progress stays one outage.
- [ ] **MON-06**: After a database restart or a transient error, outage detection resumes within 60 s of the database accepting connections, with no manual action; an error for one location does not stop detection for the others.

#### Alerts (ALRT)

- [ ] **ALRT-01**: Subscribers receive an OFF alert in the location's language saying how long power was on.
- [ ] **ALRT-02**: Subscribers receive an ON alert saying how long power was off.
- [ ] **ALRT-03**: Alerts survive Telegram outages and process restarts: they are queued durably and retried until delivered, or expire after a configurable maximum age (default 6 h), in which case the admin is notified.
- [ ] **ALRT-04**: An alert delivered more than ~2 minutes after its transition was recorded states the actual event time (outage start for OFF, restore time for ON).
- [ ] **ALRT-05**: Subscribers never receive duplicate alerts for the same event.
- [ ] **ALRT-06**: Telegram rate limits are respected without delaying detection or alerts for other locations.

#### Weekly chart (CHRT)

- [ ] **CHRT-01**: Each location's chat has a pinned weekly chart (rows Mon–Sun × 24 h) that tells apart on, off, not monitored (maintenance / server downtime) and no data.
- [ ] **CHRT-02**: The pinned chart refreshes every 15 minutes; today's row runs up to a "now" marker and the future is empty.
- [ ] **CHRT-03**: Each day row shows that day's total off time and number of outages (daily totals).
- [ ] **CHRT-04**: The chart caption shows today's summary (off time, outage count) and the last-updated time.
- [ ] **CHRT-05**: At local midnight a new chart is posted and pinned, and the previous one gets a final update and is unpinned; midnights missed during downtime are caught up afterwards; no duplicate or orphaned pinned charts are left behind.
- [ ] **CHRT-06**: The chart is correct on DST-transition days and for outages that cross midnight.
- [ ] **CHRT-07**: Days after today show the previous week's same weekday, visually dimmed.
- [ ] **CHRT-08**: Chart labels are localized (uk/en/ru) and the visual design follows `docs/chart-spec.md`.

#### History (DATA)

- [ ] **DATA-01**: Power history (on / off / not-monitored intervals) is kept indefinitely; raw heartbeats, if stored at all, are deleted after a configurable retention period (default 30 days).
- [ ] **DATA-02**: Admin sees a location's recent outages and can remove a false one; the chart and totals update, and live monitoring is unaffected.
- [ ] **DATA-03**: Admin can reset a location's history.
- [ ] **DATA-04**: Changing a location's thresholds never rewrites past history.

#### Operations & self-alerts (OPS)

- [ ] **OPS-01**: Admin receives ops alerts in a dedicated admin Telegram chat configured in the environment.
- [ ] **OPS-02**: Admin is notified about monitoring gaps (server down, worker crashed or stuck, database unreachable): once when monitoring resumes, with the gap's start and end, and, if the database stays unreachable for more than 5 min, once while it is still down.
- [ ] **OPS-03**: Admin is notified when alert delivery for a location keeps failing (e.g. bot removed, bad token) and when it recovers.
- [ ] **OPS-04**: Admin is notified when every active location (at least 2, not in maintenance) goes silent at once, a probable server- or network-side problem.
- [ ] **OPS-05**: Every service has a container health check; a worker that crashes or whose loop stops making progress is restarted automatically (it exits by itself and Docker's restart policy restarts it; no extra supervisor container).
- [ ] **OPS-06**: The database is backed up nightly with retention (default 14 days), and a restore procedure is documented.
- [ ] **OPS-07**: One command runs the stack locally, and one command, run on the VPS, deploys or updates production; migrations run exactly once per deploy.
- [ ] **OPS-08**: Every service logs to stdout with size-capped Docker logs, and bot tokens and device keys never appear in any log (app, HTTP client library, proxy access log).

#### Security (SEC)

- [ ] **SEC-01**: Production refuses to start with missing or default secrets (secret key, admin password, DB password), and debug mode is off unless the local environment enables it.
- [ ] **SEC-02**: Only the TLS reverse proxy is publicly exposed; app ports bind to the internal network; secure cookies and HSTS are on.
- [ ] **SEC-03**: Repeated failed admin logins from one client IP are throttled with HTTP 429 (e.g. after 5 failures in 1 min).
- [ ] **SEC-04**: Bot tokens are write-only in the admin UI (shown masked, never sent back to the browser); device keys are masked and shown in full only on request, on the location's setup page.

### Out of Scope

| Feature | Reason |
|---|---|
| Multiple admin users, roles, self-service sign-up | Single admin by design |
| Admin-account extras: 2FA, password reset by email, audit log, admin REST API / OpenAPI, admin UI localisation or themes | One admin with env-provided credentials and SSH access to the VPS |
| Public web status page | Telegram is the subscriber UI (KD6) |
| Web charts or analytics in the admin panel (beyond the recent-outages list in DATA-02) | The Telegram chart is the chart; the admin panel is for configuration and status |
| Public API, webhooks, integrations other than Telegram | Nothing consumes them; Telegram is the only output (KD6) |
| SMS / email / push / Viber notifications | Telegram only; no paid services |
| Per-subscriber preferences (quiet hours, filters, subscriptions via bot) | Subscribers are read-only channel members; Telegram's own mute covers this |
| Flap detection, escalation policies, alert acknowledgements | Router-reconnect grace covers the known flapping case; subscribers are read-only |
| Server-side checks (pinging a router's public IP, polling a device URL) | Push heartbeats only: most devices sit behind NAT/CGNAT, and one mechanism keeps the engine simple |
| Native mobile app | Telegram already runs on every phone |
| Device firmware beyond copy-paste examples | Any device that can run curl/wget works; no firmware to maintain |
| Voltage / energy / UPS / battery metrics | Binary on/off only |
| Outage prediction or "probably off" guesses | Alerts must be facts, not guesses |
| Importing legacy data; legacy API compatibility | Fresh start, all devices new (KD5) |
| Encrypting bot tokens at rest | Covered by write-only tokens in the UI (SEC-04), an internal-only DB (SEC-02) and permission-restricted backups (OPS-06) |
| Prometheus / Grafana, Kubernetes, message brokers or queue infrastructure (Redis, RabbitMQ, Celery…) | Overkill for ~20 locations on one VPS; a DB-backed outbox is enough |
| Extra ops containers or services (autoheal, log shippers, external uptime or dead-man's-switch pingers) | Worker watchdog + Docker restart policy (OPS-05) and Telegram ops alerts (OPS-01…OPS-04) |
| CI/CD pipelines, container registries, zero-downtime or blue-green deploys | One VPS; deploying is one command on the VPS; deploy downtime is recorded as not monitored (KD3) |
| Automated off-site / cloud backups | No paid services; the README documents copying dumps off the box by hand |
| High availability / multi-server | One small VPS; downtime is handled as "not monitored" (KD3) |

Deferred scope (acknowledged, not in this milestone: ALRT-07, BOT-01, CHRT-09, STAT-01, STAT-02, TG-01, SCHED-01) is listed in `.planning/REQUIREMENTS.md` › v2 Requirements.

## Context

**Naming.** "Legacy system" means the old Django app that was shut down, which `docs/v1-lessons.md` describes. "v1 Requirements" means the first milestone of *this* project, in GSD's sense, and "v2" means GSD's deferred list. In `docs/v1-lessons.md`, "v1" always means the legacy system; in all companion docs, "v2" only ever means the deferred scope. This is a greenfield project: no code, data or API from the legacy system carries over (KD5). `docs/assets/mock_generator.py.txt` is a design reference saved as text on purpose.

**Reference docs:**
- `docs/v1-lessons.md`: the verified legacy defects, grouped by root cause, turned into invariants (INV-01…INV-26) and baseline scenarios (K-1…K-6) with Given/When/Then acceptance; its section 5 maps them to REQ-IDs. The Pitfalls researcher reads it, and every phase's discuss, research and plan steps read the parts that cover the phase's REQ-IDs. Its scenarios become the phase's acceptance tests (section 7 › Acceptance tests).
- `docs/chart-spec.md` + `docs/assets/`: the weekly chart image (layout, colours, text and duration formats, data and DST rules, acceptance checks). Binding for CHRT-01…CHRT-08. Its section 8 duration format also applies to alerts.
- Each phase in ROADMAP.md carries a `**Canonical refs**:` line naming the parts of these docs it needs (section 11).

### Who it's for

- **Admin (1 person, the maintainer).** Sets up locations and devices in a small web admin panel, keeps the VPS running, and gets ops alerts in a private admin Telegram chat.
- **Subscribers (family, neighbours, colleagues).** Read-only members of a location's Telegram channel or chat. They want to know right away when power goes off and comes back, and to plan around the week's pattern. They never use the web UI.
- **Devices (actors, not users).** One small mains-powered device per location with no UPS: a router running a cron job, an ESP32 or a Raspberry Pi. When power is lost, the device goes silent.

### How it works (domain rules)

**Location settings:** name; heartbeat period (s, default 60, min 10); grace (s, default 30, min 10); Telegram bot token + chat/channel ID (per location); language (`uk` / `en` / `ru`); alerts on/off; maintenance mode; router-reconnect grace on/off. All times are stored in UTC and displayed in the instance timezone (default `Europe/Kyiv`).

**Location status** (shown to the admin, LOC-03): on, off, maintenance, or waiting for first heartbeat.

**Heartbeat.** Every *period* seconds the device sends one HTTP request that carries its location key. It must work from a router cron job with plain `curl`/`wget`. The key goes in a header, with a query-parameter fallback for devices that can only set a URL (INV-24); planning may change this within HB-01 (router-friendly) and OPS-08 (keys never appear in logs, including proxy access logs).

**OFF rule.** A location is OFF when no heartbeat has arrived for more than `period + grace` seconds. If router-reconnect grace is on and the last heartbeat came within 300 s after power returned, add 180 s, because routers without a UPS reboot and reconnect right after power comes back. The outage is **backdated to the last heartbeat**.

**ON rule.** The location is ON at the first heartbeat after an outage.

**Durations.**
- "Was ON for" = last heartbeat − ON time.
- "Was OFF for" = restore time − outage start.
- ON and OFF durations tile the timeline exactly, apart from not-monitored spans.

Worked example (period 60 s, grace 30 s):
- Power came on at 08:53:10. The last heartbeat was at 14:05:10.
- At the first detection cycle after 14:06:40 (the timeout is strict: more than period + grace), the location is marked OFF, starting at 14:05:10, and the alert says "was ON for 5h 12m".
- The first heartbeat after that arrives at 17:20:10. The location is ON, and the alert says "was OFF for 3h 15m".
- Until 17:25:10, with router grace on, the OFF deadline is 270 s instead of 90 s.

**First heartbeat.** It starts monitoring with status ON and sends no alert.

**Maintenance mode.** No OFF detection and no alerts. The chart shows the period as *not monitored*. Leaving maintenance starts a fresh detection window.

**Alerts off.** State tracking and the chart keep working, and no alerts are sent. Alerts are suppressed, not queued.

**Server downtime** (restart, deploy, crash, database unreachable) is never an outage. After the worker starts, after any detection lapse, and after the web app starts, every location that was on gets a fresh detection window. The downtime is recorded as *not monitored*, and the admin gets one ops notice. An outage that begins during the downtime and continues after it is still alerted within one detection window. An outage that was already in progress stays one outage, with no second OFF alert (INV-11). If every active location goes silent at once, the likely cause is the server or network side: the admin is told (OPS-04). Subscriber alerts are not held by default (Open questions, question 2).

**Timeline states** (the single source of truth, KD1):

| State | Meaning |
|---|---|
| on | Power present (heartbeats arriving) |
| off | Outage, from the last heartbeat before detection to the first heartbeat after |
| not monitored | Maintenance mode, or the service itself was down |
| no data | Before the location's first heartbeat, after a history reset, and the future |

Stored intervals (on / off / not monitored) never overlap. Alerts, the chart and the daily totals are all read from this timeline.

**Alert format.** Telegram HTML, in the location's language: status emoji + **bold status**, then the duration of the previous state. Examples:

```
🔴 <b>СВІТЛО ЗНИКЛО</b>
⚡ Світло було: <b>5 год 12 хв</b>

🟢 <b>POWER ON</b>
⚡ Power was OFF for: <b>3h 15m</b>
```

Strings in each language:

| | uk | en | ru |
|---|---|---|---|
| OFF status | СВІТЛО ЗНИКЛО | POWER OFF | СВЕТ ВЫКЛЮЧИЛСЯ |
| ON status | СВІТЛО ПОВЕРНУЛОСЯ | POWER ON | СВЕТ ВЕРНУЛСЯ |
| Was ON for | Світло було | Power was ON for | Свет был |
| Was OFF for | Світла не було | Power was OFF for | Света не было |

**Durations in text.** Alerts, row totals and the caption share one duration formatter. Its units and rules are in `docs/chart-spec.md` section 8:
- Units: uk `д`/`год`/`хв`/`с`, en `d`/`h`/`m`/`s`, ru `д`/`ч`/`мин`/`с`. There is a space between number and unit in uk and ru (`5 год 12 хв`) and none in en (`5h 12m`).
- Zero parts are left out (`2h`, not `2h 0m`).
- Alerts show seconds only below 1 h (`12m 5s`, `45s`) and add a day unit from 24 h (`1d 5h`). Row totals and the caption show hours and minutes only.
- Values are rounded half up to the smallest unit shown, so an alert's "was OFF for" and the chart total for the same outage agree once rounded to minutes.

If an alert is delivered more than ~2 min after its transition was recorded, it also states the event's local time (ALRT-04; wording decided in planning).

**Telegram.** Each location has its own bot, which must be an admin of the channel with post, edit and pin rights. Ops alerts go to a separate admin chat set in the environment (bot token + chat ID).

**Weekly chart** (visual details in `docs/chart-spec.md`):
- **Layout:** 7 rows, Mon–Sun of the current week, × 24 h.
- **Today's row:** runs up to a "now" marker; the future is empty.
- **Days after today:** show the previous week's same weekday, dimmed.
- **Each row:** that day's total off time and outage count, e.g. `3h 20m · 2` under an "off time · outages" column header (exact formats and zero/empty-day rules in `docs/chart-spec.md` section 8).
- **Caption:** today's summary and the last-updated time.
- **Posting:** a new chart is posted and pinned in each location's chat at local 00:00. The previous day's message gets a final update and is unpinned.
- **Updates:** the chart is edited in place every 15 minutes.
- **Look:** localized title and weekdays (uk: Пн Вт Ср Чт Пт Сб Нд; all strings in `docs/chart-spec.md` section 8), bundled fonts with Cyrillic, one light theme.

### Background: the legacy system and why it is being rebuilt

A Django app (PostgreSQL, python-telegram-bot, svgwrite + cairosvg, gunicorn, one polling worker) was vibecoded in early 2026 and has since been shut down. The product idea and the rules above worked. The implementation had design-level defects, confirmed by a verification pass, and they cluster into six root causes. **Full evidence, reproductions and fix hints: `docs/v1-lessons.md`.** This stack is not carried over by default: the defects came from design, not from the framework.

- **C1 Non-atomic transitions with Telegram I/O inside them.** The restore path saved status, sent the alert, then saved the heartbeat time. A check in that window caused a negative duration, then an error, a ghost OFF state and a duplicate inflated ON alert. This hit roughly 6–30% of alerting restorations, and sometimes an OFF alert was never sent. Concurrent heartbeats or two workers duplicated events, and one exception aborted a check for all locations.
- **C2 Two sources of truth.** Alerts came from state + event rows, but the chart re-derived on/off from raw heartbeat gaps. So they disagreed: maintenance and server downtime were drawn as outages, config edits rewrote past days, and the admin's "delete event" could return a 500 error or destroy data.
- **C3 Server downtime looked like power loss.** Downtime longer than the timeout caused false alerts for every location, and a short deploy caused a false OFF/ON pair for each location that missed a heartbeat. A real outage during downtime never got an OFF alert. If ingress was down while the worker ran, every location got a false OFF alert.
- **C4 The worker was a silent single point of failure.** It never reconnected after a DB restart, swallowed all exceptions and had no health check. The midnight job only fired inside the 00:00 minute, with no catch-up. Telegram rate-limit sleeps (up to 93 s) blocked detection for every location.
- **C5 The Telegram message lifecycle was not transactional.** Messages were sent and pinned before the DB record existed. A pin failure left that day's chart never updated. Re-runs, resets and missed midnights left orphaned pinned messages.
- **C6 Ops and security gaps.** Insecure defaults applied when env vars were missing (DEBUG, public SECRET_KEY, admin/admin). The app port was published past the TLS proxy. Bot tokens were logged in cleartext. The device key went over plain-HTTP query strings. There were no backups and no tests. Log files were shared between processes, dependencies were unbounded, DST days broke the chart, the heartbeat table grew forever, and "alerts off" also silently stopped the chart.

### Open questions for `/gsd-discuss-phase`

Status: undecided. `/gsd-discuss-phase` asks the maintainer in the phase that owns the listed requirements; in `--auto` runs the recommended default applies. Research may inform them.

1. **(HB-01) Plain-HTTP heartbeats for devices that cannot do TLS?** Recommended default: HTTPS only. Allow HTTP only if a chosen device cannot do TLS, and then only for the heartbeat path.
2. **(OPS-04, MON-05) Besides the admin ops alert, also hold subscriber OFF alerts while all locations are silent?** Recommended default: **no.** OPS-04 only notifies the admin, and subscriber alerts behave normally.
   - *All-silent* (used by OPS-04 either way) starts when there are at least 2 active locations (monitored, not in maintenance) and each of them has gone longer than its own heartbeat period without a heartbeat, counted from max(last heartbeat, end of the last lapse). It ends when any heartbeat arrives.
   - Why no hold by default: the verified legacy trigger (the app port public past the proxy, blocked by 2 slow clients) is removed by SEC-02, and short web-app restarts are covered by the fresh detection window (MON-05). What is left, the VPS network or proxy failing for minutes while the worker keeps running, is rare. A hold would add about 30 s to every OFF alert to guard against it, which works against the Core Value ("timely").
   - If the maintainer answers yes: when all-silent starts, a fixed 3-min global hold begins, and OFF alerts not yet sent stay unsent. A location whose heartbeat returns during the hold gets neither its OFF nor its ON alert, and the gap becomes not monitored. Held OFF alerts go out when the hold ends, stating their event time (ALRT-04). With 2 or more active locations every OFF alert is sent no earlier than about 30 s after its transition, so the first location to time out can still be held. Success criterion 1 then allows +60 s instead of +30 s.
3. **(LOC-02) Per-location bot tokens or one shared bot?** Recommended default: per-location, as in the legacy system; a shared bot is v2 (TG-01). LOC-02 already assumes this default; choosing otherwise means editing that requirement.
4. **(CHRT-05) A new chart message every day, or one message per week?** Recommended default: daily, as in the legacy system (post + pin at local midnight). CHRT-05 already assumes this default; choosing otherwise means editing that requirement.
5. **(LOC-02, CHRT-08) Keep Russian (`ru`)?** Recommended default: keep uk/en/ru. LOC-02 and CHRT-08 already assume this default; choosing otherwise means editing those requirements.
6. **(MON-05, ALRT-02) An outage already in progress when server downtime or maintenance starts: what does its ON alert's "was OFF for" say?** Recommended default: restore time − original outage start, so the not-monitored span is included in the alert but excluded from the chart's off time (`docs/v1-lessons.md` INV-11).

### Success criteria / Definition of Done

v1 is done when all of the following can be observed. Each item names its requirements and the phase (Suggested roadmap shape, below) where it becomes checkable.

1. **Outage alerts** (MON-02, MON-03, ALRT-01, ALRT-02; chart part CHRT-02). Phase 1; chart part phase 3. Unplug one device while the other locations keep reporting: an OFF alert arrives within period + grace + 30 s (+180 s when router-reconnect grace applies). Power back: an ON alert arrives within 30 s of the first heartbeat. The chart shows the outage within 15 min.
2. **Server downtime** (MON-05, OPS-02; chart part CHRT-01). Phase 2; chart part phase 3. Stop the whole stack for 10 min while all devices stay powered, then start it again: no location alerts, one admin ops message with the downtime window, and the chart shows that window as not monitored.
3. **Telegram outage** (ALRT-03, ALRT-04, ALRT-05). Phase 2. Block the Telegram API for 10 min during an outage: alerts arrive after connectivity returns, each states the actual event time, and none are duplicated.
4. **Database restart** (MON-06, OPS-02). Phase 2. Restart only the database: detection resumes within 1 min with no manual action.
5. **Race tests** (MON-04, ALRT-05). Phase 2. Automated race tests (a heartbeat arriving while a check or transition runs; a second worker process started next to the first) never produce duplicate or ghost transitions.
6. **Chart edge cases** (CHRT-06). Phase 3. DST-transition days and cross-midnight outages render correctly (automated tests).
7. **Fresh deploy** (OPS-07). Phase 1, re-run at the end of the milestone. A fresh VPS reaches a working deployment in ≤ 30 min by following the README.
8. **Tests and coverage.** Every phase, per Constraints › Acceptance tests (a constraint, not a requirement; no REQ-ID). The test suite passes, with ≥ 80% coverage on the engine, alert and chart logic.

### Suggested roadmap shape (suggestion only)

This is a hint for the roadmapper, not a mandate. It uses coarse granularity and vertical slices, and maps all 49 requirements, each to exactly one phase.

1. **Walking skeleton.** A real device's heartbeat produces real OFF/ON alerts in a Telegram channel on the production VPS.
   Requirements: LOC-01, LOC-02, LOC-05, HB-01, HB-02, MON-01, MON-02, MON-03, ALRT-01, ALRT-02, OPS-07, SEC-01, SEC-02.
   **Canonical refs**: `docs/v1-lessons.md` K-1–K-4, K-6, INV-01, INV-21, INV-22, INV-24, INV-26; `docs/chart-spec.md` section 8 (duration format).
2. **Reliable detection, delivery and self-alerts.** No false, missed or duplicate alerts through races, restarts, DB restarts and Telegram outages, and the admin is told when the service itself is the problem.
   Requirements: HB-03, MON-04, MON-05, MON-06, ALRT-03, ALRT-04, ALRT-05, ALRT-06, OPS-01, OPS-02, OPS-04, OPS-05, OPS-08.
   **Canonical refs**: `docs/v1-lessons.md` INV-01, INV-02, INV-10, INV-11, INV-12, INV-13, INV-14, INV-15, INV-16, INV-20, INV-23.
3. **Weekly chart.** Rendering to the spec, daily totals, caption, and the pinned-message lifecycle with midnight catch-up.
   Requirements: CHRT-01, CHRT-02, CHRT-03, CHRT-04, CHRT-05, CHRT-06, CHRT-07, CHRT-08.
   **Canonical refs**: `docs/chart-spec.md`, `docs/assets/`, `docs/v1-lessons.md` K-5, INV-03, INV-04, INV-08, INV-17, INV-18, INV-19.
4. **Admin panel, history and backups.**
   Requirements: LOC-03, LOC-04, LOC-06, LOC-07, LOC-08, LOC-09, LOC-10, DATA-01, DATA-02, DATA-03, DATA-04, OPS-03, OPS-06, SEC-03, SEC-04.
   **Canonical refs**: `docs/v1-lessons.md` K-4, INV-05, INV-06, INV-07, INV-09, INV-16, INV-19, INV-20, INV-21, INV-23, INV-24, INV-25.

Notes for the roadmapper:
- Give every phase a `**Canonical refs**:` line in ROADMAP.md, as above. `/gsd-discuss-phase` copies it into CONTEXT.md, which phase researchers and planners read.
- The admin phase comes after the chart so that unpin-on-delete, maintenance shown as not monitored, alerts-off-keeps-chart and history corrections can be verified.

## Constraints

- **Scope (closed)**: v1 is exactly the 49 requirements LOC-01…SEC-04 in section 5. A feature that research calls table stakes but section 5 does not list goes to v2, unless a listed requirement cannot be met without it (then it is part of that requirement and gets no new ID). Nothing in Out of Scope comes back. When in doubt, pick the smaller solution: *"do not overengineer it, just make it slightly better and fix issues"* (maintainer).
- **Reference specs (binding)**: `docs/v1-lessons.md` (legacy failures as invariants INV-01…INV-26 and baseline scenarios K-1…K-6, with Given/When/Then acceptance; its section 5 maps them to REQ-IDs) and `docs/chart-spec.md` + `docs/assets/` (layout, colours, text and duration formats, data and DST rules, and acceptance checks for CHRT-01…CHRT-08; its duration format also applies to alerts). Read the parts that cover a phase's REQ-IDs before discussing, researching or planning it.
- **Acceptance tests**: each phase plan lists the INV/K scenarios for the requirements it delivers. Scenarios that run in-process (injectable clock, fake Telegram, a PostgreSQL test database) become automated tests named with their INV/K number, as do the `docs/chart-spec.md` section 10 checks for CHRT work. Scenarios that need real infrastructure (listed in `docs/v1-lessons.md` section 1) are run once as a manual or scripted check during `/gsd-verify-work`, and the result is recorded; do not build a test harness that starts, stops or restarts containers. A phase is not done while an affected scenario has no passing test or recorded check. Engine, alert and chart modules stay at ≥ 80% coverage at the end of every phase that touches them.
- **Architecture (locked from the walking skeleton on)**:
  - Web app + one worker + DB + TLS reverse proxy (+ a backup job). No broker, cache, orchestrator, metrics stack or extra services.
  - KD1–KD4: one stored timeline drives alerts, chart and totals; a transition is a single conditional DB write with no network I/O; alerts leave only through the DB outbox drained by the worker; server downtime is "not monitored".
  - Only one worker is active at a time: a second instance waits on a database lock (e.g. a PostgreSQL advisory lock), so the outbox and scheduled jobs need no multi-consumer claiming. Heartbeat-vs-worker races are handled by the conditional writes.
  - Inside the worker, two loops (threads or processes) are enough: detection, and Telegram I/O (alerts first, then chart work). The I/O loop sends one message at a time with short timeouts and skips bots that are backing off after a 429 or an error. No per-bot workers, async fan-out or task queue.
- **Scale**: ≤ ~20 locations, 1 admin, a few hundred subscribers across channels. This sets the size of everything: no horizontal scaling and no performance engineering beyond "don't block".
- **Hosting**: one small Linux VPS (1 vCPU, 1–2 GB RAM), Docker Compose, behind a TLS reverse proxy. This is the only infrastructure, so the memory budget matters (chart rendering, worker).
- **Budget / dependencies**: no paid services beyond the VPS. The Telegram Bot API is the only external dependency.
- **Time**: all stored times are UTC. The display timezone is instance-wide and configurable (default `Europe/Kyiv`). DST must be handled (CHRT-06).
- **Rendering**: the chart uses bundled fonts with Cyrillic coverage and never depends on system fonts, so rendering is deterministic.
- **Devices**: heartbeat period ≥ 10 s. Clients are dumb (curl/wget from cron, or a few lines on an ESP32), with no SDK.
- **Localization**: subscriber-facing text (alerts, chart) is uk/en/ru. The admin UI does not need localization.
- **Compatibility**: none with the legacy system: no API compatibility, no data import (KD5).
- **Tech stack**: **open, decided by GSD research** within these constraints (KD7). Pick a boring, well-supported stack with few dependencies, and pin versions (the legacy system broke on unbounded dependencies).
- **Soft preferences (maintainer conventions)**: research may deviate with a stated justification.
  - Python, PEP 8 + type hints, feature-module structure.
  - Docker Compose with `docker-compose.local.yml` and `docker-compose.prod.yml`.
  - `.env.example` committed; `.env.docker_local` and `.env.docker_production` gitignored.
  - One container per service; PostgreSQL container as the default DB; persistent data under `docker_data/` (separate directories for local and prod). Local compose: only the app publishes a port. Production compose: the TLS reverse proxy is a service in `docker-compose.prod.yml` and is the only service that publishes host ports (80/443); the app and DB are reachable only on the internal network (SEC-02).
  - Tests in `/tests`. Every tested function gets an expected case, an edge case and a failure case. ≥ 80% coverage on critical business logic. External APIs (Telegram) are mocked.

(In these constraints, "section 5" means Requirements › Active above, and "section 7 › Acceptance tests" means Constraints › Acceptance tests.)

## Key Decisions

| Decision | Rationale | Outcome |
|---|---|---|
| KD1 One source of truth: the engine's stored power timeline (intervals: on / off / not monitored) drives alerts, the chart and totals; the chart never re-derives state from raw heartbeats (locked) | Removes C2 (chart vs alert disagreement, history rewrites, broken event deletion) | — Pending |
| KD2 State transitions are atomic conditional updates; no network I/O inside a transition or in the heartbeat request; alerts go through a DB-backed outbox drained by the worker (locked) | Removes C1 and the blocking part of C4 (races, ghost states, duplicate or missed alerts, Telegram stalls) | — Pending |
| KD3 Server downtime is "not monitored", never "off": fresh detection window after a restart or lapse + downtime intervals + one ops notice (locked) | Removes C3 (false alerts on restart/deploy, missed OFF after downtime) | — Pending |
| KD4 The worker is supervised and self-healing: container health check, DB reconnect, and an in-process watchdog that exits the process when its loop stalls so Docker's restart policy restarts it (no autoheal or sidecar container); scheduled jobs are conditions checked every cycle, so they catch up after missed runs (locked) | Removes C4 and C5 (silent dead worker, missed midnight posts, orphaned pins) | — Pending |
| KD5 Fresh start: no legacy compatibility or data import (locked) | Maintainer decision; old system is shut down and all devices are new | — Pending |
| KD6 Telegram is the only subscriber surface; the web UI is admin-only (locked) | Scope: keeps the product small | — Pending |
| KD7 Tech stack (language, web framework, chart renderer, Telegram client, scheduler) | Research decides within section 7; soft preference for Python | — Pending |

## Evolution

This document evolves at phase transitions and milestone boundaries.

**After each phase transition** (via `/gsd-transition`):
1. Requirements invalidated? → Move to Out of Scope with reason
2. Requirements validated? → Move to Validated with phase reference
3. New requirements emerged? → Add to Active
4. Decisions to log? → Add to Key Decisions
5. "What This Is" still accurate? → Update if drifted

**After each milestone** (via `/gsd-complete-milestone`):
1. Full review of all sections
2. Core Value check — still the right priority?
3. Audit Out of Scope — reasons still valid?
4. Update Context with current state

---
*Last updated: 2026-09-30 after initialization*
