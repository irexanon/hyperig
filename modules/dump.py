"""Public-profile tools for Telegram.

/dump <username or link> shows a profile card with a button menu (posts, reels, stories, highlights,
followers and following lists, JSON). Plain commands (/profile, /pfp, /posts, /reels, /stories, /highlights) do the
same things without the menu.

Rules this module keeps:
* PUBLIC accounts only. A private account gets its public card and nothing else.
* No contact details (phone, e-mail, street address, coordinates) are ever shown or exported.
* One job per chat at a time, and a short pause between items, so the logged-in Instagram account
  is not hammered (instagrapi Best Practices: moderate pace, no bursts).
"""
import asyncio
import io
import json
import random
import re

import requests
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.error import BadRequest, TelegramError

from core.auth import describe_error
from modules.base import BaseModule, Command
from utils import media as media_utils
from utils.logger import get_logger
from utils.progress import Progress
from utils.tgsend import caption_of, clip as _clip, esc as _esc, items_of, num as _num, send_items, send_one

log = get_logger("dump")

USERNAME_RE = re.compile(r"^[A-Za-z0-9._]{1,30}$")
URL_RE = re.compile(r"^(?:https?://)?(?:www\.)?instagram\.com/([A-Za-z0-9._]{1,30})/?(?:[?#].*)?$", re.I)
RESERVED = {"p", "reel", "reels", "stories", "explore", "tv", "accounts", "direct", "s"}

POSTS_DEFAULT, POSTS_MAX = 5, 50
REELS_DEFAULT, REELS_MAX = 3, 30
LIST_DEFAULT, LIST_MAX = 200, 2000   # followers / following list size
HIGHLIGHT_BUTTONS = 20
PREFIX = "dump"


def parse_target(raw: str) -> str | None:
    """'@name', 'name' or an instagram.com/name link -> 'name' (or None if it is not a username)."""
    raw = (raw or "").strip().lstrip("@")
    match = URL_RE.match(raw)
    if match:
        name = match.group(1)
        return None if name.lower() in RESERVED else name.lower()
    return raw.lower() if USERNAME_RE.match(raw) else None


def _btn(text: str, act: str, arg: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text, callback_data=f"m:{PREFIX}:{act}:{arg}")


class DumpModule(BaseModule):
    name = "dump"
    toggleable = True
    label = "Profile tools (/dump, /profile, /posts ...)"
    description = "Public profile card with action buttons, plus profile commands"

    def __init__(self, ctx):
        super().__init__(ctx)
        self._busy: set[int] = set()          # chats with a job running
        self._private: dict[str, bool] = {}    # username -> is_private, so "Back" needs no new request
        self._awaiting: dict[int, tuple[str, str]] = {}   # chat id -> (action, username) waiting for a typed number
        self._fmt: dict[int, str] = {}         # chat id -> "txt" | "json" for followers / following lists

    # ------------------------------------------------------------------ registration
    def get_commands(self):
        return {
            "dump": Command(self.cmd_dump, "Profile card with action buttons", "/dump <username or link>", takes_update=True),
            "profile": Command(self.cmd_profile, "Public profile info", "/profile <username or link>", takes_update=True),
            "pfp": Command(self.cmd_pfp, "Profile picture (full size)", "/pfp <username>", takes_update=True),
            "posts": Command(self.cmd_posts, f"Latest posts (1-{POSTS_MAX}, default {POSTS_DEFAULT})", "/posts <username> [count]", takes_update=True),
            "reels": Command(self.cmd_reels, f"Latest reels (1-{REELS_MAX}, default {REELS_DEFAULT})", "/reels <username> [count]", takes_update=True),
            "stories": Command(self.cmd_stories, "Current stories", "/stories <username>", takes_update=True),
            "highlights": Command(self.cmd_highlights, "Highlights list with buttons", "/highlights <username>", takes_update=True),
            "followers": Command(self.cmd_followers, f"Followers list as a file (up to {LIST_MAX})", "/followers <username> [count] [json]", takes_update=True),
            "following": Command(self.cmd_following, f"Following list as a file (up to {LIST_MAX})", "/following <username> [count] [json]", takes_update=True),
        }

    def get_callbacks(self):
        return {PREFIX: self.on_button}

    # ------------------------------------------------------------------ instagram helpers
    def _need_login(self) -> None:
        if not self.ctx.ig.logged_in:
            raise RuntimeError("Instagram is not connected. Use /connect.")

    async def _ig(self, fn):
        """Run a blocking instagrapi call under the client's lock. fn is a no-argument callable."""
        self._need_login()
        return await self.ctx.ig._call(fn)

    async def _profile(self, username: str):
        user = await self._ig(lambda: self.ctx.ig.cl.user_info_by_username(username))
        self._private[username] = bool(user.is_private)
        return user

    async def _public_profile(self, username: str):
        user = await self._profile(username)
        if user.is_private:
            raise PermissionError("That account is private. I only work with public accounts.")
        return user

    def _explain(self, e: Exception) -> str:
        if isinstance(e, PermissionError):
            return str(e)
        if isinstance(e, RuntimeError):
            return f"{e}"
        try:
            from instagrapi import exceptions as ig_exc

            if isinstance(e, (ig_exc.UserNotFound, ig_exc.ClientNotFoundError)):
                return "That account was not found."
            if isinstance(e, ig_exc.PrivateAccount):
                return "That account is private. I only work with public accounts."
            info = describe_error(e, self.ctx.ig.cl)
            return f"{info.text}"
        except Exception:
            return f"{type(e).__name__}: {e}"

    # ------------------------------------------------------------------ text builders
    @staticmethod
    def _card(u, detailed: bool = False) -> str:
        badges = ("✅" if u.is_verified else "") + ("🔒" if u.is_private else "")
        lines = [f"<b>{_esc(u.full_name or u.username)}</b> {badges}".strip(), f"@{_esc(u.username)}"]
        lines.append(
            f"📸 {_num(u.media_count)} posts · 👥 {_num(u.follower_count)} followers · ➡️ {_num(u.following_count)} following"
        )
        if u.biography:
            lines += ["", _esc(_clip(u.biography, 300))]
        if u.external_url:
            lines.append(f"🔗 {_esc(u.external_url)}")
        category = u.category or u.category_name
        if category:
            lines.append(f"🏷 {_esc(category)}")
        if detailed:
            lines += ["", f"🆔 <code>{u.pk}</code>"]
        kind = "💼 Business" if u.is_business else "👤 Personal"
        lines.append(f"{'🔒 Private' if u.is_private else '🌐 Public'} account · {kind}" if detailed else
                     f"{'🔒 Private' if u.is_private else '🌐 Public'} account")
        if u.is_private:
            lines += ["", "🔒 Private account: only this public info is shown."]
        return "\n".join(lines)

    @staticmethod
    def _menu(username: str, private: bool) -> InlineKeyboardMarkup:
        rows = []
        if not private:
            rows.append([_btn("Posts", "pm", username), _btn("Reels", "rm", username)])
            rows.append([_btn("Stories", "st", username), _btn("Highlights", "hl", username)])
            rows.append([_btn("Followers / Following", "lm", username)])
        rows.append([_btn("JSON", "js", username)])
        rows.append([InlineKeyboardButton("Open on Instagram", url=f"https://www.instagram.com/{username}/")])
        return InlineKeyboardMarkup(rows)

    @staticmethod
    def _count_menu(kind: str, username: str) -> InlineKeyboardMarkup:
        counts = (5, 10, 20, POSTS_MAX) if kind == "p" else (3, 5, 10, REELS_MAX)
        row = [_btn(f"{n}", f"{kind}{n}", username) for n in counts]
        label = "posts" if kind == "p" else "reels"
        return InlineKeyboardMarkup([row, [_btn("Type a number", "cu", f"{kind}:{username}")],
                                     [_btn(f"Back (how many {label}?)", "bk", username)]])

    def _list_menu(self, chat_id: int, username: str) -> InlineKeyboardMarkup:
        fmt = self._fmt.get(chat_id, "txt")
        rows = []
        for kind, label in (("f", "Followers"), ("g", "Following")):
            rows.append([_btn(f"{label} {n}", f"l{kind}{n}", username) for n in (100, 500, 1000)])
            rows.append([_btn(f"{label}: type a number", "cu", f"l{kind}:{username}")])
        rows.append([_btn(f"File format: {fmt.upper()} (tap to switch)", "ft", username)])
        rows.append([_btn("Back", "bk", username)])
        return InlineKeyboardMarkup(rows)

    # ------------------------------------------------------------------ telegram helpers
    @staticmethod
    async def _edit(message, text: str, markup=None) -> None:
        try:
            await message.edit_text(text, parse_mode="HTML", reply_markup=markup)
        except TelegramError:
            pass

    async def _fetch(self, url: str) -> bytes | None:
        try:
            return await self.ctx.tg._fetch(url)
        except (media_utils.MediaError, requests.RequestException) as e:
            log.info("Could not download media (%s)", e)
            return None

    # ------------------------------------------------------------------ media helpers
    @staticmethod
    def _post_meta(m) -> dict:
        return {
            "code": m.code,
            "url": f"https://www.instagram.com/p/{m.code}/" if m.code else None,
            "type": m.product_type or {1: "photo", 2: "video", 8: "album"}.get(m.media_type, str(m.media_type)),
            "taken_at": m.taken_at.isoformat() if m.taken_at else None,
            "caption": m.caption_text,
            "likes": m.like_count,
            "comments": m.comment_count,
            "views": m.view_count or m.play_count,
        }

    # ------------------------------------------------------------------ the jobs
    TITLES = {"p": "Downloading posts", "r": "Downloading reels", "st": "Downloading stories", "h": "Downloading highlight",
              "pp": "Fetching profile picture", "js": "Exporting profile", "lf": "Collecting followers", "lg": "Collecting following"}

    async def _run(self, bot, chat_id: int, act: str, username: str, n: int = 0, hl_pk: str = "") -> None:
        """Run one action. Never raises: problems are reported in the chat."""
        if act == "hl":  # just a list of buttons, no downloading
            await self._show_highlights(bot, chat_id, username)
            return
        if chat_id in self._busy:
            await bot.send_message(chat_id, "Another job is still running here. Wait for it to finish.")
            return
        self._busy.add(chat_id)
        status = await Progress.begin(bot, chat_id, self.TITLES.get(act, "Working"), f"@{username}", "Reading profile")
        try:
            sent, total = await self._job(bot, chat_id, status, act, username, n, hl_pk)
            if total:
                await status.done(f"Sent {sent} of {total} from @{username}" + ("" if sent == total else " (some files could not be downloaded)"))
            else:
                await status.done(f"@{username}")
        except Exception as e:
            if not isinstance(e, (PermissionError, RuntimeError)):
                log.warning("Dump %s for @%s failed: %s", act, username, e)
            await status.fail(self._explain(e))
        finally:
            self._busy.discard(chat_id)

    async def _job(self, bot, chat_id, status, act, username, n, hl_pk) -> tuple[int, int]:
        """Returns (items sent, items attempted); (0, 0) for jobs without a count."""
        if act == "pp":
            user = await self._profile(username)
            url = str(user.profile_pic_url_hd or user.profile_pic_url or "")
            data = await self._fetch(url) if url else None
            if not data:
                raise RuntimeError("No profile picture could be downloaded.")
            await send_one(bot, chat_id, "photo", data, f"🖼 @{_esc(username)}")
            return 0, 0

        if act == "js":
            user = await self._profile(username)
            doc = {
                "pk": str(user.pk),
                "username": user.username,
                "full_name": user.full_name,
                "biography": user.biography,
                "external_url": str(user.external_url) if user.external_url else None,
                "is_verified": user.is_verified,
                "is_private": user.is_private,
                "is_business": user.is_business,
                "category": user.category or user.category_name,
                "media_count": user.media_count,
                "follower_count": user.follower_count,
                "following_count": user.following_count,
                "profile_pic_url_hd": str(user.profile_pic_url_hd or user.profile_pic_url or ""),
            }
            if not user.is_private:
                medias = await self._ig(lambda: self.ctx.ig.cl.user_medias(str(user.pk), 12))
                doc["recent_posts"] = [self._post_meta(m) for m in medias]
            raw = json.dumps(doc, indent=2, ensure_ascii=False, default=str).encode("utf-8")
            await bot.send_document(chat_id, document=InputFile(io.BytesIO(raw), filename=f"{username}_profile.json"))
            return 0, 0

        user = await self._public_profile(username)  # everything below needs a public account
        pk = str(user.pk)

        if act in ("lf", "lg"):
            return await self._send_list(bot, chat_id, status, user, act == "lf", n)

        if act in ("p", "r"):
            if act == "p":
                n = max(1, min(n or POSTS_DEFAULT, POSTS_MAX))
                medias = await self._ig(lambda: self.ctx.ig.cl.user_medias(pk, n))
            else:
                n = max(1, min(n or REELS_DEFAULT, REELS_MAX))
                medias = await self._ig(lambda: self.ctx.ig.cl.user_clips(pk, n))
            if not medias:
                raise RuntimeError("No posts found." if act == "p" else "No reels found.")
            return await self._send_many(bot, chat_id, status, medias[:n], "post" if act == "p" else "reel")

        if act == "st":
            stories = await self._ig(lambda: self.ctx.ig.cl.user_stories(pk))
            if not stories:
                raise RuntimeError("No active stories right now.")
            return await self._send_many(bot, chat_id, status, stories, "story")

        if act == "h":
            hl = await self._ig(lambda: self.ctx.ig.cl.highlight_info(hl_pk))
            if not hl.items:
                raise RuntimeError("That highlight is empty.")
            return await self._send_many(bot, chat_id, status, hl.items, "story")

        raise RuntimeError("Unknown action.")

    async def _send_list(self, bot, chat_id, status, user, followers: bool, n: int) -> tuple[int, int]:
        """Followers / following of a public account as a txt or json file: id, username and display name only."""
        n = max(1, min(n or LIST_DEFAULT, LIST_MAX))
        word = "followers" if followers else "following"
        total = user.follower_count if followers else user.following_count
        await status.set(f"Collecting up to {n}. Large lists take a few minutes.", None)
        pk = str(user.pk)
        cl = self.ctx.ig.cl
        found = await self._ig(lambda: (cl.user_followers(pk, amount=n) if followers else cl.user_following(pk, amount=n)))
        people = [{"id": str(u.pk), "username": u.username, "full_name": u.full_name or ""} for u in list(found.values())[:n]]
        if not people:
            raise RuntimeError(f"No {word} could be read (the list may be hidden).")
        fmt = self._fmt.get(chat_id, "txt")
        if fmt == "json":
            raw = json.dumps({"account": user.username, "list": word, "listed": len(people), "total": total, "users": people},
                             indent=2, ensure_ascii=False).encode("utf-8")
        else:
            head = f"# {word} of @{user.username}: {len(people)} of {total}\n# username | full name | id\n"
            raw = (head + "\n".join(f"{p['username']} | {p['full_name']} | {p['id']}" for p in people)).encode("utf-8")
        await bot.send_document(
            chat_id, document=InputFile(io.BytesIO(raw), filename=f"{user.username}_{word}.{fmt}"),
            caption=f"{len(people)} {word} of @{user.username} (of {_num(total)} total)",
        )
        return 0, 0

    async def _send_many(self, bot, chat_id, status, medias, kind: str) -> tuple[int, int]:
        sent = 0
        for i, m in enumerate(medias, 1):
            await status.set(f"Sending {i} of {len(medias)}", (i - 1) / len(medias))
            items = items_of(m)
            if items and await send_items(bot, chat_id, items, caption_of(m, kind), self._fetch):
                sent += 1
            if i != len(medias):
                await asyncio.sleep(random.uniform(0.8, 1.8))  # gentle pace, for Instagram and Telegram
        return sent, len(medias)

    async def _show_highlights(self, bot, chat_id: int, username: str) -> None:
        try:
            user = await self._public_profile(username)
            highlights = await self._ig(lambda: self.ctx.ig.cl.user_highlights(str(user.pk)))
        except Exception as e:
            await bot.send_message(chat_id, self._explain(e))
            return
        if not highlights:
            await bot.send_message(chat_id, f"@{username} has no highlights.")
            return
        rows, row = [], []
        for h in highlights[:HIGHLIGHT_BUTTONS]:
            pk = str(h.pk).split(":")[-1]
            row.append(_btn(f"{_clip(h.title or 'Highlight', 18)} ({h.media_count or '?'})", "h", f"{pk}:{username}"))
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        await bot.send_message(
            chat_id, f"⭐ <b>Highlights of @{_esc(username)}</b>\nTap one to download it.", parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(rows),
        )

    # ------------------------------------------------------------------ commands
    async def _target(self, args, update, usage: str) -> str | None:
        username = parse_target(args[0]) if args else None
        if not username:
            await update.effective_message.reply_text(f"Usage: {usage}\n(a username, @username or instagram.com link)")
        return username

    async def _card_message(self, update, username: str, detailed: bool, with_menu: bool) -> None:
        message = update.effective_message
        try:
            user = await self._profile(username)
        except Exception as e:
            await message.reply_text(self._explain(e))
            return
        text = self._card(user, detailed)
        markup = self._menu(username, bool(user.is_private)) if with_menu else None
        pic = str(user.profile_pic_url_hd or user.profile_pic_url or "")
        data = await self._fetch(pic) if pic else None
        if data:
            try:
                await message.reply_photo(photo=data, caption=text, parse_mode="HTML", reply_markup=markup)
                return
            except BadRequest as e:
                log.info("Card photo refused (%s), sending text", e)
        await message.reply_text(text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)

    async def cmd_dump(self, args, update):
        username = await self._target(args, update, "/dump <username or link>")
        if username:
            await self._card_message(update, username, detailed=True, with_menu=True)

    async def cmd_profile(self, args, update):
        username = await self._target(args, update, "/profile <username or link>")
        if username:
            await self._card_message(update, username, detailed=True, with_menu=False)

    async def _simple(self, args, update, usage: str, act: str, default: int = 0, cap: int = 0) -> None:
        username = await self._target(args, update, usage)
        if not username:
            return
        n = default
        if cap and len(args) > 1:
            try:
                n = max(1, min(int(args[1]), cap))
            except ValueError:
                pass
        await self._run(update.get_bot(), update.effective_chat.id, act, username, n)

    async def _list_cmd(self, args, update, act: str, usage: str) -> None:
        username = await self._target(args, update, usage)
        if not username:
            return
        chat_id, n = update.effective_chat.id, LIST_DEFAULT
        for a in args[1:]:
            if a.lower() in ("json", "txt"):
                self._fmt[chat_id] = a.lower()
            else:
                try:
                    n = max(1, min(int(a), LIST_MAX))
                except ValueError:
                    pass
        await self._run(update.get_bot(), chat_id, act, username, n)

    async def cmd_followers(self, args, update):
        await self._list_cmd(args, update, "lf", "/followers <username> [count] [json]")

    async def cmd_following(self, args, update):
        await self._list_cmd(args, update, "lg", "/following <username> [count] [json]")

    async def cmd_pfp(self, args, update):
        await self._simple(args, update, "/pfp <username>", "pp")

    async def cmd_posts(self, args, update):
        await self._simple(args, update, "/posts <username> [count]", "p", POSTS_DEFAULT, POSTS_MAX)

    async def cmd_reels(self, args, update):
        await self._simple(args, update, "/reels <username> [count]", "r", REELS_DEFAULT, REELS_MAX)

    async def cmd_stories(self, args, update):
        await self._simple(args, update, "/stories <username>", "st")

    async def cmd_highlights(self, args, update):
        await self._simple(args, update, "/highlights <username>", "hl")

    # ------------------------------------------------------------------ buttons
    async def on_button(self, rest: str, query, context) -> None:
        act, _, arg = rest.partition(":")
        bot, chat_id = context.bot, query.message.chat_id

        if act == "h":
            hl_pk, _, username = arg.partition(":")
            if hl_pk.isdigit() and USERNAME_RE.match(username):
                await self._run(bot, chat_id, "h", username, hl_pk=hl_pk)
            return

        if act == "cu":  # "Type a number"
            kind, _, who = arg.partition(":")
            if kind in ("p", "r", "lf", "lg") and USERNAME_RE.match(who):
                self._awaiting[chat_id] = (kind, who)
                cap = {"p": POSTS_MAX, "r": REELS_MAX}.get(kind, LIST_MAX)
                await bot.send_message(chat_id, f"Send how many (1-{cap}) for @{who}, or “cancel”.")
            return

        username = arg
        if not USERNAME_RE.match(username):
            return

        if act == "ft":
            self._fmt[chat_id] = "json" if self._fmt.get(chat_id, "txt") == "txt" else "txt"
            act = "lm"
        if act == "lm":
            try:
                await query.edit_message_reply_markup(reply_markup=self._list_menu(chat_id, username))
            except TelegramError:
                pass
            return

        if act in ("pm", "rm", "bk"):
            if act == "bk":
                private = self._private.get(username)
                if private is None:
                    try:
                        private = bool((await self._profile(username)).is_private)
                    except Exception as e:
                        await query.message.reply_text(self._explain(e))
                        return
                markup = self._menu(username, private)
            else:
                markup = self._count_menu("p" if act == "pm" else "r", username)
            try:
                await query.edit_message_reply_markup(reply_markup=markup)
            except TelegramError:
                pass
            return

        counted = re.fullmatch(r"([pr])(\d+)", act)
        if counted:
            await self._run(bot, chat_id, counted.group(1), username, int(counted.group(2)))
            return

        listed = re.fullmatch(r"l([fg])(\d+)", act)
        if listed:
            await self._run(bot, chat_id, "l" + listed.group(1), username, int(listed.group(2)))
        elif act in ("pp", "st", "hl", "js"):
            await self._run(bot, chat_id, act, username)

    # ------------------------------------------------------------------ typed number
    async def on_text(self, text: str, update) -> bool:
        message = update.effective_message
        wanted = self._awaiting.pop(message.chat_id, None)
        if not wanted:
            return False
        kind, username = wanted
        word = text.strip().lower()
        if word in ("cancel", "stop"):
            await message.reply_text("Cancelled.")
            return True
        cap = {"p": POSTS_MAX, "r": REELS_MAX}.get(kind, LIST_MAX)
        if not word.isdigit() or not 1 <= int(word) <= cap:
            self._awaiting[message.chat_id] = wanted
            await message.reply_text(f"Send a number from 1 to {cap} (or “cancel”).")
            return True
        await self._run(update.get_bot(), message.chat_id, kind, username, int(word))
        return True
