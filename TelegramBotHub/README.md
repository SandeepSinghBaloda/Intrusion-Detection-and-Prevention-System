# Telegram Bot Hub

Web dashboard (frontend) + bot runner (backend) for managing multiple Telegram bots. No dependencies — only Node.js 18+.

## Run

```bash
cd TelegramBotHub
npm start            # or: node server.js
# open http://127.0.0.1:3000
```

Optional environment variables: `PORT`, `HOST` (default `127.0.0.1`), `DASHBOARD_PASSWORD` (+ `DASHBOARD_USER`, default `admin`) to enable HTTP Basic auth, `TELEGRAM_INVITE_LINK`, `DATA_DIR`.
**Set `DASHBOARD_PASSWORD` if you bind `HOST=0.0.0.0`** — the dashboard can send messages with your bots.

## Setup

1. In Telegram, message **@BotFather** → `/newbot` → copy the token (one per bot).
2. Paste each token in the dashboard → **Add & start**. Bots run in the backend (long polling) and auto-restart with the server.
3. **Add each bot to your group** (<https://t.me/+SW4aCwaA0mNiZWZk>) as a member/admin. A bot cannot join through an invite link; a group admin must add it (group → Add members → search the bot's @username). For channels, make the bot an admin. If the bot should read all group messages, disable privacy mode via BotFather (`/setprivacy`).
4. Send `/id` in the group — the chat appears in "Known chats" and you can send/broadcast to it.

## Features

- Add/start/stop/remove multiple bots; tokens validated against Telegram (`getMe`)
- Built-in commands: `/start /help /ping /id /rules`
- Auto-reply rules (contains / exact; global or per-bot)
- Send to one chat or broadcast to all known chats, live log via SSE
- State saved in `data/store.json` (mode 600, git-ignored — contains bot tokens)

## Telegram account (API ID) — join the group & add bots

Get an **API ID + API hash** at <https://my.telegram.org> → *API development tools*. Enter them in the dashboard's *Telegram account* card (or set `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` env vars), then log in with your phone number + the code Telegram sends (and your 2-step password if enabled). Then:

- **Join group** — your account joins the invite link (saved in the dashboard).
- **Join & add all bots** — also adds every configured bot to the group. Your account needs the *Add members* admin right; bots must have a username.

The login session is stored in `data/store.json` (git-ignored, mode 600) — treat it like a password. Never share your API hash or session. Use this only with your own account and groups, and keep activity modest: Telegram can limit or ban accounts that automate aggressively.
Run `npm install` once before `npm start`.
