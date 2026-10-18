"""Telegram side:
1. Forum Group (chat_id starting with -100): Bridges Instagram DM conversations into forum topics
   for whichever Instagram account is currently attached to the bridge.
2. Bot Private Chat (DM): Interactive registration/login on /start, full account & bridge management,
   selecting which Instagram account to attach to the bridge, and direct command execution.

NOTE: this package is called `tg`, not `telegram`, because a local folder named
`telegram` would shadow the python-telegram-bot library.
"""
import asyncio
import io
import json
import tempfile
from collections import defaultdict
from pathlib import Path

import requests
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    Update,
)
from telegram.error import BadRequest, RetryAfter, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)

from utils import media as media_utils
from utils.db import sanitize_username
from utils.logger import add_user_log, current_user_var, get_logger

log = get_logger("telegram")


def _topic_gone(err: Exception) -> bool:
    s = str(err).lower()
    return "thread not found" in s or "topic_deleted" in s or "topic deleted" in s


class _TokenHub:
    """Manages a python-telegram-bot Application instance for a bot token.

    Handles:
    - Private DM onboarding (/start -> Register or Login to a User Workspace)
    - Private DM interactive control panel (Select/Attach IG account to bridge, Login IG via Password/Cookie,
      Toggle Realtime & FBNS, Set Group Chat ID, Download Session JSON, Run Commands)
    - Forum Group topic routing for the attached Instagram account.
    """

    _hubs: dict[str, "_TokenHub"] = {}
    _lock = asyncio.Lock()
    _manager = None

    def __init__(self, token: str):
        self.token = token
        self.app = Application.builder().token(token).build()
        self.bot = self.app.bot
        self.running = False
        self.error = ""
        self.bot_username = ""
        self.bridges: set["TelegramBridge"] = set()
        self._commands_published = False
        # Per-Telegram-user conversational state: tg_user_id -> {"step": str, ...}
        self._chat_state: dict[int, dict] = {}

    @classmethod
    def set_manager(cls, manager) -> None:
        cls._manager = manager

    @classmethod
    async def ensure_Started(cls, token: str, module_manager=None) -> "_TokenHub | None":
        if not token or not token.strip():
            return None
        async with cls._lock:
            hub = cls._hubs.get(token)
            if hub is None:
                hub = _TokenHub(token)
                cls._hubs[token] = hub
            if not hub.running:
                await hub._start(module_manager)
            return hub

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
        async with cls._lock:
            self.bridges.discard(bridge)

    async def _start(self, module_manager=None) -> None:
        self.app.add_handler(CallbackQueryHandler(self._on_callback))
        self.app.add_handler(MessageHandler(filters.ALL, self._on_update))
        self.app.add_error_handler(self._on_error)
        await self.app.initialize()
        me = await self.bot.get_me()
        self.bot_username = getattr(me, "username", "") or ""
        if not self._commands_published:
            try:
                base_cmds = [
                    BotCommand("start", "Open Bot Control Panel or Register/Login"),
                    BotCommand("panel", "Manage Instagram accounts & Bridge settings"),
                    BotCommand("status", "Show bridge, Realtime MQTT & FBNS status"),
                    BotCommand("session", "Show active Instagram device & session info"),
                    BotCommand("relogin", "Refresh Instagram login keeping device UUID"),
                    BotCommand("cookie", "Log in with browser sessionid cookie"),
                    BotCommand("code", "Submit 2FA or challenge verification code"),
                    BotCommand("logout", "Log out active Instagram account"),
                    BotCommand("link", "Link current Forum Group as bridge destination"),
                    BotCommand("help", "Show all available commands"),
                ]
                await self.bot.set_my_commands(base_cmds)
                self._commands_published = True
            except TelegramError:
                log.warning("Could not publish the command menu")
        await self.app.start()
        await self.app.updater.start_polling(
            drop_pending_updates=True,
            allowed_updates=[Update.MESSAGE, Update.CALLBACK_QUERY],
        )
        self.running = True
        self.error = ""
        log.info("Telegram Bot @%s is online and listening for /start", self.bot_username)

    async def _stop(self) -> None:
        self.running = False
        for step in (self.app.updater.stop, self.app.stop, self.app.shutdown):
            try:
                await step()
            except Exception:
                pass

    # ------------------------------------------------- workspace & bridge lookup
    def _find_workspace_for_user(self, tg_user_id: int):
        mgr = self._manager
        if not mgr:
            return None
        for ws in mgr.workspaces.values():
            if ws.tg_admin_id and ws.tg_admin_id == tg_user_id:
                return ws
        return None

    def _match_group_bridge(self, chat_id: int, topic_id: int | None, tg_user_id: int | None) -> "TelegramBridge | None":
        # Match only bridges that are actively attached for this chat_id
        matching = [
            b for b in self.bridges
            if b.chat_id and b.chat_id == chat_id and b.attached
        ]
        if not matching:
            return None
        if topic_id is not None:
            for b in matching:
                if topic_id in b.threads:
                    if b.cfg.tg_admin_id and tg_user_id and tg_user_id != b.cfg.tg_admin_id:
                        return None
                    return b
        for b in matching:
            if b.ig and getattr(b.ig, "logged_in", False):
                if b.cfg.tg_admin_id and tg_user_id and tg_user_id != b.cfg.tg_admin_id:
                    return None
                return b
        b = matching[0]
        if b.cfg.tg_admin_id and tg_user_id and tg_user_id != b.cfg.tg_admin_id:
            return None
        return b

    # ------------------------------------------------- Private Bot UI Builders
    def _welcome_View(self) -> tuple[str, InlineKeyboardMarkup]:
        mgr = self._manager
        existing = list(mgr.workspaces.keys()) if mgr else []
        text = (
            "👋 <b>Welcome to Hyper Insta Bridge</b>\n\n"
            "Manage your Instagram accounts, Realtime MQTT/FBNS channels, session files, "
            "and Telegram Forum bridge directly from this bot.\n\n"
            "Choose an option below to get started:"
        )
        rows = [
            [
                InlineKeyboardButton("🆕 Register New Workspace", callback_data="auth:register"),
                InlineKeyboardButton("🔑 Login Existing Workspace", callback_data="auth:login"),
            ]
        ]
        if existing:
            quick_row = [
                InlineKeyboardButton(f"📂 Open '{u}'", callback_data=f"auth:open:{u}")
                for u in existing[:3]
            ]
            rows.append(quick_row)
        return text, InlineKeyboardMarkup(rows)

    def _control_panel_view(self, ws) -> tuple[str, InlineKeyboardMarkup]:
        slot = ws.get_attached_slot() or ws.get_active_slot()
        chat_str = f"<code>{ws.tg_chat_id}</code>" if ws.tg_chat_id else "<i>Not linked yet (send /link in your Forum Group)</i>"

        if slot:
            st_icon = "🟢 Logged In" if slot.ig.logged_in else "🟠 Logged Out"
            rt_str = "ON (Connected)" if slot.ig.realtime.rt_connected else ("ON" if slot.cfg.realtime else "OFF")
            fbns_str = "ON (Connected)" if slot.ig.realtime.fbns_connected else ("ON" if slot.cfg.fbns else "OFF")
            acc_summary = (
                f"📸 <b>Attached IG Account:</b> <code>@{slot.ig_username}</code>\n"
                f"🔐 <b>Auth Status:</b> {st_icon} ({slot.ig.auth.login_method})\n"
                f"⚡ <b>Realtime MQTT:</b> {rt_str} · 🔔 <b>FBNS:</b> {fbns_str}\n"
                f"💬 <b>Bridged Topics:</b> {len(slot.bridge.topics)} · <b>DMs Forwarded:</b> {slot.bridge.forwarded}"
            )
        else:
            acc_summary = "📸 <b>Instagram Accounts:</b> <i>None added yet. Click ➕ Login IG below to add one.</i>"

        text = (
            f"🎛 <b>Hyper Insta Control Center</b>\n"
            f"👤 <b>Workspace DB:</b> <code>{ws.username}</code>\n"
            f"📡 <b>Forum Group ID:</b> {chat_str}\n\n"
            f"{acc_summary}\n\n"
            f"<i>Select an Instagram account below to attach it to your Telegram bridge, or manage your session:</i>"
        )

        rows: list[list[InlineKeyboardButton]] = []
        # Account selector buttons (choose which IG account is attached to the bridge)
        if ws.slots:
            acc_btns = []
            for ig_u, s in ws.slots.items():
                is_attached = (ig_u == ws.bridged_ig)
                prefix = "✅ " if is_attached else "⚪ "
                online = "🟢" if s.ig.logged_in else "🟠"
                acc_btns.append(
                    InlineKeyboardButton(
                        f"{prefix}@{ig_u} {online}",
                        callback_data=f"ig:attach:{ig_u}",
                    )
                )
            for i in range(0, len(acc_btns), 2):
                rows.append(acc_btns[i : i + 2])

        rows.append(
            [
                InlineKeyboardButton("➕ Login IG (Password)", callback_data="ig:add_pw"),
                InlineKeyboardButton("🍪 Login IG (Cookie)", callback_data="ig:add_ck"),
            ]
        )

        if slot:
            rt_lbl = f"⚡ Realtime: {'ON' if slot.cfg.realtime else 'OFF'}"
            fb_lbl = f"🔔 FBNS: {'ON' if slot.cfg.fbns else 'OFF'}"
            rows.append(
                [
                    InlineKeyboardButton(rt_lbl, callback_data=f"ig:toggle_rt:{slot.ig_username}"),
                    InlineKeyboardButton(fb_lbl, callback_data=f"ig:toggle_fbns:{slot.ig_username}"),
                ]
            )
            rows.append(
                [
                    InlineKeyboardButton("📊 Status", callback_data="cmd:status"),
                    InlineKeyboardButton("📱 Device Info", callback_data="cmd:session"),
                    InlineKeyboardButton("📥 Session .json", callback_data=f"ig:dl:{slot.ig_username}"),
                ]
            )
            rows.append(
                [
                    InlineKeyboardButton("🔄 Relogin", callback_data="cmd:relogin"),
                    InlineKeyboardButton("💬 Set Group ID", callback_data="ws:set_group"),
                    InlineKeyboardButton("🚪 Logout IG", callback_data=f"ig:logout:{slot.ig_username}"),
                ]
            )
        else:
            rows.append(
                [
                    InlineKeyboardButton("💬 Set Group Chat ID", callback_data="ws:set_group"),
                ]
            )

        rows.append(
            [
                InlineKeyboardButton("🔀 Switch Workspace DB", callback_data="auth:switch"),
                InlineKeyboardButton("🔄 Refresh Panel", callback_data="panel:refresh"),
            ]
        )
        return text, InlineKeyboardMarkup(rows)

    # ------------------------------------------------- Callback Query Handler
    async def _on_callback(self, update: Update, context) -> None:
        query = update.callback_query
        if not query or not query.from_user:
            return
        await query.answer()
        user_id = query.from_user.id
        data = query.data or ""
        mgr = self._manager
        if not mgr:
            return

        if data in ("auth:register", "auth:login", "auth:switch"):
            mode = "register" if data == "auth:register" else "login"
            self._chat_state[user_id] = {"step": "await_workspace_username", "mode": mode}
            prompt = (
                "🆕 <b>Create / Register Workspace</b>\n\n"
                "Reply with the <b>username</b> you want to use for your database (e.g. <code>arshman</code>):"
                if mode == "register"
                else "🔑 <b>Login to Workspace</b>\n\nReply with your existing <b>workspace username</b> (e.g. <code>rex</code>):"
            )
            await query.message.reply_text(prompt, parse_mode="HTML")
            return

        if data.startswith("auth:open:"):
            uname = sanitize_username(data.split(":", 2)[2])
            if uname:
                ws = await mgr.get_or_create(uname)
                await ws.update_telegram({"tg_admin_id": str(user_id)})
                self._chat_state.pop(user_id, None)
                text, markup = self._control_panel_view(ws)
                await query.message.reply_text(text, parse_mode="HTML", reply_markup=markup)
            return

        ws = self._find_workspace_for_user(user_id)
        if not ws:
            text, markup = self._welcome_View()
            await query.message.reply_text(text, parse_mode="HTML", reply_markup=markup)
            return

        tok = current_user_var.set(ws.username)
        try:
            if data == "panel:refresh":
                text, markup = self._control_panel_view(ws)
                try:
                    await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
                except TelegramError:
                    await query.message.reply_text(text, parse_mode="HTML", reply_markup=markup)
                return

            if data.startswith("ig:attach:"):
                ig_u = sanitize_username(data.split(":", 2)[2])
                await ws.attach_account(ig_u)
                text, markup = self._control_panel_view(ws)
                try:
                    await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
                except TelegramError:
                    await query.message.reply_text(text, parse_mode="HTML", reply_markup=markup)
                return

            if data == "ig:add_pw":
                self._chat_state[user_id] = {"step": "await_ig_pw"}
                await query.message.reply_text(
                    "🔐 <b>Instagram Password Login</b>\n"
                    "<i>(Realtime &amp; FBNS will be automatically enabled)</i>\n\n"
                    "Reply in this format:\n"
                    "<code>ig_username password</code>\n"
                    "or with a proxy:\n"
                    "<code>ig_username password http://user:pass@host:port</code>",
                    parse_mode="HTML",
                )
                return

            if data == "ig:add_ck":
                self._chat_state[user_id] = {"step": "await_ig_ck"}
                await query.message.reply_text(
                    "🍪 <b>Instagram Cookie Login</b>\n"
                    "<i>(Realtime &amp; FBNS will be automatically disabled)</i>\n\n"
                    "Reply in this format:\n"
                    "<code>ig_username sessionid_cookie</code>\n"
                    "or with a proxy:\n"
                    "<code>ig_username sessionid_cookie http://user:pass@host:port</code>",
                    parse_mode="HTML",
                )
                return

            if data == "ws:set_group":
                self._chat_state[user_id] = {"step": "await_group_id"}
                await query.message.reply_text(
                    "💬 <b>Link Telegram Forum Group</b>\n\n"
                    "Reply with your Forum Group's Chat ID (starts with <code>-100</code>), "
                    "OR simply add this bot as an Admin in your Forum Group and type <code>/link</code> inside the group.",
                    parse_mode="HTML",
                )
                return

            if data.startswith("ig:toggle_rt:"):
                ig_u = sanitize_username(data.split(":", 2)[2])
                slot = ws.slots.get(ig_u)
                if slot:
                    await slot.apply_config({"realtime": not slot.cfg.realtime})
                text, markup = self._control_panel_view(ws)
                try:
                    await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
                except TelegramError:
                    pass
                return

            if data.startswith("ig:toggle_fbns:"):
                ig_u = sanitize_username(data.split(":", 2)[2])
                slot = ws.slots.get(ig_u)
                if slot:
                    await slot.apply_config({"fbns": not slot.cfg.fbns})
                text, markup = self._control_panel_view(ws)
                try:
                    await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
                except TelegramError:
                    pass
                return

            if data.startswith("ig:dl:"):
                ig_u = sanitize_username(data.split(":", 2)[2])
                slot = ws.slots.get(ig_u)
                session_obj = await slot.get_session_json() if slot else None
                if not session_obj:
                    await query.message.reply_text("⚠️ No saved session found for this account yet.")
                    return
                raw_bytes = json.dumps(session_obj, indent=2, default=str).encode("utf-8")
                await query.message.reply_document(
                    document=InputFile(io.BytesIO(raw_bytes), filename=f"{ig_u}_session.json"),
                    caption=f"📦 Session JSON for @{ig_u}",
                )
                return

            if data.startswith("ig:logout:"):
                ig_u = sanitize_username(data.split(":", 2)[2])
                slot = ws.slots.get(ig_u)
                if slot:
                    await slot.logout()
                text, markup = self._control_panel_view(ws)
                try:
                    await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
                except TelegramError:
                    pass
                return

            if data.startswith("cmd:"):
                cmd_name = data.split(":", 1)[1]
                slot = ws.get_attached_slot() or ws.get_active_slot()
                if not slot:
                    await query.message.reply_text("⚠️ Add and select an Instagram account first.")
                    return
                reply = await slot.run_command(f"/{cmd_name}")
                await query.message.reply_text(reply)
                return
        finally:
            current_user_var.reset(tok)

    # ------------------------------------------------- Message & Command Handler
    async def _on_update(self, update: Update, context) -> None:
        message = update.effective_message
        chat = update.effective_chat
        user = update.effective_user
        if not message or not chat:
            return

        text = (message.text or "").strip()

        # 1. Private Chat with Bot: Registration, Login, Control Center & Direct Commands
        if chat.type == "private" and user:
            await self._handle_private_message(update, user.id, text)
            return

        # 2. Forum Group Chat: /link command or Topic Reply -> Instagram DM
        if text.startswith("/link") and user:
            ws = self._find_workspace_for_user(user.id)
            if not ws and self._manager and len(self._manager.workspaces) == 1:
                ws = next(iter(self._manager.workspaces.values()))
            if ws:
                await ws.update_telegram({"tg_chat_id": str(chat.id), "tg_admin_id": str(user.id)})
                await message.reply_text(
                    f"✅ Linked this Forum Group (<code>{chat.id}</code>) to workspace <b>{ws.username}</b>.\n"
                    f"Manage accounts and run commands directly in my private chat (@{self.bot_username}).",
                    parse_mode="HTML",
                )
            else:
                await message.reply_text(
                    f"ℹ️ Group Chat ID is <code>{chat.id}</code>.\n"
                    f"Open @{self.bot_username} in private chat and send /start first to register your workspace.",
                    parse_mode="HTML",
                )
            return

        # Ignore other slash commands in group chats (commands execute directly in the bot's private chat)
        if text.startswith("/"):
            return

        bridge = self._match_group_bridge(
            chat.id,
            getattr(message, "message_thread_id", None),
            user.id if user else None,
        )
        if not bridge:
            return

        tok = current_user_var.set(bridge.username)
        try:
            if getattr(message, "is_topic_message", False):
                if message.text:
                    await bridge._on_text(update, context)
                else:
                    await bridge._on_media(update, context)
        finally:
            current_user_var.reset(tok)

    async def _handle_private_message(self, update: Update, user_id: int, text: str) -> None:
        message = update.effective_message
        mgr = self._manager
        if not mgr:
            return

        ws = self._find_workspace_for_user(user_id)
        state = self._chat_state.get(user_id)

        # Quick /start <username> or /login <username> or /register <username>
        if text.startswith(("/start ", "/login ", "/register ")):
            arg = sanitize_username(text.split(None, 1)[1])
            if arg:
                ws = await mgr.get_or_create(arg)
                await ws.update_telegram({"tg_admin_id": str(user_id)})
                self._chat_state.pop(user_id, None)
                view_text, markup = self._control_panel_view(ws)
                await message.reply_text(view_text, parse_mode="HTML", reply_markup=markup)
                return

        # Step 1: Awaiting workspace username during Registration / Login
        if state and state.get("step") == "await_workspace_username" and not text.startswith("/"):
            uname = sanitize_username(text)
            if not uname:
                await message.reply_text("⚠️ Please send a valid username (letters, numbers, dots, underscores).")
                return
            ws = await mgr.get_or_create(uname)
            await ws.update_telegram({"tg_admin_id": str(user_id)})
            self._chat_state.pop(user_id, None)
            await message.reply_text(f"✅ Connected to workspace database <b>{ws.username}</b>!", parse_mode="HTML")
            view_text, markup = self._control_panel_view(ws)
            await message.reply_text(view_text, parse_mode="HTML", reply_markup=markup)
            return

        # If not registered/logged into a workspace yet, prompt to register or login
        if not ws:
            view_text, markup = self._welcome_View()
            await message.reply_text(view_text, parse_mode="HTML", reply_markup=markup)
            return

        tok = current_user_var.set(ws.username)
        try:
            # Check if any slot is currently waiting for a 2FA / Challenge verification code
            for s in ws.slots.values():
                if s.ig.auth.prompter.waiting and text and not text.startswith("/"):
                    code = text.strip().replace(" ", "")
                    if s.ig.auth.prompter.submit(code):
                        await message.reply_text(f"✅ Verification code submitted for @{s.ig_username}.")
                        return

            # Handle uploaded session.json document in private chat
            if message.document and (message.document.file_name or "").endswith(".json"):
                tg_file = await message.document.get_file()
                raw_bytes = await tg_file.download_as_bytearray()
                raw_json = raw_bytes.decode("utf-8", errors="replace")
                ig_u = sanitize_username(message.document.file_name.replace("_session.json", "").replace(".json", ""))
                slot = await ws.ensure_slot(ig_u or ws.bridged_ig or ws.username)
                ok, msg_txt = await slot.import_session(raw_json)
                if ok and not ws.bridged_ig:
                    await ws.attach_account(slot.ig_username)
                await message.reply_text(f"{'✅' if ok else '❌'} {msg_txt}")
                view_text, markup = self._control_panel_view(ws)
                await message.reply_text(view_text, parse_mode="HTML", reply_markup=markup)
                return

            # Conversational step handlers
            if state and not text.startswith("/"):
                step = state.get("step")
                self._chat_state.pop(user_id, None)

                if step == "await_group_id":
                    try:
                        await ws.update_telegram({"tg_chat_id": text.strip()})
                        await message.reply_text(f"✅ Telegram Forum Group Chat ID set to <code>{ws.tg_chat_id}</code>.", parse_mode="HTML")
                    except Exception as e:
                        await message.reply_text(f"❌ {e}")
                    view_text, markup = self._control_panel_view(ws)
                    await message.reply_text(view_text, parse_mode="HTML", reply_markup=markup)
                    return

                if step == "await_ig_pw":
                    parts = text.split()
                    if len(parts) < 2:
                        await message.reply_text("⚠️ Format: <code>ig_username password [proxy]</code>", parse_mode="HTML")
                        return
                    ig_u, pw = parts[0], parts[1]
                    proxy = parts[2] if len(parts) > 2 else ""
                    slot = await ws.ensure_slot(ig_u)
                    await ws.attach_account(slot.ig_username)
                    wait_msg = await message.reply_text(f"⏳ Logging in to @{slot.ig_username} (Realtime=ON, FBNS=ON)...")
                    ok, res_msg = await slot.login_with_password({"password": pw, "proxy": proxy})
                    await wait_msg.edit_text(f"{'✅' if ok else '❌'} {res_msg}")
                    view_text, markup = self._control_panel_view(ws)
                    await message.reply_text(view_text, parse_mode="HTML", reply_markup=markup)
                    return

                if step == "await_ig_ck":
                    parts = text.split()
                    if len(parts) < 2:
                        await message.reply_text("⚠️ Format: <code>ig_username sessionid_cookie [proxy]</code>", parse_mode="HTML")
                        return
                    ig_u, ck = parts[0], parts[1]
                    proxy = parts[2] if len(parts) > 2 else ""
                    slot = await ws.ensure_slot(ig_u)
                    await ws.attach_account(slot.ig_username)
                    wait_msg = await message.reply_text(f"⏳ Validating cookie for @{slot.ig_username} (Realtime=OFF, FBNS=OFF)...")
                    ok, res_msg = await slot.login_with_cookie(ck, proxy=proxy)
                    await wait_msg.edit_text(f"{'✅' if ok else '❌'} {res_msg}")
                    view_text, markup = self._control_panel_view(ws)
                    await message.reply_text(view_text, parse_mode="HTML", reply_markup=markup)
                    return

            # Slash commands in Bot Private Chat
            if text.startswith("/"):
                parts = text.split()
                cmd_name = parts[0][1:].split("@")[0].lower()
                args = parts[1:]

                if cmd_name in ("start", "panel", "menu"):
                    view_text, markup = self._control_panel_view(ws)
                    await message.reply_text(view_text, parse_mode="HTML", reply_markup=markup)
                    return

                if cmd_name == "attach" and args:
                    await ws.attach_account(args[0])
                    await message.reply_text(f"✅ Attached @{ws.bridged_ig} to the Telegram bridge.")
                    return

                slot = ws.get_attached_slot() or ws.get_active_slot()
                if not slot:
                    view_text, markup = self._control_panel_view(ws)
                    await message.reply_text(
                        "⚠️ Add an Instagram account first using the buttons below:",
                        reply_markup=markup,
                    )
                    return

                await slot.bridge._handle_command(cmd_name, args, update)
                return

            # Default: show control panel
            view_text, markup = self._control_panel_view(ws)
            await message.reply_text(view_text, parse_mode="HTML", reply_markup=markup)
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
        self.attached: bool = True
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

        if not self.cfg.tg_token:
            self.running = False
            self.error = "No Telegram Bot Token configured"
            return

        try:
            self.hub = await _TokenHub.acquire(self.cfg.tg_token, self, module_manager)
            self.bot = self.hub.bot
            self.bot_username = self.hub.bot_username
            if self.chat_id and self.attached:
                await self._check_chat()
                self.running = True
                self.error = ""
                log.info(
                    "[@%s] Telegram bridge attached to group %s (%d topics)",
                    self.cfg.ig_username,
                    self.chat_id,
                    len(self.topics),
                )
            elif not self.chat_id:
                self.running = False
                self.error = f"Send /start to @{self.bot_username} or set Group Chat ID"
            else:
                self.running = False
                self.error = "Standby (not attached to bridge)"
        except Exception as e:
            self.running = False
            self.error = f"{type(e).__name__}: {e}"
            log.warning("Could not start Telegram bridge: %s", self.error)

    async def reconfigure(self, new_cfg, ig, module_manager) -> None:
        old_token = self.cfg.tg_token
        self.cfg = new_cfg
        self.chat_id = new_cfg.tg_chat_id
        if self.hub and old_token != new_cfg.tg_token:
            await self.hub.release(self)
            self.hub = None
            self.bot = None
            self.running = False
        await self.start(ig, module_manager)

    async def stop(self) -> None:
        self.running = False
        if self.hub:
            await self.hub.release(self)
            self.hub = None
            self.bot = None

    async def _check_chat(self) -> None:
        if not self.bot or not self.chat_id:
            return
        try:
            chat = await self.bot.get_chat(self.chat_id)
            if not getattr(chat, "is_forum", False):
                log.warning("The group %s has Topics disabled. Enable Topics in the group settings.", self.chat_id)
            me = await self.bot.get_me()
            member = await self.bot.get_chat_member(self.chat_id, me.id)
            if member.status != "administrator" or not getattr(member, "can_manage_topics", False):
                log.warning("Make the bot (@%s) an admin with 'Manage Topics' permission in chat %s.", me.username, self.chat_id)
        except TelegramError as e:
            log.warning("Could not verify Telegram group %s (%s). Make sure the bot is in the group.", self.chat_id, e)

    async def alert(self, text: str) -> None:
        """Send alerts and 2FA code prompts directly to the user's Private Bot DM first, fallback to group."""
        if not self.bot:
            log.info("Alert (Telegram not active): %s", text)
            return
        target = self.cfg.tg_admin_id or self.chat_id
        if not target:
            log.info("Alert: %s", text)
            return
        try:
            await self.bot.send_message(target, f"[@{self.cfg.ig_username}] {text}")
        except TelegramError:
            if self.chat_id and target != self.chat_id:
                try:
                    await self.bot.send_message(self.chat_id, f"[@{self.cfg.ig_username}] {text}")
                except TelegramError:
                    pass

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
        if not self.bot or not self.running or not self.attached or not self.chat_id:
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
        if not self.bot or not self.running or not self.attached or not self.chat_id:
            raise RuntimeError("Telegram bridge is not attached or connected")
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
                    else:
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
            if message.caption:
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
        if message.photo:
            return "photo", message.photo[-1], ".jpg"
        if message.voice:
            return "voice", message.voice, ".ogg"
        if message.audio:
            return "voice", message.audio, ".audio"
        if message.video_note:
            return "video", message.video_note, ".mp4"
        if message.animation:
            return "video", message.animation, ".mp4"
        if message.video:
            return "video", message.video, ".mp4"
        sticker = message.sticker
        if sticker:
            if sticker.is_animated:
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
            await update.effective_message.reply_text(f"Unknown command /{cmd_name}. Send /help or /panel.")
            return
        command, _mod = found
        try:
            reply = await command.handler(args)
        except Exception as e:
            log.warning("Command /%s failed: %s", cmd_name, e)
            reply = f"❌ {type(e).__name__}: {e}"
        message = update.effective_message
        await message.reply_text(reply)
        if command.sensitive:
            try:
                await message.delete()
            except TelegramError:
                log.debug("Could not delete sensitive message")
