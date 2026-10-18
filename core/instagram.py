"""Instagram side: poll the inbox and send replies. Login/session logic lives in core/auth.py.

Polling rules (instagrapi Best Practices): read-only monitoring reuses ONE saved session, polls
every 30s+ (never seconds), remembers the last-seen message per thread, and every Instagram call
goes through one lock because instagrapi's Client is not thread-safe.
"""
import asyncio
import random
from copy import deepcopy
from pathlib import Path
from collections import defaultdict
from datetime import datetime, timezone

from core.auth import AuthManager, CookieError, describe_error, parse_cookie_input
from core.models import IncomingMessage
from core.realtime import RealtimeService
from instagrapi.extractors import extract_direct_message
from utils.formatting import render
from utils.logger import get_logger

log = get_logger("instagram")

INIT_KEY = "__init__"       # cursor key holding the time the bridge first started
THREAD_LIMIT = 20           # how many recent threads to check each poll
MESSAGES_PER_THREAD = 10    # how many recent messages to look at per thread
MIN_POLL_GAP = 5            # never sync the inbox more often than this, whatever wakes us
THROTTLE_PAUSE = 900        # seconds to pause after a rate limit / feedback_required


def _aware(dt: datetime) -> datetime:
    """instagrapi builds message times with datetime.fromtimestamp(), i.e. naive LOCAL time. Treating those
    as UTC would shift every message by the server's UTC offset and break the cursor comparison."""
    return dt if dt.tzinfo else dt.astimezone(timezone.utc)


class InstagramClient:
    def __init__(self, cfg, store):
        self.cfg = cfg
        self.store = store
        self.auth = AuthManager(cfg, store)

        self.logged_in = False
        self.last_alert: str = ""
        self.on_alert = None                 # async callable(text), set by main
        self.cursors: dict[str, str] = {}    # ig thread id -> ISO time of last handled message
        self.delivered: dict[str, None] = {}  # ids of messages already sent to Telegram (ordered, bounded)
        self.last_poll: datetime | None = None

        self.meta: dict[str, tuple[str, bool, dict]] = {}   # thread id -> (name, is_group, {user id: username})
        self.realtime = RealtimeService(self)

        self._lock = asyncio.Lock()             # one instagrapi call at a time (the Client is not thread-safe)
        self._deliver_lock = asyncio.Lock()     # one delivery at a time, so poll + realtime never double-send
        self._wake = asyncio.Event()
        self._on_message = None
        self._stop = asyncio.Event()
        self._alert_at: dict[str, float] = {}

    # The instagrapi Client lives in the auth manager (it is rebuilt on a fresh login).
    @property
    def cl(self):
        return self.auth.cl

    @cl.setter
    def cl(self, value):
        self.auth.cl = value

    @property
    def baseline_done(self) -> bool:
        return INIT_KEY in self.cursors

    def wake(self) -> None:
        """Ask the poller to check the inbox now (FBNS push, MQTT reconnect, unparsable realtime item)."""
        self._wake.set()

    # ------------------------------------------------------------------ helpers
    async def _call(self, fn, *args):
        async with self._lock:
            return await asyncio.to_thread(fn, *args)

    async def _say(self, text: str) -> None:
        """Send a message to you on Telegram (used for code prompts and alerts)."""
        if self.on_alert:
            await self.on_alert(text)

    async def _alert(self, key: str, text: str, cooldown: int = 3600) -> None:
        self.last_alert = text
        log.warning(text)
        loop = asyncio.get_running_loop()
        now = loop.time()
        last = self._alert_at.get(key)
        if last is not None and now - last < cooldown:
            return
        self._alert_at[key] = now
        try:
            await self._say(text)
        except Exception:
            log.exception("Could not deliver alert")

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def _sleep_or_wake(self, seconds: float) -> None:
        """Sleep until the next scheduled poll, a wake() or shutdown, but never poll twice within MIN_POLL_GAP."""
        stop = asyncio.ensure_future(self._stop.wait())
        wake = asyncio.ensure_future(self._wake.wait())
        try:
            await asyncio.wait({stop, wake}, timeout=seconds, return_when=asyncio.FIRST_COMPLETED)
        finally:
            stop.cancel()
            wake.cancel()
        self._wake.clear()
        if not self._stop.is_set():
            await self._sleep(MIN_POLL_GAP)

    # -------------------------------------------------------------------- session
    async def _save_session(self, force: bool = False) -> None:
        settings = await self._call(self.cl.get_settings)
        await self.auth.save_session(settings, force=force)

    async def login(self) -> bool:
        """Log in using the saved session if there is one. Never raises."""
        self.auth.bind(asyncio.get_running_loop(), self._say)
        self.cursors = await self.store.get("cursors", {}) or {}
        self.delivered = dict.fromkeys(await self.store.get("delivered", []) or [])
        session = await self.auth.load_session()
        has_saved_auth = bool((session or {}).get("authorization_data"))
        if not self.cfg.ig_username or (not has_saved_auth and not self.cfg.ig_password and not self.cfg.ig_sessionid):
            self.logged_in = False
            log.warning(
                "Instagram credentials not configured (set IG_USERNAME and IG_PASSWORD or IG_SESSIONID, or use /cookie)."
            )
            return False
        log.info("Logging in as @%s (%s)", self.cfg.ig_username, "saved session" if session else "new device")
        try:
            method = await self._call(self.auth.login_sync, session)
        except Exception as e:
            self.logged_in = False
            info = describe_error(e, self.cl)
            await self._alert("login", f"❌ Instagram login failed: {info.text}", cooldown=0)
            # Keep the device identity so the next attempt is the same "phone", not a new one.
            try:
                await self._save_session()
            except Exception:
                log.exception("Could not persist device identity")
            return False
        self.logged_in = True
        self.last_alert = ""
        await self._save_session(force=True)
        log.info("Instagram login OK (%s)", method)
        return True

    async def cookie_login(self, raw: str) -> tuple[str, str]:
        """Log in from a browser cookie sent via /cookie. Returns (method, note): method is "password"
        if the password upgrade worked, else "cookie"; note explains why the upgrade did not happen.
        Raises CookieError (message ready to show you) on any failure; the previous session is untouched."""
        sessionid = parse_cookie_input(raw)
        self.auth.bind(asyncio.get_running_loop(), self._say)
        session = await self.auth.load_session()
        upgrade = self.cfg.ig_cookie_upgrade and bool(self.cfg.ig_password)
        try:
            method = await self._call(self.auth.cookie_login_sync, sessionid, upgrade, session)
        except CookieError:
            raise
        except Exception as e:
            log.warning("Cookie login failed: %s", type(e).__name__)
            info = describe_error(e, self.cl)
            extra = "" if info.kind == "other" else f" {info.text}"
            raise CookieError(
                f"Instagram rejected that cookie ({type(e).__name__}). It may be expired, from a browser you logged "
                f"out of, or a browser session the mobile API won't accept.{extra}"
            ) from None
        self.logged_in = True
        self.last_alert = ""
        await self._save_session(force=True)
        log.info("Instagram login OK (%s via /cookie)", method)
        return method, self.auth.upgrade_note

    async def relogin(self) -> bool:
        """Forced fresh password login that keeps the device identity (/relogin)."""
        self.auth.bind(asyncio.get_running_loop(), self._say)
        try:
            await self._call(self.auth.relogin_sync)
        except Exception as e:
            self.logged_in = False
            info = describe_error(e, self.cl)
            await self._alert("login", f"❌ Re-login failed: {info.text}", cooldown=0)
            return False
        self.logged_in = True
        self.last_alert = ""
        await self._save_session(force=True)
        return True

    async def restore_saved_session(self) -> bool:
        """Restore an already-authenticated saved session from the user's store on boot without attempting a new password login."""
        self.auth.bind(asyncio.get_running_loop(), self._say)
        self.cursors = await self.store.get("cursors", {}) or {}
        self.delivered = dict.fromkeys(await self.store.get("delivered", []) or [])
        session = await self.auth.load_session()
        has_saved_auth = bool((session or {}).get("authorization_data") or (session or {}).get("cookies", {}).get("sessionid"))
        if not has_saved_auth:
            self.logged_in = False
            return False
        log.info("Restoring saved session for @%s", self.cfg.ig_username)
        try:
            def _validate():
                cl = self.auth._build_client(session)
                cl.account_info()
                self.auth.login_method = "session"
                return True
            await self._call(_validate)
            self.logged_in = True
            self.last_alert = ""
            self.wake()
            log.info("Saved session restored for @%s", self.cfg.ig_username)
            return True
        except Exception as e:
            self.logged_in = False
            log.warning("Saved session for @%s is no longer valid (%s). Please log in via the web dashboard.", self.cfg.ig_username, type(e).__name__)
            return False

    async def reconfigure(self, new_cfg) -> None:
        self.cfg = new_cfg
        self.auth.cfg = new_cfg
        self.auth.prompter.timeout = new_cfg.code_timeout
        if new_cfg.ig_proxy != getattr(self.cl, "proxy", None):
            try:
                self.cl.set_proxy(new_cfg.ig_proxy)
                if not new_cfg.ig_proxy:
                    self.cl.proxy = None
            except Exception:
                pass
        await self.realtime.reconfigure(new_cfg)
        self.wake()

    async def import_session_json(self, raw_json: str) -> bool:
        """Import a full instagrapi settings JSON dictionary and validate it."""
        import json as _json
        data = _json.loads(raw_json)
        if not isinstance(data, dict) or "uuids" not in data:
            raise ValueError("Invalid instagrapi session JSON (must be a JSON object containing 'uuids').")
        self.auth.bind(asyncio.get_running_loop(), self._say)
        def _apply():
            cl = self.auth._build_client(data)
            cl.account_info()
            self.auth.login_method = "session"
        await self._call(_apply)
        self.logged_in = True
        self.last_alert = ""
        await self._save_session(force=True)
        self.wake()
        log.info("Imported and verified session JSON for @%s", self.cfg.ig_username)
        return True

    async def logout(self) -> None:
        """Log out on Instagram's side and forget the saved session (/logout)."""
        self.logged_in = False
        try:
            await self._call(self.auth.logout_sync)
        finally:
            await self.auth.forget_session()

    async def reset_session(self) -> bool:
        """Throw away the saved session AND device identity, then log in as a new device."""
        self.logged_in = False
        await self.auth.forget_session()
        return await self.login()

    # ------------------------------------------------------------- inbox -> messages
    @staticmethod
    def _meta_of(thread) -> tuple[str, bool, dict]:
        is_group = bool(thread.is_group)
        if is_group:
            name = thread.thread_title or "Group chat"
        else:
            first = thread.users[0].username if thread.users else None
            name = f"@{first}" if first else (thread.thread_title or f"chat {str(thread.id)[-6:]}")
        return name, is_group, {str(u.pk): u.username for u in (thread.users or [])}

    def _incoming(self, m, tid: str, meta: tuple[str, bool, dict]) -> IncomingMessage:
        name, is_group, users = meta
        kind, text, media_url = render(m)
        sender_id = str(m.user_id or "")
        return IncomingMessage(
            id=str(m.id),
            thread_id=tid,
            thread_name=name,
            is_group=is_group,
            sender_id=sender_id,
            sender_username=users.get(sender_id, f"user_{sender_id}"),
            timestamp=_aware(m.timestamp),
            item_type=m.item_type or "",
            kind=kind,
            text=text,
            media_url=media_url,
            raw=m,
        )

    def _is_mine(self, m) -> bool:
        return bool(m.is_sent_by_viewer or (m.user_id and str(m.user_id) == str(self.cl.user_id)))

    def _poll_sync(self):
        """Fetch recent threads. Returns (new_messages_oldest_first, newest_ts_per_thread, baseline)."""
        cl = self.cl
        threads = cl.direct_threads(amount=THREAD_LIMIT, thread_message_limit=MESSAGES_PER_THREAD)
        if self.cfg.include_requests:
            try:
                threads += cl.direct_requests(amount=10)
            except Exception as e:  # requests folder is optional, never fatal
                log.debug("Could not read message requests: %s", e)

        baseline = INIT_KEY not in self.cursors
        out: list[IncomingMessage] = []
        newest: dict[str, str] = {}
        seen_threads: set[str] = set()

        for thread in threads:
            tid = str(thread.id)
            if tid in seen_threads:
                continue
            seen_threads.add(tid)
            self.meta[tid] = self._meta_of(thread)
            msgs = sorted(thread.messages or [], key=lambda x: _aware(x.timestamp))
            if not msgs:
                continue
            newest[tid] = _aware(msgs[-1].timestamp).isoformat()
            if baseline:
                self.delivered.update(dict.fromkeys(str(m.id) for m in msgs))  # history is not replayed
                continue  # first run: remember where we are
            out.extend(self._incoming(m, tid, self.meta[tid]) for m in msgs if not self._is_mine(m))
        return out, newest, baseline

    def _cursor(self, tid: str) -> datetime | None:
        raw = self.cursors.get(tid) or self.cursors.get(INIT_KEY)
        return datetime.fromisoformat(raw) if raw else None

    async def _ingest(self, tid: str, msgs: list[IncomingMessage], newest: str | None = None) -> bool:
        """Deliver the messages of one thread that are newer than its cursor, oldest first.

        Every source (poll, realtime, targeted sync) goes through here under one lock, which is what
        guarantees a message is delivered once. The cursor only moves past messages that were delivered,
        so a Telegram outage never loses anything. Returns False if a delivery failed."""
        async with self._deliver_lock:
            cursor = self._cursor(tid)
            changed = False
            for m in sorted(msgs, key=lambda x: x.timestamp):
                # Instagram times have 1-second resolution, so two messages can share a timestamp: the
                # cursor only rules out OLDER messages, the delivered-id set rules out repeats.
                if m.id in self.delivered or (cursor and m.timestamp < cursor):
                    continue
                try:
                    await self._on_message(m)
                except Exception:
                    log.exception("Delivery failed for message %s, will retry on the next sync", m.id)
                    if changed:
                        await self._save_cursors()
                    return False
                self.cursors[tid] = m.timestamp.isoformat()
                self.delivered[m.id] = None
                cursor, changed = m.timestamp, True
                await self._after_delivery(m)
            if newest and (cursor is None or datetime.fromisoformat(newest) > cursor):
                self.cursors[tid] = newest  # skip over our own newer messages
                changed = True
            if changed:
                await self._save_cursors()
            return True

    async def _save_cursors(self) -> None:
        while len(self.delivered) > 5000:
            del self.delivered[next(iter(self.delivered))]
        await self.store.set("cursors", self.cursors)
        await self.store.set("delivered", list(self.delivered))

    async def _after_delivery(self, m: IncomingMessage) -> None:
        if not self.cfg.mark_seen:
            return
        try:
            if not await self.realtime.mark_seen(m.thread_id, m.id):
                await self._call(self.cl.direct_message_seen, int(m.thread_id), int(m.id))
        except Exception:
            log.debug("Could not mark %s as seen", m.id, exc_info=True)

    async def _poll_once(self, on_message=None) -> None:
        if on_message is not None:
            self._on_message = on_message
        messages, newest, baseline = await self._call(self._poll_sync)
        self.last_poll = datetime.now(timezone.utc)

        if baseline:
            async with self._deliver_lock:
                self.cursors.update(newest)
                self.cursors[INIT_KEY] = datetime.now(timezone.utc).isoformat()
                await self._save_cursors()
            log.info("First run: baseline set for %d threads, only new messages will be bridged", len(newest))
            await self._save_session()
            return

        by_thread = defaultdict(list)
        for m in messages:
            by_thread[m.thread_id].append(m)
        for tid, ts in newest.items():
            await self._ingest(tid, by_thread.get(tid, []), ts)
        await self._save_session()  # the session rotates tokens as we go; keep the saved copy fresh

    # ---------------------------------------------------------------- realtime input
    async def sync_thread(self, tid: str) -> None:
        """Fetch one thread and deliver whatever is new in it (used when a realtime item is not enough)."""
        thread = await self._call(self.cl.direct_thread, int(tid), MESSAGES_PER_THREAD)
        self.meta[tid] = self._meta_of(thread)
        msgs = sorted(thread.messages or [], key=lambda x: _aware(x.timestamp))
        if not msgs:
            return
        mine = [m for m in msgs if not self._is_mine(m)]
        await self._ingest(tid, [self._incoming(m, tid, self.meta[tid]) for m in mine], _aware(msgs[-1].timestamp).isoformat())

    async def handle_realtime_item(self, tid: str, item: dict) -> None:
        """A message pushed over MQTT. Plain text is delivered straight from the push; anything richer
        (media, shares, ...) is re-read from the thread so we have complete, signed media URLs."""
        if not self.baseline_done or self._on_message is None:
            return  # the first poll has not set the baseline yet
        if str(item.get("user_id") or "") == str(self.cl.user_id):
            return  # our own message (sent from Telegram or from the phone)
        meta = self.meta.get(tid)
        if meta and item.get("item_type") in ("text", "like"):
            try:
                dm = extract_direct_message(deepcopy(item))
                await self._ingest(tid, [self._incoming(dm, tid, meta)])
                return
            except Exception:
                log.debug("Could not parse realtime item, reading the thread instead", exc_info=True)
        await self.sync_thread(tid)

    # ------------------------------------------------------------------------ run
    async def run(self, on_message) -> None:
        """Keep the bridge fed forever. on_message(IncomingMessage) must raise if delivery failed.

        Realtime MQTT / FBNS deliver instantly; this loop is the safety net that also does the very
        first baseline. Error policy (instagrapi Best Practices): back off on throttling, never
        retry-loop a login, stop and tell you when a human is needed."""
        self._on_message = on_message
        self.realtime.start()
        backoff = 0
        try:
            while not self._stop.is_set():
                if not self.logged_in:
                    await self._sleep(5)
                    continue
                try:
                    await self._poll_once()
                    backoff = 0
                except Exception as e:
                    info = describe_error(e, self.cl)
                    if info.kind in ("throttle", "feedback"):
                        await self._alert(info.kind, f"⚠️ {info.text} Pausing {THROTTLE_PAUSE // 60} minutes.")
                        await self._sleep(THROTTLE_PAUSE)
                    elif info.needs_you:
                        # session expired (the auth layer already tried one re-login), challenge, 2FA, ...
                        self.logged_in = False
                        text = info.text + (" Send /relogin." if info.kind == "session" else "")
                        await self._alert(info.kind, f"⚠️ {text}")
                    else:
                        backoff = min(max(backoff * 2, 30), 600)
                        log.warning("Poll failed (%s: %s), retrying in %ss", type(e).__name__, str(e)[:200], backoff)
                        await self._sleep(backoff)
                        continue
                live = self.realtime.rt_connected or self.realtime.fbns_connected
                interval = self.cfg.safety_poll_interval if live else self.cfg.poll_interval
                await self._sleep_or_wake(interval * random.uniform(0.8, 1.3))
        finally:
            await self.realtime.stop()

    # ------------------------------------------------------------------ sending
    def _need_login(self) -> None:
        if not self.logged_in:
            raise RuntimeError("Instagram is not logged in. Send /relogin.")

    async def send_text(self, thread_id: str, text: str) -> None:
        self._need_login()
        if await self.realtime.send_text(thread_id, text):
            return
        await self._call(self.cl.direct_answer, int(thread_id), text)

    async def send_media(self, thread_id: str, kind: str, path: Path) -> None:
        """kind: photo (JPEG), video (MP4 H.264/AAC) or voice (M4A AAC). Convert first (utils/media.py)."""
        self._need_login()
        send = {"photo": self.cl.direct_send_photo, "video": self.cl.direct_send_video, "voice": self.cl.direct_send_voice}[kind]
        await self._call(lambda: send(path, thread_ids=[int(thread_id)]))

    async def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        self.auth.prompter.cancel()
        try:
            await self.realtime.stop()
        except Exception:
            log.debug("realtime stop failed", exc_info=True)
        if self.logged_in:
            try:
                await self._save_session(force=True)
            except Exception:
                log.exception("Could not save session on shutdown")
