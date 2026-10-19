"""Mocked end-to-end checks for the watcher (no Instagram, no Telegram). Run: python -m pytest tests/test_watcher.py"""
import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.context import AppContext  # noqa: E402
from core.module_manager import ModuleManager  # noqa: E402
from utils.prefs import Prefs  # noqa: E402

T0 = datetime(2026, 10, 18, tzinfo=timezone.utc)


def post(pk, caption="hello", likes=1, comments=0, product="feed", mins=0):
    return NS(pk=str(pk), code=f"C{pk}", media_type=2 if product == "clips" else 1, product_type=product,
              taken_at=T0 + timedelta(minutes=mins), caption_text=caption, like_count=likes, comment_count=comments,
              thumbnail_url=f"http://x/{pk}.jpg", video_url=None, resources=[])


class Cl:
    def __init__(self):
        self.posts, self.stories, self.hls = [post(1, mins=1)], [], []
        self.user = dict(pk=77, full_name="Nasa", biography="bio", external_url=None, category="Space",
                         category_name=None, is_verified=False, follower_count=900, is_private=False,
                         profile_pic_url_hd="http://x/p.jpg?sig=1", profile_pic_url=None)

    def user_info_by_username(self, u, use_cache=True):
        return NS(username=u, **self.user)

    def user_medias(self, pk, n):
        return sorted(self.posts, key=lambda m: m.taken_at, reverse=True)[:n]

    def user_stories(self, pk):
        return self.stories

    def user_highlights(self, pk):
        return self.hls


class Store:
    def __init__(self):
        self.d = {}

    async def get(self, k, default=None):
        return self.d.get(k, default)

    async def set(self, k, v):
        self.d[k] = v


class Bot:
    def __init__(self):
        self.log = []

    async def send_message(self, chat, text, **kw):
        self.log.append(text)

    async def send_photo(self, chat, **kw):
        self.log.append("photo:" + kw.get("caption", ""))


def build(store, cl, bot):
    async def call(fn):
        return fn()

    async def fetch(url):
        return b"x"

    ig = NS(logged_in=True, cl=cl, _call=call)
    ctx = AppContext(cfg=NS(tg_admin_id=555, ig_timezone_offset=0), ig=ig, store=store,
                     bridge=NS(bot=bot, chat_id=0, _fetch=fetch), prefs=Prefs(store))
    mm = ModuleManager(ctx)
    ctx.modules = mm
    mm.load_modules()
    return ctx, mm.get("watcher")


def run(coro):
    real = asyncio.sleep

    async def fast(*_a, **_k):
        await real(0)

    asyncio.sleep = fast
    try:
        return asyncio.run(coro)
    finally:
        asyncio.sleep = real


def test_everything():
    async def main():
        store, cl, bot = Store(), Cl(), Bot()
        ctx, w = build(store, cl, bot)
        await w.on_start()
        w._task.cancel()
        ok, _ = await w.add_account("nasa", {"p", "s", "pr", "hl", "ed", "en"})
        assert ok and not bot.log                      # baseline is silent
        assert not (await w.add_account("nasa", {"p"}))[0]

        cl.user.update(biography="new bio", follower_count=1200, profile_pic_url_hd="http://x/q.jpg?sig=2")
        cl.posts[0].caption_text = "edited"
        cl.posts[0].like_count = 150
        await w._check_all()
        text = "\n".join(bot.log)
        assert "Bio" in text and "Profile picture" in text and "1,000 followers" in text
        assert "caption edited" in text and "100 likes" in text
        n = len(bot.log)
        await w._check_all()
        assert len(bot.log) == n                       # nothing repeats

        cl.posts = []                                  # the only post disappeared
        await w._check_all()
        assert "no longer there" in "\n".join(bot.log)

        cl.hls = [NS(pk="highlight:5", title="Trip", media_count=3)]
        await w._check_all()
        assert "new highlight" in "\n".join(bot.log)

        # pause, switch off
        await w.update_entry("nasa", paused=True)
        cl.posts.append(post(10, "nothing", mins=60))
        before = len(bot.log)
        await w._check_all()
        assert len(bot.log) == before
        await ctx.prefs.set_feature("watcher", False)
        assert not ctx.modules.is_enabled(w)

        # restart keeps state
        ctx2, w2 = build(store, cl, bot)
        await w2.on_start()
        w2._task.cancel()
        assert "nasa" in w2._watch and w2._watch["nasa"]["paused"]

    run(main())
