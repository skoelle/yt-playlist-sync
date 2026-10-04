# Copyright (c) 2026 Stefan Koelle (https://stefankoelle.de)
# Licensed under the MIT License. See LICENSE file in project root for details.
"""REST API (SPEC section 8)."""
from __future__ import annotations

import asyncio
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import select

from .db import session_scope
from .models import Job, Playlist, Run
from .ytdlp import read_video_entries

router = APIRouter(prefix="/api")
_background: set[asyncio.Task] = set()


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat() + "Z" if dt else None


def _duration(job: Job) -> float | None:
    if job.started_at and job.finished_at:
        return (job.finished_at - job.started_at).total_seconds()
    return None


def job_dict(job: Job, title: str | None = None) -> dict[str, Any]:
    return {
        "id": job.id, "playlist_pk": job.playlist_id, "playlist_title": title, "trigger": job.trigger,
        "status": job.status, "queued_at": iso(job.queued_at), "started_at": iso(job.started_at),
        "finished_at": iso(job.finished_at), "duration_s": _duration(job),
        "items_new": job.items_new, "items_skipped": job.items_skipped, "items_failed": job.items_failed,
        "error_summary": job.error_summary, "progress_percent": job.progress_percent,
        "current_item": job.current_item,
    }


def playlist_dict(pl: Playlist, last_job: Job | None) -> dict[str, Any]:
    return {
        "id": pl.id, "playlist_id": pl.playlist_id, "title": pl.title, "type": pl.type,
        "url": f"https://www.youtube.com/playlist?list={pl.playlist_id}",
        "folder_name": pl.folder_name, "remote_item_count": pl.remote_item_count,
        "downloaded_count": pl.downloaded_count, "skipped_count": pl.skipped_count,
        "failed_count": pl.failed_count, "size_bytes": pl.size_bytes, "state": pl.state,
        "remote_status": pl.remote_status, "ignored": pl.ignored,
        "first_seen_at": iso(pl.first_seen_at), "last_seen_at": iso(pl.last_seen_at),
        "first_downloaded_at": iso(pl.first_downloaded_at),
        "completed_at": iso(pl.completed_at), "last_sync_at": iso(pl.last_sync_at),
        "last_job": job_dict(last_job) if last_job else None,
    }


def _get_playlist(s, pid: int) -> Playlist:
    pl = s.get(Playlist, pid)
    if pl is None:
        raise HTTPException(404, "playlist not found")
    return pl


def _enqueue_or_409(request: Request, pid: int, trigger: str) -> dict[str, Any]:
    job_id = request.app.state.queue.enqueue(pid, trigger)
    if job_id is None:
        raise HTTPException(409, "job not queued (already queued/running, or already done)")
    return {"job_id": job_id}


@router.get("/status")
async def status(request: Request) -> dict[str, Any]:
    st = request.app.state
    cfg, queue, sched = st.settings, st.queue, st.scheduler
    with session_scope() as s:
        queued = s.execute(
            select(Job, Playlist.title).join(Playlist, Playlist.id == Job.playlist_id)
            .where(Job.status == "queued").order_by(Job.priority, Job.queued_at, Job.id).limit(20)
        ).all()
        runs: dict[str, Any] = {}
        for kind in ("discovery", "sync"):
            run = s.scalar(select(Run).where(Run.kind == kind).order_by(Run.id.desc()).limit(1))
            runs[kind] = None if run is None else {
                "status": run.status, "started_at": iso(run.started_at),
                "finished_at": iso(run.finished_at), "message": run.message,
            }
        failed_oneshots = len(s.scalars(select(Playlist.id).where(
            Playlist.type == "oneshot", Playlist.state == "failed")).all())
        queue_items = [{"job_id": j.id, "playlist_title": t, "trigger": j.trigger} for j, t in queued]
    try:
        usage = shutil.disk_usage(cfg.data_dir)
        free, total = usage.free, usage.total
    except OSError:
        free = total = None
    return {
        "dry_run": cfg.dry_run, "channel": cfg.youtube_channel, "oneshot_keyword": cfg.oneshot_keyword,
        "ytdlp_version": sched.ytdlp_version, "data_writable": cfg.data_writable(),
        "free_bytes": free, "total_bytes": total, "current": dict(queue.live) or None,
        "queue": queue_items, "paused_until": iso(queue.paused_until), "failed_oneshots": failed_oneshots,
        "schedules": {
            "discovery": {"last": runs["discovery"], "next": sched.next_run("discovery")},
            "sync": {"last": runs["sync"], "next": sched.next_run("sync")},
        },
    }


@router.get("/playlists")
async def playlists(type: str | None = Query(None, pattern="^(sync|oneshot)$")) -> list[dict[str, Any]]:
    with session_scope() as s:
        stmt = select(Playlist)
        if type:
            stmt = stmt.where(Playlist.type == type)
        out = []
        for pl in s.scalars(stmt.order_by(Playlist.title)).all():
            last = s.scalar(select(Job).where(Job.playlist_id == pl.id).order_by(Job.id.desc()).limit(1))
            out.append(playlist_dict(pl, last))
    if type == "oneshot":
        out.sort(key=lambda p: p["completed_at"] or p["first_downloaded_at"] or "", reverse=True)
    return out


@router.get("/playlists/{pid}")
async def playlist_detail(pid: int) -> dict[str, Any]:
    with session_scope() as s:
        pl = _get_playlist(s, pid)
        jobs = s.scalars(
            select(Job).where(Job.playlist_id == pid).order_by(Job.id.desc()).limit(25)
        ).all()
        return {**playlist_dict(pl, jobs[0] if jobs else None),
                "jobs": [job_dict(j, pl.title) for j in jobs]}


@router.get("/playlists/{pid}/files")
async def playlist_files(pid: int, request: Request) -> dict[str, Any]:
    cfg = request.app.state.settings
    with session_scope() as s:
        pl = _get_playlist(s, pid)
        folder = pl.folder_name
    base = cfg.data_dir.resolve()
    files: list[dict[str, Any]] = []
    exists = False
    if folder:
        target = (base / folder).resolve()
        if target.is_relative_to(base) and target.is_dir():
            exists = True
            for p in sorted(target.iterdir(), key=lambda x: x.name):
                if not p.is_file():
                    continue
                try:
                    st = p.stat()
                except OSError:
                    continue
                mtime = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).replace(tzinfo=None)
                files.append({"name": p.name, "size_bytes": st.st_size, "modified_at": iso(mtime)})
    by_ext: dict[str, int] = {}
    for f in files:
        name = f["name"]
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        by_ext[ext] = by_ext.get(ext, 0) + 1
    return {
        "exists": exists, "folder_name": folder,
        "total_files": len(files), "total_bytes": sum(f["size_bytes"] for f in files),
        "by_ext": by_ext, "files": files,
    }


def _playlist_folder(cfg, folder_name: str | None) -> Path | None:
    if not folder_name:
        return None
    base = cfg.data_dir.resolve()
    target = (base / folder_name).resolve()
    return target if target.is_relative_to(base) else None


_EMPTY_VIDEOS: dict[str, Any] = {
    "exists": False, "cover": None, "video_count": 0, "total_duration_s": 0, "videos": [],
}


@router.get("/playlists/{pid}/videos")
async def playlist_videos(pid: int, request: Request) -> dict[str, Any]:
    with session_scope() as s:
        pl = _get_playlist(s, pid)
        folder_name, playlist_id = pl.folder_name, pl.playlist_id
    target = _playlist_folder(request.app.state.settings, folder_name)
    if target is None:
        return dict(_EMPTY_VIDEOS)
    return read_video_entries(target, playlist_id)


_IMG_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
_VIDEO_EXTS = {".mkv", ".mp4", ".webm"}
_VIDEO_MEDIA = {".mkv": "video/x-matroska", ".mp4": "video/mp4", ".webm": "video/webm"}


def _safe_media_file(cfg, folder_name: str | None, file: str, exts: set[str]) -> Path:
    """Resolve a single media file inside the playlist folder, or raise 404."""
    if not file or "/" in file or "\\" in file or Path(file).suffix.lower() not in exts:
        raise HTTPException(404, "file not found")
    target = _playlist_folder(cfg, folder_name)
    if target is None:
        raise HTTPException(404, "file not found")
    f = target / Path(file).name
    if not f.is_file():
        raise HTTPException(404, "file not found")
    return f


def _folder_name(pid: int) -> str | None:
    with session_scope() as s:
        return _get_playlist(s, pid).folder_name


@router.get("/playlists/{pid}/thumb")
async def playlist_thumb(request: Request, pid: int, file: str = Query(...)) -> FileResponse:
    f = _safe_media_file(request.app.state.settings, _folder_name(pid), file, _IMG_EXTS)
    return FileResponse(f, headers={"Cache-Control": "private, max-age=3600"})


@router.get("/playlists/{pid}/video")
async def playlist_video(request: Request, pid: int, file: str = Query(...)) -> FileResponse:
    f = _safe_media_file(request.app.state.settings, _folder_name(pid), file, _VIDEO_EXTS)
    media = _VIDEO_MEDIA[f.suffix.lower()]
    return FileResponse(f, media_type=media, headers={"Cache-Control": "private, max-age=3600"})


@router.post("/playlists/{pid}/run")
async def run_playlist_now(pid: int, request: Request) -> dict[str, Any]:
    with session_scope() as s:
        pl = _get_playlist(s, pid)
        if pl.type == "oneshot" and pl.state == "done":
            raise HTTPException(409, "oneshot already done, use rerun")
    return _enqueue_or_409(request, pid, "manual")


@router.post("/playlists/{pid}/retry")
async def retry_playlist(pid: int, request: Request) -> dict[str, Any]:
    with session_scope() as s:
        pl = _get_playlist(s, pid)
        if pl.state != "failed":
            raise HTTPException(409, "only failed playlists can be retried")
    return _enqueue_or_409(request, pid, "retry")


@router.post("/playlists/{pid}/rerun")
async def rerun_playlist(pid: int, request: Request) -> dict[str, Any]:
    with session_scope() as s:
        _get_playlist(s, pid)
    return _enqueue_or_409(request, pid, "full_rerun")


class IgnoreBody(BaseModel):
    ignored: bool = True


@router.post("/playlists/{pid}/ignore")
async def ignore_playlist(pid: int, body: IgnoreBody) -> dict[str, Any]:
    with session_scope() as s:
        pl = _get_playlist(s, pid)
        pl.ignored = body.ignored
    return {"ignored": body.ignored}


class TypeBody(BaseModel):
    type: str


@router.post("/playlists/{pid}/type")
async def set_playlist_type(pid: int, body: TypeBody) -> dict[str, Any]:
    if body.type not in ("sync", "oneshot"):
        raise HTTPException(422, "type must be 'sync' or 'oneshot'")
    with session_scope() as s:
        pl = _get_playlist(s, pid)
        if pl.state in ("queued", "running"):
            raise HTTPException(409, "playlist has an open job")
        pl.type = body.type
        if body.type == "sync" and pl.state == "done":
            pl.state = "idle"
        if body.type == "oneshot" and pl.state == "idle" and pl.last_sync_at is not None:
            pl.state = "done"
            pl.completed_at = pl.completed_at or pl.last_sync_at
    return {"type": body.type}


@router.get("/jobs")
async def jobs(limit: int = Query(50, ge=1, le=500)) -> list[dict[str, Any]]:
    with session_scope() as s:
        rows = s.execute(
            select(Job, Playlist.title).join(Playlist, Playlist.id == Job.playlist_id)
            .order_by(Job.id.desc()).limit(limit)
        ).all()
        return [job_dict(j, t) for j, t in rows]


@router.get("/jobs/{job_id}/log")
async def job_log(job_id: int, offset: int = Query(0, ge=0)) -> dict[str, Any]:
    with session_scope() as s:
        job = s.get(Job, job_id)
        if job is None:
            raise HTTPException(404, "job not found")
        path, job_status = job.log_path, job.status
    text, new_offset = "", offset
    if path:
        try:
            with open(path, "rb") as fh:
                fh.seek(offset)
                chunk = fh.read(256 * 1024)
                new_offset = offset + len(chunk)
                text = chunk.decode("utf-8", errors="replace")
        except FileNotFoundError:
            pass
    return {"text": text, "offset": new_offset, "finished": job_status not in ("queued", "running")}


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(job_id: int, request: Request) -> dict[str, Any]:
    if not request.app.state.queue.cancel(job_id):
        raise HTTPException(409, "job cannot be cancelled")
    return {"cancelled": True}


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)


@router.post("/discovery/run", status_code=202)
async def run_discovery_now(request: Request) -> dict[str, Any]:
    _spawn(request.app.state.scheduler.discovery_job(force=True))
    return {"started": True}


@router.post("/sync/run", status_code=202)
async def run_sync_now(request: Request) -> dict[str, Any]:
    _spawn(request.app.state.scheduler.sync_job())
    return {"started": True}
