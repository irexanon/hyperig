"""Settings sub-screens for Features and Downloads (callback prefix "pn:").
Watcher alerts, pace and quiet hours live on the Watchlist screen (modules/watcher.py)."""
from telegram import InlineKeyboardButton as Btn, InlineKeyboardMarkup as Markup

SIZES = (10, 20, 35, 50)


def _back():
    return [Btn("Back", callback_data="nav:settings")]


def _slot(ws):
    return ws.get_attached_slot() or ws.get_active_slot()


def view_for(ws, name: str, notice: str = ""):
    slot = _slot(ws)
    if not slot:
        return "Add and log in to an Instagram account first.", Markup([_back()])
    prefs, head = slot.ctx.prefs, (f"{notice}\n\n" if notice else "")
    if name == "dl":
        mb, audio = slot.cfg.max_media_mb, prefs.get("dl_audio")
        rows = [[Btn(f"[{s} MB]" if s == mb else f"{s} MB", callback_data=f"pn:mb:{s}") for s in SIZES],
                [Btn(f"Audio button: {'on' if audio else 'off'}", callback_data="pn:audio"), *_back()]]
        return (f"{head}<b>Downloads</b>\n\nLargest file: <b>{mb} MB</b> (Telegram bots allow up to 50)\n"
                f"Audio button under reels: <b>{'on' if audio else 'off'}</b>"), Markup(rows)
    # features
    lines, buttons = [], []
    for m in slot.modules.modules:
        if getattr(m, "toggleable", False):
            on = prefs.feature_on(m.name)
            lines.append(f"{m.label or m.name}: <b>{'on' if on else 'off'}</b>")
            buttons.append(Btn(f"{'Turn off' if on else 'Turn on'} {m.name}", callback_data=f"pn:ft:{m.name}"))
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)] + [_back()]
    return f"{head}<b>Features</b>\n\n" + "\n".join(lines) + "\n\nA switched-off feature stops working and its commands say so.", Markup(rows)


async def handle(ws, data: str):
    """Apply a "pn:..." button and return (text, markup) to show."""
    slot = _slot(ws)
    parts = data.split(":")
    act, arg = parts[1], parts[2] if len(parts) > 2 else ""
    if not slot:
        return view_for(ws, "ft")
    prefs, view = slot.ctx.prefs, "ft"
    if act == "ft" and arg:
        await prefs.set_feature(arg, not prefs.feature_on(arg))
    elif act == "dl":
        view = "dl"
    elif act == "mb":
        await slot.apply_config({"max_media_mb": int(arg)})
        view = "dl"
    elif act == "audio":
        await prefs.set("dl_audio", not prefs.get("dl_audio"))
        view = "dl"
    return view_for(ws, view)
