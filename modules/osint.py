import re
from core import osint_tools
from modules.base import BaseModule, Command
from utils.db import sanitize_username


def _strip_html(text: str) -> str:
    s = re.sub(r"<[^>]+>", "", text)
    return s.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")


class OSINTModule(BaseModule):
    name = "osint"
    description = "OSINT Profile Lookup, DB History, Downloader & Watcher"

    def get_commands(self):
        return {
            "profile": Command(
                self.cmd_profile,
                "Fetch profile + OSINT metadata & save to Profile DB",
                "/profile [@username]",
            ),
            "dump": Command(
                self.cmd_profile,
                "Interactive OSINT dump & profile lookup",
                "/dump <@username>",
            ),
            "watch": Command(
                self.cmd_watch,
                "List or toggle watched Instagram accounts",
                "/watch [@username]",
            ),
            "dl": Command(
                self.cmd_dl,
                "Auto-detect and download any Instagram URL on Telegram",
                "/dl <instagram_url>",
            ),
        }

    async def cmd_profile(self, args: list[str]) -> str:
        target = args[0] if args else self.ctx.cfg.ig_username
        uname = sanitize_username(target)
        if not uname:
            return "Usage: /profile <@username>"
        if not self.ctx.ig.logged_in:
            return "⚠️ Log in to an Instagram account first."
        rec = await self.ctx.ig._call(osint_tools._fetch_profile_sync, self.ctx.ig.cl, uname, True)
        lines = [
            f"🕵️ @{rec['username']} (PK: {rec['pk']})",
            f"Name: {rec.get('full_name') or '—'}",
            f"Followers: {rec['follower_count']:,} | Following: {rec['following_count']:,} | Posts: {rec['media_count']:,}",
            f"Private: {rec['is_private']} | Verified: {rec['is_verified']} | Business: {rec['is_business']}",
        ]
        if rec.get("biography"):
            lines.append(f"Bio: {rec['biography']}")
        if rec.get("external_url"):
            lines.append(f"URL: {rec['external_url']}")
        about = rec.get("about") or {}
        if about:
            lines.append(
                f"OSINT: Joined={about.get('date_joined', '?')} | Country={about.get('country', '?')} | Former Usernames={about.get('former_usernames', '?')}"
            )
        return "\n".join(lines)

    async def cmd_watch(self, args: list[str]) -> str:
        if not args:
            return "Use /watch <@username> in the Telegram Bot to manage watched targets and receive story/profile alerts."
        uname = sanitize_username(args[0])
        return f"Use /watch @{uname} in your Telegram Bot DM to toggle background OSINT & Story monitoring."

    async def cmd_dl(self, args: list[str]) -> str:
        if not args:
            return "Paste any Instagram link directly into your Telegram Bot chat (or /dl <url>) to auto-download."
        cat, target = osint_tools.detect_ig_url_category(args[0])
        return f"Detected category: {cat.upper()} ({target}). Send this link in your Telegram Bot DM to receive the downloaded media."
