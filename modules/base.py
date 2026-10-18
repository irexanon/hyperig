from dataclasses import dataclass
from typing import Awaitable, Callable


@dataclass
class Command:
    """A Telegram slash command. The handler gets the arguments and returns the reply text."""

    handler: Callable[[list[str]], Awaitable[str]]
    description: str
    usage: str = ""
    sensitive: bool = False  # True: the bot deletes your command message afterwards (codes, etc.)
    # True: the handler is called as handler(args, update) and sends its own replies (buttons, media, ...)
    # instead of returning text.
    takes_update: bool = False


class BaseModule:
    """Drop a new file in modules/ with a subclass of this and it is loaded automatically."""

    name = "base"
    description = ""

    def __init__(self, ctx):
        self.ctx = ctx

    def get_commands(self) -> dict[str, Command]:
        return {}

    def get_callbacks(self) -> dict:
        """Inline-button handlers. Key = prefix, value = async handler(rest, query, context).
        A button whose callback_data is "m:<prefix>:<rest>" is routed to that handler."""
        return {}

    async def on_start(self) -> None:
        """Called once the Instagram account is up (use it to start a background task)."""

    async def on_stop(self) -> None:
        """Called on shutdown (cancel background tasks here)."""

    async def process(self, message):
        """Called for every incoming Instagram message before it goes to Telegram.
        Return the message (optionally modified) or None to drop it."""
        return message
