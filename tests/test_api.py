# Copyright (c) 2026 Stefan Koelle (https://stefankoelle.de)
# Licensed under the MIT License. See LICENSE file in project root for details.
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")
pytest.importorskip("alembic")
pytest.importorskip("pydantic_settings")
pytest.importorskip("apscheduler")

from fastapi.testclient import TestClient  # noqa: E402

from app.config import Settings  # noqa: E402
from app.db import session_scope  # noqa: E402
from app.discovery import apply_discovery  # noqa: E402
from app.main import create_app  # noqa: E402
from app.models import Playlist  # noqa: E402
from app.paths import sanitize_folder_name  # noqa: E402
from app.ytdlp import PlaylistInfo  # noqa: E402


@pytest.fixture
def client(tmp_path):
    cfg = Settings(
        youtube_channel="@beispielkanal", data_dir=tmp_path / "data", config_dir=tmp_path / "config"
    )
    with TestClient(create_app(cfg, start_background=False)) as c:
        yield c


def test_health_and_status(client):
    assert client.get("/healthz").json() == {"status": "ok"}
    st = client.get("/api/status").json()
    assert st["channel"] == "@beispielkanal" and st["queue"] == [] and st["current"] is None
    assert client.get("/").status_code == 200


def test_playlist_endpoints(client):
    apply_discovery([PlaylistInfo("PL1", "Sommer Mix"), PlaylistInfo("PL2", "Setlist A")], "setlist")
    assert len(client.get("/api/playlists").json()) == 2
    sync = client.get("/api/playlists?type=sync").json()
    one = client.get("/api/playlists?type=oneshot").json()
    assert [p["playlist_id"] for p in sync] == ["PL1"] and [p["playlist_id"] for p in one] == ["PL2"]
    pid = sync[0]["id"]
    assert client.post(f"/api/playlists/{pid}/retry").status_code == 409
    assert client.post(f"/api/playlists/{pid}/ignore", json={"ignored": True}).json() == {"ignored": True}
    assert client.post(f"/api/playlists/{pid}/run").status_code == 200
    assert client.post(f"/api/playlists/{pid}/run").status_code == 409
    assert client.post(f"/api/playlists/{pid}/type", json={"type": "bogus"}).status_code == 422
    assert client.post("/api/playlists/9999/run").status_code == 404
    jobs = client.get("/api/jobs").json()
    assert len(jobs) == 1 and jobs[0]["status"] == "queued"
    assert client.post(f"/api/jobs/{jobs[0]['id']}/cancel").status_code == 200
    assert client.get(f"/api/jobs/{jobs[0]['id']}/log").json()["finished"] is True
    assert client.get("/api/jobs/9999/log").status_code == 404


def test_ui_has_no_page_reload():
    js = (Path(__file__).parent.parent / "app" / "static" / "app.js").read_text()
    assert "location.reload" not in js


def test_playlist_detail_and_files(client):
    apply_discovery([PlaylistInfo("PL1", "Sommer Mix"), PlaylistInfo("PL2", "Setlist A")], "setlist")
    pid = client.get("/api/playlists?type=sync").json()[0]["id"]

    det = client.get(f"/api/playlists/{pid}").json()
    assert det["title"] == "Sommer Mix" and det["type"] == "sync"
    assert det["jobs"] == [] and det["last_job"] is None
    assert det["folder_name"] is None and det["last_seen_at"] is not None
    assert det["url"] == "https://www.youtube.com/playlist?list=PL1"
    assert client.get("/api/playlists/9999").status_code == 404
    assert client.get("/api/playlists/9999/files").status_code == 404

    assert client.post(f"/api/playlists/{pid}/run").status_code == 200
    det = client.get(f"/api/playlists/{pid}").json()
    assert len(det["jobs"]) == 1 and det["last_job"]["status"] == "queued"
    assert det["folder_name"] is None  # set by the worker when the job starts

    # no folder on disk yet (emulate what _run_job writes into the DB)
    files = client.get(f"/api/playlists/{pid}/files").json()
    assert files["exists"] is False and files["files"] == [] and files["folder_name"] is None

    with session_scope() as s:
        folder = sanitize_folder_name("Sommer Mix", "PL1")
        s.get(Playlist, pid).folder_name = folder
    data_dir = client.app.state.settings.data_dir
    (data_dir / folder).mkdir(parents=True)
    (data_dir / folder / "01 - Song [abc123].mkv").write_bytes(b"x" * 10)
    (data_dir / folder / "01 - Song [abc123].info.json").write_text("{}")
    files = client.get(f"/api/playlists/{pid}/files").json()
    assert files["exists"] is True and files["folder_name"] == folder
    assert files["total_files"] == 2 and files["total_bytes"] == 12
    assert files["by_ext"] == {"mkv": 1, "json": 1}
    assert [f["name"] for f in files["files"]] == sorted(f["name"] for f in files["files"])
    assert all(f["modified_at"].endswith("Z") for f in files["files"])

    # path traversal must never leave data_dir
    with session_scope() as s:
        s.get(Playlist, pid).folder_name = "../../etc"
    files = client.get(f"/api/playlists/{pid}/files").json()
    assert files["exists"] is False and files["files"] == []
