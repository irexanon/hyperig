import asyncio
import json
import os
import signal
import socket
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

# Pre-bind port 3000 immediately before heavy third-party imports (instagrapi, telegram)
# so the container runtime sees port 3000 listening within <10ms of process launch.
_EARLY_PORT = int(os.getenv("PORT", "3000"))
_EARLY_SOCK: socket.socket | None = None
if __name__ == "__main__":
    try:
        _s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        _s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        _s.bind(("0.0.0.0", _EARLY_PORT))
        _s.listen(128)
        _s.setblocking(False)
        _EARLY_SOCK = _s
    except OSError:
        _EARLY_SOCK = None

from config import Config, build_user_config, load_config
from core.auth import CookieError
from core.context import AppContext
from core.instagram import InstagramClient
from core.message_handler import MessageHandler
from core.module_manager import ModuleManager
from tg.bridge import TelegramBridge, _TokenHub
from utils.db import (
    ScopedStore,
    Store,
    create_user_store,
    list_registered_users,
    sanitize_username,
)
from utils.logger import (
    add_user_log,
    clear_user_logs,
    current_user_var,
    get_logger,
    get_user_logs,
    setup_logging,
)

log = get_logger("main")
UI_HTML_PATH = Path(__file__).resolve().parent / "web" / "index.html"


class InstagramSlot:
    """One Instagram account instance inside a User's workspace database."""

    def __init__(
        self,
        user_id: str,
        ig_username: str,
        base_cfg: Config,
        store: Store,
        saved_cfg: dict,
        attached_to_bridge: bool = False,
    ):
        self.user_id = user_id
        self.ig_username = ig_username
        self.base_cfg = base_cfg
        self.store = store
        self.saved_cfg = dict(saved_cfg or {})
        self.saved_cfg["ig_username"] = ig_username
        self.cfg = build_user_config(base_cfg, self.saved_cfg, ig_username)

        self.ig = InstagramClient(self.cfg, self.store)
        self.bridge = TelegramBridge(self.cfg, self.store, username=user_id)
        self.bridge.attached = attached_to_bridge
        self.ctx = AppContext(cfg=self.cfg, ig=self.ig, store=self.store, bridge=self.bridge)
        self.modules = ModuleManager(self.ctx)
        self.ctx.modules = self.modules
        self.modules.load_modules()
        self.handler = MessageHandler(self.modules, self.bridge)

        self.ig.on_alert = self.bridge.alert
        self.ig.realtime.on_typing = self.bridge.typing
        self._poller_task: asyncio.Task | None = None
        self.login_in_progress = False

    @property
    def attached_to_bridge(self) -> bool:
        return self.bridge.attached

    async def set_attached(self, attached: bool) -> None:
        self.bridge.attached = attached
        await self.bridge.reconfigure(self.cfg, self.ig, self.modules)

    async def start(self) -> None:
        tok = current_user_var.set(self.user_id)
        try:
            await self.bridge.start(self.ig, self.modules)
            await self.ig.restore_saved_session()
            self._poller_task = asyncio.create_task(self._run_poller())
            await self.modules.start_all()
        finally:
            current_user_var.reset(tok)

    async def _run_poller(self) -> None:
        tok = current_user_var.set(self.user_id)
        try:
            await self.ig.run(self.handler.handle)
        except asyncio.CancelledError:
            pass
        finally:
            current_user_var.reset(tok)

    async def apply_config(self, updates: dict) -> Config:
        tok = current_user_var.set(self.user_id)
        try:
            for k, v in updates.items():
                if k in ("ig_password", "ig_totp_secret", "tg_token"):
                    if v is not None and str(v).strip() != "":
                        self.saved_cfg[k] = str(v).strip()
                    elif v == "__CLEAR__":
                        self.saved_cfg.pop(k, None)
                else:
                    self.saved_cfg[k] = v
            self.saved_cfg["ig_username"] = self.ig_username
            await self.store.set("user_config", self.saved_cfg)
            self.cfg = build_user_config(self.base_cfg, self.saved_cfg, self.ig_username)
            self.ctx.cfg = self.cfg
            await self.ig.reconfigure(self.cfg)
            await self.bridge.reconfigure(self.cfg, self.ig, self.modules)
            return self.cfg
        finally:
            current_user_var.reset(tok)

    async def login_with_password(self, payload: dict) -> tuple[bool, str]:
        tok = current_user_var.set(self.user_id)
        try:
            updates = {
                "ig_proxy": str(payload.get("proxy") or "").strip(),
                "ig_totp_secret": str(payload.get("totp_secret") or "").strip(),
                "ig_phone_number": str(payload.get("phone_number") or "").strip(),
                "ig_locale": str(payload.get("locale") or "en_US").strip(),
                "ig_country": str(payload.get("country") or "US").strip(),
                "ig_timezone_offset": str(payload.get("timezone_offset") or "").strip(),
                # Auto-enable Realtime and FBNS on password login
                "realtime": True,
                "fbns": True,
            }
            pw = str(payload.get("password") or "")
            if pw:
                updates["ig_password"] = pw

            await self.apply_config(updates)
            if not self.cfg.ig_password:
                return False, "Please enter your Instagram password."

            self.login_in_progress = True
            add_user_log(
                self.user_id,
                "INFO",
                "auth",
                f"[@{self.ig_username}] Starting password login (Realtime=ON, FBNS=ON)...",
            )
            try:
                ok = await self.ig.login()
            finally:
                self.login_in_progress = False
            if ok:
                return True, f"Logged in as @{self.ig_username} ({self.ig.auth.login_method}). Realtime & FBNS enabled."
            return False, self.ig.last_alert or "Login failed. See Live Logs for details."
        finally:
            current_user_var.reset(tok)

    async def login_with_cookie(self, raw_cookie: str, proxy: str = "") -> tuple[bool, str]:
        tok = current_user_var.set(self.user_id)
        try:
            if not raw_cookie.strip():
                return False, "Please paste your Instagram sessionid or browser cookie."
            # Auto-disable Realtime and FBNS on cookie login
            updates = {
                "ig_cookie_upgrade": False,
                "realtime": False,
                "fbns": False,
            }
            if proxy.strip():
                updates["ig_proxy"] = proxy.strip()
            await self.apply_config(updates)

            self.login_in_progress = True
            add_user_log(
                self.user_id,
                "INFO",
                "auth",
                f"[@{self.ig_username}] Starting cookie login (Realtime=OFF, FBNS=OFF)...",
            )
            try:
                method, note = await self.ig.cookie_login(raw_cookie)
            except CookieError as e:
                add_user_log(self.user_id, "WARNING", "auth", f"[@{self.ig_username}] {e}")
                return False, str(e)
            finally:
                self.login_in_progress = False
            msg = f"Logged in as @{self.ig_username} via {method}. Realtime & FBNS disabled."
            if note:
                msg += f" ({note})"
            return True, msg
        finally:
            current_user_var.reset(tok)

    async def import_session(self, raw_json: str, proxy: str = "") -> tuple[bool, str]:
        tok = current_user_var.set(self.user_id)
        try:
            if proxy.strip():
                await self.apply_config({"ig_proxy": proxy.strip()})
            await self.ig.import_session_json(raw_json)
            add_user_log(self.user_id, "INFO", "auth", f"[@{self.ig_username}] Imported and verified session JSON.")
            return True, f"Session JSON imported and verified for @{self.ig_username}."
        except Exception as e:
            add_user_log(self.user_id, "WARNING", "auth", f"[@{self.ig_username}] Session import failed: {e}")
            return False, f"Session import failed: {e}"
        finally:
            current_user_var.reset(tok)

    async def get_session_json(self) -> dict | None:
        session = await self.store.get("session")
        if session and isinstance(session, dict):
            return session
        if self.ig.logged_in:
            try:
                return await self.ig._call(self.ig.cl.get_settings)
            except Exception:
                return None
        return None

    async def relogin(self) -> tuple[bool, str]:
        tok = current_user_var.set(self.user_id)
        try:
            self.login_in_progress = True
            add_user_log(self.user_id, "INFO", "auth", f"[@{self.ig_username}] Running /relogin (keeping device identity)...")
            try:
                ok = await self.ig.relogin()
            finally:
                self.login_in_progress = False
            if ok:
                return True, f"Re-logged in as @{self.ig_username}."
            return False, self.ig.last_alert or "Re-login failed."
        finally:
            current_user_var.reset(tok)

    async def reset_device(self) -> tuple[bool, str]:
        tok = current_user_var.set(self.user_id)
        try:
            self.login_in_progress = True
            add_user_log(self.user_id, "INFO", "auth", f"[@{self.ig_username}] Resetting device identity and session...")
            try:
                ok = await self.ig.reset_session()
            finally:
                self.login_in_progress = False
            if ok:
                return True, f"Created new device identity and logged in as @{self.ig_username}."
            return False, "Session cleared. Log in with password or cookie to initialize new device."
        finally:
            current_user_var.reset(tok)

    async def logout(self) -> None:
        tok = current_user_var.set(self.user_id)
        try:
            await self.ig.logout()
            add_user_log(self.user_id, "INFO", "auth", f"[@{self.ig_username}] Logged out and cleared saved session.")
        finally:
            current_user_var.reset(tok)

    async def unlink_topic(self, thread_id: str) -> None:
        tok = current_user_var.set(self.user_id)
        try:
            await self.bridge._forget_topic(str(thread_id))
            add_user_log(self.user_id, "INFO", "telegram", f"[@{self.ig_username}] Unlinked topic for thread {thread_id}.")
        finally:
            current_user_var.reset(tok)

    async def run_command(self, raw_cmd: str) -> str:
        tok = current_user_var.set(self.user_id)
        try:
            tokens = (raw_cmd or "").strip().split()
            if not tokens:
                return "Enter a command (e.g. /status, /session, /help)."
            cmd_name = tokens[0].lstrip("/").lower()
            args = tokens[1:]
            cmds = self.modules.all_commands()
            if cmd_name not in cmds:
                return f"Unknown command '/{cmd_name}'. Available: " + ", ".join(f"/{c}" for c in sorted(cmds))
            return await cmds[cmd_name][0].handler(args)
        except Exception as e:
            return f"{type(e).__name__}: {e}"
        finally:
            current_user_var.reset(tok)

    async def stop(self) -> None:
        tok = current_user_var.set(self.user_id)
        try:
            await self.modules.stop_all()
            await self.ig.stop()
            if self._poller_task:
                self._poller_task.cancel()
                await asyncio.gather(self._poller_task, return_exceptions=True)
            await self.bridge.stop()
        finally:
            current_user_var.reset(tok)

    async def to_dict(self) -> dict:
        session_data = await self.store.get("session")
        has_saved_session = bool(isinstance(session_data, dict) and session_data.get("uuids"))
        topics_list = []
        for tid, top_id in self.bridge.topics.items():
            meta = self.ig.meta.get(tid)
            title = meta[0] if meta else f"Thread {str(tid)[-6:]}"
            topics_list.append({"thread_id": tid, "topic_id": top_id, "title": title})

        return {
            "ig_username": self.ig_username,
            "attached_to_bridge": self.attached_to_bridge,
            "ig_logged_in": self.ig.logged_in,
            "login_in_progress": self.login_in_progress,
            "login_method": self.ig.auth.login_method,
            "last_alert": self.ig.last_alert,
            "waiting_for_code": self.ig.auth.prompter.waiting,
            "code_hint": self.ig.auth.prompter.hint,
            "has_saved_session": has_saved_session,
            "has_password": bool(self.cfg.ig_password),
            "has_totp": bool(self.cfg.ig_totp_secret),
            "proxy": self.cfg.ig_proxy or "",
            "phone_number": self.cfg.ig_phone_number or "",
            "locale": self.cfg.ig_locale or "en_US",
            "country": self.cfg.ig_country or "US",
            "timezone_offset": self.cfg.ig_timezone_offset if self.cfg.ig_timezone_offset is not None else "",
            "realtime": self.cfg.realtime,
            "fbns": self.cfg.fbns,
            "rt_connected": self.ig.realtime.rt_connected,
            "rt_events": self.ig.realtime.events,
            "rt_error": self.ig.realtime.rt_error,
            "fbns_connected": self.ig.realtime.fbns_connected,
            "fbns_pushes": self.ig.realtime.pushes,
            "fbns_error": self.ig.realtime.fbns_error,
            "linked_topics": len(self.bridge.topics),
            "topics": topics_list,
            "forwarded": self.bridge.forwarded,
            "tg_running": self.bridge.running,
            "tg_bot_username": self.bridge.bot_username,
            "tg_error": self.bridge.error,
            "session_info": self.ig.auth.info(),
        }


import hashlib
import secrets


def _hash_workspace_password(raw_pw: str) -> str:
    return hashlib.sha256(f"hib_ws_v1:{raw_pw}".encode("utf-8")).hexdigest()


class UserWorkspace:
    """An authenticated User Workspace with its own isolated database and multiple Instagram accounts."""

    def __init__(self, username: str, base_cfg: Config, store: Store):
        self.username = username
        self.base_cfg = base_cfg
        self.store = store
        self.password_hash: str = ""
        self.tg_chat_id: int = 0
        self.tg_admin_id: int = 0
        self.tg_token: str = ""
        self.slots: dict[str, InstagramSlot] = {}
        self.active_ig: str = ""
        self.bridged_ig: str = ""
        self.bridge_enabled: bool = True
        self._lock = asyncio.Lock()

    def get_attached_slot(self) -> InstagramSlot | None:
        if self.bridged_ig and self.bridged_ig in self.slots:
            return self.slots[self.bridged_ig]
        return next(iter(self.slots.values()), None)

    def get_active_slot(self) -> InstagramSlot | None:
        if self.active_ig and self.active_ig in self.slots:
            return self.slots[self.active_ig]
        return next(iter(self.slots.values()), None)

    async def set_password(self, raw_password: str) -> None:
        self.password_hash = _hash_workspace_password(raw_password.strip())
        await self._save_profile()

    async def verify_password(self, raw_password: str) -> bool:
        clean = (raw_password or "").strip()
        if not clean:
            return False
        candidate_hash = _hash_workspace_password(clean)
        if self.password_hash:
            if secrets.compare_digest(self.password_hash, candidate_hash):
                return True
            # Also allow authenticating with any saved Instagram account password in this workspace
            for slot in self.slots.values():
                if slot.cfg.ig_password and secrets.compare_digest(slot.cfg.ig_password, clean):
                    return True
            return False

        # Legacy workspace created before workspace passwords existed:
        # Check if password matches any saved ig_password in legacy/slot configs, or bind on first login
        legacy_cfg = await self.store.get("user_config") or {}
        known_pws = set()
        if isinstance(legacy_cfg, dict) and legacy_cfg.get("ig_password"):
            known_pws.add(str(legacy_cfg["ig_password"]).strip())
        for slot in self.slots.values():
            if slot.cfg.ig_password:
                known_pws.add(slot.cfg.ig_password.strip())
        if known_pws:
            if clean in known_pws:
                await self.set_password(clean)
                return True
            return False
        # No password was ever set on this legacy workspace: bind the provided password now
        if len(clean) >= 4:
            await self.set_password(clean)
            return True
        return False

    async def start(self) -> None:
        tok = current_user_var.set(self.username)
        try:
            profile = await self.store.get("profile")
            legacy_cfg = await self.store.get("user_config")
            if not isinstance(profile, dict):
                legacy_session = await self.store.get("session")
                accounts: list[str] = []
                tg_chat_id = 0
                tg_admin_id = 0
                tg_token = ""
                if isinstance(legacy_cfg, dict) or isinstance(legacy_session, dict):
                    ig_u = sanitize_username((legacy_cfg or {}).get("ig_username") or self.username)
                    if ig_u:
                        accounts.append(ig_u)
                        scoped = ScopedStore(self.store, f"acct_{ig_u}_")
                        if legacy_cfg:
                            await scoped.set("user_config", legacy_cfg)
                            try:
                                tg_chat_id = int(legacy_cfg.get("tg_chat_id") or 0)
                            except ValueError:
                                tg_chat_id = 0
                            try:
                                tg_admin_id = int(legacy_cfg.get("tg_admin_id") or 0)
                            except ValueError:
                                tg_admin_id = 0
                            tg_token = str(legacy_cfg.get("tg_token") or "")
                        if legacy_session:
                            await scoped.set("session", legacy_session)
                        for key in ("cursors", "delivered", "topics", "session_meta"):
                            val = await self.store.get(key)
                            if val is not None:
                                await scoped.set(key, val)
                first_ig = accounts[0] if accounts else ""
                profile = {
                    "tg_chat_id": tg_chat_id,
                    "tg_admin_id": tg_admin_id,
                    "tg_token": tg_token,
                    "accounts": accounts,
                    "active_account": first_ig,
                    "bridged_account": first_ig,
                }
                await self.store.set("profile", profile)

            self.password_hash = str(profile.get("password_hash") or "")
            try:
                self.tg_chat_id = int(profile.get("tg_chat_id") or 0)
            except ValueError:
                self.tg_chat_id = 0
            try:
                self.tg_admin_id = int(profile.get("tg_admin_id") or 0)
            except ValueError:
                self.tg_admin_id = 0
            self.tg_token = str(profile.get("tg_token") or "")

            # Recover tg_admin_id or tg_chat_id from legacy_cfg if missing in profile
            if isinstance(legacy_cfg, dict):
                if not self.tg_admin_id and legacy_cfg.get("tg_admin_id"):
                    try:
                        self.tg_admin_id = int(legacy_cfg["tg_admin_id"])
                    except ValueError:
                        pass
                if not self.tg_chat_id and legacy_cfg.get("tg_chat_id"):
                    try:
                        self.tg_chat_id = int(legacy_cfg["tg_chat_id"])
                    except ValueError:
                        pass

            account_names = [sanitize_username(a) for a in (profile.get("accounts") or []) if sanitize_username(a)]
            self.active_ig = sanitize_username(profile.get("active_account") or "")
            self.bridged_ig = sanitize_username(profile.get("bridged_account") or self.active_ig or "")
            self.bridge_enabled = bool(profile.get("bridge_enabled", True))
            if not self.bridged_ig and account_names:
                self.bridged_ig = account_names[0]

            add_user_log(self.username, "INFO", "workspace", f"Loaded database for user '{self.username}'")

            for ig_u in account_names:
                await self._init_slot(ig_u)
            if not self.active_ig and self.slots:
                self.active_ig = next(iter(self.slots))
            if not self.bridged_ig and self.slots:
                self.bridged_ig = self.active_ig
        finally:
            current_user_var.reset(tok)

    async def _save_profile(self) -> None:
        await self.store.set(
            "profile",
            {
                "password_hash": self.password_hash,
                "tg_chat_id": self.tg_chat_id,
                "tg_admin_id": self.tg_admin_id,
                "tg_token": self.tg_token,
                "accounts": list(self.slots.keys()),
                "active_account": self.active_ig,
                "bridged_account": self.bridged_ig,
                "bridge_enabled": self.bridge_enabled,
            },
        )

    async def _init_slot(self, ig_username: str) -> InstagramSlot:
        if ig_username in self.slots:
            return self.slots[ig_username]
        scoped = ScopedStore(self.store, f"acct_{ig_username}_")
        saved_cfg = await scoped.get("user_config", {}) or {}
        if not self.tg_admin_id and saved_cfg.get("tg_admin_id"):
            try:
                self.tg_admin_id = int(saved_cfg["tg_admin_id"])
            except ValueError:
                pass
        if self.tg_chat_id:
            saved_cfg["tg_chat_id"] = self.tg_chat_id
        if self.tg_admin_id:
            saved_cfg["tg_admin_id"] = self.tg_admin_id
        if self.tg_token:
            saved_cfg["tg_token"] = self.tg_token
        if not self.bridged_ig:
            self.bridged_ig = ig_username
        is_attached = (ig_username == self.bridged_ig and self.bridge_enabled)
        slot = InstagramSlot(
            self.username,
            ig_username,
            self.base_cfg,
            scoped,
            saved_cfg,
            attached_to_bridge=is_attached,
        )
        await slot.start()
        self.slots[ig_username] = slot
        return slot

    async def ensure_slot(self, raw_ig_username: str) -> InstagramSlot:
        ig_u = sanitize_username(raw_ig_username)
        if not ig_u:
            raise ValueError("Enter a valid Instagram username.")
        async with self._lock:
            slot = await self._init_slot(ig_u)
            self.active_ig = ig_u
            if not self.bridged_ig or self.bridged_ig not in self.slots:
                self.bridged_ig = ig_u
                await slot.set_attached(True)
            await self._save_profile()
            return slot

    async def attach_account(self, raw_ig_username: str) -> InstagramSlot:
        tok = current_user_var.set(self.username)
        try:
            slot = await self.ensure_slot(raw_ig_username)
            self.bridged_ig = slot.ig_username
            self.active_ig = slot.ig_username
            self.bridge_enabled = True
            for uname, s in self.slots.items():
                await s.set_attached(uname == self.bridged_ig)
            await self._save_profile()
            add_user_log(
                self.username,
                "INFO",
                "bridge",
                f"Attached @{self.bridged_ig} to the Telegram bridge.",
            )
            return slot
        finally:
            current_user_var.reset(tok)

    async def set_bridge_enabled(self, enabled: bool) -> None:
        """Enable or pause Instagram-to-Telegram DM bridging for this workspace."""
        tok = current_user_var.set(self.username)
        try:
            self.bridge_enabled = bool(enabled)
            if self.bridge_enabled and not self.bridged_ig and self.slots:
                self.bridged_ig = self.active_ig or next(iter(self.slots))
            for uname, slot in self.slots.items():
                await slot.set_attached(self.bridge_enabled and uname == self.bridged_ig)
            await self._save_profile()
            state = "enabled" if self.bridge_enabled else "paused"
            add_user_log(
                self.username,
                "INFO",
                "bridge",
                f"Instagram DM bridge {state}.",
            )
        finally:
            current_user_var.reset(tok)

    async def remove_slot(self, raw_ig_username: str) -> None:
        ig_u = sanitize_username(raw_ig_username)
        async with self._lock:
            slot = self.slots.pop(ig_u, None)
            if slot:
                await slot.stop()
                scoped = ScopedStore(self.store, f"acct_{ig_u}_")
                for k in ("session", "session_meta", "user_config", "cursors", "delivered", "topics"):
                    await scoped.set(k, None)
                add_user_log(self.username, "INFO", "workspace", f"Removed Instagram account @{ig_u}")
            if self.active_ig == ig_u:
                self.active_ig = next(iter(self.slots), "")
            if self.bridged_ig == ig_u:
                self.bridged_ig = self.active_ig
                if self.bridged_ig and self.bridged_ig in self.slots:
                    await self.slots[self.bridged_ig].set_attached(self.bridge_enabled)
            await self._save_profile()

    async def update_telegram(self, payload: dict) -> None:
        tok = current_user_var.set(self.username)
        try:
            if "tg_chat_id" in payload:
                raw_chat = str(payload.get("tg_chat_id") or "").strip()
                try:
                    self.tg_chat_id = int(raw_chat) if raw_chat else 0
                except ValueError:
                    raise ValueError("Telegram Chat ID must be a number (e.g. -1001234567890).")
            if "tg_admin_id" in payload:
                raw_admin = str(payload.get("tg_admin_id") or "").strip()
                try:
                    self.tg_admin_id = int(raw_admin) if raw_admin else 0
                except ValueError:
                    raise ValueError("Telegram Admin ID must be a numeric user ID.")
            if "tg_token" in payload:
                raw_token = str(payload.get("tg_token") or "").strip()
                if raw_token == "__CLEAR__":
                    self.tg_token = ""
                elif raw_token:
                    self.tg_token = raw_token

            await self._save_profile()
            for slot in self.slots.values():
                updates = {
                    "tg_chat_id": self.tg_chat_id,
                    "tg_admin_id": self.tg_admin_id,
                }
                if self.tg_token:
                    updates["tg_token"] = self.tg_token
                await slot.apply_config(updates)
            add_user_log(
                self.username,
                "INFO",
                "telegram",
                f"Updated Telegram routing (Group ID={self.tg_chat_id or 'none'}, Admin ID={self.tg_admin_id or 'none'}).",
            )
        finally:
            current_user_var.reset(tok)

    async def stop(self) -> None:
        for slot in list(self.slots.values()):
            try:
                await slot.stop()
            except Exception:
                pass

    async def to_state_dict(self) -> dict:
        accounts = [await slot.to_dict() for slot in self.slots.values()]
        tg_running = any(a["tg_running"] for a in accounts)
        bot_uname = next((a["tg_bot_username"] for a in accounts if a["tg_bot_username"]), "")
        if not bot_uname:
            for hub in _TokenHub._hubs.values():
                if hub.bot_username:
                    bot_uname = hub.bot_username
                    break
        tg_err = next((a["tg_error"] for a in accounts if a["attached_to_bridge"] and a["tg_error"]), "")
        return {
            "username": self.username,
            "storage_backend": "MongoDB" if self.base_cfg.mongo_uri else f"data/users/{self.username}",
            "tg_chat_id": self.tg_chat_id or "",
            "tg_admin_id": self.tg_admin_id or "",
            "has_custom_tg_token": bool(self.tg_token),
            "has_default_tg_token": bool(self.base_cfg.tg_token),
            "tg_running": tg_running,
            "tg_bot_username": bot_uname,
            "tg_error": tg_err,
            "active_ig": self.active_ig,
            "bridged_ig": self.bridged_ig,
            "bridge_enabled": self.bridge_enabled,
            "accounts": accounts,
        }


class MultiUserManager:
    """Manages per-username workspaces, authentication, and databases."""

    def __init__(self, base_cfg: Config):
        self.base_cfg = base_cfg
        self.workspaces: dict[str, UserWorkspace] = {}
        self.tokens: dict[str, str] = {}  # auth_token -> username
        self._lock = asyncio.Lock()

    async def bootstrap_existing(self) -> None:
        if self.base_cfg.tg_token:
            try:
                await _TokenHub.ensure_Started(self.base_cfg.tg_token)
            except Exception as e:
                log.warning("Could not start default Telegram bot on boot: %s", e)

        names = list_registered_users(self.base_cfg)
        for name in names:
            try:
                await self.get_or_create(name)
            except Exception as e:
                log.warning("Could not restore workspace %s: %s", name, e)

    def workspace_exists(self, raw_username: str) -> bool:
        uname = sanitize_username(raw_username)
        if not uname:
            return False
        if uname in self.workspaces:
            return True
        return uname in list_registered_users(self.base_cfg)

    async def register_workspace(self, raw_username: str, raw_password: str) -> UserWorkspace:
        uname = sanitize_username(raw_username)
        if not uname:
            raise ValueError("Please enter a valid username.")
        clean_pw = (raw_password or "").strip()
        if len(clean_pw) < 4:
            raise ValueError("Password must be at least 4 characters.")
        if self.workspace_exists(uname):
            raise ValueError(f"Workspace '{uname}' is already registered. Please Sign In instead.")
        ws = await self.get_or_create(uname)
        await ws.set_password(clean_pw)
        add_user_log(ws.username, "INFO", "auth", f"Workspace '{ws.username}' registered.")
        return ws

    async def authenticate_workspace(self, raw_username: str, raw_password: str) -> tuple[UserWorkspace | None, str]:
        uname = sanitize_username(raw_username)
        if not uname:
            return None, "Please enter your workspace username."
        clean_pw = (raw_password or "").strip()
        if not clean_pw:
            return None, "Please enter your workspace password."
        if not self.workspace_exists(uname):
            return None, f"Workspace '{uname}' does not exist. Switch to Register to create it."
        ws = await self.get_or_create(uname)
        ok = await ws.verify_password(clean_pw)
        if not ok:
            return None, "Incorrect password for this workspace."
        return ws, ""

    def issue_token(self, ws: UserWorkspace) -> str:
        tok = hashlib.sha256(f"hib_tok_v1:{ws.username}:{ws.password_hash}".encode("utf-8")).hexdigest()
        self.tokens[tok] = ws.username
        return tok

    def verify_token(self, token: str, username: str) -> bool:
        if not token:
            return False
        uname = sanitize_username(username)
        if self.tokens.get(token) == uname:
            return True
        ws = self.workspaces.get(uname)
        if ws and ws.password_hash:
            expected = hashlib.sha256(f"hib_tok_v1:{ws.username}:{ws.password_hash}".encode("utf-8")).hexdigest()
            if secrets.compare_digest(token, expected):
                self.tokens[token] = uname
                return True
        return False

    async def get_or_create(self, raw_username: str) -> UserWorkspace:
        uname = sanitize_username(raw_username)
        if not uname:
            raise ValueError("Please enter a valid username.")
        async with self._lock:
            if uname in self.workspaces:
                return self.workspaces[uname]
            store = create_user_store(self.base_cfg, uname)
            ws = UserWorkspace(uname, self.base_cfg, store)
            await ws.start()
            self.workspaces[uname] = ws
            return ws

    def list_summaries(self) -> list[dict]:
        out = []
        for uname, ws in sorted(self.workspaces.items()):
            logged_in_count = sum(1 for s in ws.slots.values() if s.ig.logged_in)
            out.append(
                {
                    "username": uname,
                    "account_count": len(ws.slots),
                    "logged_in_count": logged_in_count,
                    "bridged_ig": ws.bridged_ig,
                    "tg_chat_id": ws.tg_chat_id or "",
                }
            )
        return out

    async def stop_all(self) -> None:
        for ws in list(self.workspaces.values()):
            try:
                await ws.stop()
            except Exception:
                pass


def _send_json(writer: asyncio.StreamWriter, status_code: int, payload: dict) -> None:
    status_map = {
        200: "200 OK",
        400: "400 Bad Request",
        401: "401 Unauthorized",
        404: "404 Not Found",
        500: "500 Internal Server Error",
    }
    body = json.dumps(payload, default=str).encode("utf-8")
    headers = (
        f"HTTP/1.1 {status_map.get(status_code, '200 OK')}\r\n"
        "Content-Type: application/json; charset=utf-8\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Cache-Control: no-store\r\n"
        "Connection: close\r\n\r\n"
    ).encode("utf-8")
    writer.write(headers + body)


async def _start_web_server(manager: MultiUserManager, host: str = "0.0.0.0", port: int = 3000):
    web_log = get_logger("web")

    async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=10.0)
            if not request_line:
                return
            parts = request_line.decode("utf-8", errors="replace").strip().split(" ")
            if len(parts) < 2:
                return
            method, raw_target = parts[0].upper(), parts[1]
            parsed = urlparse(raw_target)
            path = parsed.path
            query = parse_qs(parsed.query)

            content_length = 0
            auth_header_token = ""
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=10.0)
                if not line or line in (b"\r\n", b"\n"):
                    break
                header = line.decode("utf-8", errors="replace").strip()
                lower_h = header.lower()
                if lower_h.startswith("content-length:"):
                    try:
                        content_length = int(header.split(":", 1)[1].strip())
                    except ValueError:
                        content_length = 0
                elif lower_h.startswith("x-auth-token:"):
                    auth_header_token = header.split(":", 1)[1].strip()

            body = b""
            if content_length > 0:
                body = await asyncio.wait_for(reader.readexactly(min(content_length, 512 * 1024)), timeout=15.0)

            def parse_json_body() -> dict:
                if not body:
                    return {}
                try:
                    return json.loads(body.decode("utf-8", errors="replace"))
                except Exception:
                    return {}

            if path == "/api/users" and method == "GET":
                _send_json(writer, 200, {"users": []})
                await writer.drain()
                return

            if path == "/api/auth/register" and method == "POST":
                data = parse_json_body()
                raw_uname = sanitize_username(str(data.get("username") or ""))
                raw_pw = str(data.get("password") or "").strip()
                if not raw_uname:
                    _send_json(writer, 400, {"error": "Please enter a valid workspace username."})
                    await writer.drain()
                    return
                if len(raw_pw) < 4:
                    _send_json(writer, 400, {"error": "Password must be at least 4 characters."})
                    await writer.drain()
                    return
                if manager.workspace_exists(raw_uname):
                    _send_json(
                        writer,
                        400,
                        {"error": f"Workspace '{raw_uname}' is already registered. Switch to Sign In and enter your password."},
                    )
                    await writer.drain()
                    return
                ws = await manager.register_workspace(raw_uname, raw_pw)
                token = manager.issue_token(ws)
                _send_json(
                    writer,
                    200,
                    {"ok": True, "username": ws.username, "token": token, "state": await ws.to_state_dict()},
                )
                await writer.drain()
                return

            if path == "/api/auth/login" and method == "POST":
                data = parse_json_body()
                raw_uname = sanitize_username(str(data.get("username") or ""))
                raw_pw = str(data.get("password") or "").strip()
                ws, err = await manager.authenticate_workspace(raw_uname, raw_pw)
                if not ws:
                    _send_json(writer, 401, {"error": err})
                    await writer.drain()
                    return
                token = manager.issue_token(ws)
                _send_json(
                    writer,
                    200,
                    {"ok": True, "username": ws.username, "token": token, "state": await ws.to_state_dict()},
                )
                await writer.drain()
                return

            if path.startswith("/api/users/"):
                segments = [unquote(s) for s in path.split("/") if s]
                if len(segments) >= 3:
                    uname = sanitize_username(segments[2])
                    subpath = "/".join(segments[3:])
                    req_tok = auth_header_token or ((query.get("token") or [""])[0])
                    if not manager.verify_token(req_tok, uname):
                        _send_json(writer, 401, {"error": "Session expired or unauthorized. Please Sign In."})
                        await writer.drain()
                        return
                    if not manager.workspace_exists(uname):
                        _send_json(writer, 404, {"error": "Workspace not found."})
                        await writer.drain()
                        return
                    ws = await manager.get_or_create(uname)

                    if subpath == "state" and method == "GET":
                        _send_json(writer, 200, await ws.to_state_dict())
                        await writer.drain()
                        return

                    if subpath == "telegram" and method == "POST":
                        data = parse_json_body()
                        await ws.update_telegram(data)
                        _send_json(writer, 200, {"ok": True, "state": await ws.to_state_dict()})
                        await writer.drain()
                        return

                    if subpath == "accounts/attach" and method == "POST":
                        data = parse_json_body()
                        slot = await ws.attach_account(str(data.get("ig_username") or ""))
                        _send_json(
                            writer,
                            200,
                            {"ok": True, "ig_username": slot.ig_username, "state": await ws.to_state_dict()},
                        )
                        await writer.drain()
                        return

                    if subpath == "accounts/login" and method == "POST":
                        data = parse_json_body()
                        slot = await ws.ensure_slot(str(data.get("ig_username") or ""))
                        await ws.attach_account(slot.ig_username)
                        ok, msg = await slot.login_with_password(data)
                        _send_json(
                            writer,
                            200,
                            {"ok": ok, "message": msg, "ig_username": slot.ig_username, "state": await ws.to_state_dict()},
                        )
                        await writer.drain()
                        return

                    if subpath == "accounts/cookie" and method == "POST":
                        data = parse_json_body()
                        slot = await ws.ensure_slot(str(data.get("ig_username") or ""))
                        await ws.attach_account(slot.ig_username)
                        ok, msg = await slot.login_with_cookie(
                            raw_cookie=str(data.get("cookie") or ""),
                            proxy=str(data.get("proxy") or ""),
                        )
                        _send_json(
                            writer,
                            200,
                            {"ok": ok, "message": msg, "ig_username": slot.ig_username, "state": await ws.to_state_dict()},
                        )
                        await writer.drain()
                        return

                    if subpath == "accounts/import" and method == "POST":
                        data = parse_json_body()
                        slot = await ws.ensure_slot(str(data.get("ig_username") or ""))
                        await ws.attach_account(slot.ig_username)
                        ok, msg = await slot.import_session(
                            raw_json=str(data.get("session_json") or ""),
                            proxy=str(data.get("proxy") or ""),
                        )
                        _send_json(
                            writer,
                            200,
                            {"ok": ok, "message": msg, "ig_username": slot.ig_username, "state": await ws.to_state_dict()},
                        )
                        await writer.drain()
                        return

                    if subpath == "accounts/toggle" and method == "POST":
                        data = parse_json_body()
                        slot = await ws.ensure_slot(str(data.get("ig_username") or ""))
                        updates = {}
                        for k in ("realtime", "fbns"):
                            if k in data:
                                updates[k] = bool(data[k])
                        await slot.apply_config(updates)
                        add_user_log(
                            ws.username,
                            "INFO",
                            "config",
                            f"[@{slot.ig_username}] Realtime={'ON' if slot.cfg.realtime else 'OFF'}, FBNS={'ON' if slot.cfg.fbns else 'OFF'}",
                        )
                        _send_json(writer, 200, {"ok": True, "state": await ws.to_state_dict()})
                        await writer.drain()
                        return

                    if subpath == "accounts/relogin" and method == "POST":
                        data = parse_json_body()
                        slot = await ws.ensure_slot(str(data.get("ig_username") or ""))
                        ok, msg = await slot.relogin()
                        _send_json(writer, 200, {"ok": ok, "message": msg, "state": await ws.to_state_dict()})
                        await writer.drain()
                        return

                    if subpath == "accounts/reset_device" and method == "POST":
                        data = parse_json_body()
                        slot = await ws.ensure_slot(str(data.get("ig_username") or ""))
                        ok, msg = await slot.reset_device()
                        _send_json(writer, 200, {"ok": ok, "message": msg, "state": await ws.to_state_dict()})
                        await writer.drain()
                        return

                    if subpath == "accounts/code" and method == "POST":
                        data = parse_json_body()
                        slot = await ws.ensure_slot(str(data.get("ig_username") or ""))
                        code = str(data.get("code") or "").strip()
                        accepted = slot.ig.auth.prompter.submit(code)
                        if accepted:
                            add_user_log(ws.username, "INFO", "auth", f"[@{slot.ig_username}] Verification code submitted.")
                        _send_json(writer, 200, {"ok": accepted})
                        await writer.drain()
                        return

                    if subpath == "accounts/logout" and method == "POST":
                        data = parse_json_body()
                        slot = await ws.ensure_slot(str(data.get("ig_username") or ""))
                        await slot.logout()
                        _send_json(writer, 200, {"ok": True, "state": await ws.to_state_dict()})
                        await writer.drain()
                        return

                    if subpath == "accounts/remove" and method == "POST":
                        data = parse_json_body()
                        await ws.remove_slot(str(data.get("ig_username") or ""))
                        _send_json(writer, 200, {"ok": True, "state": await ws.to_state_dict()})
                        await writer.drain()
                        return

                    if subpath == "accounts/unlink_topic" and method == "POST":
                        data = parse_json_body()
                        slot = await ws.ensure_slot(str(data.get("ig_username") or ""))
                        await slot.unlink_topic(str(data.get("thread_id") or ""))
                        _send_json(writer, 200, {"ok": True, "state": await ws.to_state_dict()})
                        await writer.drain()
                        return

                    if subpath == "accounts/command" and method == "POST":
                        data = parse_json_body()
                        ig_u = str(data.get("ig_username") or "")
                        slot = ws.slots.get(sanitize_username(ig_u)) or ws.get_attached_slot()
                        cmd_str = str(data.get("command") or "").strip()
                        if not slot:
                            add_user_log(ws.username, "WARNING", "console", "Add an Instagram account first to run commands.")
                            _send_json(writer, 200, {"reply": "Add an Instagram account first."})
                            await writer.drain()
                            return
                        add_user_log(ws.username, "INFO", "console", f"[@{slot.ig_username}] > {cmd_str}")
                        reply = await slot.run_command(cmd_str)
                        add_user_log(ws.username, "INFO", "console", reply)
                        _send_json(writer, 200, {"reply": reply})
                        await writer.drain()
                        return

                    if len(segments) == 6 and segments[3] == "accounts" and segments[5] == "session" and method == "GET":
                        ig_u = sanitize_username(segments[4])
                        slot = ws.slots.get(ig_u)
                        session_obj = await slot.get_session_json() if slot else None
                        if not session_obj:
                            _send_json(writer, 404, {"error": "No saved session for this Instagram account."})
                            await writer.drain()
                            return
                        raw_bytes = json.dumps(session_obj, indent=2, default=str).encode("utf-8")
                        headers = (
                            "HTTP/1.1 200 OK\r\n"
                            "Content-Type: application/json; charset=utf-8\r\n"
                            f'Content-Disposition: attachment; filename="{ig_u}_session.json"\r\n'
                            f"Content-Length: {len(raw_bytes)}\r\n"
                            "Connection: close\r\n\r\n"
                        ).encode("utf-8")
                        writer.write(headers + raw_bytes)
                        await writer.drain()
                        return

                    if subpath == "logs" and method == "GET":
                        try:
                            since = int((query.get("since") or ["0"])[0])
                        except ValueError:
                            since = 0
                        _send_json(writer, 200, {"logs": get_user_logs(ws.username, since_id=since)})
                        await writer.drain()
                        return

                    if subpath == "logs/clear" and method == "POST":
                        clear_user_logs(ws.username)
                        _send_json(writer, 200, {"ok": True})
                        await writer.drain()
                        return

            resp_body = UI_HTML_PATH.read_bytes()
            headers = (
                "HTTP/1.1 200 OK\r\n"
                "Content-Type: text/html; charset=utf-8\r\n"
                f"Content-Length: {len(resp_body)}\r\n"
                "Cache-Control: no-store\r\n"
                "Connection: close\r\n\r\n"
            ).encode("utf-8")
            writer.write(headers + resp_body)
            await writer.drain()
        except Exception as e:
            try:
                _send_json(writer, 400, {"error": str(e)})
                await writer.drain()
            except Exception:
                pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    global _EARLY_SOCK
    if _EARLY_SOCK is not None:
        sock = _EARLY_SOCK
        _EARLY_SOCK = None
        server = await asyncio.start_server(handle_client, sock=sock)
    else:
        server = await asyncio.start_server(handle_client, host, port, reuse_address=True)
    web_log.info("Multi-user web dashboard listening on http://%s:%d", host, port)
    return server


async def main() -> None:
    base_cfg = load_config(strict=False)
    setup_logging(base_cfg.log_level)

    manager = MultiUserManager(base_cfg)
    _TokenHub.set_manager(manager)

    port = int(os.getenv("PORT", "3000"))
    web_server = await _start_web_server(manager, "0.0.0.0", port)
    bootstrap_task = asyncio.create_task(manager.bootstrap_existing())

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    log.info("Hyper Insta Multi-User Bridge is ready on port %d.", port)
    try:
        await stop.wait()
    except asyncio.CancelledError:
        pass
    finally:
        log.info("Shutting down...")
        bootstrap_task.cancel()
        web_server.close()
        await web_server.wait_closed()
        await manager.stop_all()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
