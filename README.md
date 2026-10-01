# Power Monitor

## 1. What it is

Power Monitor tracks whether mains power is on at a few locations. A small mains-powered
device at each location (a router cron job, an ESP32, a Raspberry Pi; no UPS) sends a
heartbeat every minute or so. When the heartbeats stop for longer than the location's
period plus grace, the location's Telegram channel gets an OFF alert; the first heartbeat
after the outage brings an ON alert. One admin manages the locations in a small web panel.

The stack is Django (web panel and heartbeat endpoint), one worker process (outage
detection and Telegram delivery), PostgreSQL and Caddy (TLS), run with Docker Compose.

**Test channel rule (current release).** Until the restart-safety work of the next phase
is verified, every location's chat ID must point at a **private** Telegram channel whose
only member is the maintainer. A deploy or restart can still produce a false OFF/ON pair,
and real subscribers must never see one. Connect real subscriber channels only after that
phase is verified.

## 2. Prerequisites

- A VPS with 1 vCPU and 1-2 GB RAM, running Ubuntu or Debian, with SSH access.
- A domain name whose A record (and AAAA record, if the VPS has IPv6) points at the VPS
  **before the first start**: Caddy requests the TLS certificate on first start.
- Ports 80 and 443 open to the internet (check the provider's firewall too).
- For each location: a Telegram bot token from @BotFather and a private channel (see the
  rule above) with the bot added as an administrator with the "Post messages" right.

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

`timedatectl` must show `System clock synchronized: yes`.

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

## 5. Deploy

One command deploys a fresh install and every update:

```sh
git pull
docker compose -f docker-compose.prod.yml up -d --build --wait
```

It builds the image, starts PostgreSQL, runs the one-shot `migrate` service (apply
migrations, then sync the admin account from the env file), and starts `web`, `worker`
and `caddy` only after `migrate` succeeded. Check the result:

```sh
docker compose -f docker-compose.prod.yml ps
```

`db`, `web` and `caddy` are `healthy` and `worker` is running. `ps -a` also shows `migrate`
as `Exited (0)`. `docker compose -f docker-compose.prod.yml logs worker` shows
`worker active`. `curl -fsS https://DOMAIN/healthz` prints `ok`.

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

A deploy restarts the web app and the worker. Until the next phase is verified, that
downtime can cause a false OFF/ON pair, which is why alerts go to a private channel only
(section 1).

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
docker compose -f docker-compose.local.yml run --build --rm web sh -c "ruff check . && ruff format --check . && mypy powermon && pytest -q --cov=powermon --cov-report=term-missing:skip-covered && coverage report --include='powermon/engine/*,powermon/alerts/*,powermon/i18n/*,powermon/telegram/*,powermon/worker/detection.py,powermon/worker/io_loop.py' --fail-under=80"
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
  `web`, `worker`, `caddy`). Each service keeps at most 3 files of 10 MB. Bot tokens and
  device keys are redacted from the app logs, and Caddy writes no access log.
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
- **Worker restarts:** only one worker is active at a time (a database lock). If its lock
  connection drops (for example on a database restart), it exits with code 3 and Docker
  restarts it; a second worker started by mistake logs
  `standby: waiting for the worker lock` and does nothing.
- **Data:** everything lives in `docker_data/prod/` (PostgreSQL data and Caddy
  certificates). Nightly backups are not part of this release yet.
