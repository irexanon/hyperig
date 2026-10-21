# Fetch IG

A Telegram bot for Instagram. Every user connects their own Instagram account inside the bot, then can:

- paste an Instagram link (reel, post, story, highlight) and get the media
- open public profiles with `/dump`: posts, reels, stories, highlights, followers and following exports
- watch public accounts with `/watch` and get new posts, stories and profile changes in the chat
- check `/status`, change `/settings`, and delete everything with `/logout`

There is no bridge, web dashboard or realtime connection. The bot only talks to Instagram when you ask it to, or when the watcher checks.

## Connecting an account

`/connect` offers two ways, both inside the chat:

- **Cookie (recommended):** paste the `sessionid` cookie from instagram.com. It avoids new-login checks.
- **Password:** username and password. If Instagram asks for a verification code, send it as the next message. The password is used once and never saved; your message with it is deleted.

Only the resulting session is stored, per Telegram user. Nobody can use anyone else's account, and `/logout` deletes the session, settings and watchlist.

Limits: 10 watched accounts per user, one running job per chat, files up to 50 MB. Public accounts only.

## Run

```
pip install -r requirements.txt
cp .env.example .env   # set TELEGRAM_BOT_TOKEN
python main.py
```

Docker: `docker build -t fetch-ig . && docker run --env-file .env -v fetchig-data:/app/data fetch-ig`

Set `MONGO_URI` to keep data on a database instead of files. Instagram flags datacenter IPs and repeated logins, so for a server deployment set `IG_PROXY` to a residential proxy and prefer cookie login.

## Add a feature

Drop a file in `modules/` with a `BaseModule` subclass. It is loaded for every user automatically. See `modules/status.py` for the smallest example.

