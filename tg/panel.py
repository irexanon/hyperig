"""Settings sub-screens: Features, Downloads, Notifications, Safety, Health. Callback prefix "pn:"."""
import time

from telegram import InlineKeyboardButton as Btn, InlineKeyboardMarkup as Markup

from utils.prefs import PACES

PACE_INFO = {
    "safe": "Safe: about every 20 min. Gentlest on the Instagram login.",
    "normal": "Normal: about every 10 min. Recommended.",
    "fast": "Fast: about every 5 min. Faster alerts, more risk of limits.",
}
SIZES = (10, 20, 35, 50)


def _back(to="pn:home", label="⬅️ Watch Settings"):
    return [Btn(label, callback_data=to)]


def _hour(h: int) -> str:
    return f"{int(h) % 24:02d}:00"


def _slot(ws):
    return ws.get_attached_slot() or ws.get_active_slot()


def home(ws, notice: str = ""):
    return view_for(ws, "home", notice)


def view_for(ws, name: str, notice: str = ""):
    slot = _slot(ws)
    if not slot:
        return "Add and log in to an Instagram account first.", Markup([_back()])
    prefs, head = slot.ctx.prefs, (f"{notice}\n\n" if notice else "")
    if name == "ft":
        rows, lines = [], []
        for m in slot.modules.modules:
            if getattr(m, "toggleable", False):
                on = prefs.feature_on(m.name)
                lines.append(f"{'🟢' if on else '⚪️'} {m.label or m.name}")
                rows.append([Btn(f"{'Turn off' if on else 'Turn on'}: {m.name}", callback_data=f"pn:ft:{m.name}")])
        rows.append(_back())
        return f"{head}<b>🧩 Features</b>\n\n" + "\n".join(lines) + "\n\nA switched-off feature stops working and its commands say so.", Markup(rows)
    if name == "dl":
        mb, audio = slot.cfg.max_media_mb, prefs.get("dl_audio")
        rows = [[Btn(f"{'✅ ' if s == mb else ''}{s} MB", callback_data=f"pn:mb:{s}") for s in SIZES],
                [Btn(f"🎵 Audio button: {'ON' if audio else 'OFF'}", callback_data="pn:audio")], _back()]
        return f"{head}<b>📥 Downloads</b>\n\nLargest file: <b>{mb} MB</b> (Telegram bots allow up to 50).\nAudio button under reels: <b>{'ON' if audio else 'OFF'}</b>", Markup(rows)
    if name == "nt":
        quiet, digest = prefs.get("quiet"), prefs.get("digest")
        text = (
            f"{head}<b>🔔 Notifications</b> (watcher alerts)\n\n"
            f"Send to: <b>{'Forum group' if prefs.get('alerts_to') == 'group' else 'This chat'}</b>\n"
            f"Quiet hours: <b>{(_hour(prefs.get('quiet_start')) + '–' + _hour(prefs.get('quiet_end'))) if quiet else 'off'}</b>\n"
            f"Mode: <b>{'Daily digest at ' + _hour(prefs.get('digest_hour')) if digest == 'daily' else 'Instant'}</b>\n"
            f"Timezone: UTC{prefs.tz_hours(slot.cfg):+g}"
        )
        rows = [
            [Btn("📍 Alerts: " + ("group" if prefs.get("alerts_to") == "dm" else "this chat"), callback_data="pn:to")],
            [Btn(f"🌙 Quiet hours: {'ON' if quiet else 'OFF'}", callback_data="pn:q"),
             Btn("−1h start", callback_data="pn:qs:-1"), Btn("+1h start", callback_data="pn:qs:1")],
            [Btn("−1h end", callback_data="pn:qe:-1"), Btn("+1h end", callback_data="pn:qe:1")],
            [Btn("📰 Daily digest" if digest != "daily" else "⚡ Instant", callback_data="pn:dg"),
             Btn("−1h", callback_data="pn:dh:-1"), Btn("+1h", callback_data="pn:dh:1")],
            _back(),
        ]
        return text, Markup(rows)
    if name == "sf":
        pace = prefs.get("pace")
        rows = [[Btn(f"{'✅ ' if p == pace else ''}{p.title()}", callback_data=f"pn:pace:{p}") for p in PACES], _back()]
        return f"{head}<b>🛡 Safety</b>\n\nHow gently the watcher talks to Instagram. Fewer, slower requests lower the chance of limits.\n\n" + "\n".join(PACE_INFO[p] for p in PACES), Markup(rows)
    if name == "hl":
        w = slot.modules.get("watcher")
        h = w.health() if w else None
        lines = [f"<b>🩺 Health</b> · @{slot.ig_username}", f"Instagram: {'🟢 logged in' if slot.ig.logged_in else '🔴 not logged in (/relogin)'}"]
        if h:
            ago = lambda t: "never" if not t else f"{int((time.time() - t) // 60)} min ago"
            lines += [f"Watcher: {h['accounts']} account(s), {h['paused']} paused, last pass {ago(h['last_check'])}"]
            if h["paused_until"] > time.time():
                lines.append(f"⏸ Paused for another {int((h['paused_until'] - time.time()) // 60) + 1} min (rate limit)")
            lines.append(f"Alerts held back: {h['queued']}")
            lines += [f"⚠️ {ago(t)} @{u}: {e}" for t, u, e in h["errors"][-5:]] or ["No recent errors."]
        return head + "\n".join(lines), Markup([[Btn("🔄 Refresh", callback_data="pn:hl")], _back()])
    # home
    return (
        f"{head}<b>👁 Watch Settings</b>\n\nWhat to watch, which features are on, and how alerts and pacing behave for @{slot.ig_username}.",
        Markup([[Btn("👁 Watchlist (what to watch)", callback_data="nav:watch")],
                [Btn("🧩 Features on/off", callback_data="pn:ft"), Btn("🔔 Notifications", callback_data="pn:nt")],
                [Btn("🛡 Safety / pace", callback_data="pn:sf"), Btn("📥 Downloads", callback_data="pn:dl")],
                [Btn("🩺 Health", callback_data="pn:hl")], _back("nav:settings", "⬅️ Settings")]),
    )


async def handle(ws, data: str):
    """Apply a "pn:..." button and return (text, markup) to show."""
    slot = _slot(ws)
    parts = data.split(":")
    act, arg = parts[1], parts[2] if len(parts) > 2 else ""
    if not slot:
        return view_for(ws, "home")
    prefs, view = slot.ctx.prefs, act
    if act == "ft":
        if arg:
            await prefs.set_feature(arg, not prefs.feature_on(arg))
        view = "ft"
    elif act == "mb":
        await slot.apply_config({"max_media_mb": int(arg)})
        view = "dl"
    elif act == "audio":
        await prefs.set("dl_audio", not prefs.get("dl_audio"))
        view = "dl"
    elif act == "to":
        await prefs.set("alerts_to", "group" if prefs.get("alerts_to") == "dm" else "dm")
        view = "nt"
    elif act == "q":
        await prefs.set("quiet", not prefs.get("quiet"))
        view = "nt"
    elif act in ("qs", "qe", "dh"):
        key = {"qs": "quiet_start", "qe": "quiet_end", "dh": "digest_hour"}[act]
        await prefs.set(key, (int(prefs.get(key)) + int(arg)) % 24)
        view = "nt"
    elif act == "dg":
        await prefs.set("digest", "instant" if prefs.get("digest") == "daily" else "daily")
        view = "nt"
    elif act == "pace" and arg in PACES:
        await prefs.set("pace", arg)
        view = "sf"
    return view_for(ws, view if act != "home" else "home")
