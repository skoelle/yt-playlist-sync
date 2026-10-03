# Copyright (c) 2026 Stefan Koelle (https://stefankoelle.de)
# Licensed under the MIT License. See LICENSE file in project root for details.
"""Cron driven tasks: discovery, nightly sync, yt-dlp updates."""
from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select

from . import ytdlp
from .backup import backup_archives, backup_database
from .config import Settings
from .db import session_scope
from .discovery import finish_run, run_discovery, start_run
from .healthchecks import ping
from .jobqueue import JobQueue
from .models import Playlist

log = logging.getLogger(__name__)


class AppScheduler:
    def __init__(self, settings: Settings, queue: JobQueue) -> None:
        self.settings = settings
        self.queue = queue
        self.ytdlp_version = "unknown"
        self.scheduler = AsyncIOScheduler(timezone=settings.tzinfo)

    async def start(self) -> None:
        s = self.settings
        self.scheduler.add_job(
            self.discovery_job, CronTrigger.from_crontab(s.discovery_cron, timezone=s.tzinfo),
            id="discovery", max_instances=1, coalesce=True,
        )
        self.scheduler.add_job(
            self.sync_job, CronTrigger.from_crontab(s.sync_cron, timezone=s.tzinfo),
            id="sync", max_instances=1, coalesce=True,
        )
        self.scheduler.add_job(
            self.update_job, CronTrigger.from_crontab(s.ytdlp_update_cron, timezone=s.tzinfo),
            id="ytdlp_update", max_instances=1, coalesce=True,
        )
        self.scheduler.add_job(
            self.startup_job, "date", run_date=datetime.now(timezone.utc) + timedelta(seconds=10),
            id="startup",
        )
        self.scheduler.add_job(
            self.cleanup_logs, CronTrigger.from_crontab("15 4 * * *", timezone=s.tzinfo), id="cleanup_logs",
        )
        self.scheduler.add_job(
            self.backup_run, CronTrigger.from_crontab("30 0 * * *", timezone=s.tzinfo),
            id="backup", max_instances=1, coalesce=True,
        )
        self.scheduler.start()
        self.ytdlp_version = await ytdlp.get_version(s.ytdlp_bin, ytdlp.ytdlp_env(s.config_dir))

    async def stop(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

    def next_run(self, job_id: str) -> str | None:
        job = self.scheduler.get_job(job_id)
        nrt = getattr(job, "next_run_time", None) if job else None
        if nrt is None:
            return None
        return nrt.astimezone(timezone.utc).replace(tzinfo=None).isoformat() + "Z"

    async def startup_job(self) -> None:
        await self.update_ytdlp(wait_for_idle=False)
        await self.discovery_job()

    async def discovery_job(self) -> None:
        url = self.settings.hc_discovery_url
        await ping(url, "start")
        try:
            stats = await run_discovery(self.settings, self.queue)
            await ping(url, "success", f"new={stats.new} updated={stats.updated} removed={stats.removed}")
        except Exception as exc:  # noqa: BLE001
            log.error("discovery failed: %s", exc)
            await ping(url, "fail", str(exc))

    async def sync_job(self) -> None:
        url = self.settings.hc_sync_url
        run_id = start_run("sync")
        await ping(url, "start")
        try:
            with session_scope() as s:
                pks = list(s.scalars(
                    select(Playlist.id).where(
                        Playlist.type == "sync",
                        Playlist.ignored.is_(False),
                        Playlist.remote_status == "active",
                    )
                ).all())
            job_ids = [j for j in (self.queue.enqueue(pk, "nightly") for pk in pks) if j is not None]
            statuses = await self.queue.wait_for_jobs(job_ids)
            failed = [j for j, st in statuses.items() if st != "success"]
            msg = f"{len(job_ids)} jobs, {len(failed)} not successful"
            finish_run(run_id, "failed" if failed else "success", msg)
            await ping(url, "fail" if failed else "success", msg)
        except Exception as exc:  # noqa: BLE001
            log.exception("nightly sync failed")
            finish_run(run_id, "failed", str(exc)[:500])
            await ping(url, "fail", str(exc))

    async def update_job(self) -> None:
        await self.update_ytdlp(wait_for_idle=True)

    async def update_ytdlp(self, wait_for_idle: bool) -> None:
        s = self.settings
        if wait_for_idle:
            for _ in range(180):
                if self.queue.is_idle():
                    break
                await asyncio.sleep(60)
            else:
                log.warning("queue stayed busy, skipping yt-dlp update")
                return
        lib = s.config_dir / "ytdlp-lib"
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "pip", "install", "--upgrade", "--no-warn-script-location",
                "--target", str(lib), "yt-dlp",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=300)
            if proc.returncode != 0:
                log.warning("yt-dlp update failed: %s", out.decode(errors="replace")[-300:])
        except Exception as exc:  # noqa: BLE001
            log.warning("yt-dlp update failed: %s", exc)
        self.ytdlp_version = await ytdlp.get_version(s.ytdlp_bin, ytdlp.ytdlp_env(s.config_dir))
        log.info("yt-dlp version: %s", self.ytdlp_version)

    async def cleanup_logs(self) -> None:
        cutoff = datetime.now().timestamp() - self.settings.log_retention_days * 86400
        for f in self.settings.logs_dir.glob("*.log"):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
            except OSError:
                pass

    async def backup_run(self) -> None:
        for fn in (backup_database, backup_archives):
            try:
                path = fn(self.settings)
                if path:
                    log.info("backup written: %s", path)
            except Exception:  # noqa: BLE001
                log.exception("backup failed: %s", fn.__name__)
