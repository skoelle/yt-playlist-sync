"""FastAPI application factory."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import api
from .config import Settings, ensure_dirs, get_settings
from .db import init_engine, migrate
from .jobqueue import JobQueue
from .scheduler import AppScheduler

log = logging.getLogger("yt-playlist-sync")
STATIC_DIR = Path(__file__).parent / "static"


def create_app(settings: Settings | None = None, start_background: bool = True) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        cfg = settings or get_settings()
        logging.basicConfig(
            level=cfg.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s"
        )
        ensure_dirs(cfg)
        init_engine(cfg.db_url)
        migrate(cfg.db_url)
        if not cfg.data_writable():
            log.error("DATA_DIR %s is not writable, downloads will be paused", cfg.data_dir)
        queue = JobQueue(cfg)
        scheduler = AppScheduler(cfg, queue)
        app.state.settings, app.state.queue, app.state.scheduler = cfg, queue, scheduler
        if start_background:
            await queue.start()
            await scheduler.start()
            log.info("started (channel=%s, dry_run=%s)", cfg.youtube_channel, cfg.dry_run)
        yield
        if start_background:
            await scheduler.stop()
            await queue.stop()

    app = FastAPI(title="yt-playlist-sync", lifespan=lifespan)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(api.router)
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app


app = create_app()
