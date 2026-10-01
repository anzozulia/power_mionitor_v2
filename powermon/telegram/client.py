"""Telegram Bot API client (RED interface stub; filled in GREEN)."""

from dataclasses import dataclass
from typing import Literal

SendKind = Literal["ok", "not_sent", "maybe_delivered", "rate_limited", "transient", "permanent"]


@dataclass(frozen=True)
class SendResult:
    kind: SendKind
    retry_after: int | None = None
    code: str = ""


class TelegramClient:
    def __init__(
        self,
        token: str,
        *,
        api_base: str = "https://api.telegram.org",
        timeout: tuple[float, float] = (5.0, 10.0),
    ) -> None:
        self._timeout = timeout

    def send_message(self, chat_id: int, text: str) -> SendResult:
        return SendResult("ok")
