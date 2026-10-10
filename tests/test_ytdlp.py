# Copyright (c) 2026 Stefan Koelle (https://stefankoelle.de)
# Licensed under the MIT License. See LICENSE file in project root for details.
import os
from pathlib import Path

from app import ytdlp


def test_channel_urls():
    assert ytdlp.channel_playlists_url("@beispielkanal") == "https://www.youtube.com/@beispielkanal/playlists"
    assert ytdlp.channel_playlists_url("UC" + "a" * 22) == (
        f"https://www.youtube.com/channel/UC{'a' * 22}/playlists"
    )
    assert ytdlp.channel_playlists_url("https://www.youtube.com/@x/") == "https://www.youtube.com/@x/playlists"
    assert ytdlp.channel_playlists_url("https://www.youtube.com/@x/playlists").count("playlists") == 1
    assert ytdlp.channel_playlists_url("beispiel") == "https://www.youtube.com/@beispiel/playlists"


def test_parse_playlist_listing_flat_and_nested():
    data = {"entries": [
        {"id": "PL1", "title": "A", "playlist_count": 5},
        {"id": "UCx", "title": "tab", "entries": [{"id": "PL2", "title": "B"}]},
        {"id": "", "title": "bad"},
        {"id": "PL1", "title": "dup"},
    ]}
    infos = ytdlp.parse_playlist_listing(data)
    assert [(i.id, i.title, i.item_count) for i in infos] == [("PL1", "A", 5), ("PL2", "B", None)]


def test_parse_video_listing_marks_unavailable():
    data = {"entries": [{"id": "a", "title": "ok"}, {"id": "b", "title": "[Private video]"},
                        {"id": "c", "title": "[Deleted video]"}]}
    assert [v.unavailable for v in ytdlp.parse_video_listing(data)] == [False, True, True]


def test_parse_video_listing_positions_and_duration():
    data = {"entries": [
        {"id": "a", "title": "A", "duration": 61.5},
        {"id": "b", "title": "B", "duration": None},
        {"id": "c", "title": "C"},
        {"id": "", "title": "skip me"},
        {"id": "d", "title": "D", "playlist_index": 9},
    ]}
    out = ytdlp.parse_video_listing(data)
    assert [(e.id, e.position, e.duration_s) for e in out] == [
        ("a", 1, 61), ("b", 2, None), ("c", 3, None), ("d", 9, None),
    ]


def test_parse_line_events():
    assert ytdlp.parse_line("YTPS| 42.5%|1.2MiB/s|00:10") == {
        "type": "progress", "percent": 42.5, "speed": "1.2MiB/s", "eta": "00:10"}
    assert ytdlp.parse_line("[download] Downloading item 2 of 7") == {"type": "item", "index": 2, "total": 7}
    assert ytdlp.parse_line("[download] Destination: /data/x/01 - A [abc].mp4")["name"] == "01 - A [abc].mp4"
    assert ytdlp.parse_line("[youtube] abcdefghijk: Downloading webpage") == {
        "type": "video",
        "id": "abcdefghijk",
    }
    assert ytdlp.parse_line("some other line") is None


def test_parse_line_error_classes():
    e = ytdlp.parse_line("ERROR: [youtube] abcdefghijk: Video unavailable")
    assert e["id"] == "abcdefghijk" and e["permanent"] and not e["ratelimit"]
    e = ytdlp.parse_line("ERROR: [youtube] abcdefghijk: Private video. Sign in if you've been granted access")
    assert e["permanent"]
    e = ytdlp.parse_line("ERROR: [youtube] abcdefghijk: HTTP Error 429: Too Many Requests")
    assert e["ratelimit"] and not e["permanent"]
    e = ytdlp.parse_line("ERROR: [youtube] abcdefghijk: Sign in to confirm you\u2019re not a bot")
    assert e["ratelimit"]
    e = ytdlp.parse_line("ERROR: [youtube] abcdefghijk: Unable to download webpage: HTTP Error 503")
    assert not e["permanent"] and not e["ratelimit"]
    e = ytdlp.parse_line("ERROR: unable to download video data: HTTP Error 403: Forbidden")
    assert e["forbidden"] and not e["ratelimit"] and not e["permanent"] and e["id"] is None
    e = ytdlp.parse_line("ERROR: [youtube] abcdefghijk: HTTP Error 429: Too Many Requests")
    assert not e["forbidden"]
    assert ytdlp.is_forbidden("Could not list playlist: HTTP Error 403: Forbidden")
    assert not ytdlp.is_forbidden("HTTP Error 503: Service Unavailable")
    e = ytdlp.parse_line("ERROR: Postprocessing: ffmpeg exited")
    assert e["id"] is None


def test_evaluate():
    entries = [ytdlp.VideoEntry("a", "A"), ytdlp.VideoEntry("b", "B"),
               ytdlp.VideoEntry("c", "[Private video]", True), ytdlp.VideoEntry("d", "D")]
    errors = [{"id": "d", "permanent": True, "message": "x"}]
    ev = ytdlp.evaluate(entries, {"a"}, errors, dry_run=False)
    assert ev.missing == ["b"] and ev.unavailable == 2 and ev.archived == 1
    ev = ytdlp.evaluate(entries, {"a"}, errors, dry_run=True)
    assert ev.missing == [] and ev.simulated == 1


def test_read_archive(tmp_path: Path):
    f = tmp_path / "a.txt"
    f.write_text("youtube abc\n\nyoutube def\n")
    assert ytdlp.read_archive(f) == {"abc", "def"}
    assert ytdlp.read_archive(tmp_path / "missing.txt") == set()


def test_build_command():
    cmd = ytdlp.build_download_command(
        ytdlp_bin="yt-dlp", playlist_id="PL1", folder="F [PL1]", data_dir="/data",
        archive_path="/config/archives/PL1.txt", sleep_min=3, sleep_max=10, extra_args="--limit-rate 5M",
        dry_run=True)
    assert cmd[0] == "yt-dlp" and cmd[-1] == "https://www.youtube.com/playlist?list=PL1"
    assert "--simulate" in cmd and "--limit-rate" in cmd
    assert cmd[cmd.index("--download-archive") + 1] == "/config/archives/PL1.txt"
    assert cmd[cmd.index("--merge-output-format") + 1] == "mp4/mkv"
    assert cmd[cmd.index("-f") + 1] == "bv*+ba/b"
    assert cmd[cmd.index("-o") + 1].startswith("/data/F [PL1]/")
    cmd2 = ytdlp.build_download_command(
        ytdlp_bin="yt-dlp", playlist_id="PL1", folder="F", data_dir="/data",
        archive_path="/a.txt", sleep_min=1, sleep_max=2, dry_run=False)
    assert "--simulate" not in cmd2


def test_build_update_command():
    cmd = ytdlp.build_update_command("/config/ytdlp-libs/.staging")
    # the default extra is required: yt-dlp-ejs hangs on it and must be updated too
    assert cmd[-1] == "yt-dlp[default]" and "yt-dlp" not in cmd[:-1]
    assert "--upgrade" in cmd and "--target" in cmd
    assert cmd[cmd.index("--target") + 1] == "/config/ytdlp-libs/.staging"


def test_build_cache_clear_command():
    assert ytdlp.build_cache_clear_command("yt-dlp") == ["yt-dlp", "--rm-cache-dir"]


def test_read_video_entries(tmp_path):
    import json as _json

    # folder name contains brackets: glob would treat them as a character class
    folder = tmp_path / "Setlist [LIVE] Mix"
    folder.mkdir()
    (folder / "00 - Setlist [LIVE] Mix [PLABC1234567].jpg").write_bytes(b"cover")
    (folder / "00 - Setlist [LIVE] Mix [PLABC1234567].info.json").write_text("{}")
    (folder / "01 - No Son [nAUaWGdv6So].mkv").write_bytes(b"v" * 10)
    (folder / "01 - No Son [nAUaWGdv6So].jpg").write_bytes(b"t")
    (folder / "01 - No Son [nAUaWGdv6So].description").write_text("desc")
    (folder / "01 - No Son [nAUaWGdv6So].info.json").write_text(_json.dumps({
        "title": "No Son", "duration": 79, "upload_date": "20251127",
        "view_count": 3816, "like_count": 56, "channel": "Excide - Topic",
        "width": 1080, "height": 1080, "vcodec": "av01.0.08M.08", "vbr": 401.524,
        "acodec": "opus", "abr": 122.52,
    }))
    (folder / "02 - Second Song [abcdefghijk].mp4").write_bytes(b"v")
    (folder / "02 - Second Song [abcdefghijk].info.json").write_text("{broken json")
    (folder / "03 - No Streams [ABCDEFGHIJK].mkv").write_bytes(b"v")
    (folder / "03 - No Streams [ABCDEFGHIJK].info.json").write_text(_json.dumps({
        "title": "No Streams", "duration": 10, "vcodec": "none", "acodec": "none",
        "vbr": 0, "width": None, "height": 1080,
    }))

    out = ytdlp.read_video_entries(folder, "PLABC1234567")
    assert out["exists"] is True
    assert out["cover"] == "00 - Setlist [LIVE] Mix [PLABC1234567].jpg"
    assert out["video_count"] == 3
    assert out["total_duration_s"] == 89

    first, second, third = out["videos"]
    assert first["index"] == 1 and first["video_id"] == "nAUaWGdv6So"
    assert first["title"] == "No Son" and first["file"] == "01 - No Son [nAUaWGdv6So].mkv"
    assert first["thumb"] == "01 - No Son [nAUaWGdv6So].jpg"
    assert first["duration_s"] == 79 and first["upload_date"] == "20251127"
    assert first["view_count"] == 3816 and first["like_count"] == 56
    assert first["channel"] == "Excide - Topic" and first["size_bytes"] == 10
    assert "mkv" in first["sidecars"] and "jpg" in first["sidecars"]
    assert "info.json" in first["sidecars"] and "description" in first["sidecars"]
    # technical metadata from the merged format
    assert first["resolution"] == "1080x1080"
    assert first["vcodec"] == "av01.0.08M.08" and first["vbr"] == 401.524
    assert first["acodec"] == "opus" and first["abr"] == 122.52
    # broken info.json falls back to the file name
    assert second["title"] == "Second Song" and second["duration_s"] is None
    assert second["thumb"] is None and second["size_bytes"] == 1
    assert second["resolution"] is None and second["vcodec"] is None
    # 'none' codecs and zero bitrate are dropped
    assert third["vcodec"] is None and third["acodec"] is None
    assert third["vbr"] is None and third["abr"] is None
    assert third["resolution"] is None

    # cover fallback: playlist id mismatch still finds the 00-*.jpg
    assert ytdlp.read_video_entries(folder, "PLNOMATCH")["cover"].startswith("00 - ")
    # missing folder
    out = ytdlp.read_video_entries(tmp_path / "nope", "PL1")
    assert out["exists"] is False and out["videos"] == [] and out["cover"] is None


def test_is_gone():
    assert ytdlp.is_gone("Could not list playlist: ERROR: [youtube:playlist] PL1: "
                         "The playlist does not exist")
    assert ytdlp.is_gone("ERROR: [youtube:playlist] PL1: This playlist is private")
    assert ytdlp.is_gone("ERROR: [youtube:playlist] PL1: The playlist has been removed")
    assert ytdlp.is_gone("ERROR: [youtube:playlist] PL1: Resource not found")
    assert ytdlp.is_gone("HTTP Error 404: Not Found")
    # transient or unrelated errors must never count as gone
    assert not ytdlp.is_gone("HTTP Error 403: Forbidden")
    assert not ytdlp.is_gone("HTTP Error 429: Too Many Requests")
    assert not ytdlp.is_gone("Sign in to confirm your age")
    assert not ytdlp.is_gone("yt-dlp timed out after 60s")
    assert not ytdlp.is_gone("empty output")


def test_append_archive_is_additive(tmp_path):
    p = tmp_path / "archives" / "PL1.txt"
    assert ytdlp.append_archive(p, ["vidA", "vidB"], dry_run=False) == 2
    assert p.read_text() == "youtube vidA\nyoutube vidB\n"
    # existing lines are never touched, known ids are not repeated
    assert ytdlp.append_archive(p, ["vidB", "vidC"], dry_run=False) == 1
    assert p.read_text() == "youtube vidA\nyoutube vidB\nyoutube vidC\n"
    # dry-run counts but writes nothing
    assert ytdlp.append_archive(p, ["vidD"], dry_run=True) == 1
    assert p.read_text().count("vidD") == 0
    assert ytdlp.read_archive(p) == {"vidA", "vidB", "vidC"}


def test_lib_env_puts_staging_first(tmp_path):
    env = ytdlp.lib_env(tmp_path, tmp_path / "ytdlp-libs" / ".staging")
    parts = env["PYTHONPATH"].split(os.pathsep)
    assert parts[0] == str(tmp_path / "ytdlp-libs" / ".staging")


def test_playlist_id_from_url():
    pid = "PLLEkTmNIRKoYVBjXHuqiWQUk-Q9ZMOC_jm"
    assert ytdlp.playlist_id_from_url(pid) == pid
    assert ytdlp.playlist_id_from_url(f"https://www.youtube.com/playlist?list={pid}") == pid
    assert ytdlp.playlist_id_from_url(f"https://youtube.com/watch?v=abcdefghijk&list={pid}&index=1") == pid
    assert ytdlp.playlist_id_from_url(f"https://youtu.be/abcdefghijk?list={pid}") == pid
    assert ytdlp.playlist_id_from_url(f"  {pid}  ") == pid
    assert ytdlp.playlist_id_from_url("https://www.youtube.com/@beispielkanal/playlists") is None
    assert ytdlp.playlist_id_from_url("https://www.youtube.com/watch?v=abcdefghijk") is None
    assert ytdlp.playlist_id_from_url("") is None
    assert ytdlp.playlist_id_from_url("https://www.youtube.com/playlist?list=") is None


def test_versioned_and_staging_lib_dirs(tmp_path):
    assert ytdlp.staging_lib_dir(tmp_path) == tmp_path / "ytdlp-libs" / ".staging"
    assert ytdlp.versioned_lib_dir(tmp_path, "2025.10.08") == tmp_path / "ytdlp-libs" / "yt-dlp-2025.10.08"
    # versions are sanitized so a hostile string cannot escape the directory
    assert ytdlp.versioned_lib_dir(tmp_path, "../etc").name == "yt-dlp-etc"


def test_swap_ytdlp_lib_parks_real_dir_and_flips_symlink(tmp_path):
    cfg = tmp_path
    old = cfg / "ytdlp-lib"
    old.mkdir()
    (old / "f.txt").write_text("old")
    target = ytdlp.versioned_lib_dir(cfg, "1.2.3")
    target.mkdir(parents=True)
    (target / "f.txt").write_text("new")

    ytdlp.swap_ytdlp_lib(cfg, target)
    link = cfg / "ytdlp-lib"
    assert link.is_symlink()
    assert (link / "f.txt").read_text() == "new"
    prev = cfg / "ytdlp-lib.prev"
    assert prev.is_dir() and not prev.is_symlink()
    assert (prev / "f.txt").read_text() == "old"

    # a second swap replaces .prev and follows the new target
    t2 = ytdlp.versioned_lib_dir(cfg, "2.0.0")
    t2.mkdir(parents=True)
    (t2 / "f.txt").write_text("newer")
    ytdlp.swap_ytdlp_lib(cfg, t2)
    assert (link / "f.txt").read_text() == "newer"
    assert (prev / "f.txt").read_text() == "new"


def test_swap_rotates_old_versions_but_never_deletes_unrelated(tmp_path):
    cfg = tmp_path
    versions = []
    for name in ("2024.01.01", "2024.06.01", "2025.01.01"):
        d = ytdlp.versioned_lib_dir(cfg, name)
        d.mkdir(parents=True)
        versions.append(d)
    os.utime(versions[0], (1, 1))
    os.utime(versions[1], (2, 2))
    os.utime(versions[2], (3, 3))
    link = cfg / "ytdlp-lib"
    link.symlink_to(os.path.relpath(versions[2], cfg))

    ytdlp.swap_ytdlp_lib(cfg, versions[2])
    assert not versions[0].exists()
    assert versions[1].exists() and versions[2].exists()
    assert link.is_symlink()
