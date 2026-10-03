from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")
pytest.importorskip("alembic")
pytest.importorskip("pydantic_settings")
pytest.importorskip("apscheduler")

from fastapi.testclient import TestClient  # noqa: E402

from app.config import Settings  # noqa: E402
from app.discovery import apply_discovery  # noqa: E402
from app.main import create_app  # noqa: E402
from app.ytdlp import PlaylistInfo  # noqa: E402


@pytest.fixture
def client(tmp_path):
    cfg = Settings(youtube_channel="@beispielkanal", data_dir=tmp_path / "data", config_dir=tmp_path / "config")
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
