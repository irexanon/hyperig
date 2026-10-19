"""One plain progress message that is edited in place while a job runs, then left in the chat as the result.

    Downloading reel
    @nasa

    [#####-----]  50%
    Fetching media
"""
import html
import time

from telegram.error import TelegramError

BAR = 10
MIN_GAP = 1.8   # seconds between edits (Telegram rate limits message edits)


def bar(fraction: float) -> str:
    fraction = max(0.0, min(1.0, fraction))
    filled = round(fraction * BAR)
    return f"[{'#' * filled}{'-' * (BAR - filled)}]  {int(fraction * 100):>3d}%"


class Progress:
    def __init__(self, message, title: str, subject: str = ""):
        self.message = message
        self.title = title
        self.subject = subject
        self._last = 0.0
        self._shown = ""

    @classmethod
    async def begin(cls, bot, chat_id: int, title: str, subject: str = "", step: str = "Starting"):
        text = cls._render(title, subject, step, 0.0)
        message = await bot.send_message(chat_id, text, parse_mode="HTML")
        self = cls(message, title, subject)
        self._shown, self._last = text, time.monotonic()
        return self

    @staticmethod
    def _render(title: str, subject: str, step: str, fraction: float | None, count: str = "") -> str:
        lines = [f"<b>{html.escape(title)}</b>"]
        if subject:
            lines.append(html.escape(subject))
        lines.append("")
        if fraction is not None:
            lines.append(f"<code>{bar(fraction)}</code>")
        lines.append(html.escape(step + (f"  {count}" if count else "")))
        return "\n".join(lines)

    async def _push(self, text: str, force: bool = False) -> None:
        now = time.monotonic()
        if text == self._shown or (not force and now - self._last < MIN_GAP):
            return
        try:
            await self.message.edit_text(text, parse_mode="HTML")
            self._shown, self._last = text, now
        except TelegramError:
            pass

    async def set(self, step: str, fraction: float | None = None, count: str = "") -> None:
        """Show a step with an optional bar, or a plain count (for jobs whose size is not known up front)."""
        await self._push(self._render(self.title, self.subject, step, fraction, count))

    async def done(self, summary: str) -> None:
        await self._push(f"<b>Done</b>\n{html.escape(summary)}", force=True)

    async def fail(self, reason: str) -> None:
        """`reason` is plain text or already-safe HTML-free text."""
        await self._push(f"<b>Failed</b>\n{html.escape(reason)}", force=True)
