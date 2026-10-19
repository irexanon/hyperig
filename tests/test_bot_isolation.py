"""A bot token serves only the logins that belong to it. Run: python -m pytest tests/test_bot_isolation.py"""
import sys
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tg.bridge import _TokenHub  # noqa: E402


def hub_for(token, default="DEFAULT"):
    hub = _TokenHub.__new__(_TokenHub)
    hub.token = token
    hub._manager = NS(base_cfg=NS(tg_token=default), workspaces={})
    return hub


def ws(name, token="", admin=0):
    return NS(username=name, tg_token=token, tg_admin_id=admin)


def test_default_bot_serves_logins_without_their_own_bot():
    hub = hub_for("DEFAULT")
    a, b = ws("a", admin=11), ws("b", token="B_TOKEN", admin=22)
    hub._manager.workspaces = {"a": a, "b": b}
    assert hub._is_default_bot()
    assert hub._find_workspace_for_user(11) is a
    assert hub._find_workspace_for_user(22) is None      # b has its own bot, the default one does not serve it


def test_private_bot_serves_only_its_owner():
    hub = hub_for("B_TOKEN")
    a, b = ws("a", admin=11), ws("b", token="B_TOKEN", admin=22)
    hub._manager.workspaces = {"a": a, "b": b}
    assert not hub._is_default_bot()
    assert hub._find_workspace_for_user(22) is b
    assert hub._find_workspace_for_user(11) is None      # user c / a login on the default bot cannot use B's bot
    text, markup = hub._view_welcome()
    assert [b.callback_data for r in markup.inline_keyboard for b in r] == ["auth:login"]   # no Register
    assert "private bot" in text


def test_default_bot_still_offers_register():
    hub = hub_for("DEFAULT")
    _text, markup = hub._view_welcome()
    assert [b.callback_data for r in markup.inline_keyboard for b in r] == ["auth:login", "auth:register"]
