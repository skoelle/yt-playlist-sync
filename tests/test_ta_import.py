# Copyright (c) 2026 Stefan Koelle (https://stefankoelle.de)
# Licensed under the MIT License. See LICENSE file in project root for details.
"""Tests for the offline import of an exported tree (no network, tmp paths only)."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

pytest.importorskip("sqlalchemy")
pytest.importorskip("alembic")
pytest.importorskip("pydantic_settings")

from sqlalchemy import select  # noqa: E402

from app.config import Settings, ensure_dirs  # noqa: E402
from app.db import init_engine, migrate, session_scope, utcnow  # noqa: E402
from app.models import Playlist, PlaylistEntry  # noqa: E402
from app.ta_import import (  # noqa: E402
    Manifest,
    apply_import,
    main,
    resolve_folder,
    scan_manifests,
    select_manifests,
)

FOLDER = "Sommer Mix [PL1]"


@pytest.fixture
def cfg(tmp_path):
    c = Settings(
        youtube_channel="@beispielkanal", data_dir=tmp_path / "data", config_dir=tmp_path / "config",
        sleep_min=0, sleep_max=0, job_gap_min=0, job_gap_max=0,
    )
    ensure_dirs(c)
    c.data_dir.mkdir(parents=True, exist_ok=True)
    init_engine(c.db_url)
    migrate(c.db_url)
    return c


def write_export(
    root: Path,
    folder: str = FOLDER,
    pid: str = "PL1",
    title: str = "Sommer Mix",
    vids: tuple[str, ...] = ("vid00000001", "vid00000002"),
    videos: tuple[bool, ...] | bool = True,
    extra_entries: tuple[dict, ...] = (),
) -> Path:
    """Write one exported folder: manifest.json plus (optionally) the video files."""
    flags = [videos] * len(vids) if isinstance(videos, bool) else list(videos)
    d = root / folder
    d.mkdir(parents=True, exist_ok=True)
    rows = []
    for i, (vid, has_file) in enumerate(zip(vids, flags, strict=True), 1):
        name = f"{i:02d} - Song {i} [{vid}].mp4"
        if has_file:
            (d / name).write_bytes(b"x" * 16)
        rows.append(
            {
                "position": i, "video_id": vid, "title": f"Song {i}", "uploader": "Ch",
                "duration_s": 60 + i, "downloaded": has_file, "file": name if has_file else None,
            }
        )
    rows.extend(extra_entries)
    manifest = {
        "source": "tubearchivist", "playlist_id": pid, "title": title, "folder_name": folder,
        "remote_item_count": len(rows), "entries": rows,
    }
    (d / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return d


def move_into_place(cfg, export: Path, folder: str = FOLDER) -> None:
    """The user moves the exported folder into the data dir before importing."""
    shutil.copytree(export / folder, cfg.data_dir / folder)


def playlist_row(pid: str = "PL1"):
    with session_scope() as s:
        pl = s.scalar(select(Playlist).where(Playlist.playlist_id == pid))
        if pl is None:
            return None
        return (
            pl.type, pl.state, pl.remote_status, pl.downloaded_count, pl.folder_name,
            pl.size_bytes, pl.completed_at is not None, pl.title, pl.skipped_count,
        )


def entry_rows(pid: str = "PL1"):
    with session_scope() as s:
        pl = s.scalar(select(Playlist).where(Playlist.playlist_id == pid))
        rows = s.scalars(
            select(PlaylistEntry).where(PlaylistEntry.playlist_id == pl.id)
            .order_by(PlaylistEntry.position, PlaylistEntry.video_id)
        ).all()
        return [
            (r.video_id, r.position, r.title, r.reason, r.unavailable, r.remote_present)
            for r in rows
        ]


def test_scan_manifests_skips_broken_files(tmp_path):
    write_export(tmp_path / "export", folder="A [PL1]", pid="PL1")
    write_export(tmp_path / "export", folder="B [PL2]", pid="PL2", title="Andere")
    errors: list[str] = []
    manifests = scan_manifests(tmp_path / "export", errors)
    assert [m.playlist_id for m in manifests] == ["PL1", "PL2"]
    assert errors == []
    assert manifests[1].title == "Andere" and manifests[1].entries[0]["video_id"] == "vid00000001"

    broken = tmp_path / "export" / "C [PL3]"
    broken.mkdir()
    (broken / "manifest.json").write_text("{not json", encoding="utf-8")
    incomplete = tmp_path / "export" / "D [PL4]"
    incomplete.mkdir()
    (incomplete / "manifest.json").write_text(json.dumps({"title": "ohne id"}), encoding="utf-8")
    manifests = scan_manifests(tmp_path / "export", errors)
    assert [m.playlist_id for m in manifests] == ["PL1", "PL2"]
    assert len(errors) == 2


def test_select_manifests_allow_list_and_skip(tmp_path):
    write_export(tmp_path / "export", folder="A [PL1]", pid="PL1")
    write_export(tmp_path / "export", folder="B [PL2]", pid="PL2")
    manifests = scan_manifests(tmp_path / "export")
    assert [m.playlist_id for m in manifests] == ["PL1", "PL2"]

    selected, unknown = select_manifests(manifests, ["PL2"], None)
    assert [m.playlist_id for m in selected] == ["PL2"] and unknown == []
    selected, unknown = select_manifests(manifests, ["PL9"], None)
    assert selected == [] and unknown == ["PL9"]
    selected, unknown = select_manifests(manifests, None, ["PL1"])
    assert [m.playlist_id for m in selected] == ["PL2"] and unknown == []
    # the allow-list wins over --skip
    selected, unknown = select_manifests(manifests, ["PL1"], ["PL1"])
    assert [m.playlist_id for m in selected] == ["PL1"] and unknown == []


def test_resolve_folder_keeps_database_name(tmp_path):
    data = tmp_path / "data"
    manifest = Manifest("PL1", "Titel", "New [PL1]", [])
    (data / "Old [PL1]").mkdir(parents=True)
    (data / "New [PL1]").mkdir(parents=True)
    name, path, warning = resolve_folder(data, "Old [PL1]", manifest)
    assert name == "Old [PL1]" and path == data / "Old [PL1]"
    assert warning is not None and "keeping Old [PL1]" in warning

    # the database folder is gone: the manifest folder on disk wins
    name, path, warning = resolve_folder(data, "Gone [PL1]", manifest)
    assert name == "New [PL1]" and path == data / "New [PL1]"
    assert warning is not None and "database had Gone [PL1]" in warning

    # nothing on disk yet
    name, path, warning = resolve_folder(tmp_path / "missing", None, manifest)
    assert name == "New [PL1]" and warning is not None and "not found" in warning


def test_import_creates_oneshot_done_with_entries(cfg, tmp_path):
    export = tmp_path / "export"
    write_export(export)
    (export / "archives").mkdir()
    (export / "archives" / "PL1.txt").write_text("youtube vid00000001\n")
    move_into_place(cfg, export)

    source = export / "archives" / "PL1.txt"
    before = source.read_bytes()
    manifests = scan_manifests(export)
    report = apply_import(manifests, data_dir=cfg.data_dir, config_dir=cfg.config_dir)
    assert report.playlists[0].action == "new" and report.playlists[0].state == "done"
    assert report.playlists[0].archive_merged == 2
    assert report.errors == [] and report.warnings == []

    row = playlist_row()
    assert row[0:4] == ("oneshot", "done", "active", 2)
    assert row[4] == FOLDER and row[5] > 0 and row[6] is True
    assert row[7] == "Sommer Mix"
    assert entry_rows() == [
        ("vid00000001", 1, "Song 1", None, False, True),
        ("vid00000002", 2, "Song 2", None, False, True),
    ]
    # every local file gets an append-only archive line in config/archives
    assert (cfg.archives_dir / "PL1.txt").read_text().splitlines() == [
        "youtube vid00000001", "youtube vid00000002",
    ]
    assert source.read_bytes() == before  # the export stays byte-identical


def test_import_incomplete_files_stay_idle_and_pending(cfg, tmp_path):
    export = tmp_path / "export"
    write_export(export, videos=(True, False))  # only the first video was exported
    move_into_place(cfg, export)

    report = apply_import(scan_manifests(export), data_dir=cfg.data_dir, config_dir=cfg.config_dir)
    prow = report.playlists[0]
    assert prow.state == "idle" and prow.files == 1 and prow.archive_merged == 1
    row = playlist_row()
    assert row[0:4] == ("oneshot", "idle", "active", 1)
    assert row[6] is False  # no completed_at without a complete folder
    # the missing entry stays without reason: the gallery shows it as pending
    assert entry_rows()[1] == ("vid00000002", 2, "Song 2", None, False, True)
    assert (cfg.archives_dir / "PL1.txt").read_text() == "youtube vid00000001\n"


def test_import_update_never_downgrades_done_or_failed(cfg, tmp_path):
    export = tmp_path / "export"
    write_export(export, videos=(True, False))  # incomplete folder
    move_into_place(cfg, export)
    now = utcnow()
    with session_scope() as s:
        s.add(
            Playlist(
                playlist_id="PL1", title="Fertig", type="oneshot", folder_name=FOLDER,
                state="done", remote_status="active", downloaded_count=2,
                first_seen_at=now, last_seen_at=now, completed_at=now,
            )
        )

    report = apply_import(scan_manifests(export), data_dir=cfg.data_dir, config_dir=cfg.config_dir)
    prow = report.playlists[0]
    assert prow.action == "updated" and prow.state == "done"
    row = playlist_row()
    assert row[0:4] == ("oneshot", "done", "active", 1)  # counts follow the files
    assert row[6] is True  # completed_at stays

    with session_scope() as s:
        pl = s.scalar(select(Playlist).where(Playlist.playlist_id == "PL1"))
        pl.state = "failed"

    apply_import(scan_manifests(export), data_dir=cfg.data_dir, config_dir=cfg.config_dir)
    assert playlist_row()[1] == "failed"  # an update never clears a failure either


def test_import_dry_run_changes_nothing(cfg, tmp_path):
    export = tmp_path / "export"
    write_export(export)
    (export / "archives").mkdir()
    (export / "archives" / "PL1.txt").write_text("youtube vid00000001\n")
    move_into_place(cfg, export)

    report = apply_import(
        scan_manifests(export), data_dir=cfg.data_dir, config_dir=cfg.config_dir, dry_run=True,
    )
    assert report.dry_run is True
    assert report.playlists[0].action == "new" and report.playlists[0].state == "done"
    assert report.playlists[0].archive_merged == 2
    assert playlist_row() is None
    assert not (cfg.archives_dir / "PL1.txt").exists()
    with session_scope() as s:
        assert s.scalars(select(PlaylistEntry)).all() == []


def test_import_merges_archive_and_never_touches_source(cfg, tmp_path):
    export = tmp_path / "export"
    write_export(export)
    (export / "archives").mkdir()
    source = export / "archives" / "PL1.txt"
    source.write_text("youtube vid00000001\nyoutube vid00000002\n")
    before = source.read_bytes()
    cfg.archives_dir.mkdir(parents=True, exist_ok=True)
    (cfg.archives_dir / "PL1.txt").write_text("youtube vid00000002\n")
    move_into_place(cfg, export)

    report = apply_import(scan_manifests(export), data_dir=cfg.data_dir, config_dir=cfg.config_dir)
    assert report.playlists[0].archive_merged == 1
    assert (cfg.archives_dir / "PL1.txt").read_text().splitlines() == [
        "youtube vid00000002", "youtube vid00000001",
    ]
    assert source.read_bytes() == before  # the export stays byte-identical


def test_import_updates_existing_and_keeps_youtube_knowledge(cfg, tmp_path):
    export = tmp_path / "export"
    write_export(export)
    move_into_place(cfg, export)
    with session_scope() as s:
        pl = Playlist(
            playlist_id="PL1", title="Alt", type="sync", folder_name=FOLDER, state="idle",
            remote_status="removed", downloaded_count=5, first_seen_at=utcnow(),
            last_seen_at=utcnow(),
        )
        s.add(pl)
        s.flush()
        s.add(
            PlaylistEntry(
                playlist_id=pl.id, video_id="vid00000001", position=1, title="Alt",
                reason="HTTP Error 404", last_seen_at=utcnow(),
            )
        )
        s.add(
            PlaylistEntry(
                playlist_id=pl.id, video_id="vid99999999", position=9, title="Alte Zeile",
                remote_present=False, reason="Video unavailable", last_seen_at=utcnow(),
            )
        )

    report = apply_import(scan_manifests(export), data_dir=cfg.data_dir, config_dir=cfg.config_dir)
    assert report.playlists[0].action == "updated"
    row = playlist_row()
    assert row[0:4] == ("oneshot", "done", "removed", 2)  # remote_status is never reset
    assert row[4] == FOLDER and row[7] == "Sommer Mix" and row[8] == 0
    rows = entry_rows()
    assert len(rows) == 3  # nothing is deleted
    assert rows[0] == ("vid00000001", 1, "Song 1", "HTTP Error 404", False, True)
    assert rows[1] == ("vid00000002", 2, "Song 2", None, False, True)
    assert rows[2] == ("vid99999999", 9, "Alte Zeile", "Video unavailable", False, False)


def test_import_skips_playlist_with_open_job(cfg, tmp_path):
    export = tmp_path / "export"
    write_export(export, title="Neu")
    with session_scope() as s:
        s.add(
            Playlist(
                playlist_id="PL1", title="Laeuft", type="sync", folder_name=FOLDER, state="running",
                remote_status="active", first_seen_at=utcnow(), last_seen_at=utcnow(),
            )
        )

    report = apply_import(scan_manifests(export), data_dir=cfg.data_dir, config_dir=cfg.config_dir)
    assert report.playlists[0].action == "skipped"
    assert any("open job" in w for w in report.warnings)
    row = playlist_row()
    assert row[0] == "sync" and row[1] == "running" and row[7] == "Laeuft"
    with session_scope() as s:
        assert s.scalars(select(PlaylistEntry)).all() == []


def test_cli_import_reports_and_exits(tmp_path, monkeypatch, capsys):
    export = tmp_path / "export"
    write_export(export)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.delenv("YOUTUBE_CHANNEL", raising=False)
    (tmp_path / "data").mkdir()
    shutil.copytree(export / FOLDER, tmp_path / "data" / FOLDER)

    assert main(["--source", str(export)]) == 0
    out = capsys.readouterr().out
    assert FOLDER in out and "new" in out and "dry-run" not in out
    assert playlist_row()[0:3] == ("oneshot", "done", "active")

    # unknown ids are reported, the rest is imported
    assert main(["--source", str(export), "--playlist", "PL9", "--playlist", "PL1"]) == 0
    out = capsys.readouterr().out
    assert "no manifest for playlist PL9" in out and "updated" in out

    # nothing selected at all is an error
    assert main(["--source", str(export), "--playlist", "PL9"]) == 2
    capsys.readouterr()
    # a missing source folder is an error too
    assert main(["--source", str(tmp_path / "nope")]) == 2
    capsys.readouterr()
