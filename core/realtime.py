"""Live delivery with instagrapi's own realtime stack.

Two channels, both provided by instagrapi (``cl.realtime_*`` and ``cl.fbns_*``):

* Realtime MQTT (edge-mqtt.facebook.com): the channel the Instagram app uses for live DMs.
  After ``direct_subscribe()`` every new message, typing indicator and send-acknowledgement is pushed
  to us within about a second. Text replies from Telegram can also go out over it.
* FBNS (mqtt-mini.facebook.com): Instagram's push-notification channel. A push tells us "something
  happened in your DMs", which wakes the inbox sync immediately. It keeps working even when the
  realtime socket is down.

Both are blocking sockets, so each one is read by a small thread (MqttPump) that hands events to the
asyncio loop. Polling is NOT removed: it stays as a safety net (every SAFETY_POLL_INTERVAL while MQTT is
up, every POLL_INTERVAL when it is not), and a message can never be delivered twice because every path
goes through InstagramClient's per-thread cursor.
"""
import asyncio
import json
import random
import select
import threading
import time
from typing import Any, Callable

from core.auth import describe_error
from utils.logger import get_logger

log = get_logger("realtime")

REALTIME_PING = 15      # the MQTT keep-alive instagrapi announces is 20s
FBNS_PING = 45          # FBNS announces 60s
MIN_BACKOFF = 5
MAX_BACKOFF = 300
STABLE_AFTER = 60       # a connection that lived this long resets the backoff
SEND_ACK_TIMEOUT = 12


class MqttPump(threading.Thread):
    """Reads one instagrapi MQTT client (RealtimeClient or FbnsClient) until it dies.

    Waits for the socket to be readable (so a read timeout can never cut a packet in half), hands the
    packet to client.read_once() (which fires the registered handlers), and pings on schedule.
    """

    def __init__(self, name: str, client, ping_every: float, on_dead: Callable[[Exception | None], None]):
        super().__init__(name=f"{name}-pump", daemon=True)
        self.client = client
        self.ping_every = ping_every
        self.on_dead = on_dead
        self._halt = threading.Event()

    def halt(self) -> None:
        self._halt.set()

    def run(self) -> None:
        error: Exception | None = None
        last_ping = time.monotonic()
        try:
            while not self._halt.is_set():
                sock = getattr(self.client.transport, "sock", None)
                if sock is None:
                    raise ConnectionError("socket closed")
                pending = getattr(sock, "pending", lambda: 0)()
                if pending or select.select([sock], [], [], 1.0)[0]:
                    self.client.read_once()
                if time.monotonic() - last_ping >= self.ping_every:
                    if not self.client.ping():
                        raise ConnectionError("no ping response")
                    last_ping = time.monotonic()
        except Exception as e:  # noqa: BLE001 - any failure just means "reconnect"
            if not self._halt.is_set():
                error = e
        finally:
            self.on_dead(error)


def _locked_send(transport) -> None:
    """The pump (acks, pings) and the event loop (our own publishes) both write to one TLS socket."""
    lock = threading.Lock()
    original = transport.send

    def send(packet: bytes) -> None:
        with lock:
            original(packet)

    transport.send = send


class RealtimeService:
    def __init__(self, ig):
        self.ig = ig
        self.cfg = ig.cfg
        self.rt_connected = False
        self.fbns_connected = False
        self.rt_error = ""
        self.fbns_error = ""
        self.events = 0
        self.pushes = 0
        self.last_event: float | None = None
        self.on_typing: Callable[[str], Any] | None = None   # async callable(thread_id), set by main

        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self._queue: asyncio.Queue = asyncio.Queue()
        self._acks: dict[str, asyncio.Future] = {}
        self._rt = None          # the live RealtimeClient
        self._rt_client = None   # the instagrapi Client it belongs to

    # --------------------------------------------------------------- lifecycle
    def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop.clear()
        self._tasks = [t for t in self._tasks if not t.done()]
        if self._tasks:
            return
        if self.cfg.realtime:
            self._tasks.append(asyncio.create_task(self._worker()))
            self._tasks.append(asyncio.create_task(self._supervise("realtime", self._connect_realtime, REALTIME_PING)))
        if self.cfg.fbns:
            self._tasks.append(asyncio.create_task(self._supervise("fbns", self._connect_fbns, FBNS_PING)))

    async def stop(self) -> None:
        self._stop.set()
        for t in self._tasks:
            t.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        self.rt_connected = False
        self.fbns_connected = False
        await asyncio.to_thread(self._disconnect_all)

    async def reconfigure(self, new_cfg) -> None:
        old_rt, old_fbns = self.cfg.realtime, self.cfg.fbns
        self.cfg = new_cfg
        if old_rt != new_cfg.realtime or old_fbns != new_cfg.fbns:
            log.info(
                "Reconfiguring live channels (Realtime MQTT=%s, FBNS=%s)",
                "on" if new_cfg.realtime else "off",
                "on" if new_cfg.fbns else "off",
            )
            await self.stop()
            self.start()

    def status(self) -> str:
        def one(label, on, err, enabled):
            if not enabled:
                return f"{label}: off"
            return f"{label}: ✅ connected" if on else f"{label}: ❌ down" + (f" ({err})" if err else "")

        age = f", last event {int(time.time() - self.last_event)}s ago" if self.last_event else ""
        return (
            one("Realtime MQTT", self.rt_connected, self.rt_error, self.cfg.realtime)
            + f" · events {self.events}{age}\n"
            + one("FBNS push", self.fbns_connected, self.fbns_error, self.cfg.fbns)
            + f" · pushes {self.pushes}"
        )

    # -------------------------------------------------------------- connecting
    def _from_thread(self, fn: Callable, payload: Any) -> None:
        """Called on a pump thread: hop onto the event loop."""
        try:
            self._loop.call_soon_threadsafe(fn, payload)
        except RuntimeError:  # loop already closed during shutdown
            pass

    def _connect_realtime(self):
        cl = self.ig.cl
        cl.realtime = None
        cl.realtime_on("message", lambda p: self._from_thread(self._on_message, p))
        cl.realtime_on("typing", lambda p: self._from_thread(self._on_typing, p))
        cl.realtime_on("send_response", lambda p: self._from_thread(self._on_send_response, p))
        rt = cl.realtime_connect()
        _locked_send(rt.transport)
        rt.direct_subscribe(amount=1)  # tells Instagram where our inbox snapshot is (iris subscribe)
        self._rt, self._rt_client = rt, cl
        return rt

    def _connect_fbns(self):
        cl = self.ig.cl
        cl.fbns = None
        cl.fbns_on("push", lambda p: self._from_thread(self._on_push, p))
        fb = cl.fbns_connect()  # connects, registers the push token, stores fbns_auth in the settings
        _locked_send(fb.transport)
        return fb

    def _disconnect_all(self) -> None:
        for name in ("realtime_disconnect", "fbns_disconnect"):
            try:
                getattr(self.ig.cl, name)()
            except Exception:
                log.debug("%s failed", name, exc_info=True)

    async def _supervise(self, name: str, connect: Callable, ping_every: float) -> None:
        """Keep one channel connected: reconnect with backoff, restart when the Instagram client is rebuilt."""
        backoff = MIN_BACKOFF
        flag, err = ("rt_connected", "rt_error") if name == "realtime" else ("fbns_connected", "fbns_error")
        while not self._stop.is_set():
            if not self.ig.logged_in or not self.ig.baseline_done:
                await self._nap(5)
                continue
            cl = self.ig.cl
            dead = asyncio.Event()
            result: dict[str, Exception | None] = {"error": None}

            def on_dead(e, dead=dead, result=result):
                result["error"] = e
                try:
                    self._loop.call_soon_threadsafe(dead.set)
                except RuntimeError:
                    pass

            try:
                client = await self.ig._call(connect)
            except Exception as e:  # noqa: BLE001
                info = describe_error(e, cl)
                setattr(self, err, f"{type(e).__name__}: {str(e)[:100]}")
                log.warning("%s connect failed (%s: %s), retry in ~%ss", name, type(e).__name__, str(e)[:150], backoff)
                wait = 900 if info.kind in ("throttle", "feedback") else backoff
                backoff = min(backoff * 2, MAX_BACKOFF)
                await self._nap(wait * random.uniform(0.8, 1.3))
                continue

            pump = MqttPump(name, client, ping_every, on_dead)
            pump.start()
            setattr(self, flag, True)
            setattr(self, err, "")
            began = time.monotonic()
            log.info("%s connected", name)
            self.ig.wake()  # backfill anything that arrived while we were not connected
            if name == "fbns":
                try:
                    await self.ig._save_session()  # stores the freshly issued FBNS device auth
                except Exception:
                    log.debug("could not save session after FBNS connect", exc_info=True)

            while not dead.is_set() and not self._stop.is_set() and self.ig.logged_in and self.ig.cl is cl:
                try:
                    await asyncio.wait_for(dead.wait(), 1)
                except asyncio.TimeoutError:
                    pass

            pump.halt()
            setattr(self, flag, False)
            if result["error"] is not None:
                setattr(self, err, f"{type(result['error']).__name__}: {str(result['error'])[:100]}")
                log.warning("%s dropped (%s: %s)", name, type(result["error"]).__name__, str(result["error"])[:150])
            try:
                await asyncio.to_thread(self._disconnect_one, cl, name)
            except Exception:
                log.debug("disconnect failed", exc_info=True)
            self.ig.wake()
            if time.monotonic() - began > STABLE_AFTER:
                backoff = MIN_BACKOFF
            if not self._stop.is_set():
                await self._nap(backoff * random.uniform(0.8, 1.3))
                backoff = min(backoff * 2, MAX_BACKOFF)

    @staticmethod
    def _disconnect_one(cl, name: str) -> None:
        (cl.realtime_disconnect if name == "realtime" else cl.fbns_disconnect)()

    async def _nap(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    # ------------------------------------------------------------------ events
    def _touch(self) -> None:
        self.events += 1
        self.last_event = time.time()

    def _on_message(self, payload: Any) -> None:
        """RealtimeClient "message" event: a direct item was added/changed in a thread."""
        self._touch()
        msg = payload.get("message") if isinstance(payload, dict) else None
        if not isinstance(msg, dict) or msg.get("op") != "add":
            return
        parts = str(msg.get("path") or "").split("/")  # /direct_v2/threads/<tid>/items/<item_id>
        if len(parts) != 6 or parts[1:3] != ["direct_v2", "threads"] or parts[4] != "items":
            return
        item = {k: v for k, v in msg.items() if k not in ("path", "op", "thread_id")}
        item.setdefault("item_id", parts[5])
        self._queue.put_nowait((parts[3], item))

    def _on_typing(self, event: Any) -> None:
        self._touch()
        if not isinstance(event, dict) or not self.on_typing:
            return
        value = event.get("value")
        value = value if isinstance(value, dict) else {}
        thread_id = event.get("thread_id")
        sender = str(value.get("sender_id") or "")
        if not thread_id or str(value.get("activity_status")) not in ("1", "True", "true"):
            return
        if sender and sender == str(self.ig.cl.user_id):
            return
        asyncio.ensure_future(self._typing(str(thread_id)))

    async def _typing(self, thread_id: str) -> None:
        try:
            await self.on_typing(thread_id)
        except Exception:
            log.debug("typing relay failed", exc_info=True)

    def _on_push(self, push: Any) -> None:
        """FBNS push. Never trusted as content (it is truncated); it just means "go look"."""
        self.pushes += 1
        text = json.dumps(push, default=str).lower() if push is not None else ""
        if "direct" in text:
            log.debug("FBNS push about direct messages, syncing the inbox")
            self.ig.wake()

    def _on_send_response(self, payload: Any) -> None:
        self._touch()
        text = json.dumps(payload, default=str)
        ok = True
        if isinstance(payload, dict):
            status = str(payload.get("status") or "").lower()
            code = str(payload.get("status_code") or "")
            ok = status == "ok" or code == "200"
        for ctx, fut in list(self._acks.items()):
            if ctx in text and not fut.done():
                fut.set_result(ok)

    async def _worker(self) -> None:
        """Handle realtime items strictly in arrival order (the cursor logic depends on it)."""
        while not self._stop.is_set():
            tid, item = await self._queue.get()
            try:
                await self.ig.handle_realtime_item(tid, item)
            except Exception:
                log.exception("Could not handle realtime item, falling back to an inbox sync")
                self.ig.wake()

    # ----------------------------------------------------------------- sending
    @property
    def can_send(self) -> bool:
        return bool(self.cfg.realtime_send and self.rt_connected and self._rt is not None and self._rt_client is self.ig.cl)

    async def send_text(self, thread_id: str, text: str) -> bool:
        """Send over MQTT and wait for Instagram's acknowledgement. False = channel not usable (use HTTP)."""
        if not self.can_send:
            return False
        rt = self._rt
        ctx = rt.new_client_context()
        fut = self._loop.create_future()
        self._acks[ctx] = fut
        try:
            try:
                await asyncio.to_thread(rt.direct_send_text, thread_id, text, ctx)
            except Exception:
                log.debug("MQTT publish failed, using HTTP", exc_info=True)
                return False
            try:
                ok = await asyncio.wait_for(fut, SEND_ACK_TIMEOUT)
            except asyncio.TimeoutError:
                raise RuntimeError(
                    "Instagram did not confirm the message in time. Check the chat before sending it again."
                ) from None
            if not ok:
                raise RuntimeError("Instagram refused the message.")
            return True
        finally:
            self._acks.pop(ctx, None)

    async def mark_seen(self, thread_id: str, item_id: str) -> bool:
        if not self.can_send:
            return False
        try:
            await asyncio.to_thread(self._rt.direct_mark_seen, thread_id, item_id)
            return True
        except Exception:
            log.debug("MQTT mark_seen failed", exc_info=True)
            return False
