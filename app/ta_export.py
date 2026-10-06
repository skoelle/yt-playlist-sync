# Copyright (c) 2026 Stefan Koelle (https://stefankoelle.de)
# Licensed under the MIT License. See LICENSE file in project root for details.
"""TubeArchivist export into the yt-playlist-sync folder layout (standalone, no DB).

Reads playlists and video metadata over the TubeArchivist REST API, resolves the
media files on disk and writes an export tree that matches what yt-dlp produces:
``<folder>/NN - <title> [<video id>].<ext>`` with sidecars, a playlist cover,
a download archive and a ``manifest.json`` holding everything a later DB import
needs. Never deletes, never overwrites, idempotent on rerun.
"""
from __future__ import annotations

import argparse
import errno
import json
import logging
import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from .paths import sanitize_filename, sanitize_folder_name
from .ytdlp import append_archive

log = logging.getLogger(__name__)

API_PLAYLISTS = "/api/playlist/"
API_VIDEOS = "/api/video/"
MANIFEST_NAME = "manifest.json"
_THUMB_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
_VIDEO_EXTS = {".mp4", ".mkv", ".webm"}


class TaError(RuntimeError):
    """Fatal TubeArchivist access problem (auth, connectivity, shape)."""


@dataclass
class TaEntry:
    video_id: str
    title: str
    uploader: str = ""
    idx: int = 0
    downloaded: bool = True


@dataclass
class TaPlaylist:
    playlist_id: str
    title: str
    channel: str = ""
    last_refresh: str | None = None
    entries: list[TaEntry] = field(default_factory=list)


@dataclass
class VideoMeta:
    video_id: str
    title: str = ""
    description: str = ""
    published: str | None = None
    duration_s: int | None = None
    view_count: int | None = None
    like_count: int | None = None
    media_url: str = ""
    thumb_url: str = ""
    width: int | None = None
    height: int | None = None
    vcodec: str | None = None
    acodec: str | None = None
    vbr: float | None = None
    abr: float | None = None


@dataclass
class PlaylistReport:
    playlist_id: str
    title: str
    folder: str = ""
    total: int = 0
    copied: int = 0
    hardlinked: int = 0
    skipped: int = 0
    missing: int = 0
    failed: int = 0
    repaired: int = 0
    cover: bool = False
    bytes_copied: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass
class ExportReport:
    playlists: list[PlaylistReport] = field(default_factory=list)
    dry_run: bool = False
    metadata_only: bool = False

    @property
    def bytes_copied(self) -> int:
        return sum(p.bytes_copied for p in self.playlists)

    @property
    def failed(self) -> int:
        return sum(p.failed for p in self.playlists)

    @property
    def missing(self) -> int:
        return sum(p.missing for p in self.playlists)

    def totals(self) -> dict[str, int]:
        return {
            "playlists": len(self.playlists),
            "videos": sum(p.total for p in self.playlists),
            "copied": sum(p.copied for p in self.playlists),
            "hardlinked": sum(p.hardlinked for p in self.playlists),
            "skipped": sum(p.skipped for p in self.playlists),
            "missing": sum(p.missing for p in self.playlists),
            "failed": sum(p.failed for p in self.playlists),
            "repaired": sum(p.repaired for p in self.playlists),
            "bytes": self.bytes_copied,
        }


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _float(value: Any) -> float | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _kbps(value: Any) -> float | None:
    bps = _float(value)
    return round(bps / 1000, 1) if bps and bps > 0 else None


def parse_playlist(raw: dict[str, Any]) -> TaPlaylist | None:
    """One playlist document; entries are ordered by their TA ``idx`` (0 based)."""
    pid, title = raw.get("playlist_id"), raw.get("playlist_name")
    if not pid or not title:
        return None
    entries: list[TaEntry] = []
    for pos, item in enumerate(raw.get("playlist_entries") or []):
        if not isinstance(item, dict) or not item.get("youtube_id"):
            continue
        idx = _int(item.get("idx"))
        entries.append(TaEntry(
            video_id=str(item["youtube_id"]),
            title=str(item.get("title") or ""),
            uploader=str(item.get("uploader") or ""),
            idx=idx if idx is not None and idx >= 0 else pos,
            downloaded=bool(item.get("downloaded", True)),
        ))
    entries.sort(key=lambda e: e.idx)
    return TaPlaylist(
        playlist_id=str(pid),
        title=str(title),
        channel=str(raw.get("playlist_channel") or ""),
        last_refresh=str(raw["playlist_last_refresh"]) if raw.get("playlist_last_refresh") else None,
        entries=entries,
    )


def parse_video(raw: dict[str, Any]) -> VideoMeta | None:
    """One video document with the fields our info.json needs."""
    vid = raw.get("youtube_id")
    if not vid:
        return None
    streams = [s for s in (raw.get("streams") or []) if isinstance(s, dict)]
    vstream = next((s for s in streams if s.get("type") == "video"), None)
    astream = next((s for s in streams if s.get("type") == "audio"), None)
    stats = raw.get("stats") if isinstance(raw.get("stats"), dict) else {}
    player = raw.get("player") if isinstance(raw.get("player"), dict) else {}
    return VideoMeta(
        video_id=str(vid),
        title=str(raw.get("title") or ""),
        description=str(raw.get("description") or ""),
        published=str(raw["published"]) if raw.get("published") else None,
        duration_s=_int(player.get("duration")),
        view_count=_int(stats.get("view_count")),
        like_count=_int(stats.get("like_count")),
        media_url=str(raw.get("media_url") or ""),
        thumb_url=str(raw.get("vid_thumb_url") or ""),
        width=_int(vstream.get("width")) if vstream else None,
        height=_int(vstream.get("height")) if vstream else None,
        vcodec=str(vstream["codec"]) if vstream and vstream.get("codec") else None,
        acodec=str(astream["codec"]) if astream and astream.get("codec") else None,
        vbr=_kbps(vstream.get("bitrate")) if vstream else None,
        abr=_kbps(astream.get("bitrate")) if astream else None,
    )


class TaClient:
    """Minimal TubeArchivist REST client (Token auth, page based pagination)."""

    def __init__(
        self,
        url: str,
        token: str,
        host_header: str = "",
        timeout: float = 60.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        headers = {"Authorization": f"Token {token}", "Accept": "application/json"}
        if host_header:
            headers["Host"] = host_header
        self._http = httpx.Client(
            base_url=url.rstrip("/"), headers=headers, timeout=timeout, transport=transport,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> TaClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _page(self, path: str, page: int) -> tuple[list[Any], dict[str, Any]]:
        try:
            resp = self._http.get(path, params={"page": page})
        except httpx.HTTPError as exc:
            raise TaError(f"{path}: {exc}") from exc
        if resp.status_code in (401, 403):
            raise TaError(f"{path}: authentication failed (HTTP {resp.status_code})")
        if resp.status_code != 200:
            raise TaError(f"{path}: HTTP {resp.status_code}")
        try:
            payload = resp.json()
        except ValueError as exc:
            raise TaError(f"{path}: response is not JSON") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise TaError(f"{path}: unexpected response shape")
        paginate = payload.get("paginate")
        return payload["data"], paginate if isinstance(paginate, dict) else {}

    def _all(self, path: str) -> list[Any]:
        out: list[Any] = []
        page = 1
        while True:
            data, paginate = self._page(path, page)
            out.extend(data)
            last = _int(paginate.get("last_page")) or page
            if page >= last or not data:
                break
            page += 1
        return out

    def playlists(self) -> list[TaPlaylist]:
        found = [parse_playlist(raw) for raw in self._all(API_PLAYLISTS)]
        return [p for p in found if p is not None]

    def videos(self) -> dict[str, VideoMeta]:
        out: dict[str, VideoMeta] = {}
        for raw in self._all(API_VIDEOS):
            meta = parse_video(raw)
            if meta is not None:
                out[meta.video_id] = meta
        return out


def index_media(media_root: Path | str) -> dict[str, Path]:
    """Map ``<video id> -> media file`` for the TA library (``<root>/media/<channel>/``)."""
    root = Path(media_root) / "media"
    if not root.is_dir():
        raise TaError(f"media root not found: {root}")
    index: dict[str, Path] = {}
    try:
        channels = sorted(p for p in root.iterdir() if p.is_dir())
    except OSError as exc:
        raise TaError(f"cannot read media root: {exc}") from exc
    for channel in channels:
        try:
            files = sorted(p for p in channel.iterdir() if p.is_file())
        except OSError:
            continue
        for path in files:
            if path.suffix.lower() in _VIDEO_EXTS:
                index.setdefault(path.stem, path)
    return index


def build_info_json(
    meta: VideoMeta | None, entry: TaEntry, playlist: TaPlaylist, index: int, ext: str
) -> dict[str, Any]:
    """yt-dlp shaped info.json so the gallery can read titles, counts and codecs."""
    title = (meta.title if meta and meta.title else entry.title) or entry.video_id
    upload_date = meta.published[:10].replace("-", "") if meta and meta.published else None
    return {
        "id": entry.video_id,
        "title": title,
        "ext": ext.lstrip("."),
        "webpage_url": f"https://www.youtube.com/watch?v={entry.video_id}",
        "uploader": entry.uploader or None,
        "channel": entry.uploader or playlist.channel or None,
        "duration": meta.duration_s if meta else None,
        "upload_date": upload_date,
        "view_count": meta.view_count if meta else None,
        "like_count": meta.like_count if meta else None,
        "description": (meta.description if meta else "") or "",
        "width": meta.width if meta else None,
        "height": meta.height if meta else None,
        "vcodec": meta.vcodec if meta else None,
        "acodec": meta.acodec if meta else None,
        "vbr": meta.vbr if meta else None,
        "abr": meta.abr if meta else None,
        "playlist": playlist.playlist_id,
        "playlist_title": playlist.title,
        "playlist_index": index,
    }


def build_manifest(
    playlist: TaPlaylist, rows: list[dict[str, Any]], folder: str, metadata_only: bool
) -> dict[str, Any]:
    """Everything a later DB import needs, independent of the surrounding files."""
    return {
        "source": "tubearchivist",
        "exported_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "metadata_only": metadata_only,
        "playlist_id": playlist.playlist_id,
        "title": playlist.title,
        "channel": playlist.channel,
        "folder_name": folder,
        "remote_item_count": len(playlist.entries),
        "playlist_last_refresh": playlist.last_refresh,
        "entries": rows,
    }


def _safe_id(playlist_id: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in playlist_id) or "unknown"


def _same_device(src: Path, dst_dir: Path) -> bool:
    """True when ``src`` and ``dst_dir`` share a filesystem, so hardlinking can work.

    Unknown (a stat that fails) counts as True: the real ``os.link`` decides then.
    """
    try:
        return src.stat().st_dev == dst_dir.stat().st_dev
    except OSError:
        return True


def _place_file(src: Path, dst: Path, mode: str, dry_run: bool,
                linkable: bool | None = None) -> tuple[int, str]:
    """Create ``dst`` from ``src`` atomically (hardlink or copy).

    Returns ``(bytes transferred, action)`` with action ``skip`` (identical file
    already present), ``link``, ``copy`` or ``repair`` (a partial or truncated
    file was replaced by the complete one). The data goes to ``<name>.part``
    first, so an interrupted run can never leave a broken file under the final
    name. ``linkable`` short-circuits the hardlink attempt when source and
    target are known to live on different filesystems.
    """
    size = src.stat().st_size
    try:
        existing = dst.stat().st_size if dst.exists() else None
    except OSError:
        existing = None
    if existing == size:
        return 0, "skip"
    repair = existing is not None
    if dry_run:
        return size, "repair" if repair else "copy"
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f"{dst.name}.part")
    if tmp.exists():
        tmp.unlink()
    linked = False
    if mode in {"auto", "hardlink"}:
        if linkable is None:
            linkable = _same_device(src, dst.parent)
        if linkable:
            try:
                os.link(src, tmp)
                linked = True
            except OSError as exc:
                if mode == "hardlink":
                    raise
                log.warning("hardlink not possible for %s (%s), copying instead", dst.name, exc)
        elif mode == "hardlink":
            raise OSError(errno.EXDEV, "source and target are on different filesystems")
    if not linked:
        shutil.copy2(src, tmp)
    os.replace(tmp, dst)
    if repair:
        log.warning("repaired truncated file: %s", dst.name)
        return size, "repair"
    return size, "link" if linked else "copy"


def _write_file(path: Path, data: bytes, dry_run: bool) -> bool:
    """Create ``path`` only when it does not exist yet; True when it was written."""
    if path.exists():
        return False
    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.part")
        tmp.write_bytes(data)
        os.replace(tmp, path)
    return True


def _thumb_src(media_root: Path, meta: VideoMeta | None) -> Path | None:
    if meta and meta.thumb_url:
        candidate = media_root / meta.thumb_url.lstrip("/")
        if candidate.is_file():
            return candidate
    return None


def _cover_src(media_root: Path, playlist: TaPlaylist) -> Path | None:
    candidate = media_root / "cache" / "playlists" / f"{playlist.playlist_id}.jpg"
    return candidate if candidate.is_file() else None


def _export_entry(
    playlist: TaPlaylist,
    entry: TaEntry,
    meta: VideoMeta | None,
    media: dict[str, Path],
    media_root: Path,
    dest: Path,
    index: int,
    mode: str,
    metadata_only: bool,
    dry_run: bool,
    report: PlaylistReport,
    linkable: bool | None = None,
) -> dict[str, Any]:
    """Write video and sidecars for one entry; returns the manifest row."""
    src = media.get(entry.video_id)
    ext = src.suffix if src else Path(meta.media_url).suffix if meta and meta.media_url else ".mp4"
    title = (meta.title if meta and meta.title else entry.title) or entry.video_id
    stem = f"{index:02d} - {sanitize_filename(title)} [{entry.video_id}]"
    file_name = f"{stem}{ext}"

    if src is None:
        report.missing += 1
        report.errors.append(f"{entry.video_id}: media file not found")
    elif metadata_only:
        report.skipped += 1
    else:
        try:
            size, action = _place_file(src, dest / file_name, mode, dry_run, linkable)
            report.bytes_copied += size
            if action == "skip":
                report.skipped += 1
            elif action == "link":
                report.hardlinked += 1
            elif action == "repair":
                report.repaired += 1
            else:
                report.copied += 1
        except OSError as exc:
            report.failed += 1
            report.errors.append(f"{entry.video_id}: {exc}")
            log.warning("%s: copy failed: %s", entry.video_id, exc)

    info = build_info_json(meta, entry, playlist, index, ext)
    _write_file(
        dest / f"{stem}.info.json",
        json.dumps(info, ensure_ascii=False, indent=1).encode("utf-8"),
        dry_run,
    )
    if meta and meta.description:
        _write_file(dest / f"{stem}.description", meta.description.encode("utf-8"), dry_run)

    thumb = _thumb_src(media_root, meta)
    if thumb is not None and thumb.suffix.lower() in _THUMB_EXTS:
        try:
            size, _action = _place_file(
                thumb, dest / f"{stem}{thumb.suffix.lower()}", "copy", dry_run
            )
            report.bytes_copied += size
        except OSError as exc:
            report.errors.append(f"{entry.video_id}: thumbnail: {exc}")

    row = {
        "position": index,
        "video_id": entry.video_id,
        "title": title,
        "uploader": entry.uploader,
        "duration_s": info["duration"],
        "published": meta.published if meta else None,
        "downloaded": src is not None,
        "file": file_name if src is not None else None,
    }
    return row


def _export_cover(
    playlist: TaPlaylist, media_root: Path, dest: Path, dry_run: bool, report: PlaylistReport
) -> None:
    cover = _cover_src(media_root, playlist)
    if cover is None:
        return
    name = f"00 - {sanitize_filename(playlist.title)} [{_safe_id(playlist.playlist_id)}].jpg"
    try:
        size, _action = _place_file(cover, dest / name, "copy", dry_run)
        report.bytes_copied += size
        report.cover = True
    except OSError as exc:
        report.errors.append(f"cover: {exc}")


def export(
    client: TaClient,
    *,
    media_root: Path | str,
    target: Path | str,
    mode: str = "auto",
    metadata_only: bool = False,
    dry_run: bool = False,
    with_archives: bool = True,
    playlist_ids: list[str] | None = None,
    progress: Callable[[str], None] | None = None,
) -> ExportReport:
    """Export playlists from TubeArchivist into ``target`` in our folder layout."""
    if mode not in {"auto", "copy", "hardlink"}:
        raise ValueError(f"unsupported mode: {mode}")
    media_root = Path(media_root)
    target = Path(target)
    playlists = client.playlists()
    videos = client.videos()
    media = index_media(media_root)
    if playlist_ids:
        wanted = set(playlist_ids)
        playlists = [p for p in playlists if p.playlist_id in wanted]
        unknown = wanted - {p.playlist_id for p in playlists}
        for pid in sorted(unknown):
            log.warning("playlist not found in TubeArchivist: %s", pid)

    report = ExportReport(dry_run=dry_run, metadata_only=metadata_only)
    device_warned = False
    for playlist in playlists:
        prow = PlaylistReport(playlist_id=playlist.playlist_id, title=playlist.title)
        prow.total = len(playlist.entries)
        prow.folder = sanitize_folder_name(playlist.title, playlist.playlist_id)
        dest = target / prow.folder
        if not dry_run:
            dest.mkdir(parents=True, exist_ok=True)
        linkable: bool | None = None
        if not dry_run and mode in {"auto", "hardlink"}:
            linkable = _same_device(media_root, dest)
            if not linkable and not device_warned:
                log.warning(
                    "source %s and target %s are on different filesystems: "
                    "hardlinks are impossible, copying instead",
                    media_root, target,
                )
                device_warned = True
        if progress:
            progress(f"{prow.folder}: {prow.total} entries")

        rows: list[dict[str, Any]] = []
        archived: list[str] = []
        for entry in playlist.entries:
            meta = videos.get(entry.video_id)
            row = _export_entry(
                playlist, entry, meta, media, media_root, dest, entry.idx + 1, mode,
                metadata_only, dry_run, prow, linkable,
            )
            rows.append(row)
            if row["file"]:
                archived.append(entry.video_id)

        _export_cover(playlist, media_root, dest, dry_run, prow)
        _write_file(
            dest / MANIFEST_NAME,
            json.dumps(build_manifest(playlist, rows, prow.folder, metadata_only),
                       ensure_ascii=False, indent=1).encode("utf-8"),
            dry_run,
        )
        if with_archives:
            append_archive(target / "archives" / f"{playlist.playlist_id}.txt", archived, dry_run)
        report.playlists.append(prow)
        if progress:
            progress(
                f"{prow.folder}: linked={prow.hardlinked} copied={prow.copied} "
                f"repaired={prow.repaired} skipped={prow.skipped} "
                f"missing={prow.missing} failed={prow.failed}"
            )
    return report


def load_dotenv(path: Path | str) -> dict[str, str]:
    """Minimal ``.env`` reader (KEY=VALUE, ``#`` comments); the shell environment wins."""
    file = Path(path)
    values: dict[str, str] = {}
    if not file.is_file():
        return values
    for line in file.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key:
            values[key] = value.strip().strip('"').strip("'")
    return values


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def _human(size: float) -> str:
    for unit in ("B", "K", "M", "G", "T"):
        if size < 1024 or unit == "T":
            return f"{size:.1f}{unit}" if unit != "B" else f"{int(size)}B"
        size /= 1024
    return f"{size:.1f}T"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.ta_export",
        description="Export TubeArchivist playlists into the yt-playlist-sync folder layout.",
    )
    parser.add_argument("--env-file", default="", help=".env file (default: <repo>/.env)")
    parser.add_argument("--url", default="", help="TubeArchivist base URL, env TA_API_URL")
    parser.add_argument("--token", default="", help="API token, env TA_API_TOKEN")
    parser.add_argument("--host-header", default="", help="Host header override, env TA_API_HOST")
    parser.add_argument("--target", default="", help="export root, env TA_EXPORT_TARGET")
    parser.add_argument("--media-root", default="", help="TubeArchivist data dir, env TA_MEDIA_ROOT")
    parser.add_argument("--mode", choices=("auto", "copy", "hardlink"), default="auto",
                        help="auto = hardlink with copy fallback (default)")
    parser.add_argument("--metadata-only", action="store_true",
                        help="write metadata, archives and manifests but no video files")
    parser.add_argument("--dry-run", action="store_true", help="write nothing, only report")
    parser.add_argument("--playlist", action="append", metavar="PLAYLIST_ID",
                        help="export only this playlist (repeatable)")
    parser.add_argument("--no-archives", action="store_true", help="skip archives/<pid>.txt files")
    parser.add_argument("--timeout", type=float, default=60.0, help="HTTP timeout per request")
    parser.add_argument("-q", "--quiet", action="store_true", help="only print the final table")
    return parser


def _print_report(report: ExportReport, target: str) -> None:
    print()
    header = (f"{'playlist':40} {'tot':>5} {'link':>5} {'copy':>5} {'rep':>4} "
              f"{'skip':>5} {'miss':>5} {'fail':>5}")
    print(header)
    print("-" * len(header))
    for prow in report.playlists:
        print(f"{prow.title[:40]:40} {prow.total:>5} {prow.hardlinked:>5} {prow.copied:>5} "
              f"{prow.repaired:>4} {prow.skipped:>5} {prow.missing:>5} {prow.failed:>5}")
        for err in prow.errors[:10]:
            print(f"    ! {err}")
        if len(prow.errors) > 10:
            print(f"    ! ... {len(prow.errors) - 10} more")
    totals = report.totals()
    flags = []
    if report.dry_run:
        flags.append("dry-run")
    if report.metadata_only:
        flags.append("metadata-only")
    suffix = f" ({', '.join(flags)})" if flags else ""
    print("-" * len(header))
    print(f"target={target}{suffix}")
    print(f"playlists={totals['playlists']} videos={totals['videos']} "
          f"linked={totals['hardlinked']} copied={totals['copied']} "
          f"repaired={totals['repaired']} skipped={totals['skipped']} "
          f"missing={totals['missing']} failed={totals['failed']} "
          f"bytes={_human(totals['bytes'])}")


def main(argv: list[str] | None = None) -> int:
    """CLI entry point; returns the process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    env_file = args.env_file or str(Path(__file__).resolve().parents[1] / ".env")
    for key, value in load_dotenv(env_file).items():
        os.environ.setdefault(key, value)

    url = args.url or _env("TA_API_URL")
    token = args.token or _env("TA_API_TOKEN")
    host = args.host_header or _env("TA_API_HOST")
    target = args.target or _env("TA_EXPORT_TARGET")
    media_root = args.media_root or _env("TA_MEDIA_ROOT")
    required = [name for name, value in (
        ("--url/TA_API_URL", url),
        ("--token/TA_API_TOKEN", token),
        ("--target/TA_EXPORT_TARGET", target),
        ("--media-root/TA_MEDIA_ROOT", media_root),
    ) if not value]
    if required:
        parser.error("missing required options: " + ", ".join(required))

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(levelname)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    progress = (lambda msg: print(msg, flush=True)) if not args.quiet else None

    try:
        with TaClient(url, token, host_header=host, timeout=args.timeout) as client:
            report = export(
                client,
                media_root=media_root,
                target=target,
                mode=args.mode,
                metadata_only=args.metadata_only,
                dry_run=args.dry_run,
                with_archives=not args.no_archives,
                playlist_ids=args.playlist,
                progress=progress,
            )
    except TaError as exc:
        log.error("%s", exc)
        return 2
    except OSError as exc:
        log.error("%s", exc)
        return 2

    _print_report(report, target)
    return 1 if report.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
