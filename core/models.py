from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass
class IncomingMessage:
    """A normalized Instagram DM, handed to modules and then to the Telegram bridge."""

    id: str
    thread_id: str
    thread_name: str
    is_group: bool
    sender_id: str
    sender_username: str
    timestamp: datetime
    item_type: str
    kind: str                       # "text" | "photo" | "audio"
    text: str
    media_url: str | None = None
    raw: Any = None
