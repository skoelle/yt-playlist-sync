# Copyright (c) 2026 Stefan Koelle (https://stefankoelle.de)
# Licensed under the MIT License. See LICENSE file in project root for details.
"""FastAPI application factory."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import api
from .config import Settings, ensure_dirs, get_settings
from .db import init_engine, migrate
from .jobqueue import JobQueue, backfill_channels
from .scheduler import AppScheduler

log = logging.getLogger("yt-playlist-sync")
STATIC_DIR = Path(__file__).parent / "static"


def _channel_backfill(cfg: Settings) -> None:
    try:
        backfill_channels(cfg)
    except Exception:  # noqa: BLE001
        log.exception("channel backfill failed")


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
        queue.on_forbidden = scheduler.schedule_update_after_403
        queue.is_ytdlp_updating = scheduler.is_ytdlp_updating
        app.state.settings, app.state.queue, app.state.scheduler = cfg, queue, scheduler
        backfill_task: asyncio.Task | None = None
        if start_background:
            await queue.start()
            await scheduler.start()
            backfill_task = asyncio.create_task(asyncio.to_thread(_channel_backfill, cfg))
            log.info("started (channel=%s, dry_run=%s)", cfg.youtube_channel, cfg.dry_run)
        yield
        if start_background:
            if backfill_task is not None:
                backfill_task.cancel()
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
