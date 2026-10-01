# Power Monitor

## 1. What it is

Power Monitor tracks whether mains power is on at a few locations. A small mains-powered
device at each location (a router cron job, an ESP32, a Raspberry Pi; no UPS) sends a
heartbeat every minute or so. When the heartbeats stop for longer than the location's
period plus grace, the location's Telegram channel gets an OFF alert; the first heartbeat
after the outage brings an ON alert. One admin manages the locations in a small web panel.

The stack is Django (web panel and heartbeat endpoint), one worker process (outage
detection and Telegram delivery), PostgreSQL and Caddy (TLS), run with Docker Compose.

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
  rule above) with the bot added as an administrator with the "Post messages" right.
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

## 5. Deploy

One command deploys a fresh install and every update:

```sh
git pull
docker compose -f docker-compose.prod.yml up -d --build --wait
```

It builds the image, starts PostgreSQL, runs the one-shot `migrate` service (apply
migrations, then sync the admin account from the env file), and starts `web` and
`worker` only after `migrate` succeeded. `caddy` does not wait for `migrate`: it starts
on its own and keeps running if a migration fails (section 8). Check the result:

```sh
docker compose -f docker-compose.prod.yml ps
```

`db`, `web`, `worker` and `caddy` are `healthy`. `ps -a` also shows `migrate` as
`Exited (0)`. `docker compose -f docker-compose.prod.yml logs worker` shows
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
   "Post messages" right.
3. On the location's setup page, click **Reveal key** and copy the curl or cron example
   onto the device. The device must run on mains power only (no UPS): heartbeats measure
   power and internet at the device.
4. The location list shows **Waiting for first heartbeat**, then **On** after the first
   heartbeat. The first heartbeat sends no alert.

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

This runs `db`, `migrate`, `web` and `worker` (no Caddy). The app is on
`http://localhost:8000`; sign in with the admin account from `.env.docker_local`.
Local alerts are real Telegram messages too, so use a private test channel here as well.
Stop the stack with `docker compose -f docker-compose.local.yml down`.

Tests run inside the stack against its PostgreSQL:

```sh
docker compose -f docker-compose.local.yml run --build --rm web pytest
```

The full check (lint, format, types, tests, coverage gate):

```sh
docker compose -f docker-compose.local.yml run --build --rm web sh -c "ruff check . && ruff format --check . && mypy powermon && pytest -q --cov=powermon --cov-report=term-missing:skip-covered && coverage report --include='powermon/engine/*,powermon/alerts/*,powermon/i18n/*,powermon/telegram/*,powermon/worker/detection.py,powermon/worker/io_loop.py,powermon/worker/lease.py,powermon/worker/supervision.py' --fail-under=80"
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
  `web`, `worker`, `caddy`). Each service keeps at most 3 files of 10 MB (Docker's
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
  `unhealthy` for `db`, `web`, `worker` and `caddy`. The worker touches its health file
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
- **Late alerts:** an alert sent more than 2 minutes after its transition was recorded
  starts with the local time of its event (the outage start for OFF, the restore time for
  ON): `🔴 17:27 POWER OFF`, or `🔴 30.09 23:58 POWER OFF` when the event was on another
  day. While Telegram is unreachable, alerts wait in the outbox and are retried per bot;
  heartbeats and detection never wait for Telegram.
- **Data:** everything lives in `docker_data/prod/` (PostgreSQL data and Caddy
  certificates). Nightly backups are not part of this release yet.

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

1. Unplug device A. As soon as the location list shows A as **Off** (the OFF is
   recorded), make Telegram refuse connections from the worker:

   ```sh
   docker compose -f docker-compose.prod.yml exec -u root worker sh -c 'echo "127.0.0.1 api.telegram.org" >> /etc/hosts'
   ```

   Check that it is refused, not a timeout. This must end with `ConnectionRefusedError`:

   ```sh
   docker compose -f docker-compose.prod.yml exec worker python -c "import socket; socket.create_connection(('api.telegram.org', 443), 3)"
   ```

   Never block Telegram with a dead HTTPS proxy: the client counts a failed proxy as
   "may have been delivered", so the alert would not be resent.
2. After a few minutes, restore A's power.
3. While Telegram is blocked, time heartbeats from another machine, with the key of a
   location whose device is on (never A's key while A is unplugged: every request
   counts as a heartbeat):

   ```sh
   curl -s -o /dev/null -w '%{time_total}\n' 'https://DOMAIN/hb' -H 'Authorization: Bearer <key>'
   ```

4. After 10 minutes, remove the block (`sed -i` cannot edit the container's
   `/etc/hosts`, so the file is rewritten in place):

   ```sh
   docker compose -f docker-compose.prod.yml exec -u root worker sh -c 'grep -v api.telegram.org /etc/hosts > /tmp/hosts && cat /tmp/hosts > /etc/hosts'
   ```

   A worker restart also removes the block, because Docker rewrites `/etc/hosts`.

Expected:

- within about 60 s of unblocking, A's OFF and then its ON arrive exactly once each, both
  starting with their local event time (the outage start, then the restore time);
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

Expected: every long-running service (`db`, `web`, `worker`, `caddy`) shows a health
status.
