import json
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app.config import BASE_DIR, load_rules, load_settings
from app.db import Store
from app.parser import classify, extract

RULES = load_rules(BASE_DIR / "config.yaml")


@pytest.mark.parametrize("text,expected", [
    ("🚨 CRITICAL: Intrusion detected", "critical"),
    ("Brute-force attack blocked from 1.2.3.4", "high"),
    ("Warning: suspicious scan", "medium"),
    ("Notice: cert renews soon", "low"),
    ("Heartbeat OK", "info"),
])
def test_classify(text, expected):
    assert classify(text, RULES) == expected


def test_extract():
    found = extract("Blocked 10.0.0.5 and 10.0.0.5 on port 3389, CVE-2024-3400 via evil.com", RULES)
    assert found["ip"] == ["10.0.0.5"]
    assert found["port"] == ["3389"]
    assert found["cve"] == ["CVE-2024-3400"]
    assert found["domain"] == ["evil.com"]


def test_store_upsert_is_idempotent_and_updates_edits(tmp_path):
    store = Store(tmp_path / "m.db")
    base = dict(bot="b", tg_message_id=7, date="2026-01-01T00:00:00+00:00",
                severity="info", extracted={})
    first = store.upsert(text="v1", **base)
    second = store.upsert(text="v2", edited_at="2026-01-01T00:01:00+00:00", **base)
    assert first["id"] == second["id"]
    assert second["text"] == "v2" and second["edited_at"]
    assert store.query()["total"] == 1
    assert store.last_message_id("b") == 7


def test_query_escapes_like_wildcards(tmp_path):
    store = Store(tmp_path / "m.db")
    for i, text in enumerate(["100% done", "1000 done"]):
        store.upsert(bot="b", tg_message_id=i, date=f"2026-01-01T00:00:0{i}+00:00",
                     text=text, severity="info", extracted={})
    assert [m["text"] for m in store.query(q="100%")["items"]] == ["100% done"]


@pytest.fixture
def client(tmp_path, monkeypatch):
    import random
    random.seed(7)  # deterministic demo data: includes logs, photos and an un-saved capture
    from app.main import create_app
    settings = replace(load_settings(), demo_mode=True, api_key="secret",
                       db_path=tmp_path / "m.db", media_dir=tmp_path / "media")
    with TestClient(create_app(settings)) as c:
        yield c


def test_api_requires_key(client):
    assert client.get("/api/messages").status_code == 401
    assert client.get("/api/health").status_code == 200


def test_api_demo_flow(client):
    h = {"X-API-Key": "secret"}
    cfg = client.get("/api/config", headers=h).json()
    assert [b["username"] for b in cfg["bots"]] == ["perimeterlog_bot", "TTMlogsBot"]

    msgs = client.get("/api/messages", headers=h).json()
    assert msgs["total"] >= 60  # demo seeds history

    crit = client.get("/api/messages?severity=critical&include_outgoing=false", headers=h).json()
    assert all(m["severity"] == "critical" for m in crit["items"])

    r = client.post("/api/bots/TTMlogsBot/send", headers=h, json={"text": "/status"})
    assert r.status_code == 200
    mine = client.get("/api/messages?bot=TTMlogsBot&q=/status", headers=h).json()["items"]
    assert any(m["outgoing"] and m["text"] == "/status" for m in mine)

    assert client.post("/api/bots/unknown/send", headers=h, json={"text": "x"}).status_code == 404

    stats = client.get("/api/stats", headers=h).json()
    assert len(stats["hourly_24h"]) == 24
    assert client.get("/api/status", headers=h).json()["mode"] == "demo"

    assert client.get("/", headers=h).status_code == 200  # dashboard served


def test_old_database_is_migrated(tmp_path):
    import sqlite3
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.executescript("""CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, bot TEXT NOT NULL,
        tg_message_id INTEGER NOT NULL, date TEXT NOT NULL, edited_at TEXT, outgoing INTEGER NOT NULL DEFAULT 0,
        text TEXT NOT NULL DEFAULT '', severity TEXT NOT NULL, extracted TEXT NOT NULL DEFAULT '{}',
        media_type TEXT, media_name TEXT, media_path TEXT, received_at TEXT NOT NULL,
        UNIQUE (bot, tg_message_id));""")
    conn.close()
    rec = Store(db).upsert(bot="b", tg_message_id=1, date="2026-01-01T00:00:00+00:00", text="x",
                           severity="info", extracted={}, media_type="document", media_name="a.log",
                           media_size=10, media_mime="text/plain")
    assert rec["media_size"] == 10 and rec["media_mime"] == "text/plain"


def test_files_listing_download_and_on_demand_fetch(client):
    h = {"X-API-Key": "secret"}
    files = client.get("/api/messages?media_only=true&limit=1000", headers=h).json()["items"]
    assert files and all(f["media_type"] for f in files)
    assert client.get("/api/stats", headers=h).json()["files"]["total"] == len(files)

    saved = next(f for f in files if f["has_file"] and f["media_name"].endswith(".log"))
    r = client.get(f"/api/messages/{saved['id']}/file", headers=h)
    assert r.status_code == 200 and r.content.count(b"\n") == 40
    assert "attachment" in r.headers["content-disposition"]
    assert r.headers["content-type"] == "application/octet-stream"
    assert r.headers["x-content-type-options"] == "nosniff"
    # inline is refused for non-images: still served as a download
    r = client.get(f"/api/messages/{saved['id']}/file?inline=true", headers=h)
    assert "attachment" in r.headers["content-disposition"]

    photo = next(f for f in files if f["media_type"] == "photo")
    r = client.get(f"/api/messages/{photo['id']}/file?inline=true", headers=h)
    assert r.headers["content-type"] == "image/png" and r.content.startswith(b"\x89PNG")
    assert r.headers["content-disposition"].startswith("inline")

    big = next(f for f in files if not f["has_file"])
    rec = client.post(f"/api/messages/{big['id']}/fetch", headers=h).json()
    assert rec["has_file"] and rec["id"] == big["id"]
    r = client.get(f"/api/messages/{big['id']}/file", headers=h)
    assert r.status_code == 200 and r.content.startswith(b"DEMO packet capture")

    plain = client.get("/api/messages?q=Heartbeat", headers=h).json()["items"]
    no_file = next(m for m in plain if not m["media_type"])
    assert client.get(f"/api/messages/{no_file['id']}/file", headers=h).status_code == 404


def test_on_demand_limit(tmp_path):
    from app.main import create_app
    settings = replace(load_settings(), demo_mode=True, api_key=None,
                       db_path=tmp_path / "m.db", media_dir=tmp_path / "media")
    app = create_app(settings)
    with TestClient(app) as c:
        store = app.state.store
        rec = store.upsert(bot="TTMlogsBot", tg_message_id=999999, date="2026-01-01T00:00:00+00:00",
                           text="huge", severity="info", extracted={}, media_type="document",
                           media_name="huge.pcap", media_size=10 * 1024 ** 3)
        r = c.post(f"/api/messages/{rec['id']}/fetch")
        assert r.status_code == 413 and "limit" in r.json()["detail"]
