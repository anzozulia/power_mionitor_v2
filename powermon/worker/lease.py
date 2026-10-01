"""RED-phase interface stub of the single-worker lease (replaced in GREEN)."""

from collections.abc import Mapping
from typing import Any

LOCK_KEY = 0x504F5745524D4F4E


class Lease:
    def __init__(self, settings_dict: Mapping[str, Any]) -> None:
        self._settings = settings_dict

    def try_acquire(self) -> bool:
        return False

    def alive(self) -> bool:
        return False

    @property
    def pid(self) -> int | None:
        return -1

    def close(self) -> None:
        return None
