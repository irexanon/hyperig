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


class UserWorkspace:
    """Isolated per-user Instagram + Telegram bridge instance with its own database store and live log stream."""

    def __init__(self, username: str, base_cfg: Config, store: Store, saved_cfg: dict):
        self.username = username
        self.base_cfg = base_cfg
        self.store = store
        self.saved_cfg = dict(saved_cfg or {})
        self.cfg = build_user_config(base_cfg, self.saved_cfg, username)

        self.ig = InstagramClient(self.cfg, self.store)
        self.bridge = TelegramBridge(self.cfg, self.store, username=username)
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
        tok = current_user_var.set(self.username)
        try:
            add_user_log(self.username, "INFO", "workspace", f"Initialized workspace and database for @{self.username}")
            await self.bridge.start(self.ig, self.modules)
            await self.ig.restore_saved_session()
            self._poller_task = asyncio.create_task(self._run_poller())
        finally:
            current_user_var.reset(tok)

    async def _run_poller(self) -> None:
        tok = current_user_var.set(self.username)
        try:
            await self.ig.run(self.handler.handle)
        except asyncio.CancelledError:
            pass
        finally:
            current_user_var.reset(tok)

    async def update_config(self, updates: dict) -> Config:
        tok = current_user_var.set(self.username)
        try:
            for k, v in updates.items():
                if k in ("ig_password", "ig_totp_secret", "ig_sessionid", "tg_token"):
                    # Only overwrite secret fields if non-empty or explicitly cleared
                    if v is not None and str(v).strip() != "":
                        self.saved_cfg[k] = str(v).strip()
                    elif v == "__CLEAR__":
                        self.saved_cfg.pop(k, None)
                else:
                    self.saved_cfg[k] = v

            await self.store.set("user_config", self.saved_cfg)
            self.cfg = build_user_config(self.base_cfg, self.saved_cfg, self.username)
            self.ctx.cfg = self.cfg
            await self.ig.reconfigure(self.cfg)
            await self.bridge.reconfigure(self.cfg, self.ig, self.modules)
            add_user_log(
                self.username,
                "INFO",
                "config",
                f"Configuration saved (Realtime MQTT={'on' if self.cfg.realtime else 'off'}, "
                f"FBNS={'on' if self.cfg.fbns else 'off'}, "
                f"Telegram Chat={self.cfg.tg_chat_id or 'none'})",
            )
            return self.cfg
        finally:
            current_user_var.reset(tok)

    async def login_with_password(self, payload: dict) -> tuple[bool, str]:
        tok = current_user_var.set(self.username)
        try:
            await self.update_config(payload)
            if not self.cfg.ig_password:
                return False, "Please enter your Instagram password."
            self.login_in_progress = True
            add_user_log(self.username, "INFO", "auth", f"Starting Instagram login for @{self.cfg.ig_username}...")
            try:
                ok = await self.ig.login()
            finally:
                self.login_in_progress = False
            if ok:
                return True, f"Logged in as @{self.cfg.ig_username} via {self.ig.auth.login_method}."
            return False, self.ig.last_alert or "Instagram login failed. Check Live Logs for details."
        finally:
            current_user_var.reset(tok)

    async def login_with_cookie(self, raw_cookie: str, proxy: str | None = None) -> tuple[bool, str]:
        tok = current_user_var.set(self.username)
        try:
            if proxy is not None and proxy.strip():
                await self.update_config({"ig_proxy": proxy.strip()})
            self.login_in_progress = True
            add_user_log(self.username, "INFO", "auth", f"Starting browser cookie login for @{self.cfg.ig_username}...")
            try:
                method, note = await self.ig.cookie_login(raw_cookie)
            except CookieError as e:
                add_user_log(self.username, "WARNING", "auth", str(e))
                return False, str(e)
            finally:
                self.login_in_progress = False
            msg = f"Logged in via {method}. Session saved to @{self.username} database."
            if note:
                msg += f" (Note: {note})"
            return True, msg
        finally:
            current_user_var.reset(tok)

    async def import_session(self, raw_json: str) -> tuple[bool, str]:
        tok = current_user_var.set(self.username)
        try:
            await self.ig.import_session_json(raw_json)
            return True, f"Session JSON imported and verified for @{self.cfg.ig_username}."
        except Exception as e:
            add_user_log(self.username, "WARNING", "auth", f"Session JSON import failed: {e}")
            return False, f"Could not import session JSON: {e}"
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

    async def run_command(self, raw_cmd: str) -> str:
        tok = current_user_var.set(self.username)
        try:
            tokens = (raw_cmd or "").strip().split()
            if not tokens:
                return "Enter a command (e.g. /status, /session, /help)."
            cmd_name = tokens[0].lstrip("/").lower()
            args = tokens[1:]
            cmds = self.modules.all_commands()
            if cmd_name not in cmds:
                return f"Unknown command '/{cmd_name}'. Try /help."
            return await cmds[cmd_name][0].handler(args)
        except Exception as e:
            return f"{type(e).__name__}: {e}"
        finally:
            current_user_var.reset(tok)

    async def stop(self) -> None:
        tok = current_user_var.set(self.username)
        try:
            await self.ig.stop()
            if self._poller_task:
                self._poller_task.cancel()
                await asyncio.gather(self._poller_task, return_exceptions=True)
            await self.bridge.stop()
        finally:
            current_user_var.reset(tok)

    async def to_state_dict(self) -> dict:
        session_data = await self.store.get("session")
        has_saved_session = bool(isinstance(session_data, dict) and session_data.get("uuids"))
        cmds = self.modules.all_commands()
        status_cmd = cmds.get("status")
        status_text = await status_cmd[0].handler([]) if status_cmd else ""
        up = int((datetime.now(timezone.utc) - self.ctx.started_at).total_seconds())
        return {
            "username": self.username,
            "ig_logged_in": self.ig.logged_in,
            "login_in_progress": self.login_in_progress,
            "login_method": self.ig.auth.login_method,
            "last_alert": self.ig.last_alert,
            "waiting_for_code": self.ig.auth.prompter.waiting,
            "code_hint": self.ig.auth.prompter.hint,
            "has_saved_session": has_saved_session,
            "rt_connected": self.ig.realtime.rt_connected,
            "rt_error": self.ig.realtime.rt_error,
            "rt_events": self.ig.realtime.events,
            "fbns_connected": self.ig.realtime.fbns_connected,
            "fbns_error": self.ig.realtime.fbns_error,
            "fbns_pushes": self.ig.realtime.pushes,
            "tg_running": self.bridge.running,
            "tg_bot_username": self.bridge.bot_username,
            "tg_error": self.bridge.error,
            "linked_topics": len(self.bridge.topics),
            "topics_map": self.bridge.topics,
            "forwarded": self.bridge.forwarded,
            "uptime_seconds": up,
            "config": self.cfg.to_public_dict(),
            "status_text": status_text,
            "session_info": self.ig.auth.info(),
            "storage_backend": "MongoDB" if self.base_cfg.mongo_uri else f"JSON ({self.cfg.data_dir})",
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
            raise ValueError("Please enter a valid Instagram username (letters, numbers, dots, underscores).")
        async with self._lock:
            if uname in self.workspaces:
                return self.workspaces[uname]
            store = create_user_store(self.base_cfg, uname)
            saved_cfg = await store.get("user_config", {}) or {}
            ws = UserWorkspace(uname, self.base_cfg, store, saved_cfg)
            await ws.start()
            self.workspaces[uname] = ws
            return ws

    def list_summaries(self) -> list[dict]:
        out = []
        for uname, ws in sorted(self.workspaces.items()):
            out.append(
                {
                    "username": uname,
                    "ig_logged_in": ws.ig.logged_in,
                    "login_method": ws.ig.auth.login_method,
                    "tg_running": ws.bridge.running,
                    "tg_chat_id": ws.cfg.tg_chat_id or "",
                    "realtime": ws.cfg.realtime,
                    "fbns": ws.cfg.fbns,
                    "rt_connected": ws.ig.realtime.rt_connected,
                    "fbns_connected": ws.ig.realtime.fbns_connected,
                    "forwarded": ws.bridge.forwarded,
                    "linked_topics": len(ws.bridge.topics),
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
      --bg-canvas: #0B0F17;
      --bg-surface: #111827;
      --bg-input: #080C14;
      --border-subtle: #1E293B;
      --border-focus: #38BDF8;
      --text-primary: #F1F5F9;
      --text-secondary: #94A3B8;
      --text-muted: #64748B;
      --accent: #0EA5E9;
      --accent-hover: #38BDF8;
      --status-ok: #10B981;
      --status-warn: #F59E0B;
      --status-err: #EF4444;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: 'Plus Jakarta Sans', -apple-system, BlinkMacSystemFont, sans-serif;
      background: var(--bg-canvas);
      color: var(--text-primary);
      font-size: 14px;
      line-height: 1.5;
      letter-spacing: 0.01em;
      min-height: 100vh;
      display: flex;
      flex-direction: column;
    }
    .mono, code, pre, input.mono-input {
      font-family: 'JetBrains Mono', monospace;
      font-variant-numeric: tabular-nums;
    }
    .tabular { font-variant-numeric: tabular-nums; }

    /* Top Bar Contract: 3 zones */
    header.topbar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 32px;
      padding: 14px 28px;
      border-bottom: 1px solid var(--border-subtle);
      background: var(--bg-surface);
    }
    .brand-wordmark {
      font-size: 17px;
      font-weight: 700;
      color: var(--text-primary);
      text-decoration: none;
      white-space: nowrap;
      flex-shrink: 0;
      letter-spacing: -0.02em;
    }
    nav.topnav {
      display: flex;
      align-items: center;
      gap: 24px;
    }
    nav.topnav a {
      color: var(--text-secondary);
      text-decoration: none;
      font-size: 13px;
      font-weight: 500;
      white-space: nowrap;
      flex-shrink: 0;
      padding: 4px 0;
      border-bottom: 2px solid transparent;
      transition: color 150ms ease, border-color 150ms ease;
    }
    nav.topnav a:hover, nav.topnav a.active {
      color: var(--text-primary);
      border-bottom-color: var(--accent);
    }
    .topbar-cta {
      flex-shrink: 0;
    }

    /* Workspace Layout */
    .workspace-shell {
      display: grid;
      grid-template-columns: 270px 1fr;
      flex: 1;
      max-width: 1440px;
      width: 100%;
      margin: 0 auto;
    }
    aside.sidebar {
      border-right: 1px solid var(--border-subtle);
      background: var(--bg-surface);
      padding: 20px;
      display: flex;
      flex-direction: column;
      gap: 20px;
    }
    main.viewport {
      padding: 28px 32px;
      overflow-y: auto;
      display: flex;
      flex-direction: column;
      gap: 28px;
    }
    @media (max-width: 960px) {
      .workspace-shell { grid-template-columns: 1fr; }
      aside.sidebar { border-right: none; border-bottom: 1px solid var(--border-subtle); }
      nav.topnav { display: none; }
    }

    /* Controls & Inputs */
    .section-label {
      font-size: 12px;
      font-weight: 600;
      color: var(--text-secondary);
      margin-bottom: 10px;
    }
    .field-group {
      display: flex;
      flex-direction: column;
      gap: 6px;
      margin-bottom: 14px;
    }
    .field-group label {
      font-size: 12px;
      font-weight: 500;
      color: var(--text-secondary);
      display: flex;
      justify-content: space-between;
    }
    input[type="text"], input[type="password"], input[type="number"], textarea, select {
      width: 100%;
      background: var(--bg-input);
      border: 1px solid var(--border-subtle);
      color: var(--text-primary);
      padding: 9px 12px;
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
      background: #1E293B;
      color: var(--text-primary);
      border: 1px solid #334155;
      padding: 8px 16px;
      border-radius: 6px;
      font-size: 13px;
      font-weight: 600;
      font-family: inherit;
      cursor: pointer;
      white-space: nowrap;
      flex-shrink: 0;
      transition: opacity 150ms ease, transform 150ms ease;
    }
    button:hover:not(:disabled) { opacity: 0.92; }
    button:disabled { opacity: 0.45; cursor: not-allowed; }
    button.btn-primary {
      background: var(--accent);
      color: #080C14;
      border-color: var(--accent);
    }
    button.btn-danger {
      background: transparent;
      color: var(--status-err);
      border-color: rgba(239, 68, 68, 0.35);
    }

    /* User Workspace List */
    .user-list {
      display: flex;
      flex-direction: column;
      gap: 4px;
    }
    .user-item {
      display: flex;
      align-items: center;
      justify-content: space-between;
      width: 100%;
      text-align: left;
      padding: 10px 12px;
      border-radius: 6px;
      background: transparent;
      border: 1px solid transparent;
      color: var(--text-secondary);
      font-weight: 500;
    }
    .user-item:hover {
      background: rgba(255, 255, 255, 0.03);
      color: var(--text-primary);
    }
    .user-item.active {
      background: rgba(14, 165, 233, 0.1);
      border-color: rgba(14, 165, 233, 0.35);
      color: var(--text-primary);
    }

    /* Panels & Grids (Single-level elevation, no nested cards) */
    .panel {
      background: var(--bg-surface);
      border: 1px solid var(--border-subtle);
      border-radius: 8px;
      padding: 24px;
    }
    .panel-header {
      display: flex;
      align-items: baseline;
      justify-content: space-between;
      gap: 16px;
      padding-bottom: 16px;
      margin-bottom: 20px;
      border-bottom: 1px solid var(--border-subtle);
      flex-wrap: wrap;
    }
    .panel-title {
      margin: 0;
      font-size: 16px;
      font-weight: 600;
      color: var(--text-primary);
    }
    .meta-line {
      font-size: 12px;
      color: var(--text-secondary);
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
    }
    .status-text-ok { color: var(--status-ok); font-weight: 600; }
    .status-text-warn { color: var(--status-warn); font-weight: 600; }
    .status-text-err { color: var(--status-err); font-weight: 600; }

    /* KPI Strip */
    .kpi-strip {
      display: grid;
      grid-template-columns: repeat(4, 1fr);
      border: 1px solid var(--border-subtle);
      border-radius: 8px;
      background: var(--bg-surface);
    }
    .kpi-cell {
      padding: 18px 20px;
      border-right: 1px solid var(--border-subtle);
    }
    .kpi-cell:last-child { border-right: none; }
    .kpi-label { font-size: 12px; color: var(--text-secondary); margin-bottom: 6px; }
    .kpi-value { font-size: 18px; font-weight: 600; color: var(--text-primary); }
    .kpi-sub { font-size: 12px; color: var(--text-muted); margin-top: 4px; }
    @media (max-width: 768px) {
      .kpi-strip { grid-template-columns: 1fr 1fr; }
      .kpi-cell:nth-child(2) { border-right: none; }
      .kpi-cell:nth-child(1), .kpi-cell:nth-child(2) { border-bottom: 1px solid var(--border-subtle); }
    }

    .two-col {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 24px;
      align-items: start;
    }
    @media (max-width: 1024px) {
      .two-col { grid-template-columns: 1fr; }
    }

    .form-grid-2 {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 12px;
    }

    /* Toggle Rows */
    .toggle-list {
      display: flex;
      flex-direction: column;
    }
    .toggle-row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 12px 0;
      border-bottom: 1px solid rgba(255, 255, 255, 0.05);
    }
    .toggle-row:last-child { border-bottom: none; }
    .toggle-info { display: flex; flex-direction: column; gap: 2px; }
    .toggle-title { font-size: 13px; font-weight: 500; color: var(--text-primary); }
    .toggle-desc { font-size: 12px; color: var(--text-secondary); }
    input[type="checkbox"].switch {
      width: 38px;
      height: 20px;
      appearance: none;
      background: #334155;
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
      background: #F8FAFC;
      border-radius: 50%;
      transition: transform 150ms ease;
    }
    input[type="checkbox"].switch:checked { background: var(--accent); }
    input[type="checkbox"].switch:checked::after { transform: translateX(18px); }

    /* Segmented Filter */
    .segmented {
      display: inline-flex;
      background: var(--bg-input);
      border: 1px solid var(--border-subtle);
      border-radius: 6px;
      padding: 2px;
      gap: 2px;
    }
    .segmented button {
      background: transparent;
      border: none;
      padding: 5px 12px;
      font-size: 12px;
      color: var(--text-secondary);
      border-radius: 4px;
    }
    .segmented button.active {
      background: #1E293B;
      color: var(--text-primary);
    }

    /* Live Logs Console */
    .log-stream {
      background: var(--bg-input);
      border: 1px solid var(--border-subtle);
      border-radius: 6px;
      height: 320px;
      overflow-y: auto;
      padding: 12px 14px;
      font-size: 12px;
      line-height: 1.6;
    }
    .log-line {
      display: grid;
      grid-template-columns: 145px 65px 80px 1fr;
      gap: 10px;
      padding: 2px 0;
      border-bottom: 1px solid rgba(255, 255, 255, 0.02);
    }
    .log-ts { color: var(--text-muted); }
    .log-lvl-INFO { color: #38BDF8; }
    .log-lvl-WARNING { color: var(--status-warn); }
    .log-lvl-ERROR { color: var(--status-err); font-weight: 600; }
    .log-src { color: var(--text-secondary); }
    .log-msg { color: var(--text-primary); word-break: break-word; }

    .banner-notice {
      background: rgba(245, 158, 11, 0.08);
      border: 1px solid rgba(245, 158, 11, 0.35);
      border-radius: 8px;
      padding: 16px 20px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      flex-wrap: wrap;
    }
    .feedback-msg {
      font-size: 12px;
      margin-top: 8px;
      min-height: 18px;
    }
  </style>
</head>
<body>
  <!-- Top Bar Contract: Zone 1 Brand | Zone 2 Nav Links | Zone 3 Primary Action -->
  <header class="topbar">
    <a href="#" class="brand-wordmark">Hyper Insta Bridge</a>
    <nav class="topnav">
      <a href="#section-auth" class="active">Authentication</a>
      <a href="#section-telegram">Telegram Bridge</a>
      <a href="#section-realtime">Realtime &amp; Polling</a>
      <a href="#section-logs">Live Logs</a>
    </nav>
    <div class="topbar-cta">
      <button class="btn-primary" id="btn-download-session" onclick="downloadSession()" disabled>Download Session JSON</button>
    </div>
  </header>

  <div class="workspace-shell">
    <!-- Sidebar: Multi-User Workspace & DB Switcher -->
    <aside class="sidebar">
      <div>
        <div class="section-label">Create or Open User DB</div>
        <div style="display: flex; gap: 6px;">
          <input type="text" id="new-user-input" class="mono-input" placeholder="Instagram username..." />
          <button class="btn-primary" onclick="openOrCreateUser()">Open</button>
        </div>
        <div style="font-size: 11px; color: var(--text-muted); margin-top: 6px;">
          Creates an isolated database &amp; session store for this username.
        </div>
      </div>

      <div style="flex: 1; overflow-y: auto;">
        <div class="section-label">User Workspaces</div>
        <div id="user-list" class="user-list"></div>
      </div>

      <div style="border-top: 1px solid var(--border-subtle); padding-top: 14px; font-size: 12px; color: var(--text-secondary);">
        <div>Active User DB: <strong id="sidebar-active-user" class="mono">None</strong></div>
        <div id="sidebar-db-backend" style="color: var(--text-muted); font-size: 11px; margin-top: 4px;">Select or create a username above</div>
      </div>
    </aside>

    <!-- Main Viewport -->
    <main class="viewport">
      <!-- Empty State when no user workspace is open -->
      <div id="empty-workspace" class="panel" style="text-align: center; padding: 56px 24px;">
        <h2 style="margin: 0 0 8px 0; font-size: 20px;">Select or Create a User Workspace</h2>
        <p style="color: var(--text-secondary); max-width: 520px; margin: 0 auto 20px auto;">
          Enter your Instagram username to initialize your dedicated database store, log in with password or browser cookie, connect your Telegram Forum Group Chat ID, and manage Realtime MQTT &amp; FBNS channels.
        </p>
        <div style="display: flex; gap: 8px; max-width: 380px; margin: 0 auto;">
          <input type="text" id="hero-user-input" class="mono-input" placeholder="e.g. alex_studio" />
          <button class="btn-primary" onclick="openFromHero()">Create User DB</button>
        </div>
      </div>

      <!-- Active Workspace Content -->
      <div id="active-workspace" style="display: none; flex-direction: column; gap: 24px;">
        <!-- 2FA / Challenge Code Prompt Banner (Shown automatically when Instagram asks for a code) -->
        <div id="code-banner" class="banner-notice" style="display: none;">
          <div>
            <div style="font-weight: 600; color: var(--status-warn);">Verification / 2FA Code Required by Instagram</div>
            <div id="code-banner-hint" style="font-size: 12px; color: var(--text-secondary); margin-top: 2px;"></div>
          </div>
          <div style="display: flex; gap: 8px; align-items: center;">
            <input type="text" id="code-input" class="mono-input" placeholder="6 or 8 digit code" style="width: 180px;" />
            <button class="btn-primary" onclick="submitVerificationCode()">Submit Code</button>
          </div>
        </div>

        <!-- 4-Column KPI Summary Strip -->
        <div class="kpi-strip">
          <div class="kpi-cell">
            <div class="kpi-label">Instagram Session</div>
            <div class="kpi-value" id="kpi-ig-state">Logged Out</div>
            <div class="kpi-sub mono" id="kpi-ig-sub">Method: none</div>
          </div>
          <div class="kpi-cell">
            <div class="kpi-label">Telegram Forum Bridge</div>
            <div class="kpi-value" id="kpi-tg-state">Unconfigured</div>
            <div class="kpi-sub mono" id="kpi-tg-sub">Chat ID: none</div>
          </div>
          <div class="kpi-cell">
            <div class="kpi-label">Realtime MQTT &amp; FBNS</div>
            <div class="kpi-value" id="kpi-rt-state">Standby</div>
            <div class="kpi-sub mono" id="kpi-rt-sub">0 events · 0 pushes</div>
          </div>
          <div class="kpi-cell">
            <div class="kpi-label">Bridged Conversations</div>
            <div class="kpi-value tabular" id="kpi-topics-count">0 Topics</div>
            <div class="kpi-sub mono" id="kpi-forwarded-sub">0 messages forwarded</div>
          </div>
        </div>

        <!-- Section 1: Instagram Authentication & Session Management -->
        <section id="section-auth" class="two-col">
          <!-- Left: Username / Password / Proxy / Device Login -->
          <div class="panel">
            <div class="panel-header">
              <h2 class="panel-title">01. Instagram Credentials &amp; Proxy Login</h2>
              <div class="meta-line">
                <span id="auth-status-indicator">Not logged in</span>
                <span>·</span>
                <span class="mono" id="auth-user-label">@user</span>
              </div>
            </div>

            <div class="form-grid-2">
              <div class="field-group">
                <label>Instagram Username</label>
                <input type="text" id="cfg-ig-username" class="mono-input" placeholder="username" />
              </div>
              <div class="field-group">
                <label>Instagram Password <span id="saved-pw-hint" style="color: var(--status-ok);"></span></label>
                <input type="password" id="cfg-ig-password" placeholder="Enter password to login or update" />
              </div>
            </div>

            <div class="form-grid-2">
              <div class="field-group">
                <label>HTTP / SOCKS5 Proxy (Optional)</label>
                <input type="text" id="cfg-ig-proxy" class="mono-input" placeholder="http://user:pass@host:port" />
              </div>
              <div class="field-group">
                <label>2FA TOTP Secret (Optional Base32)</label>
                <input type="password" id="cfg-ig-totp" class="mono-input" placeholder="Auto-generates 2FA codes" />
              </div>
            </div>

            <div class="form-grid-2">
              <div class="field-group">
                <label>Device Locale &amp; Country</label>
                <div style="display: flex; gap: 8px;">
                  <input type="text" id="cfg-ig-locale" class="mono-input" placeholder="en_US" />
                  <input type="text" id="cfg-ig-country" class="mono-input" placeholder="US" style="width: 90px;" />
                </div>
              </div>
              <div class="field-group">
                <label>Timezone Offset (Seconds from UTC)</label>
                <input type="number" id="cfg-ig-tz" class="mono-input" placeholder="e.g. 0 or 18000" />
              </div>
            </div>

            <div style="display: flex; gap: 10px; align-items: center; margin-top: 8px; flex-wrap: wrap;">
              <button class="btn-primary" id="btn-password-login" onclick="loginWithPassword()">Save &amp; Login with Password</button>
              <button onclick="saveConfigOnly()">Save Settings Only</button>
              <button onclick="triggerRelogin()">Relogin</button>
            </div>
            <div id="auth-feedback" class="feedback-msg"></div>
          </div>

          <!-- Right: Cookie Login & Session JSON Import/Export -->
          <div class="panel">
            <div class="panel-header">
              <h2 class="panel-title">02. Browser Cookie Login &amp; Session File</h2>
              <div class="meta-line">
                <span>Recommended for cloud / datacenter IPs</span>
              </div>
            </div>

            <div class="field-group">
              <label>Paste Browser Cookie (sessionid value, Cookie header, or JSON export)</label>
              <textarea id="cookie-login-input" class="mono-input" rows="3" placeholder="123456789%3AAbCdEfGhIjKlMn... or full JSON cookie array"></textarea>
            </div>
            <div style="display: flex; gap: 10px; align-items: center; margin-bottom: 18px; flex-wrap: wrap;">
              <button class="btn-primary" id="btn-cookie-login" onclick="loginWithCookie()">Login with Cookie</button>
              <button onclick="downloadSession()" id="btn-export-session">Download Session JSON</button>
              <button class="btn-danger" onclick="logoutSession()">Logout &amp; Clear Session</button>
            </div>

            <div style="border-top: 1px solid var(--border-subtle); padding-top: 16px;">
              <div class="field-group" style="margin-bottom: 10px;">
                <label>Import Saved instagrapi Session JSON (Paste or Upload .json)</label>
                <textarea id="session-json-input" class="mono-input" rows="3" placeholder='{"uuids": {...}, "authorization_data": {...}}'></textarea>
              </div>
              <div style="display: flex; gap: 10px; align-items: center; justify-content: space-between; flex-wrap: wrap;">
                <input type="file" id="session-file-picker" accept=".json,application/json" onchange="loadSessionFile(event)" style="font-size: 12px; max-width: 240px;" />
                <button onclick="importSessionJson()">Import Session JSON</button>
              </div>
            </div>
            <div id="cookie-feedback" class="feedback-msg"></div>
          </div>
        </section>

        <!-- Section 2: Telegram Forum Group Bridge & Realtime / FBNS Toggles -->
        <section id="section-telegram" class="two-col">
          <!-- Left: Telegram Group & Bot Setup -->
          <div class="panel">
            <div class="panel-header">
              <h2 class="panel-title">03. Telegram Forum Group Routing</h2>
              <div class="meta-line">
                <span id="tg-status-label">Waiting for Chat ID</span>
                <span>·</span>
                <span class="mono" id="tg-bot-name-label">Shared Bot</span>
              </div>
            </div>

            <p style="margin: 0 0 14px 0; font-size: 12px; color: var(--text-secondary);">
              Create a Telegram group, enable <strong>Topics</strong> in group settings, add the bot as an Admin with <strong>Manage Topics</strong>, and enter your group's Chat ID (starts with <code class="mono">-100</code>).
            </p>

            <div class="form-grid-2">
              <div class="field-group">
                <label>Telegram Forum Chat ID</label>
                <input type="text" id="cfg-tg-chat-id" class="mono-input" placeholder="-1001234567890" />
              </div>
              <div class="field-group">
                <label>Telegram Admin User ID (Optional)</label>
                <input type="text" id="cfg-tg-admin-id" class="mono-input" placeholder="Numeric user ID (optional)" />
              </div>
            </div>

            <div class="field-group">
              <label>Custom Telegram Bot Token (Optional — leave blank to use default bot)</label>
              <input type="password" id="cfg-tg-token" class="mono-input" placeholder="Uses server TELEGRAM_BOT_TOKEN if blank" />
            </div>

            <div style="display: flex; gap: 10px; align-items: center; margin-top: 8px;">
              <button class="btn-primary" onclick="saveTelegramConfig()">Connect Telegram Group</button>
              <button onclick="runCommand('/status')">Test /status</button>
              <button onclick="runCommand('/ping')">Test /ping</button>
            </div>
            <div id="tg-feedback" class="feedback-msg"></div>

            <div style="margin-top: 18px; border-top: 1px solid var(--border-subtle); padding-top: 14px;">
              <div class="section-label">Active Device &amp; Session Details</div>
              <pre id="session-details-box" class="mono" style="background: var(--bg-input); border: 1px solid var(--border-subtle); border-radius: 6px; padding: 12px; font-size: 12px; margin: 0; white-space: pre-wrap; color: var(--text-secondary);"></pre>
            </div>
          </div>

          <!-- Right: Realtime MQTT, FBNS Push & Polling Toggles -->
          <div id="section-realtime" class="panel">
            <div class="panel-header">
              <h2 class="panel-title">04. Realtime MQTT, FBNS &amp; Delivery Controls</h2>
              <div class="meta-line">
                <span id="rt-summary-line" class="mono">MQTT: off · FBNS: off</span>
              </div>
            </div>

            <div class="toggle-list">
              <div class="toggle-row">
                <div class="toggle-info">
                  <span class="toggle-title">Realtime MQTT Live Delivery</span>
                  <span class="toggle-desc">Subscribe to edge-mqtt.facebook.com for instant DM &amp; typing events</span>
                </div>
                <input type="checkbox" class="switch" id="toggle-realtime" onchange="toggleFeature('realtime', this.checked)" />
              </div>

              <div class="toggle-row">
                <div class="toggle-info">
                  <span class="toggle-title">FBNS Push Notification Channel</span>
                  <span class="toggle-desc">Listen on mqtt-mini.facebook.com push channel to wake inbox sync</span>
                </div>
                <input type="checkbox" class="switch" id="toggle-fbns" onchange="toggleFeature('fbns', this.checked)" />
              </div>

              <div class="toggle-row">
                <div class="toggle-info">
                  <span class="toggle-title">Send Replies over Realtime MQTT</span>
                  <span class="toggle-desc">Send Telegram text replies through the live MQTT socket instead of HTTP</span>
                </div>
                <input type="checkbox" class="switch" id="toggle-realtime-send" onchange="toggleFeature('realtime_send', this.checked)" />
              </div>

              <div class="toggle-row">
                <div class="toggle-info">
                  <span class="toggle-title">Mark Messages as Seen on Instagram</span>
                  <span class="toggle-desc">Send read receipts once a message is delivered to your Telegram topic</span>
                </div>
                <input type="checkbox" class="switch" id="toggle-mark-seen" onchange="toggleFeature('mark_seen', this.checked)" />
              </div>

              <div class="toggle-row">
                <div class="toggle-info">
                  <span class="toggle-title">Bridge Message Requests Folder</span>
                  <span class="toggle-desc">Include pending Instagram message requests during inbox polling</span>
                </div>
                <input type="checkbox" class="switch" id="toggle-include-requests" onchange="toggleFeature('include_requests', this.checked)" />
              </div>

              <div class="toggle-row">
                <div class="toggle-info">
                  <span class="toggle-title">Legacy Login Endpoint Mode</span>
                  <span class="toggle-desc">Use login_legacy() instead of Bloks CAA login flow</span>
                </div>
                <input type="checkbox" class="switch" id="toggle-legacy-login" onchange="toggleFeature('ig_legacy_login', this.checked)" />
              </div>
            </div>

            <div class="form-grid-2" style="margin-top: 16px; padding-top: 14px; border-top: 1px solid var(--border-subtle);">
              <div class="field-group" style="margin-bottom: 0;">
                <label>Poll Interval (Seconds, min 15)</label>
                <input type="number" id="cfg-poll-interval" class="mono-input" min="15" onchange="toggleFeature('poll_interval', this.value)" />
              </div>
              <div class="field-group" style="margin-bottom: 0;">
                <label>Safety Poll Interval (Seconds, min 60)</label>
                <input type="number" id="cfg-safety-poll" class="mono-input" min="60" onchange="toggleFeature('safety_poll_interval', this.value)" />
              </div>
            </div>
            <div id="rt-feedback" class="feedback-msg"></div>
          </div>
        </section>

        <!-- Section 3: Live Logs & Bot Command Runner -->
        <section id="section-logs" class="panel">
          <div class="panel-header">
            <div>
              <h2 class="panel-title">05. Live User Logs &amp; Command Console</h2>
              <div class="meta-line" style="margin-top: 4px;">
                <span>Isolated log stream for <strong id="log-user-label" class="mono">@user</strong></span>
                <span>·</span>
                <span id="log-count-label" class="tabular">0 entries</span>
              </div>
            </div>

            <div style="display: flex; gap: 10px; align-items: center; flex-wrap: wrap;">
              <div class="segmented">
                <button id="filter-ALL" class="active" onclick="setLogFilter('ALL')">All</button>
                <button id="filter-INFO" onclick="setLogFilter('INFO')">Info</button>
                <button id="filter-WARNING" onclick="setLogFilter('WARNING')">Warnings</button>
                <button id="filter-ERROR" onclick="setLogFilter('ERROR')">Errors</button>
              </div>
              <button onclick="clearUserLogs()">Clear Logs</button>
              <button onclick="downloadUserLogs()">Download Logs</button>
            </div>
          </div>

          <div style="display: flex; gap: 8px; margin-bottom: 14px; flex-wrap: wrap;">
            <input type="text" id="console-cmd-input" class="mono-input" placeholder="Run bot command for this user (e.g. /status, /session, /help, /cookie <sid>, /code 123456)" style="flex: 1; min-width: 260px;" />
            <button class="btn-primary" onclick="runCustomCommand()">Execute Command</button>
          </div>

          <div id="log-stream" class="log-stream mono"></div>
        </section>
      </div>
    </main>
  </div>

  <script>
    let currentUser = localStorage.getItem('hyperinsta_active_user') || '';
    let userLogs = [];
    let lastLogId = 0;
    let logFilter = 'ALL';
    let formHydratedForUser = '';

    async function api(path, options = {}) {
      const res = await fetch(path, {
        headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
        ...options
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || ('HTTP ' + res.status));
      return data;
    }

    async function loadUserList() {
      try {
        const data = await api('/api/users');
        const listEl = document.getElementById('user-list');
        const users = data.users || [];

        if (!currentUser && users.length > 0) {
          currentUser = users[0].username;
          localStorage.setItem('hyperinsta_active_user', currentUser);
        }

        if (users.length === 0) {
          listEl.innerHTML = '<div style="font-size: 12px; color: var(--text-muted); padding: 8px 0;">No user workspaces yet. Enter a username above to create one.</div>';
        } else {
          listEl.innerHTML = users.map(u => {
            const isAct = u.username === currentUser;
            const st = u.ig_logged_in ? 'Logged In' : 'Logged Out';
            const stClass = u.ig_logged_in ? 'status-text-ok' : 'status-text-warn';
            return `
              <button class="user-item ${isAct ? 'active' : ''}" onclick="selectUser('${u.username}')">
                <span class="mono">@${u.username}</span>
                <span class="${stClass}" style="font-size: 11px;">${st}</span>
              </button>
            `;
          }).join('');
        }

        if (currentUser) {
          document.getElementById('empty-workspace').style.display = 'none';
          document.getElementById('active-workspace').style.display = 'flex';
          await refreshActiveUser();
        } else {
          document.getElementById('empty-workspace').style.display = 'block';
          document.getElementById('active-workspace').style.display = 'none';
          document.getElementById('btn-download-session').disabled = true;
        }
      } catch (e) {
        console.error('Failed to load users:', e);
      }
    }

    async function selectUser(username) {
      currentUser = username;
      localStorage.setItem('hyperinsta_active_user', currentUser);
      userLogs = [];
      lastLogId = 0;
      formHydratedForUser = '';
      await loadUserList();
    }

    async function openOrCreateUser() {
      const inp = document.getElementById('new-user-input');
      const raw = inp.value.trim();
      if (!raw) return;
      try {
        const res = await api('/api/users', {
          method: 'POST',
          body: JSON.stringify({ username: raw })
        });
        inp.value = '';
        await selectUser(res.username);
      } catch (e) {
        document.getElementById('sidebar-db-backend').textContent = e.message;
      }
    }

    async function openFromHero() {
      const inp = document.getElementById('hero-user-input');
      const raw = inp.value.trim();
      if (!raw) return;
      document.getElementById('new-user-input').value = raw;
      await openOrCreateUser();
    }

    async function refreshActiveUser() {
      if (!currentUser) return;
      try {
        const st = await api(`/api/users/${encodeURIComponent(currentUser)}/state`);
        document.getElementById('sidebar-active-user').textContent = '@' + st.username;
        document.getElementById('sidebar-db-backend').textContent = 'DB: ' + st.storage_backend;
        document.getElementById('auth-user-label').textContent = '@' + st.config.ig_username;
        document.getElementById('log-user-label').textContent = '@' + st.username;
        document.getElementById('btn-download-session').disabled = !(st.ig_logged_in || st.has_saved_session);

        // 2FA / Challenge banner
        const banner = document.getElementById('code-banner');
        if (st.waiting_for_code) {
          banner.style.display = 'flex';
          document.getElementById('code-banner-hint').textContent = st.code_hint || 'Check your SMS, email, or authenticator app and enter the code.';
        } else {
          banner.style.display = 'none';
        }

        // KPIs
        const igStateEl = document.getElementById('kpi-ig-state');
        if (st.ig_logged_in) {
          igStateEl.textContent = 'Logged In';
          igStateEl.className = 'kpi-value status-text-ok';
          document.getElementById('auth-status-indicator').textContent = 'Authenticated';
          document.getElementById('auth-status-indicator').className = 'status-text-ok';
        } else if (st.login_in_progress) {
          igStateEl.textContent = 'Logging In...';
          igStateEl.className = 'kpi-value status-text-warn';
        } else {
          igStateEl.textContent = 'Logged Out';
          igStateEl.className = 'kpi-value status-text-warn';
          document.getElementById('auth-status-indicator').textContent = 'Awaiting Login';
          document.getElementById('auth-status-indicator').className = 'status-text-warn';
        }
        document.getElementById('kpi-ig-sub').textContent = `Method: ${st.login_method}${st.has_saved_session ? ' · Saved in DB' : ''}`;

        const tgStateEl = document.getElementById('kpi-tg-state');
        if (st.tg_running) {
          tgStateEl.textContent = 'Connected';
          tgStateEl.className = 'kpi-value status-text-ok';
          document.getElementById('tg-status-label').textContent = 'Active';
          document.getElementById('tg-status-label').className = 'status-text-ok';
        } else {
          tgStateEl.textContent = 'Waiting';
          tgStateEl.className = 'kpi-value status-text-warn';
          document.getElementById('tg-status-label').textContent = st.tg_error || 'Not configured';
          document.getElementById('tg-status-label').className = 'status-text-warn';
        }
        document.getElementById('kpi-tg-sub').textContent = st.config.tg_chat_id ? `Chat ID: ${st.config.tg_chat_id}` : 'Enter Chat ID below';
        document.getElementById('tg-bot-name-label').textContent = st.tg_bot_username ? ('Bot: @' + st.tg_bot_username) : (st.config.has_tg_token ? 'Bot Token Set' : 'No Bot Token');

        const rtStateEl = document.getElementById('kpi-rt-state');
        const liveOn = st.rt_connected || st.fbns_connected;
        rtStateEl.textContent = liveOn ? 'Connected' : ((st.config.realtime || st.config.fbns) ? 'Standby' : 'Disabled');
        rtStateEl.className = 'kpi-value ' + (liveOn ? 'status-text-ok' : '');
        document.getElementById('kpi-rt-sub').textContent = `${st.rt_events} MQTT events · ${st.fbns_pushes} FBNS pushes`;
        document.getElementById('rt-summary-line').textContent = `MQTT: ${st.config.realtime ? (st.rt_connected ? 'connected' : 'enabled') : 'off'} · FBNS: ${st.config.fbns ? (st.fbns_connected ? 'connected' : 'enabled') : 'off'}`;

        document.getElementById('kpi-topics-count').textContent = `${st.linked_topics} Linked Topics`;
        document.getElementById('kpi-forwarded-sub').textContent = `${st.forwarded} DMs forwarded to Telegram`;

        document.getElementById('session-details-box').textContent = st.session_info;
        document.getElementById('saved-pw-hint').textContent = st.config.has_password ? '(Saved)' : '';

        // Hydrate form inputs once when switching user
        if (formHydratedForUser !== st.username) {
          formHydratedForUser = st.username;
          document.getElementById('cfg-ig-username').value = st.config.ig_username || st.username;
          document.getElementById('cfg-ig-password').value = '';
          document.getElementById('cfg-ig-proxy').value = st.config.ig_proxy || '';
          document.getElementById('cfg-ig-totp').value = '';
          document.getElementById('cfg-ig-locale').value = st.config.ig_locale || 'en_US';
          document.getElementById('cfg-ig-country').value = st.config.ig_country || 'US';
          document.getElementById('cfg-ig-tz').value = st.config.ig_timezone_offset;
          document.getElementById('cfg-tg-chat-id').value = st.config.tg_chat_id || '';
          document.getElementById('cfg-tg-admin-id').value = st.config.tg_admin_id || '';
          document.getElementById('cfg-tg-token').value = '';
          document.getElementById('cfg-poll-interval').value = st.config.poll_interval || 30;
          document.getElementById('cfg-safety-poll').value = st.config.safety_poll_interval || 300;
        }

        // Always sync switches
        document.getElementById('toggle-realtime').checked = Boolean(st.config.realtime);
        document.getElementById('toggle-fbns').checked = Boolean(st.config.fbns);
        document.getElementById('toggle-realtime-send').checked = Boolean(st.config.realtime_send);
        document.getElementById('toggle-mark-seen').checked = Boolean(st.config.mark_seen);
        document.getElementById('toggle-include-requests').checked = Boolean(st.config.include_requests);
        document.getElementById('toggle-legacy-login').checked = Boolean(st.config.ig_legacy_login);

        await fetchUserLogs();
      } catch (e) {
        console.error('Error refreshing user state:', e);
      }
    }

    function collectAuthForm() {
      return {
        ig_username: document.getElementById('cfg-ig-username').value.trim(),
        ig_password: document.getElementById('cfg-ig-password').value,
        ig_proxy: document.getElementById('cfg-ig-proxy').value.trim(),
        ig_totp_secret: document.getElementById('cfg-ig-totp').value.trim(),
        ig_locale: document.getElementById('cfg-ig-locale').value.trim(),
        ig_country: document.getElementById('cfg-ig-country').value.trim(),
        ig_timezone_offset: document.getElementById('cfg-ig-tz').value.trim()
      };
    }

    async function saveConfigOnly() {
      const fb = document.getElementById('auth-feedback');
      fb.textContent = 'Saving settings...';
      fb.className = 'feedback-msg';
      try {
        await api(`/api/users/${encodeURIComponent(currentUser)}/config`, {
          method: 'POST',
          body: JSON.stringify(collectAuthForm())
        });
        fb.textContent = 'Settings saved to user database.';
        fb.className = 'feedback-msg status-text-ok';
        await refreshActiveUser();
      } catch (e) {
        fb.textContent = e.message;
        fb.className = 'feedback-msg status-text-err';
      }
    }

    async function loginWithPassword() {
      const fb = document.getElementById('auth-feedback');
      const btn = document.getElementById('btn-password-login');
      fb.textContent = 'Logging in to Instagram (if 2FA/challenge is needed, a prompt will appear)...';
      fb.className = 'feedback-msg status-text-warn';
      btn.disabled = true;
      try {
        const res = await api(`/api/users/${encodeURIComponent(currentUser)}/login`, {
          method: 'POST',
          body: JSON.stringify(collectAuthForm())
        });
        fb.textContent = res.message;
        fb.className = 'feedback-msg ' + (res.ok ? 'status-text-ok' : 'status-text-warn');
        await loadUserList();
      } catch (e) {
        fb.textContent = e.message;
        fb.className = 'feedback-msg status-text-err';
      } finally {
        btn.disabled = false;
      }
    }

    async function triggerRelogin() {
      const fb = document.getElementById('auth-feedback');
      fb.textContent = 'Running /relogin...';
      fb.className = 'feedback-msg status-text-warn';
      try {
        const res = await api(`/api/users/${encodeURIComponent(currentUser)}/command`, {
          method: 'POST',
          body: JSON.stringify({ command: '/relogin' })
        });
        fb.textContent = res.reply;
        fb.className = 'feedback-msg';
        await loadUserList();
      } catch (e) {
        fb.textContent = e.message;
        fb.className = 'feedback-msg status-text-err';
      }
    }

    async function loginWithCookie() {
      const raw = document.getElementById('cookie-login-input').value.trim();
      const proxy = document.getElementById('cfg-ig-proxy').value.trim();
      const fb = document.getElementById('cookie-feedback');
      if (!raw) {
        fb.textContent = 'Paste your instagram.com sessionid cookie value first.';
        fb.className = 'feedback-msg status-text-warn';
        return;
      }
      const btn = document.getElementById('btn-cookie-login');
      btn.disabled = true;
      fb.textContent = 'Validating cookie with Instagram...';
      fb.className = 'feedback-msg status-text-warn';
      try {
        const res = await api(`/api/users/${encodeURIComponent(currentUser)}/cookie`, {
          method: 'POST',
          body: JSON.stringify({ cookie: raw, proxy })
        });
        fb.textContent = res.message;
        fb.className = 'feedback-msg ' + (res.ok ? 'status-text-ok' : 'status-text-err');
        if (res.ok) document.getElementById('cookie-login-input').value = '';
        await loadUserList();
      } catch (e) {
        fb.textContent = e.message;
        fb.className = 'feedback-msg status-text-err';
      } finally {
        btn.disabled = false;
      }
    }

    function loadSessionFile(event) {
      const file = event.target.files && event.target.files[0];
      if (!file) return;
      const reader = new FileReader();
      reader.onload = () => {
        document.getElementById('session-json-input').value = reader.result;
      };
      reader.readAsText(file);
    }

    async function importSessionJson() {
      const raw = document.getElementById('session-json-input').value.trim();
      const fb = document.getElementById('cookie-feedback');
      if (!raw) {
        fb.textContent = 'Paste or select a session JSON file first.';
        fb.className = 'feedback-msg status-text-warn';
        return;
      }
      fb.textContent = 'Importing and verifying session JSON...';
      fb.className = 'feedback-msg status-text-warn';
      try {
        const res = await api(`/api/users/${encodeURIComponent(currentUser)}/session/import`, {
          method: 'POST',
          body: JSON.stringify({ session_json: raw })
        });
        fb.textContent = res.message;
        fb.className = 'feedback-msg ' + (res.ok ? 'status-text-ok' : 'status-text-err');
        if (res.ok) document.getElementById('session-json-input').value = '';
        await loadUserList();
      } catch (e) {
        fb.textContent = e.message;
        fb.className = 'feedback-msg status-text-err';
      }
    }

    function downloadSession() {
      if (!currentUser) return;
      const a = document.createElement('a');
      a.href = `/api/users/${encodeURIComponent(currentUser)}/session/download`;
      a.download = `${currentUser}_session.json`;
      document.body.appendChild(a);
      a.click();
      a.remove();
    }

    async function logoutSession() {
      const fb = document.getElementById('cookie-feedback');
      fb.textContent = 'Logging out and clearing saved session...';
      try {
        const res = await api(`/api/users/${encodeURIComponent(currentUser)}/command`, {
          method: 'POST',
          body: JSON.stringify({ command: '/logout confirm' })
        });
        fb.textContent = res.reply;
        fb.className = 'feedback-msg status-text-warn';
        await loadUserList();
      } catch (e) {
        fb.textContent = e.message;
        fb.className = 'feedback-msg status-text-err';
      }
    }

    async function saveTelegramConfig() {
      const fb = document.getElementById('tg-feedback');
      fb.textContent = 'Connecting Telegram group...';
      fb.className = 'feedback-msg status-text-warn';
      try {
        await api(`/api/users/${encodeURIComponent(currentUser)}/config`, {
          method: 'POST',
          body: JSON.stringify({
            tg_chat_id: document.getElementById('cfg-tg-chat-id').value.trim(),
            tg_admin_id: document.getElementById('cfg-tg-admin-id').value.trim(),
            tg_token: document.getElementById('cfg-tg-token').value.trim()
          })
        });
        fb.textContent = 'Telegram group settings updated.';
        fb.className = 'feedback-msg status-text-ok';
        await refreshActiveUser();
      } catch (e) {
        fb.textContent = e.message;
        fb.className = 'feedback-msg status-text-err';
      }
    }

    async function toggleFeature(key, value) {
      const fb = document.getElementById('rt-feedback');
      fb.textContent = `Updating ${key}...`;
      fb.className = 'feedback-msg';
      try {
        await api(`/api/users/${encodeURIComponent(currentUser)}/config`, {
          method: 'POST',
          body: JSON.stringify({ [key]: value })
        });
        fb.textContent = `Updated ${key}.`;
        fb.className = 'feedback-msg status-text-ok';
        await refreshActiveUser();
      } catch (e) {
        fb.textContent = e.message;
        fb.className = 'feedback-msg status-text-err';
      }
    }

    async function submitVerificationCode() {
      const inp = document.getElementById('code-input');
      const code = inp.value.trim();
      if (!code) return;
      try {
        await api(`/api/users/${encodeURIComponent(currentUser)}/code`, {
          method: 'POST',
          body: JSON.stringify({ code })
        });
        inp.value = '';
        await refreshActiveUser();
      } catch (e) {
        console.error(e);
      }
    }

    async function runCommand(cmd) {
      if (!currentUser) return;
      try {
        const res = await api(`/api/users/${encodeURIComponent(currentUser)}/command`, {
          method: 'POST',
          body: JSON.stringify({ command: cmd })
        });
        await refreshActiveUser();
        return res.reply;
      } catch (e) {
        return e.message;
      }
    }

    async function runCustomCommand() {
      const inp = document.getElementById('console-cmd-input');
      const val = inp.value.trim();
      if (!val) return;
      inp.value = '';
      await runCommand(val);
    }

    async function fetchUserLogs() {
      if (!currentUser) return;
      try {
        const res = await api(`/api/users/${encodeURIComponent(currentUser)}/logs?since=${lastLogId}`);
        const incoming = res.logs || [];
        if (incoming.length > 0) {
          userLogs.push(...incoming);
          if (userLogs.length > 400) userLogs = userLogs.slice(-400);
          lastLogId = userLogs[userLogs.length - 1].id;
          renderLogs();
        } else if (userLogs.length === 0) {
          renderLogs();
        }
      } catch (e) {
        console.error(e);
      }
    }

    function setLogFilter(level) {
      logFilter = level;
      ['ALL', 'INFO', 'WARNING', 'ERROR'].forEach(l => {
        document.getElementById('filter-' + l).className = (l === level) ? 'active' : '';
      });
      renderLogs();
    }

    function renderLogs() {
      const box = document.getElementById('log-stream');
      const filtered = logFilter === 'ALL' ? userLogs : userLogs.filter(x => x.level === logFilter);
      document.getElementById('log-count-label').textContent = `${filtered.length} entries`;
      if (filtered.length === 0) {
        box.innerHTML = '<div style="color: var(--text-muted); padding: 12px 0;">No log entries matching filter.</div>';
        return;
      }
      const wasAtBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
      box.innerHTML = filtered.map(item => `
        <div class="log-line">
          <span class="log-ts">${item.timestamp}</span>
          <span class="log-lvl-${item.level}">${item.level}</span>
          <span class="log-src">${item.logger}</span>
          <span class="log-msg">${escapeHtml(item.message)}</span>
        </div>
      `).join('');
      if (wasAtBottom) box.scrollTop = box.scrollHeight;
    }

    function escapeHtml(str) {
      return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;');
    }

    async function clearUserLogs() {
      if (!currentUser) return;
      await api(`/api/users/${encodeURIComponent(currentUser)}/logs/clear`, { method: 'POST', body: '{}' });
      userLogs = [];
      lastLogId = 0;
      renderLogs();
    }

    function downloadUserLogs() {
      if (!currentUser) return;
      const text = userLogs.map(l => `${l.timestamp} ${l.level.padEnd(7)} ${l.logger}: ${l.message}`).join('\\n');
      const blob = new Blob([text], { type: 'text/plain;charset=utf-8' });
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = `${currentUser}_logs.txt`;
      document.body.appendChild(a);
      a.click();
      a.remove();
    }

    document.getElementById('new-user-input').addEventListener('keydown', e => {
      if (e.key === 'Enter') openOrCreateUser();
    });
    document.getElementById('hero-user-input').addEventListener('keydown', e => {
      if (e.key === 'Enter') openFromHero();
    });
    document.getElementById('console-cmd-input').addEventListener('keydown', e => {
      if (e.key === 'Enter') runCustomCommand();
    });

    loadUserList();
    setInterval(() => {
      if (currentUser) refreshActiveUser();
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

            # List or create users
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

            # User-scoped routes: /api/users/<username>/...
            if path.startswith("/api/users/"):
                segments = [unquote(s) for s in path.split("/") if s]
                # segments: ['api', 'users', '<username>', ...]
                if len(segments) >= 3:
                    uname = sanitize_username(segments[2])
                    subpath = "/".join(segments[3:])
                    ws = await manager.get_or_create(uname)

                    if subpath == "state" and method == "GET":
                        _send_json(writer, 200, await ws.to_state_dict())
                        await writer.drain()
                        return

                    if subpath == "config" and method == "POST":
                        data = parse_json_body()
                        await ws.update_config(data)
                        _send_json(writer, 200, {"ok": True, "state": await ws.to_state_dict()})
                        await writer.drain()
                        return

                    if subpath == "login" and method == "POST":
                        data = parse_json_body()
                        ok, msg = await ws.login_with_password(data)
                        _send_json(writer, 200, {"ok": ok, "message": msg, "state": await ws.to_state_dict()})
                        await writer.drain()
                        return

                    if subpath == "cookie" and method == "POST":
                        data = parse_json_body()
                        ok, msg = await ws.login_with_cookie(
                            str(data.get("cookie") or ""),
                            proxy=data.get("proxy"),
                        )
                        _send_json(writer, 200, {"ok": ok, "message": msg, "state": await ws.to_state_dict()})
                        await writer.drain()
                        return

                    if subpath == "session/import" and method == "POST":
                        data = parse_json_body()
                        ok, msg = await ws.import_session(str(data.get("session_json") or ""))
                        _send_json(writer, 200, {"ok": ok, "message": msg, "state": await ws.to_state_dict()})
                        await writer.drain()
                        return

                    if subpath == "session/download" and method == "GET":
                        session_obj = await ws.get_session_json()
                        if not session_obj:
                            _send_json(writer, 404, {"error": "No session saved yet for this user."})
                            await writer.drain()
                            return
                        raw_bytes = json.dumps(session_obj, indent=2, default=str).encode("utf-8")
                        headers = (
                            "HTTP/1.1 200 OK\r\n"
                            "Content-Type: application/json; charset=utf-8\r\n"
                            f'Content-Disposition: attachment; filename="{ws.username}_session.json"\r\n'
                            f"Content-Length: {len(raw_bytes)}\r\n"
                            "Connection: close\r\n\r\n"
                        ).encode("utf-8")
                        writer.write(headers + raw_bytes)
                        await writer.drain()
                        return

                    if subpath == "code" and method == "POST":
                        data = parse_json_body()
                        code = str(data.get("code") or "").strip()
                        accepted = ws.ig.auth.prompter.submit(code)
                        if accepted:
                            add_user_log(ws.username, "INFO", "auth", "Verification code submitted from Web UI.")
                        _send_json(writer, 200, {"ok": accepted})
                        await writer.drain()
                        return

                    if subpath == "command" and method == "POST":
                        data = parse_json_body()
                        cmd_str = str(data.get("command") or "").strip()
                        add_user_log(ws.username, "INFO", "console", f"> {cmd_str}")
                        reply = await ws.run_command(cmd_str)
                        add_user_log(ws.username, "INFO", "console", reply)
                        _send_json(writer, 200, {"reply": reply})
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

            # Serve main web application
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
