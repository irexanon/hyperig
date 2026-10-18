from dataclasses import dataclass
from typing import Awaitable, Callable


@dataclass
class Command:
    """A Telegram slash command. The handler gets the arguments and returns the reply text."""

    handler: Callable[[list[str]], Awaitable[str]]
    description: str
    usage: str = ""
    sensitive: bool = False  # True: the bot deletes your command message afterwards (codes, etc.)


class BaseModule:
    """Drop a new file in modules/ with a subclass of this and it is loaded automatically."""

    name = "base"
    description = ""

    def __init__(self, ctx):
        self.ctx = ctx

    def get_commands(self) -> dict[str, Command]:
        return {}

    async def process(self, message):
        """Called for every incoming Instagram message before it goes to Telegram.
        Return the message (optionally modified) or None to drop it."""
        return message
