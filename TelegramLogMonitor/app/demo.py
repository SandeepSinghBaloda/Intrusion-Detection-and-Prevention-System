"""DEMO_MODE collector: emits fake bot messages so the dashboard can be tried without Telegram."""
from __future__ import annotations

import asyncio
import random
import re
import struct
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .collector import BaseCollector, FileUnavailable, _iso

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


def _png(width: int = 160, height: int = 90) -> bytes:
    """A tiny bar-chart PNG built without image libraries (stands in for a bot's screenshot)."""
    heights = [random.randint(10, height - 10) for _ in range(8)]
    rows = []
    for y in range(height):
        row = bytearray([0])
        for x in range(width):
            bar = x * len(heights) // width
            on = (x % (width // len(heights))) > 3 and height - y <= heights[bar]
            row += bytes((37, 99, 235) if on else (240, 242, 245))
        rows.append(bytes(row))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"".join(rows))) + chunk(b"IEND", b""))


def _log_file() -> bytes:
    now = datetime.now(timezone.utc)
    lines = [f"{(now - timedelta(seconds=i * 37)).isoformat(timespec='seconds')} {_sample()}" for i in range(40)]
    return ("\n".join(reversed(lines)) + "\n").encode()


def _sample() -> str:
    ip = f"{random.randint(11, 223)}.{random.randint(0, 255)}.{random.randint(0, 255)}.{random.randint(1, 254)}"
    return random.choice(SAMPLES).format(ip=ip, port=random.choice([22, 80, 443, 3389, 8080]))


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", name)[:60] or "results"


def _search_demo_file(choice: str) -> bytes:
    """Fake, clearly-placeholder result lines in the chosen format (for DEMO_MODE only)."""
    rows = []
    for i in range(1, 26):
        user, pw = f"user{i}@example.com", f"ExamplePass-{i:03d}"
        if "url" in choice:
            rows.append(f"https://portal{i}.example.com:{user}:{pw}")
        else:
            rows.append(f"{user}:{pw}")
    header = f"# DEMO results ({choice}) - placeholder data, example.com only\n"
    return (header + "\n".join(rows) + "\n").encode()


class DemoCollector(BaseCollector):
    def __init__(self, *args, interval: float = 4.0, **kwargs):
        super().__init__(*args, **kwargs)
        self.status.update(mode="demo", bots={b.username: {"ok": True, "error": None} for b in self.rules.bots})
        self._interval = interval
        self._task: asyncio.Task | None = None
        self._next_id = {b.username: (self.store.last_message_id(b.username) or 0) + 1 for b in self.rules.bots}

    def _emit(self, bot: str, text: str, outgoing: bool = False, when: datetime | None = None,
              **media) -> dict:
        msg_id = self._next_id[bot]
        self._next_id[bot] += 1
        return self.ingest(bot=bot, tg_message_id=msg_id, date=_iso(when or datetime.now(timezone.utc)),
                           text=text, outgoing=outgoing, **media)

    def _emit_file(self, bot: str, when: datetime | None = None) -> dict:
        """Sends a fake file: a small log (auto-saved), a chart photo, or a big capture (fetch on click)."""
        msg_id = self._next_id[bot]
        stamp = (when or datetime.now(timezone.utc)).strftime("%Y%m%d_%H%M")
        kind = random.choice(["log", "log", "photo", "capture"])
        if kind == "capture":  # bigger than the auto-download limit -> stays on "Telegram"
            return self._emit(bot, "Full packet capture attached for the blocked attack", when=when,
                              media_type="document", media_name=f"capture_{stamp}.pcap",
                              media_size=self.rules.media_max_bytes + 3 * 1024 * 1024,
                              media_mime="application/vnd.tcpdump.pcap")
        if kind == "photo":
            name, data, mime, text, mtype = f"photo_{msg_id}.png", _png(), "image/png", "Traffic graph (last hour)", "photo"
        else:
            name, data, mime, text, mtype = f"firewall_{stamp}.log", _log_file(), "text/plain", "Firewall log export", "document"
        target = self.media_target(bot, msg_id, name)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return self._emit(bot, text, when=when, media_type=mtype, media_name=name, media_path=str(target),
                          media_size=len(data), media_mime=mime)

    async def _download(self, row, target: Path) -> None:
        if not row["media_name"].endswith(".pcap"):
            raise FileUnavailable("Demo file is missing")
        await asyncio.sleep(1.5)  # pretend to pull it from Telegram
        target.write_bytes(b"DEMO packet capture - in real mode this is the file the bot sent.\n" * 200)

    async def start(self) -> None:
        if not any(self.store.last_message_id(b.username) for b in self.rules.bots):
            now = datetime.now(timezone.utc)
            seeds = sorted(random.randint(1, 24 * 60) for _ in range(72))[::-1]
            for i, minutes in enumerate(seeds):  # seed a day of history, oldest first
                bot = random.choice(self.rules.bots).username
                when = now - timedelta(minutes=minutes)
                if i % 6 == 0:
                    self._emit_file(bot, when)
                else:
                    self._emit(bot, _sample(), when=when)
        self.status["connected"] = True
        self._task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval * random.uniform(0.5, 1.5))
            bot = random.choice(self.rules.bots).username
            if random.random() < 0.15:
                self._emit_file(bot)
            else:
                self._emit(bot, _sample())

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

    async def run_search(self, bot: str, command_text: str, *, button: str | None = None,
                         suffix: str = "", timeout: int = 60) -> dict:
        cfg = self.rules.bot(bot)
        if cfg is None:
            raise KeyError(bot)
        self._emit(cfg.username, command_text + (suffix if suffix and not button else ""), outgoing=True)
        await asyncio.sleep(1.0)
        choice = button or suffix.strip() or "results"
        name = f"search_{_safe(choice)}_{datetime.now(timezone.utc).strftime('%H%M%S')}.txt"
        data = _search_demo_file(choice)
        target = self.media_target(cfg.username, self._next_id[cfg.username], name)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        record = self._emit(cfg.username, f"Results for your search ({choice})", media_type="document",
                            media_name=name, media_path=str(target), media_size=len(data), media_mime="text/plain")
        return {"clicked": bool(button), "file": record}
