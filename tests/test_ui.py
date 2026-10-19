"""Mocked checks for the panels: start/status/settings, watchlist screens, progress message."""
import asyncio
import sys
import time
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_watcher import Bot, Cl, Store, build, run  # noqa: E402

from tg import panel  # noqa: E402
from tg.bridge import _TokenHub  # noqa: E402
from utils.progress import Progress, bar  # noqa: E402


def callbacks(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]


def make_ws():
    ctx, w = build(Store(), Cl(), Bot())
    ig = NS(logged_in=True, realtime=NS(rt_connected=True, fbns_connected=False, events=4, pushes=0), auth=NS(login_method="pw"))
    slot = NS(ctx=ctx, modules=ctx.modules, cfg=NS(max_media_mb=20, realtime=True, fbns=False, ig_timezone_offset=0),
              ig_username="me", ig=ig, bridge=NS(topics={"a": 1, "b": 2, "c": 3}, forwarded=9))

    async def apply(u):
        slot.cfg.max_media_mb = u.get("max_media_mb", slot.cfg.max_media_mb)
    slot.apply_config = apply
    ws = NS(get_attached_slot=lambda: slot, get_active_slot=lambda: slot, slots={"me": slot}, bridged_ig="me",
            bridge_enabled=True, tg_chat_id=-100, username="rex")
    return ws, slot, ctx, w


def test_views():
    ws, slot, ctx, w = make_ws()
    hub = _TokenHub.__new__(_TokenHub)

    text, markup = hub._view_home(ws)
    assert markup is None and "/status" not in text and "/dl" not in text and "Paste an Instagram link" in text

    text, markup = hub._view_status(ws)
    assert "Topics: 3" in text and "Watcher:" in text and markup is not None

    text, markup = hub._view_settings(ws)
    rows = [[b.callback_data for b in r] for r in markup.inline_keyboard]
    assert rows[0] == ["nav:accounts", "nav:bridge", "nav:watch"]      # horizontal, Watchlist is its own entry
    assert "Health" not in text and "Realtime" not in text

    text, markup = hub._view_bridge(ws)
    assert "settings:bridge" in callbacks(markup) and not any("rt" in c or "fbns" in c for c in callbacks(markup))


def test_panel_and_watchlist():
    async def main():
        ws, slot, ctx, w = make_ws()
        text, markup = await panel.handle(ws, "pn:ft:watcher")
        assert "off" in text and not ctx.modules.is_enabled(w)
        await panel.handle(ws, "pn:ft:watcher")
        text, _ = await panel.handle(ws, "pn:mb:35")
        assert slot.cfg.max_media_mb == 35
        await ctx.prefs.set("dl_audio", True)
        text, markup = await panel.handle(ws, "pn:audio")
        assert ctx.prefs.get("dl_audio") is False

        await w.on_start()
        w._task.cancel()
        await w.add_account("nasa", {"p"})
        text, markup = w.list_view()
        cbs = callbacks(markup)
        assert "m:watch:ac:nasa" in cbs and "m:watch:cfg:pace:safe" in cbs and not any(c.endswith(":x:-") for c in cbs)

        class Q:
            message = NS(chat_id=1, reply_text=None)
            shown = []

            async def edit_message_text(self, t, **k):
                Q.shown.append(t)
        q = Q()
        await w.on_button("cfg:pace:safe", q, None)
        assert ctx.prefs.get("pace") == "safe" and "Pace: safe" in Q.shown[-1]
        await w.on_button("cfg:dg:daily", q, None)
        assert "daily digest" in Q.shown[-1]
        await w.on_button("pa:-", q, None)
        assert w._watch["nasa"]["paused"]
        await w.on_button("pa:-", q, None)
        assert not w._watch["nasa"]["paused"]
        await w.on_button("aq:1", q, None)
        await w.on_button("aqs:1", q, None)
        assert ctx.prefs.get("quiet") and ctx.prefs.get("quiet_start") == 0 and "Quiet hours: on" in Q.shown[-1]
    run(main())


def test_progress():
    assert bar(0.5).startswith("[#####-----]") and bar(2).endswith("100%")

    async def main():
        log = []

        class Msg:
            async def edit_text(self, t, **k):
                log.append(t)

        class B:
            async def send_message(self, chat, t, **k):
                log.append(t)
                return Msg()
        p = await Progress.begin(B(), 1, "Downloading reel", "@nasa", "Reading link")
        await p.set("Fetching media", 0.5)            # throttled: too soon after the first message
        assert len(log) == 1
        p._last -= 10
        await p.set("Fetching media", 0.5)
        assert "[#####-----]" in log[-1] and "Fetching media" in log[-1]
        await p.done("1 file")
        assert log[-1].startswith("<b>Done</b>")
        await p.fail("x < y")
        assert "&lt;" in log[-1]
    asyncio.run(main())
