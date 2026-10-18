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

from core import osint_tools
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
      Toggle Realtime & FBNS, Set Group Chat ID, Download Session JSON)
    - Auto-Detect Instagram Link Downloader (Reels, Posts, Carousels, Stories, Highlights, Profile Links)
    - /dump & /profile Interactive OSINT & Profile DB Card with Manual Count / ALL selectors
    - /watch Background & On-Demand OSINT Account / Story / Post Watcher
    - Forum Group topic routing for the attached Instagram account.
    """

    _hubs: dict[str, "_TokenHub"] = {}
    _lock = asyncio.Lock()
    _manager = None

    def __init__(self, token: str):
        self.token = token
        self.app = Application.builder().token(token).concurrent_updates(True).build()
        self.bot = self.app.bot
        self.running = False
        self.error = ""
        self.bot_username = ""
        self.bridges: set["TelegramBridge"] = set()
        self._commands_published = False
        # Per-Telegram-user conversational state: tg_user_id -> {"step": str, ...}
        self._chat_state: dict[int, dict] = {}
        # Active single panel message ID per Telegram user so menus edit in-place
        self._panel_msg_id: dict[int, int] = {}

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
                    BotCommand("start", "Open main menu"),
                    BotCommand("dump", "OSINT Dump & action menu (@username)"),
                    BotCommand("profile", "Fetch profile data + DB history (@username)"),
                    BotCommand("dl", "Download any Instagram link"),
                    BotCommand("watch", "Manage OSINT & Story Watcher"),
                    BotCommand("accounts", "View & switch Instagram accounts"),
                    BotCommand("status", "Show connection status"),
                    BotCommand("dump", "Open a public profile with action buttons"),
                    BotCommand("profile", "Public profile info"),
                    BotCommand("link", "Link Telegram Forum Group"),
                    BotCommand("logout", "Sign out of workspace"),
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

    @staticmethod
    def _get_logged_in_slot(ws):
        """Return the attached or active logged-in InstagramSlot, or any logged-in slot in the workspace."""
        for candidate in (ws.get_attached_slot(), ws.get_active_slot()):
            if candidate and candidate.ig.logged_in:
                return candidate
        for s in ws.slots.values():
            if s.ig.logged_in:
                return s
        return None

    def _match_group_bridge(self, chat_id: int, topic_id: int | None, tg_user_id: int | None) -> "TelegramBridge | None":
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

    # ------------------------------------------------- In-Place Message Renderer
    async def _show_panel(
        self,
        chat_id: int,
        user_id: int,
        text: str,
        markup: InlineKeyboardMarkup | None = None,
        edit_msg=None,
        force_new: bool = False,
    ) -> None:
        """Updates the user's single active bot message in-place, or sends a new one if needed."""
        if edit_msg is not None and not force_new:
            try:
                await edit_msg.edit_text(text, parse_mode="HTML", reply_markup=markup)
                self._panel_msg_id[user_id] = edit_msg.message_id
                return
            except BadRequest as e:
                if "message is not modified" in str(e).lower():
                    self._panel_msg_id[user_id] = edit_msg.message_id
                    return
            except TelegramError:
                pass

        msg_id = self._panel_msg_id.get(user_id)
        if msg_id and not force_new:
            try:
                await self.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=msg_id,
                    text=text,
                    parse_mode="HTML",
                    reply_markup=markup,
                )
                return
            except BadRequest as e:
                if "message is not modified" in str(e).lower():
                    return
            except TelegramError:
                pass

        if msg_id:
            try:
                await self.bot.delete_message(chat_id=chat_id, message_id=msg_id)
            except TelegramError:
                pass

        sent = await self.bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode="HTML",
            reply_markup=markup,
        )
        self._panel_msg_id[user_id] = sent.message_id

    @staticmethod
    async def _try_delete(message) -> None:
        if not message:
            return
        try:
            await message.delete()
        except TelegramError:
            pass

    async def _send_downloaded_files(
        self,
        chat_id: int,
        files: list[Path],
        caption: str = "",
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> int:
        """Send a list of downloaded media files (photos/videos) to Telegram."""
        sent_count = 0
        total = len(files)
        for idx, fpath in enumerate(files):
            if not fpath or not fpath.exists():
                continue
            ext = fpath.suffix.lower()
            cap = caption if idx == 0 else f"({idx + 1}/{total})"
            rm = reply_markup if idx == total - 1 else None
            try:
                with open(fpath, "rb") as fp:
                    if ext in (".mp4", ".mov", ".m4v", ".webm"):
                        await self.bot.send_video(
                            chat_id=chat_id,
                            video=fp,
                            caption=cap[:1024] if cap else None,
                            parse_mode="HTML",
                            reply_markup=rm,
                        )
                    elif ext in (".jpg", ".jpeg", ".png", ".webp"):
                        await self.bot.send_photo(
                            chat_id=chat_id,
                            photo=fp,
                            caption=cap[:1024] if cap else None,
                            parse_mode="HTML",
                            reply_markup=rm,
                        )
                    else:
                        await self.bot.send_document(
                            chat_id=chat_id,
                            document=fp,
                            caption=cap[:1024] if cap else None,
                            parse_mode="HTML",
                            reply_markup=rm,
                        )
                sent_count += 1
            except TelegramError as e:
                log.warning("Failed sending media %s: %s", fpath.name, e)
        return sent_count

    # ------------------------------------------------- Clean Menu Views
    def _view_welcome(self, notice: str = "") -> tuple[str, InlineKeyboardMarkup]:
        head = f"{notice}\n\n" if notice else ""
        text = (
            f"{head}"
            "<b>Hyper Insta Bridge</b>\n"
            "Sign in to your workspace or create a new one."
        )
        rows = [
            [
                InlineKeyboardButton("🔑 Login", callback_data="auth:login"),
                InlineKeyboardButton("🆕 Register", callback_data="auth:register"),
            ]
        ]
        return text, InlineKeyboardMarkup(rows)

    def _view_home(self, ws, notice: str = "") -> tuple[str, InlineKeyboardMarkup]:
        slot = ws.get_attached_slot() or ws.get_active_slot()
        group_str = f"Linked (<code>{ws.tg_chat_id}</code>)" if ws.tg_chat_id else "Not linked"
        if slot:
            badge = "🟢 Online" if slot.ig.logged_in else "🟠 Offline"
            ig_str = f"<code>@{slot.ig_username}</code> ({badge})"
        else:
            ig_str = "None"

        head = f"{notice}\n\n" if notice else ""
        text = (
            f"{head}"
            f"<b>Hyper Insta Bridge</b>\n\n"
            f"👤 <b>Workspace:</b> <code>{ws.username}</code>\n"
            f"💬 <b>Group:</b> {group_str}\n"
            f"📸 <b>Bridged IG:</b> {ig_str}\n\n"
            f"<i>💡 Paste any Instagram link here to auto-download, or use Dump / Watcher below.</i>"
        )
        count = len(ws.slots)
        rows = [
            [
                InlineKeyboardButton(f"📸 Accounts ({count})", callback_data="nav:accounts"),
                InlineKeyboardButton("➕ Add IG", callback_data="nav:add_ig"),
            ],
            [
                InlineKeyboardButton("🕵️ Dump / Profile", callback_data="nav:dump"),
                InlineKeyboardButton("👁 OSINT Watcher", callback_data="nav:watcher"),
            ],
            [
                InlineKeyboardButton("📊 Status", callback_data="nav:status"),
                InlineKeyboardButton("🔗 Link Group", callback_data="nav:group"),
            ],
            [
                InlineKeyboardButton("🔄 Refresh", callback_data="nav:home"),
                InlineKeyboardButton("🚪 Sign Out", callback_data="auth:logout"),
            ],
        ]
        return text, InlineKeyboardMarkup(rows)

    def _view_accounts(self, ws, notice: str = "") -> tuple[str, InlineKeyboardMarkup]:
        head = f"{notice}\n\n" if notice else ""
        if not ws.slots:
            text = (
                f"{head}"
                "<b>Instagram Accounts</b>\n\n"
                "No Instagram accounts added yet."
            )
        else:
            text = (
                f"{head}"
                "<b>Instagram Accounts</b>\n"
                "Tap an account to manage it or attach it to the bridge:"
            )

        rows: list[list[InlineKeyboardButton]] = []
        for ig_u, s in ws.slots.items():
            is_attached = (ig_u == ws.bridged_ig)
            pin = "✅" if is_attached else "⚪"
            dot = "🟢" if s.ig.logged_in else "🟠"
            rows.append(
                [
                    InlineKeyboardButton(
                        f"{pin} @{ig_u} ({dot})",
                        callback_data=f"acct:view:{ig_u}",
                    )
                ]
            )
        rows.append(
            [
                InlineKeyboardButton("➕ Add IG", callback_data="nav:add_ig"),
                InlineKeyboardButton("⬅️ Back", callback_data="nav:home"),
            ]
        )
        return text, InlineKeyboardMarkup(rows)

    def _view_account_card(self, ws, ig_u: str, notice: str = "") -> tuple[str, InlineKeyboardMarkup]:
        slot = ws.slots.get(ig_u)
        if not slot:
            return self._view_accounts(ws, notice="⚠️ Account not found.")

        is_attached = (ig_u == ws.bridged_ig)
        status_str = "🟢 Logged In" if slot.ig.logged_in else "🟠 Logged Out"
        rt_str = "ON" if slot.cfg.realtime else "OFF"
        fbns_str = "ON" if slot.cfg.fbns else "OFF"
        head = f"{notice}\n\n" if notice else ""

        text = (
            f"{head}"
            f"<b>@{slot.ig_username}</b>\n\n"
            f"Status: {status_str} (<code>{slot.ig.auth.login_method}</code>)\n"
            f"Realtime: <b>{rt_str}</b> · FBNS: <b>{fbns_str}</b>\n"
            f"Bridged Topics: <b>{len(slot.bridge.topics)}</b>"
        )

        attach_btn = (
            InlineKeyboardButton("✅ Attached to Bridge", callback_data=f"acct:attach:{ig_u}")
            if is_attached
            else InlineKeyboardButton("📌 Attach to Bridge", callback_data=f"acct:attach:{ig_u}")
        )

        rows = [
            [attach_btn],
            [
                InlineKeyboardButton(f"⚡ Realtime: {rt_str}", callback_data=f"acct:rt:{ig_u}"),
                InlineKeyboardButton(f"🔔 FBNS: {fbns_str}", callback_data=f"acct:fbns:{ig_u}"),
            ],
            [
                InlineKeyboardButton("🔄 Relogin", callback_data=f"acct:relogin:{ig_u}"),
                InlineKeyboardButton("📥 Session .json", callback_data=f"acct:dl:{ig_u}"),
            ],
            [
                InlineKeyboardButton("🕵️ My Profile Dump", callback_data=f"dump:open:{ig_u}"),
                InlineKeyboardButton("🗑 Logout IG", callback_data=f"acct:logout:{ig_u}"),
            ],
            [
                InlineKeyboardButton("⬅️ Back", callback_data="nav:accounts"),
            ],
        ]
        return text, InlineKeyboardMarkup(rows)

    def _view_add_ig(self, notice: str = "") -> tuple[str, InlineKeyboardMarkup]:
        head = f"{notice}\n\n" if notice else ""
        text = (
            f"{head}"
            "<b>Add Instagram Account</b>\n\n"
            "Choose a login method below, or send a <code>session.json</code> file in this chat."
        )
        rows = [
            [
                InlineKeyboardButton("🔐 Password Login", callback_data="add:pw"),
                InlineKeyboardButton("🍪 Cookie Login", callback_data="add:ck"),
            ],
            [
                InlineKeyboardButton("⬅️ Back", callback_data="nav:home"),
            ],
        ]
        return text, InlineKeyboardMarkup(rows)

    def _view_status(self, ws) -> tuple[str, InlineKeyboardMarkup]:
        slot = ws.get_attached_slot() or ws.get_active_slot()
        group_str = f"<code>{ws.tg_chat_id}</code>" if ws.tg_chat_id else "Not linked"
        lines = [
            "<b>Bridge Status</b>",
            "",
            f"👤 <b>Workspace:</b> <code>{ws.username}</code>",
            f"💬 <b>Forum Group:</b> {group_str}",
        ]
        if slot:
            st = "🟢 Online" if slot.ig.logged_in else "🟠 Offline"
            rt = "🟢 Connected" if slot.ig.realtime.rt_connected else ("ON" if slot.cfg.realtime else "OFF")
            fb = "🟢 Connected" if slot.ig.realtime.fbns_connected else ("ON" if slot.cfg.fbns else "OFF")
            lines.extend(
                [
                    f"📸 <b>Attached IG:</b> <code>@{slot.ig_username}</code> ({st})",
                    f"⚡ <b>Realtime MQTT:</b> {rt} ({slot.ig.realtime.events} events)",
                    f"🔔 <b>FBNS Push:</b> {fb} ({slot.ig.realtime.pushes} pushes)",
                    f"💬 <b>Topics:</b> {len(slot.bridge.topics)} · <b>Forwarded:</b> {slot.bridge.forwarded}",
                ]
            )
        else:
            lines.append("📸 <b>Attached IG:</b> None")

        rows = [
            [
                InlineKeyboardButton("🔄 Refresh", callback_data="nav:status"),
                InlineKeyboardButton("⬅️ Back", callback_data="nav:home"),
            ]
        ]
        return "\n".join(lines), InlineKeyboardMarkup(rows)

    def _view_group(self, ws, notice: str = "") -> tuple[str, InlineKeyboardMarkup]:
        head = f"{notice}\n\n" if notice else ""
        group_str = f"<code>{ws.tg_chat_id}</code>" if ws.tg_chat_id else "Not linked"
        text = (
            f"{head}"
            f"<b>Link Telegram Forum Group</b>\n\n"
            f"Current Group: {group_str}\n\n"
            f"1. Add @{self.bot_username} as an <b>Admin</b> in your Forum Group (enable <i>Manage Topics</i>).\n"
            f"2. Type <code>/link</code> inside the group, or tap below to enter the Group ID."
        )
        rows = [
            [
                InlineKeyboardButton("✏️ Enter Group ID", callback_data="group:set"),
                InlineKeyboardButton("⬅️ Back", callback_data="nav:home"),
            ]
        ]
        return text, InlineKeyboardMarkup(rows)

    # ------------------------------------------------- Dump / Profile DB / Watcher Views
    @staticmethod
    def _dump_card_markup(uname: str, is_watched: bool) -> InlineKeyboardMarkup:
        watch_label = "🔕 Unwatch" if is_watched else "👁 Watch Account"
        rows = [
            [
                InlineKeyboardButton("🖼 HD Avatar", callback_data=f"dump:avatar:{uname}"),
                InlineKeyboardButton("🗄 Profile DB (.json)", callback_data=f"dump:dbjson:{uname}"),
            ],
            [
                InlineKeyboardButton("📖 Active Stories", callback_data=f"dump:stories:{uname}"),
                InlineKeyboardButton("✨ Highlights", callback_data=f"dump:hl:{uname}"),
            ],
            [
                InlineKeyboardButton("📸 Posts", callback_data=f"dump:sel:posts:{uname}"),
                InlineKeyboardButton("🎬 Reels", callback_data=f"dump:sel:reels:{uname}"),
                InlineKeyboardButton("🏷 Tagged", callback_data=f"dump:sel:tagged:{uname}"),
            ],
            [
                InlineKeyboardButton("👥 Followers", callback_data=f"dump:sel:followers:{uname}"),
                InlineKeyboardButton("👣 Following", callback_data=f"dump:sel:following:{uname}"),
            ],
            [
                InlineKeyboardButton(watch_label, callback_data=f"dump:watch:{uname}"),
                InlineKeyboardButton("🔄 Rescan", callback_data=f"dump:open:{uname}"),
            ],
            [
                InlineKeyboardButton("🗄 Saved Profiles DB", callback_data="nav:profile_db"),
                InlineKeyboardButton("⬅️ Home", callback_data="nav:home"),
            ],
        ]
        return InlineKeyboardMarkup(rows)

    @staticmethod
    def _view_count_selector(kind: str, uname: str) -> tuple[str, InlineKeyboardMarkup]:
        labels = {
            "posts": ("📸 Posts", [3, 5, 10, 25], "📦 Download ALL Posts"),
            "reels": ("🎬 Reels", [3, 5, 10, 25], "📦 Download ALL Reels"),
            "tagged": ("🏷 Tagged Media", [3, 5, 10, 25], "📦 Download ALL Tagged"),
            "followers": ("👥 Followers CSV/JSON", [50, 100, 250], "📦 Export ALL Followers"),
            "following": ("👣 Following CSV/JSON", [50, 100, 250], "📦 Export ALL Following"),
        }
        title, presets, all_label = labels.get(kind, ("📦 Items", [5, 10, 25], "📦 Download ALL"))
        text = (
            f"<b>{title} — @{uname}</b>\n\n"
            f"Choose how many items to fetch, tap <b>🔢 Enter Custom Count</b> to type any number, or tap <b>{all_label}</b>:"
        )
        preset_row = [
            InlineKeyboardButton(str(n), callback_data=f"dump:run:{kind}:{n}:{uname}")
            for n in presets
        ]
        rows = [
            preset_row,
            [
                InlineKeyboardButton("🔢 Enter Custom Count", callback_data=f"dump:custom:{kind}:{uname}"),
                InlineKeyboardButton(all_label, callback_data=f"dump:run:{kind}:0:{uname}"),
            ],
            [
                InlineKeyboardButton(f"⬅️ Back to @{uname}", callback_data=f"dump:open:{uname}"),
            ],
        ]
        return text, InlineKeyboardMarkup(rows)

    async def _view_watcher(self, ws, notice: str = "") -> tuple[str, InlineKeyboardMarkup]:
        targets = await osint_tools.get_watched_targets(ws)
        head = f"{notice}\n\n" if notice else ""
        if not targets:
            text = (
                f"{head}"
                "<b>👁 OSINT &amp; Story Watcher</b>\n\n"
                "No accounts are being watched yet.\n"
                "Add a target to monitor profile changes (bio, username, followers, privacy) and auto-grab new Stories &amp; Posts."
            )
        else:
            lines = [
                f"{head}<b>👁 OSINT &amp; Story Watcher ({len(targets)})</b>",
                "Tap any watched account to open its OSINT Dump Card:",
                "",
            ]
            for u, meta in targets.items():
                last_c = (meta.get("last_checked") or "Not scanned yet")[:16].replace("T", " ")
                s_cnt = len(meta.get("seen_stories") or [])
                lines.append(f"• <b>@{u}</b> — Last check: <code>{last_c}</code> ({s_cnt} stories tracked)")
            text = "\n".join(lines)

        rows: list[list[InlineKeyboardButton]] = []
        for u in list(targets.keys())[:10]:
            rows.append(
                [
                    InlineKeyboardButton(f"🕵️ @{u}", callback_data=f"dump:open:{u}"),
                    InlineKeyboardButton("🗑 Unwatch", callback_data=f"watch:rm:{u}"),
                ]
            )
        rows.append(
            [
                InlineKeyboardButton("➕ Add Target", callback_data="watch:add"),
                InlineKeyboardButton("⚡ Scan All Now", callback_data="watch:scan_all"),
            ]
        )
        rows.append(
            [
                InlineKeyboardButton("🗄 Profile DB", callback_data="nav:profile_db"),
                InlineKeyboardButton("⬅️ Back", callback_data="nav:home"),
            ]
        )
        return text, InlineKeyboardMarkup(rows)

    async def _view_profile_db(self, ws, notice: str = "") -> tuple[str, InlineKeyboardMarkup]:
        db_map: dict = await ws.store.get("profile_db", {}) or {}
        head = f"{notice}\n\n" if notice else ""
        if not db_map:
            text = (
                f"{head}"
                "<b>🗄 Saved Profile Database</b>\n\n"
                "No profiles saved in database yet. Use <code>/profile @username</code> or <code>/dump @username</code> to scan and store profiles."
            )
            rows = [
                [
                    InlineKeyboardButton("🕵️ Scan Profile", callback_data="nav:dump"),
                    InlineKeyboardButton("⬅️ Back", callback_data="nav:home"),
                ]
            ]
            return text, InlineKeyboardMarkup(rows)

        entries = sorted(
            db_map.values(),
            key=lambda e: str(e.get("last_seen") or ""),
            reverse=True,
        )
        lines = [
            f"{head}<b>🗄 Saved Profile Database ({len(entries)} profiles)</b>",
            "Every scan saves full profile metadata &amp; change history in your workspace DB:",
            "",
        ]
        rows: list[list[InlineKeyboardButton]] = []
        for e in entries[:10]:
            lat = e.get("latest") or {}
            uname = lat.get("username") or e.get("pk")
            scans = e.get("scan_count", 1)
            flw = int(lat.get("follower_count", 0) or 0)
            lines.append(f"• <b>@{uname}</b> (<code>{e.get('pk')}</code>) — {flw:,} flw · {scans} scans")
            if uname:
                rows.append(
                    [
                        InlineKeyboardButton(f"🕵️ @{uname} ({scans} scans)", callback_data=f"dump:open:{uname}"),
                        InlineKeyboardButton("📥 DB .json", callback_data=f"dump:dbjson:{uname}"),
                    ]
                )
        rows.append(
            [
                InlineKeyboardButton("📦 Export Full DB (.json)", callback_data="db:export_all"),
                InlineKeyboardButton("🕵️ New Lookup", callback_data="nav:dump"),
            ]
        )
        rows.append([InlineKeyboardButton("⬅️ Back", callback_data="nav:home")])
        return "\n".join(lines), InlineKeyboardMarkup(rows)

    async def _open_dump_card(
        self,
        chat_id: int,
        user_id: int,
        ws,
        target_username: str,
        send_db_json: bool = False,
        edit_msg=None,
        notice: str = "",
    ) -> None:
        slot = self._get_logged_in_slot(ws)
        if not slot:
            text, markup = self._view_add_ig("⚠️ Log in to an Instagram account first to run OSINT / Profile dumps.")
            await self._show_panel(chat_id, user_id, text, markup, edit_msg=edit_msg)
            return

        uname = sanitize_username(target_username)
        if not uname:
            await self._show_panel(
                chat_id,
                user_id,
                "⚠️ Please provide a valid Instagram username.",
                self._cancel_markup("nav:home"),
                edit_msg=edit_msg,
            )
            return

        await self._show_panel(
            chat_id,
            user_id,
            f"🔎 <b>Scanning @{uname}</b> (fetching Profile, OSINT metadata &amp; updating Profile DB)...",
            edit_msg=edit_msg,
        )
        try:
            rec, diffs, db_entry = await osint_tools.fetch_and_save_profile_db(ws, slot, uname, deep_osint=True)
            watched = await osint_tools.is_target_watched(ws, rec["username"])
            card_html = osint_tools.format_profile_card_html(rec, diffs, db_entry, is_watched=watched)
            if notice:
                card_html = f"{notice}\n\n{card_html}"
            markup = self._dump_card_markup(rec["username"], watched)
            await self._show_panel(chat_id, user_id, card_html, markup, edit_msg=edit_msg)

            if send_db_json:
                raw_bytes = json.dumps(db_entry, indent=2, ensure_ascii=False, default=str).encode("utf-8")
                await self.bot.send_document(
                    chat_id=chat_id,
                    document=InputFile(io.BytesIO(raw_bytes), filename=f"profile_db_{rec['username']}.json"),
                    caption=f"🗄 Profile DB Record & History for @{rec['username']} (Scan #{db_entry.get('scan_count', 1)})",
                )
        except Exception as e:
            text, markup = self._view_home(ws, notice=f"❌ Failed to fetch @{uname}: {e}")
            await self._show_panel(chat_id, user_id, text, markup, edit_msg=edit_msg)

    async def _execute_dump_batch(
        self,
        chat_id: int,
        user_id: int,
        ws,
        kind: str,
        amount: int,
        uname: str,
        edit_msg=None,
    ) -> None:
        slot = self._get_logged_in_slot(ws)
        if not slot:
            text, markup = self._view_add_ig("⚠️ Log in to an Instagram account first.")
            await self._show_panel(chat_id, user_id, text, markup, edit_msg=edit_msg)
            return

        amt_str = "ALL" if amount <= 0 else str(amount)
        await self._show_panel(
            chat_id,
            user_id,
            f"⏳ <b>Fetching {amt_str} {kind} for @{uname}...</b>\nPlease wait while files are prepared.",
            edit_msg=edit_msg,
        )

        try:
            if kind in ("followers", "following"):
                csv_bytes, json_bytes, count, canonical = await slot.ig._call(
                    osint_tools._export_follow_list_sync,
                    slot.ig.cl,
                    uname,
                    kind,
                    amount,
                )
                await self.bot.send_document(
                    chat_id=chat_id,
                    document=InputFile(io.BytesIO(csv_bytes), filename=f"{canonical}_{kind}_{count}.csv"),
                    caption=f"📊 @{canonical} — {count} {kind} (.csv)",
                )
                await self.bot.send_document(
                    chat_id=chat_id,
                    document=InputFile(io.BytesIO(json_bytes), filename=f"{canonical}_{kind}_{count}.json"),
                    caption=f"🗄 @{canonical} — {count} {kind} (.json)",
                )
                await self._open_dump_card(
                    chat_id,
                    user_id,
                    ws,
                    canonical,
                    notice=f"✅ Exported <b>{count} {kind}</b> for @{canonical}.",
                )
                return

            # Media dump: posts | reels | tagged
            items, canonical = await slot.ig._call(
                osint_tools._fetch_user_media_list_sync,
                slot.ig.cl,
                uname,
                kind,
                amount,
            )
            if not items:
                await self._open_dump_card(
                    chat_id,
                    user_id,
                    ws,
                    canonical,
                    notice=f"ℹ️ No {kind} found for @{canonical}.",
                )
                return

            sent_files = 0
            with tempfile.TemporaryDirectory(prefix="ig_dump_") as tmp:
                tmp_path = Path(tmp)
                for idx, m in enumerate(items, start=1):
                    files = await slot.ig._call(
                        osint_tools._download_media_item_sync,
                        slot.ig.cl,
                        m,
                        tmp_path,
                    )
                    likes = int(getattr(m, "like_count", 0) or 0)
                    comments = int(getattr(m, "comment_count", 0) or 0)
                    cap_txt = str(getattr(m, "caption_text", "") or "").strip()
                    cap_short = (cap_txt[:250] + "…") if len(cap_txt) > 250 else cap_txt
                    safe_cap = cap_short.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                    cap = f"📸 <b>@{canonical}</b> ({idx}/{len(items)}) · ❤️ {likes:,} · 💬 {comments:,}"
                    if safe_cap:
                        cap += f"\n{safe_cap}"
                    sent_files += await self._send_downloaded_files(chat_id, files, caption=cap)

            await self._open_dump_card(
                chat_id,
                user_id,
                ws,
                canonical,
                notice=f"✅ Sent <b>{sent_files}</b> media file(s) from <b>{len(items)} {kind}</b> of @{canonical}.",
            )
        except Exception as e:
            await self._open_dump_card(
                chat_id,
                user_id,
                ws,
                uname,
                notice=f"❌ Dump failed: {e}",
            )

    async def _handle_ig_link_download(
        self,
        chat_id: int,
        user_id: int,
        ws,
        url: str,
    ) -> None:
        cat, target = osint_tools.detect_ig_url_category(url)
        if cat == "profile":
            await self._open_dump_card(chat_id, user_id, ws, target)
            return

        slot = self._get_logged_in_slot(ws)
        if not slot:
            text, markup = self._view_add_ig("⚠️ Log in to an Instagram account first to download links.")
            await self._show_panel(chat_id, user_id, text, markup, force_new=True)
            return

        await self._show_panel(
            chat_id,
            user_id,
            f"📥 <b>Auto-Detected {cat.upper()} Link</b>\nDownloading media from Instagram...",
            force_new=True,
        )
        try:
            with tempfile.TemporaryDirectory(prefix="ig_dl_") as tmp:
                res = await osint_tools.download_ig_link(slot, url, Path(tmp))
                files = res.get("files") or []
                author = sanitize_username(res.get("author") or "")
                caption = res.get("caption") or "📥 Downloaded from Instagram"
                btn_rows = []
                if author:
                    btn_rows.append(
                        [InlineKeyboardButton(f"🕵️ Dump @{author}", callback_data=f"dump:open:{author}")]
                    )
                btn_rows.append([InlineKeyboardButton("🏠 Main Menu", callback_data="nav:home")])
                rm = InlineKeyboardMarkup(btn_rows)
                count = await self._send_downloaded_files(chat_id, files, caption=caption, reply_markup=rm)
            v_txt, markup = self._view_home(ws, notice=f"✅ Downloaded {count} file(s) ({res.get('category', 'Media')}).")
            await self._show_panel(chat_id, user_id, v_txt, markup)
        except Exception as e:
            v_txt, markup = self._view_home(ws, notice=f"❌ Download failed: {e}")
            await self._show_panel(chat_id, user_id, v_txt, markup)

    @staticmethod
    def _cancel_markup(target: str = "nav:home") -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=target)]])

    # ------------------------------------------------- Callback Query Handler
    async def _on_callback(self, update: Update, context) -> None:
        query = update.callback_query
        if not query or not query.from_user or not query.message:
            return
        await query.answer()
        user_id = query.from_user.id
        chat_id = query.message.chat_id
        data = query.data or ""
        mgr = self._manager
        if not mgr:
            return

        # Clear any pending prompt state when navigating
        self._chat_state.pop(user_id, None)

        if data == "auth:cancel":
            text, markup = self._view_welcome()
            await self._show_panel(chat_id, user_id, text, markup, edit_msg=query.message)
            return

        if data in ("auth:register", "auth:login", "auth:logout"):
            if data == "auth:logout":
                prev_ws = self._find_workspace_for_user(user_id)
                if prev_ws:
                    await prev_ws.update_telegram({"tg_admin_id": "0"})
                text, markup = self._view_welcome("✅ Signed out.")
                await self._show_panel(chat_id, user_id, text, markup, edit_msg=query.message)
                return

            mode = "register" if data == "auth:register" else "login"
            self._chat_state[user_id] = {"step": "ws_user", "mode": mode}
            title = "🆕 <b>Register Workspace</b>" if mode == "register" else "🔑 <b>Workspace Login</b>"
            await self._show_panel(
                chat_id,
                user_id,
                f"{title}\n\nSend your <b>workspace username</b>:",
                self._cancel_markup("auth:cancel"),
                edit_msg=query.message,
            )
            return

        ws = self._find_workspace_for_user(user_id)
        if not ws:
            text, markup = self._view_welcome()
            await self._show_panel(chat_id, user_id, text, markup, edit_msg=query.message)
            return

        tok = current_user_var.set(ws.username)
        try:
            # Buttons owned by a module ("m:<prefix>:<rest>"), e.g. the /dump menu.
            if data.startswith("m:"):
                prefix, _, rest = data[2:].partition(":")
                slot = ws.get_attached_slot() or ws.get_active_slot()
                found = slot.modules.all_callbacks().get(prefix) if slot else None
                if not found:
                    return
                try:
                    await found[0](rest, query, context)
                except Exception as e:
                    log.warning("Button handler %s failed: %s", prefix, e)
                    try:
                        await query.message.reply_text(f"❌ {type(e).__name__}: {e}")
                    except TelegramError:
                        pass
                return

            if data == "nav:home":
                text, markup = self._view_home(ws)
                await self._show_panel(chat_id, user_id, text, markup, edit_msg=query.message)
                return

            if data == "nav:accounts":
                text, markup = self._view_accounts(ws)
                await self._show_panel(chat_id, user_id, text, markup, edit_msg=query.message)
                return

            if data == "nav:add_ig":
                text, markup = self._view_add_ig()
                await self._show_panel(chat_id, user_id, text, markup, edit_msg=query.message)
                return

            if data == "nav:status":
                text, markup = self._view_status(ws)
                await self._show_panel(chat_id, user_id, text, markup, edit_msg=query.message)
                return

            if data == "nav:group":
                text, markup = self._view_group(ws)
                await self._show_panel(chat_id, user_id, text, markup, edit_msg=query.message)
                return

            if data == "nav:dump":
                self._chat_state[user_id] = {"step": "dump_target"}
                db_map: dict = await ws.store.get("profile_db", {}) or {}
                rows: list[list[InlineKeyboardButton]] = []
                recent_entries = sorted(
                    db_map.values(),
                    key=lambda e: str(e.get("last_seen") or ""),
                    reverse=True,
                )[:6]
                cur_row: list[InlineKeyboardButton] = []
                for e in recent_entries:
                    u = (e.get("latest") or {}).get("username")
                    if u:
                        cur_row.append(InlineKeyboardButton(f"@{u}", callback_data=f"dump:open:{u}"))
                        if len(cur_row) == 2:
                            rows.append(cur_row)
                            cur_row = []
                if cur_row:
                    rows.append(cur_row)
                rows.append(
                    [
                        InlineKeyboardButton("🗄 Saved Profile DB", callback_data="nav:profile_db"),
                        InlineKeyboardButton("❌ Cancel", callback_data="nav:home"),
                    ]
                )
                await self._show_panel(
                    chat_id,
                    user_id,
                    "🕵️ <b>OSINT Dump &amp; Profile Lookup</b>\n\n"
                    "Send any Instagram <b>@username</b> or profile URL (or tap a recent profile below):",
                    InlineKeyboardMarkup(rows),
                    edit_msg=query.message,
                )
                return

            if data == "nav:watcher":
                text, markup = await self._view_watcher(ws)
                await self._show_panel(chat_id, user_id, text, markup, edit_msg=query.message)
                return

            if data == "nav:profile_db":
                text, markup = await self._view_profile_db(ws)
                await self._show_panel(chat_id, user_id, text, markup, edit_msg=query.message)
                return

            if data == "db:export_all":
                db_map = await ws.store.get("profile_db", {}) or {}
                raw_bytes = json.dumps(db_map, indent=2, ensure_ascii=False, default=str).encode("utf-8")
                await query.message.reply_document(
                    document=InputFile(io.BytesIO(raw_bytes), filename=f"{ws.username}_profile_db.json"),
                    caption=f"🗄 Complete Profile Database ({len(db_map)} profiles)",
                )
                return

            if data == "watch:add":
                self._chat_state[user_id] = {"step": "watch_target"}
                await self._show_panel(
                    chat_id,
                    user_id,
                    "👁 <b>Add Account to Watcher</b>\n\nSend the Instagram <b>@username</b> to monitor:",
                    self._cancel_markup("nav:watcher"),
                    edit_msg=query.message,
                )
                return

            if data.startswith("watch:rm:"):
                uname = sanitize_username(data.split(":", 2)[2])
                await osint_tools.toggle_watch_target(ws, uname)
                text, markup = await self._view_watcher(ws, notice=f"🔕 Removed <b>@{uname}</b> from Watcher.")
                await self._show_panel(chat_id, user_id, text, markup, edit_msg=query.message)
                return

            if data == "watch:scan_all":
                slot = self._get_logged_in_slot(ws)
                if not slot:
                    text, markup = self._view_add_ig("⚠️ Log in to an Instagram account first.")
                    await self._show_panel(chat_id, user_id, text, markup, edit_msg=query.message)
                    return
                targets = await osint_tools.get_watched_targets(ws)
                if not targets:
                    text, markup = await self._view_watcher(ws, notice="ℹ️ No watched accounts to scan.")
                    await self._show_panel(chat_id, user_id, text, markup, edit_msg=query.message)
                    return
                await self._show_panel(
                    chat_id,
                    user_id,
                    f"⚡ <b>Scanning {len(targets)} watched account(s)...</b>",
                    edit_msg=query.message,
                )
                alerts_sent = 0
                with tempfile.TemporaryDirectory(prefix="ig_watch_") as tmp:
                    tmp_path = Path(tmp)
                    for u in list(targets.keys()):
                        try:
                            res = await osint_tools.check_watched_target(ws, slot, u, tmp_path)
                            diffs = res.get("diffs") or []
                            s_files = res.get("new_story_files") or []
                            p_files = res.get("new_post_files") or []
                            if diffs:
                                msg = f"🔔 <b>Watcher Alert: @{res['username']}</b>\n" + "\n".join(diffs)
                                await self.bot.send_message(chat_id=chat_id, text=msg, parse_mode="HTML")
                                alerts_sent += 1
                            if s_files:
                                await self._send_downloaded_files(
                                    chat_id, s_files, caption=f"📖 <b>New Story from @{res['username']}</b>"
                                )
                                alerts_sent += 1
                            if p_files:
                                await self._send_downloaded_files(
                                    chat_id, p_files, caption=f"📸 <b>New Post from @{res['username']}</b>"
                                )
                                alerts_sent += 1
                        except Exception as e:
                            log.warning("Watch scan failed for %s: %s", u, e)
                note = f"✅ Scanned {len(targets)} account(s). ({alerts_sent} new update alert(s))."
                text, markup = await self._view_watcher(ws, notice=note)
                await self._show_panel(chat_id, user_id, text, markup, edit_msg=query.message)
                return

            # ------------------------------------------------- Dump Card Callbacks
            if data.startswith("dump:open:"):
                uname = sanitize_username(data.split(":", 2)[2])
                await self._open_dump_card(chat_id, user_id, ws, uname, edit_msg=query.message)
                return

            if data.startswith("dump:watch:"):
                uname = sanitize_username(data.split(":", 2)[2])
                now_watched = await osint_tools.toggle_watch_target(ws, uname)
                note = f"👁 Now watching <b>@{uname}</b>." if now_watched else f"🔕 Unwatched <b>@{uname}</b>."
                await self._open_dump_card(chat_id, user_id, ws, uname, edit_msg=query.message, notice=note)
                return

            if data.startswith("dump:dbjson:"):
                uname = sanitize_username(data.split(":", 2)[2])
                await self._open_dump_card(chat_id, user_id, ws, uname, send_db_json=True, edit_msg=query.message)
                return

            if data.startswith("dump:avatar:"):
                uname = sanitize_username(data.split(":", 2)[2])
                slot = self._get_logged_in_slot(ws)
                if not slot:
                    text, markup = self._view_add_ig("⚠️ Log in to an Instagram account first.")
                    await self._show_panel(chat_id, user_id, text, markup, edit_msg=query.message)
                    return
                await self._show_panel(chat_id, user_id, f"🖼 <b>Downloading HD Avatar for @{uname}...</b>", edit_msg=query.message)
                try:
                    with tempfile.TemporaryDirectory(prefix="ig_av_") as tmp:
                        fpath, canonical = await slot.ig._call(
                            osint_tools._download_avatar_sync, slot.ig.cl, uname, Path(tmp)
                        )
                        await self._send_downloaded_files(chat_id, [fpath], caption=f"🖼 <b>HD Avatar — @{canonical}</b>")
                    await self._open_dump_card(chat_id, user_id, ws, canonical, notice="✅ Sent HD Avatar.")
                except Exception as e:
                    await self._open_dump_card(chat_id, user_id, ws, uname, notice=f"❌ Avatar error: {e}")
                return

            if data.startswith("dump:stories:"):
                uname = sanitize_username(data.split(":", 2)[2])
                slot = self._get_logged_in_slot(ws)
                if not slot:
                    text, markup = self._view_add_ig("⚠️ Log in to an Instagram account first.")
                    await self._show_panel(chat_id, user_id, text, markup, edit_msg=query.message)
                    return
                await self._show_panel(chat_id, user_id, f"📖 <b>Fetching active stories for @{uname}...</b>", edit_msg=query.message)
                try:
                    with tempfile.TemporaryDirectory(prefix="ig_st_") as tmp:
                        files, canonical = await slot.ig._call(
                            osint_tools._download_active_stories_sync, slot.ig.cl, uname, Path(tmp)
                        )
                        if not files:
                            await self._open_dump_card(chat_id, user_id, ws, canonical, notice=f"ℹ️ @{canonical} has no active stories right now.")
                            return
                        cnt = await self._send_downloaded_files(
                            chat_id, files, caption=f"📖 <b>Active Stories — @{canonical}</b> ({len(files)})"
                        )
                    await self._open_dump_card(chat_id, user_id, ws, canonical, notice=f"✅ Sent {cnt} active story file(s).")
                except Exception as e:
                    await self._open_dump_card(chat_id, user_id, ws, uname, notice=f"❌ Stories error: {e}")
                return

            if data.startswith("dump:hl:"):
                uname = sanitize_username(data.split(":", 2)[2])
                slot = self._get_logged_in_slot(ws)
                if not slot:
                    text, markup = self._view_add_ig("⚠️ Log in to an Instagram account first.")
                    await self._show_panel(chat_id, user_id, text, markup, edit_msg=query.message)
                    return
                await self._show_panel(chat_id, user_id, f"✨ <b>Loading Highlights for @{uname}...</b>", edit_msg=query.message)
                try:
                    hl_list, canonical = await slot.ig._call(osint_tools._list_highlights_sync, slot.ig.cl, uname)
                    if not hl_list:
                        await self._open_dump_card(chat_id, user_id, ws, canonical, notice=f"ℹ️ @{canonical} has no Highlights.")
                        return
                    rows: list[list[InlineKeyboardButton]] = []
                    for h in hl_list[:12]:
                        rows.append(
                            [
                                InlineKeyboardButton(
                                    f"✨ {h['title']} ({h['media_count']})",
                                    callback_data=f"dump:hlget:{h['pk']}:{canonical}",
                                )
                            ]
                        )
                    rows.append(
                        [
                            InlineKeyboardButton("📦 Download ALL Highlights", callback_data=f"dump:hlall:{canonical}"),
                            InlineKeyboardButton(f"⬅️ Back to @{canonical}", callback_data=f"dump:open:{canonical}"),
                        ]
                    )
                    await self._show_panel(
                        chat_id,
                        user_id,
                        f"✨ <b>Highlights for @{canonical} ({len(hl_list)})</b>\nTap a highlight tray to download, or download all:",
                        InlineKeyboardMarkup(rows),
                        edit_msg=query.message,
                    )
                except Exception as e:
                    await self._open_dump_card(chat_id, user_id, ws, uname, notice=f"❌ Highlights error: {e}")
                return

            if data.startswith("dump:hlget:"):
                parts = data.split(":", 3)
                hpk, uname = parts[2], sanitize_username(parts[3])
                slot = self._get_logged_in_slot(ws)
                if not slot:
                    return
                await self._show_panel(chat_id, user_id, f"⏳ <b>Downloading Highlight for @{uname}...</b>", edit_msg=query.message)
                try:
                    with tempfile.TemporaryDirectory(prefix="ig_hl_") as tmp:
                        files, title = await slot.ig._call(
                            osint_tools._download_highlight_sync, slot.ig.cl, hpk, Path(tmp)
                        )
                        cnt = await self._send_downloaded_files(
                            chat_id, files, caption=f"✨ <b>Highlight '{title}' — @{uname}</b> ({len(files)})"
                        )
                    await self._open_dump_card(chat_id, user_id, ws, uname, notice=f"✅ Sent {cnt} item(s) from Highlight '{title}'.")
                except Exception as e:
                    await self._open_dump_card(chat_id, user_id, ws, uname, notice=f"❌ Highlight error: {e}")
                return

            if data.startswith("dump:hlall:"):
                uname = sanitize_username(data.split(":", 2)[2])
                slot = self._get_logged_in_slot(ws)
                if not slot:
                    return
                await self._show_panel(chat_id, user_id, f"⏳ <b>Downloading ALL Highlights for @{uname}...</b>", edit_msg=query.message)
                try:
                    hl_list, canonical = await slot.ig._call(osint_tools._list_highlights_sync, slot.ig.cl, uname)
                    total_sent = 0
                    with tempfile.TemporaryDirectory(prefix="ig_hlall_") as tmp:
                        tmp_path = Path(tmp)
                        for h in hl_list[:10]:
                            files, title = await slot.ig._call(
                                osint_tools._download_highlight_sync, slot.ig.cl, h["pk"], tmp_path
                            )
                            total_sent += await self._send_downloaded_files(
                                chat_id, files, caption=f"✨ <b>Highlight '{title}' — @{canonical}</b>"
                            )
                    await self._open_dump_card(chat_id, user_id, ws, canonical, notice=f"✅ Sent {total_sent} Highlight file(s).")
                except Exception as e:
                    await self._open_dump_card(chat_id, user_id, ws, uname, notice=f"❌ Highlights error: {e}")
                return

            if data.startswith("dump:sel:"):
                parts = data.split(":", 3)
                kind, uname = parts[2], sanitize_username(parts[3])
                text, markup = self._view_count_selector(kind, uname)
                await self._show_panel(chat_id, user_id, text, markup, edit_msg=query.message)
                return

            if data.startswith("dump:custom:"):
                parts = data.split(":", 3)
                kind, uname = parts[2], sanitize_username(parts[3])
                self._chat_state[user_id] = {"step": "dump_count", "kind": kind, "target": uname}
                await self._show_panel(
                    chat_id,
                    user_id,
                    f"🔢 <b>Custom Count — @{uname} ({kind})</b>\n\n"
                    f"Send how many <b>{kind}</b> you want to download/export (e.g. <code>12</code>, or <code>0</code> for ALL):",
                    self._cancel_markup(f"dump:open:{uname}"),
                    edit_msg=query.message,
                )
                return

            if data.startswith("dump:run:"):
                parts = data.split(":", 4)
                kind = parts[2]
                amount = int(parts[3])
                uname = sanitize_username(parts[4])
                await self._execute_dump_batch(chat_id, user_id, ws, kind, amount, uname, edit_msg=query.message)
                return

            # ------------------------------------------------- Group & Account Callbacks
            if data == "group:set":
                self._chat_state[user_id] = {"step": "group_id"}
                await self._show_panel(
                    chat_id,
                    user_id,
                    "<b>Set Forum Group ID</b>\n\nSend your Group Chat ID (starts with <code>-100</code>):",
                    self._cancel_markup("nav:group"),
                    edit_msg=query.message,
                )
                return

            if data in ("add:pw", "add:ck"):
                login_type = "pw" if data == "add:pw" else "ck"
                label = "🔐 <b>Password Login</b>" if login_type == "pw" else "🍪 <b>Cookie Login</b>"
                self._chat_state[user_id] = {"step": "ig_user", "login_type": login_type}
                await self._show_panel(
                    chat_id,
                    user_id,
                    f"{label}\n\nSend the <b>Instagram username</b>:",
                    self._cancel_markup("nav:add_ig"),
                    edit_msg=query.message,
                )
                return

            if data.startswith("acct:view:"):
                ig_u = sanitize_username(data.split(":", 2)[2])
                text, markup = self._view_account_card(ws, ig_u)
                await self._show_panel(chat_id, user_id, text, markup, edit_msg=query.message)
                return

            if data.startswith("acct:attach:"):
                ig_u = sanitize_username(data.split(":", 2)[2])
                await ws.attach_account(ig_u)
                text, markup = self._view_account_card(ws, ig_u, notice=f"📌 Attached @{ig_u} to bridge.")
                await self._show_panel(chat_id, user_id, text, markup, edit_msg=query.message)
                return

            if data.startswith("acct:rt:"):
                ig_u = sanitize_username(data.split(":", 2)[2])
                slot = ws.slots.get(ig_u)
                if slot:
                    await slot.apply_config({"realtime": not slot.cfg.realtime})
                text, markup = self._view_account_card(ws, ig_u)
                await self._show_panel(chat_id, user_id, text, markup, edit_msg=query.message)
                return

            if data.startswith("acct:fbns:"):
                ig_u = sanitize_username(data.split(":", 2)[2])
                slot = ws.slots.get(ig_u)
                if slot:
                    await slot.apply_config({"fbns": not slot.cfg.fbns})
                text, markup = self._view_account_card(ws, ig_u)
                await self._show_panel(chat_id, user_id, text, markup, edit_msg=query.message)
                return

            if data.startswith("acct:relogin:"):
                ig_u = sanitize_username(data.split(":", 2)[2])
                slot = ws.slots.get(ig_u)
                if slot:
                    await self._show_panel(
                        chat_id,
                        user_id,
                        f"⏳ Re-logging in to <b>@{ig_u}</b>...",
                        edit_msg=query.message,
                    )
                    ok, msg_txt = await slot.relogin()
                    notice = f"{'✅' if ok else '❌'} {msg_txt}"
                    text, markup = self._view_account_card(ws, ig_u, notice=notice)
                    await self._show_panel(chat_id, user_id, text, markup, edit_msg=query.message)
                return

            if data.startswith("acct:dl:"):
                ig_u = sanitize_username(data.split(":", 2)[2])
                slot = ws.slots.get(ig_u)
                session_obj = await slot.get_session_json() if slot else None
                if not session_obj:
                    text, markup = self._view_account_card(ws, ig_u, notice="⚠️ No saved session file yet.")
                    await self._show_panel(chat_id, user_id, text, markup, edit_msg=query.message)
                    return
                raw_bytes = json.dumps(session_obj, indent=2, default=str).encode("utf-8")
                await query.message.reply_document(
                    document=InputFile(io.BytesIO(raw_bytes), filename=f"{ig_u}_session.json"),
                    caption=f"📦 Session JSON for @{ig_u}",
                )
                return

            if data.startswith("acct:logout:"):
                ig_u = sanitize_username(data.split(":", 2)[2])
                slot = ws.slots.get(ig_u)
                if slot:
                    await slot.logout()
                text, markup = self._view_account_card(ws, ig_u, notice=f"🚪 Logged out @{ig_u}.")
                await self._show_panel(chat_id, user_id, text, markup, edit_msg=query.message)
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

        # 1. Private Chat with Bot
        if chat.type == "private" and user:
            await self._handle_private_message(update, user.id, text)
            return

        # 2. Forum Group Chat: /link command or Topic Reply -> Instagram DM
        if text.startswith("/link") and user:
            ws = self._find_workspace_for_user(user.id)
            if ws:
                await ws.update_telegram({"tg_chat_id": str(chat.id), "tg_admin_id": str(user.id)})
                await message.reply_text(
                    f"✅ Linked this Forum Group (<code>{chat.id}</code>) to workspace <b>{ws.username}</b>.",
                    parse_mode="HTML",
                )
            else:
                await message.reply_text(
                    f"ℹ️ Group Chat ID is <code>{chat.id}</code>.\n"
                    f"Open @{self.bot_username} in private chat and sign in first.",
                    parse_mode="HTML",
                )
            return

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

    async def _complete_workspace_auth(
        self, chat_id: int, user_id: int, uname: str, password: str, mode: str
    ) -> None:
        mgr = self._manager
        if mode == "register":
            if mgr.workspace_exists(uname):
                self._chat_state.pop(user_id, None)
                text, markup = self._view_welcome(f"❌ Workspace <b>{uname}</b> already exists. Tap <b>Login</b>.")
                await self._show_panel(chat_id, user_id, text, markup)
                return
            prev_ws = self._find_workspace_for_user(user_id)
            if prev_ws and prev_ws.username != uname:
                await prev_ws.update_telegram({"tg_admin_id": "0"})
            ws = await mgr.register_workspace(uname, password)
            await ws.update_telegram({"tg_admin_id": str(user_id)})
            self._chat_state.pop(user_id, None)
            text, markup = self._view_home(ws, notice=f"✅ Created workspace <b>{ws.username}</b>.")
            await self._show_panel(chat_id, user_id, text, markup)
        else:
            ws, err = await mgr.authenticate_workspace(uname, password)
            if not ws:
                self._chat_state.pop(user_id, None)
                text, markup = self._view_welcome(f"🔒 {err}")
                await self._show_panel(chat_id, user_id, text, markup)
                return
            prev_ws = self._find_workspace_for_user(user_id)
            if prev_ws and prev_ws.username != uname:
                await prev_ws.update_telegram({"tg_admin_id": "0"})
            await ws.update_telegram({"tg_admin_id": str(user_id)})
            self._chat_state.pop(user_id, None)
            text, markup = self._view_home(ws, notice=f"✅ Signed in to <b>{ws.username}</b>.")
            await self._show_panel(chat_id, user_id, text, markup)

    async def _handle_private_message(self, update: Update, user_id: int, text: str) -> None:
        message = update.effective_message
        chat_id = message.chat_id
        mgr = self._manager
        if not mgr:
            return

        ws = self._find_workspace_for_user(user_id)
        state = self._chat_state.get(user_id)

        # Workspace Login / Register Step 1: Username
        if state and state.get("step") == "ws_user" and not text.startswith("/"):
            await self._try_delete(message)
            uname = sanitize_username(text)
            if not uname:
                await self._show_panel(
                    chat_id,
                    user_id,
                    "⚠️ Enter a valid username (letters, numbers, dots, underscores):",
                    self._cancel_markup("auth:cancel"),
                )
                return
            mode = state.get("mode", "login")
            if mode == "register" and mgr.workspace_exists(uname):
                self._chat_state.pop(user_id, None)
                v_txt, markup = self._view_welcome(f"❌ Workspace <b>{uname}</b> already exists. Tap <b>Login</b>.")
                await self._show_panel(chat_id, user_id, v_txt, markup)
                return
            if mode == "login" and not mgr.workspace_exists(uname):
                self._chat_state.pop(user_id, None)
                v_txt, markup = self._view_welcome(f"❌ Workspace <b>{uname}</b> not found. Tap <b>Register</b>.")
                await self._show_panel(chat_id, user_id, v_txt, markup)
                return
            self._chat_state[user_id] = {"step": "ws_pass", "mode": mode, "username": uname}
            prompt = (
                f"🆕 <b>Register: {uname}</b>\n\nSend a new <b>password</b> (min 4 characters):"
                if mode == "register"
                else f"🔑 <b>Login: {uname}</b>\n\nSend your <b>password</b>:"
            )
            await self._show_panel(chat_id, user_id, prompt, self._cancel_markup("auth:cancel"))
            return

        # Workspace Login / Register Step 2: Password
        if state and state.get("step") == "ws_pass" and not text.startswith("/"):
            await self._try_delete(message)
            pw = text.strip()
            uname = state.get("username", "")
            mode = state.get("mode", "login")
            if len(pw) < 4:
                await self._show_panel(
                    chat_id,
                    user_id,
                    f"⚠️ Password must be at least 4 characters.\n\nSend password for <b>{uname}</b>:",
                    self._cancel_markup("auth:cancel"),
                )
                return
            await self._complete_workspace_auth(chat_id, user_id, uname, pw, mode)
            return

        if not ws:
            v_txt, markup = self._view_welcome()
            await self._show_panel(chat_id, user_id, v_txt, markup, force_new=True)
            return

        tok = current_user_var.set(ws.username)
        try:
            # 1. Check if any Instagram slot is waiting for a 2FA / Challenge verification code
            for s in ws.slots.values():
                if s.ig.auth.prompter.waiting and text and not text.startswith("/"):
                    await self._try_delete(message)
                    code = text.strip().replace(" ", "")
                    if s.ig.auth.prompter.submit(code):
                        await self._show_panel(
                            chat_id,
                            user_id,
                            f"✅ Verification code submitted for <b>@{s.ig_username}</b>. Completing login...",
                        )
                        return

            # 2. Handle uploaded session.json file
            if message.document and (message.document.file_name or "").endswith(".json"):
                tg_file = await message.document.get_file()
                raw_bytes = await tg_file.download_as_bytearray()
                raw_json = raw_bytes.decode("utf-8", errors="replace")
                ig_u = sanitize_username(
                    message.document.file_name.replace("_session.json", "").replace(".json", "")
                )
                slot = await ws.ensure_slot(ig_u or ws.bridged_ig or ws.username)
                ok, msg_txt = await slot.import_session(raw_json)
                if ok and not ws.bridged_ig:
                    await ws.attach_account(slot.ig_username)
                await self._try_delete(message)
                notice = f"{'✅' if ok else '❌'} {msg_txt}"
                v_txt, markup = self._view_account_card(ws, slot.ig_username, notice=notice)
                await self._show_panel(chat_id, user_id, v_txt, markup)
                return

            # 3. Conversational prompts (Group ID, IG Username, IG Password/Cookie, Dump target, Custom Count, Watch target)
            if state and not text.startswith("/"):
                step = state.get("step")

                if step == "group_id":
                    await self._try_delete(message)
                    self._chat_state.pop(user_id, None)
                    try:
                        await ws.update_telegram({"tg_chat_id": text.strip()})
                        notice = f"✅ Linked Forum Group <code>{ws.tg_chat_id}</code>."
                    except Exception as e:
                        notice = f"❌ {e}"
                    v_txt, markup = self._view_home(ws, notice=notice)
                    await self._show_panel(chat_id, user_id, v_txt, markup)
                    return

                if step == "dump_target":
                    await self._try_delete(message)
                    self._chat_state.pop(user_id, None)
                    url = osint_tools.extract_ig_url(text)
                    if url:
                        await self._handle_ig_link_download(chat_id, user_id, ws, url)
                        return
                    await self._open_dump_card(chat_id, user_id, ws, text.strip())
                    return

                if step == "watch_target":
                    await self._try_delete(message)
                    self._chat_state.pop(user_id, None)
                    uname = sanitize_username(text)
                    if uname:
                        await osint_tools.toggle_watch_target(ws, uname)
                        v_txt, markup = await self._view_watcher(ws, notice=f"👁 Added <b>@{uname}</b> to Watcher.")
                    else:
                        v_txt, markup = await self._view_watcher(ws, notice="⚠️ Invalid username.")
                    await self._show_panel(chat_id, user_id, v_txt, markup)
                    return

                if step == "dump_count":
                    await self._try_delete(message)
                    kind = state.get("kind", "posts")
                    target = state.get("target", "")
                    raw_num = text.strip().lower()
                    if raw_num in ("all", "0", "max"):
                        amount = 0
                    else:
                        try:
                            amount = max(0, int(raw_num))
                        except ValueError:
                            await self._show_panel(
                                chat_id,
                                user_id,
                                f"⚠️ Send a valid number (e.g. <code>10</code> or <code>0</code> for ALL):",
                                self._cancel_markup(f"dump:open:{target}"),
                            )
                            return
                    self._chat_state.pop(user_id, None)
                    await self._execute_dump_batch(chat_id, user_id, ws, kind, amount, target)
                    return

                if step == "ig_user":
                    await self._try_delete(message)
                    ig_u = sanitize_username(text)
                    login_type = state.get("login_type", "pw")
                    if not ig_u:
                        await self._show_panel(
                            chat_id,
                            user_id,
                            "⚠️ Send a valid Instagram username:",
                            self._cancel_markup("nav:add_ig"),
                        )
                        return
                    self._chat_state[user_id] = {
                        "step": "ig_secret",
                        "login_type": login_type,
                        "ig_username": ig_u,
                    }
                    secret_name = "password" if login_type == "pw" else "sessionid cookie"
                    await self._show_panel(
                        chat_id,
                        user_id,
                        f"<b>@{ig_u}</b>\n\nSend your Instagram <b>{secret_name}</b>:",
                        self._cancel_markup("nav:add_ig"),
                    )
                    return

                if step == "ig_secret":
                    await self._try_delete(message)
                    secret = text.strip()
                    ig_u = state.get("ig_username", "")
                    login_type = state.get("login_type", "pw")
                    self._chat_state.pop(user_id, None)

                    slot = await ws.ensure_slot(ig_u)
                    await ws.attach_account(slot.ig_username)
                    await self._show_panel(
                        chat_id,
                        user_id,
                        f"⏳ <b>Logging in to @{slot.ig_username}...</b>\nPlease wait a moment.",
                    )
                    if login_type == "pw":
                        ok, res_msg = await slot.login_with_password({"password": secret})
                    else:
                        ok, res_msg = await slot.login_with_cookie(secret)
                    notice = f"{'✅' if ok else '❌'} {res_msg}"
                    v_txt, markup = self._view_account_card(ws, slot.ig_username, notice=notice)
                    await self._show_panel(chat_id, user_id, v_txt, markup)
                    return

            # 4. Auto-Detect Any Pasted Instagram Link in Private Chat!
            ig_url = osint_tools.extract_ig_url(text) if not text.startswith("/") else None
            if ig_url:
                await self._try_delete(message)
                await self._handle_ig_link_download(chat_id, user_id, ws, ig_url)
                return

            # 5. Slash Commands
            if text.startswith("/"):
                self._chat_state.pop(user_id, None)
                parts = text.split()
                cmd_name = parts[0][1:].split("@")[0].lower()
                args = parts[1:]

                if cmd_name in ("start", "menu", "panel"):
                    v_txt, markup = self._view_home(ws)
                    await self._show_panel(chat_id, user_id, v_txt, markup, force_new=True)
                    return

                if cmd_name == "accounts":
                    v_txt, markup = self._view_accounts(ws)
                    await self._show_panel(chat_id, user_id, v_txt, markup, force_new=True)
                    return

                if cmd_name == "status":
                    v_txt, markup = self._view_status(ws)
                    await self._show_panel(chat_id, user_id, v_txt, markup, force_new=True)
                    return

                if cmd_name in ("dump", "osint"):
                    if args:
                        url = osint_tools.extract_ig_url(args[0])
                        target = url if url else args[0]
                        if url:
                            await self._handle_ig_link_download(chat_id, user_id, ws, url)
                        else:
                            await self._open_dump_card(chat_id, user_id, ws, target)
                        return
                    self._chat_state[user_id] = {"step": "dump_target"}
                    await self._show_panel(
                        chat_id,
                        user_id,
                        "🕵️ <b>OSINT Dump &amp; Profile Lookup</b>\n\nSend any Instagram <b>@username</b> or profile URL:",
                        self._cancel_markup("nav:home"),
                        force_new=True,
                    )
                    return

                if cmd_name == "profile":
                    target = args[0] if args else (ws.bridged_ig or ws.active_ig)
                    if not target:
                        v_txt, markup = await self._view_profile_db(ws)
                        await self._show_panel(chat_id, user_id, v_txt, markup, force_new=True)
                        return
                    await self._open_dump_card(chat_id, user_id, ws, target, send_db_json=True)
                    return

                if cmd_name in ("dl", "download"):
                    if not args:
                        await self._show_panel(
                            chat_id,
                            user_id,
                            "📥 <b>Instagram Auto-Downloader</b>\n\nPaste any Instagram Reel, Post, Story, Highlight, or Profile URL right here in the chat:",
                            self._cancel_markup("nav:home"),
                            force_new=True,
                        )
                        return
                    url = osint_tools.extract_ig_url(args[0]) or args[0]
                    await self._handle_ig_link_download(chat_id, user_id, ws, url)
                    return

                if cmd_name in ("watch", "watcher"):
                    if args:
                        uname = sanitize_username(args[0])
                        if uname:
                            now_w = await osint_tools.toggle_watch_target(ws, uname)
                            note = f"👁 Added <b>@{uname}</b> to Watcher." if now_w else f"🔕 Removed <b>@{uname}</b> from Watcher."
                            v_txt, markup = await self._view_watcher(ws, notice=note)
                            await self._show_panel(chat_id, user_id, v_txt, markup, force_new=True)
                            return
                    v_txt, markup = await self._view_watcher(ws)
                    await self._show_panel(chat_id, user_id, v_txt, markup, force_new=True)
                    return

                if cmd_name == "logout":
                    await ws.update_telegram({"tg_admin_id": "0"})
                    v_txt, markup = self._view_welcome("✅ Signed out.")
                    await self._show_panel(chat_id, user_id, v_txt, markup, force_new=True)
                    return

                slot = ws.get_attached_slot() or ws.get_active_slot()
                if slot:
                    await slot.bridge._handle_command(cmd_name, args, update)
                    return

            # Default: show Home Menu
            v_txt, markup = self._view_home(ws)
            await self._show_panel(chat_id, user_id, v_txt, markup, force_new=True)
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
            if command.takes_update:
                await command.handler(args, update)  # the command sends its own reply
                reply = None
            else:
                reply = await command.handler(args)
        except Exception as e:
            log.warning("Command /%s failed: %s", cmd_name, e)
            reply = f"❌ {type(e).__name__}: {e}"
        message = update.effective_message
        if reply:
            await message.reply_text(reply)
        if command.sensitive:
            try:
                await message.delete()
            except TelegramError:
                log.debug("Could not delete sensitive message")
