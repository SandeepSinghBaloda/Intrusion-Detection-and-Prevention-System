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

from .collector import BaseCollector, TelegramCollector
from .config import BASE_DIR, Rules, Settings, load_rules, load_settings
from .db import Store
from .demo import DemoCollector
from .hub import Hub

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("monitor")


class SendBody(BaseModel):
    text: str = Field(min_length=1, max_length=4096)


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
        limit: int = Query(default=100, ge=1, le=1000),
        offset: int = Query(default=0, ge=0),
    ):
        return store.query(bots=bot or None, severities=severity or None, q=q, since=since,
                           until=until, include_outgoing=include_outgoing, limit=limit, offset=offset)

    @app.get("/api/stats", dependencies=auth)
    def get_stats():
        return store.stats()

    @app.get("/api/messages/{message_id}/file", dependencies=auth)
    def get_file(message_id: int):
        row = store.get(message_id)
        if row is None or not row["media_path"]:
            raise HTTPException(404, "No file for this message")
        path = Path(row["media_path"]).resolve()
        if not path.is_relative_to(settings.media_dir.resolve()) or not path.is_file():
            raise HTTPException(404, "File not available")
        return FileResponse(path, filename=row["media_name"] or path.name)

    @app.post("/api/bots/{username}/send", dependencies=auth)
    async def send_to_bot(username: str, body: SendBody):
        if rules.bot(username) is None:
            raise HTTPException(404, "Bot is not configured")
        try:
            await collector.send(username, body.text)
        except KeyError:
            raise HTTPException(503, "Bot is not connected")
        return {"sent": True}

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
