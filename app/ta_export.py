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
import json
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from .export_common import (
    MANIFEST_NAME,
    THUMB_EXTS as _THUMB_EXTS,
    ExportReport,
    PlaylistReport,
    as_float as _float,
    as_int as _int,
    check_mode,
    load_dotenv,
    place_file as _place_file,
    print_report as _print_report,
    safe_id as _safe_id,
    same_device as _same_device,
    write_file as _write_file,
)
from .paths import sanitize_filename, sanitize_folder_name
from .ytdlp import append_archive

log = logging.getLogger(__name__)

API_PLAYLISTS = "/api/playlist/"
API_VIDEOS = "/api/video/"
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
    check_mode(mode)
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


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


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
