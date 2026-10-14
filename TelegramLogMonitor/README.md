# Darkweb OSINT Tool

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
| POST | `/api/bots/{username}/search` | `{"text": "/search …", "format": "<id>"}` runs the search: sends it, clicks the chosen option button, fetches the file; returns `{clicked, file}` |
| GET | `/api/messages/{id}/file` | Download a file the bot sent (fetched from Telegram first if not saved yet) |
| POST | `/api/messages/{id}/fetch` | Pull a not-yet-saved file from Telegram onto the server |
| DELETE | `/api/messages/{id}` | Delete one message (and its downloaded file) |
| POST | `/api/messages/delete` | Bulk delete by filter: `{bot, severity, q, media_only, older_than_days}` — returns `{deleted}` |
| GET | `/api/config`, `/api/status`, `/api/health` | Dashboard metadata, collector state, liveness |

## Hiding lines in messages

To drop unwanted lines from bot messages (for example upsell/advert lines), list
case-insensitive patterns under `hide_lines` in `config.yaml`. Any line matching one is
removed before the message is stored or shown — it never reaches the database, the feed, or
search. Existing stored messages are not re-filtered; only messages received after the change
are affected.

```yaml
hide_lines:
  - 'free version shows'
  - 'premium shows'
```

## Removing messages and files

Besides the automatic `retention.days` cleanup, you can delete on demand from the dashboard:

* **One at a time** — each row in the **Feed** and **Files** tabs has a ✕ button that deletes that
  message (and its downloaded file).
* **In bulk** — the **Remove … [Delete]** control deletes everything matching the current view
  and filters. Pick *all shown* or *older than 1 / 7 / 30 / 90 days*. In the **Files** tab it
  deletes only file messages; in the **Feed** tab it deletes all matching messages. The bot
  filter, severity chips and search box narrow what is removed.

Deletes are permanent (the only way back is the bot re-sending the message) and apply to every
connected dashboard immediately.

## Search command & string formats

Some bots answer a `/search` command with a set of inline buttons ("Choose strings format",
e.g. `url:login:pass` and `login:pass`) and only send the result file once you pick one. The
dashboard runs that whole exchange for you.

When a command you send starts with one of the configured trigger commands (default
`/search`, `/lookup`, `/osint`), the **Choose strings format** selector appears. On send, the
intelligence-sweep animation plays while the server:

1. sends your command to the bot (e.g. `/search acme-corp`);
2. waits for the bot's reply and **clicks the button** matching the chosen format;
3. waits for the file the bot sends back and **fetches it** into the dashboard.

The file then appears in the feed and the **Files** tab, ready to download.

Everything is configurable under `search:` in `config.yaml`, with no code changes. The key
setting is **`button`** — it must match the bot's inline button text exactly:

```yaml
search:
  trigger_commands: ['/search', '/lookup', '/osint']
  reply_timeout_seconds: 90          # how long to wait for the buttons, then the file
  default_format: url_login_pass
  formats:
    - { id: url_login_pass, label: 'url:login:pass', button: 'url:login:pass', description: URL, login and password }
    - { id: login_pass,     label: 'login:pass',     button: 'login:pass',     description: Login and password }
```

If your bot takes a text flag instead of buttons, set `suffix: ' --format=json'` on a format
and leave `button` blank — the suffix is appended to the command and no button is clicked.
If the bot offers different button labels than configured, the dashboard reports the labels it
actually saw so you can correct `button`.

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
