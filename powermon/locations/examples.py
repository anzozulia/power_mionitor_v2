"""Device setup examples: the one generator behind the setup page and the INV-24 #3 test.

Pure module: it imports nothing from Django. The caller passes the base URL (the configured
PUBLIC_BASE_URL, never a request header), the key (full or masked) and the period.

- curl and cron send the key in ``Authorization: Bearer`` (D-07, redacted by Caddy).
- Only the two wget lines, for devices that cannot set a header, use ``?key=``.
- GNU wget gets ``--max-redirect=0``; BusyBox wget and OpenWrt uclient-fetch reject that
  option, so their line omits it. That is safe because ``/hb`` never redirects (D-06).

Keys are ``[A-Za-z0-9]`` only, so no example needs quoting or URL-encoding for the key, and
no ``%`` ever reaches a crontab line.
"""

MIN_PERIOD_S = 10
MAX_PERIOD_S = 3600
_SECONDS_PER_MINUTE = 60
_MINUTES_PER_HOUR = 60


def heartbeat_url(base_url: str) -> str:
    """``<base_url>/hb``; a trailing slash on the base URL is dropped."""
    if not base_url.startswith(("https://", "http://")):
        raise ValueError("the base URL must be an absolute http:// or https:// URL")
    return f"{base_url.rstrip('/')}/hb"


def curl_cmd(url: str, key: str, *, multiline: bool = False) -> str:
    """The recommended example: curl with the key in a header (UI-SPEC example a)."""
    if multiline:
        return f'curl -fsS -m 10 -o /dev/null \\\n  -H "Authorization: Bearer {key}" \\\n  {url}'
    return f'curl -fsS -m 10 -o /dev/null -H "Authorization: Bearer {key}" {url}'


def cron_lines(url: str, key: str, period_s: int) -> list[str]:
    """Crontab lines that send a heartbeat at least every ``period_s`` seconds.

    Below a minute: one line per offset k * P while k * P < 60, the later ones stepped with
    ``sleep``. From a minute: every n = P // 60 minutes (rounded down, never less often).
    """
    if not MIN_PERIOD_S <= period_s <= MAX_PERIOD_S:
        raise ValueError(f"the period must be {MIN_PERIOD_S}-{MAX_PERIOD_S} seconds")
    cmd = curl_cmd(url, key)
    if period_s < _SECONDS_PER_MINUTE:
        offsets = range(0, _SECONDS_PER_MINUTE, period_s)
        return [
            f"* * * * * {cmd}" if offset == 0 else f"* * * * * sleep {offset}; {cmd}"
            for offset in offsets
        ]
    minutes = period_s // _SECONDS_PER_MINUTE
    if minutes == 1:
        return [f"* * * * * {cmd}"]
    if minutes < _MINUTES_PER_HOUR:
        return [f"*/{minutes} * * * * {cmd}"]
    # minutes == 60: the period is capped at 3600 s.
    return [f"0 * * * * {cmd}"]


def wget_gnu(url: str, key: str) -> str:
    """GNU wget with the key in the URL (UI-SPEC example c)."""
    return f'wget -q -O /dev/null --max-redirect=0 "{url}?key={key}"'


def wget_busybox(url: str, key: str) -> str:
    """BusyBox wget or OpenWrt uclient-fetch, key in the URL, no --max-redirect (example d)."""
    return f'wget -q -O /dev/null "{url}?key={key}"'
