# Copyright (c) 2026 Stefan Koelle (https://stefankoelle.de)
# Licensed under the MIT License. See LICENSE file in project root for details.
import re
from datetime import timedelta
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")
pytest.importorskip("alembic")
pytest.importorskip("pydantic_settings")
pytest.importorskip("apscheduler")

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.config import Settings  # noqa: E402
from app.db import session_scope, utcnow  # noqa: E402
from app.discovery import apply_discovery  # noqa: E402
from app.main import create_app  # noqa: E402
from app.models import Job, Playlist, PlaylistEntry  # noqa: E402
from app.paths import sanitize_folder_name  # noqa: E402
from app.ytdlp import PlaylistInfo  # noqa: E402


@pytest.fixture
def client(tmp_path, stub):
    cfg = Settings(
        youtube_channel="@beispielkanal", data_dir=tmp_path / "data", config_dir=tmp_path / "config",
        ytdlp_bin=stub.bin,
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


def test_oneshot_playlists_default_to_title_order(client):
    # completed_at is deliberately inverse to the title order
    apply_discovery([PlaylistInfo("PL1", "Zulu Setlist"), PlaylistInfo("PL2", "Alpha Setlist")], "setlist")
    now = utcnow()
    with session_scope() as s:
        for pl in s.scalars(select(Playlist)).all():
            pl.completed_at = now if pl.playlist_id == "PL1" else now - timedelta(days=7)
    one = client.get("/api/playlists?type=oneshot").json()
    assert [p["title"] for p in one] == ["Alpha Setlist", "Zulu Setlist"]


def test_add_playlist_creates_manual_row(client, stub):
    """POST /api/playlists: validate via stub, create the row, never auto-enqueue (SPEC 6.10)."""
    stub.data_ref["playlists"]["PLmanua"] = [
        {"id": "vid00000101", "title": "One"}, {"id": "vid00000102", "title": "Two"},
        {"id": "vid00000103", "title": "Three"},
    ]
    stub.save()
    r = client.post("/api/playlists", json={
        "url": "https://www.youtube.com/playlist?list=PLmanua", "type": "sync",
    })
    assert r.status_code == 201
    body = r.json()
    assert body["playlist_id"] == "PLmanua" and body["manual"] is True
    assert body["title"] == "PLmanua" and body["type"] == "sync"
    assert body["state"] == "new" and body["remote_status"] == "active"
    assert body["remote_item_count"] == 3 and body["folder_name"] is None
    with session_scope() as s:
        pl = s.scalar(select(Playlist).where(Playlist.playlist_id == "PLmanua"))
        assert pl.manual is True and pl.first_seen_at is not None
        # no auto-enqueue: the row waits for the existing Download now / Sync now buttons
        assert s.scalar(select(Job.id).limit(1)) is None
    # duplicate id is refused before any yt-dlp call
    assert client.post("/api/playlists", json={"url": "PLmanua"}).status_code == 409


def test_add_playlist_with_title(client, stub):
    stub.data_ref["playlists"]["PLtita"] = [{"id": "vid00000111", "title": "A"}]
    stub.save()
    r = client.post("/api/playlists", json={"url": "PLtita", "type": "oneshot", "title": "My Setlist"})
    assert r.status_code == 201
    assert r.json()["title"] == "My Setlist"


def test_add_playlist_validation_and_errors(client, stub):
    # 422: unusable type or no playlist id in the URL
    assert client.post("/api/playlists", json={"url": "PLaaaaa", "type": "bogus"}).status_code == 422
    r = client.post("/api/playlists", json={"url": "https://www.youtube.com/@x/playlists"})
    assert r.status_code == 422
    # 400: yt-dlp says the playlist is not accessible (private/removed/unknown)
    stub.data_ref["listing_error"]["PLgone1"] = "The playlist does not exist"
    stub.save()
    r = client.post("/api/playlists", json={"url": "PLgone1"})
    assert r.status_code == 400 and "playlist not accessible" in r.json()["detail"]
    # 400 (not 500): id the stub does not know at all
    r = client.post("/api/playlists", json={"url": "PLnope1"})
    assert r.status_code == 400


def test_ui_has_no_page_reload():
    js = (Path(__file__).parent.parent / "app" / "static" / "app.js").read_text()
    assert "location.reload" not in js


def test_ui_has_add_playlist_forms():
    root = Path(__file__).parent.parent / "app" / "static"
    html = (root / "index.html").read_text()
    js = (root / "app.js").read_text()
    # one form per tab, the type follows the active tab
    assert html.count('class="add-form"') == 2
    assert 'data-type="sync"' in html and 'data-type="oneshot"' in html
    assert "Add playlist" in html and "Title (optional)" in html
    assert 'method: "POST", body: { url, type: form.dataset.type' in js
    assert ".add-form" in (root / "style.css").read_text()


def test_migration_adds_manual_column_is_idempotent(tmp_path):
    import sqlalchemy as sa

    from app.config import ensure_dirs
    from app.db import init_engine, migrate

    cfg = Settings(
        youtube_channel="@beispielkanal", data_dir=tmp_path / "data", config_dir=tmp_path / "config"
    )
    ensure_dirs(cfg)
    init_engine(cfg.db_url)
    migrate(cfg.db_url)
    migrate(cfg.db_url)  # running the chain again must be a no-op (existence guards)
    eng = sa.create_engine(cfg.db_url)
    cols = {c["name"] for c in sa.inspect(eng).get_columns("playlists")}
    assert "manual" in cols


def test_ui_sync_tab_has_summary_and_sort():
    root = Path(__file__).parent.parent / "app" / "static"
    html = (root / "index.html").read_text()
    js = (root / "app.js").read_text()
    sync_head = html.split('id="sync-table"')[1].split("</table>")[0]
    assert 'id="sync-summary"' in html
    assert 'data-sort="title"' in sync_head and 'data-sort="last"' in sync_head
    assert "function renderSyncs" in js and "syncSortValue" in js
    assert 'sort: { key: "title", dir: "asc" }' in js  # oneshot default = title, like sync


def test_ui_is_english():
    root = Path(__file__).parent.parent / "app" / "static"
    html = (root / "index.html").read_text()
    js = (root / "app.js").read_text()
    assert '<html lang="en">' in html and 'lang="de"' not in html
    for src in (html, js):
        assert not re.search(r"[äöüßÄÖÜ]", src)
    german = [
        "Abbrechen", "Schließen", "Zeitpläne", "Aktueller Job", "Kein Job aktiv",
        "Warteschlange", "Freier Platz", "Letzte Jobs", "Übersprungen", "Aktionen",
        "Heruntergeladen", "Erneut versuchen", "Jetzt syncen", "Als Oneshot markieren",
        "Vorheriges", "Nächstes", "Playlist-Daten", "Job-Verlauf",
        "Dateien,", "nicht verf", "erreichbar", "de-DE",
    ]
    for src in (html, js):
        for word in german:
            assert word not in src, word


def test_ui_has_search_tab():
    root = Path(__file__).parent.parent / "app" / "static"
    html = (root / "index.html").read_text()
    js = (root / "app.js").read_text()
    assert 'href="#search"' in html and 'id="search-input"' in html
    assert 'id="search-results"' in html and 'id="search-more"' in html
    assert "function renderSearch" in js and "async function runSearch" in js
    assert '`/search?q=' in js
    assert "function searchRow" in js and "class=\"vid-table\"" in js
    assert "function startPlayer" in js and "searchPlayerItems" in js
    assert 'data-playlist="${playlistId}"' in js


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


def test_playlist_videos_and_thumb(client):
    apply_discovery([PlaylistInfo("PL1", "Sommer Mix")], "setlist")
    pid = client.get("/api/playlists?type=sync").json()[0]["id"]

    # no folder yet
    v = client.get(f"/api/playlists/{pid}/videos").json()
    assert v["exists"] is False and v["videos"] == [] and v["cover"] is None

    name = sanitize_folder_name("Sommer Mix", "PL1")
    with session_scope() as s:
        s.get(Playlist, pid).folder_name = name
    d = client.app.state.settings.data_dir / name
    d.mkdir(parents=True)
    (d / "00 - Sommer Mix [PL1].jpg").write_bytes(b"cover")
    (d / "01 - Song [nAUaWGdv6So].mkv").write_bytes(b"video-bytes")
    (d / "01 - Song [nAUaWGdv6So].jpg").write_bytes(b"thumb-bytes")
    (d / "01 - Song [nAUaWGdv6So].info.json").write_text(
        '{"title": "Song", "duration": 61, "channel": "Kanal"}')

    v = client.get(f"/api/playlists/{pid}/videos").json()
    assert v["exists"] is True
    assert v["cover"] == "00 - Sommer Mix [PL1].jpg"
    assert v["video_count"] == 1 and v["total_duration_s"] == 61
    entry = v["videos"][0]
    assert entry["title"] == "Song" and entry["duration_s"] == 61
    assert entry["thumb"] == "01 - Song [nAUaWGdv6So].jpg"
    assert entry["size_bytes"] == 11

    # thumbnail: ok, whitelist, traversal, unknown
    r = client.get(f"/api/playlists/{pid}/thumb", params={"file": "01 - Song [nAUaWGdv6So].jpg"})
    assert r.status_code == 200 and r.content == b"thumb-bytes"
    assert r.headers["content-type"].startswith("image/")
    assert "cache-control" in r.headers
    r = client.get(f"/api/playlists/{pid}/thumb", params={"file": "00 - Sommer Mix [PL1].jpg"})
    assert r.status_code == 200 and r.content == b"cover"
    bad = ["01 - Song [nAUaWGdv6So].mkv", "../../app.py", "missing.jpg", "", "x.EXE"]
    for f in bad:
        assert client.get(f"/api/playlists/{pid}/thumb", params={"file": f}).status_code == 404, f
    assert client.get("/api/playlists/9999/videos").status_code == 404
    assert client.get("/api/playlists/9999/thumb", params={"file": "a.jpg"}).status_code == 404


def test_playlist_videos_with_placeholders(client):
    """Missing remote videos show up as placeholder rows with everything we know about them."""
    apply_discovery([PlaylistInfo("PL1", "Sommer Mix")], "setlist")
    pid = client.get("/api/playlists?type=sync").json()[0]["id"]
    name = sanitize_folder_name("Sommer Mix", "PL1")
    with session_scope() as s:
        s.get(Playlist, pid).folder_name = name
        for e in (
            PlaylistEntry(playlist_id=pid, video_id="have0000001", position=1, title="Downloaded",
                          last_seen_at=utcnow()),
            PlaylistEntry(playlist_id=pid, video_id="priv0000001", position=2, title="[Private video]",
                          unavailable=True, last_seen_at=utcnow()),
            PlaylistEntry(playlist_id=pid, video_id="fail0000001", position=3, title="Broken Song",
                          duration_s=180, reason="HTTP Error 503: Service Unavailable",
                          last_seen_at=utcnow()),
            PlaylistEntry(playlist_id=pid, video_id="arch0000001", position=4, title="Archived",
                          last_seen_at=utcnow()),
            PlaylistEntry(playlist_id=pid, video_id="wait0000001", position=5, title="Waiting",
                          last_seen_at=utcnow()),
            PlaylistEntry(playlist_id=pid, video_id="gone0000001", position=6, title="Gone",
                          remote_present=False, last_seen_at=utcnow()),
        ):
            s.add(e)
    # placeholders are shown even before the folder exists
    v = client.get(f"/api/playlists/{pid}/videos").json()
    assert v["exists"] is False and len(v["videos"]) == 5
    d = client.app.state.settings.data_dir / name
    d.mkdir(parents=True)
    (d / "01 - Downloaded [have0000001].mkv").write_bytes(b"video-bytes")
    archive = client.app.state.settings.config_dir / "archives" / "PL1.txt"
    archive.parent.mkdir(parents=True, exist_ok=True)
    archive.write_text("youtube arch0000001\n")

    v = client.get(f"/api/playlists/{pid}/videos").json()
    assert v["exists"] is True and v["video_count"] == 1
    videos = v["videos"]
    assert [x["video_id"] for x in videos] == [
        "have0000001", "priv0000001", "fail0000001", "arch0000001", "wait0000001",
    ]  # ordered by position, the remote-removed entry stays hidden
    assert not videos[0].get("missing") and videos[0]["file"] is not None
    broken = videos[1:5]
    assert all(x["missing"] and x["file"] is None for x in broken)
    assert [(x["status"], x["reason"]) for x in broken] == [
        ("unavailable", None),
        ("failed", "HTTP Error 503: Service Unavailable"),
        ("archived", None),
        ("pending", None),
    ]
    assert videos[2]["title"] == "Broken Song" and videos[2]["duration_s"] == 180
    assert videos[1]["title"] == "[Private video]"


def test_search_groups_videos_across_playlists(client):
    apply_discovery([PlaylistInfo("PL1", "Sommer Mix"), PlaylistInfo("PL2", "Setlist A")], "setlist")
    pids = {p["playlist_id"]: p["id"] for p in client.get("/api/playlists").json()}
    now = utcnow()
    name = sanitize_folder_name("Setlist A", "PL2")
    with session_scope() as s:
        s.get(Playlist, pids["PL2"]).folder_name = name
        for playlist_id, entries in {
            "PL1": [("vidshared001", "Live Song", 200), ("vidonly00001", "Other", 10)],
            "PL2": [("vidshared001", "Live Song", 200), ("vidonly00002", "Live Again", 30)],
        }.items():
            for i, (vid, title, duration_s) in enumerate(entries, 1):
                s.add(PlaylistEntry(playlist_id=pids[playlist_id], video_id=vid, position=i,
                                    title=title, duration_s=duration_s, last_seen_at=now))
    d = client.app.state.settings.data_dir / name
    d.mkdir(parents=True)
    (d / "01 - Live Song [vidshared001].mkv").write_bytes(b"video-bytes")
    (d / "01 - Live Song [vidshared001].jpg").write_bytes(b"thumb-bytes")
    (d / "01 - Live Song [vidshared001].info.json").write_text(
        '{"title": "Live Song", "duration": 200, "channel": "Kanal", "width": 1280, "height": 720}')
    for query in ("live", "LIVE"):
        r = client.get("/api/search", params={"q": query}).json()
        assert r["total"] == 2
        shared = next(x for x in r["results"] if x["video_id"] == "vidshared001")
        assert shared["title"] == "Live Song" and shared["duration_s"] == 200
        assert {p["playlist_id"] for p in shared["playlists"]} == {"PL1", "PL2"}
        assert len(shared["playlists"]) == 2
        statuses = {p["playlist_id"]: p["status"] for p in shared["playlists"]}
        assert statuses == {"PL1": "pending", "PL2": "downloaded"}
        media = shared["media"]
        assert media["playlist_id"] == pids["PL2"]
        assert media["file"] == "01 - Live Song [vidshared001].mkv"
        assert media["thumb"] == "01 - Live Song [vidshared001].jpg"
        assert media["channel"] == "Kanal" and media["resolution"] == "1280x720"
    # every token must match (AND), not just one of them
    assert client.get("/api/search", params={"q": "live song"}).json()["total"] == 1
    r = client.get("/api/search", params={"q": "vidonly00001"}).json()
    assert [x["video_id"] for x in r["results"]] == ["vidonly00001"]
    # a hit without a local file is still returned as a placeholder
    r = client.get("/api/search", params={"q": "again"}).json()
    assert [x["video_id"] for x in r["results"]] == ["vidonly00002"]
    assert r["results"][0]["media"] is None
    assert r["results"][0]["playlists"][0]["status"] == "pending"


def test_search_terms_are_literal_and_all_required(client):
    apply_discovery([PlaylistInfo("PL1", "Mix")], "setlist")
    pid = client.get("/api/playlists?type=sync").json()[0]["id"]
    now = utcnow()
    with session_scope() as s:
        for i, (vid, title) in enumerate([
            ("vidone000001", "Live Song"), ("vidtwo000002", "Song Live"), ("vidthr000003", "100% Pure"),
        ], 1):
            s.add(PlaylistEntry(playlist_id=pid, video_id=vid, position=i, title=title, last_seen_at=now))
    assert client.get("/api/search", params={"q": "live song"}).json()["total"] == 2
    assert client.get("/api/search", params={"q": "song zzz"}).json()["total"] == 0
    # % and _ are literal characters, not wildcards
    r = client.get("/api/search", params={"q": "%"}).json()
    assert [x["video_id"] for x in r["results"]] == ["vidthr000003"]
    assert client.get("/api/search", params={"q": "   "}).json() == {
        "query": "   ", "total": 0, "results": [],
    }
    assert client.get("/api/search").status_code == 422


def test_search_status_and_pagination(client):
    apply_discovery([PlaylistInfo("PL1", "Mix")], "setlist")
    pid = client.get("/api/playlists?type=sync").json()[0]["id"]
    now = utcnow()
    with session_scope() as s:
        for i, entry in enumerate([
            dict(video_id="vidload00001", title="Song Downloaded"),
            dict(video_id="vidwait00002", title="Song Pending", remote_present=False),
            dict(video_id="vidfail00003", title="Song Failed", reason="HTTP Error 503"),
            dict(video_id="vidgone00004", title="Song Unavailable", unavailable=True),
            dict(video_id="vidload00005", title="Song Archived Removed", remote_present=False),
        ], 1):
            s.add(PlaylistEntry(playlist_id=pid, position=i, last_seen_at=now, **entry))
    archive = client.app.state.settings.config_dir / "archives" / "PL1.txt"
    archive.parent.mkdir(parents=True, exist_ok=True)
    archive.write_text("youtube vidload00001\nyoutube vidload00005\n")
    name = sanitize_folder_name("Mix", "PL1")
    with session_scope() as s:
        s.get(Playlist, pid).folder_name = name
    d = client.app.state.settings.data_dir / name
    d.mkdir(parents=True)
    (d / "01 - Song Downloaded [vidload00001].mkv").write_bytes(b"video-bytes")
    (d / "01 - Song Downloaded [vidload00001].jpg").write_bytes(b"thumb-bytes")
    (d / "01 - Song Downloaded [vidload00001].info.json").write_text(
        '{"title": "Song Downloaded", "duration": 42, "channel": "Kanal", "view_count": 7, '
        '"upload_date": "20240102", "width": 1920, "height": 1080, "vcodec": "vp9", "acodec": "opus"}')
    r = client.get("/api/search", params={"q": "song"}).json()
    assert r["total"] == 5
    by_id = {x["video_id"]: x for x in r["results"]}
    status = {vid: item["playlists"][0]["status"] for vid, item in by_id.items()}
    assert status == {
        "vidload00001": "downloaded", "vidwait00002": "pending", "vidfail00003": "failed",
        "vidgone00004": "unavailable", "vidload00005": "archived",
    }
    media = by_id["vidload00001"]["media"]
    assert media["playlist_id"] == pid
    assert media["file"] == "01 - Song Downloaded [vidload00001].mkv"
    assert media["thumb"] == "01 - Song Downloaded [vidload00001].jpg"
    assert media["duration_s"] == 42 and media["channel"] == "Kanal"
    assert media["resolution"] == "1920x1080" and media["view_count"] == 7
    assert media["upload_date"] == "20240102"
    assert by_id["vidload00005"]["media"] is None
    remote = {vid: item["playlists"][0]["remote_present"] for vid, item in by_id.items()}
    assert remote["vidwait00002"] is False and remote["vidload00001"] is True

    seen = []
    for offset in (0, 2, 4, 10):
        page = client.get("/api/search", params={"q": "song", "limit": 2, "offset": offset}).json()
        assert page["total"] == 5
        assert len(page["results"]) == {0: 2, 2: 2, 4: 1, 10: 0}[offset]
        seen.extend(x["video_id"] for x in page["results"])
    assert sorted(seen) == sorted(status)


def test_playlist_video_stream(client):
    apply_discovery([PlaylistInfo("PL1", "Sommer Mix")], "setlist")
    pid = client.get("/api/playlists?type=sync").json()[0]["id"]
    name = sanitize_folder_name("Sommer Mix", "PL1")
    with session_scope() as s:
        s.get(Playlist, pid).folder_name = name
    d = client.app.state.settings.data_dir / name
    d.mkdir(parents=True)
    (d / "01 - Song [nAUaWGdv6So].mkv").write_bytes(b"video-bytes-here")
    (d / "01 - Song [nAUaWGdv6So].jpg").write_bytes(b"thumb")

    f = "01 - Song [nAUaWGdv6So].mkv"
    r = client.get(f"/api/playlists/{pid}/video", params={"file": f})
    assert r.status_code == 200
    assert r.headers["content-type"] == "video/x-matroska"
    assert r.headers.get("accept-ranges") == "bytes"
    assert r.content == b"video-bytes-here"

    # range request (seeking)
    r = client.get(f"/api/playlists/{pid}/video", params={"file": f},
                   headers={"Range": "bytes=0-4"})
    assert r.status_code == 206 and r.content == b"video"
    assert r.headers["content-range"] == "bytes 0-4/16"

    # whitelist and traversal
    bad = ["01 - Song [nAUaWGdv6So].jpg", "01 - Song [nAUaWGdv6So].mkv.part",
           "../../app.py", "", "x.EXE"]
    for file in bad:
        assert client.get(f"/api/playlists/{pid}/video", params={"file": file}).status_code == 404, file
    assert client.get("/api/playlists/9999/video", params={"file": f}).status_code == 404


def test_job_log_offset(client, tmp_path):
    apply_discovery([PlaylistInfo("PL1", "Sommer Mix")], "setlist")
    log_file = tmp_path / "job.log"
    log_file.write_bytes(b"line1\nline2\nline3")
    with session_scope() as s:
        pk = s.scalar(select(Playlist.id))
        job = Job(playlist_id=pk, trigger="manual", status="running",
                  queued_at=utcnow(), log_path=str(log_file))
        s.add(job)
        s.flush()
        jid = job.id

    # full text on the first call
    r = client.get(f"/api/jobs/{jid}/log", params={"offset": 0}).json()
    assert r["text"] == "line1\nline2\nline3" and r["offset"] == 17
    assert r["finished"] is False
    # nothing new yet
    r = client.get(f"/api/jobs/{jid}/log", params={"offset": 17}).json()
    assert r["text"] == "" and r["offset"] == 17
    # appended lines are delivered exactly once from the offset
    with open(log_file, "ab") as fh:
        fh.write(b"line4\n")
    r = client.get(f"/api/jobs/{jid}/log", params={"offset": 17}).json()
    assert r["text"] == "line4\n" and r["offset"] == 23
    # offset past the end is valid and returns nothing
    r = client.get(f"/api/jobs/{jid}/log", params={"offset": 9999}).json()
    assert r["text"] == "" and r["offset"] == 9999
    # negative offsets are rejected
    assert client.get(f"/api/jobs/{jid}/log", params={"offset": -1}).status_code == 422
    # finished flag follows the job status
    with session_scope() as s:
        s.get(Job, jid).status = "success"
    assert client.get(f"/api/jobs/{jid}/log", params={"offset": 0}).json()["finished"] is True
