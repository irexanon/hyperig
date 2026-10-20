"""Auto-detect Instagram links sent to the Telegram bot and download their media.

Supported: Reels, feed posts/carousels, Stories, Highlights and profile cards.
Downloads use instagrapi's media-specific helpers and a temporary folder so full-resolution
files can be uploaded to Telegram without leaving files behind.
"""
import html
import time
import re
import tempfile
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from telegram import InputFile, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, InputMediaVideo
from telegram.error import BadRequest, TelegramError

from core.auth import describe_error
from modules.base import BaseModule, Command
from utils.logger import get_logger
from utils.progress import Progress
from utils.tgsend import send_one

TELEGRAM_MAX_BYTES = 50 * 1024 * 1024

log = get_logger("autodl")

_ALLOWED_HOSTS = {"instagram.com", "www.instagram.com", "m.instagram.com", "instagr.am", "www.instagr.am"}
_RESERVED = {
    "about", "accounts", "api", "direct", "explore", "legal", "p", "reel", "reels",
    "stories", "s", "static", "developer", "web",
}
_USERNAME_RE = re.compile(r"^[A-Za-z0-9._]{1,30}$")
_TRAILING_PUNCTUATION = ".,!?;:)]}>"


def normalize_instagram_url(raw: str) -> str | None:
    """Normalize a pasted Instagram URL and reject other hosts."""
    value = (raw or "").strip().rstrip(_TRAILING_PUNCTUATION)
    if not value:
        return None
    if not re.match(r"^https?://", value, re.I):
        value = "https://" + value.lstrip("/")
    try:
        parsed = urlparse(value)
    except ValueError:
        return None
    if parsed.scheme.lower() not in ("http", "https") or (parsed.hostname or "").lower() not in _ALLOWED_HOSTS:
        return None
    if not parsed.path or parsed.path == "/":
        return None
    return value


def classify_instagram_url(raw: str) -> tuple[str, str] | None:
    """Return (category, normalized_url) for supported Instagram URL forms."""
    url = normalize_instagram_url(raw)
    if not url:
        return None
    parts = [part for part in urlparse(url).path.split("/") if part]
    if not parts:
        return None

    first = parts[0].lower()
    if first in ("reel", "reels") and len(parts) >= 2:
        return "reel", url
    if first in ("p", "tv") and len(parts) >= 2:
        return "post", url
    if first == "stories" and len(parts) >= 3 and parts[1].lower() == "highlights":
        return "highlight", url
    if first == "stories" and len(parts) >= 3 and parts[1].lower() not in _RESERVED:
        return "story", url
    if first == "s" and len(parts) >= 2:
        return "highlight", url
    if len(parts) == 1 and _USERNAME_RE.fullmatch(parts[0]) and first not in _RESERVED:
        return "profile", url
    return None


def _value(obj, key: str):
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def _nested(obj, *keys):
    current = obj
    for key in keys:
        current = _value(current, key)
        if current is None:
            return None
    return current


def _find_url(node, keys: tuple[str, ...], depth: int = 0, seen: set[int] | None = None) -> str | None:
    """Find a direct audio resource URL in the nested instagrapi media payload."""
    if node is None or depth > 8:
        return None
    if seen is None:
        seen = set()
    if not isinstance(node, (str, int, float, bool, bytes)):
        ident = id(node)
        if ident in seen:
            return None
        seen.add(ident)

    for key in keys:
        candidate = _value(node, key)
        if candidate is not None:
            candidate_url = str(candidate)
            if candidate_url.lower().startswith(("https://", "http://")):
                path = urlparse(candidate_url).path.lower()
                if path.endswith((".m4a", ".mp3", ".aac", ".mp4")):
                    return candidate_url

    if isinstance(node, dict):
        children = node.values()
    elif isinstance(node, (list, tuple)):
        children = node
    elif hasattr(node, "model_dump"):
        try:
            children = node.model_dump().values()
        except Exception:
            children = ()
    elif hasattr(node, "__dict__"):
        children = vars(node).values()
    else:
        children = ()

    for child in children:
        result = _find_url(child, keys, depth + 1, seen)
        if result:
            return result
    return None


class AutoDownloadModule(BaseModule):
    name = "autodl"
    toggleable = True
    label = "Link downloader (paste a link or /dl)"
    description = "Auto-detect Instagram links and download media to Telegram"

    def __init__(self, ctx):
        super().__init__(ctx)
        self._busy: set[int] = set()

    def get_commands(self):
        return {
            "dl": Command(
                self.cmd_dl,
                "Download an Instagram link (add 'audio' for Reel audio)",
                "/dl <Instagram URL> [audio]",
                takes_update=True,
            )
        }

    def get_callbacks(self):
        return {"autodl": self.on_button}

    def _need_login(self) -> None:
        if not self.ctx.ig.logged_in:
            raise RuntimeError("Instagram is not connected. Connect your account with /connect.")

    async def _ig(self, fn):
        self._need_login()
        return await self.ctx.ig._call(fn)

    @staticmethod
    def _paths(result, folder: Path) -> list[Path]:
        """Normalize Path, list[Path], or directory results from instagrapi download helpers."""
        candidates = result if isinstance(result, (list, tuple, set)) else [result]
        paths: list[Path] = []
        for candidate in candidates:
            if candidate is None:
                continue
            try:
                path = Path(candidate)
            except (TypeError, ValueError):
                continue
            if path.is_dir():
                paths.extend(p for p in path.rglob("*") if p.is_file())
            elif path.is_file():
                paths.append(path)
        if not paths:
            # Folder scan is only a fallback if a helper returned no paths.
            paths = sorted((p for p in folder.rglob("*") if p.is_file()), key=lambda path: path.name.lower())
        unique: list[Path] = []
        seen: set[str] = set()
        for path in paths:
            identity = str(path.resolve())
            if identity not in seen:
                seen.add(identity)
                unique.append(path)
        # Preserve album/highlight order returned by instagrapi.
        return unique

    @staticmethod
    def _highlight_pk(cl, url: str) -> str:
        """Resolve canonical highlight URLs; follow Instagram /s/ share links if necessary."""
        try:
            return str(cl.highlight_pk_from_url(url))
        except Exception as original_error:
            if "/s/" not in urlparse(url).path:
                raise original_error
        current_url = url
        response = None
        for _ in range(4):
            parsed = urlparse(current_url)
            if parsed.scheme.lower() != "https" or (parsed.hostname or "").lower() not in _ALLOWED_HOSTS:
                raise ValueError("Instagram highlight link redirected outside instagram.com.")
            response = requests.get(
                current_url,
                timeout=15,
                allow_redirects=False,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("Location")
                if not location:
                    raise ValueError("Instagram returned an incomplete highlight redirect.")
                current_url = urljoin(current_url, location)
                continue
            response.raise_for_status()
            break
        else:
            raise ValueError("Too many redirects while resolving the Instagram highlight.")
        final_url = current_url
        if "/highlights/" not in urlparse(final_url).path:
            # Some /s/ share URLs return an HTML page instead of a direct redirect.
            match = re.search(
                r"https?://(?:www\.)?instagram\.com/stories/highlights/(\d+)/?",
                response.text if response is not None else "",
                re.I,
            )
            if match:
                final_url = match.group(0)
        if (urlparse(final_url).hostname or "").lower() not in _ALLOWED_HOSTS:
            raise ValueError("Instagram highlight link redirected outside instagram.com.")
        return str(cl.highlight_pk_from_url(final_url))

    @staticmethod
    def _track_url(cl, media) -> str:
        """Find an available original/track audio file from current Reel metadata."""
        keys = (
            "progressive_download_url",
            "fast_start_progressive_download_url",
            "reactive_audio_download_url",
        )
        clips = _value(media, "clips_metadata")
        original = _nested(clips, "original_sound_info")
        url = _find_url(original, keys)
        if url:
            return url

        url = _find_url(_nested(clips, "additional_audio_info"), keys)
        if url:
            return url
        url = _find_url(_nested(clips, "music_info"), keys)
        if url:
            return url

        canonical_id = _nested(clips, "music_canonical_id")
        if canonical_id:
            try:
                track = cl.track_info_by_canonical_id(str(canonical_id))
            except Exception:
                track = None
            url = _find_url(track, keys)
            if url:
                return url
        raise RuntimeError(
            "Instagram did not expose a downloadable audio track for this Reel. "
            "The audio may be licensed, restricted, or unavailable for download."
        )

    def _download_sync(self, url: str, category: str, folder: Path, audio: bool = False) -> dict:
        """A link you pasted is a one-off request, so skip the random pause instagrapi adds after every call
        (meant for bulk scraping). It is only a few calls, not a loop."""
        cl = self.ctx.ig.cl
        saved = cl.delay_range
        cl.delay_range = None
        try:
            return self._download_inner(url, category, folder, audio)
        finally:
            cl.delay_range = saved

    def _download_inner(self, url: str, category: str, folder: Path, audio: bool = False) -> dict:
        """Perform network operations synchronously under InstagramClient's single-call lock."""
        cl = self.ctx.ig.cl
        if category == "reel":
            pk = str(cl.media_pk_from_url(url))
            if audio:
                media = cl.media_info(pk)
                audio_url = self._track_url(cl, media)
                result = cl.track_download_by_url(audio_url, filename="original_audio", folder=folder)
                return {"paths": self._paths(result, folder), "pk": pk, "is_reel": True, "kind": "audio"}
            result = cl.clip_download(pk, folder=folder)
            return {"paths": self._paths(result, folder), "pk": pk, "is_reel": True, "kind": "reel"}

        if category == "post":
            pk = str(cl.media_pk_from_url(url))
            media = cl.media_info(pk)
            product_type = str(_value(media, "product_type") or "").lower()
            media_type = int(_value(media, "media_type") or 0)
            is_reel = product_type == "clips"
            if audio:
                if not is_reel:
                    raise ValueError("The audio option is only available for Reels.")
                audio_url = self._track_url(cl, media)
                result = cl.track_download_by_url(audio_url, filename="original_audio", folder=folder)
                return {"paths": self._paths(result, folder), "pk": pk, "is_reel": True, "kind": "audio"}
            if media_type == 8:
                result = cl.album_download(pk, folder=folder)
                label = "album"
            elif media_type == 1:
                result = cl.photo_download(pk, folder=folder)
                label = "photo"
            elif media_type == 2 and is_reel:
                result = cl.clip_download(pk, folder=folder)
                label = "reel"
            elif media_type == 2 and product_type == "igtv":
                result = cl.igtv_download(pk, folder=folder)
                label = "video"
            elif media_type == 2:
                result = cl.video_download(pk, folder=folder)
                label = "video"
            else:
                raise RuntimeError(f"Unsupported Instagram media type: {media_type}.")
            return {"paths": self._paths(result, folder), "pk": pk, "is_reel": is_reel, "kind": label}

        if audio:
            raise ValueError("The audio option is only available for Reels.")

        if category == "story":
            story_pk = str(cl.story_pk_from_url(url))
            result = cl.story_download(story_pk, folder=folder)
            return {"paths": self._paths(result, folder), "pk": story_pk, "is_reel": False, "kind": "story"}

        if category == "highlight":
            highlight_pk = self._highlight_pk(cl, url)
            highlight = cl.highlight_info(highlight_pk)
            items = list(_value(highlight, "items") or [])
            if not items:
                raise RuntimeError("That highlight has no downloadable items, or Instagram did not return its stories.")
            paths: list[Path] = []
            for index, story in enumerate(items, start=1):
                media_type = int(_value(story, "media_type") or 1)
                source_url = _value(story, "thumbnail_url") if media_type == 1 else _value(story, "video_url")
                if not source_url:
                    continue
                result = cl.story_download_by_url(str(source_url), filename=f"highlight_{index}", folder=folder)
                paths.extend(self._paths(result, folder))
            return {"paths": paths or self._paths(None, folder), "pk": highlight_pk, "is_reel": False, "kind": "highlight"}

        raise ValueError("Unsupported Instagram link category.")

    async def _send_paths(self, bot, chat_id: int, paths: list[Path], caption: str) -> int:
        if not paths:
            raise RuntimeError("Instagram returned no downloadable media files.")
        too_big = [p for p in paths if p.stat().st_size > TELEGRAM_MAX_BYTES]
        paths = [p for p in paths if p.stat().st_size <= TELEGRAM_MAX_BYTES]
        if not paths:
            mb = max(p.stat().st_size for p in too_big) / 1_000_000
            raise RuntimeError(f"The file is {mb:.0f} MB. Telegram bots can send at most 50 MB.")
        sent = 0
        # Telegram media groups accept up to 10 photo/video items per message.
        for offset in range(0, len(paths), 10):
            group = paths[offset : offset + 10]
            if len(group) == 1:
                path = group[0]
                data = path.read_bytes()
                suffix = path.suffix.lower()
                if suffix in (".m4a", ".mp3", ".aac", ".wav", ".ogg"):
                    await bot.send_audio(
                        chat_id,
                        audio=InputFile(data, filename=path.name),
                        caption=f"🎵 Original audio\n{html.escape(caption, quote=False)}",
                        parse_mode="HTML",
                    )
                else:
                    kind = "video" if suffix in (".mp4", ".mov", ".m4v", ".webm", ".mkv") else "photo"
                    await send_one(bot, chat_id, kind, data, caption if offset == 0 else "")
                sent += 1
                continue

            media = []
            for index, path in enumerate(group):
                suffix = path.suffix.lower()
                data = path.read_bytes()
                file_input = InputFile(data, filename=path.name)
                file_caption = caption if offset == 0 and index == 0 else None
                if suffix in (".mp4", ".mov", ".m4v", ".webm", ".mkv"):
                    media.append(InputMediaVideo(media=file_input, caption=file_caption, parse_mode="HTML" if file_caption else None))
                else:
                    media.append(InputMediaPhoto(media=file_input, caption=file_caption, parse_mode="HTML" if file_caption else None))
            try:
                await bot.send_media_group(chat_id, media=media)
                sent += len(group)
            except BadRequest as e:
                log.info("Could not send Instagram media group; falling back to separate files: %s", e)
                for index, path in enumerate(group):
                    suffix = path.suffix.lower()
                    data = path.read_bytes()
                    if suffix in (".mp4", ".mov", ".m4v", ".webm", ".mkv"):
                        await send_one(bot, chat_id, "video", data, caption if offset == 0 and index == 0 else "")
                    else:
                        await send_one(bot, chat_id, "photo", data, caption if offset == 0 and index == 0 else "")
                    sent += 1
        return sent

    async def _run(self, update, url: str, category: str, audio: bool = False) -> None:
        message = update.effective_message
        chat_id = update.effective_chat.id
        if chat_id in self._busy:
            await message.reply_text("Another download is still running in this chat. Please wait.")
            return
        self._busy.add(chat_id)
        status = await Progress.begin(update.get_bot(), chat_id, f"Downloading {category}" if category else "Downloading", url, "Reading link")
        try:
            with tempfile.TemporaryDirectory(prefix="hyperig-dl-") as temp:
                folder = Path(temp)
                await status.set("Fetching media from Instagram", 0.3)
                t0 = time.monotonic()
                result = await self._ig(lambda: self._download_sync(url, category, folder, audio=audio))
                t1 = time.monotonic()
                paths = result["paths"]
                if not paths:
                    raise RuntimeError("Instagram returned no downloadable media files.")
                caption = f"📥 <b>Instagram {html.escape(result['kind'].title())}</b>\n<a href=\"{html.escape(url, quote=True)}\">Open on Instagram</a>"
                total_mb = sum(p.stat().st_size for p in paths) / 1_000_000
                await status.set(f"Uploading {len(paths)} file(s), {total_mb:.1f} MB", 0.7)
                sent = await self._send_paths(update.get_bot(), chat_id, paths, caption)
                t2 = time.monotonic()
                await status.done(
                    f"{result['kind'].title()}, {sent} file(s), {total_mb:.1f} MB\n"
                    f"Instagram {t1 - t0:.1f}s, upload to Telegram {t2 - t1:.1f}s"
                )
                if result["is_reel"] and not audio and self.ctx.prefs.get("dl_audio", True):
                    markup = InlineKeyboardMarkup(
                        [[InlineKeyboardButton("Download original audio", callback_data=f"m:autodl:a:{result['pk']}")]]
                    )
                    await update.get_bot().send_message(
                        chat_id,
                        "Optional: download the Reel's available soundtrack separately.",
                        reply_markup=markup,
                    )
        except Exception as e:
            log.info("Instagram link download failed (%s)", type(e).__name__)
            await status.fail(self._explain(e))
        finally:
            self._busy.discard(chat_id)

    async def _download_audio_by_pk(self, bot, chat_id: int, media_pk: str) -> None:
        if chat_id in self._busy:
            await bot.send_message(chat_id, "Another download is still running in this chat. Please wait.")
            return
        self._busy.add(chat_id)
        status = await Progress.begin(bot, chat_id, "Downloading audio", f"Reel {media_pk}", "Fetching audio")
        try:
            with tempfile.TemporaryDirectory(prefix="hyperig-audio-") as temp:
                folder = Path(temp)

                def download():
                    cl = self.ctx.ig.cl
                    saved, cl.delay_range = cl.delay_range, None
                    try:
                        media = cl.media_info(media_pk)
                        audio_url = self._track_url(cl, media)
                        return self._paths(cl.track_download_by_url(audio_url, filename="original_audio", folder=folder), folder)
                    finally:
                        cl.delay_range = saved

                paths = await self._ig(download)
                if not paths:
                    raise RuntimeError("Instagram returned no audio file.")
                sent = await self._send_paths(bot, chat_id, paths, f"🎵 Audio from Reel {media_pk}")
                await status.done(f"{sent} audio file(s)")
        except Exception as e:
            await status.fail(self._explain(e))
        finally:
            self._busy.discard(chat_id)

    def _explain(self, error: Exception) -> str:
        if isinstance(error, (RuntimeError, ValueError, PermissionError)):
            return f"{error}"
        try:
            from instagrapi import exceptions as ig_exc
            if isinstance(error, (ig_exc.UserNotFound, ig_exc.ClientNotFoundError)):
                return "That Instagram item or account was not found."
            if isinstance(error, ig_exc.PrivateAccount):
                return "🔒 This account or item is private or is not accessible to the logged-in account."
            info = describe_error(error, self.ctx.ig.cl)
            return f"{info.text}"
        except Exception:
            return f"{type(error).__name__}: {error}"

    async def cmd_dl(self, args, update):
        if not args:
            await update.effective_message.reply_text("Usage: /dl <Instagram URL> [audio]")
            return
        classified = classify_instagram_url(args[0])
        if not classified:
            await update.effective_message.reply_text(
                "Unsupported link. Send an Instagram Reel, post, Story, Highlight, or profile URL."
            )
            return
        category, url = classified
        if category == "profile":
            manager = getattr(self.ctx, "modules", None)
            found = manager.all_commands().get("dump") if manager else None
            username = urlparse(url).path.strip("/").split("/", 1)[0]
            if found and _USERNAME_RE.fullmatch(username):
                command, _module = found
                await command.handler([username], update)
            else:
                await update.effective_message.reply_text(f"Use /dump {username or url} to open the profile card.")
            return
        audio = len(args) > 1 and args[1].lower() in {"audio", "track", "sound"}
        await self._run(update, url, category, audio=audio)

    async def on_button(self, rest: str, query, context) -> None:
        action, _, argument = rest.partition(":")
        if action != "a" or not argument.isdigit():
            return
        await query.answer()
        await self._download_audio_by_pk(context.bot, query.message.chat_id, argument)
