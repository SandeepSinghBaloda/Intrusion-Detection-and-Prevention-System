"""Loads deployment settings (.env) and business rules (config.yaml)."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent


def _bool(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _path(value: str) -> Path:
    p = Path(value)
    return p if p.is_absolute() else BASE_DIR / p


@dataclass(frozen=True)
class Settings:
    api_id: int | None
    api_hash: str | None
    session_path: Path
    host: str
    port: int
    api_key: str | None
    db_path: Path
    media_dir: Path
    config_path: Path
    demo_mode: bool


def load_settings() -> Settings:
    load_dotenv(BASE_DIR / ".env", encoding="utf-8-sig")  # tolerate a BOM from Windows editors
    api_id = os.getenv("TG_API_ID", "").strip()
    return Settings(
        api_id=int(api_id) if api_id else None,
        api_hash=os.getenv("TG_API_HASH", "").strip() or None,
        session_path=_path(os.getenv("TG_SESSION", "data/monitor")),
        host=os.getenv("APP_HOST", "127.0.0.1"),
        port=int(os.getenv("APP_PORT", "8000")),
        api_key=os.getenv("APP_API_KEY", "").strip() or None,
        db_path=_path(os.getenv("DB_PATH", "data/messages.db")),
        media_dir=_path(os.getenv("MEDIA_DIR", "data/media")),
        config_path=_path(os.getenv("CONFIG_PATH", "config.yaml")),
        demo_mode=_bool(os.getenv("DEMO_MODE")),
    )


@dataclass(frozen=True)
class BotConfig:
    username: str
    label: str
    backfill_limit: int
    start_command: str | None


@dataclass(frozen=True)
class SeverityLevel:
    name: str
    rank: int
    color: str
    patterns: tuple[re.Pattern, ...]


@dataclass(frozen=True)
class Rules:
    bots: tuple[BotConfig, ...]
    default_severity: str
    levels: tuple[SeverityLevel, ...]          # sorted highest rank first
    extractors: dict[str, re.Pattern]
    media_download: bool
    media_max_bytes: int              # auto-download limit
    media_on_demand_max_bytes: int    # limit for fetching from Telegram when the user clicks Download
    media_preview_photos: bool
    retention_days: int
    purge_interval_minutes: int
    search: dict = field(default_factory=dict)
    ui: dict = field(default_factory=dict)

    def bot(self, username: str) -> BotConfig | None:
        username = username.lower()
        return next((b for b in self.bots if b.username.lower() == username), None)

    def public(self) -> dict:
        """The subset of config the dashboard needs (no regexes)."""
        return {
            "bots": [{"username": b.username, "label": b.label} for b in self.bots],
            "severities": [{"name": l.name, "rank": l.rank, "color": l.color} for l in self.levels],
            "extractors": list(self.extractors),
            "media": {
                "auto_download_max_bytes": self.media_max_bytes if self.media_download else 0,
                "on_demand_max_bytes": self.media_on_demand_max_bytes,
                "preview_photos": self.media_preview_photos,
            },
            "search": self.search,
            "ui": self.ui,
        }


def load_rules(path: Path) -> Rules:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    bots = tuple(
        BotConfig(
            username=str(b["username"]).lstrip("@"),
            label=str(b.get("label") or b["username"]),
            backfill_limit=int(b.get("backfill_limit", 200)),
            start_command=b.get("start_command") or None,
        )
        for b in raw.get("bots", [])
    )
    if not bots:
        raise ValueError(f"No bots configured in {path}")

    sev = raw.get("severity", {})
    levels = tuple(sorted(
        (
            SeverityLevel(
                name=str(l["name"]),
                rank=int(l["rank"]),
                color=str(l.get("color", "#5f6368")),
                patterns=tuple(re.compile(p, re.IGNORECASE) for p in l.get("patterns") or []),
            )
            for l in sev.get("levels", [])
        ),
        key=lambda l: l.rank,
        reverse=True,
    ))
    default = str(sev.get("default", "info"))
    if levels and default not in {l.name for l in levels}:
        raise ValueError(f"severity.default '{default}' is not one of the configured levels")

    media = raw.get("media", {})
    retention = raw.get("retention", {})

    raw_search = raw.get("search") or {}
    formats = [
        {
            "id": str(f["id"]),
            "label": str(f.get("label") or f["id"]),
            "suffix": str(f.get("suffix") or ""),
            "description": str(f.get("description") or ""),
        }
        for f in raw_search.get("formats") or []
    ]
    search = {
        "trigger_commands": [str(c).lower() for c in raw_search.get("trigger_commands") or ["/search"]],
        "default_format": str(raw_search.get("default_format") or (formats[0]["id"] if formats else "")),
        "formats": formats,
    }
    if formats and search["default_format"] not in {f["id"] for f in formats}:
        raise ValueError(f"search.default_format '{search['default_format']}' is not a configured format")

    return Rules(
        bots=bots,
        default_severity=default,
        levels=levels,
        extractors={k: re.compile(v, re.IGNORECASE) for k, v in (raw.get("extractors") or {}).items()},
        media_download=bool(media.get("download", True)),
        media_max_bytes=int(float(media.get("max_size_mb", 20)) * 1024 * 1024),
        media_on_demand_max_bytes=int(float(media.get("on_demand_max_mb", 500)) * 1024 * 1024),
        media_preview_photos=bool(media.get("preview_photos", True)),
        retention_days=int(retention.get("days", 0)),
        purge_interval_minutes=int(retention.get("purge_interval_minutes", 60)),
        search=search,
        ui=dict(raw.get("ui") or {}),
    )
