from pathlib import Path

from app import ytdlp


def test_channel_urls():
    assert ytdlp.channel_playlists_url("@beispielkanal") == "https://www.youtube.com/@beispielkanal/playlists"
    assert ytdlp.channel_playlists_url("UC" + "a" * 22) == f"https://www.youtube.com/channel/UC{'a' * 22}/playlists"
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


def test_parse_line_events():
    assert ytdlp.parse_line("YTPS| 42.5%|1.2MiB/s|00:10") == {
        "type": "progress", "percent": 42.5, "speed": "1.2MiB/s", "eta": "00:10"}
    assert ytdlp.parse_line("[download] Downloading item 2 of 7") == {"type": "item", "index": 2, "total": 7}
    assert ytdlp.parse_line("[download] Destination: /data/x/01 - A [abc].mp4")["name"] == "01 - A [abc].mp4"
    assert ytdlp.parse_line("[youtube] abcdefghijk: Downloading webpage") == {"type": "video", "id": "abcdefghijk"}
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
