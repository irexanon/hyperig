"""Helpers shared by the profile modules (dump, watcher): HTML escaping, picking the files out of an
Instagram post / reel / story, and sending them to Telegram as a single file or an album."""
import asyncio
import html
from datetime import timezone
from typing import Awaitable, Callable

from telegram import InputFile, InputMediaPhoto, InputMediaVideo
from telegram.error import BadRequest, RetryAfter

from utils.logger import get_logger

log = get_logger("tgsend")

Fetch = Callable[[str], Awaitable["bytes | None"]]


def esc(text) -> str:
    return html.escape(str(text or ""), quote=False)


def clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def num(value) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "?"


async def tg_call(call):
    """Run a Telegram call, waiting out flood limits (up to 3 tries)."""
    for _ in range(2):
        try:
            return await call()
        except RetryAfter as e:
            wait = e.retry_after.total_seconds() if hasattr(e.retry_after, "total_seconds") else float(e.retry_after)
            await asyncio.sleep(wait + 1)
    return await call()


def items_of(m) -> list[tuple[str, str]]:
    """(kind, url) for every file of a post / reel / story."""
    out: list[tuple[str, str]] = []
    resources = getattr(m, "resources", None)
    if m.media_type == 8 and resources:
        for r in resources:
            if r.media_type == 2 and r.video_url:
                out.append(("video", str(r.video_url)))
            elif r.thumbnail_url:
                out.append(("photo", str(r.thumbnail_url)))
    elif m.media_type == 2 and m.video_url:
        out.append(("video", str(m.video_url)))
    elif m.thumbnail_url:
        out.append(("photo", str(m.thumbnail_url)))
    return out


def caption_of(m, kind: str) -> str:
    """HTML caption for a post / reel / story (kind is "post", "reel" or "story")."""
    when = m.taken_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if m.taken_at else ""
    if kind == "story":
        return f"📖 Story · {when}"
    text = esc(clip(getattr(m, "caption_text", "") or "", 600))
    link = f'<a href="https://www.instagram.com/p/{m.code}/">View on Instagram</a>' if getattr(m, "code", None) else ""
    stats = []
    likes = getattr(m, "like_count", -1)
    if likes is not None and likes >= 0:
        stats.append(f"❤️ {num(likes)}")
    if getattr(m, "comment_count", None) is not None:
        stats.append(f"💬 {num(m.comment_count)}")
    head = " · ".join(x for x in (when, *stats) if x)
    return "\n".join(x for x in (text, head, link) if x)


async def send_one(bot, chat_id: int, kind: str, data: bytes, caption: str) -> None:
    cap = caption[:1024] or None
    try:
        if kind == "photo":
            await tg_call(lambda: bot.send_photo(chat_id, photo=data, caption=cap, parse_mode="HTML"))
        else:
            await tg_call(
                lambda: bot.send_video(
                    chat_id, video=InputFile(data, filename="video.mp4"), caption=cap, parse_mode="HTML", supports_streaming=True
                )
            )
    except BadRequest as e:
        log.info("Telegram refused the %s (%s), sending it as a file", kind, e)
        name = "photo.jpg" if kind == "photo" else "video.mp4"
        await tg_call(lambda: bot.send_document(chat_id, document=InputFile(data, filename=name), caption=cap, parse_mode="HTML"))


async def send_items(bot, chat_id: int, items: list[tuple[str, str]], caption: str, fetch: Fetch) -> int:
    """Send one post/story: a single file, or an album (Telegram allows 10 per album).
    fetch(url) returns the file's bytes, or None when it could not be downloaded. Returns files sent."""
    files: list[tuple[str, bytes]] = []
    for kind, url in items[:10]:
        data = await fetch(url)
        if data:
            files.append((kind, data))
    if not files:
        return 0
    if len(files) == 1:
        await send_one(bot, chat_id, files[0][0], files[0][1], caption)
        return 1
    media = []
    for i, (kind, data) in enumerate(files):
        cap = caption[:1024] if i == 0 else None
        if kind == "photo":
            media.append(InputMediaPhoto(media=data, caption=cap, parse_mode="HTML"))
        else:
            media.append(InputMediaVideo(media=data, caption=cap, parse_mode="HTML", supports_streaming=True))
    try:
        await tg_call(lambda: bot.send_media_group(chat_id, media=media))
    except BadRequest as e:
        log.info("Album refused (%s), sending the files one by one", e)
        for i, (kind, data) in enumerate(files):
            await send_one(bot, chat_id, kind, data, caption if i == 0 else "")
    return len(files)
