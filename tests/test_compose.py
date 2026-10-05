"""The Compose files and the Caddyfile, checked as data (SEC-02, INV-22 #1, INV-23, D-02-05).

Production is never started on a dev machine; it is deployed on the VPS (01-11). These
parse checks keep the committed deploy shape honest: only the TLS proxy is exposed,
migrations gate the app, data lives under docker_data/<env>, logs are capped. Every
long-running service has a healthcheck, and the worker's reads its health file (OPS-05).
The backup service dumps from the db image's own pg_dump into docker_data/<env>/backups,
outside the data directory, and gets only the database and backup settings (OPS-06, D-11).

The checks stay true when 01-11 adds the local worker service and switches local
migrate to ``release``.
"""

import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
import yaml
from django.conf import settings

from powermon.telegram.client import DEFAULT_TIMEOUT
from powermon.worker.management.commands.run_worker import JOIN_TIMEOUT_S
from powermon.worker.supervision import HEALTH_FILE

BASE_DIR = Path(settings.BASE_DIR)
COMPOSE_FILES = {"local": "docker-compose.local.yml", "prod": "docker-compose.prod.yml"}
ENVS = sorted(COMPOSE_FILES)
# The local-only Tailwind watcher override (UI-13): -f docker-compose.local.yml -f this file.
DEV_UI_FILE = "docker-compose.dev-ui.yml"
CADDYFILE = BASE_DIR / "docker" / "Caddyfile"
# Caddy reads only these two from its environment; every other app setting is blanked.
CADDY_KEYS = frozenset({"DOMAIN", "ACME_EMAIL"})
# The backup container gets the database settings and its own; every other one is blanked.
BACKUP_KEY_PREFIXES = ("POSTGRES_", "BACKUP_")
ENV_FILES = {"local": ".env.docker_local", "prod": ".env.docker_production"}
# Caddyfile env placeholders such as {$DOMAIN} or {$PORT:443}; their braces are not blocks.
_PLACEHOLDER_RE = re.compile(r"\{\$[A-Za-z_][A-Za-z0-9_]*(?::[^}]*)?\}")


def _load(filename: str) -> dict[str, Any]:
    path = BASE_DIR / filename
    assert path.is_file(), f"{filename} is missing"
    data = yaml.safe_load(path.read_text())
    assert isinstance(data, dict), f"{filename} is not a mapping"
    return data


def _compose(env: str) -> dict[str, Any]:
    return _load(COMPOSE_FILES[env])


def _services(env: str) -> dict[str, dict[str, Any]]:
    services = _compose(env).get("services")
    assert isinstance(services, dict) and services, f"{COMPOSE_FILES[env]} has no services"
    return services


def _depends_on(service: dict[str, Any]) -> dict[str, dict[str, Any]]:
    deps = service.get("depends_on") or {}
    if isinstance(deps, list):
        # Short syntax carries no condition, which every check below rejects.
        return {name: {} for name in deps}
    return {name: spec or {} for name, spec in deps.items()}


def _flag_value(args: list[Any], flag: str) -> Any:
    assert flag in args, f"{flag} is missing"
    index = args.index(flag)
    assert index + 1 < len(args), f"{flag} has no value"
    return args[index + 1]


def _env_example_keys() -> list[str]:
    keys = []
    for raw in (BASE_DIR / ".env.example").read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, _ = line.partition("=")
        assert sep, f".env.example line is not KEY=value: {line!r}"
        keys.append(key.strip())
    return keys


def _caddyfile() -> str:
    assert CADDYFILE.is_file(), "docker/Caddyfile is missing"
    return CADDYFILE.read_text()


def _caddy_blocks(text: str) -> dict[str, list[str]]:
    """Top-level Caddyfile blocks by address ("" is the global options block)."""
    blocks: dict[str, list[str]] = {}
    current: list[str] = []
    depth = 0
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        bare = _PLACEHOLDER_RE.sub("ENV", line)
        if depth == 0:
            assert bare.endswith("{"), f"top-level Caddyfile line is not a block: {raw!r}"
            current = blocks.setdefault(line[:-1].strip(), [])
        else:
            current.append(line)
        depth += bare.count("{") - bare.count("}")
        assert depth >= 0, f"unbalanced braces at {raw!r}"
    assert depth == 0, "unbalanced braces in the Caddyfile"
    return blocks


def _nested_block(lines: list[str], opener: str) -> list[str]:
    """The lines inside the one nested block that opens with the line ``opener``."""
    assert lines.count(opener) == 1, f"expected exactly one {opener!r} line"
    inner: list[str] = []
    depth = 1
    for line in lines[lines.index(opener) + 1 :]:
        bare = _PLACEHOLDER_RE.sub("ENV", line)
        depth += bare.count("{") - bare.count("}")
        if depth <= 0:
            return inner
        inner.append(line)
    raise AssertionError(f"{opener!r} has no closing brace")


# Exposure (SEC-02, INV-22 #1)


def test_INV22_only_caddy_publishes_ports_in_prod() -> None:
    services = _services("prod")

    published = {name: svc["ports"] for name, svc in services.items() if "ports" in svc}

    assert published == {"caddy": ["80:80", "443:443"]}
    assert services["web"]["expose"] == ["8000"]
    assert not [name for name, svc in services.items() if svc.get("network_mode") == "host"]


def test_local_only_web_publishes_on_loopback() -> None:
    services = _services("local")

    published = {name: svc["ports"] for name, svc in services.items() if "ports" in svc}

    assert published == {"web": ["127.0.0.1:8000:8000"]}
    assert not [name for name, svc in services.items() if svc.get("network_mode") == "host"]


@pytest.mark.parametrize("env", ENVS)
def test_compose_files_declare_their_project_name(env: str) -> None:
    # D-03: the deploy command needs no -p flag.
    expected = {"local": "powermon-local", "prod": "powermon-prod"}[env]

    assert _compose(env)["name"] == expected


# Logs, images, commands


@pytest.mark.parametrize("env", ENVS)
def test_every_service_has_capped_json_logs(env: str) -> None:
    for name, svc in _services(env).items():
        logging = svc.get("logging") or {}
        assert logging.get("driver") == "json-file", f"{env}/{name}"
        assert {"max-size", "max-file"} <= set(logging.get("options") or {}), f"{env}/{name}"


@pytest.mark.parametrize("env", ENVS)
def test_no_service_has_both_build_and_image(env: str) -> None:
    # P-2: a shared image tag once made Compose re-run the old migrate container.
    offenders = [name for name, svc in _services(env).items() if "build" in svc and "image" in svc]

    assert offenders == []


@pytest.mark.parametrize("env", ENVS)
def test_commands_are_exec_form_and_gunicorn_flags(env: str) -> None:
    services = _services(env)
    for name, svc in services.items():
        if "command" in svc:
            assert isinstance(svc["command"], list), f"{env}/{name} uses a shell-form command"

    web = services["web"]["command"]

    assert web[0] == "gunicorn"
    # Redacted UTC gunicorn.error lines and the once-per-container web start (D-16, MON-05).
    assert _flag_value(web, "powermon.wsgi") == "--config"
    assert _flag_value(web, "--config") == "python:powermon.web.gunicorn_conf"
    assert "--no-control-socket" in web
    # gunicorn's worker heartbeat dir on tmpfs (a flag value, not a temp file we write).
    assert _flag_value(web, "--worker-tmp-dir") == "/dev/shm"  # noqa: S108
    # Below the 30 s stop_grace_period, so a deploy never SIGKILLs a request (STACK G7).
    assert _flag_value(web, "--graceful-timeout") == "20"
    assert services["web"]["stop_grace_period"] == "30s"
    # The access log would print ?key= device keys.
    assert not [arg for arg in web if str(arg).startswith("--access-log")]


def test_prod_and_local_web_run_the_same_gunicorn_command() -> None:
    assert _services("prod")["web"]["command"] == _services("local")["web"]["command"]


# The local Tailwind dev watcher (UI-13, T-06-23)


def test_dev_ui_override_is_local_only() -> None:
    services = _load(DEV_UI_FILE).get("services")
    assert isinstance(services, dict)
    local = _services("local")

    assert set(services) == {"css", "web"}
    css, web = services["css"], services["web"]
    # The watcher builds only the css stage and writes the gitignored build file through
    # the bind mount; it publishes nothing.
    assert css["build"] == {"context": ".", "target": "css"}
    assert css["command"] == [
        "tailwindcss",
        "-i",
        "powermon/web/assets/css/app.css",
        "-o",
        "powermon/web/static/web/build/app.css",
        "--watch=always",
    ]
    assert css["volumes"] == ["./powermon:/app/powermon"]
    assert "ports" not in css
    assert "expose" not in css
    # web only swaps gunicorn for runserver with DEBUG on; its one published port stays the
    # local file's loopback binding.
    assert web["command"] == ["python", "manage.py", "runserver", "0.0.0.0:8000"]
    assert web["environment"] == {"DEBUG": "1"}
    assert web["volumes"] == ["./powermon:/app/powermon"]
    assert "ports" not in web
    assert local["web"]["ports"] == ["127.0.0.1:8000:8000"]
    for name, svc in services.items():
        logging = svc.get("logging") or {}
        assert logging.get("driver") == "json-file", name
        assert {"max-size", "max-file"} <= set(logging.get("options") or {}), name
        assert isinstance(svc["command"], list), f"{name} uses a shell-form command"
        assert "image" not in svc, name
    # The production deployment never references the override.
    for path in (COMPOSE_FILES["prod"], "docker/Caddyfile"):
        assert "dev-ui" not in (BASE_DIR / path).read_text(), path


# Migrations gate the app (D-02, OPS-07, INV-26)


@pytest.mark.parametrize("env", ENVS)
def test_migrate_is_a_one_shot_gate(env: str) -> None:
    services = _services(env)

    assert services["migrate"]["restart"] == "no"
    assert _depends_on(services["migrate"])["db"] == {"condition": "service_healthy"}
    assert "migrate" in _depends_on(services["web"])
    for name, svc in services.items():
        deps = _depends_on(svc)
        if "migrate" in deps:
            assert deps["migrate"] == {"condition": "service_completed_successfully"}, name


def test_prod_migrate_runs_release_before_web_and_worker() -> None:
    services = _services("prod")

    assert services["migrate"]["command"] == ["python", "manage.py", "release"]
    assert services["worker"]["command"] == ["python", "manage.py", "run_worker"]
    for name in ("web", "worker"):
        deps = _depends_on(services[name])
        assert deps["db"] == {"condition": "service_healthy"}, name
        assert deps["migrate"] == {"condition": "service_completed_successfully"}, name
        assert services[name]["restart"] == "unless-stopped", name


def test_local_runs_the_same_release_and_worker_as_prod() -> None:
    # OPS-07: the local one-command stack runs the deploy steps production runs.
    local = _services("local")
    prod = _services("prod")

    assert local["migrate"]["command"] == prod["migrate"]["command"]
    assert local["worker"]["command"] == ["python", "manage.py", "run_worker"]
    deps = _depends_on(local["worker"])
    assert deps["db"] == {"condition": "service_healthy"}
    assert deps["migrate"] == {"condition": "service_completed_successfully"}
    assert local["worker"]["restart"] == "unless-stopped"
    # Above the joins' 20 s budget, so a stop never SIGKILLs a send mid-write.
    assert local["worker"]["stop_grace_period"] == prod["worker"]["stop_grace_period"] == "30s"
    assert "ports" not in local["worker"]


def _duration_s(value: str) -> int:
    match = re.fullmatch(r"([0-9]+)s", value)
    assert match, f"{value!r} is not a whole number of seconds"
    return int(match.group(1))


@pytest.mark.parametrize("env", ENVS)
def test_worker_join_budget_covers_one_send_and_fits_the_grace_period(env: str) -> None:
    # On SIGTERM the relay starts no new send; the one in flight must end, and its outcome
    # be written, before serve stops waiting for the thread. Cut off earlier, a row that
    # never left the client stays "sending" and the next start makes it "uncertain".
    # Docker sends SIGKILL when stop_grace_period runs out, so the joins end before it.
    connect, read = DEFAULT_TIMEOUT
    grace = _duration_s(_services(env)["worker"]["stop_grace_period"])

    assert connect + read < JOIN_TIMEOUT_S < grace


# Health checks (OPS-05, D-15)


def _worker_healthcheck(env: str) -> dict[str, Any]:
    healthcheck = _services(env)["worker"].get("healthcheck")
    assert isinstance(healthcheck, dict), f"{env}/worker has no healthcheck"
    return healthcheck


def _worker_healthcheck_code(env: str) -> str:
    """The Python source of the worker's exec-form ``python -c`` healthcheck."""
    test = _worker_healthcheck(env).get("test")
    assert isinstance(test, list) and len(test) == 4, f"{env}/worker healthcheck is not exec form"
    assert test[:3] == ["CMD", "python", "-c"], f"{env}/worker healthcheck is not python -c"
    code = test[3]
    assert isinstance(code, str) and code, f"{env}/worker healthcheck has no code"
    return code


def _run_healthcheck(code: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


@pytest.mark.parametrize("env", ENVS)
def test_every_long_running_service_has_a_healthcheck(env: str) -> None:
    # Docker reports a health status only for a service that has a healthcheck. A one-shot
    # declares restart "no" and is exempt; a service without a restart key is not.
    exempt = []
    for name, svc in _services(env).items():
        if svc.get("restart") == "no":
            exempt.append(name)
            continue
        healthcheck = svc.get("healthcheck") or {}
        test = healthcheck.get("test")
        assert healthcheck.get("disable") is not True, f"{env}/{name} disables its healthcheck"
        # ["NONE"] or an empty list would turn the inherited check off.
        assert isinstance(test, list) and len(test) > 1, f"{env}/{name} has no healthcheck"
        assert test[0] in {"CMD", "CMD-SHELL"}, f"{env}/{name} healthcheck is {test!r}"

    assert exempt == ["migrate"]


@pytest.mark.parametrize("env", ENVS)
def test_worker_healthcheck_checks_the_health_file_age(env: str) -> None:
    # D-15: healthy only while the file the worker touches is under 30 s old. The path is
    # the writer's own constant, so the file the worker writes and the one Docker reads
    # cannot drift apart. Unhealthy alone restarts nothing; a hung loop exits 70 instead.
    healthcheck = _worker_healthcheck(env)
    code = _worker_healthcheck_code(env)

    assert f"os.stat({str(HEALTH_FILE)!r}).st_mtime < 30" in code
    assert {key: value for key, value in healthcheck.items() if key != "test"} == {
        "interval": "10s",
        "timeout": "5s",
        "retries": 3,
        "start_period": "30s",
    }
    assert healthcheck == _worker_healthcheck("prod")


def test_worker_healthcheck_command_passes_only_for_a_fresh_file() -> None:
    # The command Docker runs, run as written against the real path (OPS-05 boundary):
    # touched 5 s ago is healthy, 31 s ago is not, and a missing file is not.
    code = _worker_healthcheck_code("local")
    saved = HEALTH_FILE.stat() if HEALTH_FILE.exists() else None
    try:
        HEALTH_FILE.touch()
        now = time.time()
        os.utime(HEALTH_FILE, (now - 5, now - 5))
        fresh = _run_healthcheck(code)
        now = time.time()
        os.utime(HEALTH_FILE, (now - 31, now - 31))
        stale = _run_healthcheck(code)
        HEALTH_FILE.unlink()
        missing = _run_healthcheck(code)
    finally:
        if saved is None:
            HEALTH_FILE.unlink(missing_ok=True)
        else:
            HEALTH_FILE.touch()
            os.utime(HEALTH_FILE, ns=(saved.st_atime_ns, saved.st_mtime_ns))

    assert fresh.returncode == 0, fresh.stderr
    assert stale.returncode == 1, stale.stderr
    assert missing.returncode != 0
    assert "FileNotFoundError" in missing.stderr


# Images, env files and data (D-03, D-04)


@pytest.mark.parametrize("env", ENVS)
def test_postgres_18_layout_and_logging(env: str) -> None:
    db = _services(env)["db"]

    assert db["image"] == "postgres:18.6-trixie"
    # PG 18 layout: PGDATA is /var/lib/postgresql/18/docker under this mount (D-04).
    assert f"./docker_data/{env}/postgres:/var/lib/postgresql" in db["volumes"]
    # Failed statements are never logged with their literal values (STACK G4).
    assert "log_min_error_statement=panic" in db["command"]
    assert "log_error_verbosity=terse" in db["command"]


# Backups (OPS-06, INV-25, D-09, D-11)


@pytest.mark.parametrize("env", ENVS)
def test_backup_service_shape(env: str) -> None:
    services = _services(env)
    backup = services["backup"]

    # The db's own image tag, so pg_dump's major version matches the server (D-09).
    assert backup["image"] == services["db"]["image"] == "postgres:18.6-trixie"
    assert "build" not in backup
    assert backup["command"] == ["bash", "/backup/backup.sh"]
    assert (BASE_DIR / "docker" / "backup" / "backup.sh").is_file()
    # Dumps hold every bot token: their own directory, outside PGDATA's mount (D-11).
    assert backup["volumes"] == [
        "./docker/backup:/backup:ro",
        f"./docker_data/{env}/backups:/backups",
    ]
    sources = [volume.split(":", 1)[0] for volume in backup["volumes"]]
    assert not [s for s in sources if s.startswith(f"./docker_data/{env}/postgres")]
    assert "ports" not in backup
    assert "expose" not in backup
    # Never migrate: the restore runs this service against a new, empty database.
    assert _depends_on(backup) == {"db": {"condition": "service_healthy"}}
    assert backup["restart"] == "unless-stopped"
    assert backup["healthcheck"] == {
        "test": ["CMD", "bash", "/backup/backup.sh", "--health"],
        "interval": "60s",
        "timeout": "10s",
        "retries": 3,
        "start_period": "5m",
        "start_interval": "5s",
    }
    assert backup["logging"] == services["db"]["logging"]


@pytest.mark.parametrize("env", ENVS)
def test_backup_gets_only_database_and_backup_settings(env: str) -> None:
    backup = _services(env)["backup"]
    keys = _env_example_keys()
    passed = {key for key in keys if key.startswith(BACKUP_KEY_PREFIXES)}

    environment = backup.get("environment")

    # `environment` overrides `env_file`: the one env file stays, and every app secret
    # (SECRET_KEY, ADMIN_PASSWORD, OPS_BOT_TOKEN, ...) is blanked. A key added to
    # .env.example later fails here until the backup service blanks or passes it (D-11).
    assert backup["env_file"] == [ENV_FILES[env]]
    assert isinstance(environment, dict), "backup environment must be a KEY: value mapping"
    assert {"POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD", "BACKUP_KEEP"} <= passed
    for key in keys:
        if key in passed:
            assert key not in environment, f"backup must read {key} from the env file"
        else:
            # A bare key (None) would pass the deploying shell's value through.
            assert environment.get(key) == "", f"backup must blank {key} with an explicit ''"
    assert set(environment) == set(keys) - passed


def test_prod_app_services_build_runtime_and_read_prod_env() -> None:
    services = _services("prod")

    built = {name for name, svc in services.items() if "build" in svc}

    assert built == {"migrate", "web", "worker"}
    for name in sorted(built):
        assert services[name]["build"] == {"context": ".", "target": "runtime"}, name
        assert services[name]["env_file"] == [".env.docker_production"], name
    assert services["db"]["env_file"] == [".env.docker_production"]
    assert not [n for n, s in services.items() if ".env.docker_local" in s.get("env_file", [])]


def test_local_app_services_build_dev_and_read_local_env() -> None:
    services = _services("local")

    built = {name for name, svc in services.items() if "build" in svc}

    assert {"migrate", "web"} <= built
    for name in sorted(built):
        assert services[name]["build"] == {"context": ".", "target": "dev"}, name
        assert services[name]["env_file"] == [".env.docker_local"], name
    assert not [n for n, s in services.items() if ".env.docker_production" in s.get("env_file", [])]


# Caddy (D-05, P-15, T-01-62)


def test_caddy_image_and_persistent_volumes() -> None:
    caddy = _services("prod")["caddy"]

    assert caddy["image"] == "caddy:2.11.4-alpine"
    assert "build" not in caddy
    assert set(caddy["volumes"]) == {
        "./docker/Caddyfile:/etc/caddy/Caddyfile:ro",
        "./docker_data/prod/caddy/data:/data",
        "./docker_data/prod/caddy/config:/config",
    }
    # 127.0.0.1, not localhost: localhost:2019 is refused in the alpine image.
    assert caddy["healthcheck"]["test"] == [
        "CMD",
        "wget",
        "-q",
        "-O",
        "/dev/null",
        "http://127.0.0.1:2019/config/",
    ]
    assert caddy["restart"] == "unless-stopped"


def test_caddy_gets_no_app_secrets() -> None:
    caddy = _services("prod")["caddy"]
    keys = _env_example_keys()

    environment = caddy.get("environment")

    # Compose `environment` overrides `env_file`, so the one env file (D-03) stays while
    # the internet-facing proxy holds only DOMAIN and ACME_EMAIL. A key added to
    # .env.example later fails here until caddy blanks it.
    assert caddy["env_file"] == [".env.docker_production"]
    assert isinstance(environment, dict), "caddy environment must be a KEY: value mapping"
    assert CADDY_KEYS <= set(keys)
    for key in keys:
        if key in CADDY_KEYS:
            assert key not in environment, f"caddy must read {key} from the env file"
        else:
            # A bare key (None) would pass the deploying shell's value through.
            assert environment.get(key) == "", f"caddy must blank {key} with an explicit ''"


def test_caddyfile_https_only_proxy() -> None:
    text = _caddyfile()
    lines = [line.strip() for line in text.splitlines()]

    blocks = _caddy_blocks(text)

    assert "email {$ACME_EMAIL}" in lines
    assert "protocols h1 h2" in lines
    # Exactly the global options block and one HTTPS site; Caddy's HTTP->HTTPS redirect
    # covers port 80, so there is no plain-HTTP site (D-05).
    assert set(blocks) == {"", "{$DOMAIN}"}
    addresses = [part for address in blocks for part in re.split(r"[,\s]+", address) if part]
    assert not [a for a in addresses if a.lower().startswith("http://")]
    # acme_ca would replace the default issuers and drop the ZeroSSL fallback (P-15).
    assert not [line for line in lines if line.startswith("acme_ca")]
    site = blocks["{$DOMAIN}"]
    assert "reverse_proxy web:8000 {" in site
    assert "request_buffers 64KB" in site
    # No access log, which would print every ?key= device key. The error log still sees
    # request URIs; test_INV23_caddy_logs_cut_the_query_out_of_request_uris covers it.
    assert not [line for line in site if line.split()[0] == "log"]


def test_INV23_caddy_logs_cut_the_query_out_of_request_uris() -> None:
    # No access log is not enough: Caddy's error logger (http.log.error) writes the request
    # URI of every 5xx, e.g. a 502 while web restarts during a deploy, so a heartbeat key
    # sent as ?key= would reach the proxy log (INV-23, OPS-08; Wave 3 audit F3). Caddy has
    # one logger, it takes every log name, and it cuts the query out of request>uri.
    global_options = _caddy_blocks(_caddyfile())[""]

    default_logger = _nested_block(global_options, "log default {")
    log_filter = _nested_block(default_logger, "format filter {")

    assert [line for line in global_options if line.split()[0] == "log"] == ["log default {"]
    assert not [line for line in default_logger if line.split()[0] == "include"]
    uri_filters = [line.split() for line in log_filter if line.split()[0] == "request>uri"]
    assert len(uri_filters) == 1, "request>uri needs exactly one filter"
    assert len(uri_filters[0]) == 4, "expected: request>uri regexp <pattern> <replacement>"
    _, kind, pattern, replacement = uri_filters[0]
    # A regexp on the raw string: Caddy's query filter parses the URI and writes it
    # unchanged when parsing fails, and leaves anything after a '#' alone.
    assert kind == "regexp"
    for uri in ("/hb?key=SECRETKEY123", "/hb?x=1&key=SECRETKEY123", "/hb#?key=SECRETKEY123"):
        logged = re.sub(pattern, replacement, uri)
        assert "SECRETKEY123" not in logged, uri
        assert logged.startswith(uri.partition("?")[0]), uri  # the path stays readable
