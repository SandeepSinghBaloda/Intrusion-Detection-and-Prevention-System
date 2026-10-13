"""SQLite storage for bot messages."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    bot           TEXT    NOT NULL,
    tg_message_id INTEGER NOT NULL,
    date          TEXT    NOT NULL,            -- ISO-8601 UTC, when Telegram sent it
    edited_at     TEXT,
    outgoing      INTEGER NOT NULL DEFAULT 0,  -- 1 = command we sent to the bot
    text          TEXT    NOT NULL DEFAULT '',
    severity      TEXT    NOT NULL,
    extracted     TEXT    NOT NULL DEFAULT '{}',
    media_type    TEXT,
    media_name    TEXT,
    media_path    TEXT,
    media_size    INTEGER,                     -- bytes, as reported by Telegram
    media_mime    TEXT,
    received_at   TEXT    NOT NULL,
    UNIQUE (bot, tg_message_id)
);
CREATE INDEX IF NOT EXISTS ix_messages_date     ON messages (date);
CREATE INDEX IF NOT EXISTS ix_messages_bot_date ON messages (bot, date);
CREATE INDEX IF NOT EXISTS ix_messages_severity ON messages (severity);
"""

# Columns added after the first release; created on startup for existing databases.
MIGRATIONS = {"media_size": "INTEGER", "media_mime": "TEXT"}

PUBLIC_COLUMNS = ("id", "bot", "tg_message_id", "date", "edited_at", "outgoing", "text",
                  "severity", "extracted", "media_type", "media_name", "media_path",
                  "media_size", "media_mime")


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _row(row: sqlite3.Row) -> dict:
    d = {k: row[k] for k in PUBLIC_COLUMNS}
    d["outgoing"] = bool(d["outgoing"])
    d["extracted"] = json.loads(d["extracted"] or "{}")
    d["has_file"] = bool(d.pop("media_path"))
    return d


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        existing = {r["name"] for r in self._conn.execute("PRAGMA table_info(messages)")}
        for column, kind in MIGRATIONS.items():
            if column not in existing:
                self._conn.execute(f"ALTER TABLE messages ADD COLUMN {column} {kind}")
        self._conn.commit()
        self._lock = threading.Lock()

    def close(self) -> None:
        self._conn.close()

    def upsert(self, *, bot: str, tg_message_id: int, date: str, text: str, severity: str,
               extracted: dict, outgoing: bool = False, edited_at: str | None = None,
               media_type: str | None = None, media_name: str | None = None,
               media_path: str | None = None, media_size: int | None = None,
               media_mime: str | None = None) -> dict:
        with self._lock, self._conn:
            row = self._conn.execute(
                """
                INSERT INTO messages (bot, tg_message_id, date, edited_at, outgoing, text, severity,
                                      extracted, media_type, media_name, media_path,
                                      media_size, media_mime, received_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (bot, tg_message_id) DO UPDATE SET
                    text       = excluded.text,
                    severity   = excluded.severity,
                    extracted  = excluded.extracted,
                    edited_at  = COALESCE(excluded.edited_at, messages.edited_at),
                    media_type = COALESCE(excluded.media_type, messages.media_type),
                    media_name = COALESCE(excluded.media_name, messages.media_name),
                    media_path = COALESCE(excluded.media_path, messages.media_path),
                    media_size = COALESCE(excluded.media_size, messages.media_size),
                    media_mime = COALESCE(excluded.media_mime, messages.media_mime)
                RETURNING *
                """,
                (bot, tg_message_id, date, edited_at, int(outgoing), text, severity,
                 json.dumps(extracted), media_type, media_name, media_path,
                 media_size, media_mime, utcnow()),
            ).fetchone()
        return _row(row)

    def last_message_id(self, bot: str) -> int | None:
        row = self._conn.execute(
            "SELECT MAX(tg_message_id) FROM messages WHERE bot = ?", (bot,)).fetchone()
        return row[0]

    def get(self, message_id: int) -> sqlite3.Row | None:
        return self._conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()

    def record(self, message_id: int) -> dict | None:
        row = self.get(message_id)
        return _row(row) if row else None

    def record_by_tg(self, bot: str, tg_message_id: int) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM messages WHERE bot = ? AND tg_message_id = ?", (bot, tg_message_id)).fetchone()
        return _row(row) if row else None

    def set_media_path(self, message_id: int, path: str) -> dict:
        with self._lock, self._conn:
            row = self._conn.execute(
                "UPDATE messages SET media_path = ? WHERE id = ? RETURNING *", (path, message_id)).fetchone()
        return _row(row)

    def query(self, *, bots: list[str] | None = None, severities: list[str] | None = None,
              q: str | None = None, since: str | None = None, until: str | None = None,
              include_outgoing: bool = True, media_only: bool = False,
              limit: int = 100, offset: int = 0) -> dict:
        where, args = [], []
        if bots:
            where.append(f"bot IN ({','.join('?' * len(bots))})")
            args += bots
        if severities:
            where.append(f"severity IN ({','.join('?' * len(severities))})")
            args += severities
        if q:
            where.append("(text LIKE ? ESCAPE '\\' OR extracted LIKE ? ESCAPE '\\' "
                         "OR media_name LIKE ? ESCAPE '\\')")
            like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            args += [like, like, like]
        if since:
            where.append("date >= ?")
            args.append(since)
        if until:
            where.append("date <= ?")
            args.append(until)
        if not include_outgoing:
            where.append("outgoing = 0")
        if media_only:
            where.append("media_type IS NOT NULL")
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        total = self._conn.execute(f"SELECT COUNT(*) FROM messages {clause}", args).fetchone()[0]
        rows = self._conn.execute(
            f"SELECT * FROM messages {clause} ORDER BY date DESC, id DESC LIMIT ? OFFSET ?",
            args + [limit, offset]).fetchall()
        return {"total": total, "items": [_row(r) for r in rows]}

    def stats(self) -> dict:
        now = datetime.now(timezone.utc)
        day_ago = (now - timedelta(hours=24)).isoformat(timespec="seconds")
        c = self._conn
        per_bot = {r["bot"]: {"total": r["n"], "last_message_at": r["last"]} for r in c.execute(
            "SELECT bot, COUNT(*) n, MAX(date) last FROM messages WHERE outgoing = 0 GROUP BY bot")}
        per_sev = {r["severity"]: r["n"] for r in c.execute(
            "SELECT severity, COUNT(*) n FROM messages WHERE outgoing = 0 AND date >= ? "
            "GROUP BY severity", (day_ago,))}
        hourly = {r["h"]: r["n"] for r in c.execute(
            "SELECT substr(date, 1, 13) h, COUNT(*) n FROM messages "
            "WHERE outgoing = 0 AND date >= ? GROUP BY h", (day_ago,))}
        buckets = []
        for i in range(23, -1, -1):
            h = (now - timedelta(hours=i)).strftime("%Y-%m-%dT%H")
            buckets.append({"hour": h + ":00:00+00:00", "count": hourly.get(h, 0)})
        files = c.execute("SELECT COUNT(*) n, COUNT(media_path) saved FROM messages "
                          "WHERE media_type IS NOT NULL AND outgoing = 0").fetchone()
        return {
            "files": {"total": files["n"], "saved": files["saved"]},
            "total": sum(b["total"] for b in per_bot.values()),
            "last_24h": sum(per_sev.values()),
            "by_bot": per_bot,
            "by_severity_24h": per_sev,
            "hourly_24h": buckets,
        }

    def purge_older_than(self, days: int) -> list[str]:
        """Deletes old rows; returns media paths that should be removed from disk."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
        with self._lock, self._conn:
            paths = [r[0] for r in self._conn.execute(
                "SELECT media_path FROM messages WHERE date < ? AND media_path IS NOT NULL", (cutoff,))]
            self._conn.execute("DELETE FROM messages WHERE date < ?", (cutoff,))
        return paths
