from datetime import datetime, timezone

from modules.base import BaseModule, Command


def _ago(dt: datetime | None) -> str:
    if not dt:
        return "never"
    return f"{int((datetime.now(timezone.utc) - dt).total_seconds())}s ago"


class CoreModule(BaseModule):
    name = "core"
    description = "Core bot commands and system information"

    def __init__(self, ctx):
        super().__init__(ctx)
        self.seen = 0

    def get_commands(self):
        return {
            "ping": Command(self.ping, "Check the bot is alive", "/ping"),
            "status": Command(self.status, "Show bot and Instagram status", "/status"),
        }

    async def process(self, message):
        self.seen += 1
        return message

    async def ping(self, args):
        return "🏓 Pong!"

    async def status(self, args):
        ig = self.ctx.ig
        up = datetime.now(timezone.utc) - self.ctx.started_at
        hours, rem = divmod(int(up.total_seconds()), 3600)
        minutes, seconds = divmod(rem, 60)
        return (
            "🚀 Hyper Insta\n"
            f"Instagram: {'✅ logged in' if ig.logged_in else '❌ logged out (use /relogin)'}\n"
            f"Last inbox check: {_ago(ig.last_poll)}\n"
            f"{ig.realtime.status()}\n"
            f"Messages seen: {self.seen}\n"
            f"Forwarded to Telegram: {self.ctx.bridge.forwarded if self.ctx.bridge else 0}\n"
            f"Linked chats: {len(self.ctx.bridge.topics) if self.ctx.bridge else 0}\n"
            f"Uptime: {hours}h {minutes}m {seconds}s"
        )
