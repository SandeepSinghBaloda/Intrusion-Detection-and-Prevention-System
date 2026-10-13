"""DEMO_MODE collector: emits fake bot messages so the dashboard can be tried without Telegram."""
from __future__ import annotations

import asyncio
import random
from datetime import datetime, timedelta, timezone

from .collector import BaseCollector, _iso

SAMPLES = [
    "🚨 CRITICAL: Intrusion detected from {ip} on port {port} - session terminated",
    "Brute-force attack blocked: 25 failed SSH logins from {ip}",
    "⚠ Warning: suspicious port scan from {ip} (ports 20-1024)",
    "Firewall denied inbound connection {ip}:{port}",
    "Login failed for user admin from {ip}",
    "Notice: certificate for portal.example.com renews in 14 days",
    "Heartbeat OK - perimeter sensors online",
    "High: CVE-2024-3400 exploitation attempt from {ip}",
    "Backup completed successfully",
]


def _sample() -> str:
    ip = f"{random.randint(11, 223)}.{random.randint(0, 255)}.{random.randint(0, 255)}.{random.randint(1, 254)}"
    return random.choice(SAMPLES).format(ip=ip, port=random.choice([22, 80, 443, 3389, 8080]))


class DemoCollector(BaseCollector):
    def __init__(self, *args, interval: float = 4.0, **kwargs):
        super().__init__(*args, **kwargs)
        self.status.update(mode="demo", bots={b.username: {"ok": True, "error": None} for b in self.rules.bots})
        self._interval = interval
        self._task: asyncio.Task | None = None
        self._next_id = {b.username: (self.store.last_message_id(b.username) or 0) + 1 for b in self.rules.bots}

    def _emit(self, bot: str, text: str, outgoing: bool = False, when: datetime | None = None) -> dict:
        msg_id = self._next_id[bot]
        self._next_id[bot] += 1
        return self.ingest(bot=bot, tg_message_id=msg_id, date=_iso(when or datetime.now(timezone.utc)),
                           text=text, outgoing=outgoing)

    async def start(self) -> None:
        if not any(self.store.last_message_id(b.username) for b in self.rules.bots):
            now = datetime.now(timezone.utc)
            for _ in range(60):  # seed a day of history
                bot = random.choice(self.rules.bots).username
                self._emit(bot, _sample(), when=now - timedelta(minutes=random.randint(1, 24 * 60)))
        self.status["connected"] = True
        self._task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval * random.uniform(0.5, 1.5))
            self._emit(random.choice(self.rules.bots).username, _sample())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        self.status["connected"] = False

    async def send(self, bot: str, text: str) -> None:
        cfg = self.rules.bot(bot)
        if cfg is None:
            raise KeyError(bot)
        self._emit(cfg.username, text, outgoing=True)
        await asyncio.sleep(0.3)
        self._emit(cfg.username, f"(demo) received command: {text}")
