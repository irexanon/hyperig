"""One user's Instagram connection. Login/session logic lives in core/auth.py.

There is no inbox polling and no realtime connection: the client only talks to Instagram when a command,
a pasted link or the watcher asks it to. Every call goes through one lock because instagrapi's Client is
not thread-safe.
"""
import asyncio
from dataclasses import replace

from core.auth import AuthManager, CookieError, describe_error, parse_cookie_input
from utils.logger import get_logger

log = get_logger("instagram")


class InstagramClient:
    def __init__(self, cfg, store):
        self.cfg = cfg
        self.store = store
        self.auth = AuthManager(cfg, store)
        self.logged_in = False
        self.last_alert = ""
        self.on_alert = None            # async callable(text): tells the user about login prompts and problems
        self._lock = asyncio.Lock()     # one instagrapi call at a time

    # The instagrapi Client lives in the auth manager (it is rebuilt on a fresh login).
    @property
    def cl(self):
        return self.auth.cl

    @property
    def username(self) -> str:
        return self.cfg.ig_username or getattr(self.cl, "username", "") or ""

    async def _call(self, fn, *args):
        async with self._lock:
            return await asyncio.to_thread(fn, *args)

    async def _say(self, text: str) -> None:
        if self.on_alert:
            await self.on_alert(text)

    def _set_cfg(self, **changes) -> None:
        self.cfg = replace(self.cfg, **changes)
        self.auth.cfg = self.cfg

    async def _save_session(self, force: bool = False) -> None:
        settings = await self._call(self.cl.get_settings)
        await self.auth.save_session(settings, force=force)

    def _bind(self) -> None:
        self.auth.bind(asyncio.get_running_loop(), self._say)

    # ------------------------------------------------------------------ connecting
    async def login_with_password(self, username: str, password: str, totp_secret: str | None = None) -> tuple[bool, str]:
        """Log in with a password. The password is kept in memory only for this attempt. Never raises."""
        self._bind()
        await self.auth.forget_session()
        self._set_cfg(ig_username=username, ig_password=password, ig_totp_secret=totp_secret, ig_sessionid=None)
        try:
            await self._call(self.auth.login_sync, None)
        except Exception as e:
            self.logged_in = False
            info = describe_error(e, self.cl)
            self.last_alert = info.text
            log.info("Password login failed (%s)", type(e).__name__)
            return False, info.text
        finally:
            self._set_cfg(ig_password=None)  # never keep it
        self.logged_in = True
        self.last_alert = ""
        await self._save_session(force=True)
        return True, f"Connected as @{self.username} ({self.auth.login_method})."

    async def login_with_cookie(self, raw: str) -> tuple[bool, str]:
        """Log in with a browser sessionid cookie. Never raises."""
        self._bind()
        try:
            sessionid = parse_cookie_input(raw)
        except CookieError as e:
            return False, str(e)
        self._set_cfg(ig_username="", ig_password=None)
        try:
            await self._call(self.auth.cookie_login_sync, sessionid, False, None)
        except CookieError as e:
            return False, str(e)
        except Exception as e:
            log.info("Cookie login failed (%s)", type(e).__name__)
            info = describe_error(e, self.cl)
            extra = "" if info.kind == "other" else f" {info.text}"
            return False, (
                f"Instagram rejected that cookie ({type(e).__name__}). It may be expired, or from a browser you "
                f"logged out of.{extra}"
            )
        self._set_cfg(ig_username=getattr(self.cl, "username", "") or "")
        self.logged_in = True
        self.last_alert = ""
        await self._save_session(force=True)
        return True, f"Connected as @{self.username} (cookie)."

    async def restore_saved_session(self) -> bool:
        """Use the session saved earlier, without a new login. Returns False if there is none or it expired."""
        self._bind()
        session = await self.auth.load_session()
        if not (session or {}).get("authorization_data") and not (session or {}).get("cookies", {}).get("sessionid"):
            self.logged_in = False
            return False
        meta = await self.store.get(self.auth.META) or {}
        if meta.get("username") and not self.cfg.ig_username:
            self._set_cfg(ig_username=meta["username"])

        def _validate():
            cl = self.auth._build_client(session)
            cl.account_info()
            self.auth.login_method = "session"

        try:
            await self._call(_validate)
        except Exception as e:
            self.logged_in = False
            log.info("Saved session is no longer valid (%s)", type(e).__name__)
            return False
        self.logged_in = True
        self.last_alert = ""
        return True

    async def logout(self) -> None:
        """Forget the saved session. Nothing is done on Instagram's side: logging out there would also end the
        session you are using in the app or browser."""
        self.logged_in = False
        self.auth.prompter.cancel()
        await self.auth.forget_session()

    async def stop(self) -> None:
        self.auth.prompter.cancel()
        if self.logged_in:
            try:
                await self._save_session(force=True)
            except Exception:
                log.exception("Could not save the session on shutdown")
