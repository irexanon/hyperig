"""Everything about logging in and keeping the Instagram session alive.

This follows instagrapi's own documentation (Best Practices, Login Migration, Challenge
Resolver, Handle Exceptions, TOTP):

* Log in ONCE, persist the settings (device identity + auth + cookies), and always reload them.
  login() validates a saved session and only does a real password login when Instagram rejects it,
  reusing the same device identity.
* The device identity is saved even when a fresh login fails, so the next try is the same "phone"
  instead of yet another new device.
* Locale / country / timezone / app version are applied only when a NEW device is created. They
  are never changed on an existing session (consistency matters more than anything).
* Verification codes (SMS, email, 2FA, backup codes) are asked for on Telegram: the bot messages
  you, you reply /code 123456. The login thread blocks until you answer or the timeout passes.
* handle_exception follows the documented policy: relogin once on LoginRequired (never in a
  loop), resolve resolvable challenges, stop and tell you about manual ones.
* Old saved sessions without a Bloks hash are upgraded automatically (override_app_version).
"""
import asyncio
import concurrent.futures
import hashlib
import json
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import quote, unquote, urlparse

from instagrapi import Client
from instagrapi.config import DEFAULT_APP_VERSION
from instagrapi.exceptions import (
    BadCredentials,
    BadPassword,
    ChallengeRequired,
    ChallengeSelfieCaptcha,
    ChallengeUnknownStep,
    ClientConnectionError,
    ClientError,
    ClientLoginRequired,
    ClientNeedsUpgradeError,
    ClientRequestTimeout,
    ClientThrottledError,
    FeedbackRequired,
    LoginRequired,
    PleaseWaitFewMinutes,
    RecaptchaChallengeForm,
    ReloginAttemptExceeded,
    SelectContactPointRecoveryForm,
    TwoFactorRequired,
)
from instagrapi.mixins.challenge import ChallengeChoice
from instagrapi.utils import json_value

from utils.logger import get_logger

log = get_logger("auth")

RELOGIN_COOLDOWN = 600  # never auto-relogin twice within 10 minutes (prevents login loops)


# --------------------------------------------------------------------------- cookie input
class CookieError(Exception):
    """A cookie problem. The message is written for you and shown on Telegram."""


def parse_cookie_input(raw: str) -> str:
    """Find the Instagram sessionid in whatever you pasted: a bare sessionid, a 'sessionid=...; ...'
    cookie string, or a JSON cookie export (Cookie-Editor / EditThisCookie list, or an object).
    Returns it exactly as pasted (URL-encoded or not). Raises CookieError with advice."""
    text = (raw or "").strip().strip("`'\"")
    value = None
    if text[:1] in "[{":
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            raise CookieError("That looks like JSON but isn't valid JSON (did the chat app change the quotes?).") from None
        if isinstance(data, dict) and isinstance(data.get("cookies"), list):
            data = data["cookies"]
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and item.get("name") == "sessionid":
                    value = item.get("value")
        elif isinstance(data, dict):
            value = data.get("sessionid")
    else:
        match = re.search(r"sessionid\s*[=:]\s*([^;\s\"']+)", text)
        value = match.group(1) if match else text
    sid = str(value or "").strip().strip("\"'")
    decoded = unquote(sid)
    # instagrapi needs: longer than 30 characters, starting with the numeric user id
    if len(decoded) <= 30 or not re.match(r"^\d+:\S+$", decoded):
        raise CookieError(
            "I couldn't find a valid sessionid in that. It looks like 123456789:AbCd...:27:AYx... "
            "(or 123456789%3AAbCd...). Copy the 'sessionid' cookie value from instagram.com."
        )
    return sid


def sessionid_candidates(sid: str) -> list[str]:
    """Browsers store the value URL-encoded (%3A) but the mobile API uses colons. Try the pasted form
    first, then the other one, so either way of copying works."""
    other = unquote(sid) if "%" in sid else quote(sid, safe="")
    return [sid] if other == sid else [sid, other]


# --------------------------------------------------------------------------- codes via Telegram
class CodePrompter:
    """Lets a blocking login thread ask you for a code on Telegram or the Web UI and wait for the answer."""

    def __init__(self, timeout: int):
        self.timeout = timeout
        self.hint: str = ""
        self._loop = None
        self._notify = None
        self._pending: concurrent.futures.Future | None = None
        self._lock = threading.Lock()

    def bind(self, loop, notify) -> None:
        self._loop, self._notify = loop, notify

    @property
    def waiting(self) -> bool:
        with self._lock:
            return self._pending is not None and not self._pending.done()

    def ask(self, hint: str) -> str:
        """Blocking. Call from a worker thread. Returns "" on timeout."""
        fut: concurrent.futures.Future = concurrent.futures.Future()
        with self._lock:
            self._pending = fut
            self.hint = hint
        minutes = max(1, self.timeout // 60)
        text = f"{hint}\nSend the verification code here (waiting {minutes} min)."
        log.info(text)
        if self._loop is not None and self._notify is not None:
            asyncio.run_coroutine_threadsafe(self._notify(text), self._loop)
        try:
            return fut.result(timeout=self.timeout)
        except concurrent.futures.TimeoutError:
            log.warning("No code received within %ss", self.timeout)
            return ""
        finally:
            with self._lock:
                if self._pending is fut:
                    self._pending = None
                    self.hint = ""

    def _resolve(self, value: str) -> bool:
        with self._lock:
            fut = self._pending
        if fut is None or fut.done():
            return False
        try:
            fut.set_result(value)
        except concurrent.futures.InvalidStateError:
            return False
        return True

    def submit(self, code: str) -> bool:
        code = "".join((code or "").split())
        return bool(code) and self._resolve(code)

    def cancel(self) -> None:
        self._resolve("")


# --------------------------------------------------------------------------- error explanations
@dataclass
class ErrorInfo:
    kind: str          # password | 2fa | challenge | feedback | throttle | network | session | other
    text: str          # message for you
    needs_you: bool    # True = automation should stop until you act


def describe_error(e: Exception, cl=None) -> ErrorInfo:
    """Turn any instagrapi exception into advice. Order matters (subclasses first)."""
    detail = str(e)[:300]
    if isinstance(e, ReloginAttemptExceeded):
        return ErrorInfo("password", "Too many re-login attempts. Open the Instagram app, make sure the account is fine, then send /relogin.", True)
    if isinstance(e, ClientNeedsUpgradeError) or _needs_app_migration(e):
        return ErrorInfo(
            "password",
            "Instagram blocked direct password login from this cloud server IP (CAA fallback returned needs_upgrade). "
            "Log in using a browser session cookie instead: copy the 'sessionid' cookie from instagram.com and send "
            "/cookie <sessionid> on Telegram (or in the web console), or set IG_SESSIONID in .env.",
            True,
        )
    if isinstance(e, (BadPassword, BadCredentials)):
        return ErrorInfo(
            "password",
            "Instagram rejected the login. If the password is right, this is usually a risk check on the IP or device: "
            "open the Instagram app on the same network, approve any 'was this you?' prompt, then send /relogin, "
            "or log in with a browser cookie using /cookie <sessionid>. "
            f"I will not retry automatically. ({detail})",
            True,
        )
    if isinstance(e, TwoFactorRequired):
        return ErrorInfo("2fa", "Instagram needs a 2FA code and none arrived in time. Send /relogin and reply with /code, or set IG_TOTP_SECRET.", True)
    if isinstance(e, ChallengeSelfieCaptcha):
        return ErrorInfo("challenge", "Instagram wants a selfie/captcha. Complete it in the official Instagram app, then send /relogin.", True)
    if isinstance(e, (RecaptchaChallengeForm, SelectContactPointRecoveryForm, ChallengeUnknownStep)):
        return ErrorInfo("challenge", f"Instagram started a security check that can't be automated. Finish it in the Instagram app, then send /relogin. ({type(e).__name__})", True)
    if isinstance(e, ChallengeRequired):
        return ErrorInfo("challenge", f"Instagram wants a security check. Approve it in the Instagram app, then send /relogin. ({detail})", True)
    if isinstance(e, FeedbackRequired):
        message = ""
        if cl is not None:
            try:
                message = str((cl.last_json or {}).get("feedback_message", ""))[:200]
            except Exception:
                pass
        return ErrorInfo("feedback", f"Instagram restricted this action ({message or 'feedback_required'}). Cooling down.", False)
    if isinstance(e, (PleaseWaitFewMinutes, ClientThrottledError)):
        return ErrorInfo("throttle", f"Instagram is rate-limiting us ({type(e).__name__}). Backing off.", False)
    if isinstance(e, (LoginRequired, ClientLoginRequired)):
        return ErrorInfo("session", "The Instagram session expired.", True)
    if isinstance(e, (ClientConnectionError, ClientRequestTimeout)):
        return ErrorInfo("network", f"Network problem talking to Instagram ({type(e).__name__}).", False)
    return ErrorInfo("other", f"{type(e).__name__}: {detail}", False)


def _needs_app_migration(e: Exception) -> bool:
    """Recognize both missing-Bloks migrations and Instagram's explicit needs_upgrade response."""
    s = str(e).lower()
    error_type = str(getattr(e, "error_type", "") or "").strip().lower()
    return (
        error_type == "needs_upgrade"
        or type(e).__name__ == "ClientNeedsUpgradeError"
        or "your version of instagram is out of date" in s
        or "bloks_versioning_id" in s
        or "bloks hash" in s
    )


def _mask_proxy(proxy: str | None) -> str:
    if not proxy:
        return "none"
    p = urlparse(proxy if "://" in proxy else f"http://{proxy}")
    return f"{p.scheme}://{p.hostname}:{p.port}" if p.hostname else "set"


# --------------------------------------------------------------------------- the manager
class AuthManager:
    SESSION = "session"
    META = "session_meta"

    def __init__(self, cfg, store, client_factory=Client):
        self.cfg = cfg
        self.store = store
        self.client_factory = client_factory
        self.prompter = CodePrompter(cfg.code_timeout)
        self.login_method = "none"        # session | password | sessionid | cookie
        self.upgrade_note = ""            # why the password upgrade after /cookie did not happen
        self._loop = None
        self._notify = None
        self._in_login = False
        self._last_relogin = float("-inf")  # monotonic() counts from boot, so 0.0 would block a fresh container
        self._saved_hash: str | None = None
        self._custom_profile = False
        self.cl = self._build_client(None)

    # ------------------------------------------------------------- wiring
    def bind(self, loop, notify) -> None:
        """notify: async callable(text) that posts to Telegram."""
        self._loop, self._notify = loop, notify
        self.prompter.bind(loop, notify)

    def notify_threadsafe(self, text: str) -> None:
        if self._loop and self._notify:
            asyncio.run_coroutine_threadsafe(self._notify(text), self._loop)

    # ------------------------------------------------------------- client construction
    def _build_client(self, session: dict | None):
        cfg = self.cfg
        cl = self.client_factory(
            settings=session or None,
            proxy=cfg.ig_proxy,
            delay_range=cfg.ig_delay_range,
            override_app_version=cfg.ig_override_app_version,
            tls_verify=cfg.ig_tls_verify,
        )
        self._custom_profile = False
        if cfg.ig_app_version and not (session or {}).get("authorization_data"):
            # A logged-in saved session keeps the app version it was created with. Anything else
            # (new device, or a device identity left behind by a failed login) uses the configured one.
            self._apply_app_profile(cl)
        if not session:
            # Brand-new device: apply the identity you configured. Never done for saved sessions.
            if cfg.ig_locale:
                cl.set_locale(cfg.ig_locale)
            if cfg.ig_country:
                cl.set_country(cfg.ig_country)
            if cfg.ig_country_code is not None:
                cl.set_country_code(cfg.ig_country_code)
            if cfg.ig_timezone_offset is not None:
                cl.set_timezone_offset(cfg.ig_timezone_offset)
        if cfg.ig_phone_number:
            cl.phone_number = cfg.ig_phone_number  # for "confirm your phone" challenges
        cl.challenge_code_handler = self._challenge_code_handler
        cl.change_password_handler = self._change_password_handler
        cl.handle_exception = self._handle_exception
        self.cl = cl
        return cl

    def _apply_app_profile(self, cl) -> None:
        """Present as IG_APP_VERSION. A version instagrapi ships is used as is; any other version is built
        from IG_APP_VERSION_CODE / IG_BLOKS_VERSIONING_ID. Without a Bloks hash the CAA login cannot work,
        so the legacy login flow is used instead (see _attempt_login)."""
        cfg = self.cfg
        try:
            cl.set_app(cfg.ig_app_version)
            return
        except ValueError:
            pass
        profile = {"app_version": cfg.ig_app_version}
        if cfg.ig_app_version_code:
            profile["version_code"] = cfg.ig_app_version_code
        if cfg.ig_bloks_versioning_id:
            profile["bloks_versioning_id"] = cfg.ig_bloks_versioning_id
        cl.set_app(profile)
        cl.set_user_agent()  # rebuild the User-Agent so it names the new version
        self._custom_profile = True
        if not cfg.ig_app_version_code:
            log.warning("IG_APP_VERSION=%s has no IG_APP_VERSION_CODE; the version code of the default profile is used", cfg.ig_app_version)

    # ------------------------------------------------------------- handlers (run in worker thread)
    def _challenge_code_handler(self, username, choice):
        # Must BLOCK until the code is known: returning "" on the first call aborts the flow.
        via = {ChallengeChoice.SMS: "SMS", ChallengeChoice.EMAIL: "email"}.get(choice, str(choice))
        return self.prompter.ask(f"Instagram sent a verification code to @{username} by {via}.")

    def _change_password_handler(self, username):
        msg = (
            "Instagram requires a password change for this account. Change it in the Instagram app, "
            "update IG_PASSWORD, then send /relogin."
        )
        self.notify_threadsafe("🔒 " + msg)
        raise ChallengeRequired(msg)

    def _handle_exception(self, client, e):
        """instagrapi calls this on any private-API error. Returning normally = retry the request
        once; raising = give up. Mirrors the documented policy."""
        if isinstance(e, BadPassword):
            log.warning("bad_password from Instagram: %s", e)
            if client.relogin_attempt > 0:
                raise ReloginAttemptExceeded(e)
            raise e
        if isinstance(e, (LoginRequired, ClientLoginRequired)):
            if self._in_login or not self.cfg.ig_password:
                raise e  # login() handles this itself
            if time.monotonic() - self._last_relogin < RELOGIN_COOLDOWN:
                log.warning("Session rejected again within %ss of a re-login, giving up", RELOGIN_COOLDOWN)
                raise e
            log.warning("login_required: session rejected, re-logging in once with the same device")
            self._relogin_core(client)
            return
        if isinstance(e, ChallengeRequired):
            api_path = json_value(client.last_json, "challenge", "api_path")
            if api_path == "/challenge/":
                log.warning("Generic challenge flow needs manual handling")
                raise e
            client.challenge_resolve(client.last_json)  # uses the Telegram code handler; raises if it can't
            return
        if isinstance(e, FeedbackRequired):
            log.warning("feedback_required: %s", (client.last_json or {}).get("feedback_message", ""))
        elif isinstance(e, ClientThrottledError):
            log.warning("HTTP 429 from Instagram, back off and review proxy/account pressure")
        elif isinstance(e, PleaseWaitFewMinutes):
            log.warning("Instagram says: please wait a few minutes (%s)", e)
        raise e

    # ------------------------------------------------------------- login (all of these block)
    def _totp(self, cl) -> str:
        return cl.totp_generate_code(self.cfg.ig_totp_secret) if self.cfg.ig_totp_secret else ""

    def _caa_login(self, cl, username: str, password: str, relogin: bool = False, verification_code: str = "") -> bool:
        """Run Android CAA login without falling back to the retired /api/v1/accounts/login/ endpoint."""
        cl.username = (username or "").strip()
        cl.password = password
        if not cl.username or not cl.password:
            raise BadCredentials("Both username and password must be provided.")
        if relogin:
            cl._clear_session_state(
                clear_authorization_data=True,
                clear_authorization_header=True,
                clear_private_cookies=True,
                clear_public_cookies=True,
            )
            if cl.relogin_attempt > 1:
                raise ReloginAttemptExceeded()
            cl.relogin_attempt += 1
        if cl.user_id and not relogin:
            try:
                cl.account_info()
                return True
            except LoginRequired:
                return self._caa_login(cl, username, password, relogin=True, verification_code=verification_code)
        if not cl.bloks_versioning_id:
            raise ClientError(
                "CAA login requires bloks_versioning_id for the saved app profile. "
                "Load settings with override_app_version=True to use the supported app profile."
            )
        outcome = cl.bloks_caa_login(verification_code=verification_code)
        logged = bool(outcome.get("logged_in"))
        if not logged:
            context = cl._extract_two_step_verification_context(outcome)
            if context:
                exc = TwoFactorRequired(
                    "Instagram returned a Bloks two-factor context from the CAA login flow; provide verification_code",
                    response=cl.last_response,
                    **cl._exception_context(outcome),
                )
                if not verification_code.strip():
                    raise exc
                logged = cl._login_with_bloks_two_factor(verification_code, outcome, exc)
            if not logged:
                rejection = cl._caa_extract_rejection_message(outcome.get("result"))
                markers = cl._caa_result_action_markers(outcome)
                reason = rejection or ", ".join(markers) or str(outcome.get("reason") or "CAA login rejected")
                raise ClientNeedsUpgradeError(reason)
        cl.login_flow()
        cl.last_login = time.time()
        cl.relogin_attempt = 0
        return True

    def _attempt_login(self, cl, relogin: bool) -> None:
        cfg = self.cfg
        legacy = cfg.ig_legacy_login or (self._custom_profile and not cfg.ig_bloks_versioning_id)
        login = cl.login_legacy if legacy else (lambda u, p, relogin=False, verification_code="": self._caa_login(cl, u, p, relogin=relogin, verification_code=verification_code))
        code = self._totp(cl)
        try:
            login(cfg.ig_username, cfg.ig_password, relogin=relogin, verification_code=code)
        except TwoFactorRequired:
            # Not covered by IG_TOTP_SECRET: ask for the SMS / authenticator / backup code on Telegram.
            code = self.prompter.ask("Instagram needs your 2FA code (authenticator app, SMS or 8-digit backup code).")
            if not code:
                raise
            login(cfg.ig_username, cfg.ig_password, relogin=relogin, verification_code=code)

    def _password_login(self, cl, relogin: bool = False) -> None:
        if not self.cfg.ig_password:
            raise BadCredentials("No IG_PASSWORD configured, so a password login is not possible")
        try:
            self._attempt_login(cl, relogin)
        except ClientError as e:
            current_ver = (getattr(cl, "device_settings", None) or {}).get("app_version")
            already_current = bool(
                cl.override_app_version
                or (current_ver == DEFAULT_APP_VERSION and getattr(cl, "bloks_versioning_id", None))
            )
            if not _needs_app_migration(e) or already_current:
                raise
            # Older saved session without a Bloks hash: upgrade the app profile, keep device + cookies.
            log.warning("Instagram rejected the old app profile (%s); retrying once with %s", current_ver, DEFAULT_APP_VERSION)
            cl.override_app_version = True
            cl.set_settings(cl.get_settings())
            if self.cfg.ig_proxy:
                cl.set_proxy(self.cfg.ig_proxy)
            self._attempt_login(cl, relogin)

    def _relogin_core(self, cl) -> None:
        self._in_login = True
        self._last_relogin = time.monotonic()
        try:
            cl.relogin_attempt = 0
            self._password_login(cl, relogin=True)
            self.login_method = "password"
        finally:
            self._in_login = False

    def login_sync(self, session: dict | None) -> str:
        """Build a client from the saved session (if any) and log in. Returns how it logged in."""
        self._in_login = True
        try:
            cl = self._build_client(session)
            has_auth = bool((session or {}).get("authorization_data"))
            if not has_auth and self.cfg.ig_sessionid:
                try:
                    cl.login_by_sessionid(self.cfg.ig_sessionid)
                    self._check_cookie_account(cl)
                    self.login_method = "sessionid"
                    return self.login_method
                except Exception as e:
                    log.warning("sessionid login failed (%s), falling back to password", type(e).__name__)
                    cl = self._build_client(session)  # clean client, same device identity
            self._password_login(cl)
            self.login_method = "session" if has_auth else "password"
            return self.login_method
        finally:
            self._in_login = False


    def _check_cookie_account(self, cl) -> None:
        """login_by_sessionid fetches the account's own profile, so cl.username is who the cookie is for.
        Never run as the wrong account."""
        owner = (getattr(cl, "username", None) or "").lower()
        if owner and self.cfg.ig_username and owner != self.cfg.ig_username.lower():
            raise CookieError(f"That cookie belongs to @{owner}, but IG_USERNAME is @{self.cfg.ig_username}. Not logging in.")

    def cookie_login_sync(self, sessionid: str, upgrade: bool, session: dict | None) -> str:
        """Log in with a browser sessionid (sent via /cookie). Keeps the stored device identity but drops
        stale tokens. If `upgrade`, then also try a password login from this machine on top of it and
        quietly stay on the cookie session if that fails. No server-side logout is ever done (it would
        destroy the only working session). A failure puts the previous client back. Returns the method."""
        previous = self.cl
        self._in_login = True
        try:
            base = {k: v for k, v in (session or {}).items() if k not in ("authorization_data", "cookies")} or None
            cl, error = None, None
            for candidate in sessionid_candidates(sessionid):
                cl = self._build_client(base)
                try:
                    cl.login_by_sessionid(candidate)
                    error = None
                    break
                except Exception as e:
                    error = e
                    log.info("sessionid form rejected (%s), trying the other form", type(e).__name__)
            if error is not None:
                raise error
            self._check_cookie_account(cl)
            self.login_method = "cookie"
            self.upgrade_note = ""
            if upgrade and self.cfg.ig_password:
                cookie_settings = cl.get_settings()
                try:
                    cl.relogin_attempt = 0
                    self._password_login(cl, relogin=True)
                    self.login_method = "password"
                except Exception as e:
                    log.warning("Password login on top of the cookie failed (%s), keeping the cookie session", type(e).__name__)
                    self.upgrade_note = describe_error(e, cl).text
                    self._build_client(cookie_settings)  # restore the working cookie session
                    self.login_method = "cookie"
            return self.login_method
        except Exception:
            self.cl = previous  # a bad cookie must never replace the client we already had
            raise
        finally:
            self._in_login = False

    def relogin_sync(self) -> None:
        """Forced fresh login that keeps the device identity (used by /relogin)."""
        self._relogin_core(self.cl)

    def logout_sync(self) -> None:
        self.cl.logout()

    # ------------------------------------------------------------- persistence
    async def load_session(self) -> dict | None:
        data = await self.store.get(self.SESSION)
        if not isinstance(data, dict) or "uuids" not in data:
            return None
        meta = await self.store.get(self.META) or {}
        saved_user = (meta.get("username") or "").lower()
        if saved_user and self.cfg.ig_username and saved_user != self.cfg.ig_username.lower():
            log.warning("Saved session belongs to @%s but IG_USERNAME is @%s. Ignoring it.", saved_user, self.cfg.ig_username)
            return None
        return data

    async def save_session(self, settings: dict, force: bool = False) -> None:
        """Persist settings. A logged-out device identity is saved only when nothing better exists,
        so a failed login can never overwrite a working session."""
        authed = bool(settings.get("authorization_data"))
        if not authed and await self.store.get(self.SESSION):
            return
        digest = hashlib.sha256(json.dumps(settings, sort_keys=True, default=str).encode()).hexdigest()
        if not force and digest == self._saved_hash:
            return
        await self.store.set(self.SESSION, settings)
        await self.store.set(
            self.META,
            {"username": self.cfg.ig_username or getattr(self.cl, "username", ""), "saved_at": datetime.now(timezone.utc).isoformat(), "authenticated": authed},
        )
        self._saved_hash = digest

    async def forget_session(self) -> None:
        await self.store.set(self.SESSION, None)
        await self.store.set(self.META, None)
        self._saved_hash = None

    # ------------------------------------------------------------- introspection
    def info(self) -> str:
        """Safe summary for /session. Never includes the password, sessionid or proxy credentials."""
        cl = self.cl
        try:
            device = getattr(cl, "device_settings", {}) or {}
            last_login = getattr(cl, "last_login", None)
            age = f"{int((time.time() - last_login) // 3600)}h ago" if last_login else "unknown"
            acct = self.cfg.ig_username or getattr(cl, "username", None) or "not configured"
            return "\n".join(
                [
                    f"Account: @{acct} (id {getattr(cl, 'user_id', None) or 'not logged in'})",
                    f"Logged in via: {self.login_method}",
                    f"Last login: {age}",
                    f"Device: {device.get('manufacturer', '?')} {device.get('model', '?')} (Android {device.get('android_release', '?')})",
                    f"App version: {device.get('app_version', '?')}",
                    f"Locale/country/tz: {getattr(cl, 'locale', '?')} / {getattr(cl, 'country', '?')} / {getattr(cl, 'timezone_offset', '?')}",
                    f"Proxy: {_mask_proxy(self.cfg.ig_proxy)}",
                    f"2FA auto-code: {'on' if self.cfg.ig_totp_secret else 'off'}",
                    f"Login flow: {'legacy' if self.cfg.ig_legacy_login else 'CAA (default)'}",
                    f"Private requests this run: {getattr(cl, 'private_requests_count', '?')}",
                ]
            )
        except Exception as e:  # never let a diagnostics command crash
            return f"Could not read session info: {type(e).__name__}"
