# Telegram Log Monitor

Collects messages from third-party Telegram bots (**@perimeterlog_bot**, **@TTMlogsBot**) in the
background, stores them, classifies severity, extracts indicators (IPs, ports, CVEs, domains) and
shows everything live in a web dashboard.

## How it works (and why)

```
 @perimeterlog_bot ─┐                        ┌──────────────── FastAPI ────────────────┐
                    ├─► Telegram (MTProto) ─► Telethon user client ─► SQLite ─► REST API ─► Dashboard
 @TTMlogsBot ───────┘    as YOUR account      (live + backfill)       │        SSE /api/stream (live)
                                                                      └─ data/media (files bots send)
```

These bots belong to someone else, so the **Bot API cannot be used**: reading a bot's
messages through the Bot API needs that bot's token, and only its owner has it. Bots also
can't see other bots' messages in groups.

Instead, the backend logs in as **your own Telegram account**, using the user API (MTProto via
[Telethon](https://docs.telethon.dev)). It reads your private chats with those bots, exactly
as the Telegram app does. Whatever the bots send you shows up in the dashboard, and you can
send them commands (`/start`, `/status`, …) from the dashboard too.

* On first start it backfills the last `backfill_limit` messages per bot. After a restart it
  resumes from the last stored message id, so nothing is missed while it was down.
* Edited bot messages are updated in place.
* Files and photos the bots send can be downloaded from the dashboard (see below).
* All rules live in `config.yaml`: bots, severity keywords/colours, extractors, retention and UI.
  You change the config, not the code.

## Setup

1. **Get API credentials.** Sign in at <https://my.telegram.org>, open *API development tools* and
   create an app. Note the `api_id` and `api_hash`.
2. **Install**
   ```bash
   cd TelegramLogMonitor
   python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
   pip install -r requirements.txt
   cp .env.example .env        # then fill in TG_API_ID and TG_API_HASH
   ```
3. **Log in once** (interactive: phone number, login code, 2FA password if you use one):
   ```bash
   python -m app.login
   ```
   This creates `data/monitor.session` and checks both bots. If you have never opened a bot, it
   offers to send `/start` to it.
4. **Run**
   ```bash
   python -m app.main            # or: uvicorn app.main:app --host 127.0.0.1 --port 8000
   ```
   Open <http://127.0.0.1:8000>.

**Try it without Telegram:** set `DEMO_MODE=true` in `.env` (or run
`DEMO_MODE=true python -m app.main`). The server then generates realistic fake messages.

## Downloading files the bots send

Files and photos the bots send appear in two places on the dashboard:

* **Feed:** each message with a file shows a file card with a **Download** button. Photos also
  show a thumbnail, and clicking the thumbnail opens it full size.
* **Files tab:** every file from every bot in one list, with its name, bot, time, size and caption.
  The bot filter and search box work here too, and search also matches file names.

How the server stores files:

| Size | What happens |
|---|---|
| Up to `media.max_size_mb` (default 20 MB) | Saved to `data/media/` as soon as it arrives. The button says **Download**. |
| Larger, up to `media.on_demand_max_mb` (default 500 MB) | Stays on Telegram until you click **Fetch & download**. The server then pulls it from Telegram, saves it and starts the download. |
| Over `media.on_demand_max_mb` | The button shows **Too large**. Raise the limit in `config.yaml` if you need it. |

Downloads are served as attachments, so a file can never run inside the dashboard. Only images
are displayed in the page. Saved files are deleted together with their messages when
`retention.days` expires.

## Configuration

| Where | What |
|---|---|
| `.env` | Credentials, session path, host/port, `APP_API_KEY`, DB/media paths, `DEMO_MODE` |
| `config.yaml` → `bots` | Usernames, display labels, backfill size, start command |
| `config.yaml` → `severity` | Levels (name, rank, colour, regex patterns) and the default level |
| `config.yaml` → `extractors` | Named regexes for indicators shown as clickable tags |
| `config.yaml` → `media` | Auto-save limit, on-demand fetch limit, photo previews |
| `config.yaml` → `retention` / `ui` | Days to keep, page size, refresh rate |

To monitor another bot, add it under `bots:` and restart.

## API

All endpoints except `/api/health` require `X-API-Key` (or `?key=`) when `APP_API_KEY` is set.

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/messages?bot=&severity=&q=&since=&until=&include_outgoing=&media_only=&limit=&offset=` | Search and filter stored messages (`bot`/`severity` repeatable; `media_only=true` lists files) |
| GET | `/api/stats` | Totals, per-bot counts and last-seen time, 24h severity counts, hourly histogram |
| GET | `/api/stream` | Server-Sent Events: one `message` event per new or edited message |
| POST | `/api/bots/{username}/send` | `{"text": "/status"}` sends a message to the bot from your account |
| GET | `/api/messages/{id}/file` | Download a file the bot sent (fetched from Telegram first if not saved yet) |
| POST | `/api/messages/{id}/fetch` | Pull a not-yet-saved file from Telegram onto the server |
| GET | `/api/config`, `/api/status`, `/api/health` | Dashboard metadata, collector state, liveness |

## Security notes

* **`data/monitor.session` is a full login to your Telegram account.** Keep it private. It is
  git-ignored, and you should never commit or share it. To revoke it, go to Telegram →
  Settings → Devices.
* The server binds to `127.0.0.1` by default. If you expose it, set `APP_API_KEY` and put it
  behind HTTPS (reverse proxy).
* Bot messages are untrusted input. The dashboard renders them as plain text only.
* Use this only with bots you're allowed to use. Automating a user account is allowed under
  Telegram's API terms, but heavy or abusive automation can get the account limited.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```
