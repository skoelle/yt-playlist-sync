# Copyright (c) 2026 Stefan Koelle (https://stefankoelle.de)
# Licensed under the MIT License. See LICENSE file in project root for details.
"""Single-worker job queue backed by the database."""
from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from sqlalchemy import select

from . import ytdlp
from .config import Settings
from .db import session_scope, utcnow
from .models import Job, Playlist, PlaylistEntry
from .paths import sanitize_folder_name
from .runner import ProcessHandle, RunParams, RunResult, run_playlist

log = logging.getLogger(__name__)

PRIORITY = {"manual": 0, "retry": 0, "full_rerun": 0, "discovery": 1, "nightly": 2}
AUTO_TRIGGERS = ("discovery", "nightly")
RATE_LIMIT_PAUSE = timedelta(minutes=30)


class JobQueue:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.live: dict[str, Any] = {}
        self.paused_until = None
        self.current_job_id: int | None = None
        self.on_forbidden: Callable[[], None] | None = None
        self.is_ytdlp_updating: Callable[[], bool] | None = None
        self._wakeup = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None
        self._handle: ProcessHandle | None = None
        self._stopping = False
        self._last_persist = 0.0
        self._gap_pending = False

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self.recover()
        self._task = asyncio.create_task(self._worker(), name="job-worker")

    async def stop(self) -> None:
        self._stopping = True
        if self._handle is not None:
            self._handle.terminate()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    def _poke(self) -> None:
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._wakeup.set)

    def recover(self) -> None:
        """Jobs left in 'running' after a crash are marked interrupted and queued again."""
        requeue: list[tuple[int, str]] = []
        with session_scope() as s:
            for job in s.scalars(select(Job).where(Job.status == "running")).all():
                job.status = "interrupted"
                job.finished_at = utcnow()
                pl = s.get(Playlist, job.playlist_id)
                if pl is not None:
                    pl.state = "new" if pl.type == "oneshot" else "idle"
                    requeue.append((pl.id, job.trigger))
        for pk, trigger in requeue:
            self.enqueue(pk, trigger)

    # ------------------------------------------------------------- enqueue
    def enqueue(self, playlist_pk: int, trigger: str) -> int | None:
        with session_scope() as s:
            pl = s.get(Playlist, playlist_pk)
            if pl is None:
                return None
            if trigger in AUTO_TRIGGERS and (pl.ignored or pl.remote_status == "removed"):
                return None
            if pl.type == "oneshot" and pl.state == "done" and trigger != "full_rerun":
                return None
            open_job = s.scalar(
                select(Job.id).where(Job.playlist_id == pl.id, Job.status.in_(("queued", "running")))
            )
            if open_job is not None:
                return None
            job = Job(
                playlist_id=pl.id, trigger=trigger, status="queued",
                priority=PRIORITY[trigger], queued_at=utcnow(),
            )
            s.add(job)
            pl.state = "queued"
            s.flush()
            job_id = job.id
        self._poke()
        return job_id

    def cancel(self, job_id: int) -> bool:
        with session_scope() as s:
            job = s.get(Job, job_id)
            if job is None:
                return False
            if job.status == "queued":
                job.status = "cancelled"
                job.finished_at = utcnow()
                job.error_summary = "Cancelled by user"
                pl = s.get(Playlist, job.playlist_id)
                if pl is not None:
                    pl.state = "failed" if pl.type == "oneshot" else "idle"
                return True
        if job_id == self.current_job_id and self._handle is not None:
            self._handle.cancel()
            return True
        return False

    async def wait_for_jobs(self, job_ids: list[int], poll: float = 5.0) -> dict[int, str]:
        """Wait until all given jobs have left the queued/running state."""
        while True:
            with session_scope() as s:
                rows = (
                    s.execute(select(Job.id, Job.status).where(Job.id.in_(job_ids))).all()
                    if job_ids
                    else []
                )
            statuses = {r[0]: r[1] for r in rows}
            if all(st not in ("queued", "running") for st in statuses.values()):
                return statuses
            await asyncio.sleep(poll)

    def is_idle(self) -> bool:
        with session_scope() as s:
            busy = s.scalar(select(Job.id).where(Job.status.in_(("queued", "running"))).limit(1))
        return busy is None

    # -------------------------------------------------------------- worker
    def _paused_seconds(self) -> float:
        if self.paused_until is None:
            return 0.0
        left = (self.paused_until - utcnow()).total_seconds()
        if left <= 0:
            self.paused_until = None
            return 0.0
        return left

    def _next_job(self) -> int | None:
        with session_scope() as s:
            return s.scalar(
                select(Job.id).where(Job.status == "queued")
                .order_by(Job.priority, Job.queued_at, Job.id).limit(1)
            )

    async def _wait_gap(self) -> None:
        """Randomized pause between two jobs so back-to-back starts are not metronomic (SPEC 6.6)."""
        lo, hi = self.settings.job_gap_min, self.settings.job_gap_max
        if hi <= 0:
            return
        delay = random.uniform(lo, hi) if hi > lo else float(lo)
        log.debug("waiting %.1fs before next job", delay)
        await asyncio.sleep(delay)

    async def _worker(self) -> None:
        while not self._stopping:
            wait = self._paused_seconds()
            if wait > 0:
                self._gap_pending = False
                await asyncio.sleep(min(wait, 30))
                continue
            if self.is_ytdlp_updating is not None and self.is_ytdlp_updating():
                # yt-dlp library is mid-swap: importing a half-written tree must not
                # happen, the jobs simply wait (SPEC 6.7).
                self._gap_pending = False
                await asyncio.sleep(5)
                continue
            if not self.settings.dry_run and not self.settings.data_writable():
                self._gap_pending = False
                log.error("%s is not writable, downloads are paused", self.settings.data_dir)
                await asyncio.sleep(60)
                continue
            self._wakeup.clear()
            job_id = self._next_job()
            if job_id is None:
                self._gap_pending = False
                try:
                    await asyncio.wait_for(self._wakeup.wait(), timeout=30)
                except asyncio.TimeoutError:
                    pass
                continue
            if self._gap_pending:
                self._gap_pending = False
                await self._wait_gap()
            try:
                await self._run_job(job_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.exception("job %s crashed", job_id)
                self._mark_crashed(job_id, str(exc))
            finally:
                self._gap_pending = True

    def _mark_crashed(self, job_id: int, message: str) -> None:
        with session_scope() as s:
            job = s.get(Job, job_id)
            if job is None:
                return
            job.status = "failed"
            job.finished_at = utcnow()
            job.error_summary = f"Internal error: {message[:300]}"
            pl = s.get(Playlist, job.playlist_id)
            if pl is not None:
                pl.state = "failed"
        self.live = {}
        self.current_job_id = None

    async def _run_job(self, job_id: int) -> None:
        cfg = self.settings
        with session_scope() as s:
            job = s.get(Job, job_id)
            if job is None or job.status != "queued":
                return
            pl = s.get(Playlist, job.playlist_id)
            if pl is None:
                job.status = "failed"
                job.error_summary = "Playlist vanished"
                return
            if not pl.folder_name:
                pl.folder_name = sanitize_folder_name(pl.title, pl.playlist_id)
            log_path = cfg.logs_dir / f"{job.id}.log"
            job.status = "running"
            job.started_at = utcnow()
            job.log_path = str(log_path)
            pl.state = "running"
            if pl.first_downloaded_at is None and not cfg.dry_run:
                pl.first_downloaded_at = utcnow()
            params = RunParams(
                ytdlp_bin=cfg.ytdlp_bin, playlist_id=pl.playlist_id, folder=pl.folder_name,
                data_dir=cfg.data_dir, config_dir=cfg.config_dir, log_path=log_path,
                sleep_min=cfg.sleep_min, sleep_max=cfg.sleep_max, extra_args=cfg.ytdlp_extra_args,
                dry_run=cfg.dry_run,
            )
            playlist_title = pl.title
            playlist_type = pl.type
            playlist_pk = pl.id

        self.current_job_id = job_id
        self.live = {
            "job_id": job_id, "playlist_pk": playlist_pk, "playlist_title": playlist_title,
            "current_item": None, "percent": None, "speed": None, "eta": None,
            "item_index": None, "item_total": None,
        }
        self._handle = ProcessHandle()
        self._last_persist = 0.0

        def on_event(ev: dict[str, Any]) -> None:
            kind = ev["type"]
            if kind == "progress":
                self.live.update(percent=ev["percent"], speed=ev["speed"], eta=ev["eta"])
            elif kind == "item":
                self.live.update(item_index=ev["index"], item_total=ev["total"], percent=None)
            elif kind == "destination":
                self.live["current_item"] = ev["name"]
            now = time.monotonic()
            if now - self._last_persist >= 2.0:
                self._last_persist = now
                self._persist_live(job_id)

        result = await run_playlist(params, on_event, self._handle)
        size = None
        if not cfg.dry_run:
            size = await asyncio.to_thread(ytdlp.folder_size, cfg.data_dir / params.folder)
        self._finalize(job_id, playlist_type, result, size)
        self._handle = None
        self.current_job_id = None
        self.live = {}
        if result.rate_limited or result.forbidden:
            self.paused_until = utcnow() + RATE_LIMIT_PAUSE
            log.warning("%s, queue paused until %s UTC",
                        "forbidden (HTTP 403)" if result.forbidden else "rate limited",
                        self.paused_until)
        if result.forbidden and self.on_forbidden is not None:
            self.on_forbidden()

    def _persist_live(self, job_id: int) -> None:
        try:
            with session_scope() as s:
                job = s.get(Job, job_id)
                if job is not None and job.status == "running":
                    job.current_item = (self.live.get("current_item") or "")[:500] or None
                    job.progress_percent = self.live.get("percent")
        except Exception:  # noqa: BLE001
            log.debug("could not persist live state", exc_info=True)

    def _finalize(self, job_id: int, playlist_type: str, r: RunResult, size: int | None) -> None:
        dry = self.settings.dry_run
        with session_scope() as s:
            job = s.get(Job, job_id)
            pl = s.get(Playlist, job.playlist_id)
            job.finished_at = utcnow()
            job.exit_code = r.exit_code
            job.items_new = r.new_count
            job.items_skipped = r.skipped
            job.items_failed = r.failed
            job.error_summary = r.error_summary
            job.current_item = None
            if r.cancelled:
                job.status = "cancelled"
            elif r.success:
                job.status = "success"
                job.progress_percent = 100.0
            else:
                job.status = "failed"

            if r.total:
                pl.remote_item_count = r.total
            if r.entries:
                self._persist_entries(s, pl, r)
            if r.cancelled:
                pl.state = "failed" if playlist_type == "oneshot" else "idle"
                return
            if dry:
                pl.state = "new"
                return
            if r.gone:
                # yt-dlp confirmed the playlist itself is gone (deleted, private, 404).
                # Local files, entries and counts stay untouched - only the sync status
                # changes: no more nightly sync, the retry button stays available.
                pl.remote_status = "removed"
                if playlist_type == "sync":
                    pl.type = "oneshot"
                pl.state = "failed"
                return
            if r.entries and pl.remote_status == "removed":
                # The listing worked again, so the playlist exists (it may just not be
                # listed by the channel). Back to active; the type is never restored here.
                pl.remote_status = "active"
            pl.downloaded_count = r.archived
            pl.skipped_count = r.skipped
            pl.failed_count = r.failed
            if size is not None:
                pl.size_bytes = size
            if r.success:
                if playlist_type == "oneshot":
                    pl.state = "done"
                    if pl.completed_at is None or job.trigger == "full_rerun":
                        pl.completed_at = utcnow()
                else:
                    pl.state = "idle"
                    pl.last_sync_at = utcnow()
            else:
                pl.state = "failed"

    def _persist_entries(self, s, pl: Playlist, r: RunResult) -> None:
        """Upsert the remote listing snapshot of this run. Rows are updated, never deleted."""
        now = utcnow()
        rows = {
            e.video_id: e for e in s.scalars(
                select(PlaylistEntry).where(PlaylistEntry.playlist_id == pl.id)
            ).all()
        }
        for entry in r.entries:
            row = rows.get(entry.id)
            if row is None:
                row = PlaylistEntry(playlist_id=pl.id, video_id=entry.id, last_seen_at=now)
                s.add(row)
                rows[entry.id] = row
            row.position = entry.position
            row.title = entry.title[:500]
            row.duration_s = entry.duration_s
            row.unavailable = entry.unavailable
            row.remote_present = True
            row.last_seen_at = now
        listed = {e.id for e in r.entries}
        for vid, row in rows.items():
            if vid not in listed:
                row.remote_present = False
        for err in r.errors:
            vid = err.get("id")
            if not vid:
                continue
            row = rows.get(vid)
            if row is None:  # vanished from the listing between listing and finalize
                row = PlaylistEntry(playlist_id=pl.id, video_id=vid, last_seen_at=now)
                s.add(row)
                rows[vid] = row
            row.reason = str(err.get("message") or "")[:1000] or None
            if err.get("permanent"):
                row.unavailable = True
        archive = self.settings.config_dir / "archives" / f"{pl.playlist_id}.txt"
        archived = ytdlp.read_archive(archive)
        for vid, row in rows.items():
            if vid in archived:
                row.reason = None
