# Hyper Insta (Python)

Bridges your Instagram DMs to Telegram. Each Instagram chat becomes a topic in a Telegram
group; type in the topic to reply on Instagram. Built on `instagrapi` + `python-telegram-bot`.

```
main.py            entry point
config.py          settings from environment variables
core/              Instagram client (login, session, polling), realtime MQTT/FBNS, built-in /status and session commands, module manager, message handler
modules/           drop-in plugins (help.py, ...); each adds Telegram commands
tg/                Telegram bridge (named tg, not telegram, to avoid shadowing the library)
utils/             logger, storage (JSON files or MongoDB), message formatting
```

## Setup
1. **Telegram bot:** create one with @BotFather, copy the token.
2. **Group:** create a group, open its settings and enable **Topics**. Add your bot and make it
   an **admin with "Manage Topics"**. Get the group id (starts with `-100`) and your own user id
   (e.g. from @userinfobot).
3. `cp .env.example .env` and fill it in. Never commit `.env`.
4. `pip install -r requirements.txt` then `python main.py`
   (or build the Dockerfile and mount a volume on `/app/data`).

## Login and session management
This follows instagrapi's own guidance (Best Practices, Login Migration, Challenge Resolver,
Handle Exceptions, TOTP). Everything lives in `core/auth.py`.

* **Log in once, reuse forever.** The full session (device identity, tokens, cookies) is saved to
  `data/` (or MongoDB) after login and whenever it changes, and always reloaded. `login()` validates
  it and only does a real password login if Instagram rejects it, with the *same* device.
* **Device identity is sticky.** It is saved even when a fresh login fails, so the next attempt is
  the same "phone". Locale / country / timezone / app version from `.env` apply only when a *new*
  device is created, never to an existing session.
* **Codes come to Telegram.** SMS / email challenge codes, 2FA codes and 8-digit backup codes: the bot
  messages you, you reply `/code 123456` (the bot deletes your message). With `IG_TOTP_SECRET` set,
  authenticator codes are generated automatically.
* **No login loops.** One automatic re-login on `login_required`, never twice within 10 minutes.
  After that you get an alert and use `/relogin`. A failed start does not exit (a restart loop
  would hammer Instagram); the bot stays up and waits for you.
* **Errors are classified.** Throttling / `feedback_required` -> 15 min pause. Bad password, security
  check, selfie/captcha, expired session -> stops and tells you exactly what to do.
* **Old app profiles are migrated.** If a saved session lacks a Bloks hash or Instagram returns `needs_upgrade`, the bot retries once with instagrapi's current supported app profile, keeping the device identity and cookies. Set `IG_OVERRIDE_APP_VERSION=true` to force the current profile from startup.
  (`override_app_version`), keeping the device and cookies.
* A saved session for a different `IG_USERNAME` is ignored, never reused.

**First login from a cloud server: `/cookie`.** Password logins from datacenter IPs are often blocked
(`please wait a few minutes`, `bad_password`). Log in to instagram.com on your computer, copy the
`sessionid` cookie (DevTools > Application > Cookies, or a cookie-export extension), and send
`/cookie <value>` to the bot (a bare value, a `sessionid=...; ...` string, or exported JSON all work).
The bot deletes your message, logs in with it (no password attempt), checks it belongs to
`IG_USERNAME`, saves the session, then (if `IG_COOKIE_UPGRADE=true`) tries a password login from
the server on top of it and silently keeps the cookie session if that fails. Give the bot the
"Delete messages" permission so your cookie doesn't stay in the chat.

**First-login checklist** (the usual causes of logouts):
1. Log in from the same machine/IP that will keep running the bot. Don't change IP afterwards.
2. If you use a proxy, use one stable proxy (`IG_PROXY`) and never rotate it.
3. Keep the `data/` volume. Deleting it forces a new device.
4. Approve the "was this you?" prompt in the Instagram app if it appears.
5. Keep `POLL_INTERVAL` at 30s or more.

## Commands (admin only)
| Command | What it does |
|---|---|
| `/ping`, `/status`, `/help` | health and help |
| `/session` | account, login method, device, app version, locale, proxy host (no secrets) |
| `/cookie <sessionid>` | first login from a server using a browser cookie |
| `/relogin` | fresh login, same device identity |
| `/code 123456` | answer a verification / 2FA prompt |
| `/resetsession confirm` | forget session AND device, log in as a new device (last resort) |
| `/logout confirm` | log out on Instagram and forget the session |
| `/dump <user or link>` | profile card with buttons: posts, reels, stories, highlights, profile pic, JSON |
| `/profile <user>` | detailed public profile info |
| `/pfp <user>` | profile picture, full size |
| `/posts <user> [n]`, `/reels <user> [n]` | latest posts (1-20) or reels (1-10) |
| `/stories <user>`, `/highlights <user>` | current stories, or a button list of highlights |

The profile tools (`modules/dump.py`) work on **public accounts only**; a private account just shows its
public card. They never show or export phone numbers, e-mail addresses or street addresses. One job runs
per chat at a time, with a short pause between items, to keep the logged-in account out of rate limits.
Run them in the private chat with the bot.

## Realtime delivery (MQTT + FBNS)
Both channels are instagrapi's own (`cl.realtime_*`, `cl.fbns_*`), enabled by default:

* **Realtime MQTT** is what the Instagram app uses for live DMs. After `direct_subscribe()` new messages,
  typing indicators and send acknowledgements are pushed to the bot in about a second. Your Telegram text
  replies are sent over the same connection and confirmed by Instagram (`IG_REALTIME_SEND`).
* **FBNS** is Instagram's push-notification channel. A push about a DM makes the bot check the inbox at once,
  and keeps working when the realtime socket is down. The FBNS device credentials are saved with the session.
* **Polling stays** as the safety net: every `SAFETY_POLL_INTERVAL` (300s) while MQTT is connected, every
  `POLL_INTERVAL` when it is not. A message is delivered exactly once whichever path sees it first
  (per-thread cursor plus a set of delivered message ids, saved with the session).
* Dropped connections reconnect with backoff and trigger an immediate backfill. `/status` shows both channels.
* Set `IG_REALTIME=false` / `IG_FBNS=false` to go back to polling only.

## Media
| | Instagram -> Telegram | Telegram -> Instagram |
|---|---|---|
| Text | yes | yes |
| Photo | native photo | photo (sent as JPEG) |
| Video | native video | video (converted to H.264/AAC MP4) |
| Voice note | Telegram voice bubble | voice message (converted to AAC .m4a) |
| Video note (round video) | - | video |
| GIF / animation | animation | video |
| Music / audio file | - | voice message |
| Sticker | - | photo (static) or video (video sticker); animated `.tgs` is refused |
| Image / video sent as a file | - | photo / video |
| Disappearing photo/video | forwarded, labelled "[disappearing]" | - |
| Shared posts, reels, stories, links | readable text + link | - |

Instagram media cannot carry a caption, so a Telegram caption follows the media as a separate text message.
Files are downloaded and re-uploaded by the bot (limit `MAX_MEDIA_MB`, Telegram bots can download up to 20 MB);
if a download or upload fails you get a link instead of losing the message. Conversion needs **ffmpeg**
(already in the Dockerfile; install it yourself if you run without Docker).

## Scope
Everything above, plus typing indicators (Instagram -> Telegram) and optional read receipts (`MARK_SEEN`).
Messages you send from the Instagram app itself are not mirrored to Telegram. Reactions and message edits/unsends
are not bridged yet.
