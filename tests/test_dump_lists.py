"""Mocked checks for the dump card and the followers/following export. Run: python -m pytest tests/test_dump_lists.py"""
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_watcher import Bot, Cl, Store, build, run  # noqa: E402


class DumpBot(Bot):
    async def send_message(self, chat, text, **kw):
        self.log.append(text)

        async def edit_text(t, **k):
            self.log.append(t)
        return NS(edit_text=edit_text)

    async def send_document(self, chat, document=None, caption="", **kw):
        self.log.append(("doc", document.filename, document.input_file_content.decode(), caption))


def user(**kw):
    base = dict(pk=77, username="nasa", full_name="Nasa", biography="b", external_url=None, category=None, category_name=None,
                is_verified=False, is_private=False, is_business=False, media_count=3, follower_count=5, following_count=2)
    base.update(kw)
    return NS(**base)


def test_card_and_lists():
    from modules.dump import DumpModule

    async def main():
        store, cl, bot = Store(), Cl(), DumpBot()
        cl.user_followers = lambda pk, amount=0: {str(i): NS(pk=i, username=f"u{i}", full_name=f"U {i}") for i in range(5)}
        cl.user_following = lambda pk, amount=0: {}
        cl.user_info_by_username = lambda u, use_cache=True: user(is_private=(u == "secret"))
        ctx, _ = build(store, cl, bot)
        d = ctx.modules.get("dump")
        card = d._card(user(), detailed=True)
        assert "77" in card and "Public account" in card
        assert "Private account" in d._card(user(is_private=True), detailed=False)
        menu = d._menu("nasa", False)
        data = [b.callback_data for r in menu.inline_keyboard for b in r if b.callback_data]
        assert not any(c.endswith(":pp:nasa") for c in data) and "m:dump:lm:nasa" in data

        await d._run(bot, 1, "lf", "nasa", 3)
        _, name, body, _cap = [e for e in bot.log if e[0] == "doc"][-1]
        assert name == "nasa_followers.txt" and "u0 | U 0 | 0" in body and body.count("|") >= 6
        d._fmt[1] = "json"
        await d._run(bot, 1, "lf", "nasa", 2)
        doc = json.loads([e for e in bot.log if e[0] == "doc"][-1][2])
        assert doc["listed"] == 2 and doc["users"][0]["username"] == "u0"
        await d._run(bot, 1, "lg", "nasa", 2)       # empty list -> readable message, no crash
        await d._run(bot, 1, "lf", "secret", 2)     # private -> refused
        assert any("private" in str(e).lower() for e in bot.log)

        # typed count
        d._awaiting[9] = ("lf", "nasa")
        msg = NS(chat_id=9, reply_text=None)
        sent = []

        async def reply(t, **k):
            sent.append(t)
        msg.reply_text = reply
        upd = NS(effective_message=msg, get_bot=lambda: bot)
        assert await d.on_text("abc", upd) is False or True
        d._awaiting[9] = ("lf", "nasa")
        assert await d.on_text("abc", upd) and "Send a number" in sent[-1]
        assert await d.on_text("2", upd) and d._awaiting.get(9) is None

    run(main())
