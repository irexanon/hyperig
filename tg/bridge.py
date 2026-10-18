"""Telegram side. One forum topic per Instagram conversation.

Instagram -> Telegram: send_to_telegram() posts into that conversation's topic
                       (creating it on first message).
Telegram -> Instagram: text you type inside a topic is sent to that conversation.

Security: only TELEGRAM_ADMIN_ID, only inside TELEGRAM_CHAT_ID (or a private chat
with the bot for commands), can do anything. Everyone else is ignored.

NOTE: this package is called `tg`, not `telegram`, because a local folder named
`telegram` would shadow the python-telegram-bot library.
"""
import asyncio
import tempfile
from collections import defaultdict
from pathlib import Path

import requests
from telegram import BotCommand, InputFile, Update
from telegram.error import BadRequest, RetryAfter, TelegramError
from telegram.ext import Application, CommandHandler, MessageHandler, filters

from utils import media as media_utils
from utils.logger import current_user_var, get_logger

log = get_logger("telegram")


def _topic_gone(err: Exception) -> bool:
    s = str(err).lower()
    return "thread not found" in s or "topic_deleted" in s or "topic deleted" in s


class _TokenHub:
    """Shares a single python-telegram-bot Application polling loop across multiple users who use the same bot token."""

    _hubs: dict[str, "_TokenHub"] = {}
    _lock = asyncio.Lock()

    def __init__(self, token: str):
        self.token = token
        self.app = Application.builder().token(token).build()
        self.bot = self.app.bot
        self.running = False
        self.error = ""
        self.bot_username = ""
        self.bridges: set["TelegramBridge"] = set()
        self._commands_published = False

    @classmethod
    async def acquire(cls, token: str, bridge: "TelegramBridge", module_manager) -> "_TokenHub":
        async with cls._lock:
            hub = cls._hubs.get(token)
            if hub is None:
                hub = _TokenHub(token)
                cls._hubs[token] = hub
            hub.bridges.add(bridge)
            if not hub.running:
                await hub._start(module_manager)
            return hub

    async def release(self, bridge: "TelegramBridge") -> None:
        async with self._lock:
            self.bridges.discard(bridge)
            if not self.bridges and self.running:
                await self._stop()
                self._hubs.pop(self.token, None)

    async def _start(self, module_manager) -> None:
        self.app.add_handler(MessageHandler(filters.ALL, self._on_update))
        self.app.add_error_handler(self._on_error)
        await self.app.initialize()
        me = await self.bot.get_me()
        self.bot_username = getattr(me, "username", "") or ""
        if not self._commands_published and module_manager:
            try:
                await self.bot.set_my_commands(
                    [BotCommand(n, c.description[:250]) for n, (c, _m) in module_manager.all_commands().items()]
                )
                self._commands_published = True
            except TelegramError:
                log.warning("Could not publish the command menu")
        await self.app.start()
        await self.app.updater.start_polling(drop_pending_updates=True, allowed_updates=[Update.MESSAGE])
        self.running = True
        self.error = ""

    async def _stop(self) -> None:
        self.running = False
        for step in (self.app.updater.stop, self.app.stop, self.app.shutdown):
            try:
                await step()
            except Exception:
                pass

    def _match_bridge(self, update: Update) -> "TelegramBridge | None":
        chat = update.effective_chat
        user = update.effective_user
        message = update.effective_message
        if not chat:
            return None
        matching_chat = [b for b in self.bridges if b.chat_id and chat.id == b.chat_id]
        if matching_chat:
            topic_id = getattr(message, "message_thread_id", None) if message else None
            if topic_id is not None:
                for b in matching_chat:
                    if topic_id in b.threads:
                        if b.cfg.tg_admin_id and user and user.id != b.cfg.tg_admin_id:
                            return None
                        return b
            for b in matching_chat:
                if b.ig and getattr(b.ig, "logged_in", False):
                    if b.cfg.tg_admin_id and user and user.id != b.cfg.tg_admin_id:
                        return None
                    return b
            b = matching_chat[0]
            if b.cfg.tg_admin_id and user and user.id != b.cfg.tg_admin_id:
                return None
            return b
        if chat.type == "private" and user:
            for b in self.bridges:
                if b.cfg.tg_admin_id and user.id == b.cfg.tg_admin_id:
                    return b
            if len(self.bridges) == 1:
                only = next(iter(self.bridges))
                if not only.cfg.tg_admin_id:
                    return only
        return None

    async def _on_update(self, update: Update, context) -> None:
        message = update.effective_message
        if not message:
            return
        bridge = self._match_bridge(update)
        if not bridge:
            return
        tok = current_user_var.set(bridge.username)
        try:
            text = (message.text or "").strip()
            if text.startswith("/"):
                parts = text.split()
                cmd_name = parts[0][1:].split("@")[0].lower()
                args = parts[1:]
                await bridge._handle_command(cmd_name, args, update)
                return
            if update.effective_chat.id == bridge.chat_id and getattr(message, "is_topic_message", False):
                if message.text and not text.startswith("/"):
                    await bridge._on_text(update, context)
                else:
                    await bridge._on_media(update, context)
        finally:
            current_user_var.reset(tok)

    async def _on_error(self, update, context) -> None:
        log.warning("Telegram handler error: %s", context.error)


class TelegramBridge:
    def __init__(self, cfg, store, username: str = ""):
        self.cfg = cfg
        self.store = store
        self.username = username or cfg.ig_username
        self.chat_id = cfg.tg_chat_id
        self.hub: _TokenHub | None = None
        self.bot = None
        self.running = False
        self.bot_username = ""
        self.error: str = ""
        self.ig = None
        self.module_manager = None
        self.topics: dict[str, int] = {}   # instagram thread id -> telegram topic id
        self.threads: dict[int, str] = {}  # telegram topic id -> instagram thread id
        self.forwarded = 0
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    # ---------------------------------------------------------------- lifecycle
    async def start(self, ig, module_manager) -> None:
        self.ig = ig
        self.module_manager = module_manager
        self.chat_id = self.cfg.tg_chat_id
        saved = await self.store.get("topics", {}) or {}
        self.topics = {k: int(v) for k, v in saved.items()}
        self.threads = {v: k for k, v in self.topics.items()}

        if not self.cfg.tg_token or not self.cfg.tg_chat_id:
            self.running = False
            self.error = "Enter your Telegram Forum Group Chat ID to connect Telegram"
            log.info("Telegram bridge waiting for Chat ID configuration.")
            return

        try:
            self.hub = await _TokenHub.acquire(self.cfg.tg_token, self, module_manager)
            self.bot = self.hub.bot
            self.bot_username = self.hub.bot_username
            await self._check_chat()
            self.running = True
            self.error = ""
            log.info("Telegram bridge connected to chat %s (%d linked topics)", self.chat_id, len(self.topics))
        except Exception as e:
            self.running = False
            self.error = f"{type(e).__name__}: {e}"
            log.warning("Could not start Telegram bridge: %s", self.error)

    async def reconfigure(self, new_cfg, ig, module_manager) -> None:
        old_token = self.cfg.tg_token
        old_chat = self.cfg.tg_chat_id
        self.cfg = new_cfg
        self.chat_id = new_cfg.tg_chat_id
        if self.hub and (old_token != new_cfg.tg_token or not new_cfg.tg_chat_id):
            await self.hub.release(self)
            self.hub = None
            self.bot = None
            self.running = False
        if new_cfg.tg_token and new_cfg.tg_chat_id:
            if not self.running or old_token != new_cfg.tg_token or old_chat != new_cfg.tg_chat_id:
                await self.start(ig, module_manager)
        else:
            self.running = False
            self.error = "Enter your Telegram Forum Group Chat ID to connect Telegram"

    async def stop(self) -> None:
        self.running = False
        if self.hub:
            await self.hub.release(self)
            self.hub = None
            self.bot = None

    async def _check_chat(self) -> None:
        if not self.bot:
            return
        try:
            chat = await self.bot.get_chat(self.chat_id)
            if not getattr(chat, "is_forum", False):
                log.warning("The group %s has Topics disabled. Enable Topics in the group settings.", self.chat_id)
            me = await self.bot.get_me()
            member = await self.bot.get_chat_member(self.chat_id, me.id)
            if member.status != "administrator" or not getattr(member, "can_manage_topics", False):
                log.warning("Make the bot (@%s) an admin with the 'Manage Topics' permission in chat %s.", me.username, self.chat_id)
        except TelegramError as e:
            log.warning("Could not verify the Telegram group %s (%s). Make sure the bot is added to the group.", self.chat_id, e)

    async def alert(self, text: str) -> None:
        if not self.bot or not self.running or not self.chat_id:
            log.info("Alert (Telegram not active): %s", text)
            return
        try:
            await self.bot.send_message(self.chat_id, text)
        except TelegramError:
            log.warning("Could not send alert to Telegram chat %s", self.chat_id)

    # ------------------------------------------------------------------- topics
    async def _save_topics(self) -> None:
        await self.store.set("topics", self.topics)

    async def _ensure_topic(self, msg) -> int:
        async with self._locks[msg.thread_id]:
            if msg.thread_id in self.topics:
                return self.topics[msg.thread_id]
            name = (msg.thread_name or f"chat {msg.thread_id[-6:]}")[:120]
            topic = await self.bot.create_forum_topic(self.chat_id, name)
            self.topics[msg.thread_id] = topic.message_thread_id
            self.threads[topic.message_thread_id] = msg.thread_id
            await self._save_topics()
            log.info("Created topic %r (%s) for Instagram thread %s", name, topic.message_thread_id, msg.thread_id)
            return topic.message_thread_id

    async def _forget_topic(self, thread_id: str) -> None:
        topic_id = self.topics.pop(thread_id, None)
        if topic_id is not None:
            self.threads.pop(topic_id, None)
        await self._save_topics()

    # --------------------------------------------------- Instagram -> Telegram
    async def _deliver(self, msg, send) -> None:
        """Run send(topic_id). Recreates a deleted topic and honours Telegram flood limits."""
        for _ in range(3):
            topic_id = await self._ensure_topic(msg)
            try:
                await send(topic_id)
                return
            except RetryAfter as e:
                wait = e.retry_after.total_seconds() if hasattr(e.retry_after, "total_seconds") else float(e.retry_after)
                log.warning("Telegram flood limit, waiting %ss", wait)
                await asyncio.sleep(wait + 1)
            except BadRequest as e:
                if _topic_gone(e):
                    log.warning("Topic %s was deleted, recreating it", topic_id)
                    await self._forget_topic(msg.thread_id)
                    continue
                raise
        raise RuntimeError("Telegram delivery failed after 3 attempts")

    async def typing(self, thread_id: str) -> None:
        """Instagram says the person is typing: show it in the topic."""
        if not self.bot or not self.running:
            return
        topic_id = self.topics.get(thread_id)
        if topic_id is None:
            return
        try:
            await self.bot.send_chat_action(self.chat_id, "typing", message_thread_id=topic_id)
        except TelegramError:
            pass

    async def _fetch(self, url: str) -> bytes:
        limit = self.cfg.max_media_mb * 1_000_000
        return await asyncio.to_thread(media_utils.download, url, limit, self.cfg.ig_proxy, self.cfg.ig_tls_verify)

    async def send_to_telegram(self, msg) -> None:
        if not self.bot or not self.running:
            raise RuntimeError("Telegram bridge is not connected")
        prefix = f"@{msg.sender_username}: " if msg.is_group else ""
        caption = (prefix + msg.text).strip()
        link_text = f"{prefix}[{msg.kind}] {msg.media_url} {msg.text}".strip() if msg.media_url else None

        data = None
        if msg.media_url and msg.kind in ("photo", "video", "voice", "animation"):
            try:
                data = await self._fetch(msg.media_url)
            except (media_utils.MediaError, requests.RequestException) as e:
                log.info("Could not download the %s (%s), sending a link instead", msg.kind, e)

        async def send(topic_id: int) -> None:
            common = dict(chat_id=self.chat_id, message_thread_id=topic_id)
            if data is not None:
                try:
                    cap = caption[:1024] or None
                    if msg.kind == "photo":
                        await self.bot.send_photo(photo=data, caption=cap, **common)
                    elif msg.kind == "video":
                        await self.bot.send_video(
                            video=InputFile(data, filename="video.mp4"), caption=cap, supports_streaming=True, **common
                        )
                    elif msg.kind == "animation":
                        await self.bot.send_animation(animation=InputFile(data, filename="animation.mp4"), caption=cap, **common)
                    else:  # voice note: a real Telegram voice bubble
                        await self.bot.send_voice(voice=InputFile(data, filename="voice.m4a"), caption=cap, **common)
                    return
                except BadRequest as e:
                    if _topic_gone(e):
                        raise
                    log.info("Telegram refused the %s (%s), sending a link instead", msg.kind, e)
            if link_text:
                await self.bot.send_message(text=link_text, **common)
            else:
                await self.bot.send_message(text=prefix + msg.text, **common)

        await self._deliver(msg, send)
        self.forwarded += 1

    # --------------------------------------------------- Telegram -> Instagram
    async def _on_text(self, update: Update, context) -> None:
        message = update.effective_message
        ig_thread = self.threads.get(message.message_thread_id)
        if not ig_thread:
            await message.reply_text("⚠️ This topic isn't linked to an Instagram chat.")
            return
        try:
            await self.ig.send_text(ig_thread, message.text)
        except Exception as e:
            log.exception("Sending to Instagram failed")
            await message.reply_text(f"❌ Not sent: {type(e).__name__}: {e}")
            return
        try:
            await message.set_reaction("👍")
        except TelegramError:
            pass

    async def _on_media(self, update: Update, context) -> None:
        """Telegram photo / video / video note / voice / audio / GIF / sticker / file -> Instagram."""
        message = update.effective_message
        ig_thread = self.threads.get(message.message_thread_id)
        if not ig_thread:
            await message.reply_text("⚠️ This topic isn't linked to an Instagram chat.")
            return
        kind, tg_obj, ext = self._classify(message)
        if kind is None:
            await message.reply_text("⚠️ Instagram chats can only carry photos, videos and voice messages.")
            return
        limit = self.cfg.max_media_mb * 1_000_000
        if (getattr(tg_obj, "file_size", 0) or 0) > limit:
            await message.reply_text(f"❌ Too big: the limit is {self.cfg.max_media_mb} MB.")
            return
        try:
            with tempfile.TemporaryDirectory(prefix="hyperig-") as tmp:
                src = Path(tmp) / f"in{ext}"
                tg_file = await tg_obj.get_file()
                await tg_file.download_to_drive(src)
                out = await asyncio.to_thread(self._convert, kind, src, Path(tmp))
                await self.ig.send_media(ig_thread, kind, out)
            if message.caption:  # Instagram media cannot carry a caption, so it follows as text
                await self.ig.send_text(ig_thread, message.caption)
        except media_utils.MediaError as e:
            await message.reply_text(f"❌ {e}")
            return
        except BadRequest as e:
            await message.reply_text(f"❌ Telegram would not give me that file ({e}). Bots can download up to 20 MB.")
            return
        except Exception as e:
            log.exception("Sending media to Instagram failed")
            await message.reply_text(f"❌ Not sent: {type(e).__name__}: {e}")
            return
        try:
            await message.set_reaction("👍")
        except TelegramError:
            pass

    @staticmethod
    def _classify(message):
        """-> (instagram kind, telegram file object, file extension) or (None, None, None)."""
        if message.photo:
            return "photo", message.photo[-1], ".jpg"
        if message.voice:
            return "voice", message.voice, ".ogg"
        if message.audio:                      # a music file: Instagram only has voice messages
            return "voice", message.audio, ".audio"
        if message.video_note:
            return "video", message.video_note, ".mp4"
        if message.animation:                  # GIFs arrive as silent MP4s
            return "video", message.animation, ".mp4"
        if message.video:
            return "video", message.video, ".mp4"
        sticker = message.sticker
        if sticker:
            if sticker.is_animated:            # .tgs (Lottie) cannot be converted
                return None, None, None
            return ("video", sticker, ".webm") if sticker.is_video else ("photo", sticker, ".webp")
        doc = message.document
        if doc:
            mime = (doc.mime_type or "").lower()
            if mime.startswith("image/"):
                return "photo", doc, ".img"
            if mime.startswith("video/"):
                return "video", doc, ".vid"
            if mime.startswith("audio/"):
                return "voice", doc, ".audio"
        return None, None, None

    @staticmethod
    def _convert(kind: str, src: Path, tmp: Path) -> Path:
        if kind == "photo":
            return media_utils.to_ig_photo(src, tmp / "out.jpg")
        if kind == "video":
            return media_utils.to_ig_video(src, tmp / "out.mp4")
        return media_utils.to_ig_voice(src, tmp / "out.m4a")

    async def _handle_command(self, cmd_name: str, args: list[str], update: Update) -> None:
        if not self.module_manager:
            return
        found = self.module_manager.all_commands().get(cmd_name)
        if not found:
            return
        command, _mod = found
        try:
            reply = await command.handler(args)
        except Exception as e:
            log.warning("Command /%s failed: %s", cmd_name, e)
            reply = f"❌ {type(e).__name__}: {e}"
        message = update.effective_message
        await message.reply_text(reply)
        if command.sensitive:  # e.g. /code 123456: don't leave it in the chat history
            try:
                await message.delete()
            except TelegramError:
                log.debug("Could not delete the sensitive message (give the bot 'Delete messages')")
