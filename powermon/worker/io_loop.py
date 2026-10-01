"""RED-phase interface stub of the outbox relay (replaced in GREEN)."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta

BACKOFF_CAP_S = 30
PERMANENT_BACKOFF = timedelta(minutes=15)
MAX_RETRY_AFTER_S = 3600


@dataclass
class RelayState:
    not_before: dict[str, datetime] = field(default_factory=dict)


def bot_key(token: str) -> str:
    return ""


def run_iteration(now: datetime, state: RelayState) -> bool:
    return False
