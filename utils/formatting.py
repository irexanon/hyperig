"""Turns an instagrapi DirectMessage into (kind, text, media_url) for Telegram.

Instagram has many message types. Anything we cannot forward natively is
turned into a short readable placeholder so nothing is silently dropped.
"""
from typing import Any


def _dig(obj: Any, *path: Any) -> Any:
    """Safely walk dicts, pydantic models and lists: _dig(m, 'a', 'b', 0, 'url')."""
    for key in path:
        if obj is None:
            return None
        if isinstance(key, int):
            try:
                obj = obj[key]
            except (IndexError, TypeError, KeyError):
                return None
        elif isinstance(obj, dict):
            obj = obj.get(key)
        else:
            obj = getattr(obj, key, None)
    return obj


def _url(value: Any) -> str | None:
    return str(value) if value else None


def render(m: Any) -> tuple[str, str, str | None]:
    """Return (kind, text, media_url). kind is text | photo | video | voice | animation."""
    item_type = m.item_type or ""
    text = (m.text or "").strip()

    if item_type == "text":
        return "text", text or "[empty message]", None
    if item_type == "like":
        return "text", "❤️", None

    if item_type == "link" and m.link:
        link_text = (m.link.text or text or "").strip()
        url = _dig(m.link, "link_context", "link_url")
        return "text", " ".join(p for p in (link_text, str(url) if url else "") if p), None

    # Voice notes
    audio = _dig(m.media, "audio_url")
    if audio:
        return "voice", "", _url(audio)

    # Photos / videos (including disappearing ones)
    label = "disappearing " if item_type == "raven_media" or m.visual_media else ""
    visual = m.media or _dig(m.visual_media, "media")
    video = _url(_dig(visual, "video_url") or _dig(visual, "video_versions", 0, "url"))
    photo = _url(_dig(visual, "thumbnail_url") or _dig(visual, "image_versions2", "candidates", 0, "url"))
    if visual is not None:
        if video:
            return "video", (f"[{label.strip()}] " if label else "") + text, video
        if photo:
            return "photo", (f"[{label.strip()}] " if label else "") + text, photo
        if item_type in ("media", "raven_media"):
            return "text", f"[{label}media no longer available]", None

    # Shared posts / reels
    shared = m.clip or m.media_share
    code = _dig(shared, "code")
    if code:
        kind = "reel" if m.clip else "post"
        path = "reel" if m.clip else "p"
        return "text", f"[{kind}] https://www.instagram.com/{path}/{code}/", None
    if m.xma_share and (_dig(m.xma_share, "video_url") or _dig(m.xma_share, "title")):
        xma = m.xma_share
        return "text", f"[shared] {_dig(xma, 'title') or ''} {_dig(xma, 'video_url') or ''}".strip(), None

    # Story replies / shares
    if item_type == "reel_share" and m.reel_share:
        reply = _dig(m.reel_share, "text") or text
        return "text", f"[story reply] {reply}".strip(), None
    if item_type == "story_share":
        return "text", f"[shared a story] {_dig(m.story_share, 'message') or text}".strip(), None

    # GIFs
    images = _dig(m.animated_media, "images", "fixed_height")
    gif = _dig(images, "mp4") or _dig(images, "url")
    if gif:
        return "animation", text, _url(gif)

    if m.placeholder:
        note = _dig(m.placeholder, "message") or _dig(m.placeholder, "title") or "unavailable"
        return "text", f"[{note}]", None

    return "text", f"[{item_type or 'unsupported message'}] {text}".strip(), None
