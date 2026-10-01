# v1 lessons: failure modes to design out

**Naming:** in this file "v1" always means the legacy Django system that was shut down. It is unrelated to "v1 Requirements" in `.planning/REQUIREMENTS.md`, which are this project's first milestone. "v2" here means GSD's deferred list.

Source: a September 2026 investigation of the v1 codebase (Django, last changed Feb 2026). Most "v1 failure" notes were reproduced by a test harness run against v1 code. The restoration race, the DB restart, the gunicorn timeout and the slow-client cases were also confirmed on real PostgreSQL and gunicorn; the double-heartbeat, two-worker and server-restart cases used deterministic interleavings. Points confirmed only by reading code or config are marked "(code read)". v1 is shut down and nothing here implies compatibility with it (decision KD5). Mechanisms are described without reference to a framework. Where a line mentions a Django or library detail, it is only to say what v1 did.

## 1. Purpose and how to use this file

- **Readers:** the researchers, planners and verifiers for any phase that touches the engine (heartbeats, state, timeline), alerts, the chart, or ops/deploy.
- **Rule:** each such phase plan lists the INV-IDs it affects. Scenarios that run with the injectable clock, fake Telegram and a PostgreSQL test database become automated tests with the same numbers, named with their INV/K number (simulate a dropped DB connection with `pg_terminate_backend`). Scenarios that need real infrastructure are run once as a written manual or scripted check during `/gsd-verify-work`, with the result recorded in the phase verification file: INV-13 against a real DB container restart, a 5-min DB outage and the Docker restart after a watchdog exit; INV-22 #2 (slow clients through the proxy); INV-25 #2 (restore); INV-26 (fresh VPS, deploy with a migration, README accuracy); and brief section 10 items 2, 3, 4 and 7. Do not build a test harness that starts, stops or restarts containers. A phase is not done while an affected scenario has no passing test or recorded check.
- **Where the IDs go:** list INV/K IDs in plan objectives, task text and test names, not in PLAN.md `requirements:` frontmatter, so GSD's requirement-coverage checks see only real REQ-IDs.
- **Test defaults** (unless a scenario says otherwise): heartbeat period 60 s, grace 30 s (effective timeout 90 s), router-reconnect grace off, language en, display TZ Europe/Kyiv. Time comes from an injectable clock. Telegram is faked at the HTTP boundary, and a global network guard makes any real outbound call fail the test.
- **Race scenarios** (INV-01, INV-02) run against the production DB engine (PostgreSQL in a test container). They use real parallel requests or processes, or a deterministic hook between steps. In-memory SQLite cannot show these races.
- **Text formats:** row totals and captions are quoted in the en format of `docs/chart-spec.md` section 8; that spec wins on any formatting difference. Alert durations use the same formatter, with the alert rules in PROJECT-BRIEF section 3 (seconds below 1 h, a day unit from 24 h).
- **Vocabulary:** *Timeline:* the engine's stored intervals, each one of **on / off / not monitored**. **No data** is the time before monitoring started, and the future. *Transition:* an on→off or off→on change recorded in the timeline. *Effective timeout:* period + grace, plus 180 s router-reconnect grace when it applies. *Detection cycle:* one pass of the worker over all locations. *Lapse:* a gap between completed detection cycles above the lapse threshold (INV-10). *Outbox:* the durable DB table of messages waiting to be sent to Telegram.
- **Scope:** this file is not a spec. It states what must hold. How to achieve it is the planner's call, within the decisions in PROJECT.md (KD1–KD7). Chart visuals are specified in `docs/chart-spec.md`.

## 2. Keep: what v1 got right

- **Heartbeats:** a heartbeat is any authenticated HTTP request from a mains-powered device without a UPS (a router cron job, an ESP32). The server's receive time is the only timestamp, so device clocks never matter.
- **Timeout rule:** off when `now - last heartbeat > period + grace` (strict `>`). Period and grace are each >= 10 s, with defaults 60 s and 30 s.
- **Router-reconnect grace** (per-location toggle): add 180 s to the timeout when the last heartbeat arrived <= 300 s after the location's latest on transition (inclusive). The reason: routers without a UPS reboot and reconnect right after power returns, which would otherwise look like a second short outage.
- **Start of monitoring:** the first heartbeat starts monitoring as **on**, silently.
- **Off start:** an off transition is backdated to the last heartbeat. The on transition happens at the first heartbeat after the outage.
- **Durations:** "was ON for" = last heartbeat - on time. "was OFF for" = restore time - outage start. The on and off durations tile the timeline exactly. Example: heartbeats 10:00-10:05, then silence, then a heartbeat at 11:00 gives "ON for 5m" and "OFF for 55m".
- **Maintenance mode:** no off detection or alerts, while heartbeats and restoration keep working.
- **Alert format:** emoji + bold localized status + previous-state duration, for example `🔴 POWER OFF` / `⚡ Power was ON for: 5h 12m`. The only text is static strings plus numbers, so there is no HTML-injection surface. Anything user-typed that gets added later must be escaped.
- **i18n:** uk/en/ru tables with identical key sets and fallback to en. v1 had a test-worthy property here: no missing keys.
- **Chart concept:** 7 rows, Mon-Sun × 24 h. Days after today show the same weekday of the previous week, dimmed. A new chart is posted and pinned at local 00:00, and the previous one gets a final edit and is unpinned. The chart is edited in place every 15 min. v1 installed Cyrillic-capable fonts in the image (its uk rendering was verified); the rebuild bundles its fonts for deterministic rendering.
- **Admin panel:** the test-message button and the device setup info on the location page. Destructive actions are POST-only, with CSRF protection and a confirmation step. Output is escaped: a location named `<script>…` rendered safely everywhere.
- **Other:** all stored times are UTC, and the engine's duration arithmetic on aware UTC timestamps was DST-safe. Only one process ran migrations. Each location has its own bot, so flood limits are rarely hit.

**Baseline scenarios (must pass as-is):**
- K-1: Given a new location, when its first heartbeat arrives at 08:00:00, then its status is on, no alert is sent, and the chart shows no data before 08:00. (MON-01)
- K-2: Given heartbeats every 60 s until 10:05:00 and then silence, then the off transition is recorded after 10:06:30 (not at it) with outage start 10:05:00. The OFF alert is queued in the location's language with "Power was ON for" = 10:05:00 - the on time. (MON-02, ALRT-01)
- K-3: Given that outage, when a heartbeat arrives at 11:00:00, then the ON alert says "Power was OFF for: 55m". (MON-03, ALRT-02)
- K-4: Given router grace on and an on transition at 12:00:00:
  - with the last heartbeat at 12:04:00, the off transition is not recorded before 12:08:30;
  - with the last heartbeat at 12:05:30, the off transition is recorded at the first cycle after 12:07:00;
  - with the last heartbeat at exactly 12:05:00, the grace still applies. (LOC-09)
- K-5: Given a Wednesday, then the Thu-Sun rows show the previous week's Thu-Sun, dimmed. Titles and weekday names are localized and Cyrillic renders in uk and ru. (CHRT-07, CHRT-08)
- K-6: Given a location form with a period or grace below 10 s, then the save is rejected with a field error. (LOC-02)

## 3. Invariants

### C1: State transitions are atomic, with no network I/O inside them

#### INV-01 A transition is one conditional write that does no network I/O
- **Rule:** A transition writes status, transition time and last-heartbeat time together, in one conditional update that succeeds only if the row still matches the state the decision was based on. If 0 rows change, someone else already decided, and the transition is skipped quietly. No network call runs inside a transition or anywhere in the heartbeat request. A computed negative duration means a stale decision. It must never be partially written.
- **v1 failure:** The restore path saved status=on, sent the Telegram alert inside the HTTP request, and only then saved the heartbeat time. A detection cycle that ran inside that window saw "on + a heartbeat hours old" and committed off. The window is one Telegram round trip, 0.3-1.5 s against a 5 s cycle, so this hit about 6-30% of alerting restorations. The cycle then failed the DB's non-negative CHECK on the duration. The result was a ghost off state and a second, inflated "POWER ON" alert (2h 59m, then 3h). If power died again before the next heartbeat, no OFF alert was ever sent. When the web server killed a request stuck in a slow send, it left the same half-written state.
- **Acceptance:**
  - Given a location off since 10:01:00, when the restoring heartbeat arrives at 13:00:00 and a detection cycle runs between the heartbeat's read and its write (deterministic hook or real concurrency), then there is exactly 1 on transition and 1 queued ON alert ("was OFF for 2h 59m"), the status is on, and the cycle raises no error.
  - Given the same restore, when the device goes silent right after 13:00:00, then an off transition dated 13:00:00 is recorded and its OFF alert is queued by 13:01:30 plus one detection cycle.
  - Given Telegram hangs for 60 s on every call, when a restoring heartbeat arrives, then the HTTP response returns in under 1 s and the status is already on.
- **Covers:** MON-03, MON-04, HB-03, ALRT-05

#### INV-02 Exactly one transition and one alert per event under concurrency
- **Rule:** Heartbeats, overlapping detection cycles, a second worker process started by mistake and admin edits may interleave in any order. The result is still one transition and one alert per real event. Only one worker is active at a time: a second instance waits on a database lock (e.g. a PostgreSQL advisory lock), so the outbox and scheduled jobs need no multi-consumer claiming. Heartbeat-vs-worker races are handled by the conditional writes of INV-01. Saving configuration writes only config fields and never state fields.
- **v1 failure:** v1 had no locks or conditional updates anywhere. Two restoring heartbeats at the same moment produced two ON events and two alerts. A second worker produced duplicate OFF alerts. The admin edit form saved the whole row from a snapshot. That brought back "on" after the detector had set "off", and a second "POWER OFF" alert followed.
- **Acceptance:**
  - Given a location that is off, when two restoring heartbeats hit the endpoint in parallel, then 1 on transition and 1 ON alert exist.
  - Given a second worker process started next to the first on the same DB, when a location times out, then 1 off transition and 1 OFF alert exist.
  - Given the detector sets off at 17:02:30, when the admin saves a grace change at 17:02:31 using a form loaded while the status was on, then the status stays off and only one OFF alert exists.
- **Covers:** MON-04, ALRT-05

### C2: One source of truth, the stored timeline

#### INV-03 The timeline alone drives alerts, the chart, daily totals and the caption
- **Rule:** The chart, the per-day totals ("3h 20m · 2", format in `docs/chart-spec.md` section 8) and the caption are read from the stored timeline and never re-derived from raw heartbeats. The timeout and grace rules live in exactly one module. An outage's off span on the chart equals the alert's "was OFF for". For an outage that spans a not-monitored interval (INV-11), off span + the not-monitored span inside the outage = "was OFF for".
- **v1 failure:** Alerts came from the location's state, while the chart re-classified heartbeat gaps using the current timeout. Red started at last heartbeat + 90 s. The chart showed 7110 s for an outage the alert reported as 7260 s. The chart restarted the router grace from the previous day's last heartbeat, so it hid 90-270 s outages just after midnight that the engine did alert on. Outages the blocked detector missed showed as red with no alert, and ghost-off states showed as green.
- **Acceptance:**
  - Given heartbeats until 10:00:00 and the next one at 12:00:00, then the ON alert says "was OFF for 2h"; the off span on the chart is exactly 10:00:00-12:00:00; the row total reads "2h · 1"; the caption at 12:05 shows the same today totals and the time of the last update.
  - Given any day in any test, then its on + off + not monitored + no data intervals do not overlap and add up to the day's length (24 h, or 23 h / 25 h on DST days).
  - Given the first heartbeat on Tuesday at 08:00, then Monday and Tuesday 00:00-08:00 are no data and count toward neither off time nor outages.
- **Covers:** CHRT-01, CHRT-03, CHRT-04, MON-02, MON-03

#### INV-04 "Not monitored" is never "off"
- **Rule:** Maintenance periods and detection lapses (INV-10) are stored as not monitored and drawn in the not-monitored style from `docs/chart-spec.md`. They are excluded from off time and outage counts. An off interval never overlaps a not-monitored interval. An outage that begins at the end of one starts at max(last heartbeat, end of the not-monitored interval). An outage already in progress when a not-monitored interval starts stays one outage (INV-11).
- **v1 failure:** With maintenance on and a 2 h heartbeat gap, the engine correctly sent nothing (0 events), but the chart showed 10:00:30-12:00 as red. Server downtime was drawn red for every location. When maintenance was switched off, the next cycle sent an OFF alert backdated to the heartbeat from before maintenance.
- **Acceptance:**
  - Given maintenance on from 10:00 to 12:00 and no heartbeats in that span, then 0 alerts are sent, the chart shows 10:00-12:00 as not monitored, and the row total shows the "no outages" text.
  - Given maintenance switched off at 12:00 while the device is still silent (last heartbeat 09:58), then no OFF is recorded before 12:01:30, and the outage starts at 12:00, not 09:58.
- **Covers:** LOC-08, MON-05, CHRT-01, CHRT-03

#### INV-05 Each toggle has exactly one effect
- **Rule:** "Alerts off" stops only the messages to subscribers. Transitions, the timeline, the chart and the midnight re-pin carry on. Maintenance only suppresses off detection. Router grace only changes the effective timeout for future decisions. Whether an alert is sent is decided when its transition is recorded.
- **v1 failure:** Every chart job (and the refresh/sync/reset commands) filtered on "alerting enabled". Muting alerts therefore also silently stopped the chart and left yesterday's chart pinned. (code read)
- **Acceptance:**
  - Given alerts off for location A, when A has an outage, then subscribers get 0 messages, while the timeline, the 15-min refresh, the totals and the midnight re-pin all work as normal.
  - Given alerts off when the off transition is recorded and back on before power returns, then only the ON alert is sent.
- **Covers:** LOC-08, LOC-09, LOC-10

#### INV-06 Changing thresholds never rewrites history
- **Rule:** Period, grace and router-grace changes apply only to decisions made after the change. Stored intervals are never recomputed.
- **v1 failure:** The chart applied the current timeout to every past day. Raising grace from 30 s to 600 s turned an 8-min outage green. Changing 60/30 to 30/10 turned one 4 h outage into 1200 red slivers totalling 10.67 h.
- **Acceptance:**
  - Given yesterday has an outage 10:00-10:08 ("8m · 1"), when the admin sets grace to 600 s, then yesterday's row and totals are unchanged.
  - Given the last heartbeat 80 s ago, when the admin lowers grace from 30 s to 10 s, then the next cycle records off, starting at that last heartbeat.
- **Covers:** DATA-04

#### INV-07 Admin corrections edit the timeline only, never heartbeats or live detection
- **Rule:** "Remove false outage" turns a closed off interval into on and recomputes that day's totals and chart. It sends no message. "Reset history" clears the timeline and returns the location to "waiting for first heartbeat". Neither action touches raw heartbeats. No admin action may leave a location in a state the detector ignores. An outage still in progress cannot be removed (default).
- **v1 failure:** Event delete was either broken or destructive. It returned HTTP 500 for any event that had a later event, because it saved a field that does not exist. Deleting the newest ON event deleted the real heartbeats that followed it (120 → 60), flipped the status to off, and made the next heartbeat send a false "POWER ON, was OFF for 1h 1m". Deleting a new location's only event left its status "unknown" forever, a state the detector never checked, so a later 1 h outage produced no alert.
- **Acceptance:**
  - Given a day with outages 09:00-10:00 and 15:00-15:30, when the admin removes the first, then 09:00-10:00 becomes on; the totals change from "1h 30m · 2" to "30m · 1"; the second outage is untouched; 0 messages are sent; the current status is unchanged.
  - Given a location that has just started monitoring, when the admin resets its history, then later heartbeats restart monitoring (K-1), and a 1 h silence after that produces exactly one OFF alert.
  - Given an outage in progress, when the admin tries to remove it, then the action is refused with an explanation.
- **Covers:** DATA-02, DATA-03

#### INV-08 Day geometry follows the local wall clock (DST, midnight, now)
- **Rule:** Rows and totals are built per local calendar day in the display TZ. Each interval boundary is converted to local time on its own. Never compute a position as "local midnight + elapsed seconds". A day lasts whatever the TZ says: 23, 24 or 25 h. Intervals are split at local midnight. Today's row ends exactly at the now-marker, and nothing is drawn after it.
- **v1 failure:** v1 computed positions as elapsed seconds since midnight, which mixed UTC and local arithmetic. On 2026-03-29, a 10:00-12:00 outage was drawn at 09:00-11:00. On 2026-10-25, a 10:00-12:00 outage was drawn at 11:00-13:00, a 23:30-23:50 outage vanished, and the live bar ran 1 h into the future. Earlier versions had two other bugs. They mapped the next midnight to 0.0 and dropped every segment that ended at 24:00, so a 22:30-24:00 outage vanished. One painted the hours before monitoring started, on the first day, in red.
- **Acceptance:**
  - Given 2026-03-29 in Europe/Kyiv (23 h; 03:00-04:00 does not exist) with an outage 10:00-12:00 local, then the bar is at 10:00-12:00 and the total is "2h · 1".
  - Given 2026-10-25 (25 h; 03:00-04:00 happens twice) with outages 10:00-12:00 and 23:30-23:50, both are drawn at those positions and the total is "2h 20m · 2". Separately, an outage from 03:30 EEST (00:30 UTC) to 03:30 EET (01:30 UTC) counts as 1h. How the repeated hour is drawn follows `docs/chart-spec.md` section 9.
  - Given an outage from Mon 22:30 to Tue 01:15, then Monday shows off 22:30-24:00 ("1h 30m · 1"); Tuesday shows off 00:00-01:15 ("1h 15m · 1"). An outage counts on every day it touches (`docs/chart-spec.md` section 8). At Tue 00:10, with the outage still open, Tuesday's row shows off 00:00-00:10 and nothing after the now-marker.
- **Covers:** CHRT-06, CHRT-02, CHRT-03

#### INV-09 Raw heartbeats are disposable input
- **Rule:** Heartbeats, if stored at all, are pruned after the retention period (default 30 days, configurable). The timeline is kept indefinitely. Nothing needs pruned heartbeats: not the chart, the totals, outage removal, reset, or status.
- **v1 failure:** The chart was built from raw heartbeats, so they could never be pruned. That is about 0.5 M rows per location per year at 60 s, or 3.15 M at 10 s, with no retention. Admin corrections created or deleted heartbeat rows to steer the chart.
- **Acceptance:**
  - Given 40 days of history, when the nightly prune runs, then heartbeats older than 30 days are gone, and every row, total and outage for those days is identical to before the prune.
  - Given retention set to 7 days, then only the last 7 days of heartbeats remain.
- **Covers:** DATA-01

### C3: Server downtime is never reported as a power outage

#### INV-10 Detection lapses are "not monitored", and every restart gives a fresh window
- **Rule:** The worker stores a "last completed detection cycle" timestamp. A lapse is any gap between completed cycles above the lapse threshold: the whole stack down, the worker down or stuck, or the DB unreachable. Keep the threshold below the smallest effective timeout the instance allows (period and grace are each ≥ 10 s, so 20 s); suggested 15 s. Then a stalled detector never leaves a real silence recorded as plain on: either a cycle saw the silence, or the stall is recorded as a lapse. (Detection still has a resolution of one cycle: a silence that exceeds the timeout by less than the gap between two cycles may end before any cycle sees it.) A lapse is recorded as not monitored for every monitored location, and produces 0 subscriber alerts and exactly 1 ops notice stating the window (OPS-02). After the worker starts, after any lapse, and after the web app (which receives the heartbeats) starts, the timeout of each location that is on counts from max(last heartbeat, that moment). The web app records its start time in the DB for this; that is its only part in recovery. A location that is already off stays off (INV-11). Only the worker runs detection and restart recovery.
- **v1 failure:** The web container ran recovery at boot and silently marked timed-out locations off. The first heartbeat after that sent a false "POWER ON, was OFF for 10-40s" for every location. If the worker's first check won the race instead, every location got a false OFF + ON pair. A 20 s web-only redeploy that swallowed one heartbeat produced a false OFF + ON pair.
- **Acceptance:**
  - Given 3 locations heartbeating every 60 s, when the whole stack is stopped 10:00-10:10 and started again, then subscribers get 0 alerts; the admin gets 1 ops message stating about 10:00-10:10; all 3 charts show that window as not monitored.
  - Given a device that heartbeats at second :10 of each minute, when only the web app is redeployed 10:00:05-10:00:25 and the 10:00:10 heartbeat is lost, then no off transition happens (the 10:01:10 heartbeat arrives before 10:00:25 + 90 s).
  - Given the detection loop is stalled 10:10-10:14 while heartbeats are still accepted, then 10:10-10:14 is not monitored for all locations, subscribers get 0 alerts for that span, and the admin gets 1 ops notice.
  - Given the last heartbeat at 10:00:00 and detection cycles stalled 10:01:00-10:01:40, when the next heartbeat arrives at 10:01:35, then the timeline shows on until 10:01:00 and not monitored 10:01:00-10:01:40 (the stall exceeded the lapse threshold), no subscriber alert is sent, and the admin gets 1 ops notice.
- **Covers:** MON-05, MON-06, OPS-02

#### INV-11 An outage that continues after a restart is alerted exactly once
- **Rule:**
  - A location that was on when the lapse started and stays silent for one full window after it gets an off transition, starting at the end of the not-monitored interval, and a normal OFF alert. An outage that began and ended inside the lapse cannot be known and is not reported.
  - An outage already in progress when a lapse or maintenance starts stays one outage. After the restart (or when maintenance ends) the location stays off, with no second OFF alert and no fresh-window re-detection. The not-monitored span is stored inside the outage. The outage counts once in the outage count, and its off time excludes the not-monitored span.
  - The ON alert is still sent at restoration if the OFF alert was sent. Recommended default (brief section 9, question 6): "was OFF for" = restore − original outage start, so INV-03's equality becomes off span + not-monitored span inside the outage = "was OFF for".
- **v1 failure:** A real outage that began during downtime never got an OFF alert. The later ON alert said "was OFF for 1h 40m" for a 2 h outage.
- **Acceptance:**
  - Given location A's last heartbeat at 09:59:00 and the stack down 10:00-10:10, when A stays silent, then A's OFF alert is queued by 10:11:30 plus one cycle; the timeline shows 10:00-10:10 not monitored and off from 10:10; when power returns at 11:00, the ON alert says "was OFF for 50m", matching the chart.
  - Given A off since 09:00 with its OFF alert sent, when the stack is down 10:00-10:10 and power returns at 11:00, then no second OFF alert is sent; the timeline is off 09:00-10:00, not monitored 10:00-10:10, off 10:10-11:00; the row total is "1h 50m · 1"; exactly one ON alert is sent ("was OFF for 2h" under the default).
- **Covers:** MON-05, ALRT-01, ALRT-02, CHRT-03

#### INV-12 When every location goes silent at once, tell the admin
- **Rule** (recommended default, brief section 9 question 2: notify only, no hold):
  - *All-silent* starts when there are at least 2 active locations (monitored, not in maintenance) and each has gone longer than its own heartbeat period without a heartbeat, counted from max(last heartbeat, end of the last lapse). It ends when any heartbeat arrives. The admin gets 1 ops alert when it starts and 1 when it ends (INV-20).
  - Subscriber alerts are not changed by all-silent. The legacy trigger for mass false alerts is removed at its source instead: only the proxy is exposed (INV-22), and web-app restarts give a fresh detection window (INV-10).
  - If the maintainer opts into the hold in discuss-phase, the brief's section 9 question 2 defines it. The planner then replaces the acceptance scenarios below: during the hold subscribers get 0 alerts and the silent spans become not monitored; silence that outlasts the 3-min hold leads to each OFF alert being sent once, stating its real outage start.
- **v1 failure:** With 3 locations and ingress down 14:10-14:13 while the worker ran, v1 sent 6 alerts (3 false OFF + 3 false ON), and the admin was never told the server side was the problem. The app port was public, with 2 sync workers, so 2 idle slow clients blocked all heartbeats. In the reproduction a heartbeat timed out after 12 s.
- **Acceptance (default):**
  - Given 3 active locations whose last heartbeats fall between 14:09:10 and 14:10:00, when heartbeats resume only at 14:13:00-14:13:30, then the admin gets 1 ops alert at about 14:11:00 and 1 recovery notice, and subscriber alerts follow the normal rules.
  - Given 2 active locations and only A goes silent, then no ops alert is sent and A's OFF alert is sent normally.
  - Given only 1 active location, then all-silent never triggers.
- **Covers:** OPS-04, MON-05

### C4: The worker is supervised and heals itself

#### INV-13 Detection survives DB restarts, errors in one location, and hangs
- **Rule:**
  - Each cycle starts by checking that the DB connection is usable, and reconnects if it is not. An error for one location is logged, and the other locations are still processed.
  - Docker Compose restart policies act only when a process exits; an "unhealthy" status alone restarts nothing. So a watchdog inside the worker (e.g. a thread that checks when each loop last made progress) exits the process non-zero when a loop has made no progress for N s, for example because it hangs on a call that never returns. Docker's restart policy then restarts it. Do not add an autoheal or supervisor container.
  - An unreachable DB is not a hang. The loop keeps retrying the DB, which counts as progress, the health check reports unhealthy, and the worker does not exit, so the incident gets one notice. If the DB stays unreachable for more than 5 min, the worker sends the admin one direct notice straight to Telegram (the outbox lives in the DB), using the admin bot token and chat from the environment (OPS-02). When the DB is back, the lapse is recorded per INV-10.
- **v1 failure:** After a PostgreSQL restart, the worker's connection stayed dead: every 5 s it logged "the connection is closed". v1 never discarded broken connections. All exceptions were swallowed, and there was no health check, so the restart policy never fired. Detection stopped silently until someone restarted the worker by hand. One location's exception also aborted the cycle for the locations after it.
- **Acceptance:**
  - Given the worker running on PostgreSQL, when the DB container restarts (down about 10 s), then a cycle succeeds within 60 s of the DB accepting connections, and a location that times out afterwards is alerted, with no manual action.
  - Given the DB unreachable for more than 5 min, then the worker keeps retrying and reports unhealthy, the admin gets exactly 1 direct notice while the DB is still down, and when the DB returns, the lapse is recorded per INV-10 and 1 resume notice is sent.
  - Given the detection loop hangs on a call that never returns, then the worker process exits within N s (and Docker restarts it; the restart itself is a manual check).
  - Given location A's processing raises on every cycle, then location B still times out and is alerted on schedule.
- **Covers:** MON-06, OPS-05, OPS-02, MON-02

#### INV-14 Telegram and chart rendering never block detection or heartbeats
- **Rule:** No Telegram call, retry wait or chart render runs in the detection path or in the heartbeat request. A 429 flood limit delays only that bot's messages. The retry is scheduled for later, never waited out with a sleep in a shared loop.
- **v1 failure:** One thread ran detection, alerts and chart uploads. A 429 with retry_after=30 slept 31 s, three times over (93 s, including after the last attempt). Detection stopped for every location during that time. The same client ran inside the web request, where the 30 s server timeout killed requests mid-transition. An unreachable Telegram cost about 18 s per call.
- **Acceptance:**
  - Given every call for bot A returns 429 retry_after=30, when location B goes silent, then B's off transition is recorded within 90 s plus one cycle, and B's alert is delivered without waiting on A.
  - Given Telegram unreachable (connect timeouts), then heartbeats answer in under 1 s and cycles keep their normal cadence.
  - Given the midnight chart job for 20 locations, then detection cycles keep their normal cadence during it.
- **Covers:** ALRT-06, HB-03, MON-02

### C5: Telegram delivery and the chart message lifecycle

#### INV-15 Alerts go through a durable, ordered outbox
- **Rule:** An alert is written to the outbox in the same transaction as its transition. The worker delivers alerts per location, in event order, with capped backoff, until each is delivered or expires. Expiry happens after a configurable maximum age (default 6 h) and notifies the admin. Each alert is marked sent exactly once. An alert delivered more than about 2 min after its transition was recorded states the actual event time. Lateness is measured from when the transition was recorded, not from the backdated outage start: an OFF alert always arrives at least 90 s after its outage start, by design.
- **v1 failure:** v1 sent alerts inline with 3 attempts (1 s and 2 s backoff, 5 s timeouts), so a Telegram or network outage of about 20 s lost the OFF alert for good. The "not sent" flag was never read again. Subscribers saw only "POWER ON, was OFF for 59m". If the process was killed between saving the state and sending, the alert was lost. Late alerts carried no time.
- **Acceptance:**
  - Given the Telegram API blocked 10:00-10:10, with the last heartbeat at 10:02:00 and power back at 10:06:00, when the connection returns at 10:10, then within 60 s subscribers get the OFF alert and then the ON alert, each exactly once, stating 10:02 and 10:06.
  - Given the worker killed right after a transition commits, when it restarts, then the alert is delivered once.
  - Given an alert still undeliverable at its expiry age, then it is marked expired and never sent, and the admin gets 1 ops notice.
- **Covers:** ALRT-01, ALRT-02, ALRT-03, ALRT-04, ALRT-05

#### INV-16 Telegram errors are classified; failures and recoveries are visible
- **Rule:** Retry only transient failures: connection errors before the request was sent, 5xx responses, and 429 after its retry_after. 400/401/403 (bad token, chat not found, bot removed) are not retried in a loop. That bot's messages back off to a long interval (e.g. 15 min), and the location is marked failing until any send succeeds; messages still expire per INV-15. A read timeout after the request was sent means "possibly delivered", so do not resend. Any successful send, the admin test message included, marks delivery healthy again.
- **v1 failure:** v1 retried every error 3 times, including "bot was kicked". A timeout that came after Telegram had accepted the message caused re-sends. The harness saw 3 delivered copies. A successful test message did not clear the "alerting failed" badge.
- **Acceptance:**
  - Given sendMessage returns 403 "bot was kicked", then there is 1 attempt, the admin list shows delivery failing, and the admin gets 1 ops notice. After the bot is re-added and a test message succeeds, the badge clears, a recovery notice is sent, and the queued alerts are delivered with their event times, at the latest at their next retry.
  - Given Telegram accepts a message and the response then times out, then the message is not sent again.
  - Given a 429 with retry_after=30, then the next attempt for that bot comes at least 30 s later, and no thread sleeps while waiting.
- **Covers:** ALRT-05, ALRT-06, OPS-03, LOC-03, LOC-07

#### INV-17 Chart messages are recorded before pinning, and posting is idempotent
- **Rule:** The chart record (location, local date, chat id, message id) is stored as soon as the send succeeds, before pinning. Whether the message is pinned is stored in a separate field. Before posting, check whether a record already exists for that location and date. If it does, edit that message instead of posting a new one. The refresh edits today's message whether or not it is pinned. If Telegram reports "message to edit not found", post one replacement.
- **v1 failure:** v1 sent and pinned first, then inserted the record, and swallowed the error when the record already existed. A bot that could post but not pin, or three quick pin errors, left no record. That day's chart was then never refreshed, every day, and the only trace was an ERROR log line. A restart at 00:00:40 or a manual command posted and pinned an extra chart that nothing tracked. A chart deleted by a channel admin made every 15-min edit fail for the rest of the day.
- **Acceptance:**
  - Given a bot that can post but not pin, when the midnight job runs, then the chart is posted, recorded and refreshed every 15 min, and the admin is told that pinning failed.
  - Given today's chart already exists, when the daily job runs again (a restart at 00:00:40 or a manual trigger), then 0 new messages are posted.
  - Given today's chart was deleted in the channel, when the next refresh runs, then exactly 1 replacement is posted and pinned.
- **Covers:** CHRT-05, CHRT-02

#### INV-18 Scheduled jobs catch up after missed runs
- **Rule:** Scheduled work is a condition checked on every cycle: is today's chart missing, is a refresh due, did last night's heartbeat prune run? (Backups run in their own container, INV-25.) The last-run state is stored in the DB. Never rely on the process seeing the exact minute 00:00.
- **v1 failure:** The daily chart ran only if the loop happened to be running during local 00:00. The last run was held only in memory. Starting at 00:05, restarting at 00:01, or a 75 s block across midnight meant no chart all day, and yesterday's chart stayed pinned. At 00:00 the loop also skipped its sleep and spun at 100% CPU for the whole minute (about 200k iterations).
- **Acceptance:**
  - Given the worker down from 23:58 to 00:07, when it starts at 00:07, then within one scheduler cycle yesterday's chart gets its final edit and is unpinned, and today's chart is posted and pinned.
  - Given the worker down for all of 10-02 and started on 10-03 at 09:00, then exactly one chart (10-03) is posted and pinned, and the 10-01 chart is finalized and unpinned.
- **Covers:** CHRT-05

#### INV-19 No orphaned pinned charts
- **Rule:** Each location has at most one pinned chart. Every run of the daily job gives a final edit to, and unpins, every pinned chart not dated today, not only yesterday's. Edits and unpins use the chat id stored with the message. Changing the chat id, deleting the location or resetting its history unpins the old chart in its own chat, then removes the records. For a deleted location, alerts still queued are dropped. Telegram errors during this cleanup are logged and do not block the admin action.
- **v1 failure:** Unpinning looked only at yesterday's chart, so a missed midnight left an old chart pinned forever. Reset deleted the chart records without unpinning. After a chat id change, v1 kept editing the old message id in the new chat and never unpinned the old chat. In the harness, one reset plus one manual send left 3 charts pinned, 2 of them untracked.
- **Acceptance:**
  - Given charts for 10-01 and 10-02 both still pinned, when the daily job runs on 10-03, then both get a final edit and are unpinned, and only the 10-03 chart is pinned.
  - Given the admin changes the chat id, deletes the location or resets its history, then the old chart is unpinned in the old chat, no later edit targets it, and a deleted location's queued alerts are never sent.
- **Covers:** CHRT-05, LOC-04, DATA-03

### C6: Operations and security

#### INV-20 Silent failures become exactly one ops notice and one recovery notice
- **Rule:** Ops notices go to the admin chat configured in env, through the same outbox. The one exception is the database-unreachable notice, which goes straight to Telegram because the outbox lives in the DB (INV-13). Each incident gets 1 notice when it starts and 1 when it recovers. Delivery failures are per location. Downtime and all-silent are global. No code path sends to a hard-coded chat. If the admin chat is not configured, startup logs a warning and the admin UI shows it.
- **v1 failure:** All failures were silent: a failing bot showed only as a UI badge; a dead worker only wrote log lines; a pin failure produced only an ERROR log line. The only other destination in the code was a personal chat id hard-coded in a debug command. That command sent every location's chart there.
- **Acceptance:**
  - Given location A's deliveries fail with 403 for 1 h, with several alerts queued, then the admin gets exactly 1 "failing" notice, and 1 "recovered" notice after the next success.
  - Given the admin chat is unreachable, then ops notices wait in the outbox and never delay subscriber alerts.
- **Covers:** OPS-01, OPS-02, OPS-03, OPS-04, LOC-03

#### INV-21 Fail closed on secrets, and rate-limit admin login
- **Rule:** In production the app refuses to start if the secret key, admin password or DB password is missing, empty, or equal to a committed example value. Debug is off unless the local env enables it. Login attempts are rate-limited per client IP. The env is the source of truth for the single admin account: each start re-syncs the password, and a changed ADMIN_USERNAME renames the account.
- **v1 failure:** With an empty env, v1 ran with DEBUG on, a public hard-coded secret key and an admin/admin superuser. The quickstart built the prod env file from the DEBUG=true dev template and relied on the operator to edit it; any variable left out fell back to an insecure default. Login had no throttling (code read). A later change to ADMIN_PASSWORD was silently ignored, and renaming ADMIN_USERNAME created a second superuser.
- **Acceptance:**
  - Given production mode with the secret key unset or equal to the `.env.example` value, when the app starts, then it exits non-zero and names the variable. The same applies to the admin password and the DB password.
  - Given 5 failed logins within 1 min from one client IP, then further attempts get HTTP 429 for a cool-down period (the planner sets the exact values).
  - Given ADMIN_PASSWORD (or ADMIN_USERNAME) changed and the app restarted, then only the new credentials work, and exactly one admin account exists.
- **Covers:** SEC-01, SEC-03, LOC-01

#### INV-22 Only the TLS proxy is exposed
- **Rule:** Only the proxy publishes ports. The app and DB listen on the internal network only. The proxy buffers requests, so slow clients cannot tie up app workers. Forwarded-proto/host headers are trusted only when they come from the proxy. Cookies are Secure, and HSTS is on.
- **v1 failure:** The prod compose file published the app server on 0.0.0.0:8000, which bypassed the proxy and the host firewall (Docker's port rules skip ufw). Two idle slow clients blocked all heartbeats. X-Forwarded-Proto was trusted from any client. Cookies were not Secure, and there was no HSTS.
- **Acceptance:**
  - Given docker-compose.prod.yml, then only the proxy service publishes ports (automated check).
  - Given 2 clients holding half-sent requests to the public endpoint, then a heartbeat still gets 200 within 1 s.
  - Given an HTTPS admin login, then the session and CSRF cookies are Secure and the response has Strict-Transport-Security.
- **Covers:** SEC-02, HB-03

#### INV-23 Secrets never reach logs; the UI masks them
- **Rule:** Bot tokens and device keys never appear in app logs, HTTP-client-library logs, the proxy access log or error pages. Logs go to stdout with bounded retention. In the admin UI, bot tokens are write-only: shown masked and never sent back to the browser (to change one, type a new one). Device keys are shown masked and in full only on an explicit "reveal" on the location's setup page, which the copy-paste examples need (LOC-05).
- **v1 failure:** The HTTP client library logged every Telegram URL at INFO level, and that URL contains the bot token (`…/bot123456789:SECRET…/sendMessage`). These lines went to a log file. Keys sent in query strings end up in proxy access logs. The edit form put the token into the page HTML.
- **Acceptance:**
  - Given the most verbose log level in tests, when an alert is sent, a chart is edited and a heartbeat with its key in the query string is accepted, then no captured log line contains the token or the key (the test asserts on the literal values).
  - Given the location pages, then the bot token never appears in the HTML except in masked form, and the device key appears in full only after "reveal" on the setup page.
  - Given the compose files, then every service logs to stdout with a size limit.
- **Covers:** OPS-08, SEC-04

#### INV-24 Device keys: strict auth, rotation, and an endpoint that never redirects
- **Rule:** The key is accepted in a header, or in a query parameter for devices that can only set a URL. The endpoint answers at the exact URL shown in the admin panel, without redirects. It is HTTPS-only by default; plain HTTP is an open question, only for devices that cannot do TLS. A missing or unknown key is rejected before any write. Regenerating a key keeps the history and invalidates the old key immediately.
- **v1 failure:** Keys could not be rotated (code read). The only way out was deleting the location, which also deleted its history. The admin panel showed an http:// URL containing the key. A request without the trailing slash got a 301 redirect that echoed the key in the Location header, and many device HTTP clients do not follow redirects. The docs showed a POST sample that the endpoint rejected with 405.
- **Acceptance:**
  - Given a key regeneration, then the old key gets 401 and changes nothing, the new key gets 200, and the history is intact.
  - Given a request with no key or an unknown key, then it gets 401 and no row or status changes.
  - Given the curl and cron examples from the setup page, when they are run verbatim in a test, then they get 200 without following redirects.
- **Covers:** HB-01, HB-02, LOC-05, LOC-06

#### INV-25 Nightly backups, with a restore that has been tested
- **Rule:** A logical dump is taken every night and kept for 14 days (configurable). Backups run in their own backup container, not in the worker; if the newest dump is older than 24 h when that container starts, it dumps immediately, so a missed night is caught up. Dumps are stored outside the DB data directory with restricted permissions, because they contain bot tokens. The documented restore procedure is actually exercised once (a manual check, section 1).
- **v1 failure:** v1 had no backups at all, for data the spec said to keep forever. The local and prod compose files bound the same `docker_data/postgres` directory. (code read)
- **Acceptance:**
  - Given 16 nightly runs, then 14 dumps exist. Given the backup container starts while the newest dump is older than 24 h, then it dumps immediately.
  - Given a dump, when the documented restore runs into an empty DB, then the locations, timeline and chart records match the source.
- **Covers:** OPS-06

#### INV-26 One-command, reproducible deploys
- **Rule:** One command runs the stack locally, and one command deploys or updates production. Migrations run exactly once per deploy, in a step that runs before the new app and worker start. Dependency versions come from a lockfile, and the base image tag is pinned. Device examples are generated from the same code as the endpoint and exercised by a test (INV-24); the README is checked by following it once (brief section 10, item 7).
- **v1 failure:** Dependencies used open-ended ranges (`python-telegram-bot>=21`) with no lockfile. The next major version returns `retry_after` as a timedelta, which would make v1's 429 handler raise a TypeError. The quickstart named make targets that did not exist, gave an ESP32 sample that got 405, and listed env vars that nothing read.
- **Acceptance:**
  - Given a fresh VPS, when the operator follows the README, then a working deployment is reached within 30 min: admin login, one heartbeat and one test message all work.
  - Given a deploy with a new migration, then the migration runs exactly once, and if it fails the deploy stops before the new app or worker starts.
- **Covers:** OPS-07

## 4. Smaller pitfalls checklist

**Timing**
- Measure intervals with a monotonic clock; use the wall clock only for timestamps. In v1, a 1 h NTP step backwards stopped heartbeat checks for that hour.
- Every loop path reaches its sleep or wait. In v1, a `continue` skipped the sleep and the loop spun at 100% CPU through 00:00 every night.

**Telegram**
- Never sleep after the last retry attempt, and cap the total retry time per message.
- Never retry a non-idempotent send after a read timeout, and never retry 400/401/403.
- Parse `retry_after` whether it arrives as seconds or as a duration.
- Reuse one HTTP client per bot. v1 created a new event loop, client and TLS handshake for every call, about 96 chart edits per location per day.
- Treat "message is not modified" as success. On "message to edit not found", re-post once (INV-17) rather than retrying every 15 min.
- The admin test-message button makes one attempt with a short timeout. v1 held one of its two web workers through the whole retry sequence.

**Dependencies and code hygiene**
- Pin dependencies with a lockfile and upper bounds on major versions, and pin the base image. Review major upgrades on purpose.
- Nothing hard-codes chat ids, tokens or debug destinations. Every destination comes from the DB or the env.
- Dev, seed and mock commands refuse to run in production or against existing locations. v1's mock-data command wiped a real location's 2880 heartbeats and kept its real token.

**Logging**
- Log to stdout only, with Docker log size limits. Several processes sharing one rotating log file lost 30-53% of lines in v1.
- Keep the loggers of third-party HTTP clients at WARNING, and add a redaction filter for `bot<token>` and key-like query values.
- Log heartbeats at debug level. v1 wrote one INFO line per heartbeat, about 1.4k lines per day per location.

**Heartbeat endpoint**
- A generous per-IP limit at the proxy is enough. v1 had no limit, and every request wrote to the DB and the log.
- A heartbeat by GET changes state. Link previewers that fetch a pasted device URL will record a heartbeat, and can "restore" a location that is off. Warn against sharing the URL on the setup page.

**Time zones and the image**
- The container image must include the tz database. Test that Europe/Kyiv resolves inside the container; v1's slim image was never checked.
- DST test dates come from the tz database. If Ukraine drops DST in future tzdata, keep the cases by asserting against the real transitions or by using a zone that still has them.

**Tests**
- Tests block all real network traffic globally. The v1 investigation harness sent real requests to api.telegram.org before a guard was added.
- Race tests use the production DB engine (see section 1).
- Snapshot-test every alert string, caption and label in uk, en and ru. v1 shipped "Света не было : 3ч", with a stray space.
- Alert durations get a day unit once they reach 24 h (row totals never do). v1 printed "77год".

**Admin UI and web**
- Validate `next` redirect targets as same-host. In v1 the open redirect was latent, and the redirect was also broken.
- Admin pages that show secrets load no third-party runtime scripts; self-host the CSS and JS. v1 loaded the Tailwind Play CDN without SRI.
- Static assets must work with debug off. v1 never collected its static files and only got away with it because no page used any.
- Validate the bot token format and the chat id when saving. v1 validated neither.
- Show short error causes in the UI and keep details in the logs. v1 flashed raw exception text such as `httpx.ConnectError`.

**Deployment and docs**
- The env is the source of truth for the admin account: each start re-syncs the password, and a changed ADMIN_USERNAME renames the account (test both; INV-21). In v1 the change was silently ignored, and renaming created a second superuser.
- Containers run as a fixed non-root user. v1's compose file ran as the host UID, which is root when make runs as root.
- Local and prod use separate data directories and compose project names.
- Graceful shutdown finishes within the container stop timeout. v1's retry sleeps overran Docker's 10 s grace, so it was SIGKILLed mid-work.
- Docs must not drift from the code. v1's spec, contracts, quickstart and task checklist were never updated after the first commit: the checklist ticked items that were not built, and the spec said "no tests".

## 5. Traceability

| Invariant | Requirements |
|---|---|
| K-1…K-6 baseline | MON-01, MON-02, MON-03, ALRT-01, ALRT-02, LOC-02, LOC-09, CHRT-07, CHRT-08 |
| INV-01 atomic, I/O-free transitions | MON-03, MON-04, HB-03, ALRT-05 |
| INV-02 exactly-once under concurrency | MON-04, ALRT-05 |
| INV-03 single timeline | CHRT-01, CHRT-03, CHRT-04, MON-02, MON-03 |
| INV-04 not monitored ≠ off | LOC-08, MON-05, CHRT-01, CHRT-03 |
| INV-05 one effect per toggle | LOC-08, LOC-09, LOC-10 |
| INV-06 thresholds don't rewrite history | DATA-04 |
| INV-07 admin corrections edit the timeline only | DATA-02, DATA-03 |
| INV-08 wall-clock day geometry | CHRT-06, CHRT-02, CHRT-03 |
| INV-09 heartbeats are disposable | DATA-01 |
| INV-10 lapses are not monitored, fresh window | MON-05, MON-06, OPS-02 |
| INV-11 outage continuing after restart | MON-05, ALRT-01, ALRT-02, CHRT-03 |
| INV-12 all-silent guard | OPS-04, MON-05 |
| INV-13 worker heals itself | MON-06, OPS-05, OPS-02, MON-02 |
| INV-14 never blocked by Telegram or rendering | ALRT-06, HB-03, MON-02 |
| INV-15 durable ordered outbox | ALRT-01, ALRT-02, ALRT-03, ALRT-04, ALRT-05 |
| INV-16 error classification and health | ALRT-05, ALRT-06, OPS-03, LOC-03, LOC-07 |
| INV-17 record before pin, idempotent post | CHRT-05, CHRT-02 |
| INV-18 catch-up scheduling | CHRT-05 |
| INV-19 no orphaned pins | CHRT-05, LOC-04, DATA-03 |
| INV-20 ops notices | OPS-01, OPS-02, OPS-03, OPS-04, LOC-03 |
| INV-21 fail closed, login rate limit | SEC-01, SEC-03, LOC-01 |
| INV-22 only the proxy is exposed | SEC-02, HB-03 |
| INV-23 secrets out of logs, write-only/masked in UI | OPS-08, SEC-04 |
| INV-24 device key handling | HB-01, HB-02, LOC-05, LOC-06 |
| INV-25 backups and restore | OPS-06 |
| INV-26 reproducible deploy | OPS-07 |

**Verified v1 findings deliberately not carried over** (specific to v1):
- The heartbeat API contradicted v1's own contract doc (POST vs GET, 5 s dedup, config route). The new API is designed fresh; only the lesson about docs drifting carries over.
- The 5 s duplicate-heartbeat filter (spec'd, then removed): repeated heartbeats are harmless no-ops under INV-02.
- Chart layout did not match the v1 spec (labels on the right, major ticks at 0/6/12/18). Superseded by `docs/chart-spec.md`.
- tasks.md checkboxes and the unfilled constitution template were artifacts of v1's spec process.
- Dead code and duplicated constants: unused check/format/caption functions, an unused CSS file, stub modules, and the unused django-tailwind dependency. No v1 code is reused.
- Unreachable branches and a repeated query in the chart's heartbeat classifier (21 queries per render). The classifier is replaced by the stored timeline (INV-03).
- Redundant secondary indexes on v1's heartbeat table.
- v1 template bugs: the login flash message rendered twice, and the delete/reset confirmation pages were missing context.
- `ALLOWED_HOSTS` entries were not whitespace-stripped (parsing in v1's framework config).
- The mock-data command stopped matching the chart after it became heartbeat-only (the safety lesson is in section 4).
- The startup table guard failed on SQLite only.
- On the fall-back day, v1 skipped the repeated 03:xx refreshes because of its (date, hour, minute) dedup key. This was harmless, and the new scheduling is condition-based (INV-18).
- Alerts lacked the location name (v1 spec FR-018). Deferred to v2 (ALRT-07) by the user's decision; only late alerts carry the event time (ALRT-04).
- Bot tokens stored in plaintext, although v1's data model promised encryption. Encryption at rest is out of scope (brief section 6). The mitigations are write-only tokens in the UI (INV-23), an internal-only DB (INV-22) and protected backups (INV-25).
- v1's manual chart commands (refreshdiagrams, syncdiagrams, resetdiagrams, senddiagram) are not rebuilt: the condition-based scheduler (INV-17, INV-18, INV-19) posts, refreshes, replaces and unpins by itself. v1's `startup` recovery command is replaced by worker-owned recovery (INV-10). `createadmin` is replaced by the env-synced admin account (INV-21), and `runworker` by the new worker. A dev-only seed command (v1's `generatemockdata`) is optional and follows the section 4 safety rule.
- A heartbeat purge that ran at startup before Feb 10 in v1 history. Historical only.
