# Copyright (c) 2026 Stefan Koelle (https://stefankoelle.de)
# Licensed under the MIT License. See LICENSE file in project root for details.
"""yt-dlp integration: URLs, command building, output parsing, evaluation, download archives."""
from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROGRESS_PREFIX = "YTPS"
PROGRESS_TEMPLATE = (
    f"download:{PROGRESS_PREFIX}|%(progress._percent_str)s|%(progress._speed_str)s|%(progress._eta_str)s"
)
UNAVAILABLE_TITLES = {"[private video]", "[deleted video]"}
ARCHIVE_NAME = "youtube"

_PROGRESS = re.compile(rf"^{PROGRESS_PREFIX}\|\s*([\d.]+)%\|(.*?)\|(.*)$")
_ITEM = re.compile(r"^\[download\] Downloading item (\d+) of (\d+)")
_DEST = re.compile(r"^\[download\] Destination: (.+)$")
_VIDEO = re.compile(r"^\[youtube\] ([\w-]{11}): Downloading")
_ERROR = re.compile(r"^ERROR:\s*(?:\[youtube[^\]]*\]\s*([\w-]{11}):\s*)?(.*)$")
_RATELIMIT = re.compile(r"HTTP Error 429|Too Many Requests|not a bot|rate.?limit", re.I)
_FORBIDDEN = re.compile(r"HTTP Error 403", re.I)
_PERMANENT = re.compile(
    r"Video unavailable|Private video|video is private|has been removed|no longer available|"
    r"account .* terminated|members-only|Join this channel|confirm your age|age-restricted|"
    r"not available in your country|blocked it|copyright|This video is not available",
    re.I,
)
# Playlist-level verdicts: a playlist that no longer exists or is not accessible to us.
# Deliberately narrow - anything else (network, 429, age checks) must not count as gone.
_GONE = re.compile(
    r"playlist does not exist|this playlist is private|the playlist is private|"
    r"playlist is private|playlist (?:has been|was) removed|playlist unavailable|"
    r"this playlist is unavailable|resource not found|HTTP Error 404",
    re.I,
)


class YtDlpError(RuntimeError):
    pass


@dataclass
class PlaylistInfo:
    id: str
    title: str
    item_count: int | None = None


@dataclass
class VideoEntry:
    id: str
    title: str
    unavailable: bool = False
    position: int = 0
    duration_s: int | None = None


@dataclass
class Evaluation:
    total: int
    archived: int
    unavailable: int
    missing: list[str]
    simulated: int


def channel_playlists_url(channel: str) -> str:
    c = channel.strip()
    if c.startswith(("http://", "https://")):
        c = c.rstrip("/")
        return c if c.endswith("/playlists") else c + "/playlists"
    if c.startswith("@"):
        return f"https://www.youtube.com/{c}/playlists"
    if re.fullmatch(r"UC[\w-]{20,}", c):
        return f"https://www.youtube.com/channel/{c}/playlists"
    return f"https://www.youtube.com/@{c}/playlists"


def playlist_url(playlist_id: str) -> str:
    return f"https://www.youtube.com/playlist?list={playlist_id}"


_PLAYLIST_ID = re.compile(r"[A-Za-z0-9_-]{6,64}")


def playlist_id_from_url(url: str) -> str | None:
    """Playlist id from a bare id, a playlist URL, or any URL carrying ``list=``."""
    u = url.strip()
    if _PLAYLIST_ID.fullmatch(u):
        return u
    match = re.search(r"[?&]list=([A-Za-z0-9_-]+)", u)
    return match.group(1) if match and _PLAYLIST_ID.fullmatch(match.group(1)) else None


def ytdlp_env(config_dir: Path | str) -> dict[str, str]:
    """Environment for yt-dlp; prefers a self-updated copy in <config>/ytdlp-lib."""
    env = dict(os.environ)
    lib = Path(config_dir) / "ytdlp-lib"
    if lib.is_dir():
        old = env.get("PYTHONPATH")
        env["PYTHONPATH"] = f"{lib}{os.pathsep}{old}" if old else str(lib)
    return env


def lib_env(config_dir: Path | str, lib: Path | str) -> dict[str, str]:
    """Like ``ytdlp_env`` but with an explicit library directory (e.g. a staging install)."""
    env = dict(os.environ)
    old = env.get("PYTHONPATH")
    env["PYTHONPATH"] = f"{lib}{os.pathsep}{old}" if old else str(lib)
    return env


def staging_lib_dir(config_dir: Path | str) -> Path:
    """Scratch install directory below <config>/ytdlp-libs (never on PYTHONPATH)."""
    return Path(config_dir) / "ytdlp-libs" / ".staging"


def versioned_lib_dir(config_dir: Path | str, version: str) -> Path:
    """Final install directory for one yt-dlp version below <config>/ytdlp-libs/."""
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", version).strip("._") or "unknown"
    return Path(config_dir) / "ytdlp-libs" / f"yt-dlp-{safe}"


def swap_ytdlp_lib(config_dir: Path | str, target: Path | str) -> None:
    """Point <config>/ytdlp-lib at target with an atomic symlink swap (SPEC 6.7).

    pip --target writes a directory non-atomically, so a download or listing that
    starts mid-update would import a half-written tree. Installing into a versioned
    directory and swapping a symlink keeps both ends complete: every concurrent run
    sees either the old or the new installation. The previously active install is
    parked as ``ytdlp-lib.prev`` (never deleted, an existing ``.prev`` is replaced):
    a pre-symlink real directory directly, a symlink via its target, so an old
    version can be pointed back to by hand. Old versioned installs are rotated to
    the two newest.
    """
    cfg = Path(config_dir)
    link = cfg / "ytdlp-lib"
    target = Path(target)
    if link.is_symlink():
        old_target = Path(os.path.realpath(link))
        if (old_target.exists() and old_target.is_relative_to(cfg)
                and old_target != Path(os.path.realpath(target))):
            _park_prev(cfg, old_target)
    elif link.exists():
        _park_prev(cfg, link)
    tmp = cfg / "ytdlp-lib.tmp"
    if tmp.is_symlink():
        tmp.unlink()
    elif tmp.exists():
        shutil.rmtree(tmp)
    tmp.symlink_to(os.path.relpath(target, cfg))
    os.replace(tmp, link)
    _rotate_lib_dirs(cfg / "ytdlp-libs")


def _park_prev(cfg: Path, path: Path) -> None:
    prev = cfg / "ytdlp-lib.prev"
    if prev.is_dir():
        shutil.rmtree(prev)
    elif prev.exists():
        prev.unlink()
    path.rename(prev)


def _rotate_lib_dirs(libs: Path, keep: int = 2) -> None:
    if not libs.is_dir():
        return
    dirs = [p for p in libs.iterdir() if p.is_dir() and not p.name.startswith(".")]
    dirs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    for old in dirs[keep:]:
        shutil.rmtree(old, ignore_errors=True)


def build_update_command(lib: Path | str) -> list[str]:
    """pip command refreshing yt-dlp into lib.

    The default extra matters: yt-dlp-ejs (the JavaScript challenge solver) is
    only pulled in by yt-dlp[default] and pinned exactly, so pip installs the
    matching ejs into lib whenever the environment copy no longer matches.
    """
    return [
        sys.executable, "-m", "pip", "install", "--upgrade", "--no-warn-script-location",
        "--target", str(lib), "yt-dlp[default]",
    ]


def build_cache_clear_command(ytdlp_bin: str) -> list[str]:
    return [ytdlp_bin, "--rm-cache-dir"]


def parse_playlist_listing(data: dict[str, Any]) -> list[PlaylistInfo]:
    out: dict[str, PlaylistInfo] = {}

    def walk(entries: list[dict[str, Any]]) -> None:
        for e in entries or []:
            if not isinstance(e, dict):
                continue
            if e.get("entries") and not e.get("id", "").startswith(("PL", "UU", "OL", "FL")):
                walk(e["entries"])
                continue
            pid, title = e.get("id"), e.get("title")
            if not pid or not title:
                continue
            count = e.get("playlist_count") or e.get("n_entries")
            out.setdefault(pid, PlaylistInfo(pid, title, int(count) if count else None))

    walk(data.get("entries") or [])
    return list(out.values())


def parse_video_listing(data: dict[str, Any]) -> list[VideoEntry]:
    out: list[VideoEntry] = []
    for i, e in enumerate(data.get("entries") or [], 1):
        if not isinstance(e, dict) or not e.get("id"):
            continue
        title = e.get("title") or ""
        duration = e.get("duration")
        idx = e.get("playlist_index")
        out.append(VideoEntry(
            e["id"], title, title.strip().lower() in UNAVAILABLE_TITLES,
            position=int(idx) if isinstance(idx, int) and idx > 0 else i,
            duration_s=int(duration) if isinstance(duration, (int, float)) and duration > 0 else None,
        ))
    return out


async def _run_json(cmd: list[str], env: dict[str, str], timeout: float = 300) -> dict[str, Any]:
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        proc.kill()
        raise YtDlpError(f"yt-dlp timed out after {timeout:.0f}s") from exc
    text = out.decode("utf-8", errors="replace").strip()
    if not text:
        raise YtDlpError((err.decode("utf-8", errors="replace").strip() or "empty output")[-500:])
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise YtDlpError(f"invalid JSON from yt-dlp: {exc}") from exc


async def list_channel_playlists(ytdlp_bin: str, channel: str, env: dict[str, str]) -> list[PlaylistInfo]:
    cmd = shlex.split(ytdlp_bin) + [
        "--flat-playlist", "-J", "--ignore-errors", "--no-warnings", channel_playlists_url(channel),
    ]
    return parse_playlist_listing(await _run_json(cmd, env))


async def list_playlist_entries(ytdlp_bin: str, playlist_id: str, env: dict[str, str]) -> list[VideoEntry]:
    cmd = shlex.split(ytdlp_bin) + [
        "--flat-playlist", "-J", "--ignore-errors", "--no-warnings", playlist_url(playlist_id),
    ]
    return parse_video_listing(await _run_json(cmd, env))


async def get_version(ytdlp_bin: str, env: dict[str, str]) -> str:
    try:
        proc = await asyncio.create_subprocess_exec(
            *shlex.split(ytdlp_bin), "--version",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, env=env,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
        return out.decode().strip() or "unknown"
    except Exception:
        return "unknown"


def build_download_command(
    *,
    ytdlp_bin: str,
    playlist_id: str,
    folder: str,
    data_dir: Path | str,
    archive_path: Path | str,
    sleep_min: int,
    sleep_max: int,
    extra_args: str = "",
    dry_run: bool = False,
) -> list[str]:
    out_tpl = str(Path(data_dir) / folder / "%(playlist_index)02d - %(title).150B [%(id)s].%(ext)s")
    cmd = shlex.split(ytdlp_bin) + [
        "--ignore-errors", "--no-abort-on-error",
        "--download-archive", str(archive_path),
        "-f", "bv*+ba/b",
        "--merge-output-format", "mp4/mkv",
        "--convert-thumbnails", "jpg",
        "--embed-thumbnail", "--embed-metadata", "--embed-chapters",
        "--write-info-json", "--write-description", "--write-thumbnail",
        "--write-subs", "--sub-langs", "all,-live_chat",
        "--min-sleep-interval", str(sleep_min), "--max-sleep-interval", str(sleep_max),
        "--newline", "--no-colors", "--progress-template", PROGRESS_TEMPLATE,
        "-o", out_tpl,
    ]
    if extra_args.strip():
        cmd += shlex.split(extra_args)
    if dry_run:
        cmd.append("--simulate")
    cmd.append(playlist_url(playlist_id))
    return cmd


def parse_line(line: str) -> dict[str, Any] | None:
    line = line.rstrip("\r\n")
    if m := _PROGRESS.match(line):
        return {
            "type": "progress",
            "percent": float(m.group(1)),
            "speed": m.group(2).strip(),
            "eta": m.group(3).strip(),
        }
    if m := _ITEM.match(line):
        return {"type": "item", "index": int(m.group(1)), "total": int(m.group(2))}
    if m := _DEST.match(line):
        return {"type": "destination", "name": Path(m.group(1)).name}
    if m := _VIDEO.match(line):
        return {"type": "video", "id": m.group(1)}
    if m := _ERROR.match(line):
        msg = m.group(2).strip()
        ratelimit = bool(_RATELIMIT.search(msg))
        forbidden = bool(_FORBIDDEN.search(msg))
        return {
            "type": "error",
            "id": m.group(1),
            "message": msg,
            "ratelimit": ratelimit,
            "forbidden": forbidden,
            "permanent": (not ratelimit) and not forbidden and bool(_PERMANENT.search(msg)),
        }
    return None


def is_forbidden(text: str) -> bool:
    return bool(_FORBIDDEN.search(text))


def is_gone(text: str) -> bool:
    """True when yt-dlp says the playlist itself is gone (deleted, private or 404)."""
    return bool(_GONE.search(text))


def read_archive(path: Path | str) -> set[str]:
    p = Path(path)
    if not p.exists():
        return set()
    ids: set[str] = set()
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.split()
        if len(parts) >= 2:
            ids.add(parts[-1])
    return ids


def append_archive(path: Path | str, ids: list[str], dry_run: bool) -> int:
    """Append missing ``youtube <id>`` lines; existing lines are never touched."""
    path = Path(path)
    existing = read_archive(path)
    fresh = [v for v in ids if v not in existing]
    if fresh and not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            for vid in fresh:
                fh.write(f"{ARCHIVE_NAME} {vid}\n")
    return len(fresh)


def evaluate(
    entries: list[VideoEntry], archive_ids: set[str], errors: list[dict[str, Any]], dry_run: bool
) -> Evaluation:
    ids = [e.id for e in entries]
    perm = {e["id"] for e in errors if e.get("id") and e.get("permanent")}
    unavailable = {e.id for e in entries if e.unavailable} | (perm & set(ids))
    pending = [i for i in ids if i not in archive_ids and i not in unavailable]
    return Evaluation(
        total=len(ids),
        archived=len([i for i in ids if i in archive_ids]),
        unavailable=len(unavailable),
        missing=[] if dry_run else pending,
        simulated=len(pending) if dry_run else 0,
    )


def folder_size(path: Path | str) -> int:
    total = 0
    p = Path(path)
    if not p.exists():
        return 0
    for root, _dirs, files in os.walk(p):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


_STEM = re.compile(r"^(\d+) - (.+) \[([^\][]+)\]$")
_THUMB_EXTS = (".jpg", ".jpeg", ".png", ".webp")
_VIDEO_EXTS = {".mkv", ".mp4", ".webm"}
_SIDECARS = (".jpg", ".jpeg", ".png", ".webp", ".info.json", ".description")

# info.json files are ~100 KB each; cache parsed results keyed by (mtime_ns, size).
_INFO_CACHE: dict[str, tuple[int, int, dict[str, Any]]] = {}
_INFO_CACHE_MAX = 4096


def _read_info(path: Path) -> dict[str, Any]:
    try:
        st = path.stat()
        key = str(path)
    except OSError:
        return {}
    hit = _INFO_CACHE.get(key)
    if hit is not None and hit[0] == st.st_mtime_ns and hit[1] == st.st_size:
        return hit[2]
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    if len(_INFO_CACHE) >= _INFO_CACHE_MAX:
        _INFO_CACHE.clear()
    _INFO_CACHE[key] = (st.st_mtime_ns, st.st_size, data)
    return data


def _find_cover(names: list[str], playlist_id: str) -> str | None:
    for n in names:
        if n.startswith("00 - ") and n.endswith(".jpg") and f"[{playlist_id}]" in n:
            return n
    for n in names:
        if n.startswith("00 - ") and n.endswith(".jpg"):
            return n
    return None


def _resolution(info: dict[str, Any]) -> str | None:
    """``1080x1080`` from the merged format, or None if incomplete."""
    w, h = info.get("width"), info.get("height")
    if isinstance(w, (int, float)) and isinstance(h, (int, float)) and w > 0 and h > 0:
        return f"{int(w)}x{int(h)}"
    return None


def _codec(info: dict[str, Any], key: str) -> str | None:
    """Codec name; yt-dlp sets the unused stream of a merged format to 'none'."""
    v = info.get(key)
    return v if isinstance(v, str) and v and v != "none" else None


def _bitrate(info: dict[str, Any], key: str) -> float | None:
    """Bitrate in kbps from the merged format."""
    v = info.get(key)
    return float(v) if isinstance(v, (int, float)) and v > 0 else None


def read_video_entries(folder: Path | str, playlist_id: str) -> dict[str, Any]:
    """Parse a yt-dlp playlist folder into a cover plus per-video gallery entries.

    Uses os.listdir (not glob): folder names contain ``[...]`` which glob would
    treat as a character class. Broken info.json falls back to the file name.
    """
    d = Path(folder)
    empty: dict[str, Any] = {
        "exists": False, "cover": None, "video_count": 0, "total_duration_s": 0, "videos": [],
    }
    if not d.is_dir():
        return empty
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return empty
    entries: list[dict[str, Any]] = []
    for name in names:
        p = d / name
        if p.suffix.lower() not in _VIDEO_EXTS:
            continue
        stem = p.stem
        m = _STEM.match(stem)
        index = int(m.group(1)) if m else 0
        video_id = m.group(3) if m else ""
        info = _read_info(d / f"{stem}.info.json")
        title = str(info.get("title") or (m.group(2) if m else stem))
        duration = info.get("duration")
        thumb = next((f"{stem}{e}" for e in _THUMB_EXTS if (d / f"{stem}{e}").is_file()), None)
        sidecars = [p.suffix[1:]] + [s.lstrip(".") for s in _SIDECARS if (d / f"{stem}{s}").is_file()]
        try:
            size = p.stat().st_size
        except OSError:
            size = 0
        entries.append({
            "index": index, "video_id": video_id, "title": title, "file": name,
            "thumb": thumb,
            "duration_s": duration if isinstance(duration, (int, float)) else None,
            "upload_date": info.get("upload_date"),
            "view_count": info.get("view_count"),
            "like_count": info.get("like_count"),
            "channel": info.get("channel") or info.get("uploader"),
            "resolution": _resolution(info),
            "vcodec": _codec(info, "vcodec"),
            "acodec": _codec(info, "acodec"),
            "vbr": _bitrate(info, "vbr"),
            "abr": _bitrate(info, "abr"),
            "size_bytes": size, "sidecars": sidecars,
        })
    entries.sort(key=lambda e: (e["index"], e["file"]))
    return {
        "exists": True, "cover": _find_cover(names, playlist_id),
        "video_count": len(entries),
        "total_duration_s": int(sum(e["duration_s"] for e in entries if e["duration_s"])),
        "videos": entries,
    }
