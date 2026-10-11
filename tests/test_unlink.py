# Copyright (c) 2026 Stefan Koelle (https://stefankoelle.de)
# Licensed under the MIT License. See LICENSE file in project root for details.
"""Tests for POST /api/playlists/{id}/unlink (SPEC section 15)."""
import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")
pytest.importorskip("alembic")
pytest.importorskip("pydantic_settings")
pytest.importorskip("apscheduler")

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import func, select  # noqa: E402

from app.config import Settings  # noqa: E402
from app.db import session_scope, utcnow  # noqa: E402
from app.discovery import apply_discovery  # noqa: E402
from app.main import create_app  # noqa: E402
from app.models import Job, Playlist, PlaylistEntry  # noqa: E402
from app.paths import sanitize_folder_name  # noqa: E402
from app.ta_import import apply_import, scan_manifests  # noqa: E402
from app.ytdlp import PlaylistInfo, read_archive  # noqa: E402


@pytest.fixture
def client(tmp_path, stub):
    cfg = Settings(
        youtube_channel="@beispielkanal", data_dir=tmp_path / "data", config_dir=tmp_path / "config",
        ytdlp_bin=stub.bin,
    )
    with TestClient(create_app(cfg, start_background=False)) as c:
        yield c


def _seed(client, *, state="done"):
    """Sync playlist PL1 with two local videos, listing entries and a finished job."""
    apply_discovery([PlaylistInfo("PL1", "Sommer Mix")], "setlist")
    pid = client.get("/api/playlists?type=sync").json()[0]["id"]
    folder = sanitize_folder_name("Sommer Mix", "PL1")
    d = client.app.state.settings.data_dir / folder
    d.mkdir(parents=True)
    (d / "01 - One [vid00000001].mkv").write_bytes(b"one")
    (d / "02 - Two [vid00000002].mkv").write_bytes(b"two")
    with session_scope() as s:
        pl = s.get(Playlist, pid)
        pl.folder_name = folder
        pl.state = state
        pl.downloaded_count = 2
        for vid, pos, title in (("vid00000001", 1, "One"), ("vid00000002", 2, "Two")):
            s.add(PlaylistEntry(playlist_id=pid, video_id=vid, position=pos, title=title,
                                duration_s=60, last_seen_at=utcnow()))
        s.add(Job(playlist_id=pid, trigger="manual", status="success", queued_at=utcnow()))
    return pid, folder


def _counts():
    with session_scope() as s:
        return (
            int(s.scalar(select(func.count()).select_from(Playlist))),
            int(s.scalar(select(func.count()).select_from(PlaylistEntry))),
            int(s.scalar(select(func.count()).select_from(Job))),
        )


def test_unlink_removes_rows_and_keeps_files(client):
    pid, folder = _seed(client)
    r = client.post(f"/api/playlists/{pid}/unlink")
    assert r.status_code == 200 and r.json() == {"unlinked": True}
    assert _counts() == (0, 0, 0)
    assert client.get(f"/api/playlists/{pid}").status_code == 404

    d = client.app.state.settings.data_dir / folder
    assert (d / "01 - One [vid00000001].mkv").read_bytes() == b"one"
    assert (d / "02 - Two [vid00000002].mkv").read_bytes() == b"two"

    # re-import manifest was written from the playlist_entries snapshot
    m = json.loads((d / "manifest.json").read_text())
    assert m["source"] == "yt-playlist-sync" and m["playlist_id"] == "PL1"
    assert m["title"] == "Sommer Mix" and m["folder_name"] == folder
    assert [e["video_id"] for e in m["entries"]] == ["vid00000001", "vid00000002"]
    assert m["entries"][0] == {
        "position": 1, "video_id": "vid00000001", "title": "One", "duration_s": 60,
    }

    # local video ids were merged into the append-only download archive
    arch = client.app.state.settings.config_dir / "archives" / "PL1.txt"
    assert read_archive(arch) == {"vid00000001", "vid00000002"}


def test_unlink_keeps_existing_import_manifest(client):
    """A playlist that came from an import already has its marker; never overwrite it."""
    pid, folder = _seed(client)
    d = client.app.state.settings.data_dir / folder
    original = json.dumps({"source": "tubearchivist", "playlist_id": "PL1", "entries": []})
    (d / "manifest.json").write_text(original)
    assert client.post(f"/api/playlists/{pid}/unlink").status_code == 200
    assert (d / "manifest.json").read_text() == original
    assert (d / "01 - One [vid00000001].mkv").exists()
    assert _counts() == (0, 0, 0)


def test_unlink_without_folder_only_clears_db(client):
    apply_discovery([PlaylistInfo("PL1", "Sommer Mix")], "setlist")
    pid = client.get("/api/playlists?type=sync").json()[0]["id"]
    with session_scope() as s:
        s.add(PlaylistEntry(playlist_id=pid, video_id="vid00000001", position=1,
                            last_seen_at=utcnow()))
    r = client.post(f"/api/playlists/{pid}/unlink")
    assert r.status_code == 200 and _counts() == (0, 0, 0)
    assert not list(client.app.state.settings.data_dir.glob("*/manifest.json"))


def test_unlink_refuses_open_jobs(client):
    apply_discovery([PlaylistInfo("PL1", "Sommer Mix")], "setlist")
    pid = client.get("/api/playlists?type=sync").json()[0]["id"]
    # state alone is enough
    with session_scope() as s:
        s.get(Playlist, pid).state = "running"
    assert client.post(f"/api/playlists/{pid}/unlink").status_code == 409
    # an open job row blocks even with a calm state
    with session_scope() as s:
        s.get(Playlist, pid).state = "idle"
        s.add(Job(playlist_id=pid, trigger="manual", status="queued", queued_at=utcnow()))
    assert client.post(f"/api/playlists/{pid}/unlink").status_code == 409
    assert client.post("/api/playlists/9999/unlink").status_code == 404
    assert _counts() == (1, 0, 1)
    # a finished job does not block
    with session_scope() as s:
        s.scalar(select(Job)).status = "success"
    assert client.post(f"/api/playlists/{pid}/unlink").status_code == 200
    assert _counts() == (0, 0, 0)


def test_unlink_reimport_roundtrip(client):
    """The written manifest restores the playlist via ta_import without re-downloads."""
    pid, folder = _seed(client)
    archive = client.app.state.settings.config_dir / "archives" / "PL1.txt"
    archive.parent.mkdir(parents=True, exist_ok=True)
    archive.write_text("youtube vid00000001\n")
    assert client.post(f"/api/playlists/{pid}/unlink").status_code == 200
    assert read_archive(archive) == {"vid00000001", "vid00000002"}

    cfg = client.app.state.settings
    manifests = scan_manifests(cfg.data_dir)
    assert len(manifests) == 1 and manifests[0].playlist_id == "PL1"
    report = apply_import(manifests, data_dir=cfg.data_dir, config_dir=cfg.config_dir)
    prow = report.playlists[0]
    assert prow.action == "new" and prow.state == "done" and prow.files == 2
    assert prow.archive_merged == 0  # unlink already merged both ids

    with session_scope() as s:
        pl = s.scalar(select(Playlist).where(Playlist.playlist_id == "PL1"))
        assert pl is not None and pl.type == "oneshot" and pl.state == "done"
        assert pl.folder_name == folder and pl.downloaded_count == 2
        n = s.scalar(
            select(func.count()).select_from(PlaylistEntry)
            .where(PlaylistEntry.playlist_id == pl.id)
        )
        assert n == 2


def test_ui_has_danger_zone_unlink():
    root = Path(__file__).parent.parent / "app" / "static"
    html = (root / "index.html").read_text()
    js = (root / "app.js").read_text()
    assert 'id="detail-unlink"' in html and "Danger zone" in html
    assert 'data-action="unlink"' in html and "Unlink from database" in html
    assert 'case "unlink"' in js and "/unlink" in js
    assert "location.hash = state.detailFrom" in js
    assert 'removeAttribute("data-id")' in js  # never act on the previously shown playlist
