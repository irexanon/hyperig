from core.auth import CookieError
from modules.base import BaseModule, Command


class SessionModule(BaseModule):
    name = "session"
    description = "Instagram login and session management"

    def get_commands(self):
        return {
            "session": Command(self.session, "Show the Instagram session, device and login details", "/session"),
            "relogin": Command(self.relogin, "Fresh Instagram login, keeping the same device identity", "/relogin"),
            "cookie": Command(
                self.cookie,
                "First login from a cloud server: log in with your browser's sessionid cookie",
                "/cookie <sessionid | Cookie header | JSON export>",
                sensitive=True,
            ),
            "code": Command(
                self.code,
                "Send the verification / 2FA code Instagram asked for",
                "/code 123456",
                sensitive=True,
            ),
            "resetsession": Command(
                self.resetsession,
                "Forget the saved session AND device, log in as a new device",
                "/resetsession confirm",
            ),
            "logout": Command(self.logout, "Log out of Instagram and forget the session", "/logout confirm"),
        }

    async def session(self, args):
        ig = self.ctx.ig
        state = "✅ logged in" if ig.logged_in else "❌ logged out"
        waiting = "\n🔐 Waiting for your /code" if ig.auth.prompter.waiting else ""
        return f"{state}\n{ig.auth.info()}{waiting}"

    async def relogin(self, args):
        ok = await self.ctx.ig.relogin()
        return "✅ Logged in again." if ok else "❌ Login failed, see the message above."

    async def cookie(self, args):
        if not args:
            return (
                "🍪 First login from a server\n"
                "1. On your own phone/PC, log in to instagram.com in a browser.\n"
                "2. Copy the 'sessionid' cookie (DevTools > Application > Cookies > instagram.com, "
                "or export the cookies as JSON).\n"
                "3. Send: /cookie <value>  (a Cookie header or JSON export works too)\n"
                "4. Close that browser tab WITHOUT logging out.\n\n"
                "I delete your message right away (a private chat with me is safest). I log in with the cookie, "
                "then try a normal password login from this server on top of it, and keep the cookie session "
                "if that fails."
            )
        try:
            method, note = await self.ctx.ig.cookie_login(" ".join(args))
        except CookieError as e:
            return f"❌ {e}"
        if method == "password":
            return "✅ Logged in with the cookie, then upgraded to a normal password session. Saved."
        text = "✅ Logged in with the cookie and polling is running. Don't log out of that browser session."
        if note:
            text += f"\nℹ️ The password login on top did not work, so I kept the cookie session: {note}"
        return text

    async def code(self, args):
        if not args:
            return "Usage: /code 123456"
        if self.ctx.ig.auth.prompter.submit("".join(args)):
            return "✅ Code received, continuing the login."
        return "No code is being waited for right now."

    async def resetsession(self, args):
        if args[:1] != ["confirm"]:
            return (
                "⚠️ This deletes the saved session AND the device identity, then logs in as a brand-new device. "
                "Only do this if /relogin keeps failing.\nSend /resetsession confirm to proceed."
            )
        ok = await self.ctx.ig.reset_session()
        return "✅ New session created." if ok else "❌ Login failed, see the message above."

    async def logout(self, args):
        if args[:1] != ["confirm"]:
            return "⚠️ This logs the account out on Instagram's side. Send /logout confirm to proceed."
        await self.ctx.ig.logout()
        return "👋 Logged out. Send /relogin to log in again."
