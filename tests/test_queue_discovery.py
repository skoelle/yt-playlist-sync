# Copyright (c) 2026 Stefan Koelle (https://stefankoelle.de)
# Licensed under the MIT License. See LICENSE file in project root for details.
import asyncio

import pytest

pytest.importorskip("sqlalchemy")
pytest.importorskip("alembic")
pytest.importorskip("pydantic_settings")
pytest.importorskip("apscheduler")

from datetime import datetime, timezone  # noqa: E402

from sqlalchemy import select  # noqa: E402

from app.config import Settings, ensure_dirs  # noqa: E402
from app.db import init_engine, migrate, session_scope, utcnow  # noqa: E402
from app.discovery import apply_discovery, run_discovery  # noqa: E402
from app.jobqueue import JobQueue  # noqa: E402
from app.models import Job, Playlist, PlaylistEntry, Run  # noqa: E402
from app.scheduler import AppScheduler  # noqa: E402
from app.ytdlp import PlaylistInfo  # noqa: E402


@pytest.fixture
def cfg(tmp_path, stub):
    c = Settings(
        youtube_channel="@beispielkanal", data_dir=tmp_path / "data", config_dir=tmp_path / "config",
        ytdlp_bin=stub.bin, sleep_min=0, sleep_max=0, job_gap_min=0, job_gap_max=0,
    )
    ensure_dirs(c)
    init_engine(c.db_url)
    migrate(c.db_url)
    return c


def playlists():
    with session_scope() as s:
        return {p.playlist_id: (p.type, p.state, p.remote_status, p.downloaded_count, p.folder_name)
                for p in s.scalars(select(Playlist)).all()}


def discovery_runs():
    with session_scope() as s:
        return [(r.status, r.message) for r in s.scalars(select(Run).where(Run.kind == "discovery"))]


def record_pings(monkeypatch) -> list:
    pings: list = []

    async def fake_ping(url, kind="success", message=None):
        pings.append((kind, message))

    monkeypatch.setattr("app.scheduler.ping", fake_ping)
    return pings


def busy_queue(cfg) -> JobQueue:
    apply_discovery([PlaylistInfo("PLx", "Setlist X")], "setlist")
    with session_scope() as s:
        pk = s.scalar(select(Playlist.id))
    queue = JobQueue(cfg)
    assert queue.enqueue(pk, "nightly") is not None
    assert queue.is_idle() is False
    return queue


def test_discovery_skipped_while_queue_busy(cfg, monkeypatch):
    """Cron-Discovery überspringt, solange Jobs queued/running sind (SPEC 6.1)."""
    pings = record_pings(monkeypatch)
    queue = busy_queue(cfg)
    asyncio.run(AppScheduler(cfg, queue).discovery_job())
    assert pings == [("success", "skipped: queue busy")]
    assert discovery_runs() == []


def test_discovery_force_runs_while_busy(cfg, stub, monkeypatch):
    """Der manuelle Button (force=True) läuft auch bei lauter Queue."""
    pings = record_pings(monkeypatch)
    queue = busy_queue(cfg)
    asyncio.run(AppScheduler(cfg, queue).discovery_job(force=True))
    assert [k for k, _ in pings] == ["start", "success"]
    runs = discovery_runs()
    assert len(runs) == 1 and runs[0][0] == "success"


def test_discovery_runs_when_idle(cfg, stub, monkeypatch):
    pings = record_pings(monkeypatch)
    queue = JobQueue(cfg)
    assert queue.is_idle() is True
    asyncio.run(AppScheduler(cfg, queue).discovery_job())
    assert [k for k, _ in pings] == ["start", "success"]
    runs = discovery_runs()
    assert len(runs) == 1 and runs[0][0] == "success"


def test_discovery_scheduled_with_random_interval(cfg, monkeypatch):
    """Discovery läuft als Intervall 50–70 min statt als Cron, jeder Tick plant den nächsten (SPEC 6.1)."""
    ran: list[bool] = []

    async def fake_discovery(force: bool = False):
        ran.append(force)

    queue = JobQueue(cfg)
    sched = AppScheduler(cfg, queue)
    monkeypatch.setattr(sched, "discovery_job", fake_discovery)

    async def scenario():
        await sched.start()
        try:
            first = sched.scheduler.get_job("discovery").next_run_time
            await sched._discovery_tick()
            second = sched.scheduler.get_job("discovery").next_run_time
            return first, second
        finally:
            await sched.stop()

    first, second = asyncio.run(scenario())
    now = datetime.now(timezone.utc)
    for nxt in (first, second):
        delta = (nxt - now).total_seconds() / 60
        assert 49.5 <= delta <= 70.5
    assert ran == [False]


def test_sync_cron_has_jitter(cfg):
    """Nachtlauf startet 0–30 min nach dem Cron-Slot statt exakt, 0 schaltet den Jitter ab (SPEC 6.5)."""
    queue = JobQueue(cfg)

    async def trigger_jitter(sched):
        await sched.start()
        try:
            return sched.scheduler.get_job("sync").trigger.jitter
        finally:
            await sched.stop()

    assert asyncio.run(trigger_jitter(AppScheduler(cfg, queue))) == 1800
    cfg.sync_jitter = 0
    assert asyncio.run(trigger_jitter(AppScheduler(cfg, queue))) is None


def test_job_gap_between_playlists(cfg, stub):
    """Zwei Jobs laufen nicht metronomisch hintereinander ab, sondern mit zufälliger Pause (SPEC 6.6)."""
    cfg.job_gap_min = 1
    cfg.job_gap_max = 1
    apply_discovery([PlaylistInfo("PLaaa", "Sommer Mix"),
                     PlaylistInfo("PLbbb", "Konzert SETLIST 2024")], "setlist")
    with session_scope() as s:
        pks = {p.playlist_id: p.id for p in s.scalars(select(Playlist)).all()}
    queue = JobQueue(cfg)
    jids = [queue.enqueue(pks["PLaaa"], "manual"), queue.enqueue(pks["PLbbb"], "manual")]

    async def scenario():
        await queue.start()
        await queue.wait_for_jobs(jids, poll=0.2)
        await queue.stop()

    asyncio.run(scenario())
    with session_scope() as s:
        first, second = s.get(Job, jids[0]), s.get(Job, jids[1])
        assert first.status == "success" and second.status == "success"
        delta = (second.started_at - first.finished_at).total_seconds()
    assert delta >= 0.9


def test_forbidden_job_fails_playlist_pauses_and_triggers_update(cfg, stub):
    """HTTP 403: Job abbrechen, Playlist failed, Queue pausieren, Update anstoßen (SPEC 6.6/6.7)."""
    stub.data_ref["fail"] = {"vid00000001": "forbidden"}
    stub.save()
    apply_discovery([PlaylistInfo("PLaaa", "Sommer Mix")], "setlist")
    with session_scope() as s:
        pk = s.scalar(select(Playlist.id))
    fired: list[bool] = []
    queue = JobQueue(cfg)
    queue.on_forbidden = lambda: fired.append(True)
    jid = queue.enqueue(pk, "manual")

    async def scenario():
        await queue.start()
        await queue.wait_for_jobs([jid], poll=0.2)
        await queue.stop()

    asyncio.run(scenario())
    with session_scope() as s:
        job = s.get(Job, jid)
        pl = s.get(Playlist, pk)
        assert job.status == "failed" and "403" in (job.error_summary or "")
        assert pl.state == "failed"
    assert queue.paused_until is not None
    assert fired == [True]


def test_update_after_403_runs_update(cfg, monkeypatch):
    calls: list[bool] = []

    async def fake_update(wait_for_idle: bool):
        calls.append(wait_for_idle)

    queue = JobQueue(cfg)
    sched = AppScheduler(cfg, queue)
    monkeypatch.setattr(sched, "update_ytdlp", fake_update)
    asyncio.run(sched._update_after_403())
    assert calls == [False]


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


def entries_of(pl_pid: str):
    with session_scope() as s:
        pk = s.scalar(select(Playlist.id).where(Playlist.playlist_id == pl_pid))
        rows = s.scalars(
            select(PlaylistEntry).where(PlaylistEntry.playlist_id == pk)
            .order_by(PlaylistEntry.position, PlaylistEntry.video_id)
        ).all()
        return [(r.video_id, r.position, r.unavailable, r.reason, r.remote_present) for r in rows]


async def start_and_wait(cfg, queue: JobQueue) -> list[int]:
    """Boot the worker, run discovery and wait until every queued job finished."""
    await queue.start()
    await run_discovery(cfg, queue)
    with session_scope() as s:
        ids = list(s.scalars(select(Job.id)).all())
    await queue.wait_for_jobs(ids, poll=0.2)
    return ids


def test_playlist_entries_snapshot_and_reasons(cfg, stub):
    """Every run snapshots the remote listing; failed videos keep the last error message."""
    stub.data_ref["fail"] = {"vid00000002": "temporary"}
    stub.save()

    async def scenario():
        queue = JobQueue(cfg)
        await start_and_wait(cfg, queue)
        rows = entries_of("PLaaa")
        assert [(v, pos, present) for v, pos, _, _, present in rows] == [
            ("vid00000001", 1, True), ("vid00000002", 2, True), ("vid00000003", 3, True),
        ]
        assert rows[1][3] is not None and "503" in rows[1][3]
        assert rows[0][3] is None and rows[2][3] is None

        stub.data_ref["fail"] = {}
        stub.save()
        with session_scope() as s:
            pk = s.scalar(select(Playlist.id).where(Playlist.playlist_id == "PLaaa"))
        jid = queue.enqueue(pk, "retry")
        assert jid is not None
        await queue.wait_for_jobs([jid], poll=0.2)
        assert entries_of("PLaaa")[1][3] is None  # downloaded now: the stale reason is cleared
        await queue.stop()

    asyncio.run(scenario())


def test_playlist_entries_keep_rows_when_video_leaves_listing(cfg, stub):
    """Rows are never deleted; a video gone from the playlist is only flagged remote_present=False."""

    async def scenario():
        queue = JobQueue(cfg)
        await start_and_wait(cfg, queue)
        assert len(entries_of("PLaaa")) == 3

        stub.data_ref["playlists"]["PLaaa"] = stub.data_ref["playlists"]["PLaaa"][:2]
        stub.save()
        with session_scope() as s:
            pk = s.scalar(select(Playlist.id).where(Playlist.playlist_id == "PLaaa"))
        jid = queue.enqueue(pk, "manual")
        assert jid is not None
        await queue.wait_for_jobs([jid], poll=0.2)
        rows = entries_of("PLaaa")
        assert len(rows) == 3
        assert [present for *_, present in rows] == [True, True, False]
        await queue.stop()

    asyncio.run(scenario())


def test_playlist_entries_unavailable_marker(cfg, stub):
    """A [Private video] marker in the listing flags the entry as unavailable."""
    stub.data_ref["playlists"]["PLaaa"][1]["title"] = "[Private video]"
    stub.save()

    async def scenario():
        queue = JobQueue(cfg)
        await start_and_wait(cfg, queue)
        rows = entries_of("PLaaa")
        assert rows[1][0] == "vid00000002" and rows[1][2] is True
        assert rows[0][2] is False
        await queue.stop()

    asyncio.run(scenario())


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


def test_single_worker_never_runs_two_jobs(cfg, stub):
    """Harte Regel: genau ein Job zur Zeit – nie zwei yt-dlp-Prozesse parallel."""
    stub.data_ref["slow"] = 0.4
    stub.save()

    async def scenario():
        queue = JobQueue(cfg)
        await queue.start()
        await run_discovery(cfg, queue)
        with session_scope() as s:
            ids = list(s.scalars(select(Job.id).order_by(Job.id)).all())
        assert len(ids) == 2
        seen_running = False
        for _ in range(200):  # up to ~10 s, covers both jobs end to end
            with session_scope() as s:
                statuses = [s.get(Job, j).status for j in ids]
            running = statuses.count("running")
            assert running <= 1, f"two jobs running at once: {statuses}"
            seen_running = seen_running or running == 1
            if all(st in ("success", "failed") for st in statuses):
                break
            await asyncio.sleep(0.05)
        assert seen_running, "no job ever entered the running state"
        await queue.wait_for_jobs(ids, poll=0.2)
        await queue.stop()

    asyncio.run(scenario())


def test_restart_requeues_interrupted_jobs(cfg):
    """Simulierter Neustart mitten im Download: interrupted -> neu eingereiht,
    Playlist-States zurückgesetzt, kein doppelter Lauf (SPEC 12/9)."""
    apply_discovery(
        [PlaylistInfo("PLaaa", "Sommer Mix"), PlaylistInfo("PLbbb", "Konzert SETLIST")], "setlist"
    )
    with session_scope() as s:
        for pk in s.scalars(select(Playlist.id)).all():
            s.add(Job(playlist_id=pk, trigger="manual", status="running",
                      queued_at=utcnow(), started_at=utcnow()))
    queue = JobQueue(cfg)
    queue.recover()  # exactly what happens on a fresh start()
    with session_scope() as s:
        jobs = list(s.scalars(select(Job).order_by(Job.id)).all())
        interrupted = [j for j in jobs if j.status == "interrupted"]
        queued = [j for j in jobs if j.status == "queued"]
        assert len(jobs) == 4 and len(interrupted) == 2 and len(queued) == 2
        assert all(j.finished_at is not None for j in interrupted)
        states = {p.type: p.state for p in s.scalars(select(Playlist)).all()}
        new_ids = [j.id for j in queued]
    # recover() resets to new/idle first, enqueue() immediately marks them queued again
    assert states == {"sync": "queued", "oneshot": "queued"}

    async def scenario():
        await queue.start()
        statuses = await queue.wait_for_jobs(new_ids, poll=0.2)
        assert set(statuses.values()) == {"success"}
        await queue.stop()

    asyncio.run(scenario())
    with session_scope() as s:
        # the interrupted runs stay interrupted, only the requeued runs execute
        assert sorted(j.status for j in s.scalars(select(Job)).all()) == [
            "interrupted", "interrupted", "success", "success",
        ]
