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
