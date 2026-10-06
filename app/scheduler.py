# Copyright (c) 2026 Stefan Koelle (https://stefankoelle.de)
# Licensed under the MIT License. See LICENSE file in project root for details.
"""Scheduled tasks: discovery (random interval), nightly sync, yt-dlp updates, backups."""
from __future__ import annotations

import asyncio
import logging
import random
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
_background: set[asyncio.Task] = set()


class AppScheduler:
    def __init__(self, settings: Settings, queue: JobQueue) -> None:
        self.settings = settings
        self.queue = queue
        self.ytdlp_version = "unknown"
        self.scheduler = AsyncIOScheduler(timezone=settings.tzinfo)
        self._update_lock = asyncio.Lock()

    def _cron_trigger(self, expr: str, jitter_minutes: int) -> CronTrigger:
        """Cron trigger with a random 0..jitter delay after each slot (from_crontab has no jitter)."""
        f = expr.split()  # 5 fields, already validated by Settings
        return CronTrigger(
            minute=f[0], hour=f[1], day=f[2], month=f[3], day_of_week=f[4],
            timezone=self.settings.tzinfo, jitter=jitter_minutes * 60 or None,
        )

    async def start(self) -> None:
        s = self.settings
        self._schedule_discovery()
        self.scheduler.add_job(
            self.sync_job, self._cron_trigger(s.sync_cron, s.sync_jitter),
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

    def _schedule_discovery(self) -> None:
        """Plan the next discovery run at a random point in [MIN, MAX] minutes (SPEC 6.1)."""
        delay = random.uniform(self.settings.discovery_interval_min, self.settings.discovery_interval_max)
        when = datetime.now(timezone.utc) + timedelta(minutes=delay)
        self.scheduler.add_job(
            self._discovery_tick, "date", run_date=when, id="discovery",
            max_instances=1, replace_existing=True,
        )
        log.debug("next discovery in %.0f minutes", delay)

    async def _discovery_tick(self) -> None:
        self._schedule_discovery()  # first, so long or skipped runs do not shift the cadence
        await self.discovery_job()

    async def discovery_job(self, force: bool = False) -> None:
        url = self.settings.hc_discovery_url
        if not force and not self.queue.is_idle():
            log.info("discovery skipped: queue busy (jobs queued/running)")
            await ping(url, "success", "skipped: queue busy")
            return
        await ping(url, "start")
        try:
            stats = await run_discovery(self.settings, self.queue)
            await ping(url, "success", f"new={stats.new} updated={stats.updated}")
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

    def schedule_update_after_403(self) -> None:
        """Fire-and-forget update check after a job aborted with HTTP 403 (SPEC 6.7)."""
        task = asyncio.create_task(self._update_after_403())
        _background.add(task)
        task.add_done_callback(_background.discard)

    async def _update_after_403(self) -> None:
        for _ in range(60):  # wait for the aborted job to be finalized, at most ~10 min
            if self.queue.current_job_id is None:
                break
            await asyncio.sleep(10)
        log.info("checking for yt-dlp update after HTTP 403")
        await self.update_ytdlp(wait_for_idle=False)

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
        async with self._update_lock:
            lib = s.config_dir / "ytdlp-lib"
            # pip --target reinstalls (and reports "Successfully installed") on every
            # run, so the effective version decides whether the cache went stale.
            before = await ytdlp.get_version(s.ytdlp_bin, ytdlp.ytdlp_env(s.config_dir))
            try:
                proc = await asyncio.create_subprocess_exec(
                    *ytdlp.build_update_command(lib),
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                )
                out, _ = await asyncio.wait_for(proc.communicate(), timeout=300)
                if proc.returncode != 0:
                    log.warning("yt-dlp update failed: %s", out.decode(errors="replace")[-300:])
            except Exception as exc:  # noqa: BLE001
                log.warning("yt-dlp update failed: %s", exc)
            self.ytdlp_version = await ytdlp.get_version(s.ytdlp_bin, ytdlp.ytdlp_env(s.config_dir))
            log.info("yt-dlp version: %s", self.ytdlp_version)
            if self.ytdlp_version != before:
                await self.clear_ytdlp_cache()

    async def clear_ytdlp_cache(self) -> None:
        """Drop cached signatures/challenge values after an update (SPEC 6.7)."""
        try:
            proc = await asyncio.create_subprocess_exec(
                *ytdlp.build_cache_clear_command(self.settings.ytdlp_bin),
                env=ytdlp.ytdlp_env(self.settings.config_dir),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
            if proc.returncode != 0:
                log.warning("yt-dlp cache clear failed: %s", out.decode(errors="replace")[-200:])
            else:
                log.info("yt-dlp cache cleared")
        except Exception as exc:  # noqa: BLE001
            log.warning("yt-dlp cache clear failed: %s", exc)

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
