# Power Monitor

## 1. What it is

Power Monitor tracks whether mains power is on at a few locations. A small mains-powered
device at each location (a router cron job, an ESP32, a Raspberry Pi; no UPS) sends a
heartbeat every minute or so. When the heartbeats stop for longer than the location's
period plus grace, the location's Telegram channel gets an OFF alert; the first heartbeat
after the outage brings an ON alert. Each channel also gets a pinned weekly chart of when
power was on and off, with daily totals, that updates itself (section 10, Weekly chart).
One admin manages the locations in a small web panel: add, edit and delete them, pause
them for maintenance, switch their alerts off, rotate a device key, send a test message,
see which locations cannot deliver alerts (section 10, Location page), and remove a false
outage or reset a location's history (section 10, History corrections).

The stack is Django (web panel and heartbeat endpoint), one worker process (outage
detection and Telegram delivery), PostgreSQL, Caddy (TLS) and a nightly backup job
(section 14), run with Docker Compose.

**Test channel rule (current release).** Every location's chat ID must point at a
**private** Telegram channel whose only member is the maintainer. The rule is lifted only
after the failure drills in section 11 have been run on the production server and their
results recorded in the phase verification. Then real subscriber channels can be
connected.

## 2. Prerequisites

- A VPS with 1 vCPU and 1-2 GB RAM, running Ubuntu or Debian, with SSH access.
- A domain name whose A record (and AAAA record, if the VPS has IPv6) points at the VPS
  **before the first start**: Caddy requests the TLS certificate on first start.
- Ports 80 and 443 open to the internet (check the provider's firewall too).
- For each location: a Telegram bot token from @BotFather and a private channel (see the
  rule above) with the bot added as an administrator with the "Post messages" and
  "Edit messages of others" rights. The second one lets the bot pin, unpin and edit the
  weekly chart in a channel. In a group (not recommended) the bot needs "Pin messages"
  instead. Without the pin right the chart is still posted and refreshed, and the admin
  gets a 📌 notice (section 10).
- Optional, recommended: a private Telegram chat for the admin's ops notices (section 4).

## 3. Server prep

Run these as a user with sudo.

**Docker** from Docker's official apt repository (Compose builds through buildx, so the
`docker-buildx-plugin` package is required). On Ubuntu:

```sh
sudo apt update
sudo apt install ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
sudo tee /etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}")
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF
sudo apt update
sudo apt install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
```

On Debian, use `https://download.docker.com/linux/debian` in both URLs. Run the `docker`
commands below with `sudo`, or add your user to the `docker` group and log in again.

**Swap.** A 1 GB swapfile is the safety net for the 1 GB memory budget:

```sh
sudo fallocate -l 1G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

**Time sync.** Outage times come from the server clock:

```sh
sudo timedatectl set-ntp true
timedatectl
```

`timedatectl` must show `System clock synchronized: yes`. Keep NTP on: the worker
compares the wall clock with a monotonic clock on every cycle, and records a wall-clock
step of more than 5 s forward as a monitoring gap (with a gap notice, section 10). A step
of more than 5 s back makes it skip that cycle's decisions, with one warning in its log.

**Firewall.** Ports published by Docker bypass ufw rules. This stack publishes only 80 and
443 (Caddy); the web app and the database are on Docker's internal network and are never
published. If you enable ufw, allow SSH (`sudo ufw allow 22/tcp`) first.

## 4. Configure

```sh
git clone <repository URL> power-monitor
cd power-monitor
cp .env.example .env.docker_production
chmod 600 .env.docker_production
```

Edit `.env.docker_production`:

- `SECRET_KEY`: at least 50 random characters. Generate one with
  `python3 -c 'import secrets; print(secrets.token_urlsafe(50))'`.
- `ADMIN_USERNAME`, `ADMIN_PASSWORD`: the single admin account of the web panel.
- `POSTGRES_PASSWORD`: the database password.
- `DOMAIN`: the bare host name, e.g. `power.example.org` (no `https://`, no path).
- `ACME_EMAIL`: your email for the Let's Encrypt account.

Generate the passwords with the same command as the secret key: its output uses only
letters, digits, `-` and `_`, so the env file needs no quoting. Keep `APP_ENV=production`,
`DEBUG=0` and `DISPLAY_TZ=Europe/Kyiv` (the canonical name; the old alias `Europe/Kiev` is
rejected). `PUBLIC_BASE_URL` is ignored in production, which uses `https://DOMAIN`.

The app refuses to start, and names the variable, when a secret is missing or still has
its example value from `.env.example`.

Optional settings:

- `OPS_BOT_TOKEN`, `OPS_CHAT_ID`: the admin's ops chat, where the worker reports on its own
  state (the notices are listed in section 10). Create a bot with @BotFather (or reuse a
  location's bot), create a private group or channel whose only member is you, and add
  the bot (to a channel as an administrator with the "Post messages" right). Put the bot
  token in `OPS_BOT_TOKEN` and the chat's numeric ID (`-100...`, found the same way as a
  location's) in `OPS_CHAT_ID`. Set both or neither: one without the other, or a value
  of the wrong shape, stops the app at start with the variable's name. Without them the
  location list shows a warning, and every ops notice goes to the worker log at WARNING
  instead (`docker compose -f docker-compose.prod.yml logs worker`).
- `ALERT_MAX_AGE_HOURS`: 1 to 48, default 6. An alert that could not be delivered within
  this many hours after its transition was recorded is dropped and never sent, with one
  ops notice. The location's next alert is still delivered.
- `LOG_LEVEL`: `DEBUG`, `INFO` (the default), `WARNING` or `ERROR`. Bot tokens and device
  keys are redacted at every level. Log timestamps are UTC (for example
  `2026-10-01T11:31:06.452+00:00`), whatever `DISPLAY_TZ` is.
- `BACKUP_TIME_UTC`: the time of the nightly database dump, in UTC, as `HH:MM` (default
  `03:00`, which is 05:00 or 06:00 in Kyiv). The default is clear of local midnight, when
  the charts are posted, and of the DST change (section 14).
- `BACKUP_KEEP`: how many nightly dumps are kept, 1 to 365 (default 14).

A `BACKUP_TIME_UTC` or `BACKUP_KEEP` of the wrong shape stops the `backup` service, and its
log names the variable (`docker compose -f docker-compose.prod.yml logs backup`).

## 5. Deploy

One command deploys a fresh install and every update:

```sh
git pull
docker compose -f docker-compose.prod.yml up -d --build --wait
```

It builds the image, starts PostgreSQL, runs the one-shot `migrate` service (apply
migrations, then sync the admin account from the env file), and starts `web` and
`worker` only after `migrate` succeeded. `caddy` does not wait for `migrate`: it starts
on its own and keeps running if a migration fails (section 8). `backup` starts once the
database is up and dumps it every night (section 14). Check the result:

```sh
docker compose -f docker-compose.prod.yml ps
```

`db`, `web`, `worker`, `caddy` and `backup` are `healthy`. `backup` turns healthy once
its first dump is written, which happens at once on a fresh install. `ps -a` also shows
`migrate` as `Exited (0)`. `docker compose -f docker-compose.prod.yml logs worker` shows
`worker active`. `curl -fsS https://DOMAIN/healthz` prints `ok`.

**Every deploy or restart is a short monitoring gap, by design.** While the worker is
down, nothing is monitored. The new worker records that span as "not monitored" (never as
an outage, and never as power on), a short not-monitored sliver in the stored history,
and the ops chat gets one notice for it:

```text
⏸ Monitoring gap 01.10 10:00:12 – 10:10:40 (10m 28s). Recorded as not monitored; no subscriber alerts were sent for it.
```

Subscribers get no alert for the gap itself. A device that lost power during the gap gets
its OFF within one detection window (period plus grace) after the restart.

- The first deploy of this release sends no gap notice: no earlier cycle is recorded yet,
  and the worker logs `detection starts at …: no earlier cycle is recorded`. Every later
  deploy sends one.
- If the old worker was stopped in the middle of a send (on a normal stop it finishes the
  send first), that alert may already have reached Telegram, so it is not resent. The
  ops chat gets one notice instead:
  `❓ … may not have been delivered (the worker stopped while sending it). …`

## 6. First location

1. Open `https://DOMAIN/` and sign in with `ADMIN_USERNAME` and `ADMIN_PASSWORD`.
2. Click **Add location**. Enter a name, the heartbeat period and grace (seconds; defaults
   60 and 30, at least 10), the bot token from @BotFather, the numeric chat ID of the
   private test channel (`-100...`; the form's help text says how to find it) and the
   alert language (uk, en or ru). The bot must be an administrator of the channel with the
   "Post messages" and "Edit messages of others" rights (the second one to pin, unpin and
   edit the weekly chart; in a group it needs "Pin messages").
3. On the location's setup page, click **Reveal key** and copy the curl or cron example
   onto the device. The device must run on mains power only (no UPS): heartbeats measure
   power and internet at the device.
4. Open the location's page (click its name in the location list) and click **Send test
   message**. The bot posts one silent message in the channel
   (`🔧 Power Monitor test message: the bot can post here.`, in the location's language),
   and the page says whether it was sent. If not, the message on the page names the cause
   (the bot is not in the chat, a wrong chat ID, a bad token) and what to fix (section 10,
   "Location page"). This checks the bot token and the chat ID before the first alert.
5. The location list shows **Waiting for first heartbeat**, then **On** after the first
   heartbeat. The first heartbeat sends no alert, but within a few seconds the channel
   gets its first weekly chart, posted and pinned silently (section 10). The chart starts
   at the first heartbeat: the time before it is empty (no data).

Never paste a URL that contains the key into Telegram or any other chat: link previewers
open it, and every opening counts as a heartbeat.

## 7. Update and env changes

To update, run the deploy commands from section 5 again: `git pull`, then
`docker compose -f docker-compose.prod.yml up -d --build --wait`. Migrations run exactly
once per deploy.

After editing `.env.docker_production`, run the same deploy command. `docker compose
restart` neither re-reads the env file nor runs `migrate`, so it does not apply the
change. After an `ADMIN_USERNAME` or `ADMIN_PASSWORD` change and a deploy, only the new
credentials work and there is still exactly one admin account.

A deploy restarts the web app and the worker. That downtime is recorded as not monitored,
with one gap notice (section 5). Alerts stay on private channels until the section 11
drills are recorded (section 1).

## 8. If a migration fails

The deploy command exits non-zero. PostgreSQL rolls the failed migration back, `migrate`
shows `Exited (1)` in `docker compose -f docker-compose.prod.yml ps -a`, and `web` and
`worker` are not running: the deploy already removed the old containers, so the service
stays down until a deploy succeeds. To recover:

```sh
docker compose -f docker-compose.prod.yml logs migrate
git log --oneline
git checkout <previous commit>
docker compose -f docker-compose.prod.yml up -d --build --wait
```

The previous version comes back. Fix the migration, then `git checkout <branch>`,
`git pull` and run the deploy command again.

## 9. Local run

```sh
cp .env.example .env.docker_local
```

In `.env.docker_local`, set `APP_ENV=local` and `DEBUG=1`. Then:

```sh
docker compose -f docker-compose.local.yml up -d --build --wait
```

This runs `db`, `migrate`, `web`, `worker` and `backup` (no Caddy). The app is on
`http://localhost:8000`; sign in with the admin account from `.env.docker_local`.
Local alerts are real Telegram messages too, so use a private test channel here as well.
Local dumps go to `docker_data/local/backups/`, so a restore can be rehearsed locally
(section 15 (a)). Stop the stack with `docker compose -f docker-compose.local.yml down`.

Tests run inside the stack against its PostgreSQL:

```sh
docker compose -f docker-compose.local.yml run --build --rm web pytest
```

The full check (lint, format, types, tests, coverage gate):

```sh
docker compose -f docker-compose.local.yml run --build --rm web sh -c "ruff check . && ruff format --check . && mypy powermon && pytest -q --cov=powermon --cov-report=term-missing:skip-covered && coverage report --include='powermon/engine/*,powermon/alerts/*,powermon/i18n/*,powermon/telegram/*,powermon/chart/*,powermon/worker/detection.py,powermon/worker/io_loop.py,powermon/worker/lease.py,powermon/worker/supervision.py' --fail-under=80"
```

Dependencies are pinned in `uv.lock`, which only the pinned uv in the Dockerfile's
`uvtool` stage may write (a host uv of another version is refused). Check the lock:

```sh
docker build --target uvtool -t powermon-uvtool .
docker run --rm --user "$(id -u):$(id -g)" -e UV_CACHE_DIR=/tmp/uv-cache -v "$PWD":/app -w /app powermon-uvtool uv lock --check
```

After changing dependencies in `pyproject.toml`, run `uv lock` the same way instead of
`uv lock --check`, and review every new package name before building.

## 10. Operations notes

- **Logs:** `docker compose -f docker-compose.prod.yml logs <service>` (`db`, `migrate`,
  `web`, `worker`, `caddy`, `backup`). Each service keeps at most 3 files of 10 MB (Docker's
  size cap). App log lines start with a UTC ISO 8601 timestamp, and `LOG_LEVEL` sets
  their detail (section 4). Bot tokens and device keys are redacted from the app logs at
  every level, and Caddy writes no access log.
- **Memory baseline:** after the stack has been idle for 10 minutes, record
  `docker stats --no-stream` and `free -m`.
- **Certificate:** `echo | openssl s_client -connect DOMAIN:443 -servername DOMAIN 2>/dev/null | openssl x509 -noout -dates`
  shows the expiry. Caddy renews automatically; more than a third of the lifetime should
  be left.
- **Database password:** the postgres image reads `POSTGRES_PASSWORD` only when it creates
  the database, on the first start. To change it later, first set it inside the database
  (`docker compose -f docker-compose.prod.yml exec db psql -U <POSTGRES_USER> -d <POSTGRES_DB>`,
  then `\password <POSTGRES_USER>`), then put the same value in `.env.docker_production`
  and run the deploy command.
- **Repeated fresh deploys:** Let's Encrypt allows 5 certificates for the same name per 7
  days. While testing fresh installs repeatedly, add
  `acme_ca https://acme-staging-v02.api.letsencrypt.org/directory` to the global options
  block of `docker/Caddyfile` on the server (devices will not trust the staging
  certificate). Never commit that line: it also disables Caddy's fallback issuer. Remove
  it, delete `docker_data/prod/caddy/data/caddy/certificates/acme-staging-v02.api.letsencrypt.org-directory`
  and deploy again when done.
- **One active worker:** only one worker works at a time; it holds a database lock and
  logs `worker lock held (generation N)` and `worker active since … (generation N)`. A
  second worker started by mistake logs `standby: waiting for the worker lock` and does
  nothing: it writes and sends nothing. It checks the lock every 5 s and takes over by
  itself when the active worker stops.
- **Database outages:** the worker never exits because the database is gone. It logs one
  WARNING when the database goes away, `worker lock: database unreachable (…); retrying`
  (after `worker lock: lease session lost (…)` if it held the lock), and one when it is
  back, `worker lock: database reachable again`. A loop that hits the outage in the middle
  of its work also logs `detection: database unreachable (…); retrying` (or
  `telegram-io: …`) and later `… database reachable again after N s`. The worker then
  takes the lock back by itself (a new generation) and records the outage as a
  monitoring gap, with one gap notice. If the database stays unreachable for more than
  5 minutes, the worker that held the lock sends one `🛑 Database unreachable since …`
  notice straight to the ops chat, because the outbox lives in the database. This also
  holds when that worker was restarted during the outage (for example by the watchdog on
  a frozen database): its container keeps `/tmp/powermon-worker.held`, the time it last
  held the lock. A worker container that is recreated (a deploy) starts without it.
- **Health:** `docker compose -f docker-compose.prod.yml ps` shows `healthy` or
  `unhealthy` for `db`, `web`, `worker`, `caddy` and `backup` (`backup` is healthy while
  its newest dump is under 26 hours old, section 14). The worker touches its health file
  (`/tmp/powermon-worker.health` in the container) after every successful cycle and on
  every standby cycle, and the healthcheck wants it under 30 s old. It does not touch it
  while the database is down, so it shows `unhealthy` about 40 to 60 s into a database
  outage, without being restarted, and turns `healthy` again by itself once the database
  is back.
- **Watchdog:** if a worker loop stops making progress (detection for 60 s, Telegram
  delivery for 180 s), for example on a frozen database (`docker pause`, a VM freeze),
  the worker logs a CRITICAL line,
  `worker loop <name> made no progress for over N s or ended; exiting for a restart`,
  and a thread dump, then exits with code 70. Docker's restart policy starts it again
  (`docker inspect -f '{{.RestartCount}}' powermon-prod-worker-1` goes up by one).
- **Ops notices:** with the ops chat configured (section 4), the admin gets these, in
  English, with times in `DISPLAY_TZ`. Each delivery pass sends them after the
  subscriber alerts, so a broken ops chat never delays subscribers.
  - Monitoring gap (every deploy or restart, a database outage, a stall or a clock step):
    `⏸ Monitoring gap 01.10 10:00:12 – 10:10:40 (10m 28s). Recorded as not monitored; no subscriber alerts were sent for it.`
  - Database unreachable for more than 5 minutes, sent directly, then the gap notice
    once it is back:
    `🛑 Database unreachable since 10:02:05 (over 5 min). Detection is paused; the gap will be recorded as not monitored when it is back.`
  - All active locations silent (at least 2), and its recovery. Subscriber alerts
    continue as normal, because a blackout can really hit several locations at once:
    `⚠️ All 3 active locations silent since 14:10: an area power/ISP outage or a server/network problem. Subscriber alerts continue as normal.`
    and `✅ Heartbeats are back (first: Office, 14:13:05); all-silent lasted 3m.`
  - Alert expired (older than `ALERT_MAX_AGE_HOURS`, never sent):
    `⌛ OFF alert for Office (event 17:27) expired undelivered after 6h and will not be sent.`
  - Alert may not have been delivered (a timeout after the request went out, or a worker
    stopped mid-send; it is never resent):
    `❓ ON alert for Office (event 17:45) may not have been delivered (Telegram timed out after the request was sent). It will not be resent; please check the channel.`
  - The bot may post the weekly chart but not pin it (a missing pin right, section 2),
    once when pinning starts failing and once when it works again:
    `📌 Can't pin today's chart for Office (Telegram: http_400). The chart is still posted and refreshed; pinning is retried every 15 min. Check that the bot may pin messages in the chat.`
    and `📌 Pinning works again for Office.`
  - Telegram refuses a location's alerts for good (bot removed from the channel, wrong
    chat ID, bad token), once when it starts and once when an alert or a test message
    goes through again, however many alerts are queued ("Location page" below):
    `🚫 Alerts for Office are failing (Telegram: http_403). They stay queued and are retried every 15 min until they expire after 6h. Check that the bot is an admin of the channel, then send a test message from the admin panel.`
    and `✅ Alerts for Office are delivered again.` When Telegram reports that the group
    became a supergroup, the first one ends with
    `The group became a supergroup; its new chat ID is -100…. Update the location's chat ID.`
  - All-silent with a location in maintenance: see "All-silent and maintenance" below.
- **Late alerts:** an alert sent more than 2 minutes after its transition was recorded
  starts with the local time of its event (the outage start for OFF, the restore time for
  ON): `🔴 17:27 POWER OFF`, or `🔴 30.09 23:58 POWER OFF` when the event was on another
  day. While Telegram is unreachable, alerts wait in the outbox and are retried per bot;
  heartbeats and detection never wait for Telegram.
- **Weekly chart:** each location's channel has one pinned chart of the current week,
  in the location's language: one row per day, Monday to Sunday, in `DISPLAY_TZ`, each a
  bar showing when power was on, off or not monitored (hatched: server downtime), with
  the day's off time and outage count. Days still to come show the same weekday of the
  previous week, dimmed. The worker keeps it up to date by itself:
  - The first chart is posted at the location's first heartbeat; the chart starts there,
    and the time before it is empty (no data). A location still waiting for its first
    heartbeat gets no chart.
  - At local midnight (`DISPLAY_TZ`) a new chart for the new day is posted silently and
    pinned silently, and the previous day's chart is unpinned. Two charts are pinned for
    a few seconds at most. About two minutes later, once detection has run past
    midnight by the location's heartbeat timeout plus a short margin, the previous day's
    chart gets its final render (no now marker, the caption names its date, for example
    `No outages on Thu 01.10`). An outage that began just before midnight is then
    counted, unless detection's checks failed in every cycle of that short margin (for
    example on database errors). Each such failure is logged, and the finished chart is
    not redrawn.
  - Today's chart is edited in place every 15 minutes, so an outage shows on it within 15
    minutes of its OFF alert.
  - After the worker was down across one or more midnights, it posts exactly one chart
    for today and gives every older pinned chart its final render and unpins it. A day
    the worker missed entirely gets no chart. After any restart, the chart is first
    redrawn only once the worker has recorded the downtime as not monitored, so a
    downtime is never drawn as on.
  - The worker unpins only the charts it posted itself, by their message, in the chat
    they were posted to; pins the channel admin made stay. If today's chart is deleted in
    the channel, one replacement is posted and pinned.
  - A channel pin may leave a "pinned a photo" service message in the channel each day
    (Telegram's behaviour; it is checked once in section 12).
  - Cost: a render takes about 0.1 to 0.3 s of CPU and about 35 MB of extra worker memory
    while it runs. Chart work runs in the worker's Telegram thread only, at most one chart
    call per delivery pass and after all due alerts, so it never delays detection or
    heartbeats and delays an alert by one call at most. Each chart call logs one INFO
    line, `chart <post|pin|finalize|unpin|refresh|release> for location <id>: <result>
    (<code>) render_ms=<n> call_ms=<n>` (`release` unpins a chart after a chat or token
    change or a delete; "Location page" below).
- **Location page:** click a location's name in the location list. Every action on it
  is a button; nothing changes on a page load, and a reload never repeats an action.
  - **Status:** On, Off, Maintenance or Waiting for first heartbeat. Under maintenance
    the page also shows the power state underneath. Then on since or outage since, the
    last heartbeat and **Delivery**: `OK`, or
    `Failing since 2026-10-01 14:05:00 EEST (http_403)` with the cause and what to do.
    The list's Delivery column shows the same badge (`Failing since 14:05 (http_403)`,
    with the date when it started before today).
  - **Switches:** each changes exactly one thing, applies at the click and can be
    switched back; a second click or an old page never flips it back.
    - **Maintenance:** OFF is not detected, so no OFF alert is sent, and the chart shows
      the time as not monitored (hatched). Heartbeats are still recorded: an outage that
      was already in progress stays one outage and gets its ON alert as usual when power
      returns ("was OFF for" includes the maintenance time). Turning maintenance off
      starts a fresh detection window: silence during maintenance does not count, and
      OFF can be reported at the earliest period + grace after the switch.
    - **Alerts:** while off, subscribers get no new alerts, and none are saved for
      later. Alerts already queued still go out. The chart, its 15-minute refresh and the
      midnight re-pin carry on.
    - **Router grace:** while on, OFF waits 180 s longer when the last heartbeat came
      within 5 minutes after power returned, so a router that restarts after a blackout
      is not reported as a second outage. It changes only decisions made after the
      switch; outages already recorded and their totals stay as they are.
  - **Send test message:** one silent message to the channel with the location's bot
    and chat, to check the token and the chat ID. It is not an alert: it is sent even
    while alerts are off or maintenance is on, never queued and never retried. Telegram
    can take up to 15 s to answer. The page then says the result: sent; the bot is not
    in the chat or the chat was not found (`http_400`, `http_403`); Telegram rejected
    the token (`http_401`, `http_404`); no answer in time (it may have been sent: check
    the channel before you try again); Telegram unreachable; or Telegram asks to wait N
    seconds. Details go to the web log as a short code, never the token.
  - **Delivery failing:** when Telegram refuses a subscriber alert for good (400, 401,
    403, 404), the location shows `Failing since …` and the ops chat gets one notice. The
    alerts stay queued and are retried every 15 minutes until they expire
    (`ALERT_MAX_AGE_HOURS`). Fix the cause (make the bot an admin of the channel again,
    or correct the token or the chat ID in Edit location), then click **Send test
    message**. When it goes through, the badge clears, the ops chat gets one
    `✅ Alerts for … are delivered again.`, and the queued alerts go out in the worker's
    next pass, each with its event time (the late prefix above). A subscriber alert that
    goes through clears it too. A failed test message never marks a location failing:
    its cause is shown on the page only. If Telegram says the group became a supergroup,
    the page and the notice show the new chat ID (`-100…`): put it in Edit location, then
    send a test message. The chat ID is never changed automatically.
  - **Edit location:** name, heartbeat period, grace, chat ID, language and, optionally,
    a new bot token. The token field is always empty and the token is never shown again
    (only `123456789:••••••••`); leave it empty to keep the current one. New period and
    grace values apply from the next check and never change past days or their totals.
    A lower value can report OFF at the next check if the device has already been silent
    that long. A new chat ID or bot token moves the weekly chart: the worker posts and
    pins a new one in the new chat, and the queued alerts go there. The old chart is
    unpinned in the old chat only if this location's bot is an admin there with the
    "Edit messages of others" right; otherwise the old pin stays (one WARNING in the
    worker log), so unpin it by hand in Telegram. The switches are not part of the form,
    so saving a form opened earlier never switches them back or changes the status.
  - **Delete location:** a confirmation page, then the delete. There is no undo. The
    location's alerts stop at once and the queued ones are dropped (never sent), its
    device key gets HTTP 401, its open problems (such as failing delivery) close without
    a recovery notice, and it disappears from the admin panel. The worker unpins its
    weekly chart where the bot still can; otherwise unpin it by hand (the posted messages
    stay in the channel). Its history stays in the database but is never shown. To
    monitor the place again, add a new location (a new key and an empty history). To
    pause a location instead, turn maintenance on or alerts off.
- **History corrections:** two sections of the location page fix a wrong power history.
  Each is a confirmation page, then the change. Neither sends a message to the channel,
  and there is no undo.
  - **Recent outages:** every outage of the last 14 local days, newest first, with its
    start, end and off time (the chart's daily-total format). The outage in progress reads
    "in progress" and has no Remove link. **Remove** turns a false outage that has ended
    (for example the device or its internet connection was down while the power was on)
    into power on: its off time no longer counts in the chart or the daily totals, and time inside it
    that was not monitored stays not monitored. The pinned chart shows the change at its
    next refresh, within 15 minutes; it draws the last 7 days, and charts already finished
    for earlier days do not change. The live status stays as it is, so the next OFF
    alert's "was ON for" still counts from the end of the removed outage. An outage in
    progress cannot be removed: remove it after power returns. Alerts of the removed outage
    that are still queued (for example while Telegram was unreachable) are dropped only if
    its OFF alert never went out; if the OFF went out, its ON alert is still sent, so the
    channel is not left at power off. If one of the outage's alerts (its OFF, or the ON
    that ended it) is being sent at that moment, the page says so ("An alert about this
    outage is being sent to the channel right now. Nothing changed. Try again in a
    minute.") and nothing changes: try again a minute later.
  - **Reset history:** deletes the location's whole recorded power history. It is refused
    while an outage is in progress (reset after power returns, or delete the location).
    The location then shows **Waiting for first heartbeat**; its next heartbeat restarts
    monitoring as On without an alert, and a new chart is posted and pinned that shows no
    data before the restart. The worker unpins the old chart in its channel within a pass
    or two where the bot still can; otherwise unpin it by hand (the posted messages stay
    in the channel). Alerts already queued are still sent, because they report real
    events. The settings, the device key, the switches and the Delivery status are kept.
- **Device key rotation:** if a key has leaked, open the device setup page, click
  **Regenerate key** and confirm. The old key stops working at once (HTTP 401) and the
  history is kept. The page then shows the new key and the examples with it; a reload or
  a double click never replaces it a second time. Turn maintenance on first, update the
  device, then turn maintenance off. Without maintenance, an OFF can be recorded period +
  grace after the device's last heartbeat with the old key, and subscribers then get an
  OFF alert if alerts are on.
- **Sign-in throttle:** 5 failed sign-ins within a minute from one IP lock sign-in for
  that IP for 5 minutes: every sign-in from it then gets HTTP 429 and "Too many failed
  sign-ins. Try again in 5 minutes.", even with the right password. The IP is the one
  Caddy reports in `X-Forwarded-For` (locally, the direct client address).
- **All-silent and maintenance (Pitfall 7):** an all-silent incident starts only when at
  least 2 active locations (monitored, not in maintenance) are all silent, and its start
  is the moment the last of them fell quiet. It ends at the first heartbeat after that
  start from a location that has been active since before the start. A heartbeat from a
  location in maintenance, or from one that left maintenance or came back on after the
  start, counts only if it arrives after the incident was opened (detected, when the
  start notice is queued): a device that beat in between proves nothing about the server
  or the network. So during an area outage, a powered device in maintenance that keeps
  beating ends the incident at its first beat after the start notice, and the admin gets
  the start and the end notice at most about one heartbeat period apart, the end naming
  that location. A silence that was already reported opens no second incident (one start and
  one end notice per silence), and putting locations into maintenance or deleting them
  never ends an incident by itself. The maintainer confirms this behaviour in section 13
  (d).
- **Data:** everything lives in `docker_data/prod/`: PostgreSQL data, Caddy certificates
  and the nightly database dumps in `docker_data/prod/backups/` (section 14). The power
  history is kept indefinitely; heartbeats themselves are never stored (only each
  location's last heartbeat time), so there is nothing to prune.

## 11. Failure drills (Phase 2 verification)

These three drills prove the restart, Telegram and database behaviour on the real stack.
Run them on the production VPS while alerts still go to the private test channel
(section 1), with the ops chat configured (section 4), and after this release has been
deployed once and has run for a few minutes (the first deploy sends no gap notice,
section 5). Record what happened (times, messages, RestartCount) in the phase
verification. Only then may real subscriber channels be connected. In the commands,
`DOMAIN` is your domain and `<key>` a device key from a location's setup page.

### DoD 2: the whole stack down for 10 minutes

1. Note each location's state in the location list. Pick a device A that is on, and a
   location B whose power is already off.
2. Stop everything: `docker compose -f docker-compose.prod.yml stop`
3. Unplug device A and leave it unplugged. Leave B off.
4. After 10 minutes, deploy: `docker compose -f docker-compose.prod.yml up -d --build --wait`
5. Watch the test channel and the ops chat for 5 minutes.

Expected:

- subscribers get 0 alerts for the downtime itself;
- the ops chat gets exactly 1 `⏸ Monitoring gap …` message with the downtime window;
- A gets exactly 1 OFF within one detection window (its period plus grace) after the
  restart;
- B gets no second OFF; when B's power comes back, it gets one ON whose "was OFF for"
  counts from its original outage start.

Undo: plug A back in (one ON follows).

### DoD 3: Telegram blocked for 10 minutes during an outage

Telegram is blocked before A's OFF is recorded, so both of A's alerts have to wait in the
outbox and go out late (INV-15: blocked 10:00-10:10, last heartbeat 10:02, power back
10:06). Block first: the worker sends a new alert within about a second of recording it.

1. Make Telegram refuse connections from the worker, and note the time (the 10 minutes
   start now):

   ```sh
   docker compose -f docker-compose.prod.yml exec -u root worker sh -c 'echo "127.0.0.1 api.telegram.org" >> /etc/hosts'
   ```

   Check that it is refused, not a timeout. This must end with `ConnectionRefusedError`:

   ```sh
   docker compose -f docker-compose.prod.yml exec worker python -c "import socket; socket.create_connection(('api.telegram.org', 443), 3)"
   ```

   Never block Telegram with a dead HTTPS proxy: the client counts a failed proxy as
   "may have been delivered", so the alert would not be resent.
2. Only now unplug device A. Reload the location list until it shows A as **Off**
   (within A's period plus grace), and note A's **Last heartbeat**: that is the outage
   start. Check that the OFF is waiting in the outbox and was not sent:

   ```sh
   docker compose -f docker-compose.prod.yml exec db psql -U <POSTGRES_USER> -d <POSTGRES_DB> -c "SELECT id, kind, status, last_error FROM outbox_message WHERE channel = 'subscriber' ORDER BY id DESC LIMIT 1"
   ```

   It must show `power_off`, `pending` and `connect_error`. If it shows `sent`, the
   block did not work: remove it (step 5), plug A back in and start again.
3. A few minutes later, and at least 5 minutes before the block ends, restore A's power.
   The restore time is A's **first heartbeat** after power returns, not the moment you
   plug A in. Reload the location list until A shows **On** and note its **Last
   heartbeat**: that is the restore time. Wait until at least 3 minutes have passed
   since then before you remove the block (step 5). Otherwise the ON may go out less
   than 2 minutes after it was recorded and carry no event time.
4. While Telegram is blocked, time heartbeats from another machine, with the key of a
   location whose device is on (never A's key while A is unplugged: every request
   counts as a heartbeat):

   ```sh
   curl -s -o /dev/null -w '%{time_total}\n' 'https://DOMAIN/hb' -H 'Authorization: Bearer <key>'
   ```

5. When the block has lasted 10 minutes, remove it (`sed -i` cannot edit the
   container's `/etc/hosts`, so the file is rewritten in place):

   ```sh
   docker compose -f docker-compose.prod.yml exec -u root worker sh -c 'grep -v api.telegram.org /etc/hosts > /tmp/hosts && cat /tmp/hosts > /etc/hosts'
   ```

   A worker restart also removes the block, because Docker rewrites `/etc/hosts`.

Expected:

- while Telegram is blocked, the test channel gets nothing from A;
- within about 60 s of unblocking, A's OFF and then A's ON arrive, exactly once each.
  Both start with their local event time: the OFF with the outage start (step 2), the
  ON with the restore time, which is A's first heartbeat after power returned
  (step 3). For example `🔴 10:02 POWER OFF`, then `🟢 10:06 POWER ON`;
- every timed heartbeat took under 1 s;
- no `❓ … may not have been delivered` notice: a refused connection means "not sent",
  so the alerts were retried.

### DoD 4: database restart, database down, database frozen

First note the worker's restart count:
`docker inspect -f '{{.RestartCount}}' powermon-prod-worker-1`.

**(a) Database restart.** Restart the database and watch the worker:

```sh
docker compose -f docker-compose.prod.yml restart db
docker compose -f docker-compose.prod.yml logs -f worker
```

Expected: the worker logs `worker lock: database reachable again` or
`worker lock held (generation N)`, and detection resumes within 60 s with no manual
action; the ops chat gets one gap notice.

**(b) Database down for more than 5 minutes.**

```sh
docker compose -f docker-compose.prod.yml stop db
```

Wait 6 minutes, then run `docker compose -f docker-compose.prod.yml start db`.

Expected: exactly 1 `🛑 Database unreachable since …` message in the ops chat while the
database is down, then exactly 1 `⏸ Monitoring gap …` after it is back. While it is
down, `docker compose -f docker-compose.prod.yml ps` shows the worker `unhealthy`, and
the worker is not restarted (its restart count is unchanged).

**(c) Database frozen for 90 s.**

```sh
docker pause powermon-prod-db-1
```

After 90 s:

```sh
docker unpause powermon-prod-db-1
docker inspect -f '{{.RestartCount}}' powermon-prod-worker-1
```

Expected: the worker logs a CRITICAL line and exits with code 70, its restart count
rises by 1, and it comes back `healthy`.

**(d) Health status.** Run `docker compose -f docker-compose.prod.yml ps`.

Expected: every long-running service (`db`, `web`, `worker`, `caddy` and, from Phase 5
on, `backup`) shows a health status.

## 12. Chart checks (Phase 3 verification)

These five checks cover what the tests cannot: how the chart looks to a person, what
Telegram does with a channel pin, and the chart's behaviour and cost on the real stack.
Run (a) on the dev machine. Run (b) to (e) on the production VPS while the charts still go
to the private test channel (section 1), with the ops chat configured (section 4). Record
each result (times, values, a screenshot where it helps) in the phase verification. The
first live DST change is on 2026-10-25: the chart tests (DST goldens included) must pass
and these checks must be recorded before real subscriber channels are connected.

### (a) Design check

The five golden images in `tests/chart/goldens/` are the renderer's own output: the
sample week of `docs/chart-spec.md` section 10 in uk, en and ru, and the two DST Sundays
(2026-10-25, 25 h; 2027-03-28, 23 h) in en. The maintainer approves them here.

1. On the dev machine, in the checkout, render the five images into a new folder. The
   folder is bind-mounted because the image has no source mount; the committed goldens
   are not touched:

   ```sh
   mkdir -p /tmp/chart-check
   docker compose -f docker-compose.local.yml run --build --rm --no-deps --user "$(id -u):$(id -g)" \
     -e CHART_GOLDENS_OUT=/goldens -v /tmp/chart-check:/goldens \
     web pytest -q tests/chart/test_goldens.py
   ```

   `/tmp/chart-check` then holds `sample-uk.png`, `sample-en.png`, `sample-ru.png`,
   `dst-2026-10-25-en.png` and `dst-2027-03-28-en.png`.
2. From the Telegram app, send the five images to the private test channel as photos
   (compressed, as the bot sends them), each with its file name as the caption.
3. View each one on a phone, at about 400 px wide, in Telegram's light theme and then in
   its dark theme. The title, legend, day labels, totals and caption must be readable,
   the today row and the now marker must stand out, and both DST rows must end at 24:00
   (2026-10-25: the repeated hour drawn once; 2027-03-28: 03:00 to 04:00 empty).
4. Run each image through a colour-vision-deficiency simulator (deuteranopia,
   protanopia and grayscale). On, off and not monitored must stay easy to tell apart
   (the target is `docs/assets/chart-mock-en-cvd.png`).
5. Compare them with the mocks `docs/assets/chart-mock-uk.png`, `chart-mock-en.png` and
   `chart-mock-uk-phone.png`.

Record: "approved", or the list of fixes. A rejection means the renderer is fixed and the
goldens are regenerated on purpose (the same command with
`-v <absolute checkout path>/tests/chart/goldens:/goldens`), then checked again.

### (b) Pin service message

1. Watch the test channel while a chart is pinned: at a location's first heartbeat
   (section 6) or at local midnight.
2. Look at the channel on a phone.

Record: whether a "pinned a photo" service message appears in the channel, whether the
phone showed a notification for it, and that the chart is pinned (the pinned-message bar
at the top shows it) with no `📌 Can't pin …` notice in the ops chat.

### (c) DoD 1, chart part: an outage shows within 15 minutes

1. Unplug a device whose location is on, and note the time.
2. Wait for its OFF alert in the test channel (within its period plus grace) and note the
   time.
3. Watch the pinned chart (open it again to see an edit).

Expected: within 15 minutes of the OFF alert, today's row shows the outage in red, from
the last heartbeat (the outage start) to the now marker, and the caption's off time and
outage count include it.

Record: the unplug time, the OFF alert time and the time the chart first showed the
outage. Undo: plug the device back in (one ON follows; the next edit ends the red span).

### (d) DoD 2, chart part: a 10-minute stack downtime is hatched

This can run together with section 11's DoD 2 drill.

1. Note the time, then stop everything: `docker compose -f docker-compose.prod.yml stop`
2. After 10 minutes, deploy: `docker compose -f docker-compose.prod.yml up -d --build --wait`
3. Watch the pinned chart until it is edited, at most 15 minutes after the restart (open
   it again to see an edit).

Expected: from the first edit after the restart on, today's row shows the 10-minute
window hatched (not monitored), never red and never green, and the caption's off time
does not include it. The ops chat gets one `⏸ Monitoring gap …` notice for the same
window.

Record: the stop and start times, the time of the first edit after the restart, and
whether the window was hatched on it.

### (e) INV-14 #3: the midnight chart job on the VPS

At local midnight every location's new chart is posted and pinned and the previous one
unpinned; about two minutes later the previous one gets its final render. All of it runs
beside detection. This check measures render time and worker memory on the
real VPS, and that detection did not pause.

1. Before 23:55 local, start sampling the worker's CPU and memory into a file, in a
   shell on the VPS that stays open until after 00:10. Stop it with Ctrl-C after 00:10:

   ```sh
   while true; do
     printf '%s ' "$(date +%T)"
     docker stats --no-stream --format '{{.Name}} {{.CPUPerc}} {{.MemUsage}}' powermon-prod-worker-1
     sleep 1
   done > ~/worker-stats-midnight.txt
   ```

2. After 00:10, read the worker's chart lines from 23:55 on. `--since` takes the UTC time
   of 23:55 local on the evening's date: `20:55:00Z` while Kyiv is on summer time,
   `21:55:00Z` in winter. Replace `<date>` with that date (`YYYY-MM-DD`):

   ```sh
   docker compose -f docker-compose.prod.yml logs --since <date>T20:55:00Z worker | grep -E 'chart (post|pin|finalize|unpin|refresh) for location'
   docker compose -f docker-compose.prod.yml logs --since <date>T20:55:00Z worker | grep -o 'render_ms=[0-9]*' | sort -t= -k2 -n | tail -1
   docker compose -f docker-compose.prod.yml logs --since <date>T20:55:00Z worker | grep -o 'call_ms=[0-9]*' | sort -t= -k2 -n | tail -1
   ```

3. Check that detection did not pause: no `⏸ Monitoring gap` notice in the ops chat
   between 23:55 and 00:10, and no row from:

   ```sh
   docker compose -f docker-compose.prod.yml exec db psql -U <POSTGRES_USER> -d <POSTGRES_DB> -c "SELECT started_at, ended_at FROM ops_incident WHERE kind = 'monitoring_gap' AND ended_at >= '<date> 20:55:00+00'"
   ```

Expected: after 00:00 local every location has a `post`, a `pin`, a `finalize` and an
`unpin` line, each `ok`; there is no monitoring gap; the worker's memory stays within its
budget (about 110 MB steady, plus about 35 MB while a chart renders).

Record: the largest `render_ms` and `call_ms`, the worker's peak memory in
`~/worker-stats-midnight.txt`, and the result.

## 13. Location management checks (Phase 4 verification)

These four checks cover what the tests cannot: what real Telegram does with the test
message, a bot really removed from a channel and added back, a second bot taking over a
channel, and one behaviour the maintainer decides on. Everything else in the location
pages (switches, edit, delete, key rotation, sign-in throttle, the delivery badge, and
that no page shows a token or a key) is covered by automated tests. No harness starts,
stops or restarts containers for these checks. Run (a) to (c) on the production VPS while
alerts still go to the private test channel (section 1), with the ops chat configured
(section 4). For each check, record the result (times, messages, a screenshot where it
helps) in the phase verification file.

### (a) A real test message

1. Open a location's page and click **Send test message**.
2. Look at the private test channel on a phone.

Expected: within about 15 seconds the page shows "Test message sent. Check that it
arrived in the channel." The channel has exactly one new message,
`🔧 Power Monitor test message: the bot can post here.` (or its uk or ru text, in the
location's language), and the phone makes no sound for it (Telegram's silent message).
The ops chat gets nothing.

Record: the time, the message on the page, and whether the message arrived silently.
Record the result in the phase verification file.

### (b) Bot removed, then added back: failing, test message, recovery

1. In the test channel's settings, remove the location's bot from the administrators.
2. Unplug the device and wait for its OFF (heartbeat period plus grace). The OFF alert is
   refused.
3. Check the admin panel and the ops chat. Expected: the location list shows
   `Failing since HH:MM (http_403)` in the Delivery column, the location page shows the
   same with "The bot cannot post in the channel: …", and the ops chat has exactly one
   `🚫 Alerts for … are failing (Telegram: http_403). …`.
4. Plug the device back in, so an ON alert is queued behind the OFF. Wait 20 minutes:
   no second failing notice comes, and nothing reaches the channel.
5. Add the bot back as an administrator with the "Post messages" and "Edit messages of
   others" rights.
6. On the location page, click **Send test message**.

Expected: the page shows "Test message sent. Delivery is marked OK again, and any queued
alerts go out next." and the Delivery row shows `OK`, as does the list. The ops chat gets
exactly one `✅ Alerts for … are delivered again.` Within a few seconds the channel gets
the OFF and then the ON, each starting with the local time of its event (for example
`🔴 14:05 POWER OFF`), because they go out late. No alert arrives twice.

Record: the time of each step, the notices and the alerts as they arrived. Record the
result in the phase verification file.

### (c) A new bot takes over the channel

Telegram's documentation says that a channel administrator with the "Edit messages of
others" right can unpin any message, including one another bot posted. This has not been
tried yet.

1. Create a second bot with @BotFather. Make it an administrator of the test channel with
   the "Post messages" and "Edit messages of others" rights. Keep the first bot there.
2. Note the pinned weekly chart in the channel (posted by the first bot).
3. On the location's **Edit location** page, paste the second bot's token into "New bot
   token" and save.
4. Watch the channel for a few minutes (the worker makes one chart call per delivery
   pass), then read the worker's chart lines:

   ```sh
   docker compose -f docker-compose.prod.yml logs --since 15m worker | grep 'chart '
   ```

Expected: the old chart is unpinned (its message stays in the channel), and a new chart,
posted by the second bot, is posted and pinned. The log has a
`chart release for location <id>: ok` line, then `post` and `pin` lines. If the release
was refused, the log has one WARNING
`chart release for location <id>: permanent error …` and the old pin stays; that is the
accepted risk in section 10 ("Edit location"): unpin it by hand.

Record: whether the second bot unpinned the first bot's chart, and the log lines. Record
the result in the phase verification file. Afterwards, put the first bot's token back if
the second bot is not to be kept.

### (d) All-silent with a location in maintenance (Pitfall 7)

Section 10 ("All-silent and maintenance") describes what the admin gets when an area
outage hits while a location in maintenance keeps beating: the all-silent start notice,
then its end notice about one heartbeat period later, naming the location in
maintenance; and only one such pair per silence. This follows the decision taken for
Phase 4 (an all-silent incident may end by a heartbeat from a location in maintenance,
but only one received after the incident was opened). The automated tests cover it. The
maintainer confirms that this is the wanted behaviour.

1. Read section 10, "All-silent and maintenance".
2. Optional, on the real stack with at least 3 locations: put one location in
   maintenance and keep its device powered, then cut the internet of two active
   devices' routers (not their power) for about 5 minutes, and watch the ops chat.

Expected (optional run): one `⚠️ All … active locations silent since …` notice, then one
`✅ Heartbeats are back (first: <the location in maintenance>, …)` at most about one
heartbeat period later, and no further all-silent notice while the two devices stay cut
off. The subscribers of the two cut-off locations get their OFF alerts as usual.

Record: "accepted", or the change wanted. Record the result in the phase verification
file.

## 14. Backups and restore

### Nightly dumps

The `backup` service dumps the database every night. It runs in its own container, on the
same `postgres:18.6-trixie` image as `db`, so `pg_dump` has the server's version.

- **When:** every night at `BACKUP_TIME_UTC` (section 4; default 03:00 UTC). Whenever the
  newest dump is older than 24 hours, it dumps at once: on a fresh install, or after the
  server was down over night. It checks once a minute. A failed dump is retried after 5
  minutes, then at growing intervals of up to 1 hour.
- **How:** `pg_dump` in PostgreSQL's custom format. Each dump is checked with
  `pg_restore --list` before it replaces anything; only then are the dumps beyond the
  newest `BACKUP_KEEP` (default 14) deleted. A failed dump deletes nothing.
- **Where:** `docker_data/prod/backups/` (locally `docker_data/local/backups/`), outside
  the PostgreSQL data directory. Each file is named by its UTC start time, for example
  `powermon-20261003T030000Z.dump`. The directory is 0700 and every dump 0600, owned by
  the container's postgres user (uid 999; the host may show another name for it). Dumps
  hold every bot token and device key, so reading them needs `sudo`. There is no automated
  off-site copy: copy dumps off the VPS by hand (below).
- **Health:** `docker compose -f docker-compose.prod.yml ps backup` shows `healthy` while
  the newest dump is under 26 hours old. A failing backup shows only there and in
  `docker compose -f docker-compose.prod.yml logs backup`: each dump logs
  `backup: dump powermon-….dump ok`, each failure one `backup: error: …` line. There is no
  ops notice for it, so look at it after each deploy and now and then.

### A dump on demand

Before a risky change (a large update, a manual database edit), take a dump at once:

```sh
docker compose -f docker-compose.prod.yml exec backup bash /backup/backup.sh --dump-now
```

It logs `backup: dump powermon-….dump ok`. As after a nightly dump, only the newest
`BACKUP_KEEP` dumps are kept.

### Copying a dump off the VPS

Nothing copies dumps off the VPS automatically. To keep a copy on your computer:

1. On the VPS, in the clone, list the dumps and copy one to your home directory, owned by
   you and readable only by you:

   ```sh
   sudo ls -l docker_data/prod/backups/
   sudo install -m 600 -o "$USER" docker_data/prod/backups/<file> ~/<file>
   ```

2. On your computer: `scp <user>@<vps>:<file> .`
3. Back on the VPS: `rm ~/<file>`

Keep the copy as safe as the server: it holds every bot token and device key. Do not
stream a dump through `ssh -t … sudo cat`: the terminal mangles binary output.

### Restore from a backup

A restore replaces the whole database with a dump. Everything recorded after the dump is
lost. The restore works only into a new, empty database: `backup.sh --restore` refuses a
database that already has tables, and it never drops or cleans one.

**Same VPS** (the database is damaged, or a change must be undone). In the clone on the VPS
(for example `cd ~/power-monitor`), choose the dump, then run:

```sh
sudo ls -l docker_data/prod/backups/
F=powermon-20261003T030000Z.dump
docker compose -f docker-compose.prod.yml stop web worker backup
docker compose -f docker-compose.prod.yml stop db
sudo mv docker_data/prod/postgres "docker_data/prod/postgres.before-restore-$(date -u +%Y%m%dT%H%M%SZ)"
docker compose -f docker-compose.prod.yml up -d --wait db
docker compose -f docker-compose.prod.yml run --rm --no-deps backup bash /backup/backup.sh --restore "$F"
docker compose -f docker-compose.prod.yml run --rm --build migrate
docker compose -f docker-compose.prod.yml run --rm --no-deps migrate python manage.py post_restore
docker compose -f docker-compose.prod.yml up -d --build --wait
```

What each step does:

1. `F` is the file name of the chosen dump (the newest one from before the problem).
2. `stop web worker backup`, then `stop db`: nothing may write to the database, or dump
   it, during the restore.
3. `sudo mv …`: the old data directory is moved aside, never deleted. It is the way back
   if the restore goes wrong.
4. `up -d --wait db` starts a new, empty database with the `POSTGRES_*` values of the env
   file.
5. `--restore "$F"` restores the dump in one transaction. It logs
   `backup: restore of … ok`, or an error, and then nothing was restored.
6. `run --rm --build migrate` applies the migrations newer than the dump and syncs the
   admin account from the env file (as on every deploy).
7. `post_restore` restarts every location silently (see "What happens after a restore").
   It prints
   `post_restore: N location(s) now wait for their first heartbeat, N queued message(s) dropped, N open incident(s) closed`.
   It refuses, and changes nothing, while a worker is running.
8. `up -d --build --wait` starts everything, as a deploy does.

Rules:

- Never run a plain `up` (or the deploy command) before `post_restore`: it would start
  web and worker on a database that is not ready for them, and the dump's queued alerts
  and old states would reach subscribers.
- If a step fails, stop there: nothing has reached subscribers yet. To try again, stop
  `db`, move the new `docker_data/prod/postgres` aside as well, and start again from
  `up -d --wait db`. To go back to the old database, stop `db`, move the new directory
  aside and the `postgres.before-restore-…` directory back to `docker_data/prod/postgres`,
  then run the deploy command.
- Once the restored stack has run correctly for a day, delete the moved-aside data
  directory by hand: `sudo rm -rf docker_data/prod/postgres.before-restore-…`.

**A fresh VPS** (the old server is lost):

1. Follow sections 2 to 4: the server prep, the clone and `.env.docker_production`. New
   `POSTGRES_*` values are fine: the dump carries the data, and the restore makes the new
   database user its owner.
2. In the clone, create the backup directory:

   ```sh
   mkdir -p docker_data/prod/backups
   chmod 700 docker_data/prod/backups
   ```

3. From your computer, copy the dump into it (here the clone is `~/power-monitor`):
   `scp <file> <user>@<new-vps>:power-monitor/docker_data/prod/backups/`
4. In the clone, run the same commands as above from `up -d --wait db` on:

   ```sh
   F=powermon-20261003T030000Z.dump
   docker compose -f docker-compose.prod.yml up -d --wait db
   docker compose -f docker-compose.prod.yml run --rm --no-deps backup bash /backup/backup.sh --restore "$F"
   docker compose -f docker-compose.prod.yml run --rm --build migrate
   docker compose -f docker-compose.prod.yml run --rm --no-deps migrate python manage.py post_restore
   docker compose -f docker-compose.prod.yml up -d --build --wait
   ```

   The domain's DNS records must point at the new VPS before the last command: Caddy
   requests the TLS certificate on first start (section 2). The devices keep their keys
   and need no change.

### What happens after a restore

- Every location shows **Waiting for first heartbeat** and restarts monitoring at its
  device's next heartbeat, as On and with no alert (as at a location's first heartbeat,
  section 6). Its history up to the dump is kept. The hours from the dump to that
  heartbeat are drawn as not monitored (hatched), never as an outage.
- Subscribers get no message: the alerts the dump had queued are dropped, never sent.
- The admin gets one monitoring-gap notice (`⏸ Monitoring gap …`, section 5) for the lost
  hours. The problems the dump had open (all-silent, failing delivery, a chart that cannot
  be pinned) are closed without a notice; a problem that persists is reported again.
- An outage that was still in progress when the server was lost never gets its ON alert.
  A location whose power is off after the restore is detected only after its device has
  sent a heartbeat again.
- After a location's first heartbeat its weekly chart carries on as usual. A chart posted
  after the dump is unknown to the restored database: if it stays pinned, unpin it by hand
  in Telegram.

## 15. History and backup checks (Phase 5 verification)

These three checks cover what the tests cannot: a real restore on real containers, the
backup container on the real VPS, and a history reset seen in a real channel. Removing an
outage, resetting a location's history, the backup script's schedule, rotation, health
and restore refusal, the post-restore step, and that no new page shows a token or a key
are covered by automated tests. No harness starts, stops or restarts containers for these
checks. Run (a) on the dev machine, and (b) and (c) on the production VPS while alerts
still go to the private test channel (section 1).

### (a) INV-25 #2: a restore drill on the dev machine

The drill restores a dump of the local stack (section 9) into a second Compose project,
`pm05drill`, whose database lives in memory, then starts web and worker on the restored
database. The local database is never touched. The local stack must have some history: at
least one test location that has had heartbeats for a while (an outage too, if you can).

1. Write an override file outside the repository, for example `/tmp/restore-drill.yml`.
   It keeps the drill's database in memory and moves its web app to another port:

   ```yaml
   services:
     db:
       volumes: !override []
       tmpfs:
         - /var/lib/postgresql:size=2g
     web:
       ports: !override
         - "127.0.0.1:8001:8000"
   ```

2. Stop the local web and worker, so the data stays still, and take a dump. Note the dump's
   file name from its `backup: dump powermon-….dump ok` line:

   ```sh
   docker compose -f docker-compose.local.yml stop web worker
   docker compose -f docker-compose.local.yml exec backup bash /backup/backup.sh --dump-now
   ```

3. Take the source's fingerprint (one line per table: name, row count, checksum):

   ```sh
   docker compose -f docker-compose.local.yml run --rm -T --no-deps migrate python manage.py history_fingerprint > /tmp/fp-source.txt
   ```

4. Restore the dump into the drill project and take its fingerprint, before
   `post_restore`:

   ```sh
   F=powermon-20261003T120000Z.dump
   docker compose -p pm05drill -f docker-compose.local.yml -f /tmp/restore-drill.yml up -d --wait db
   docker compose -p pm05drill -f docker-compose.local.yml -f /tmp/restore-drill.yml run --rm --no-deps backup bash /backup/backup.sh --restore "$F"
   docker compose -p pm05drill -f docker-compose.local.yml -f /tmp/restore-drill.yml run --rm --build migrate
   docker compose -p pm05drill -f docker-compose.local.yml -f /tmp/restore-drill.yml run --rm -T --no-deps migrate python manage.py history_fingerprint > /tmp/fp-target.txt
   diff /tmp/fp-source.txt /tmp/fp-target.txt
   ```

   Expected: `diff` prints nothing. Both files have the same three lines (`location`,
   `power_interval`, `chart_message`).
5. Run the post-restore step, note the outbox's highest id, then start the drill's web and
   worker. Never start the drill's `backup` service: it would write into the local backup
   directory.

   ```sh
   docker compose -p pm05drill -f docker-compose.local.yml -f /tmp/restore-drill.yml run --rm --no-deps migrate python manage.py post_restore
   docker compose -p pm05drill -f docker-compose.local.yml -f /tmp/restore-drill.yml exec db psql -U <POSTGRES_USER> -d <POSTGRES_DB> -c "SELECT coalesce(max(id), 0) AS last_id, count(*) FILTER (WHERE status IN ('pending', 'sending')) AS queued FROM outbox_message"
   docker compose -p pm05drill -f docker-compose.local.yml -f /tmp/restore-drill.yml up -d --wait web worker
   ```

   Expected: `post_restore` prints its counts line, and `queued` is 0.
6. Wait about a minute, then check what the drill's worker queued and did (`N` is the
   `last_id` from step 5):

   ```sh
   docker compose -p pm05drill -f docker-compose.local.yml -f /tmp/restore-drill.yml exec db psql -U <POSTGRES_USER> -d <POSTGRES_DB> -c "SELECT id, channel, kind, status FROM outbox_message WHERE id > N ORDER BY id"
   docker compose -p pm05drill -f docker-compose.local.yml -f /tmp/restore-drill.yml logs worker | grep -E 'chart (post|pin|refresh)|ops chat not configured'
   ```

   Expected: exactly one new row, `ops | ops_gap`, and no `subscriber` row. If
   `.env.docker_local` has no ops chat, there is no new row at all; the worker log then
   has one `ops notice (ops chat not configured): ⏸ Monitoring gap …` WARNING instead. The
   worker log has no `chart post`, `chart pin` or `chart refresh` line.
7. Clean up: remove the drill project and its images, then start the local web and worker
   again:

   ```sh
   docker compose -p pm05drill -f docker-compose.local.yml -f /tmp/restore-drill.yml down -v --rmi local
   docker compose -f docker-compose.local.yml start web worker
   ```

Record: the `diff` result, the `post_restore` line, the new outbox rows (or the gap
WARNING) and the grep result. Record the result in the phase verification file.

### (b) The backup container on the VPS

1. After this release is deployed (section 5), on the VPS:

   ```sh
   docker compose -f docker-compose.prod.yml ps backup
   docker compose -f docker-compose.prod.yml logs backup
   sudo ls -la docker_data/prod/backups
   ```

   Expected: `backup` is `healthy`. Its log has
   `backup: started: a dump every night at 03:00 UTC, the newest 14 kept` and one
   `backup: dump powermon-<deploy time>.dump ok`. The listing shows the directory (`.`) as
   `drwx------` and one `-rw-------` dump named with the deploy time (UTC).
2. The next day, after `BACKUP_TIME_UTC`, run the same three commands.

   Expected: `backup` is still `healthy`, and there is a second dump, named with that
   night's time (just after 03:00 UTC), with its `ok` line in the log.

Record: the listings and the log lines of both days. Record the result in the phase
verification file.

### (c) A history reset on the private test channel

1. Pick a test location whose alerts go to the private test channel, whose weekly chart is
   pinned there and whose device is on.
2. On its page, in "Reset history", click **Reset history**, then confirm.
3. Watch the channel and the location page until the device's next heartbeat, then read
   the worker's chart lines:

   ```sh
   docker compose -f docker-compose.prod.yml logs --since 15m worker | grep 'chart '
   ```

Expected: the page shows "History reset. …" and **Waiting for first heartbeat**. Within a
pass or two (seconds) the pinned chart is unpinned; its message stays in the channel. The
log has a `chart release for location <id>: ok` line. No alert is sent. At the device's
next heartbeat the location shows **On**, with no alert, and a new chart is posted and
pinned (`post` and `pin` lines in the log); its bars start at that heartbeat, with no data
before it.

Record: the times, whether the old chart was unpinned, the log lines and a screenshot of
the new chart. Record the result in the phase verification file.

DoD 7 (INV-26: a fresh VPS reaches a working deployment in at most 30 minutes by following
this README, including one test message) is re-run at milestone close, not here.
