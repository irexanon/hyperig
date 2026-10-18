import asyncio
import json
import os
import signal
import socket
from datetime import datetime, timezone
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
from tg.bridge import TelegramBridge
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


class InstagramSlot:
    """One Instagram account instance inside a User's workspace database."""

    def __init__(self, user_id: str, ig_username: str, base_cfg: Config, store: Store, saved_cfg: dict):
        self.user_id = user_id
        self.ig_username = ig_username
        self.base_cfg = base_cfg
        self.store = store
        self.saved_cfg = dict(saved_cfg or {})
        self.saved_cfg["ig_username"] = ig_username
        self.cfg = build_user_config(base_cfg, self.saved_cfg, ig_username)

        self.ig = InstagramClient(self.cfg, self.store)
        self.bridge = TelegramBridge(self.cfg, self.store, username=user_id)
        self.ctx = AppContext(cfg=self.cfg, ig=self.ig, store=self.store, bridge=self.bridge)
        self.modules = ModuleManager(self.ctx)
        self.ctx.modules = self.modules
        self.modules.load_modules()
        self.handler = MessageHandler(self.modules, self.bridge)

        self.ig.on_alert = self.bridge.alert
        self.ig.realtime.on_typing = self.bridge.typing
        self._poller_task: asyncio.Task | None = None
        self.login_in_progress = False

    async def start(self) -> None:
        tok = current_user_var.set(self.user_id)
        try:
            await self.bridge.start(self.ig, self.modules)
            await self.ig.restore_saved_session()
            self._poller_task = asyncio.create_task(self._run_poller())
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


class UserWorkspace:
    """A User profile (identified by username, no password required) with its own DB and multiple Instagram accounts."""

    def __init__(self, username: str, base_cfg: Config, store: Store):
        self.username = username
        self.base_cfg = base_cfg
        self.store = store
        self.tg_chat_id: int = 0
        self.tg_admin_id: int = 0
        self.tg_token: str = ""
        self.slots: dict[str, InstagramSlot] = {}
        self.active_ig: str = ""
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        tok = current_user_var.set(self.username)
        try:
            profile = await self.store.get("profile")
            if not isinstance(profile, dict):
                legacy_cfg = await self.store.get("user_config")
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
                profile = {
                    "tg_chat_id": tg_chat_id,
                    "tg_admin_id": tg_admin_id,
                    "tg_token": tg_token,
                    "accounts": accounts,
                    "active_account": accounts[0] if accounts else "",
                }
                await self.store.set("profile", profile)

            try:
                self.tg_chat_id = int(profile.get("tg_chat_id") or 0)
            except ValueError:
                self.tg_chat_id = 0
            try:
                self.tg_admin_id = int(profile.get("tg_admin_id") or 0)
            except ValueError:
                self.tg_admin_id = 0
            self.tg_token = str(profile.get("tg_token") or "")

            account_names = [sanitize_username(a) for a in (profile.get("accounts") or []) if sanitize_username(a)]
            self.active_ig = sanitize_username(profile.get("active_account") or "")
            add_user_log(self.username, "INFO", "workspace", f"Loaded database for user '{self.username}'")

            for ig_u in account_names:
                await self._init_slot(ig_u)
            if not self.active_ig and self.slots:
                self.active_ig = next(iter(self.slots))
        finally:
            current_user_var.reset(tok)

    async def _save_profile(self) -> None:
        await self.store.set(
            "profile",
            {
                "tg_chat_id": self.tg_chat_id,
                "tg_admin_id": self.tg_admin_id,
                "tg_token": self.tg_token,
                "accounts": list(self.slots.keys()),
                "active_account": self.active_ig,
            },
        )

    async def _init_slot(self, ig_username: str) -> InstagramSlot:
        if ig_username in self.slots:
            return self.slots[ig_username]
        scoped = ScopedStore(self.store, f"acct_{ig_username}_")
        saved_cfg = await scoped.get("user_config", {}) or {}
        if self.tg_chat_id:
            saved_cfg["tg_chat_id"] = self.tg_chat_id
        if self.tg_admin_id:
            saved_cfg["tg_admin_id"] = self.tg_admin_id
        if self.tg_token:
            saved_cfg["tg_token"] = self.tg_token
        slot = InstagramSlot(self.username, ig_username, self.base_cfg, scoped, saved_cfg)
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
            await self._save_profile()
            return slot

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
            await self._save_profile()

    async def update_telegram(self, payload: dict) -> None:
        tok = current_user_var.set(self.username)
        try:
            raw_chat = str(payload.get("tg_chat_id") or "").strip()
            raw_admin = str(payload.get("tg_admin_id") or "").strip()
            raw_token = str(payload.get("tg_token") or "").strip()
            try:
                self.tg_chat_id = int(raw_chat) if raw_chat else 0
            except ValueError:
                raise ValueError("Telegram Chat ID must be a number (e.g. -1001234567890).")
            try:
                self.tg_admin_id = int(raw_admin) if raw_admin else 0
            except ValueError:
                raise ValueError("Telegram Admin ID must be a numeric user ID.")
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
                if raw_token == "__CLEAR__":
                    updates["tg_token"] = "__CLEAR__"
                elif self.tg_token:
                    updates["tg_token"] = self.tg_token
                await slot.apply_config(updates)
            add_user_log(
                self.username,
                "INFO",
                "telegram",
                f"Updated Telegram routing (Chat ID={self.tg_chat_id or 'none'}, Admin ID={self.tg_admin_id or 'any'}).",
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
        tg_err = next((a["tg_error"] for a in accounts if a["tg_error"]), "")
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
            "accounts": accounts,
        }


class MultiUserManager:
    """Manages per-username workspaces and databases."""

    def __init__(self, base_cfg: Config):
        self.base_cfg = base_cfg
        self.workspaces: dict[str, UserWorkspace] = {}
        self._lock = asyncio.Lock()

    async def bootstrap_existing(self) -> None:
        names = list_registered_users(self.base_cfg)
        for name in names:
            try:
                await self.get_or_create(name)
            except Exception as e:
                log.warning("Could not restore workspace %s: %s", name, e)

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


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Hyper Insta Bridge</title>
  <meta name="description" content="Bridges Instagram Direct Messages to Telegram forum topics using instagrapi, MQTT/FBNS realtime delivery, and python-telegram-bot." />
  <meta property="og:title" content="Hyper Insta Bridge" />
  <meta property="og:description" content="Bridges Instagram Direct Messages to Telegram forum topics using instagrapi, MQTT/FBNS realtime delivery, and python-telegram-bot." />
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600&family=Plus+Jakarta+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg: #090B10;
      --surface: #10141D;
      --surface-elevated: #161B26;
      --input: #0B0E15;
      --border: rgba(255, 255, 255, 0.08);
      --border-strong: rgba(255, 255, 255, 0.14);
      --border-focus: #38BDF8;
      --text: #F3F4F6;
      --text-secondary: #9CA3AF;
      --text-dim: #64748B;
      --accent: #0EA5E9;
      --accent-hover: #0284C7;
      --ok: #10B981;
      --warn: #F59E0B;
      --err: #EF4444;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: 'Plus Jakarta Sans', -apple-system, BlinkMacSystemFont, sans-serif;
      font-size: 13.5px;
      line-height: 1.5;
      min-height: 100vh;
      display: flex;
      flex-direction: column;
      -webkit-font-smoothing: antialiased;
    }
    .mono {
      font-family: 'JetBrains Mono', monospace;
      font-variant-numeric: tabular-nums;
    }

    /* Top Bar Contract: 3 zones */
    header.topbar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 32px;
      padding: 13px 28px;
      border-bottom: 1px solid var(--border);
      background: var(--surface);
    }
    .brand {
      font-weight: 700;
      font-size: 16px;
      color: var(--text);
      text-decoration: none;
      letter-spacing: -0.02em;
      white-space: nowrap;
      flex-shrink: 0;
    }
    nav.view-nav {
      display: flex;
      align-items: center;
      gap: 24px;
    }
    nav.view-nav a {
      color: var(--text-secondary);
      text-decoration: none;
      font-size: 13px;
      font-weight: 500;
      white-space: nowrap;
      padding: 4px 0;
      border-bottom: 2px solid transparent;
      transition: color 150ms ease, border-color 150ms ease;
    }
    nav.view-nav a:hover, nav.view-nav a.active {
      color: var(--text);
      border-bottom-color: var(--accent);
    }

    /* App Layout */
    .shell {
      display: grid;
      grid-template-columns: 265px 1fr;
      flex: 1;
      max-width: 1440px;
      width: 100%;
      margin: 0 auto;
    }
    aside.sidebar {
      background: var(--surface);
      border-right: 1px solid var(--border);
      padding: 20px 18px;
      display: flex;
      flex-direction: column;
      gap: 22px;
    }
    main.content {
      padding: 24px 28px 40px;
      display: flex;
      flex-direction: column;
      gap: 20px;
      overflow-y: auto;
    }
    @media (max-width: 960px) {
      .shell { grid-template-columns: 1fr; }
      aside.sidebar { border-right: none; border-bottom: 1px solid var(--border); }
      nav.view-nav { display: none; }
    }

    /* Controls */
    .label-title {
      font-size: 11.5px;
      font-weight: 600;
      color: var(--text-secondary);
      margin-bottom: 8px;
    }
    label {
      display: flex;
      justify-content: space-between;
      font-size: 12px;
      font-weight: 500;
      color: var(--text-secondary);
      margin-bottom: 5px;
    }
    input[type="text"], input[type="password"], input[type="number"], textarea, select {
      width: 100%;
      background: var(--input);
      border: 1px solid var(--border);
      color: var(--text);
      padding: 8.5px 11px;
      border-radius: 6px;
      font-size: 13px;
      font-family: inherit;
      outline: none;
      transition: border-color 150ms ease;
    }
    input:focus, textarea:focus, select:focus {
      border-color: var(--border-focus);
    }
    button {
      background: var(--surface-elevated);
      color: var(--text);
      border: 1px solid var(--border-strong);
      padding: 8px 14px;
      border-radius: 6px;
      font-size: 12.5px;
      font-weight: 600;
      font-family: inherit;
      cursor: pointer;
      white-space: nowrap;
      flex-shrink: 0;
      transition: background 150ms ease, opacity 150ms ease;
    }
    button:hover:not(:disabled) {
      background: #1F2637;
    }
    button:disabled {
      opacity: 0.42;
      cursor: not-allowed;
    }
    button.primary {
      background: var(--accent);
      border-color: var(--accent);
      color: #06090F;
    }
    button.primary:hover:not(:disabled) {
      background: var(--accent-hover);
      color: #fff;
    }
    button.danger {
      background: transparent;
      color: var(--err);
      border-color: rgba(239, 68, 68, 0.35);
    }

    /* Sidebar Lists */
    .item-list {
      display: flex;
      flex-direction: column;
      gap: 4px;
    }
    .item-btn {
      display: flex;
      align-items: center;
      justify-content: space-between;
      width: 100%;
      text-align: left;
      padding: 8px 10px;
      border-radius: 6px;
      background: transparent;
      border: 1px solid transparent;
      color: var(--text-secondary);
      font-weight: 500;
      font-size: 13px;
    }
    .item-btn:hover {
      background: rgba(255, 255, 255, 0.03);
      color: var(--text);
    }
    .item-btn.active {
      background: rgba(14, 165, 233, 0.12);
      border-color: rgba(14, 165, 233, 0.4);
      color: var(--text);
    }

    /* Panels */
    .panel {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 20px 22px;
    }
    .panel-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding-bottom: 14px;
      margin-bottom: 16px;
      border-bottom: 1px solid var(--border);
      flex-wrap: wrap;
    }
    .panel-title {
      margin: 0;
      font-size: 15px;
      font-weight: 600;
    }
    .meta-row {
      font-size: 12px;
      color: var(--text-secondary);
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
    }

    .grid-2 {
      display: grid;
      grid-template-columns: 1.15fr 0.85fr;
      gap: 20px;
      align-items: start;
    }
    .grid-equal {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 20px;
      align-items: start;
    }
    @media (max-width: 1080px) {
      .grid-2, .grid-equal { grid-template-columns: 1fr; }
    }
    .form-row-2 {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 10px;
    }
    .field { margin-bottom: 12px; }

    /* Segmented Tabs */
    .segmented {
      display: grid;
      grid-template-columns: repeat(3, 1fr);
      background: var(--input);
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 3px;
      margin-bottom: 16px;
    }
    .segmented button {
      border: none;
      background: transparent;
      color: var(--text-secondary);
      padding: 6px 10px;
      border-radius: 4px;
      font-size: 12px;
    }
    .segmented button.active {
      background: var(--surface-elevated);
      color: var(--text);
    }

    /* Two Switches */
    .switch-row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 12px 0;
      border-bottom: 1px solid var(--border);
    }
    .switch-row:last-of-type { border-bottom: none; }
    input[type="checkbox"].switch {
      width: 38px;
      height: 20px;
      appearance: none;
      background: #273042;
      border-radius: 12px;
      position: relative;
      cursor: pointer;
      transition: background 150ms ease;
      flex-shrink: 0;
    }
    input[type="checkbox"].switch::after {
      content: "";
      position: absolute;
      top: 2px;
      left: 2px;
      width: 16px;
      height: 16px;
      background: #fff;
      border-radius: 50%;
      transition: transform 150ms ease;
    }
    input[type="checkbox"].switch:checked { background: var(--accent); }
    input[type="checkbox"].switch:checked::after { transform: translateX(18px); }

    /* Collapsible Details */
    details.adv-box {
      border: 1px solid var(--border);
      border-radius: 6px;
      background: var(--input);
      padding: 8px 12px;
      margin-bottom: 12px;
    }
    details.adv-box summary {
      cursor: pointer;
      font-size: 12px;
      font-weight: 500;
      color: var(--text-secondary);
      user-select: none;
    }

    /* Simple Live Logs */
    .log-box {
      background: var(--input);
      border: 1px solid var(--border);
      border-radius: 6px;
      height: 240px;
      overflow-y: auto;
      padding: 12px 14px;
      font-size: 12px;
      line-height: 1.6;
    }
    .log-line {
      white-space: pre-wrap;
      word-break: break-word;
      padding: 1.5px 0;
    }
    .log-ts { color: var(--text-dim); margin-right: 8px; }
    .log-lvl-INFO { color: #38BDF8; margin-right: 6px; }
    .log-lvl-WARNING { color: var(--warn); margin-right: 6px; }
    .log-lvl-ERROR { color: var(--err); font-weight: 600; margin-right: 6px; }

    .text-ok { color: var(--ok); }
    .text-warn { color: var(--warn); }
    .text-err { color: var(--err); }
  </style>
</head>
<body>
  <!-- Top Bar: 3 zones -->
  <header class="topbar">
    <a href="#" class="brand">Hyper Insta Bridge</a>
    <nav class="view-nav">
      <a href="#sec-accounts" class="active">Instagram Accounts</a>
      <a href="#sec-telegram">Telegram &amp; Topics</a>
      <a href="#sec-session">Session &amp; Device</a>
      <a href="#sec-logs">Live Logs</a>
    </nav>
    <div>
      <button class="primary" id="btn-top-download" onclick="downloadActiveSession()" disabled>Download Session JSON</button>
    </div>
  </header>

  <div class="shell">
    <!-- Sidebar: 1) User Profile DB (no password) + 2) Multiple Instagram Accounts inside that User DB -->
    <aside class="sidebar">
      <div>
        <div class="label-title">User Database (No Password)</div>
        <div style="display: flex; gap: 6px; margin-bottom: 8px;">
          <input type="text" id="user-input" class="mono" placeholder="Enter username..." />
          <button class="primary" onclick="openUserFromSidebar()">Open</button>
        </div>
        <div id="user-list" class="item-list"></div>
      </div>

      <div id="sidebar-ig-section" style="display: none; flex: 1; flex-direction: column;">
        <div style="display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px;">
          <span class="label-title" style="margin: 0;">Instagram Accounts</span>
          <button onclick="selectIgAccount('__NEW__')" style="padding: 3px 8px; font-size: 11px;">+ Add IG</button>
        </div>
        <div id="ig-account-list" class="item-list"></div>
      </div>

      <div style="border-top: 1px solid var(--border); padding-top: 12px; font-size: 12px; color: var(--text-secondary);">
        <div>User DB: <strong id="meta-active-user" class="mono">None</strong></div>
        <div id="meta-storage-path" class="mono" style="font-size: 11px; color: var(--text-dim); margin-top: 2px;"></div>
      </div>
    </aside>

    <!-- Main Content -->
    <main class="content">
      <!-- Welcome / Empty State -->
      <div id="empty-state" class="panel" style="text-align: center; padding: 52px 24px;">
        <h2 style="margin: 0 0 8px 0; font-size: 19px;">Enter Your Username to Open Your Database</h2>
        <p style="color: var(--text-secondary); max-width: 480px; margin: 0 auto 20px;">
          No registration password needed. Your username creates an isolated database where you can add multiple Instagram accounts, link your Telegram Forum Chat ID, download/import sessions, and monitor live logs.
        </p>
        <div style="display: flex; gap: 8px; max-width: 340px; margin: 0 auto;">
          <input type="text" id="hero-user-input" class="mono" placeholder="e.g. arshman" />
          <button class="primary" onclick="openUserFromHero()">Open User DB</button>
        </div>
      </div>

      <!-- Active User Workspace -->
      <div id="user-workspace" style="display: none; flex-direction: column; gap: 20px;">

        <!-- 2FA / Challenge Code Banner (Appears automatically when Instagram requests a verification code) -->
        <div id="code-banner" style="display: none; background: rgba(245, 158, 11, 0.1); border: 1px solid rgba(245, 158, 11, 0.4); border-radius: 8px; padding: 14px 18px; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap;">
          <div>
            <div style="font-weight: 600; color: var(--warn);">Verification / 2FA Code Requested by Instagram</div>
            <div id="code-hint" style="font-size: 12px; color: var(--text-secondary); margin-top: 2px;"></div>
          </div>
          <div style="display: flex; gap: 8px;">
            <input type="text" id="code-input" class="mono" placeholder="Enter 6 or 8 digit code" style="width: 170px;" />
            <button class="primary" onclick="submitCode()">Submit Code</button>
          </div>
        </div>

        <!-- Row 1: Instagram Account Login + Realtime & FBNS Control -->
        <section id="sec-accounts" class="grid-2">
          <!-- Left: Instagram Account Login (Password / Cookie / Session JSON Import) -->
          <div class="panel">
            <div class="panel-head">
              <h2 class="panel-title" id="login-panel-title">Instagram Account Login</h2>
              <div class="meta-row">
                <span id="slot-status-text" class="text-warn">Not logged in</span>
              </div>
            </div>

            <div class="segmented">
              <button id="tab-pw" class="active" onclick="setAuthTab('password')">Password Login</button>
              <button id="tab-ck" onclick="setAuthTab('cookie')">Cookie Login</button>
              <button id="tab-js" onclick="setAuthTab('json')">Import Session JSON</button>
            </div>

            <div class="field">
              <label>Instagram Username</label>
              <input type="text" id="ig-username" class="mono" placeholder="instagram_handle" />
            </div>

            <!-- Mode 1: Password -->
            <div id="pane-password">
              <div class="form-row-2">
                <div class="field">
                  <label>Instagram Password <span id="pw-saved-tag" class="text-ok"></span></label>
                  <input type="password" id="ig-password" placeholder="Enter password" />
                </div>
                <div class="field">
                  <label>HTTP / SOCKS5 Proxy (Optional)</label>
                  <input type="text" id="ig-proxy" class="mono" placeholder="http://user:pass@host:port" />
                </div>
              </div>

              <details class="adv-box">
                <summary>2FA TOTP Secret &amp; Device Region (Optional)</summary>
                <div class="form-row-2" style="margin-top: 10px;">
                  <div class="field">
                    <label>2FA TOTP Secret (Base32) <span id="totp-saved-tag" class="text-ok"></span></label>
                    <input type="password" id="ig-totp" class="mono" placeholder="Auto-fills 2FA codes" />
                  </div>
                  <div class="field">
                    <label>Phone Number (Challenge)</label>
                    <input type="text" id="ig-phone" class="mono" placeholder="+1..." />
                  </div>
                </div>
                <div class="form-row-2">
                  <div class="field" style="margin-bottom: 0;">
                    <label>Locale &amp; Country</label>
                    <div style="display: flex; gap: 6px;">
                      <input type="text" id="ig-locale" class="mono" placeholder="en_US" />
                      <input type="text" id="ig-country" class="mono" placeholder="US" style="width: 75px;" />
                    </div>
                  </div>
                  <div class="field" style="margin-bottom: 0;">
                    <label>Timezone Offset (Sec)</label>
                    <input type="number" id="ig-tz" class="mono" placeholder="e.g. 18000" />
                  </div>
                </div>
              </details>

              <div style="display: flex; align-items: center; justify-content: space-between; gap: 10px; margin-top: 12px; flex-wrap: wrap;">
                <span style="font-size: 12px; color: var(--text-dim);">Auto-enables Realtime &amp; FBNS</span>
                <div style="display: flex; gap: 8px;">
                  <button onclick="reloginSlot()" id="btn-relogin" disabled>Relogin</button>
                  <button class="primary" id="btn-pw-login" onclick="loginPassword()">Login with Password</button>
                </div>
              </div>
            </div>

            <!-- Mode 2: Cookie -->
            <div id="pane-cookie" style="display: none;">
              <div class="field">
                <label>Browser Cookie (sessionid value, Cookie header, or JSON cookie array)</label>
                <textarea id="ig-cookie" class="mono" rows="3" placeholder="123456789%3AAbCdEf..."></textarea>
              </div>
              <div class="field">
                <label>HTTP / SOCKS5 Proxy (Optional)</label>
                <input type="text" id="ig-cookie-proxy" class="mono" placeholder="http://user:pass@host:port" />
              </div>
              <div style="display: flex; align-items: center; justify-content: space-between; gap: 10px; margin-top: 12px;">
                <span style="font-size: 12px; color: var(--text-dim);">Auto-disables Realtime &amp; FBNS</span>
                <button class="primary" id="btn-ck-login" onclick="loginCookie()">Login with Cookie</button>
              </div>
            </div>

            <!-- Mode 3: Session JSON Import -->
            <div id="pane-json" style="display: none;">
              <div class="field">
                <label>Paste instagrapi Session JSON or Upload File</label>
                <textarea id="ig-session-json" class="mono" rows="3" placeholder='{"uuids": {...}, "authorization_data": {...}}'></textarea>
              </div>
              <div style="display: flex; align-items: center; justify-content: space-between; gap: 10px; flex-wrap: wrap;">
                <input type="file" accept=".json,application/json" onchange="loadJsonFile(event)" style="font-size: 12px; max-width: 220px;" />
                <button class="primary" onclick="importSession()">Import &amp; Verify Session</button>
              </div>
            </div>

            <div id="auth-feedback" style="font-size: 12px; margin-top: 10px; min-height: 18px;"></div>
          </div>

          <!-- Right: Selected Instagram Account Controls (Realtime & FBNS + Session Actions) -->
          <div class="panel">
            <div class="panel-head">
              <div>
                <h2 class="panel-title mono" id="right-ig-title">@account</h2>
                <div class="meta-row" id="right-ig-meta" style="margin-top: 3px;">Select or login an account</div>
              </div>
              <button onclick="downloadActiveSession()" id="btn-card-download" disabled>Download Session</button>
            </div>

            <!-- Only the 2 requested toggles: Realtime and FBNS -->
            <div class="switch-row">
              <div>
                <div style="font-weight: 600; font-size: 13.5px;">Realtime MQTT</div>
                <div style="font-size: 12px; color: var(--text-secondary);" id="rt-desc">Live DM &amp; typing stream (auto-ON for password)</div>
              </div>
              <input type="checkbox" class="switch" id="sw-realtime" onchange="toggleRealtime('realtime', this.checked)" disabled />
            </div>

            <div class="switch-row">
              <div>
                <div style="font-weight: 600; font-size: 13.5px;">FBNS Push</div>
                <div style="font-size: 12px; color: var(--text-secondary);" id="fbns-desc">Background push notifications (auto-ON for password)</div>
              </div>
              <input type="checkbox" class="switch" id="sw-fbns" onchange="toggleRealtime('fbns', this.checked)" disabled />
            </div>

            <div id="sec-session" style="margin-top: 16px; padding-top: 14px; border-top: 1px solid var(--border);">
              <div class="label-title">Session &amp; Device Info</div>
              <pre id="device-info-pre" class="mono" style="background: var(--input); border: 1px solid var(--border); border-radius: 6px; padding: 10px 12px; font-size: 11.5px; color: var(--text-secondary); margin: 0 0 14px 0; white-space: pre-wrap;">No active session</pre>
              <div style="display: flex; gap: 8px; flex-wrap: wrap;">
                <button id="btn-reset-device" onclick="resetDevice()" disabled>Reset Device UUID</button>
                <button id="btn-logout-slot" onclick="logoutSlot()" disabled>Logout</button>
                <button id="btn-remove-slot" class="danger" onclick="removeSlot()" style="margin-left: auto;" disabled>Remove Account</button>
              </div>
            </div>
          </div>
        </section>

        <!-- Row 2: Telegram Forum Group Config + Bridged Topics & Bot Commands -->
        <section id="sec-telegram" class="grid-equal">
          <div class="panel">
            <div class="panel-head">
              <h2 class="panel-title">Telegram Forum Group Bridge</h2>
              <div class="meta-row">
                <span id="tg-conn-label" class="text-warn">Not connected</span>
              </div>
            </div>

            <div class="form-row-2">
              <div class="field">
                <label>Telegram Forum Chat ID</label>
                <input type="text" id="tg-chat-id" class="mono" placeholder="-1001234567890" />
              </div>
              <div class="field">
                <label>Admin User ID (Optional)</label>
                <input type="text" id="tg-admin-id" class="mono" placeholder="Numeric user ID" />
              </div>
            </div>

            <div class="field">
              <label>Custom Bot Token (Optional — leave blank to use default bot)</label>
              <input type="password" id="tg-token" class="mono" placeholder="Uses server bot token if empty" />
            </div>

            <div style="display: flex; align-items: center; justify-content: space-between; gap: 10px; margin-top: 10px;">
              <span id="tg-feedback" style="font-size: 12px; color: var(--text-secondary);"></span>
              <button class="primary" onclick="saveTelegram()">Save Telegram Settings</button>
            </div>
          </div>

          <div class="panel">
            <div class="panel-head">
              <h2 class="panel-title">Bridged Topics &amp; Bot Commands</h2>
              <div class="meta-row">
                <span id="topics-count-label" class="mono">0 linked topics</span>
              </div>
            </div>

            <div style="display: flex; gap: 6px; margin-bottom: 12px; flex-wrap: wrap;">
              <input type="text" id="cmd-input" class="mono" placeholder="Run command (/status, /session, /ping, /help)" style="flex: 1;" />
              <button class="primary" onclick="runBotCommand()">Run</button>
              <button onclick="quickCmd('/status')">/status</button>
              <button onclick="quickCmd('/ping')">/ping</button>
            </div>

            <div id="topics-list" style="background: var(--input); border: 1px solid var(--border); border-radius: 6px; max-height: 135px; overflow-y: auto; padding: 8px 12px; font-size: 12px;"></div>
          </div>
        </section>

        <!-- Row 3: Simple Live Logs (No filter clutter) -->
        <section id="sec-logs" class="panel">
          <div class="panel-head" style="margin-bottom: 12px; padding-bottom: 10px;">
            <h2 class="panel-title">Live Logs</h2>
            <div style="display: flex; gap: 8px;">
              <button onclick="downloadLogs()" style="padding: 5px 10px; font-size: 12px;">Download</button>
              <button onclick="clearLogs()" style="padding: 5px 10px; font-size: 12px;">Clear</button>
            </div>
          </div>
          <div id="log-box" class="log-box mono"></div>
        </section>

      </div>
    </main>
  </div>

  <script>
    let currentUser = localStorage.getItem('hib_active_user') || '';
    let activeIg = '';
    let authTab = 'password';
    let lastLogId = 0;
    let logs = [];
    let hydratedKey = '';

    async function api(path, options = {}) {
      const res = await fetch(path, {
        headers: { 'Content-Type': 'application/json' },
        ...options
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || ('HTTP ' + res.status));
      return data;
    }

    function setAuthTab(tab) {
      authTab = tab;
      document.getElementById('tab-pw').className = tab === 'password' ? 'active' : '';
      document.getElementById('tab-ck').className = tab === 'cookie' ? 'active' : '';
      document.getElementById('tab-js').className = tab === 'json' ? 'active' : '';
      document.getElementById('pane-password').style.display = tab === 'password' ? 'block' : 'none';
      document.getElementById('pane-cookie').style.display = tab === 'cookie' ? 'block' : 'none';
      document.getElementById('pane-json').style.display = tab === 'json' ? 'block' : 'none';
    }

    async function loadUsers() {
      try {
        const data = await api('/api/users');
        const users = data.users || [];
        const userListEl = document.getElementById('user-list');

        if (!currentUser && users.length > 0) {
          currentUser = users[0].username;
          localStorage.setItem('hib_active_user', currentUser);
        }

        userListEl.innerHTML = users.map(u => {
          const isAct = u.username === currentUser;
          return `
            <button class="item-btn ${isAct ? 'active' : ''}" onclick="switchUser('${u.username}')">
              <span class="mono">${u.username}</span>
              <span style="font-size: 11px; color: var(--text-dim);">${u.logged_in_count}/${u.account_count} IG</span>
            </button>
          `;
        }).join('');

        if (currentUser) {
          document.getElementById('empty-state').style.display = 'none';
          document.getElementById('user-workspace').style.display = 'flex';
          document.getElementById('sidebar-ig-section').style.display = 'flex';
          await refreshState();
        } else {
          document.getElementById('empty-state').style.display = 'block';
          document.getElementById('user-workspace').style.display = 'none';
          document.getElementById('sidebar-ig-section').style.display = 'none';
        }
      } catch (e) {
        console.error(e);
      }
    }

    async function openUser(raw) {
      const clean = (raw || '').trim();
      if (!clean) return;
      const res = await api('/api/users', {
        method: 'POST',
        body: JSON.stringify({ username: clean })
      });
      currentUser = res.username;
      localStorage.setItem('hib_active_user', currentUser);
      activeIg = res.state.active_ig || '';
      logs = [];
      lastLogId = 0;
      hydratedKey = '';
      await loadUsers();
    }

    function openUserFromSidebar() {
      const inp = document.getElementById('user-input');
      openUser(inp.value);
      inp.value = '';
    }

    function openUserFromHero() {
      const inp = document.getElementById('hero-user-input');
      openUser(inp.value);
    }

    async function switchUser(uname) {
      currentUser = uname;
      localStorage.setItem('hib_active_user', currentUser);
      activeIg = '';
      logs = [];
      lastLogId = 0;
      hydratedKey = '';
      await loadUsers();
    }

    function selectIgAccount(igName) {
      activeIg = igName;
      hydratedKey = '';
      document.getElementById('auth-feedback').textContent = '';
      if (igName === '__NEW__') {
        document.getElementById('ig-username').value = '';
        document.getElementById('ig-password').value = '';
        document.getElementById('ig-cookie').value = '';
        document.getElementById('ig-proxy').value = '';
        document.getElementById('ig-cookie-proxy').value = '';
      }
      refreshState();
    }

    async function refreshState() {
      if (!currentUser) return;
      try {
        const st = await api(`/api/users/${encodeURIComponent(currentUser)}/state`);
        document.getElementById('meta-active-user').textContent = st.username;
        document.getElementById('meta-storage-path').textContent = st.storage_backend;

        const accounts = st.accounts || [];
        if (!activeIg) {
          activeIg = st.active_ig || (accounts[0] ? accounts[0].ig_username : '__NEW__');
        }
        if (activeIg !== '__NEW__' && !accounts.some(a => a.ig_username === activeIg)) {
          activeIg = accounts[0] ? accounts[0].ig_username : '__NEW__';
        }

        // Sidebar Instagram accounts list
        const igListEl = document.getElementById('ig-account-list');
        if (accounts.length === 0) {
          igListEl.innerHTML = '<div style="font-size: 12px; color: var(--text-dim); padding: 4px 0;">No Instagram accounts added yet.</div>';
        } else {
          igListEl.innerHTML = accounts.map(a => {
            const isAct = a.ig_username === activeIg;
            const stTxt = a.ig_logged_in ? 'Online' : 'Offline';
            const stCls = a.ig_logged_in ? 'text-ok' : 'text-warn';
            return `
              <button class="item-btn ${isAct ? 'active' : ''}" onclick="selectIgAccount('${a.ig_username}')">
                <span class="mono">@${a.ig_username}</span>
                <span class="${stCls}" style="font-size: 11px;">${stTxt}</span>
              </button>
            `;
          }).join('');
        }

        // Telegram section
        const tgLabel = document.getElementById('tg-conn-label');
        if (st.tg_running) {
          tgLabel.textContent = `Connected${st.tg_bot_username ? ' (@' + st.tg_bot_username + ')' : ''}`;
          tgLabel.className = 'text-ok';
        } else if (st.tg_chat_id) {
          tgLabel.textContent = st.tg_error || `Chat ID ${st.tg_chat_id} saved`;
          tgLabel.className = 'text-warn';
        } else {
          tgLabel.textContent = 'Enter Telegram Forum Chat ID';
          tgLabel.className = 'text-warn';
        }

        const slot = accounts.find(a => a.ig_username === activeIg) || null;
        const hKey = `${currentUser}:${activeIg}`;
        if (hydratedKey !== hKey) {
          hydratedKey = hKey;
          document.getElementById('tg-chat-id').value = st.tg_chat_id || '';
          document.getElementById('tg-admin-id').value = st.tg_admin_id || '';
          document.getElementById('tg-token').value = '';
          document.getElementById('ig-username').value = slot ? slot.ig_username : '';
          document.getElementById('ig-password').value = '';
          document.getElementById('ig-proxy').value = slot ? slot.proxy : '';
          document.getElementById('ig-cookie-proxy').value = slot ? slot.proxy : '';
          document.getElementById('ig-phone').value = slot ? slot.phone_number : '';
          document.getElementById('ig-locale').value = slot ? slot.locale : 'en_US';
          document.getElementById('ig-country').value = slot ? slot.country : 'US';
          document.getElementById('ig-tz').value = slot ? slot.timezone_offset : '';
        }

        const codeBanner = document.getElementById('code-banner');
        if (slot && slot.waiting_for_code) {
          codeBanner.style.display = 'flex';
          document.getElementById('code-hint').textContent = slot.code_hint || 'Enter the verification code sent by Instagram.';
        } else {
          codeBanner.style.display = 'none';
        }

        if (slot) {
          document.getElementById('login-panel-title').textContent = `Manage @${slot.ig_username}`;
          const stText = document.getElementById('slot-status-text');
          stText.textContent = slot.ig_logged_in
            ? `Logged In (${slot.login_method})`
            : (slot.login_in_progress ? 'Logging in...' : 'Logged Out');
          stText.className = slot.ig_logged_in ? 'text-ok' : 'text-warn';

          document.getElementById('pw-saved-tag').textContent = slot.has_password ? '(Saved)' : '';
          document.getElementById('totp-saved-tag').textContent = slot.has_totp ? '(Saved)' : '';

          document.getElementById('right-ig-title').textContent = '@' + slot.ig_username;
          document.getElementById('right-ig-meta').textContent =
            `${slot.linked_topics} topics · ${slot.forwarded} DMs forwarded`;

          const swRt = document.getElementById('sw-realtime');
          const swFb = document.getElementById('sw-fbns');
          swRt.disabled = false;
          swFb.disabled = false;
          swRt.checked = Boolean(slot.realtime);
          swFb.checked = Boolean(slot.fbns);

          document.getElementById('rt-desc').textContent = slot.realtime
            ? (slot.rt_connected ? `Connected (${slot.rt_events} events)` : (slot.rt_error || 'Enabled · standby'))
            : 'Disabled (polling mode)';
          document.getElementById('fbns-desc').textContent = slot.fbns
            ? (slot.fbns_connected ? `Connected (${slot.fbns_pushes} pushes)` : (slot.fbns_error || 'Enabled · standby'))
            : 'Disabled';

          document.getElementById('device-info-pre').textContent = slot.session_info;

          const canDl = slot.ig_logged_in || slot.has_saved_session;
          document.getElementById('btn-top-download').disabled = !canDl;
          document.getElementById('btn-card-download').disabled = !canDl;
          document.getElementById('btn-relogin').disabled = !canDl;
          document.getElementById('btn-reset-device').disabled = false;
          document.getElementById('btn-logout-slot').disabled = !slot.ig_logged_in;
          document.getElementById('btn-remove-slot').disabled = false;

          // Topics list
          document.getElementById('topics-count-label').textContent = `${slot.linked_topics} linked topics`;
          const tBox = document.getElementById('topics-list');
          if (!slot.topics || slot.topics.length === 0) {
            tBox.innerHTML = '<div style="color: var(--text-dim);">No forum topics created yet for this account.</div>';
          } else {
            tBox.innerHTML = slot.topics.map(t => `
              <div style="display: flex; align-items: center; justify-content: space-between; padding: 4px 0; border-bottom: 1px solid var(--border);">
                <span class="mono">${escapeHtml(t.title)} <span style="color: var(--text-dim);">→ Topic #${t.topic_id}</span></span>
                <button onclick="unlinkTopic('${t.thread_id}')" style="padding: 2px 8px; font-size: 11px;">Unlink</button>
              </div>
            `).join('');
          }
        } else {
          document.getElementById('login-panel-title').textContent = 'Add Instagram Account';
          const stText = document.getElementById('slot-status-text');
          stText.textContent = 'New Account';
          stText.className = 'text-warn';
          document.getElementById('right-ig-title').textContent = 'New Account';
          document.getElementById('right-ig-meta').textContent = 'Sign in on the left to create this account slot';
          document.getElementById('device-info-pre').textContent = 'No session loaded yet.';
          document.getElementById('sw-realtime').disabled = true;
          document.getElementById('sw-fbns').disabled = true;
          document.getElementById('sw-realtime').checked = true;
          document.getElementById('sw-fbns').checked = true;
          document.getElementById('btn-top-download').disabled = true;
          document.getElementById('btn-card-download').disabled = true;
          document.getElementById('btn-relogin').disabled = true;
          document.getElementById('btn-reset-device').disabled = true;
          document.getElementById('btn-logout-slot').disabled = true;
          document.getElementById('btn-remove-slot').disabled = true;
          document.getElementById('topics-count-label').textContent = '0 linked topics';
          document.getElementById('topics-list').innerHTML = '<div style="color: var(--text-dim);">Log in to view linked Telegram topics.</div>';
        }

        await fetchLogs();
      } catch (e) {
        console.error(e);
      }
    }

    async function loginPassword() {
      const igUser = document.getElementById('ig-username').value.trim();
      const fb = document.getElementById('auth-feedback');
      const btn = document.getElementById('btn-pw-login');
      if (!igUser) {
        fb.textContent = 'Enter the Instagram username first.';
        fb.className = 'text-warn';
        return;
      }
      btn.disabled = true;
      fb.textContent = 'Logging in with password (Realtime & FBNS auto-enabled)...';
      fb.className = 'text-warn';
      try {
        const res = await api(`/api/users/${encodeURIComponent(currentUser)}/accounts/login`, {
          method: 'POST',
          body: JSON.stringify({
            ig_username: igUser,
            password: document.getElementById('ig-password').value,
            proxy: document.getElementById('ig-proxy').value.trim(),
            totp_secret: document.getElementById('ig-totp').value.trim(),
            phone_number: document.getElementById('ig-phone').value.trim(),
            locale: document.getElementById('ig-locale').value.trim(),
            country: document.getElementById('ig-country').value.trim(),
            timezone_offset: document.getElementById('ig-tz').value.trim()
          })
        });
        activeIg = res.ig_username || igUser;
        hydratedKey = `${currentUser}:${activeIg}`;
        fb.textContent = res.message;
        fb.className = res.ok ? 'text-ok' : 'text-err';
        await loadUsers();
      } catch (e) {
        fb.textContent = e.message;
        fb.className = 'text-err';
      } finally {
        btn.disabled = false;
      }
    }

    async function loginCookie() {
      const igUser = document.getElementById('ig-username').value.trim();
      const cookie = document.getElementById('ig-cookie').value.trim();
      const proxy = document.getElementById('ig-cookie-proxy').value.trim();
      const fb = document.getElementById('auth-feedback');
      const btn = document.getElementById('btn-ck-login');
      if (!igUser || !cookie) {
        fb.textContent = 'Enter both the Instagram username and cookie.';
        fb.className = 'text-warn';
        return;
      }
      btn.disabled = true;
      fb.textContent = 'Logging in with cookie (Realtime & FBNS auto-disabled)...';
      fb.className = 'text-warn';
      try {
        const res = await api(`/api/users/${encodeURIComponent(currentUser)}/accounts/cookie`, {
          method: 'POST',
          body: JSON.stringify({ ig_username: igUser, cookie, proxy })
        });
        activeIg = res.ig_username || igUser;
        hydratedKey = `${currentUser}:${activeIg}`;
        fb.textContent = res.message;
        fb.className = res.ok ? 'text-ok' : 'text-err';
        if (res.ok) document.getElementById('ig-cookie').value = '';
        await loadUsers();
      } catch (e) {
        fb.textContent = e.message;
        fb.className = 'text-err';
      } finally {
        btn.disabled = false;
      }
    }

    function loadJsonFile(ev) {
      const f = ev.target.files && ev.target.files[0];
      if (!f) return;
      const r = new FileReader();
      r.onload = () => { document.getElementById('ig-session-json').value = r.result; };
      r.readAsText(f);
    }

    async function importSession() {
      const igUser = document.getElementById('ig-username').value.trim();
      const rawJson = document.getElementById('ig-session-json').value.trim();
      const proxy = document.getElementById('ig-proxy').value.trim();
      const fb = document.getElementById('auth-feedback');
      if (!igUser || !rawJson) {
        fb.textContent = 'Enter the Instagram username and session JSON.';
        fb.className = 'text-warn';
        return;
      }
      fb.textContent = 'Importing and verifying session JSON...';
      fb.className = 'text-warn';
      try {
        const res = await api(`/api/users/${encodeURIComponent(currentUser)}/accounts/import`, {
          method: 'POST',
          body: JSON.stringify({ ig_username: igUser, session_json: rawJson, proxy })
        });
        activeIg = res.ig_username || igUser;
        hydratedKey = `${currentUser}:${activeIg}`;
        fb.textContent = res.message;
        fb.className = res.ok ? 'text-ok' : 'text-err';
        await loadUsers();
      } catch (e) {
        fb.textContent = e.message;
        fb.className = 'text-err';
      }
    }

    async function toggleRealtime(key, val) {
      if (!currentUser || !activeIg || activeIg === '__NEW__') return;
      await api(`/api/users/${encodeURIComponent(currentUser)}/accounts/toggle`, {
        method: 'POST',
        body: JSON.stringify({ ig_username: activeIg, [key]: val })
      });
      await refreshState();
    }

    async function reloginSlot() {
      if (!currentUser || !activeIg || activeIg === '__NEW__') return;
      const fb = document.getElementById('auth-feedback');
      fb.textContent = 'Re-logging in...';
      fb.className = 'text-warn';
      const res = await api(`/api/users/${encodeURIComponent(currentUser)}/accounts/relogin`, {
        method: 'POST',
        body: JSON.stringify({ ig_username: activeIg })
      });
      fb.textContent = res.message;
      fb.className = res.ok ? 'text-ok' : 'text-err';
      await refreshState();
    }

    async function resetDevice() {
      if (!currentUser || !activeIg || activeIg === '__NEW__') return;
      const fb = document.getElementById('auth-feedback');
      fb.textContent = 'Resetting device identity...';
      fb.className = 'text-warn';
      const res = await api(`/api/users/${encodeURIComponent(currentUser)}/accounts/reset_device`, {
        method: 'POST',
        body: JSON.stringify({ ig_username: activeIg })
      });
      fb.textContent = res.message;
      fb.className = res.ok ? 'text-ok' : 'text-warn';
      await refreshState();
    }

    async function logoutSlot() {
      if (!currentUser || !activeIg || activeIg === '__NEW__') return;
      await api(`/api/users/${encodeURIComponent(currentUser)}/accounts/logout`, {
        method: 'POST',
        body: JSON.stringify({ ig_username: activeIg })
      });
      await refreshState();
    }

    async function removeSlot() {
      if (!currentUser || !activeIg || activeIg === '__NEW__') return;
      await api(`/api/users/${encodeURIComponent(currentUser)}/accounts/remove`, {
        method: 'POST',
        body: JSON.stringify({ ig_username: activeIg })
      });
      activeIg = '';
      hydratedKey = '';
      await loadUsers();
    }

    function downloadActiveSession() {
      if (!currentUser || !activeIg || activeIg === '__NEW__') return;
      const a = document.createElement('a');
      a.href = `/api/users/${encodeURIComponent(currentUser)}/accounts/${encodeURIComponent(activeIg)}/session`;
      a.download = `${activeIg}_session.json`;
      document.body.appendChild(a);
      a.click();
      a.remove();
    }

    async function saveTelegram() {
      const fb = document.getElementById('tg-feedback');
      fb.textContent = 'Saving...';
      try {
        await api(`/api/users/${encodeURIComponent(currentUser)}/telegram`, {
          method: 'POST',
          body: JSON.stringify({
            tg_chat_id: document.getElementById('tg-chat-id').value.trim(),
            tg_admin_id: document.getElementById('tg-admin-id').value.trim(),
            tg_token: document.getElementById('tg-token').value.trim()
          })
        });
        fb.textContent = 'Saved Telegram settings.';
        fb.className = 'text-ok';
        await refreshState();
      } catch (e) {
        fb.textContent = e.message;
        fb.className = 'text-err';
      }
    }

    async function unlinkTopic(threadId) {
      if (!currentUser || !activeIg || activeIg === '__NEW__') return;
      await api(`/api/users/${encodeURIComponent(currentUser)}/accounts/unlink_topic`, {
        method: 'POST',
        body: JSON.stringify({ ig_username: activeIg, thread_id: threadId })
      });
      await refreshState();
    }

    async function quickCmd(cmd) {
      document.getElementById('cmd-input').value = cmd;
      await runBotCommand();
    }

    async function runBotCommand() {
      const inp = document.getElementById('cmd-input');
      const cmd = inp.value.trim();
      if (!cmd || !currentUser) return;
      inp.value = '';
      await api(`/api/users/${encodeURIComponent(currentUser)}/accounts/command`, {
        method: 'POST',
        body: JSON.stringify({ ig_username: activeIg, command: cmd })
      });
      await refreshState();
    }

    async function submitCode() {
      const code = document.getElementById('code-input').value.trim();
      if (!code || !activeIg || activeIg === '__NEW__') return;
      await api(`/api/users/${encodeURIComponent(currentUser)}/accounts/code`, {
        method: 'POST',
        body: JSON.stringify({ ig_username: activeIg, code })
      });
      document.getElementById('code-input').value = '';
      await refreshState();
    }

    async function fetchLogs() {
      if (!currentUser) return;
      try {
        const res = await api(`/api/users/${encodeURIComponent(currentUser)}/logs?since=${lastLogId}`);
        const inc = res.logs || [];
        if (inc.length > 0) {
          logs.push(...inc);
          if (logs.length > 400) logs = logs.slice(-400);
          lastLogId = logs[logs.length - 1].id;
          renderLogs();
        } else if (logs.length === 0) {
          renderLogs();
        }
      } catch (e) {
        console.error(e);
      }
    }

    function renderLogs() {
      const box = document.getElementById('log-box');
      if (logs.length === 0) {
        box.innerHTML = '<div style="color: var(--text-dim);">No log output yet.</div>';
        return;
      }
      const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
      box.innerHTML = logs.map(l =>
        `<div class="log-line"><span class="log-ts">${l.timestamp}</span><span class="log-lvl-${l.level}">${l.level}</span><span>${escapeHtml(l.message)}</span></div>`
      ).join('');
      if (atBottom) box.scrollTop = box.scrollHeight;
    }

    async function clearLogs() {
      if (!currentUser) return;
      await api(`/api/users/${encodeURIComponent(currentUser)}/logs/clear`, { method: 'POST', body: '{}' });
      logs = [];
      lastLogId = 0;
      renderLogs();
    }

    function downloadLogs() {
      if (!currentUser) return;
      const txt = logs.map(l => `${l.timestamp} [${l.level}] ${l.message}`).join('\\n');
      const blob = new Blob([txt], { type: 'text/plain;charset=utf-8' });
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = `${currentUser}_logs.txt`;
      document.body.appendChild(a);
      a.click();
      a.remove();
    }

    function escapeHtml(s) {
      return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    }

    document.getElementById('user-input').addEventListener('keydown', e => {
      if (e.key === 'Enter') openUserFromSidebar();
    });
    document.getElementById('hero-user-input').addEventListener('keydown', e => {
      if (e.key === 'Enter') openUserFromHero();
    });
    document.getElementById('cmd-input').addEventListener('keydown', e => {
      if (e.key === 'Enter') runBotCommand();
    });

    loadUsers();
    setInterval(() => {
      if (currentUser) refreshState();
    }, 3000);
  </script>
</body>
</html>
"""


def _send_json(writer: asyncio.StreamWriter, status_code: int, payload: dict) -> None:
    status_map = {200: "200 OK", 400: "400 Bad Request", 404: "404 Not Found", 500: "500 Internal Server Error"}
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
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=10.0)
                if not line or line in (b"\r\n", b"\n"):
                    break
                header = line.decode("utf-8", errors="replace").strip()
                if header.lower().startswith("content-length:"):
                    try:
                        content_length = int(header.split(":", 1)[1].strip())
                    except ValueError:
                        content_length = 0

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
                _send_json(writer, 200, {"users": manager.list_summaries()})
                await writer.drain()
                return

            if path == "/api/users" and method == "POST":
                data = parse_json_body()
                ws = await manager.get_or_create(str(data.get("username") or ""))
                _send_json(writer, 200, {"username": ws.username, "state": await ws.to_state_dict()})
                await writer.drain()
                return

            if path.startswith("/api/users/"):
                segments = [unquote(s) for s in path.split("/") if s]
                if len(segments) >= 3:
                    uname = sanitize_username(segments[2])
                    subpath = "/".join(segments[3:])
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

                    if subpath == "accounts/login" and method == "POST":
                        data = parse_json_body()
                        slot = await ws.ensure_slot(str(data.get("ig_username") or ""))
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
                        slot = ws.slots.get(sanitize_username(ig_u)) or (next(iter(ws.slots.values()), None))
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

            resp_body = DASHBOARD_HTML.encode("utf-8")
            headers = (
                "HTTP/1.1 200 OK\r\n"
                "Content-Type: text/html; charset=utf-8\r\n"
                f"Content-Length: {len(resp_body)}\r\n"
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
