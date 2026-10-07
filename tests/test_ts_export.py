# Copyright (c) 2026 Stefan Koelle (https://stefankoelle.de)
# Licensed under the MIT License. See LICENSE file in project root for details.
"""Tests for the TubeSync export (offline SQLite fixtures, never the real TubeSync)."""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from app import ts_export as ts
from app.paths import sanitize_folder_name
from app.ytdlp import read_video_entries

THUMB_BYTES = b"tube-thumb-bytes"

SCHEMA = """
    CREATE TABLE sync_source (
        uuid TEXT PRIMARY KEY, source_type TEXT, key TEXT, name TEXT, directory TEXT
    );
    CREATE TABLE sync_media (
        uuid TEXT PRIMARY KEY, source_id TEXT, key TEXT, title TEXT, duration INTEGER,
        published TEXT, created TEXT, media_file TEXT, thumb TEXT
    );
    CREATE TABLE sync_media_metadata (
        uuid TEXT PRIMARY KEY, site TEXT, key TEXT, created TEXT, value TEXT, media_id TEXT
    );
"""


def video_bytes(vid: str) -> bytes:
    return f"video-{vid}".encode()


def meta_json(
    vid: str,
    *,
    uploader: str = "Channel One",
    channel: str | None = "Channel One TV",
    duration: int = 213,
    upload_date: str = "20231004",
    playlist_index: int | None = None,
    description: str = "a long description nobody exports",
) -> str:
    return json.dumps({
        "id": vid,
        "title": f"Metadata title of {vid}",
        "webpage_url": f"https://www.youtube.com/watch?v={vid}",
        "uploader": uploader,
        "channel": channel,
        "duration": duration,
        "upload_date": upload_date,
        "view_count": 3897156,
        "like_count": 28171,
        "width": 1920,
        "height": 1080,
        "vcodec": "vp09.00.10.08",
        "acodec": "opus",
        "vbr": 1234.5,
        "abr": 128.0,
        "playlist_index": playlist_index,
        "description": description,
        "requested_formats": [{"url": "https://example.invalid/x"}],
    })


def make_ts(root: Path) -> Path:
    """Create a TubeSync shaped tree: ``config/db.sqlite3``, ``downloads/``, thumbs."""
    ts_root = root / "tubesync"
    db = ts_root / "config" / "db.sqlite3"
    db.parent.mkdir(parents=True)
    conn = sqlite3.connect(db)
    conn.executescript(SCHEMA)
    conn.executemany(
        "INSERT INTO sync_source (uuid, source_type, key, name, directory) VALUES (?, ?, ?, ?, ?)",
        [
            ("s1", "p", "PL1", "2024 Metal", "2024-metal"),
            ("s2", "p", "PL2", "Psychdelic Rock", "psychrock"),
            ("s3", "c", "@channel", "Channel Stuff", "channel"),
        ],
    )
    conn.executemany(
        "INSERT INTO sync_media (uuid, source_id, key, title, duration, published, created,"
        " media_file, thumb) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            ("m1", "s1", "vid1", "First: Song", 213, "2023-10-04 00:00:00",
             "2024-03-04 10:32:40.624150", "video/2024/vid1.mkv", "thumbs/18/t1.jpg"),
            ("m2", "s1", "vid2", "Second Song", 100, "2022-07-08 00:00:00",
             "2024-03-05 11:00:00.000000", "video/2024/vid2.mkv", "thumbs/18/t2.jpg"),
            ("m3", "s1", "vid3", "Never Downloaded", 50, "2024-03-06 00:00:00",
             "2024-03-06 11:00:00.000000", "", None),
            ("m8", "s2", "vid8", "Crawled first", 60, "2024-01-01 00:00:00",
             "2024-03-01 00:00:00.000000", "video/2024/vid8.mkv", "thumbs/18/t8.jpg"),
            ("m9", "s2", "vid9", "Crawled second", 70, "2024-01-02 00:00:00",
             "2024-03-02 00:00:00.000000", "video/2024/vid9.mkv", "thumbs/18/t9.jpg"),
            ("mc", "s3", "vidc", "Channel video", 10, None,
             "2024-03-07 00:00:00.000000", "video/2024/vidc.mkv", None),
        ],
    )
    conn.executemany(
        "INSERT INTO sync_media_metadata (uuid, site, key, created, value, media_id)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        [
            ("md1", "Youtube", "vid1", "2024-03-04 10:33:00", meta_json("vid1"), "m1"),
            # newer than md1 but unlinked: the linked row must still win
            ("mdstale", "Youtube", "vid1", "2025-01-01 00:00:00",
             meta_json("vid1", uploader="Stale Channel"), None),
            # no media_id at all: matched by key
            ("md2", "Youtube", "vid2", "2024-03-05 11:01:00",
             meta_json("vid2", uploader="Channel Two", duration=100,
                       upload_date="20220708"), None),
            ("md8", "Youtube", "vid8", "2024-03-01 00:01:00",
             meta_json("vid8", playlist_index=2), "m8"),
            ("md9", "Youtube", "vid9", "2024-03-02 00:01:00",
             meta_json("vid9", playlist_index=1), "m9"),
        ],
    )
    conn.commit()
    conn.close()

    videos = ts_root / "downloads" / "video" / "2024"
    videos.mkdir(parents=True)
    for vid in ("vid1", "vid2", "vid8", "vid9", "vidc"):
        (videos / f"{vid}.mkv").write_bytes(video_bytes(vid))

    thumbs = ts_root / "config" / "media" / "thumbs" / "18"
    thumbs.mkdir(parents=True)
    for thumb in ("t1", "t2", "t8", "t9"):
        (thumbs / f"{thumb}.jpg").write_bytes(THUMB_BYTES)
    return ts_root


def paths_of(root: Path) -> dict[str, Path]:
    return {
        "db": root / "config" / "db.sqlite3",
        "downloads": root / "downloads",
        "thumbs": root / "config" / "media",
    }


def folder_of(tmp_path: Path, title: str = "2024 Metal", pid: str = "PL1") -> Path:
    return tmp_path / "out" / sanitize_folder_name(title, pid)


def export(root: Path, tmp_path: Path, **kwargs) -> ts.ExportReport:
    p = paths_of(root)
    return ts.export(db=p["db"], media_root=p["downloads"], target=tmp_path / "out",
                     thumbs_root=p["thumbs"], **kwargs)


def test_open_db_is_read_only(tmp_path):
    root = make_ts(tmp_path)
    conn = ts.open_db(paths_of(root)["db"])
    try:
        with pytest.raises(sqlite3.Error):
            conn.execute("UPDATE sync_source SET name = 'nope'")
    finally:
        conn.close()


def test_open_db_reports_missing_file(tmp_path):
    with pytest.raises(ts.TsError, match="database not found"):
        ts.open_db(tmp_path / "nope.sqlite3")


def test_open_db_reports_unusable_database(tmp_path):
    broken = tmp_path / "broken.sqlite3"
    broken.write_bytes(b"not a database at all")
    conn = sqlite3.connect(broken)
    conn.close()
    with pytest.raises(ts.TsError):
        conn = ts.open_db(broken)
        try:
            ts.read_sources(conn)
        finally:
            conn.close()


def test_read_library_orders_and_merges_metadata(tmp_path):
    root = make_ts(tmp_path)
    conn = ts.open_db(paths_of(root)["db"])
    try:
        sources, library = ts.read_library(conn)
    finally:
        conn.close()

    assert [(s.playlist_id, s.title, s.source_type) for s in sources] == [
        ("PL1", "2024 Metal", "p"),
        ("@channel", "Channel Stuff", "c"),
        ("PL2", "Psychdelic Rock", "p"),
    ]
    assert [m.video_id for m in library["PL1"]] == ["vid1", "vid2", "vid3"]
    # playlist_index wins over the order TubeSync crawled the videos in
    assert [m.video_id for m in library["PL2"]] == ["vid9", "vid8"]

    vid1, vid2, vid3 = library["PL1"]
    assert vid1.info["uploader"] == "Channel One"  # linked row beats the newer stale one
    assert vid1.playlist_index is None  # metadata says null, so no index
    assert vid2.info["uploader"] == "Channel Two"  # matched by key only
    assert vid3.info == {}  # no metadata row at all
    assert vid3.duration_s == 50
    assert vid3.media_file is None  # empty string means "not downloaded"
    assert library["PL2"][0].playlist_index == 1


def test_prune_info_drops_description_and_blobs():
    info = ts.prune_info(meta_json("vid1"))
    assert "description" not in info and "requested_formats" not in info
    assert info["width"] == 1920 and info["vbr"] == 1234.5
    assert ts.prune_info("not json") == {}
    assert ts.prune_info("[1, 2]") == {}


def test_export_full_layout(tmp_path):
    root = make_ts(tmp_path)
    report = export(root, tmp_path, mode="copy")
    folder = folder_of(tmp_path)
    names = sorted(os.listdir(folder))
    assert "01 - First Song [vid1].mkv" in names
    assert "02 - Second Song [vid2].mkv" in names
    assert "01 - First Song [vid1].info.json" in names
    assert "01 - First Song [vid1].jpg" in names
    assert "manifest.json" in names
    assert not any(n.endswith(".description") for n in names)
    assert (folder / "01 - First Song [vid1].mkv").read_bytes() == video_bytes("vid1")

    info = json.loads((folder / "01 - First Song [vid1].info.json").read_text())
    assert info["title"] == "First: Song"
    assert info["duration"] == 213
    assert info["upload_date"] == "20231004"
    assert info["width"] == 1920 and info["height"] == 1080
    assert info["vcodec"] == "vp09.00.10.08"
    assert info["description"] == ""
    assert info["playlist"] == "PL1" and info["playlist_index"] == 1

    prow = next(p for p in report.playlists if p.playlist_id == "PL1")
    assert (prow.total, prow.copied, prow.missing, prow.failed) == (3, 2, 1, 0)
    archive = (tmp_path / "out" / "archives" / "PL1.txt").read_text().splitlines()
    assert archive == ["youtube vid1", "youtube vid2"]

    gallery = read_video_entries(folder, "PL1")
    assert gallery["exists"] and gallery["video_count"] == 2
    assert gallery["cover"] is None  # TubeSync stores no playlist cover
    assert gallery["videos"][0]["title"] == "First: Song"
    assert gallery["videos"][0]["duration_s"] == 213
    assert gallery["videos"][0]["channel"] == "Channel One TV"


def test_export_counts_missing_files_as_placeholders(tmp_path):
    root = make_ts(tmp_path)
    report = export(root, tmp_path, mode="copy")
    prow = next(p for p in report.playlists if p.playlist_id == "PL1")
    assert prow.missing == 1 and prow.errors == ["vid3: media file not found"]

    folder = folder_of(tmp_path)
    assert "03 - Never Downloaded [vid3].info.json" in os.listdir(folder)
    assert not any(n.startswith("03 -") and n.endswith(".mkv") for n in os.listdir(folder))

    manifest = json.loads((folder / "manifest.json").read_text())
    assert manifest["remote_item_count"] == 3
    assert manifest["entries"][2]["file"] is None
    assert manifest["entries"][2]["downloaded"] is False
    assert manifest["entries"][2]["duration_s"] == 50
    assert manifest["entries"][2]["published"] == "2024-03-06 00:00:00"
    # the playlist row stays "idle" because one file is missing
    assert report.playlists[0].missing == 1


def test_export_manifest_holds_db_import_data(tmp_path):
    root = make_ts(tmp_path)
    export(root, tmp_path, mode="copy")
    manifest = json.loads((folder_of(tmp_path) / "manifest.json").read_text())
    assert manifest["source"] == "tubesync"
    assert manifest["playlist_id"] == "PL1"
    assert manifest["title"] == "2024 Metal"
    assert manifest["folder_name"] == sanitize_folder_name("2024 Metal", "PL1")
    assert manifest["channel"] == ""
    assert [row["position"] for row in manifest["entries"]] == [1, 2, 3]
    assert manifest["entries"][0]["file"] == "01 - First Song [vid1].mkv"
    assert manifest["entries"][0]["uploader"] == "Channel One"


def test_export_playlist_index_wins_over_crawl_order(tmp_path):
    root = make_ts(tmp_path)
    export(root, tmp_path, mode="copy")
    folder = folder_of(tmp_path, title="Psychdelic Rock", pid="PL2")
    names = sorted(os.listdir(folder))
    assert "01 - Crawled second [vid9].mkv" in names
    assert "02 - Crawled first [vid8].mkv" in names


def test_export_is_idempotent_and_never_overwrites(tmp_path):
    root = make_ts(tmp_path)
    first = export(root, tmp_path, mode="copy")
    assert first.totals()["copied"] == 4

    info = folder_of(tmp_path) / "01 - First Song [vid1].info.json"
    info.write_text("keep me")
    second = export(root, tmp_path, mode="copy")
    assert second.totals()["copied"] == 0
    assert second.totals()["skipped"] >= 4
    assert info.read_text() == "keep me"
    archive = (tmp_path / "out" / "archives" / "PL1.txt").read_text().splitlines()
    assert archive == ["youtube vid1", "youtube vid2"]


def test_export_metadata_only_skips_videos(tmp_path):
    root = make_ts(tmp_path)
    report = export(root, tmp_path, metadata_only=True)
    folder = folder_of(tmp_path)
    names = os.listdir(folder)
    assert not any(n.endswith(".mkv") for n in names)
    assert "01 - First Song [vid1].info.json" in names
    assert "01 - First Song [vid1].jpg" in names
    assert report.totals()["copied"] == 0
    assert report.totals()["skipped"] == 4
    assert report.missing == 1
    manifest = json.loads((folder / "manifest.json").read_text())
    assert manifest["metadata_only"] is True
    assert manifest["entries"][0]["file"] == "01 - First Song [vid1].mkv"


def test_export_dry_run_writes_nothing(tmp_path):
    root = make_ts(tmp_path)
    target = tmp_path / "out"
    report = export(root, tmp_path, dry_run=True)
    assert not target.exists()
    assert report.dry_run is True
    assert report.totals()["copied"] == 4
    expected = sum(len(video_bytes(v)) for v in ("vid1", "vid2", "vid8", "vid9"))
    assert report.totals()["bytes"] == expected + 4 * len(THUMB_BYTES)


def test_export_auto_hardlinks_by_default(tmp_path):
    root = make_ts(tmp_path)
    report = export(root, tmp_path)
    totals = report.totals()
    assert totals["hardlinked"] == 4 and totals["copied"] == 0 and totals["failed"] == 0
    src = paths_of(root)["downloads"] / "video" / "2024" / "vid1.mkv"
    dst = folder_of(tmp_path) / "01 - First Song [vid1].mkv"
    assert dst.stat().st_ino == src.stat().st_ino
    assert src.read_bytes() == video_bytes("vid1")


def test_export_auto_falls_back_to_copy(tmp_path, monkeypatch):
    root = make_ts(tmp_path)

    def refuse(*_args, **_kwargs):
        raise OSError("hardlink refused by filesystem")

    monkeypatch.setattr(os, "link", refuse)
    report = export(root, tmp_path)
    totals = report.totals()
    assert totals["hardlinked"] == 0
    assert totals["copied"] == 4 and totals["failed"] == 0
    dst = folder_of(tmp_path) / "01 - First Song [vid1].mkv"
    assert dst.read_bytes() == video_bytes("vid1")


def test_export_without_archives(tmp_path):
    root = make_ts(tmp_path)
    export(root, tmp_path, with_archives=False)
    assert not (tmp_path / "out" / "archives").exists()


def test_export_filters_playlists_and_warns(tmp_path, caplog):
    root = make_ts(tmp_path)
    with caplog.at_level("WARNING"):
        report = export(root, tmp_path, mode="copy", playlist_ids=["PL2", "PL404"])
    assert [p.playlist_id for p in report.playlists] == ["PL2"]
    assert "PL404" in caplog.text
    assert not folder_of(tmp_path).exists()
    assert "not a playlist" in caplog.text  # the channel source is skipped loudly


def test_export_rejects_bad_mode(tmp_path):
    root = make_ts(tmp_path)
    with pytest.raises(ValueError, match="unsupported mode"):
        export(root, tmp_path, mode="symlink")


def test_export_missing_database_raises(tmp_path):
    with pytest.raises(ts.TsError, match="database not found"):
        ts.export(
            db=tmp_path / "nope.sqlite3",
            media_root=tmp_path,
            target=tmp_path / "out",
        )


def test_build_info_json_falls_back_to_media_row(tmp_path):
    root = make_ts(tmp_path)
    conn = ts.open_db(paths_of(root)["db"])
    try:
        _, library = ts.read_library(conn)
    finally:
        conn.close()
    media = next(m for m in library["PL1"] if m.video_id == "vid3")
    source = ts.TsSource(playlist_id="PL1", title="2024 Metal", directory="d", source_type="p")
    info = ts.build_info_json(media, source, 3, ".mkv")
    assert info["title"] == "Never Downloaded"
    assert info["duration"] == 50
    assert info["upload_date"] == "20240306"  # derived from the published date
    assert info["uploader"] is None and info["channel"] is None
    assert info["ext"] == "mkv"
    assert info["description"] == ""


def test_cli_requires_options(tmp_path, capsys, monkeypatch):
    for key in ("TS_DB", "TS_MEDIA_ROOT", "TS_THUMBS_ROOT", "TS_EXPORT_TARGET"):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(SystemExit) as exc:
        ts.main(["--env-file", str(tmp_path / "missing.env")])
    assert exc.value.code == 2
    assert "missing required options" in capsys.readouterr().err


def test_cli_missing_database_returns_two(tmp_path, monkeypatch):
    for key in ("TS_DB", "TS_MEDIA_ROOT", "TS_THUMBS_ROOT", "TS_EXPORT_TARGET"):
        monkeypatch.delenv(key, raising=False)
    code = ts.main([
        "--env-file", str(tmp_path / "missing.env"),
        "--db", str(tmp_path / "nope.sqlite3"),
        "--media-root", str(tmp_path),
        "--target", str(tmp_path / "out"),
    ])
    assert code == 2


def test_cli_reports_the_table(tmp_path, capsys, monkeypatch):
    root = make_ts(tmp_path)
    p = paths_of(root)
    for key in ("TS_DB", "TS_MEDIA_ROOT", "TS_THUMBS_ROOT", "TS_EXPORT_TARGET"):
        monkeypatch.delenv(key, raising=False)
    code = ts.main([
        "--env-file", str(tmp_path / "missing.env"),
        "--db", str(p["db"]), "--media-root", str(p["downloads"]),
        "--thumbs-root", str(p["thumbs"]), "--target", str(tmp_path / "out"),
        "--mode", "copy",
    ])
    out = capsys.readouterr().out
    assert code == 0
    assert "2024 Metal" in out and "linked=0" in out
    assert "failed=0" in out and "dry-run" not in out


def test_export_tree_imports_into_the_database(tmp_path):
    pytest.importorskip("sqlalchemy")
    pytest.importorskip("alembic")
    pytest.importorskip("pydantic_settings")
    from sqlalchemy import select

    from app.config import Settings, ensure_dirs
    from app.db import init_engine, migrate, session_scope
    from app.models import Playlist, PlaylistEntry
    from app.ta_import import apply_import, scan_manifests

    c = Settings(
        youtube_channel="@beispielkanal", data_dir=tmp_path / "data", config_dir=tmp_path / "config",
        sleep_min=0, sleep_max=0, job_gap_min=0, job_gap_max=0,
    )
    ensure_dirs(c)
    c.data_dir.mkdir(parents=True, exist_ok=True)
    init_engine(c.db_url)
    migrate(c.db_url)

    root = make_ts(tmp_path)
    p = paths_of(root)
    report = ts.export(
        db=p["db"], media_root=p["downloads"], target=c.data_dir, thumbs_root=p["thumbs"],
        mode="copy",
    )
    assert report.failed == 0
    manifests = scan_manifests(c.data_dir)
    assert [m.playlist_id for m in manifests] == ["PL1", "PL2"]
    imported = apply_import(manifests, data_dir=c.data_dir, config_dir=c.config_dir)
    assert imported.errors == []

    with session_scope() as s:
        pl = s.scalar(select(Playlist).where(Playlist.playlist_id == "PL1"))
        assert (pl.type, pl.state, pl.remote_status) == ("oneshot", "idle", "active")
        assert pl.title == "2024 Metal"
        assert pl.folder_name == sanitize_folder_name("2024 Metal", "PL1")
        assert (pl.remote_item_count, pl.downloaded_count) == (3, 2)
        rows = s.scalars(
            select(PlaylistEntry).where(PlaylistEntry.playlist_id == pl.id)
            .order_by(PlaylistEntry.position)
        ).all()
        assert [(r.position, r.video_id) for r in rows] == [
            (1, "vid1"), (2, "vid2"), (3, "vid3"),
        ]
        done = s.scalar(select(Playlist).where(Playlist.playlist_id == "PL2"))
        assert (done.state, done.downloaded_count, done.remote_item_count) == ("done", 2, 2)
        assert done.completed_at is not None
