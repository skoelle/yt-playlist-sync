# Copyright (c) 2026 Stefan Koelle (https://stefankoelle.de)
# Licensed under the MIT License. See LICENSE file in project root for details.
import asyncio

import pytest

pytest.importorskip("sqlalchemy")
pytest.importorskip("alembic")
pytest.importorskip("pydantic_settings")
pytest.importorskip("apscheduler")

from sqlalchemy import select  # noqa: E402

from app.config import Settings, ensure_dirs  # noqa: E402
from app.db import init_engine, migrate, session_scope  # noqa: E402
from app.discovery import apply_discovery, run_discovery  # noqa: E402
from app.jobqueue import JobQueue  # noqa: E402
from app.models import Job, Playlist  # noqa: E402
from app.ytdlp import PlaylistInfo  # noqa: E402


@pytest.fixture
def cfg(tmp_path, stub):
    c = Settings(
        youtube_channel="@beispielkanal", data_dir=tmp_path / "data", config_dir=tmp_path / "config",
        ytdlp_bin=stub.bin, sleep_min=0, sleep_max=0,
    )
    ensure_dirs(c)
    init_engine(c.db_url)
    migrate(c.db_url)
    return c


def playlists():
    with session_scope() as s:
        return {p.playlist_id: (p.type, p.state, p.remote_status, p.downloaded_count, p.folder_name)
                for p in s.scalars(select(Playlist)).all()}


def test_apply_discovery_types_rename_and_removal(cfg):
    stats = apply_discovery(
        [PlaylistInfo("PL1", "Sommer Mix"), PlaylistInfo("PL2", "Setlist 2024")], "setlist"
    )
    assert stats.new == 2 and len(stats.to_enqueue) == 2
    assert playlists()["PL1"][0] == "sync" and playlists()["PL2"][0] == "oneshot"
    stats = apply_discovery([PlaylistInfo("PL1", "Winter Setlist")], "setlist")
    assert playlists()["PL1"][0] == "sync"
    assert playlists()["PL2"][2] == "removed" and stats.removed == 1
    with session_scope() as s:
        assert len(s.scalars(select(Playlist)).all()) == 2


def test_full_flow_with_stub(cfg):
    async def scenario():
        queue = JobQueue(cfg)
        await queue.start()
        await run_discovery(cfg, queue)
        with session_scope() as s:
            ids = list(s.scalars(select(Job.id)).all())
        assert len(ids) == 2
        statuses = await queue.wait_for_jobs(ids, poll=0.2)
        assert set(statuses.values()) == {"success"}
        again = await run_discovery(cfg, queue)
        assert again.to_enqueue == []
        with session_scope() as s:
            pk = s.scalar(select(Playlist.id).where(Playlist.playlist_id == "PLbbb"))
        assert queue.enqueue(pk, "nightly") is None
        await queue.stop()

    asyncio.run(scenario())
    pls = playlists()
    assert pls["PLaaa"][:4] == ("sync", "idle", "active", 3)
    assert pls["PLbbb"][:4] == ("oneshot", "done", "active", 2)
    assert "[PLbbb]" in pls["PLbbb"][4]
    assert len(list((cfg.data_dir / pls["PLbbb"][4]).glob("*.mp4"))) == 2


def test_oneshot_failure_and_retry(cfg, stub):
    stub.data_ref["fail"] = {"vid00000012": "temporary"}
    stub.save()

    async def scenario():
        queue = JobQueue(cfg)
        await queue.start()
        await run_discovery(cfg, queue)
        with session_scope() as s:
            ids = list(s.scalars(select(Job.id)).all())
        await queue.wait_for_jobs(ids, poll=0.2)
        assert playlists()["PLbbb"][1] == "failed"
        stub.data_ref["fail"] = {}
        stub.save()
        with session_scope() as s:
            pk = s.scalar(select(Playlist.id).where(Playlist.playlist_id == "PLbbb"))
        jid = queue.enqueue(pk, "retry")
        assert jid is not None
        assert queue.enqueue(pk, "retry") is None
        await queue.wait_for_jobs([jid], poll=0.2)
        await queue.stop()

    asyncio.run(scenario())
    assert playlists()["PLbbb"][1] == "done"


def test_priority_order_without_worker(cfg):
    apply_discovery([PlaylistInfo(f"PL{i}", f"P{i}") for i in range(3)], "setlist")
    with session_scope() as s:
        pks = list(s.scalars(select(Playlist.id).order_by(Playlist.id)).all())
    queue = JobQueue(cfg)
    j_night = queue.enqueue(pks[0], "nightly")
    j_disc = queue.enqueue(pks[1], "discovery")
    j_man = queue.enqueue(pks[2], "manual")
    order = []
    for _ in range(3):
        nxt = queue._next_job()
        order.append(nxt)
        with session_scope() as s:
            s.get(Job, nxt).status = "success"
    assert order == [j_man, j_disc, j_night]


def test_cancel_queued_job(cfg):
    apply_discovery([PlaylistInfo("PLx", "Setlist X")], "setlist")
    with session_scope() as s:
        pk = s.scalar(select(Playlist.id))
    queue = JobQueue(cfg)
    jid = queue.enqueue(pk, "manual")
    assert queue.cancel(jid) is True
    assert playlists()["PLx"][1] == "failed"
    assert queue.cancel(99999) is False
