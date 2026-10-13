"""FastAPI app: starts the collector in the background and serves the API + dashboard.

    uvicorn app.main:app            (or)            python -m app.main
"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .collector import (
    BaseCollector,
    FileTooLarge,
    FileUnavailable,
    SearchChoiceUnavailable,
    SearchNoReply,
    TelegramCollector,
)
from .config import BASE_DIR, Rules, Settings, load_rules, load_settings
from .db import Store
from .demo import DemoCollector
from .hub import Hub

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("monitor")


SAFE_INLINE_IMAGES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
FILE_HEADERS = {"X-Content-Type-Options": "nosniff", "Content-Security-Policy": "sandbox"}


class SendBody(BaseModel):
    text: str = Field(min_length=1, max_length=4096)


class SearchBody(BaseModel):
    text: str = Field(min_length=1, max_length=4096)
    format: str | None = None


class DeleteBody(BaseModel):
    bot: list[str] = Field(default_factory=list)
    severity: list[str] = Field(default_factory=list)
    q: str | None = None
    include_outgoing: bool = True
    media_only: bool = False
    older_than_days: int | None = None  # None/absent = delete all that match


async def _purge_loop(store: Store, rules: Rules) -> None:
    while True:
        for path in store.purge_older_than(rules.retention_days):
            Path(path).unlink(missing_ok=True)
        await asyncio.sleep(rules.purge_interval_minutes * 60)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    rules = load_rules(settings.config_path)
    store = Store(settings.db_path)
    hub = Hub()
    cls = DemoCollector if settings.demo_mode else TelegramCollector
    collector: BaseCollector = cls(settings, rules, store, hub)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        try:
            await collector.start()
        except Exception as exc:  # keep serving stored data + show the error in the UI
            log.error("Collector failed to start: %s", exc)
            collector.status["error"] = str(exc)
        purge = asyncio.create_task(_purge_loop(store, rules)) if rules.retention_days > 0 else None
        yield
        if purge:
            purge.cancel()
        await collector.stop()
        store.close()

    app = FastAPI(title="Telegram Log Monitor", lifespan=lifespan)
    app.state.settings, app.state.rules, app.state.store = settings, rules, store
    app.state.hub, app.state.collector = hub, collector

    def require_key(request: Request) -> None:
        if not settings.api_key:
            return
        supplied = request.headers.get("x-api-key") or request.query_params.get("key") or ""
        if not secrets.compare_digest(supplied, settings.api_key):
            raise HTTPException(401, "Invalid or missing API key")

    auth = [Depends(require_key)]

    @app.get("/api/health")
    def health():
        return {"ok": True}

    @app.get("/api/config", dependencies=auth)
    def get_config():
        return rules.public()

    @app.get("/api/status", dependencies=auth)
    def get_status():
        return {**collector.status, "live_clients": hub.subscriber_count}

    @app.get("/api/messages", dependencies=auth)
    def list_messages(
        bot: list[str] = Query(default=[]),
        severity: list[str] = Query(default=[]),
        q: str | None = None,
        since: str | None = None,
        until: str | None = None,
        include_outgoing: bool = True,
        media_only: bool = False,
        limit: int = Query(default=100, ge=1, le=1000),
        offset: int = Query(default=0, ge=0),
    ):
        return store.query(bots=bot or None, severities=severity or None, q=q, since=since,
                           until=until, include_outgoing=include_outgoing, media_only=media_only,
                           limit=limit, offset=offset)

    @app.get("/api/stats", dependencies=auth)
    def get_stats():
        return store.stats()

    def saved_path(row) -> Path | None:
        if not row["media_path"]:
            return None
        path = Path(row["media_path"]).resolve()
        if path.is_relative_to(settings.media_dir.resolve()) and path.is_file():
            return path
        return None

    async def ensure_saved(message_id: int):
        """Returns the message row with its file on disk, fetching it from Telegram if needed."""
        row = store.get(message_id)
        if row is None or not row["media_type"]:
            raise HTTPException(404, "This message has no file")
        if saved_path(row):
            return row
        try:
            await collector.fetch_media(row)
        except FileTooLarge as exc:
            limit_mb = rules.media_on_demand_max_bytes // (1024 * 1024)
            raise HTTPException(413, f"File is {int(exc.args[0]) // (1024 * 1024)} MB; the limit is "
                                     f"{limit_mb} MB (media.on_demand_max_mb in config.yaml)")
        except FileUnavailable as exc:
            raise HTTPException(404, str(exc))
        except ConnectionError as exc:
            raise HTTPException(503, str(exc))
        return store.get(message_id)

    @app.post("/api/messages/{message_id}/fetch", dependencies=auth)
    async def fetch_file(message_id: int):
        await ensure_saved(message_id)
        return store.record(message_id)

    @app.get("/api/messages/{message_id}/file", dependencies=auth)
    async def get_file(message_id: int, inline: bool = False):
        row = await ensure_saved(message_id)
        path = saved_path(row)
        if path is None:
            raise HTTPException(404, "File not available")
        name = row["media_name"] or path.name
        # Only photos may render in the browser; everything else is forced to download so a
        # bot-supplied HTML/SVG/JS file can never execute in the dashboard's origin.
        mime = (row["media_mime"] or "").lower()
        is_image = row["media_type"] == "photo" or mime in SAFE_INLINE_IMAGES
        if inline and is_image and rules.media_preview_photos:
            return FileResponse(path, filename=name, content_disposition_type="inline",
                                media_type=mime if mime in SAFE_INLINE_IMAGES else "image/jpeg",
                                headers=FILE_HEADERS)
        return FileResponse(path, filename=name, media_type="application/octet-stream",
                            headers=FILE_HEADERS)

    @app.post("/api/bots/{username}/send", dependencies=auth)
    async def send_to_bot(username: str, body: SendBody):
        if rules.bot(username) is None:
            raise HTTPException(404, "Bot is not configured")
        try:
            await collector.send(username, body.text)
        except KeyError:
            raise HTTPException(503, "Bot is not connected")
        return {"sent": True}

    @app.post("/api/bots/{username}/search", dependencies=auth)
    async def search_bot(username: str, body: SearchBody):
        """Runs a search command end to end: send it, pick the chosen format button the bot
        offers, and fetch the file it sends back."""
        if rules.bot(username) is None:
            raise HTTPException(404, "Bot is not configured")
        formats = rules.search.get("formats", [])
        fmt = next((f for f in formats if f["id"] == body.format), None)
        if fmt is None and formats:  # fall back to the configured default
            fmt = next((f for f in formats if f["id"] == rules.search.get("default_format")), None)
        try:
            result = await collector.run_search(
                username, body.text,
                button=(fmt or {}).get("button") or None,
                suffix=(fmt or {}).get("suffix") or "",
                timeout=int(rules.search.get("reply_timeout_seconds", 90)))
        except KeyError:
            raise HTTPException(503, "Bot is not connected")
        except ConnectionError as exc:
            raise HTTPException(503, str(exc))
        except SearchChoiceUnavailable as exc:
            raise HTTPException(409, str(exc))
        except SearchNoReply as exc:
            raise HTTPException(504, str(exc))
        return result

    @app.delete("/api/messages/{message_id}", dependencies=auth)
    def delete_message(message_id: int):
        path, existed = store.delete_message(message_id)
        if not existed:
            raise HTTPException(404, "No such message")
        if path:
            Path(path).unlink(missing_ok=True)
        hub.publish({"type": "deleted", "data": {"ids": [message_id]}})
        return {"deleted": 1}

    @app.post("/api/messages/delete", dependencies=auth)
    def delete_messages(body: DeleteBody):
        ids, paths = store.delete_where(
            bots=body.bot or None, severities=body.severity or None, q=body.q,
            include_outgoing=body.include_outgoing, media_only=body.media_only,
            older_than_days=body.older_than_days)
        for p in paths:
            Path(p).unlink(missing_ok=True)
        if ids:
            # Too many ids to stream individually; tell live clients to reload.
            hub.publish({"type": "reload", "data": {}})
        return {"deleted": len(ids)}

    @app.get("/api/stream", dependencies=auth)
    async def stream(request: Request):
        q = hub.subscribe()

        async def events():
            try:
                yield "retry: 3000\n\n"
                while not await request.is_disconnected():
                    try:
                        item = await asyncio.wait_for(q.get(), timeout=15)
                    except asyncio.TimeoutError:
                        yield ": keep-alive\n\n"
                        continue
                    yield f"event: {item['type']}\ndata: {json.dumps(item['data'])}\n\n"
            finally:
                hub.unsubscribe(q)

        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    app.mount("/", StaticFiles(directory=BASE_DIR / "static", html=True), name="static")
    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn

    s = app.state.settings
    uvicorn.run(app, host=s.host, port=s.port)
