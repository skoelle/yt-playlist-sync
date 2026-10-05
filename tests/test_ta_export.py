# Copyright (c) 2026 Stefan Koelle (https://stefankoelle.de)
# Licensed under the MIT License. See LICENSE file in project root for details.
"""Tests for the TubeArchivist export (MockTransport only, never the real network)."""
from __future__ import annotations

import errno
import json
import os
from pathlib import Path

import httpx
import pytest

from app import ta_export as ta
from app.paths import sanitize_folder_name
from app.ytdlp import read_video_entries

MEDIA_BYTES = b"fake-video-bytes"
THUMB_BYTES = b"fake-thumb-bytes"
COVER_BYTES = b"fake-cover-bytes"


def playlist_payload(pid: str = "PL1", title: str = "2017/07") -> dict:
    return {
        "playlist_id": pid,
        "playlist_name": title,
        "playlist_channel": "Stefan Koelle",
        "playlist_last_refresh": "2026-10-05T02:01:54+00:00",
        "playlist_entries": [
            {"youtube_id": "vid1", "title": "First: Song", "uploader": "Channel One",
             "idx": 0, "downloaded": True},
            {"youtube_id": "vid2", "title": "Second Song", "uploader": "Channel Two",
             "idx": 1, "downloaded": True},
            {"youtube_id": "vid3", "title": "Never downloaded", "uploader": "Channel Three",
             "idx": 2, "downloaded": False},
        ],
    }


def video_payload(vid: str = "vid1") -> dict:
    return {
        "youtube_id": vid,
        "title": f"Title of {vid}",
        "description": f"Description of {vid}",
        "published": "2012-04-11T11:08:57+00:00",
        "media_url": f"/youtube/CHAN1/{vid}.mp4",
        "vid_thumb_url": f"/cache/videos/v/{vid}.jpg",
        "player": {"duration": 213},
        "stats": {"view_count": 3897156, "like_count": 28171},
        "streams": [
            {"type": "video", "codec": "vp9", "width": 640, "height": 356, "bitrate": 312112},
            {"type": "audio", "codec": "opus", "bitrate": 124418},
        ],
    }


def make_tree(tmp_path: Path) -> Path:
    root = tmp_path / "ta"
    media = root / "media" / "CHAN1"
    media.mkdir(parents=True)
    (media / "vid1.mp4").write_bytes(MEDIA_BYTES)
    (media / "vid2.mp4").write_bytes(MEDIA_BYTES)
    (media / "notes.txt").write_text("not a video")
    thumbs = root / "cache" / "videos" / "v"
    thumbs.mkdir(parents=True)
    (thumbs / "vid1.jpg").write_bytes(THUMB_BYTES)
    (thumbs / "vid2.jpg").write_bytes(THUMB_BYTES)
    covers = root / "cache" / "playlists"
    covers.mkdir(parents=True)
    (covers / "PL1.jpg").write_bytes(COVER_BYTES)
    return root


def make_client(playlists: list[dict] | None = None, videos: list[dict] | None = None,
                handler=None) -> ta.TaClient:
    playlists = [playlist_payload()] if playlists is None else playlists
    videos = [video_payload("vid1"), video_payload("vid2")] if videos is None else videos

    if handler is None:
        def handler(request: httpx.Request) -> httpx.Response:
            page = int(request.url.params.get("page", "1"))
            if request.url.path == ta.API_PLAYLISTS and page == 1:
                return httpx.Response(200, json={"data": playlists,
                                                 "paginate": {"last_page": 1}})
            if request.url.path == ta.API_VIDEOS and page == 1:
                return httpx.Response(200, json={"data": videos,
                                                 "paginate": {"last_page": 1}})
            return httpx.Response(404, json={"detail": "not found"})

    return ta.TaClient("http://ta.test", "token", transport=httpx.MockTransport(handler))


def folder_of(tmp_path: Path) -> Path:
    return tmp_path / "out" / sanitize_folder_name("2017/07", "PL1")


def test_parse_playlist_orders_and_fills_idx():
    raw = playlist_payload()
    raw["playlist_entries"].append({"youtube_id": "vid9", "title": "x", "idx": None})
    pl = ta.parse_playlist(raw)
    assert pl is not None
    assert [e.video_id for e in pl.entries] == ["vid1", "vid2", "vid3", "vid9"]
    assert pl.channel == "Stefan Koelle"
    assert pl.last_refresh.startswith("2026-10-05")
    assert ta.parse_playlist({"playlist_name": "no id"}) is None


def test_parse_video_maps_streams_and_counts():
    meta = ta.parse_video(video_payload())
    assert meta is not None
    assert meta.duration_s == 213
    assert meta.vcodec == "vp9" and meta.acodec == "opus"
    assert meta.width == 640 and meta.height == 356
    assert meta.vbr == 312.1 and meta.abr == 124.4
    assert meta.view_count == 3897156
    assert ta.parse_video({}) is None


def test_client_aggregates_pages():
    playlists = [playlist_payload("PL1", "One"), playlist_payload("PL2", "Two")]

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", "1"))
        if request.url.path == ta.API_PLAYLISTS:
            chunk = playlists[page - 1:page]
            return httpx.Response(200, json={"data": chunk, "paginate": {"last_page": 2}})
        return httpx.Response(200, json={"data": [], "paginate": {"last_page": 1}})

    client = make_client(handler=handler)
    found = client.playlists()
    assert [p.title for p in found] == ["One", "Two"]


@pytest.mark.parametrize("status", [401, 403])
def test_client_auth_failure_raises(status):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"detail": "nope"})

    with pytest.raises(ta.TaError, match="authentication failed"):
        make_client(handler=handler).playlists()


def test_client_bad_shape_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": []})

    with pytest.raises(ta.TaError, match="shape"):
        make_client(handler=handler).videos()


def test_index_media_ignores_non_video_files(tmp_path):
    root = make_tree(tmp_path)
    index = ta.index_media(root)
    assert set(index) == {"vid1", "vid2"}
    with pytest.raises(ta.TaError, match="media root not found"):
        ta.index_media(tmp_path / "nope")


def test_export_full_copy_layout(tmp_path):
    root = make_tree(tmp_path)
    report = ta.export(
        make_client(), media_root=root, target=tmp_path / "out",
        mode="copy", playlist_ids=["PL1"],
    )
    folder = folder_of(tmp_path)
    names = sorted(os.listdir(folder))
    assert "01 - Title of vid1 [vid1].mp4" in names
    assert "02 - Title of vid2 [vid2].mp4" in names
    assert "01 - Title of vid1 [vid1].info.json" in names
    assert "01 - Title of vid1 [vid1].description" in names
    assert "01 - Title of vid1 [vid1].jpg" in names
    assert "00 - 2017 07 [PL1].jpg" in names
    assert "manifest.json" in names
    assert (folder / "01 - Title of vid1 [vid1].mp4").read_bytes() == MEDIA_BYTES

    info = json.loads((folder / "01 - Title of vid1 [vid1].info.json").read_text())
    assert info["title"] == "Title of vid1"
    assert info["duration"] == 213
    assert info["upload_date"] == "20120411"
    assert info["vcodec"] == "vp9"
    assert info["playlist_index"] == 1

    prow = report.playlists[0]
    assert (prow.total, prow.copied, prow.missing, prow.failed) == (3, 2, 1, 0)
    archive = (tmp_path / "out" / "archives" / "PL1.txt").read_text().splitlines()
    assert archive == ["youtube vid1", "youtube vid2"]

    gallery = read_video_entries(folder, "PL1")
    assert gallery["exists"] and gallery["video_count"] == 2
    assert gallery["cover"] is not None
    assert gallery["videos"][0]["title"] == "Title of vid1"
    assert gallery["videos"][0]["duration_s"] == 213


def test_export_manifest_holds_db_import_data(tmp_path):
    root = make_tree(tmp_path)
    ta.export(make_client(), media_root=root, target=tmp_path / "out")
    manifest = json.loads((folder_of(tmp_path) / "manifest.json").read_text())
    assert manifest["source"] == "tubearchivist"
    assert manifest["playlist_id"] == "PL1"
    assert manifest["title"] == "2017/07"
    assert manifest["folder_name"] == sanitize_folder_name("2017/07", "PL1")
    assert manifest["remote_item_count"] == 3
    assert [row["position"] for row in manifest["entries"]] == [1, 2, 3]
    assert manifest["entries"][0]["duration_s"] == 213
    assert manifest["entries"][0]["file"] == "01 - Title of vid1 [vid1].mp4"
    assert manifest["entries"][2]["file"] is None
    assert manifest["entries"][2]["downloaded"] is False


def test_export_is_idempotent_and_never_overwrites(tmp_path):
    root = make_tree(tmp_path)
    client = make_client()
    first = ta.export(client, media_root=root, target=tmp_path / "out", mode="copy")
    assert first.totals()["copied"] == 2

    info = folder_of(tmp_path) / "01 - Title of vid1 [vid1].info.json"
    info.write_text("keep me")
    second = ta.export(client, media_root=root, target=tmp_path / "out", mode="copy")
    assert second.totals()["copied"] == 0
    assert second.totals()["skipped"] >= 2
    assert info.read_text() == "keep me"
    archive = (tmp_path / "out" / "archives" / "PL1.txt").read_text().splitlines()
    assert archive == ["youtube vid1", "youtube vid2"]


def test_export_metadata_only_skips_videos(tmp_path):
    root = make_tree(tmp_path)
    report = ta.export(
        make_client(), media_root=root, target=tmp_path / "out", metadata_only=True,
    )
    folder = folder_of(tmp_path)
    names = os.listdir(folder)
    assert not any(n.endswith(".mp4") for n in names)
    assert "01 - Title of vid1 [vid1].info.json" in names
    assert "00 - 2017 07 [PL1].jpg" in names
    assert report.totals()["copied"] == 0
    assert report.totals()["skipped"] == 2
    assert report.missing == 1
    manifest = json.loads((folder / "manifest.json").read_text())
    assert manifest["metadata_only"] is True
    assert manifest["entries"][0]["file"] == "01 - Title of vid1 [vid1].mp4"


def test_export_dry_run_writes_nothing(tmp_path):
    root = make_tree(tmp_path)
    target = tmp_path / "out"
    report = ta.export(
        make_client(), media_root=root, target=target, dry_run=True,
    )
    assert not target.exists()
    assert report.dry_run is True
    assert report.totals()["copied"] == 2
    assert report.totals()["bytes"] == 2 * len(MEDIA_BYTES) + 2 * len(THUMB_BYTES) + len(COVER_BYTES)


def test_export_hardlink_shares_inode(tmp_path):
    root = make_tree(tmp_path)
    ta.export(make_client(), media_root=root, target=tmp_path / "out", mode="hardlink")
    src = root / "media" / "CHAN1" / "vid1.mp4"
    dst = folder_of(tmp_path) / "01 - Title of vid1 [vid1].mp4"
    assert dst.stat().st_ino == src.stat().st_ino
    assert src.read_bytes() == MEDIA_BYTES


def test_export_auto_hardlinks_by_default(tmp_path):
    root = make_tree(tmp_path)
    report = ta.export(make_client(), media_root=root, target=tmp_path / "out")
    totals = report.totals()
    assert totals["hardlinked"] == 2
    assert totals["copied"] == 0 and totals["failed"] == 0
    src = root / "media" / "CHAN1" / "vid1.mp4"
    dst = folder_of(tmp_path) / "01 - Title of vid1 [vid1].mp4"
    assert dst.stat().st_ino == src.stat().st_ino


def test_export_auto_falls_back_to_copy(tmp_path, monkeypatch):
    root = make_tree(tmp_path)

    def refuse(*_args, **_kwargs):
        raise OSError("hardlink refused by filesystem")

    monkeypatch.setattr(ta.os, "link", refuse)
    report = ta.export(make_client(), media_root=root, target=tmp_path / "out")
    totals = report.totals()
    assert totals["hardlinked"] == 0
    assert totals["copied"] == 2 and totals["failed"] == 0
    dst = folder_of(tmp_path) / "01 - Title of vid1 [vid1].mp4"
    assert dst.read_bytes() == MEDIA_BYTES


def test_export_hardlink_mode_propagates_error(tmp_path, monkeypatch):
    root = make_tree(tmp_path)

    def refuse(*_args, **_kwargs):
        raise OSError("hardlink refused by filesystem")

    monkeypatch.setattr(ta.os, "link", refuse)
    report = ta.export(make_client(), media_root=root, target=tmp_path / "out", mode="hardlink")
    assert report.totals()["failed"] == 2
    assert report.totals()["hardlinked"] == 0


def test_export_warns_once_on_cross_device(tmp_path, monkeypatch, caplog):
    root = make_tree(tmp_path)
    playlists = [playlist_payload("PL1", "One"), playlist_payload("PL2", "Two")]
    link_attempts = []

    def refuse(*_args, **_kwargs):
        link_attempts.append(1)
        raise AssertionError("os.link must not run across filesystems")

    monkeypatch.setattr(ta.os, "link", refuse)
    monkeypatch.setattr(ta, "_same_device", lambda _src, _dst: False)
    with caplog.at_level("WARNING"):
        report = ta.export(
            make_client(playlists=playlists), media_root=root, target=tmp_path / "out"
        )
    totals = report.totals()
    assert totals["copied"] == 4 and totals["failed"] == 0
    assert link_attempts == []
    assert sum("different filesystems" in r.message for r in caplog.records) == 1
    first = tmp_path / "out" / "One [PL1]" / "01 - Title of vid1 [vid1].mp4"
    assert first.read_bytes() == MEDIA_BYTES


def test_place_file_hardlink_fails_across_devices(tmp_path):
    src = tmp_path / "src.mp4"
    src.write_bytes(MEDIA_BYTES)
    dst = tmp_path / "out" / "dst.mp4"
    with pytest.raises(OSError) as excinfo:
        ta._place_file(src, dst, "hardlink", False, linkable=False)
    assert excinfo.value.errno == errno.EXDEV
    assert not dst.exists()


def test_same_device_is_true_for_same_filesystem(tmp_path):
    assert ta._same_device(tmp_path, tmp_path) is True


def test_export_repairs_truncated_file(tmp_path):
    root = make_tree(tmp_path)
    dest = folder_of(tmp_path)
    dest.mkdir(parents=True)
    partial = dest / "01 - Title of vid1 [vid1].mp4"
    partial.write_bytes(MEDIA_BYTES[:5])

    report = ta.export(make_client(), media_root=root, target=tmp_path / "out", mode="copy")
    assert report.totals()["repaired"] == 1
    assert report.totals()["copied"] == 1
    assert report.totals()["failed"] == 0
    assert partial.read_bytes() == MEDIA_BYTES


def test_export_removes_leftover_part_files(tmp_path):
    root = make_tree(tmp_path)
    dest = folder_of(tmp_path)
    dest.mkdir(parents=True)
    leftover = dest / "01 - Title of vid1 [vid1].mp4.part"
    leftover.write_bytes(b"junk from a killed run")

    ta.export(make_client(), media_root=root, target=tmp_path / "out", mode="copy")
    assert not leftover.exists()
    assert (dest / "01 - Title of vid1 [vid1].mp4").read_bytes() == MEDIA_BYTES


def test_export_without_archives(tmp_path):
    root = make_tree(tmp_path)
    ta.export(
        make_client(), media_root=root, target=tmp_path / "out", with_archives=False,
    )
    assert not (tmp_path / "out" / "archives").exists()


def test_export_rejects_unknown_playlist(tmp_path, caplog):
    root = make_tree(tmp_path)
    report = ta.export(
        make_client(), media_root=root, target=tmp_path / "out", playlist_ids=["PL404"],
    )
    assert report.playlists == []
    assert "PL404" in caplog.text


def test_export_rejects_bad_mode(tmp_path):
    root = make_tree(tmp_path)
    with pytest.raises(ValueError, match="unsupported mode"):
        ta.export(make_client(), media_root=root, target=tmp_path / "out", mode="symlink")


def test_load_dotenv(tmp_path):
    env = tmp_path / "sample.env"
    env.write_text("# comment\nTA_API_URL=http://ta\n\nTA_API_TOKEN='abc def'\nBROKEN\n")
    values = ta.load_dotenv(env)
    assert values == {"TA_API_URL": "http://ta", "TA_API_TOKEN": "abc def"}
    assert ta.load_dotenv(tmp_path / "missing.env") == {}


def test_cli_requires_options(tmp_path, capsys, monkeypatch):
    for key in ("TA_API_URL", "TA_API_TOKEN", "TA_API_HOST",
                "TA_EXPORT_TARGET", "TA_MEDIA_ROOT"):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(SystemExit) as exc:
        ta.main(["--env-file", str(tmp_path / "missing.env")])
    assert exc.value.code == 2
    assert "missing required options" in capsys.readouterr().err
