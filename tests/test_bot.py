"""Fetch IG bot: per-user isolation, connect flow, text handling. No Telegram or Instagram traffic."""
import asyncio
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import Config  # noqa: E402
from tg.bot import FetchIGBot, INTRO  # noqa: E402
from utils.db import FileStore  # noqa: E402


class Msg:
    def __init__(self, text, sent):
        self.text, self.sent, self.deleted = text, sent, False

    async def reply_text(self, text, **kw):
        self.sent.append((text, kw))
        return NS(edit_text=self._noop)

    async def _noop(self, *a, **k):
        pass

    async def delete(self):
        self.deleted = True


def upd(uid, text, sent):
    m = Msg(text, sent)
    return NS(effective_user=NS(id=uid), effective_chat=NS(type="private", id=uid), effective_message=m), m


def make_bot(tmp):
    cfg = Config(tg_token="x", data_dir=Path(tmp))
    bot = FetchIGBot(cfg, FileStore(Path(tmp)))
    bot.app = NS(bot=NS(send_message=lambda *a, **k: asyncio.sleep(0)))
    return bot


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_users_are_isolated():
    async def go():
        with tempfile.TemporaryDirectory() as tmp:
            bot = make_bot(tmp)
            a, b = await bot.session(1), await bot.session(2)
            assert a is not b and a.ig is not b.ig and a.modules is not b.modules
            await a.store.set("session", {"uuids": 1})
            assert await b.store.get("session") is None
            await a.prefs.set("pace", "safe")
            assert b.prefs.get("pace") == "normal"
            assert a.ctx.tg.chat_id == 1 and b.ctx.tg.chat_id == 2
    run(go())


def test_start_is_intro_only_and_plain_text_is_ignored():
    async def go():
        with tempfile.TemporaryDirectory() as tmp:
            bot, sent = make_bot(tmp), []
            u, _ = upd(5, "/start", sent)
            await bot.on_command(u, None)
            assert sent == [(INTRO, {})]
            u, _ = upd(5, "hello there", sent)
            await bot.on_text(u, None)
            assert len(sent) == 1
    run(go())


def test_cookie_connect_flow_deletes_secret_and_starts_modules():
    async def go():
        with tempfile.TemporaryDirectory() as tmp:
            bot, sent = make_bot(tmp), []
            sess = await bot.session(7)
            got = {}

            async def fake_cookie(raw):
                got["raw"] = raw
                sess.ig.logged_in = True
                return True, "Connected as @me (cookie)."

            sess.ig.login_with_cookie = fake_cookie
            sess.awaiting = {"step": "cookie"}
            u, m = upd(7, "abc%3Adef", sent)
            await bot.on_text(u, None)
            await sess.login_task
            assert got["raw"] == "abc%3Adef" and m.deleted and sess.started and sess.awaiting is None
            await sess.stop()
    run(go())


def test_password_flow_never_stores_password_and_code_goes_to_prompter():
    async def go():
        with tempfile.TemporaryDirectory() as tmp:
            bot, sent = make_bot(tmp), []
            sess = await bot.session(8)
            calls = {}

            async def fake_pw(user, pw, totp=None):
                calls["args"] = (user, pw)
                return False, "nope"

            sess.ig.login_with_password = fake_pw
            sess.awaiting = {"step": "username"}
            u, _ = upd(8, "@some.user", sent)
            await bot.on_text(u, None)
            u, m = upd(8, "secret", sent)
            await bot.on_text(u, None)
            await sess.login_task
            assert calls["args"] == ("some.user", "secret") and m.deleted
    run(go())


def test_logout_deletes_only_that_users_data():
    async def go():
        with tempfile.TemporaryDirectory() as tmp:
            bot = make_bot(tmp)
            a, b = await bot.session(1), await bot.session(2)
            for s in (a, b):
                await s.store.set("session", {"uuids": 1})
                await s.store.set("watchlist", {"x": {}})
            await bot.wipe(a)
            assert await a.store.get("session") is None and await a.store.get("watchlist") is None
            assert await b.store.get("session") == {"uuids": 1}
            assert await bot.root_store.get("users") == [2]
            assert 1 not in bot.sessions and 2 in bot.sessions
    run(go())


def test_pasted_link_goes_to_downloader():
    async def go():
        with tempfile.TemporaryDirectory() as tmp:
            bot, sent = make_bot(tmp), []
            sess = await bot.session(3)
            seen = []

            async def fake(args, update):
                seen.append(args)

            cmd, module = sess.modules.all_commands()["dl"]
            sess.modules._commands["dl"] = (type(cmd)(fake, "d", takes_update=True), module)
            u, _ = upd(3, "look https://www.instagram.com/reel/ABC123/?igsh=1 nice", sent)
            await bot.on_text(u, None)
            assert seen == [["https://www.instagram.com/reel/ABC123/?igsh=1"]]
    run(go())


def test_status_command_when_not_connected():
    async def go():
        with tempfile.TemporaryDirectory() as tmp:
            bot, sent = make_bot(tmp), []
            u, _ = upd(4, "/status", sent)
            await bot.on_command(u, None)
            assert "not connected" in sent[0][0]
    run(go())


def test_instagram_client_drops_the_password_after_login():
    async def go():
        with tempfile.TemporaryDirectory() as tmp:
            bot = make_bot(tmp)
            sess = await bot.session(9)
            ig, seen = sess.ig, {}

            def fake_login(session):
                seen["pw"] = ig.cfg.ig_password
                ig.auth.login_method = "password"
                return "password"

            ig.auth.login_sync = fake_login
            ig.auth.cl.get_settings = lambda: {"uuids": {}, "authorization_data": {"ds_user_id": "1"}}
            ok, text = await ig.login_with_password("me", "hunter2")
            assert ok and seen["pw"] == "hunter2" and ig.cfg.ig_password is None
            assert (await sess.store.get("session"))["authorization_data"]
            assert (await sess.store.get("session_meta"))["username"] == "me"
    run(go())
