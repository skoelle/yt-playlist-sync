# Copyright (c) 2026 Stefan Koelle (https://stefankoelle.de)
# Licensed under the MIT License. See LICENSE file in project root for details.
"""Hourly discovery of public playlists (SPEC 6.1 and 6.2)."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from sqlalchemy import select

from . import ytdlp
from .config import Settings
from .db import session_scope, utcnow
from .jobqueue import JobQueue
from .models import Playlist, Run
from .paths import is_oneshot

log = logging.getLogger(__name__)
_lock = asyncio.Lock()


@dataclass
class DiscoveryStats:
    new: int = 0
    updated: int = 0
    to_enqueue: list[int] = field(default_factory=list)


def apply_discovery(infos: list[ytdlp.PlaylistInfo], keyword: str) -> DiscoveryStats:
    """Reconcile the playlist table with the discovered list. Never deletes anything.

    Playlists that are not in the list stay untouched (unlisted playlists keep
    syncing); only a sync run can confirm that a playlist is gone (SPEC 6.1).
    """
    stats = DiscoveryStats()
    now = utcnow()
    with session_scope() as s:
        existing = {p.playlist_id: p for p in s.scalars(select(Playlist)).all()}
        for info in infos:
            pl = existing.get(info.id)
            if pl is None:
                pl = Playlist(
                    playlist_id=info.id, title=info.title,
                    type="oneshot" if is_oneshot(info.title, keyword) else "sync",
                    remote_item_count=info.item_count, state="new", remote_status="active",
                    first_seen_at=now, last_seen_at=now,
                )
                s.add(pl)
                s.flush()
                stats.new += 1
                stats.to_enqueue.append(pl.id)
                continue
            if pl.title != info.title or pl.remote_status != "active":
                stats.updated += 1
            pl.title = info.title
            pl.remote_status = "active"
            pl.last_seen_at = now
            if info.item_count:
                pl.remote_item_count = info.item_count
            if pl.state == "new" and not pl.ignored:
                stats.to_enqueue.append(pl.id)
    return stats


def start_run(kind: str) -> int:
    with session_scope() as s:
        run = Run(kind=kind, started_at=utcnow(), status="running")
        s.add(run)
        s.flush()
        return run.id


def finish_run(run_id: int, status: str, message: str | None) -> None:
    with session_scope() as s:
        run = s.get(Run, run_id)
        if run is not None:
            run.status = status
            run.finished_at = utcnow()
            run.message = message


async def run_discovery(settings: Settings, queue: JobQueue) -> DiscoveryStats:
    async with _lock:
        run_id = start_run("discovery")
        try:
            env = ytdlp.ytdlp_env(settings.config_dir)
            infos = await ytdlp.list_channel_playlists(settings.ytdlp_bin, settings.youtube_channel, env)
            if not infos:
                with session_scope() as s:
                    known = s.scalar(select(Playlist.id).limit(1))
                if known is not None:
                    raise ytdlp.YtDlpError("Channel returned no playlists, refusing the listing")
            stats = apply_discovery(infos, settings.oneshot_keyword)
            for pk in stats.to_enqueue:
                queue.enqueue(pk, "discovery")
            finish_run(
                run_id, "success",
                f"{len(infos)} playlists, {stats.new} new, {stats.updated} updated",
            )
            return stats
        except Exception as exc:
            finish_run(run_id, "failed", str(exc)[:500])
            raise
