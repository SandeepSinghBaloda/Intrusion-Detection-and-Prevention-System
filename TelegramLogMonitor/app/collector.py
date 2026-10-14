"""Background collector: a Telegram *user* client that reads the configured bots' chats.

The Bot API cannot read another owner's bot, so this logs in as your own Telegram
account (MTProto via Telethon) and records what the bots send you, exactly as the
Telegram app on your phone would see it.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from telethon import TelegramClient, events
from telethon.tl.custom.message import Message

from .config import BotConfig, Rules, Settings
from .db import Store
from .hub import Hub
from .parser import classify, extract, sanitize

log = logging.getLogger(__name__)


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", name)[:100] or "file"


class FileTooLarge(Exception):
    pass


class FileUnavailable(Exception):
    """The message or its file no longer exists on Telegram (deleted, expired, not a file)."""


class SearchNoReply(Exception):
    """The bot did not reply (with option buttons, or at all) within the timeout."""


class SearchChoiceUnavailable(Exception):
    """The bot's reply did not offer a button matching the chosen format."""

    def __init__(self, wanted: str, available: list[str]):
        self.wanted = wanted
        self.available = available
        super().__init__(
            f"The bot did not offer a '{wanted}' option. It offered: "
            + (", ".join(available) if available else "no buttons"))


class BaseCollector:
    """Shared ingest path used by the real and the demo collector."""

    def __init__(self, settings: Settings, rules: Rules, store: Store, hub: Hub):
        self.settings = settings
        self.rules = rules
        self.store = store
        self.hub = hub
        self.status: dict = {"mode": "unknown", "connected": False, "error": None, "bots": {}}
        self._fetch_locks: dict[int, asyncio.Lock] = {}
        self._search_locks: dict[str, asyncio.Lock] = {}

    def ingest(self, *, bot: str, tg_message_id: int, date: str, text: str, outgoing: bool,
               edited_at: str | None = None, media_type: str | None = None,
               media_name: str | None = None, media_path: str | None = None,
               media_size: int | None = None, media_mime: str | None = None) -> dict:
        # Only the bot's own messages are classified; commands we send are stored as 'info'.
        if not outgoing:
            text = sanitize(text, self.rules)  # strip hidden lines before storing/showing
        severity = self.rules.default_severity if outgoing else classify(text, self.rules)
        record = self.store.upsert(
            bot=bot, tg_message_id=tg_message_id, date=date, text=text, severity=severity,
            extracted={} if outgoing else extract(text, self.rules), outgoing=outgoing,
            edited_at=edited_at, media_type=media_type, media_name=media_name, media_path=media_path,
            media_size=media_size, media_mime=media_mime)
        self.hub.publish({"type": "message", "data": record})
        return record

    def media_target(self, bot: str, tg_message_id: int, name: str) -> Path:
        return self.settings.media_dir / f"{_safe_name(bot)}_{tg_message_id}_{_safe_name(name)}"

    async def fetch_media(self, row) -> dict:
        """Downloads the file of a stored message that was not auto-saved; returns the updated record."""
        size = row["media_size"]
        if size is not None and size > self.rules.media_on_demand_max_bytes:
            raise FileTooLarge(size)
        lock = self._fetch_locks.setdefault(row["id"], asyncio.Lock())
        async with lock:  # double clicks / several viewers share one download
            current = self.store.get(row["id"])
            if current["media_path"] and Path(current["media_path"]).is_file():
                return self.store.set_media_path(row["id"], current["media_path"])
            target = self.media_target(row["bot"], row["tg_message_id"], row["media_name"] or "file")
            target.parent.mkdir(parents=True, exist_ok=True)
            await self._download(row, target)
            record = self.store.set_media_path(row["id"], str(target))
        self._fetch_locks.pop(row["id"], None)
        self.hub.publish({"type": "message", "data": record})
        return record

    async def _download(self, row, target: Path) -> None:
        raise FileUnavailable("Downloading is not supported by this collector")

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def send(self, bot: str, text: str) -> None: ...

    async def run_search(self, bot: str, command_text: str, *, button: str | None = None,
                         suffix: str = "", timeout: int = 60) -> dict:
        """Runs a search command and returns {"clicked": bool, "file": <record|None>}.

        Sends `command_text` to the bot. If `button` is given, waits for the bot's reply with
        inline buttons and clicks the one whose text matches `button` (the "Choose strings
        format" step); otherwise `suffix` is appended to the command. Then waits for the file
        the bot sends back and returns its record (it is also ingested by the live handler).
        """
        raise NotImplementedError


class TelegramCollector(BaseCollector):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.status["mode"] = "telegram"
        self.client: TelegramClient | None = None
        self._by_peer: dict[int, BotConfig] = {}
        self._entities: dict[str, object] = {}
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        s = self.settings
        if not (s.api_id and s.api_hash):
            raise RuntimeError("TG_API_ID / TG_API_HASH are not set in .env")
        s.session_path.parent.mkdir(parents=True, exist_ok=True)
        self.client = TelegramClient(str(s.session_path), s.api_id, s.api_hash)
        await self.client.connect()
        if not await self.client.is_user_authorized():
            raise RuntimeError("Telegram session is not logged in. Run `python -m app.login` once.")

        for bot in self.rules.bots:
            try:
                entity = await self.client.get_entity(bot.username)
            except Exception as exc:  # unknown username, privacy, flood wait...
                log.error("Cannot resolve @%s: %s", bot.username, exc)
                self.status["bots"][bot.username] = {"ok": False, "error": str(exc)}
                continue
            self._by_peer[entity.id] = bot
            self._entities[bot.username.lower()] = entity
            self.status["bots"][bot.username] = {"ok": True, "error": None}

        if not self._by_peer:
            raise RuntimeError("None of the configured bots could be resolved")

        # Register live handlers before backfilling so nothing slips through the gap;
        # upserts are idempotent so overlap is harmless.
        chats = list(self._entities.values())
        self.client.add_event_handler(self._on_event, events.NewMessage(chats=chats))
        self.client.add_event_handler(self._on_event, events.MessageEdited(chats=chats))

        for bot in self.rules.bots:
            if bot.username.lower() in self._entities:
                await self._backfill(bot)

        self.status["connected"] = True
        self._task = asyncio.create_task(self._run())
        log.info("Listening to %s", ", ".join("@" + b.username for b in self._by_peer.values()))

    async def _run(self) -> None:
        try:
            await self.client.run_until_disconnected()
        finally:
            self.status["connected"] = False

    async def stop(self) -> None:
        if self.client:
            await self.client.disconnect()
        if self._task:
            await asyncio.gather(self._task, return_exceptions=True)

    async def send(self, bot: str, text: str) -> None:
        entity = self._entities.get(bot.lower())
        if entity is None or self.client is None:
            raise KeyError(bot)
        await self.client.send_message(entity, text)  # echoed back via the NewMessage handler

    async def run_search(self, bot: str, command_text: str, *, button: str | None = None,
                         suffix: str = "", timeout: int = 60) -> dict:
        entity = self._entities.get(bot.lower())
        if entity is None or self.client is None or not self.status["connected"]:
            raise ConnectionError("Not connected to Telegram")
        bot_cfg = self.rules.bot(bot)
        lock = self._search_locks.setdefault(bot.lower(), asyncio.Lock())
        async with lock:
            # A conversation lets us wait for the bot's specific replies in order. The global
            # NewMessage handler also sees these messages, so they are stored either way.
            async with self.client.conversation(entity, timeout=timeout, exclusive=False) as conv:
                await conv.send_message(command_text + (suffix if suffix and not button else ""))
                clicked = False
                if button:
                    buttons_msg = await self._wait_for_buttons(conv, button, timeout)
                    await buttons_msg.click(text=button)  # pick the "Choose strings format" option
                    clicked = True
                file_record = await self._wait_for_file(conv, bot_cfg, timeout)
        return {"clicked": clicked, "file": file_record}

    async def _wait_for_buttons(self, conv, button: str, timeout: int):
        """Waits for a bot reply carrying inline buttons and checks the chosen one is present."""
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise SearchNoReply("The bot did not reply with options in time")
            try:
                resp = await conv.get_response(timeout=remaining)
            except asyncio.TimeoutError:
                raise SearchNoReply("The bot did not reply with options in time")
            if not resp.buttons:
                continue  # intermediate message (e.g. "Searching…"); keep waiting
            labels = [b.text for row in resp.buttons for b in row]
            if button in labels:
                return resp
            raise SearchChoiceUnavailable(button, labels)

    async def _wait_for_file(self, conv, bot_cfg: BotConfig, timeout: int):
        """Waits for the bot to send a file after the option is chosen; returns its record."""
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return None  # no file arrived; not fatal — the bot may still send it later
            try:
                resp = await conv.get_response(timeout=remaining)
            except asyncio.TimeoutError:
                return None
            if resp.media is not None and (resp.photo or resp.document):
                await self._ingest_message(bot_cfg, resp)  # idempotent with the live handler
                return self.store.record_by_tg(bot_cfg.username, resp.id)

    async def _backfill(self, bot: BotConfig) -> None:
        entity = self._entities[bot.username.lower()]
        last_id = self.store.last_message_id(bot.username)
        if last_id:  # resume: everything newer than what we already have
            msgs = [m async for m in self.client.iter_messages(entity, min_id=last_id)]
        else:
            msgs = [m async for m in self.client.iter_messages(entity, limit=bot.backfill_limit)]
        for m in reversed(msgs):  # oldest first
            await self._ingest_message(bot, m)
        log.info("@%s: backfilled %d message(s)", bot.username, len(msgs))

    async def _on_event(self, event) -> None:
        bot = self._by_peer.get(event.chat_id)
        if bot is None:
            return
        try:
            await self._ingest_message(bot, event.message)
        except Exception:
            log.exception("Failed to store message %s from @%s", event.message.id, bot.username)

    async def _save(self, m: Message, target: Path) -> None:
        """Downloads to a .part file first so an interrupted download never looks complete."""
        partial = target.with_name(target.name + ".part")
        saved = await m.download_media(file=str(partial))
        if not saved:
            raise FileUnavailable("Telegram returned no file")
        os.replace(saved, target)

    async def _download(self, row, target: Path) -> None:
        entity = self._entities.get(row["bot"].lower())
        if entity is None or self.client is None or not self.status["connected"]:
            raise ConnectionError("Not connected to Telegram")
        m = await self.client.get_messages(entity, ids=row["tg_message_id"])
        if m is None or m.media is None or not (m.photo or m.document):
            raise FileUnavailable("The message or its file no longer exists on Telegram")
        if m.file and m.file.size and m.file.size > self.rules.media_on_demand_max_bytes:
            raise FileTooLarge(m.file.size)
        await self._save(m, target)

    async def _ingest_message(self, bot: BotConfig, m: Message) -> None:
        if getattr(m, "action", None) is not None:  # service messages (chat created, etc.)
            return
        media_type = media_name = media_path = media_mime = None
        media_size = None
        if m.media is not None:
            media_type = "photo" if m.photo else "document" if m.document else type(m.media).__name__
            f = m.file
            if f is not None:
                media_size, media_mime = f.size, f.mime_type
            media_name = (f.name if f and f.name else None) or f"{media_type}_{m.id}{(f.ext if f else '') or ''}"
            if (self.rules.media_download and media_size is not None
                    and media_size <= self.rules.media_max_bytes and (m.photo or m.document)):
                target = self.media_target(bot.username, m.id, media_name)
                target.parent.mkdir(parents=True, exist_ok=True)
                try:
                    if not target.exists():
                        await self._save(m, target)
                    media_path = str(target)
                except Exception as exc:  # still store the message; the file can be fetched later
                    log.warning("Auto-download failed for @%s message %s: %s", bot.username, m.id, exc)

        self.ingest(
            bot=bot.username,
            tg_message_id=m.id,
            date=_iso(m.date),
            edited_at=_iso(m.edit_date),
            text=m.message or "",
            outgoing=bool(m.out),
            media_type=media_type,
            media_name=media_name,
            media_path=media_path,
            media_size=media_size,
            media_mime=media_mime,
        )
