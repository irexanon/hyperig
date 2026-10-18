"""Watch public Instagram accounts and get new posts, reels and stories in Telegram.

/watch <user> [posts|stories|all]   start watching (public accounts only)
/unwatch <user>                     stop
/watchlist                          list, with buttons to remove an account or check now

How it behaves (instagrapi Best Practices: poll a small set, several minutes apart, remember what was seen):
* A short, fixed list (MAX_WATCH accounts), checked about every CHECK_EVERY seconds with random jitter,
  with a pause between accounts. Nothing is checked in a tight loop.
* On /watch the current posts/stories are recorded as already seen, so you only get what appears AFTER that.
* Seen ids are saved, so a restart neither repeats old items nor loses new ones. An item is marked seen only
  after Telegram accepted it.
* Throttling or "feedback required" from Instagram pauses all checks for THROTTLE_PAUSE and tells you once.
* If a watched account turns private or disappears it is removed from the list and you are told.
* Alerts go to you (the admin) in the private chat. Public accounts only, no contact details.
"""
import asyncio
import random
import time
from datetime import datetime, timezone

import requests
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError

from core.auth import describe_error
from modules.base import BaseModule, Command
from modules.dump import parse_target
from utils import media as media_utils
from utils.logger import get_logger
from utils.tgsend import caption_of, esc, items_of, send_items

log = get_logger("watcher")

CHECK_EVERY = 600              # seconds between full checks (jittered 0.8x-1.3x)
ACCOUNT_GAP = (6.0, 14.0)      # random pause between two accounts inside one check
MAX_WATCH = 10                 # accounts per Instagram login
MAX_NEW_PER_CHECK = 5          # items sent per account per check; the rest are marked seen and summarised
THROTTLE_PAUSE = 900           # seconds to stop everything after a rate limit
CHECK_NOW_GAP = 120            # seconds between two manual "check now" presses
SEEN_CAP = 300                 # remembered ids per list
STORE_KEY = "watchlist"
PREFIX = "watch"


def _label(entry: dict) -> str:
    parts = [name for name, key in (("posts", "posts"), ("stories", "stories")) if entry.get(key)]
    return " + ".join(parts) or "nothing"


def _remember(seen: list[str], new: list[str]) -> list[str]:
    return (seen + [x for x in new if x not in seen])[-SEEN_CAP:]


class WatcherModule(BaseModule):
    name = "watcher"
    description = "Get new posts, reels and stories of public accounts"

    def __init__(self, ctx):
        super().__init__(ctx)
        self._watch: dict[str, dict] = {}
        self._lock = asyncio.Lock()          # guards the watchlist + its saved copy
        self._check_lock = asyncio.Lock()    # one check pass at a time
        self._task: asyncio.Task | None = None
        self._kick = asyncio.Event()         # "check now"
        self._last_kick = float("-inf")
        self._last_check: datetime | None = None
        self._alerted: set[str] = set()      # one-time alerts already sent (throttle, ...)

    # ------------------------------------------------------------------ registration
    def get_commands(self):
        return {
            "watch": Command(self.cmd_watch, "Watch a public account for new posts and stories", "/watch <username> [posts|stories|all]", takes_update=True),
            "unwatch": Command(self.cmd_unwatch, "Stop watching an account", "/unwatch <username>", takes_update=True),
            "watchlist": Command(self.cmd_watchlist, "Accounts you are watching", "/watchlist", takes_update=True),
        }

    def get_callbacks(self):
        return {PREFIX: self.on_button}

    async def on_start(self) -> None:
        self._watch = await self.ctx.store.get(STORE_KEY, {}) or {}
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())
        log.info("Watcher started with %d account(s)", len(self._watch))

    async def on_stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    # ------------------------------------------------------------------ helpers
    async def _save(self) -> None:
        await self.ctx.store.set(STORE_KEY, self._watch)

    def _need_login(self) -> None:
        if not self.ctx.ig.logged_in:
            raise RuntimeError("Instagram is not logged in. Send /relogin.")

    async def _ig(self, fn):
        self._need_login()
        return await self.ctx.ig._call(fn)

    async def _fetch(self, url: str) -> bytes | None:
        try:
            return await self.ctx.bridge._fetch(url)
        except (media_utils.MediaError, requests.RequestException) as e:
            log.info("Could not download media (%s)", e)
            return None

    def _target_chat(self) -> int | None:
        return self.ctx.cfg.tg_admin_id or self.ctx.bridge.chat_id or None

    async def _notify(self, text: str) -> bool:
        bot, chat = self.ctx.bridge.bot, self._target_chat()
        if not bot or not chat:
            return False
        try:
            await bot.send_message(chat, text, parse_mode="HTML", disable_web_page_preview=True)
            return True
        except TelegramError as e:
            log.warning("Could not send a watcher message: %s", e)
            return False

    async def _alert_once(self, key: str, text: str) -> None:
        if key not in self._alerted:
            self._alerted.add(key)
            await self._notify(text)

    # ------------------------------------------------------------------ the loop
    async def _wait(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._kick.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass
        self._kick.clear()

    async def _loop(self) -> None:
        await asyncio.sleep(random.uniform(20, 60))  # let login / realtime settle after a start
        while True:
            pause = CHECK_EVERY * random.uniform(0.8, 1.3)
            try:
                if self._watch and self.ctx.ig.logged_in:
                    if not await self._check_all():
                        pause = THROTTLE_PAUSE
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Watcher check failed")
            await self._wait(pause)

    async def _check_all(self) -> bool:
        """One pass over the watchlist. Returns False when Instagram told us to slow down.
        Only one pass runs at a time, so an item can never be delivered twice."""
        async with self._check_lock:
            return await self._check_pass()

    async def _check_pass(self) -> bool:
        for i, username in enumerate(list(self._watch)):
            if username not in self._watch:
                continue
            if i:
                await asyncio.sleep(random.uniform(*ACCOUNT_GAP))
            try:
                await self._check_one(username)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if not await self._handle_error(username, e):
                    return False
        self._last_check = datetime.now(timezone.utc)
        return True

    async def _handle_error(self, username: str, e: Exception) -> bool:
        """Deal with a failed check. Returns False if all checks should pause."""
        try:
            from instagrapi import exceptions as ig_exc

            gone = isinstance(e, (ig_exc.PrivateAccount, ig_exc.UserNotFound, ig_exc.ClientNotFoundError))
        except Exception:
            gone = False
        if gone:
            async with self._lock:
                self._watch.pop(username, None)
                await self._save()
            await self._notify(f"⚠️ @{esc(username)} is private or no longer exists, so I stopped watching it.")
            return True
        info = describe_error(e, self.ctx.ig.cl)
        if info.kind in ("throttle", "feedback"):
            await self._alert_once(
                f"throttle:{int(time.time() // THROTTLE_PAUSE)}",
                f"⚠️ Instagram is slowing us down. Watcher paused for {THROTTLE_PAUSE // 60} minutes.\n{esc(info.text)}",
            )
            return False
        if info.needs_you:
            log.warning("Watcher stopped this round: %s", info.text)
            return False
        log.warning("Watcher check for @%s failed (%s: %s)", username, type(e).__name__, str(e)[:200])
        return True

    async def _check_one(self, username: str) -> None:
        entry = self._watch.get(username)
        if not entry:
            return
        pk = entry["pk"]
        if entry.get("posts"):
            medias = await self._ig(lambda: self.ctx.ig.cl.user_medias(pk, 6))
            new = [m for m in medias if str(m.pk) not in entry["seen_posts"]]
            await self._deliver(username, entry, "seen_posts", sorted(new, key=lambda m: m.taken_at), "post")
        if entry.get("stories"):
            stories = await self._ig(lambda: self.ctx.ig.cl.user_stories(pk))
            new = [s for s in stories if str(s.pk) not in entry["seen_stories"]]
            await self._deliver(username, entry, "seen_stories", sorted(new, key=lambda m: m.taken_at), "story")

    async def _deliver(self, username: str, entry: dict, seen_key: str, new: list, kind: str) -> None:
        if not new:
            return
        bot, chat = self.ctx.bridge.bot, self._target_chat()
        if not bot or not chat:
            return  # Telegram is not ready yet: nothing is marked seen, so the next check retries
        to_send, skipped = new[:MAX_NEW_PER_CHECK], new[MAX_NEW_PER_CHECK:]
        done: list[str] = []
        for m in to_send:
            label = "reel" if getattr(m, "product_type", "") == "clips" else kind
            header = f"🔔 <b>@{esc(username)}</b> · new {label}\n"
            try:
                sent = await send_items(bot, chat, items_of(m), header + caption_of(m, label if kind == "post" else kind), self._fetch)
            except TelegramError as e:
                log.warning("Could not deliver a watcher alert: %s", e)
                break
            if sent == 0:
                await self._notify(f"{header}Could not download the media. 🔗 https://www.instagram.com/{esc(username)}/")
            done.append(str(m.pk))
            await asyncio.sleep(random.uniform(0.8, 1.6))
        else:
            if skipped:
                done += [str(m.pk) for m in skipped]
                await self._notify(f"🔔 <b>@{esc(username)}</b> · {len(skipped)} more new {kind}(s) not shown. Use /{'posts' if kind == 'post' else 'stories'} {esc(username)}.")
        if done:
            async with self._lock:
                if username in self._watch:
                    entry[seen_key] = _remember(entry[seen_key], done)
                    await self._save()

    # ------------------------------------------------------------------ commands
    async def cmd_watch(self, args, update):
        message = update.effective_message
        username = parse_target(args[0]) if args else None
        if not username:
            await message.reply_text("Usage: /watch <username> [posts|stories|all]\n(a username, @username or instagram.com link)")
            return
        mode = (args[1].lower() if len(args) > 1 else "all")
        if mode not in ("posts", "stories", "all"):
            await message.reply_text("Mode must be posts, stories or all.")
            return
        async with self._lock:
            if username in self._watch:
                await message.reply_text(f"👁 Already watching @{username}. Use /unwatch {username} first to change it.")
                return
            if len(self._watch) >= MAX_WATCH:
                await message.reply_text(f"⚠️ The limit is {MAX_WATCH} watched accounts (keeps the Instagram login safe). Use /unwatch to free a slot.")
                return
        try:
            user = await self._ig(lambda: self.ctx.ig.cl.user_info_by_username(username))
            if user.is_private:
                await message.reply_text("🔒 That account is private. I only watch public accounts.")
                return
            entry = {
                "pk": str(user.pk),
                "posts": mode in ("posts", "all"),
                "stories": mode in ("stories", "all"),
                "seen_posts": [],
                "seen_stories": [],
                "added": datetime.now(timezone.utc).isoformat(),
            }
            # Record what exists now as already seen: you only get what appears from here on.
            if entry["posts"]:
                current = await self._ig(lambda: self.ctx.ig.cl.user_medias(entry["pk"], 12))
                entry["seen_posts"] = _remember([], [str(m.pk) for m in current])
            if entry["stories"]:
                current = await self._ig(lambda: self.ctx.ig.cl.user_stories(entry["pk"]))
                entry["seen_stories"] = _remember([], [str(s.pk) for s in current])
        except Exception as e:
            await message.reply_text(self._explain(e))
            return
        async with self._lock:
            if username in self._watch or len(self._watch) >= MAX_WATCH:  # another /watch got in first
                await message.reply_text("⚠️ Could not add it: already watched, or the list is full.")
                return
            self._watch[username] = entry
            await self._save()
        await message.reply_text(
            f"👁 Watching <b>@{esc(username)}</b> ({_label(entry)}).\n"
            f"Checked about every {CHECK_EVERY // 60} minutes. New items arrive here; what is already there is not sent.",
            parse_mode="HTML",
        )

    async def cmd_unwatch(self, args, update):
        message = update.effective_message
        username = parse_target(args[0]) if args else None
        if not username:
            await message.reply_text("Usage: /unwatch <username>")
            return
        async with self._lock:
            removed = self._watch.pop(username, None)
            if removed:
                await self._save()
        await message.reply_text(f"🛑 Stopped watching @{username}." if removed else f"@{username} is not on your watchlist.")

    async def cmd_watchlist(self, args, update):
        text, markup = self._render()
        await update.effective_message.reply_text(text, parse_mode="HTML", reply_markup=markup)

    def _explain(self, e: Exception) -> str:
        if isinstance(e, RuntimeError):
            return f"❌ {e}"
        try:
            from instagrapi import exceptions as ig_exc

            if isinstance(e, (ig_exc.UserNotFound, ig_exc.ClientNotFoundError)):
                return "❌ That account was not found."
            return f"❌ {describe_error(e, self.ctx.ig.cl).text}"
        except Exception:
            return f"❌ {type(e).__name__}: {e}"

    # ------------------------------------------------------------------ list + buttons
    def _render(self) -> tuple[str, InlineKeyboardMarkup | None]:
        if not self._watch:
            return "👁 Your watchlist is empty. Add one with /watch <username>.", None
        last = self._last_check.strftime("%H:%M UTC") if self._last_check else "not yet"
        lines = [f"👁 <b>Watching {len(self._watch)}/{MAX_WATCH}</b> · every ~{CHECK_EVERY // 60} min · last check {last}", ""]
        lines += [f"• @{esc(u)} ({_label(e)})" for u, e in self._watch.items()]
        rows, row = [], []
        for u in self._watch:
            row.append(InlineKeyboardButton(f"🗑 {u}"[:30], callback_data=f"m:{PREFIX}:rm:{u}"))
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        rows.append(
            [
                InlineKeyboardButton("🔄 Check now", callback_data=f"m:{PREFIX}:now:-"),
                InlineKeyboardButton("✖ Close", callback_data=f"m:{PREFIX}:x:-"),
            ]
        )
        return "\n".join(lines), InlineKeyboardMarkup(rows)

    async def on_button(self, rest: str, query, context) -> None:
        act, _, arg = rest.partition(":")
        if act == "x":
            try:
                await query.message.delete()
            except TelegramError:
                pass
            return
        if act == "rm":
            async with self._lock:
                if self._watch.pop(arg, None) is not None:
                    await self._save()
        elif act == "now":
            wait = CHECK_NOW_GAP - (time.monotonic() - self._last_kick)
            if wait > 0:
                await query.message.reply_text(f"⏳ Checked a moment ago. Try again in {int(wait)}s.")
                return
            self._last_kick = time.monotonic()
            self._kick.set()
            await query.message.reply_text("🔄 Checking now. New items will arrive here.")
            return
        else:
            return
        text, markup = self._render()
        try:
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
        except TelegramError:
            pass
