"""The Compose files and the Caddyfile, checked as data (SEC-02, INV-22 #1, D-02 to D-05).

Production is never started on a dev machine; it is deployed on the VPS (01-11). These
parse checks keep the committed deploy shape honest: only the TLS proxy is exposed,
migrations gate the app, data lives under docker_data/<env>, logs are capped.

The checks stay true when 01-11 adds the local worker service and switches local
migrate to ``release``.
"""

import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from django.conf import settings

BASE_DIR = Path(settings.BASE_DIR)
COMPOSE_FILES = {"local": "docker-compose.local.yml", "prod": "docker-compose.prod.yml"}
ENVS = sorted(COMPOSE_FILES)
CADDYFILE = BASE_DIR / "docker" / "Caddyfile"
# Caddy reads only these two from its environment; every other app setting is blanked.
CADDY_KEYS = frozenset({"DOMAIN", "ACME_EMAIL"})
# Caddyfile env placeholders such as {$DOMAIN} or {$PORT:443}; their braces are not blocks.
_PLACEHOLDER_RE = re.compile(r"\{\$[A-Za-z_][A-Za-z0-9_]*(?::[^}]*)?\}")


def _compose(env: str) -> dict[str, Any]:
    path = BASE_DIR / COMPOSE_FILES[env]
    assert path.is_file(), f"{COMPOSE_FILES[env]} is missing"
    data = yaml.safe_load(path.read_text())
    assert isinstance(data, dict), f"{COMPOSE_FILES[env]} is not a mapping"
    return data


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
    # No access log: device keys in ?key= never reach a proxy log.
    assert not [line for line in site if line.split()[0] == "log"]
