"""Background collector: a Telegram *user* client that reads the configured bots' chats.

The Bot API cannot read another owner's bot, so this logs in as your own Telegram
account (MTProto via Telethon) and records what the bots send you, exactly as the
Telegram app on your phone would see it.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from telethon import TelegramClient, events
from telethon.tl.custom.message import Message

from .config import BotConfig, Rules, Settings
from .db import Store
from .hub import Hub
from .parser import classify, extract

log = logging.getLogger(__name__)


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", name)[:100] or "file"


class BaseCollector:
    """Shared ingest path used by the real and the demo collector."""

    def __init__(self, settings: Settings, rules: Rules, store: Store, hub: Hub):
        self.settings = settings
        self.rules = rules
        self.store = store
        self.hub = hub
        self.status: dict = {"mode": "unknown", "connected": False, "error": None, "bots": {}}

    def ingest(self, *, bot: str, tg_message_id: int, date: str, text: str, outgoing: bool,
               edited_at: str | None = None, media_type: str | None = None,
               media_name: str | None = None, media_path: str | None = None) -> dict:
        # Only the bot's own messages are classified; commands we send are stored as 'info'.
        severity = self.rules.default_severity if outgoing else classify(text, self.rules)
        record = self.store.upsert(
            bot=bot, tg_message_id=tg_message_id, date=date, text=text, severity=severity,
            extracted={} if outgoing else extract(text, self.rules), outgoing=outgoing,
            edited_at=edited_at, media_type=media_type, media_name=media_name, media_path=media_path)
        self.hub.publish({"type": "message", "data": record})
        return record

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def send(self, bot: str, text: str) -> None: ...


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

    async def _ingest_message(self, bot: BotConfig, m: Message) -> None:
        if getattr(m, "action", None) is not None:  # service messages (chat created, etc.)
            return
        media_type = media_name = media_path = None
        if m.media is not None:
            media_type = "photo" if m.photo else "document" if m.document else type(m.media).__name__
            f = m.file
            media_name = (f.name if f and f.name else None) or f"{media_type}{(f.ext if f else '') or ''}"
            if (self.rules.media_download and f is not None and f.size is not None
                    and f.size <= self.rules.media_max_bytes and (m.photo or m.document)):
                target = self.settings.media_dir / f"{_safe_name(bot.username)}_{m.id}_{_safe_name(media_name)}"
                target.parent.mkdir(parents=True, exist_ok=True)
                if not target.exists():
                    saved = await m.download_media(file=str(target))
                    target = Path(saved) if saved else None
                media_path = str(target) if target else None

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
        )
