from datetime import datetime, timezone

from modules.base import BaseModule, Command


def _ago(ts) -> str:
    if not ts:
        return "never"
    secs = int(datetime.now(timezone.utc).timestamp() - ts)
    if secs < 90:
        return "just now"
    if secs < 5400:
        return f"{secs // 60} min ago"
    return f"{secs // 3600} h ago"


class StatusModule(BaseModule):
    name = "status"
    description = "Connection status"

    def get_commands(self):
        return {"status": Command(self.status, "Account and watcher status", "/status")}

    async def status(self, args):
        ig = self.ctx.ig
        lines = ["Status", ""]
        if ig.logged_in:
            lines.append(f"Instagram: connected as @{ig.username} ({ig.auth.login_method})")
        else:
            lines.append("Instagram: not connected. Use /connect.")
            if ig.last_alert:
                lines.append(ig.last_alert)
        watcher = self.ctx.modules.get("watcher") if self.ctx.modules else None
        if watcher is not None:
            if not self.ctx.modules.is_enabled(watcher):
                lines.append("Watcher: off")
            else:
                h = watcher.health()
                lines.append(f"Watcher: {h['accounts']} account(s), last check {_ago(h['last_check'])}")
                for _when, user, text in h["errors"][-2:]:
                    lines.append(f"  @{user}: {text}")
        return "\n".join(lines)
