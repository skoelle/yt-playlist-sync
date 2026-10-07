# Copyright (c) 2026 Stefan Koelle (https://stefankoelle.de)
# Licensed under the MIT License. See LICENSE file in project root for details.
"""Tests for the offline forget maintenance CLI (no network, tmp paths only)."""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("sqlalchemy")
pytest.importorskip("alembic")
pytest.importorskip("pydantic_settings")

from sqlalchemy import select  # noqa: E402

from app.config import Settings, ensure_dirs  # noqa: E402
from app.db import init_engine, migrate, session_scope, utcnow  # noqa: E402
from app.forget import apply_forget, collect, main  # noqa: E402
from app.models import Job, Playlist, PlaylistEntry  # noqa: E402

PID = "PL1"
FOLDER = "Sommer Mix [PL1]"


@pytest.fixture
def cfg(tmp_path):
    c = Settings(
        youtube_channel="@beispielkanal",
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "config",
        backup_dir=tmp_path / "backup",
        sleep_min=0,
        sleep_max=0,
        job_gap_min=0,
        job_gap_max=0,
    )
    ensure_dirs(c)
    c.data_dir.mkdir(parents=True, exist_ok=True)
    init_engine(c.db_url)
    migrate(c.db_url)
    return c


def seed(
    cfg: Settings,
    *,
    pid: str = PID,
    folder: str = FOLDER,
    state: str = "idle",
    entries: int = 2,
    job_status: str | None = None,
    row: bool = True,
    folder_name: str | None = None,
    archives: bool = True,
) -> Path:
    """Create a playlist with entries, an optional job, folder and both archives."""
    path = cfg.data_dir / folder
    path.mkdir(parents=True, exist_ok=True)
    (path / "vid00000001.mkv").write_bytes(b"x")
    if archives:
        (cfg.archives_dir / f"{pid}.txt").write_text("youtube vid00000001\n", encoding="utf-8")
        data_archives = cfg.data_dir / "archives"
        data_archives.mkdir(parents=True, exist_ok=True)
        (data_archives / f"{pid}.txt").write_text("youtube vid00000002\n", encoding="utf-8")
    if not row:
        return path
    now = utcnow()
    with session_scope() as s:
        pl = Playlist(
            playlist_id=pid,
            title=folder.split(" [")[0],
            type="sync",
            folder_name=folder if folder_name is None else folder_name,
            state=state,
            remote_status="active",
            ignored=False,
            remote_item_count=entries,
            downloaded_count=entries,
            first_seen_at=now,
            last_seen_at=now,
        )
        s.add(pl)
        s.flush()
        for i in range(entries):
            s.add(
                PlaylistEntry(
                    playlist_id=pl.id,
                    video_id=f"vid{i + 1:08d}",
                    position=i + 1,
                    title=f"Video {i + 1}",
                    last_seen_at=now,
                )
            )
        if job_status:
            s.add(
                Job(
                    playlist_id=pl.id,
                    trigger="manual",
                    status=job_status,
                    priority=0,
                    queued_at=now,
                )
            )
    return path


def db_state(cfg: Settings) -> tuple[int, int, int]:
    with session_scope() as s:
        playlists = len(s.scalars(select(Playlist)).all())
        entries = len(s.scalars(select(PlaylistEntry)).all())
        jobs = len(s.scalars(select(Job)).all())
    return playlists, entries, jobs


def test_dry_run_changes_nothing(cfg: Settings):
    seed(cfg)
    report = apply_forget(cfg, [PID], dry_run=True)
    assert report.dry_run is True
    assert report.backups == []
    assert report.errors == []
    prow = report.playlists[0]
    assert prow.action == "remove"
    assert prow.folder_action == "quarantine"
    assert (prow.entries, prow.jobs, prow.folders, prow.archives) == (2, 0, 1, 2)
    assert (cfg.data_dir / FOLDER).is_dir()
    assert (cfg.archives_dir / "PL1.txt").is_file()
    assert (cfg.data_dir / "archives" / "PL1.txt").is_file()
    assert db_state(cfg) == (1, 2, 0)
    assert not cfg.backup_dir.exists()


def test_apply_removes_rows_archives_and_quarantines_folder(cfg: Settings):
    seed(cfg, job_status="success")
    report = apply_forget(cfg, [PID], dry_run=False)
    assert report.errors == []
    prow = report.playlists[0]
    assert prow.action == "removed"
    assert (prow.entries, prow.jobs, prow.folders, prow.archives) == (2, 1, 1, 2)
    assert db_state(cfg) == (0, 0, 0)
    assert not (cfg.data_dir / FOLDER).exists()
    moved = cfg.data_dir / ".quarantine" / FOLDER
    assert (moved / "vid00000001.mkv").is_file()
    assert not (cfg.archives_dir / "PL1.txt").exists()
    assert not (cfg.data_dir / "archives" / "PL1.txt").exists()
    backups = sorted(p.name for p in cfg.backup_dir.iterdir())
    assert any(name.startswith("app-") and name.endswith(".db") for name in backups)
    assert any(name.startswith("archives-") for name in backups)
    assert len(report.backups) == 2


def test_blocked_by_running_job(cfg: Settings):
    seed(cfg, job_status="running")
    report = apply_forget(cfg, [PID], dry_run=False)
    prow = report.playlists[0]
    assert prow.action == "blocked"
    assert report.errors and "open job" in report.errors[0]
    assert report.backups == []
    assert db_state(cfg) == (1, 2, 1)
    assert (cfg.data_dir / FOLDER).is_dir()
    assert (cfg.archives_dir / "PL1.txt").is_file()


def test_blocked_by_running_state(cfg: Settings):
    seed(cfg, state="running")
    report = apply_forget(cfg, [PID], dry_run=False)
    assert report.playlists[0].action == "blocked"
    assert report.errors
    assert db_state(cfg) == (1, 2, 0)


def test_missing_row_still_cleans_folder_and_archives(cfg: Settings):
    seed(cfg, row=False)
    report = apply_forget(cfg, [PID], dry_run=False)
    prow = report.playlists[0]
    assert prow.action == "missing"
    assert report.errors == []
    assert any("no database row" in w for w in report.warnings)
    assert not (cfg.data_dir / FOLDER).exists()
    assert (cfg.data_dir / ".quarantine" / FOLDER).is_dir()
    assert not (cfg.archives_dir / "PL1.txt").exists()
    assert not (cfg.data_dir / "archives" / "PL1.txt").exists()


def test_keep_folder_warns_about_duplicates(cfg: Settings):
    seed(cfg)
    report = apply_forget(cfg, [PID], dry_run=False, folder_mode="keep")
    prow = report.playlists[0]
    assert prow.folder_action == "keep"
    assert prow.folders == 1
    assert any("duplicate files" in w for w in report.warnings)
    assert (cfg.data_dir / FOLDER).is_dir()
    assert db_state(cfg) == (0, 0, 0)


def test_delete_folder_needs_yes(cfg: Settings, monkeypatch: pytest.MonkeyPatch):
    seed(cfg)
    with pytest.raises(SystemExit) as exc:
        main(["--playlist", PID, "--delete-folder", "--apply"])
    assert exc.value.code == 2
    assert (cfg.data_dir / FOLDER).is_dir()

    monkeypatch.setenv("YOUTUBE_CHANNEL", "@beispielkanal")
    monkeypatch.setenv("DATA_DIR", str(cfg.data_dir))
    monkeypatch.setenv("CONFIG_DIR", str(cfg.config_dir))
    monkeypatch.setenv("BACKUP_DIR", str(cfg.backup_dir))
    code = main(["--playlist", PID, "--delete-folder", "--yes", "--apply", "--no-backup"])
    assert code == 0
    assert not (cfg.data_dir / FOLDER).exists()
    assert not (cfg.data_dir / ".quarantine").exists()
    assert db_state(cfg) == (0, 0, 0)


def test_quarantine_collision_leaves_playlist_alone(cfg: Settings):
    seed(cfg)
    (cfg.data_dir / ".quarantine" / FOLDER).mkdir(parents=True)
    report = apply_forget(cfg, [PID], dry_run=False)
    prow = report.playlists[0]
    assert prow.action == "error"
    assert any("quarantine target already exists" in e for e in report.errors)
    assert (cfg.data_dir / FOLDER).is_dir()
    assert db_state(cfg) == (1, 2, 0)


def test_no_backup_flag(cfg: Settings):
    seed(cfg)
    report = apply_forget(cfg, [PID], dry_run=False, make_backup=False)
    assert report.backups == []
    assert not cfg.backup_dir.exists()
    assert db_state(cfg) == (0, 0, 0)


def test_unknown_playlist_is_only_a_warning(cfg: Settings):
    report = apply_forget(cfg, ["PL404"], dry_run=True)
    prow = report.playlists[0]
    assert prow.action == "missing"
    assert prow.folders == 0 and prow.archives == 0
    assert report.errors == []
    assert any("PL404" in w for w in report.warnings)


def test_folder_outside_data_dir_is_refused(cfg: Settings, tmp_path: Path):
    seed(cfg, folder_name="../outside")
    outside = cfg.data_dir.parent / "outside"
    outside.mkdir()
    report = apply_forget(cfg, [PID], dry_run=False)
    prow = report.playlists[0]
    assert prow.action == "error"
    assert any("outside" in e for e in report.errors)
    assert outside.is_dir()
    assert db_state(cfg) == (1, 2, 0)
    assert (cfg.archives_dir / "PL1.txt").is_file()


def test_one_blocked_playlist_does_not_stop_the_others(cfg: Settings):
    seed(cfg, pid="PL1", folder="Mix A [PL1]")
    seed(cfg, pid="PL2", folder="Mix B [PL2]", state="queued")
    report = apply_forget(cfg, ["PL1", "PL2"], dry_run=False)
    by_id = {p.playlist_id: p for p in report.playlists}
    assert by_id["PL1"].action == "removed"
    assert by_id["PL2"].action == "blocked"
    assert len(report.errors) == 1
    with session_scope() as s:
        left = [p.playlist_id for p in s.scalars(select(Playlist)).all()]
    assert left == ["PL2"]
    assert (cfg.data_dir / "Mix B [PL2]").is_dir()


def test_scan_finds_folders_without_the_db_folder_name(cfg: Settings):
    seed(cfg, folder=FOLDER)
    extra = cfg.data_dir / "Alt [PL1]"
    extra.mkdir()
    (extra / "clip.mkv").write_bytes(b"y")
    report = apply_forget(cfg, [PID], dry_run=False)
    prow = report.playlists[0]
    assert prow.action == "removed"
    assert prow.folders == 2
    assert not extra.exists()
    assert (cfg.data_dir / ".quarantine" / "Alt [PL1]").is_dir()


def test_collect_reports_open_jobs(cfg: Settings):
    seed(cfg, job_status="queued")
    targets, warnings = collect(cfg, [PID])
    assert warnings == []
    assert targets[0].open_jobs == 1
    assert targets[0].blocked is True
    assert targets[0].worth_doing is True


def test_main_reports_dry_run_by_default(cfg: Settings, monkeypatch: pytest.MonkeyPatch, capsys):
    seed(cfg)
    monkeypatch.setenv("YOUTUBE_CHANNEL", "@beispielkanal")
    monkeypatch.setenv("DATA_DIR", str(cfg.data_dir))
    monkeypatch.setenv("CONFIG_DIR", str(cfg.config_dir))
    monkeypatch.setenv("BACKUP_DIR", str(cfg.backup_dir))
    code = main(["--playlist", PID])
    out = capsys.readouterr().out
    assert code == 0
    assert "(dry-run)" in out
    assert "playlists=1 removed=1 blocked=0" in out
    assert db_state(cfg) == (1, 2, 0)


def test_main_requires_playlist_argument():
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 2
