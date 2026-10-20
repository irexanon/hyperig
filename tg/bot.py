"""Fetch IG: a Telegram bot where every user connects their own Instagram account.

Each Telegram user gets a private session (own Instagram client, own saved login, own settings, own watchlist),
so nobody ever acts through someone else's account. Only private chats are served.
"""
import asyncio
import re

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters)

from core.context import AppContext, Tg
from core.instagram import InstagramClient
from core.module_manager import ModuleManager
from utils.db import ScopedStore
from utils.logger import get_logger
from utils.prefs import PACES, Prefs

log = get_logger("bot")

LINK_RE = re.compile(r"https?://(?:www\.)?(?:instagram\.com|instagr\.am)/\S+", re.I)
USERS_KEY = "users"
WIPE_KEYS = ("session", "session_meta", "prefs", "watchlist")

INTRO = (
    "Fetch IG\n\n"
    "Download from Instagram links, look at public profiles and watch accounts for new posts, "
    "all through your own Instagram account.\n\n"
    "Send /connect to link your account. Then paste an Instagram link, or open /help."
)


def kb(*rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([list(r) for r in rows])


def btn(label: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(label, callback_data=data)


class UserSession:
    """Everything that belongs to one Telegram user."""

    def __init__(self, bot_app: "FetchIGBot", user_id: int):
        self.user_id = user_id
        cfg = bot_app.cfg
        self.store = ScopedStore(bot_app.root_store, f"u{user_id}_")
        self.ig = InstagramClient(cfg, self.store)
        self.prefs = Prefs(self.store)
        tg = Tg(bot_app.app.bot, user_id, cfg.max_media_mb * 1_000_000, cfg.ig_proxy, cfg.ig_tls_verify)
        self.ctx = AppContext(cfg=cfg, ig=self.ig, store=self.store, tg=tg, prefs=self.prefs)
        self.modules = ModuleManager(self.ctx)
        self.ctx.modules = self.modules
        self.modules.load_modules()
        self.awaiting: dict | None = None   # connect flow: {"step": "cookie" | "username" | "password", ...}
        self.started = False
        self.login_task: asyncio.Task | None = None
        self.ig.on_alert = self._tell

    async def _tell(self, text: str) -> None:
        try:
            await self.ctx.tg.bot.send_message(self.user_id, text)
        except TelegramError:
            log.info("Could not message user %s", self.user_id)

    async def start(self) -> None:
        if not self.started:
            self.started = True
            await self.modules.start_all()

    async def stop(self) -> None:
        if self.started:
            self.started = False
            await self.modules.stop_all()
        await self.ig.stop()


class FetchIGBot:
    def __init__(self, cfg, root_store):
        self.cfg = cfg
        self.root_store = root_store
        self.sessions: dict[int, UserSession] = {}
        self.app: Application | None = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ lifecycle
    def build(self) -> Application:
        self.app = (
            Application.builder().token(self.cfg.tg_token).concurrent_updates(True)
            .post_init(self._post_init).post_shutdown(self._post_shutdown).build()
        )
        self.app.add_handler(CallbackQueryHandler(self.on_callback))
        self.app.add_handler(MessageHandler(filters.COMMAND, self.on_command))
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_text))
        return self.app

    async def _post_init(self, app: Application) -> None:
        await app.bot.set_my_commands([
            BotCommand("start", "Start Fetch IG "),
            BotCommand("status", "Status"),
            BotCommand("dl", "Download an Instagram link"),
            BotCommand("dump", "Profile tools"),
            BotCommand("watch", "Watch an account"),
            BotCommand("watchlist", "Manage watched accounts"),
            BotCommand("settings", "Settings"),
            BotCommand("help", "All commands"),
            BotCommand("connect", "Connect your Instagram account"),
            BotCommand("logout", "Disconnect and delete your data"),
        ])
        for uid in await self.root_store.get(USERS_KEY, []) or []:
            try:
                sess = await self.session(int(uid))
                if await sess.ig.restore_saved_session():
                    await sess.start()
                    log.info("Restored the session of user %s", uid)
            except Exception:
                log.exception("Could not restore user %s", uid)

    async def _post_shutdown(self, app: Application) -> None:
        for sess in list(self.sessions.values()):
            try:
                await sess.stop()
            except Exception:
                log.exception("Error stopping a session")

    async def session(self, user_id: int) -> UserSession:
        async with self._lock:
            sess = self.sessions.get(user_id)
            if sess is None:
                sess = UserSession(self, user_id)
                await sess.prefs.load()
                self.sessions[user_id] = sess
                users = await self.root_store.get(USERS_KEY, []) or []
                if user_id not in users:
                    await self.root_store.set(USERS_KEY, users + [user_id])
            return sess

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _private(update: Update) -> bool:
        chat = update.effective_chat
        return bool(chat and chat.type == "private" and update.effective_user)

    async def _forget_message(self, update: Update) -> None:
        """Remove the user's own message when it held a password or cookie."""
        try:
            await update.effective_message.delete()
        except TelegramError:
            pass

    # ------------------------------------------------------------------ commands
    async def on_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._private(update):
            return
        message = update.effective_message
        head, *args = (message.text or "").split()
        name = head[1:].split("@")[0].lower()
        sess = await self.session(update.effective_user.id)

        if name == "start":
            await message.reply_text(INTRO)
        elif name == "connect":
            await self.connect_menu(sess, message)
        elif name == "logout":
            await message.reply_text(
                "Disconnect Instagram and delete your saved login, settings and watchlist?",
                reply_markup=kb([btn("Yes, delete", "lo:yes"), btn("Cancel", "lo:no")]),
            )
        elif name == "settings":
            text, markup = self.settings_view(sess)
            await message.reply_text(text, reply_markup=markup)
        else:
            found = sess.modules.all_commands().get(name)
            if not found:
                return
            command, module = found
            if not sess.modules.is_enabled(module):
                await message.reply_text("That feature is switched off in /settings.")
                return
            try:
                if command.takes_update:
                    await command.handler(args, update)
                else:
                    reply = await command.handler(args)
                    if reply:
                        await message.reply_text(reply)
            except Exception:
                log.exception("Command /%s failed", name)
                await message.reply_text("Something went wrong. Try again in a moment.")

    # ------------------------------------------------------------------ plain text
    async def on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._private(update):
            return
        text = (update.effective_message.text or "").strip()
        sess = await self.session(update.effective_user.id)

        if sess.ig.auth.prompter.waiting:                      # a login is waiting for its verification code
            if sess.ig.auth.prompter.submit(text):
                await self._forget_message(update)
            return
        if sess.awaiting:
            await self.connect_step(sess, update, text)
            return
        if await sess.modules.handle_text(text, update):
            return
        match = LINK_RE.search(text)
        if match:
            found = sess.modules.all_commands().get("dl")
            if found and sess.modules.is_enabled(found[1]):
                await found[0].handler([match.group(0).rstrip(".,)")], update)

    # ------------------------------------------------------------------ connecting
    async def connect_menu(self, sess: UserSession, message) -> None:
        if sess.login_task and not sess.login_task.done():
            await message.reply_text("A login is already in progress.")
            return
        head = f"Connected as @{sess.ig.username}. Connecting again replaces it.\n\n" if sess.ig.logged_in else ""
        await message.reply_text(
            head + "How do you want to connect?\n\n"
            "Cookie (recommended): paste the sessionid cookie from instagram.com in a browser where you are logged in. "
            "It avoids new-login checks.\n"
            "Password: your username and password. The password is used once and never saved.",
            reply_markup=kb([btn("Cookie", "cn:cookie"), btn("Password", "cn:pass"), btn("Cancel", "cn:no")]),
        )

    async def connect_step(self, sess: UserSession, update: Update, text: str) -> None:
        message, aw = update.effective_message, sess.awaiting
        if text.lower() in ("cancel", "/cancel"):
            sess.awaiting = None
            await message.reply_text("Cancelled.")
            return
        step = aw["step"]
        if step == "cookie":
            sess.awaiting = None
            await self._forget_message(update)
            await self._run_login(sess, message, sess.ig.login_with_cookie(text))
        elif step == "username":
            user = text.lstrip("@").strip()
            if not re.fullmatch(r"[A-Za-z0-9._]{1,30}", user):
                await message.reply_text("That is not a valid username. Send it again, or send cancel.")
                return
            sess.awaiting = {"step": "password", "username": user}
            await message.reply_text("Now send the password. Your message is deleted right after.")
        elif step == "password":
            user = aw["username"]
            sess.awaiting = None
            await self._forget_message(update)
            await self._run_login(sess, message, sess.ig.login_with_password(user, text))

    async def _run_login(self, sess: UserSession, message, coro) -> None:
        notice = await message.reply_text("Connecting to Instagram. This can take a minute.")

        async def job():
            try:
                ok, text = await coro
            except Exception:
                log.exception("Login crashed")
                ok, text = False, "Login failed unexpectedly. Try again."
            if ok:
                await sess.start()
            try:
                await notice.edit_text(text)
            except TelegramError:
                await message.reply_text(text)

        sess.login_task = asyncio.create_task(job())

    # ------------------------------------------------------------------ settings
    def settings_view(self, sess: UserSession) -> tuple[str, InlineKeyboardMarkup]:
        ig, prefs = sess.ig, sess.prefs
        pace = prefs.get("pace")
        audio = prefs.get("dl_audio")
        lines = [
            "Settings", "",
            f"Account: {'@' + ig.username if ig.logged_in else 'not connected'}",
            f"Reel audio button: {'on' if audio else 'off'} (adds a button under downloaded Reels)",
            f"Watcher pace: {pace} (safe checks less often, fast more often)",
            "Downloads always send the largest file Telegram allows (50 MB).",
        ]
        rows = [
            [btn("Watchlist", "m:watch:ls:-"), btn("Features", "nav:features")],
            [btn(f"Audio button: {'on' if audio else 'off'}", "set:audio"), btn(f"Pace: {pace}", "set:pace")],
            [btn("Account", "nav:account")],
        ]
        return "\n".join(lines), kb(*rows)

    def features_view(self, sess: UserSession) -> tuple[str, InlineKeyboardMarkup]:
        rows, lines = [], ["Features", "", "Switch a feature off to hide its commands."]
        for m in sess.modules.modules:
            if getattr(m, "toggleable", False):
                on = sess.modules.is_enabled(m)
                lines.append(f"{m.label}: {'on' if on else 'off'}")
                rows.append([btn(f"{m.name}: {'on' if on else 'off'}", f"ft:{m.name}")])
        rows.append([btn("Back", "nav:settings")])
        return "\n".join(lines), kb(*rows)

    def account_view(self, sess: UserSession) -> tuple[str, InlineKeyboardMarkup]:
        ig = sess.ig
        if ig.logged_in:
            text = f"Account\n\nConnected as @{ig.username} ({ig.auth.login_method})."
        else:
            text = "Account\n\nNot connected."
        return text, kb([btn("Connect", "nav:connect"), btn("Disconnect", "nav:logout")], [btn("Back", "nav:settings")])

    async def _show(self, query, text: str, markup) -> None:
        try:
            await query.edit_message_text(text, reply_markup=markup)
        except TelegramError:
            pass

    # ------------------------------------------------------------------ buttons
    async def on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not query or not query.message or query.message.chat.type != "private":
            return
        sess = await self.session(query.from_user.id)
        data = query.data or ""
        message = query.message

        if data.startswith("m:"):
            _, prefix, rest = (data.split(":", 2) + ["", ""])[:3]
            found = sess.modules.all_callbacks().get(prefix)
            if found and sess.modules.is_enabled(found[1]):
                await found[0](rest, query, context)
            else:
                await query.answer()
            return

        await query.answer()
        if data == "nav:settings":
            await self._show(query, *self.settings_view(sess))
        elif data == "nav:features":
            await self._show(query, *self.features_view(sess))
        elif data == "nav:account":
            await self._show(query, *self.account_view(sess))
        elif data == "nav:connect":
            await self.connect_menu(sess, message)
        elif data == "nav:logout" or data == "lo:ask":
            await self._show(query, "Disconnect Instagram and delete your saved login, settings and watchlist?",
                             kb([btn("Yes, delete", "lo:yes"), btn("Cancel", "lo:no")]))
        elif data == "lo:no":
            await self._show(query, "Cancelled.", None)
        elif data == "lo:yes":
            await self.wipe(sess)
            await self._show(query, "Disconnected. Your login, settings and watchlist were deleted. Send /connect to start again.", None)
        elif data == "set:audio":
            await sess.prefs.set("dl_audio", not sess.prefs.get("dl_audio"))
            await self._show(query, *self.settings_view(sess))
        elif data == "set:pace":
            order = list(PACES)
            nxt = order[(order.index(sess.prefs.get("pace")) + 1) % len(order)] if sess.prefs.get("pace") in order else order[0]
            await sess.prefs.set("pace", nxt)
            await self._show(query, *self.settings_view(sess))
        elif data.startswith("ft:"):
            module = sess.modules.get(data[3:])
            if module and getattr(module, "toggleable", False):
                await sess.prefs.set_feature(module.name, not sess.modules.is_enabled(module))
            await self._show(query, *self.features_view(sess))
        elif data == "cn:no":
            sess.awaiting = None
            await self._show(query, "Cancelled.", None)
        elif data == "cn:cookie":
            sess.awaiting = {"step": "cookie"}
            await self._show(
                query,
                "Send the sessionid cookie value.\n\n"
                "In a browser where you are logged in to instagram.com, open the developer tools, go to "
                "Application (or Storage), Cookies, instagram.com, and copy the value of sessionid. "
                "Your message is deleted right after. Send cancel to stop.",
                None,
            )
        elif data == "cn:pass":
            sess.awaiting = {"step": "username"}
            await self._show(query, "Send your Instagram username. Send cancel to stop.", None)

    async def wipe(self, sess: UserSession) -> None:
        """Disconnect and delete everything stored for this user."""
        if sess.login_task and not sess.login_task.done():
            sess.login_task.cancel()
        sess.awaiting = None
        await sess.stop()
        await sess.ig.logout()
        for key in WIPE_KEYS:
            await sess.store.set(key, None)
        self.sessions.pop(sess.user_id, None)
        users = await self.root_store.get(USERS_KEY, []) or []
        await self.root_store.set(USERS_KEY, [u for u in users if u != sess.user_id])
