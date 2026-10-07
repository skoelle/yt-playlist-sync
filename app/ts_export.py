# Copyright (c) 2026 Stefan Koelle (https://stefankoelle.de)
# Licensed under the MIT License. See LICENSE file in project root for details.
"""TubeSync export into the yt-playlist-sync folder layout (standalone, reads SQLite).

TubeSync has no JSON API, so the exporter opens its Django database read-only
(``sync_source``/``sync_media``/``sync_media_metadata``) and pairs the rows with
the files below its downloads root. The result matches what yt-dlp and the
TubeArchivist exporter produce: ``<folder>/NN - <title> [<video id>].<ext>``
with ``.info.json`` and thumbnail sidecars, a download archive and a
``manifest.json`` for ``python -m app.ta_import``. Never deletes, never
overwrites, idempotent on rerun.

``description`` stays in the TubeSync database: the gallery never reads it, so
a few KB per video are not copied into every export tree.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .export_common import (
    MANIFEST_NAME,
    THUMB_EXTS,
    ExportReport,
    PlaylistReport,
    as_float,
    as_int,
    check_mode,
    load_dotenv,
    place_file,
    print_report,
    same_device,
    write_file,
)
from .paths import sanitize_filename, sanitize_folder_name
from .ytdlp import append_archive

log = logging.getLogger(__name__)

SQL_SOURCES = "SELECT key, name, directory, source_type FROM sync_source"
SQL_MEDIA = """
    SELECT s.key AS source_key, m.uuid, m.key AS video_id, m.title, m.duration,
           m.published, m.created, m.media_file, m.thumb
      FROM sync_media m
      JOIN sync_source s ON s.uuid = m.source_id
"""
SQL_METADATA = "SELECT media_id, key, value FROM sync_media_metadata"
# Only what app.ytdlp.read_video_entries and the manifest consume.
_INFO_KEYS = (
    "id", "title", "webpage_url", "uploader", "channel", "duration", "upload_date",
    "view_count", "like_count", "width", "height", "vcodec", "acodec", "vbr", "abr",
    "playlist_index",
)


class TsError(RuntimeError):
    """Fatal TubeSync access problem (missing/unreadable database, bad shape)."""


@dataclass
class TsSource:
    playlist_id: str
    title: str
    directory: str
    source_type: str


@dataclass
class TsMedia:
    video_id: str
    title: str
    source_key: str
    uuid: str = ""
    created: str = ""
    published: str | None = None
    duration_s: int | None = None
    media_file: str | None = None
    thumb: str | None = None
    playlist_index: int | None = None
    info: dict[str, Any] = field(default_factory=dict)


def open_db(path: Path | str) -> sqlite3.Connection:
    """Open the TubeSync database read-only; raises :class:`TsError` when unusable."""
    db = Path(path)
    if not db.is_file():
        raise TsError(f"database not found: {db}")
    try:
        conn = sqlite3.connect(f"file:{db.resolve()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error as exc:
        raise TsError(f"{db}: {exc}") from exc
    return conn


def _rows(conn: sqlite3.Connection, sql: str) -> list[sqlite3.Row]:
    try:
        return list(conn.execute(sql))
    except sqlite3.Error as exc:
        raise TsError(f"query failed: {exc}") from exc


def read_sources(conn: sqlite3.Connection) -> list[TsSource]:
    """All TubeSync sources keyed by their YouTube id (``PL...`` for playlists)."""
    sources = [
        TsSource(
            playlist_id=str(row["key"]),
            title=str(row["name"] or row["key"]),
            directory=str(row["directory"] or ""),
            source_type=str(row["source_type"] or ""),
        )
        for row in _rows(conn, SQL_SOURCES)
    ]
    sources.sort(key=lambda s: (s.title, s.playlist_id))
    return sources


def read_media(conn: sqlite3.Connection) -> list[TsMedia]:
    """One row per (source, video), including the file paths TubeSync stores."""
    return [
        TsMedia(
            uuid=str(row["uuid"]),
            video_id=str(row["video_id"]),
            title=str(row["title"] or ""),
            source_key=str(row["source_key"]),
            created=str(row["created"] or ""),
            published=str(row["published"]) if row["published"] else None,
            duration_s=as_int(row["duration"]),
            media_file=str(row["media_file"]) if row["media_file"] else None,
            thumb=str(row["thumb"]) if row["thumb"] else None,
        )
        for row in _rows(conn, SQL_MEDIA)
    ]


def read_metadata(conn: sqlite3.Connection, uuid_to_video: dict[str, str]) -> dict[str, str]:
    """Raw yt-dlp JSON per video id.

    The row linked through ``media_id`` wins; otherwise the newest row that
    only matches by ``key`` (older TubeSync versions left them unlinked).
    """
    linked: dict[str, str] = {}
    by_key: dict[str, str] = {}
    for row in _rows(conn, SQL_METADATA + " ORDER BY rowid DESC"):
        value = row["value"]
        if not value:
            continue
        if row["media_id"]:
            video_id = uuid_to_video.get(str(row["media_id"]))
            if video_id:
                linked[video_id] = str(value)
        else:
            by_key.setdefault(str(row["key"]), str(value))
    by_key.update(linked)
    return by_key


def prune_info(raw: str) -> dict[str, Any]:
    """Keep the info.json fields the gallery reads; drop the description and blobs."""
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {key: parsed[key] for key in _INFO_KEYS if parsed.get(key) is not None}


def _position_key(item: TsMedia) -> tuple[int, int, str, str, str]:
    """TubeSync has no playlist order of its own: use ``playlist_index`` when the
    metadata carries one, otherwise the order TubeSync crawled the videos in."""
    if item.playlist_index is not None:
        return (0, item.playlist_index, item.created, item.published or "", item.video_id)
    return (1, 0, item.created, item.published or "", item.video_id)


def read_library(
    conn: sqlite3.Connection,
) -> tuple[list[TsSource], dict[str, list[TsMedia]]]:
    """Sources plus their media grouped by source key and ordered by position."""
    sources = read_sources(conn)
    media = read_media(conn)
    metadata = read_metadata(conn, {m.uuid: m.video_id for m in media})
    library: dict[str, list[TsMedia]] = {}
    for item in media:
        raw = metadata.get(item.video_id)
        if raw:
            item.info = prune_info(raw)
            index = as_int(item.info.get("playlist_index"))
            item.playlist_index = index if index is not None and index > 0 else None
        library.setdefault(item.source_key, []).append(item)
    for items in library.values():
        items.sort(key=_position_key)
    return sources, library


def _text(value: object) -> str | None:
    return str(value) if value is not None and str(value) != "" else None


def _date_compact(value: str | None) -> str | None:
    """TubeSync stores ``YYYY-MM-DD HH:MM:SS``; yt-dlp wants ``YYYYMMDD``."""
    if not value:
        return None
    head = value[:10]
    return head.replace("-", "") if len(head) == 10 and "-" in head else None


def build_info_json(
    media: TsMedia, source: TsSource, index: int, ext: str
) -> dict[str, Any]:
    """yt-dlp shaped info.json so the gallery can read titles, counts and codecs."""
    info = media.info
    title = media.title or _text(info.get("title")) or media.video_id
    uploader = _text(info.get("uploader"))
    return {
        "id": media.video_id,
        "title": title,
        "ext": ext.lstrip("."),
        "webpage_url": f"https://www.youtube.com/watch?v={media.video_id}",
        "uploader": uploader,
        "channel": _text(info.get("channel")) or uploader,
        "duration": media.duration_s if media.duration_s is not None else as_int(info.get("duration")),
        "upload_date": _text(info.get("upload_date")) or _date_compact(media.published),
        "view_count": as_int(info.get("view_count")),
        "like_count": as_int(info.get("like_count")),
        "description": "",
        "width": as_int(info.get("width")),
        "height": as_int(info.get("height")),
        "vcodec": _text(info.get("vcodec")),
        "acodec": _text(info.get("acodec")),
        "vbr": as_float(info.get("vbr")),
        "abr": as_float(info.get("abr")),
        "playlist": source.playlist_id,
        "playlist_title": source.title,
        "playlist_index": index,
    }


def build_manifest(
    source: TsSource, rows: list[dict[str, Any]], folder: str, metadata_only: bool
) -> dict[str, Any]:
    """Everything a later DB import needs, independent of the surrounding files."""
    return {
        "source": "tubesync",
        "exported_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "metadata_only": metadata_only,
        "playlist_id": source.playlist_id,
        "title": source.title,
        "channel": "",
        "folder_name": folder,
        "remote_item_count": len(rows),
        "entries": rows,
    }


def _resolve_media(media: TsMedia, downloads_root: Path) -> Path | None:
    if not media.media_file:
        return None
    candidate = downloads_root / media.media_file
    try:
        return candidate if candidate.is_file() else None
    except OSError:
        return None


def _thumb_src(media: TsMedia, thumbs_root: Path) -> Path | None:
    if not media.thumb:
        return None
    candidate = thumbs_root / media.thumb
    try:
        return candidate if candidate.is_file() else None
    except OSError:
        return None


def _export_entry(
    media: TsMedia,
    source: TsSource,
    dest: Path,
    downloads_root: Path,
    thumbs_root: Path,
    index: int,
    mode: str,
    metadata_only: bool,
    dry_run: bool,
    report: PlaylistReport,
    linkable: bool | None = None,
) -> dict[str, Any]:
    """Write video and sidecars for one entry; returns the manifest row."""
    src = _resolve_media(media, downloads_root)
    ext = Path(media.media_file).suffix if media.media_file else ""
    ext = ext or ".mkv"
    title = media.title or media.video_id
    stem = f"{index:02d} - {sanitize_filename(title)} [{media.video_id}]"
    file_name = f"{stem}{ext}"

    if src is None:
        report.missing += 1
        report.errors.append(f"{media.video_id}: media file not found")
    elif metadata_only:
        report.skipped += 1
    else:
        try:
            size, action = place_file(src, dest / file_name, mode, dry_run, linkable)
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
            report.errors.append(f"{media.video_id}: {exc}")
            log.warning("%s: copy failed: %s", media.video_id, exc)

    info = build_info_json(media, source, index, ext)
    write_file(
        dest / f"{stem}.info.json",
        json.dumps(info, ensure_ascii=False, indent=1).encode("utf-8"),
        dry_run,
    )

    thumb = _thumb_src(media, thumbs_root)
    if thumb is not None and thumb.suffix.lower() in THUMB_EXTS:
        try:
            size, _action = place_file(
                thumb, dest / f"{stem}{thumb.suffix.lower()}", "copy", dry_run
            )
            report.bytes_copied += size
        except OSError as exc:
            report.errors.append(f"{media.video_id}: thumbnail: {exc}")

    return {
        "position": index,
        "video_id": media.video_id,
        "title": title,
        "uploader": _text(info.get("uploader")) or "",
        "duration_s": info["duration"],
        "published": media.published,
        "downloaded": src is not None,
        "file": file_name if src is not None else None,
    }


def export(
    *,
    db: Path | str,
    media_root: Path | str,
    target: Path | str,
    thumbs_root: Path | str | None = None,
    mode: str = "auto",
    metadata_only: bool = False,
    dry_run: bool = False,
    with_archives: bool = True,
    playlist_ids: list[str] | None = None,
    progress: Callable[[str], None] | None = None,
) -> ExportReport:
    """Export playlists from TubeSync into ``target`` in our folder layout.

    ``media_root`` is the TubeSync downloads directory, ``thumbs_root`` its
    media directory (default: next to the database). SQLite is only ever
    opened read-only.
    """
    check_mode(mode)
    db = Path(db)
    media_root = Path(media_root)
    target = Path(target)
    thumbs = Path(thumbs_root) if thumbs_root else db.parent / "media"
    conn = open_db(db)
    try:
        sources, library = read_library(conn)
    finally:
        conn.close()

    wanted = set(playlist_ids) if playlist_ids else None
    exported: list[TsSource] = []
    for source in sources:
        if source.source_type != "p":
            log.warning(
                "skipping source %s (%s): not a playlist (type %s)",
                source.title, source.playlist_id, source.source_type,
            )
            continue
        if wanted is None or source.playlist_id in wanted:
            exported.append(source)
    if wanted is not None:
        for pid in sorted(wanted - {s.playlist_id for s in exported}):
            log.warning("playlist not found in TubeSync: %s", pid)

    report = ExportReport(dry_run=dry_run, metadata_only=metadata_only)
    device_warned = False
    for source in exported:
        media = library.get(source.playlist_id, [])
        prow = PlaylistReport(playlist_id=source.playlist_id, title=source.title)
        prow.total = len(media)
        prow.folder = sanitize_folder_name(source.title, source.playlist_id)
        dest = target / prow.folder
        if not dry_run:
            dest.mkdir(parents=True, exist_ok=True)
        linkable: bool | None = None
        if not dry_run and mode in {"auto", "hardlink"}:
            linkable = same_device(media_root, dest)
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
        for index, item in enumerate(media, 1):
            row = _export_entry(
                item, source, dest, media_root, thumbs, index, mode,
                metadata_only, dry_run, prow, linkable,
            )
            rows.append(row)
            if row["file"]:
                archived.append(item.video_id)

        write_file(
            dest / MANIFEST_NAME,
            json.dumps(build_manifest(source, rows, prow.folder, metadata_only),
                       ensure_ascii=False, indent=1).encode("utf-8"),
            dry_run,
        )
        if with_archives:
            append_archive(target / "archives" / f"{source.playlist_id}.txt", archived, dry_run)
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
        prog="python -m app.ts_export",
        description="Export TubeSync playlists into the yt-playlist-sync folder layout.",
    )
    parser.add_argument("--env-file", default="", help=".env file (default: <repo>/.env)")
    parser.add_argument("--db", default="", help="TubeSync db.sqlite3, env TS_DB")
    parser.add_argument("--media-root", default="", help="TubeSync downloads dir, env TS_MEDIA_ROOT")
    parser.add_argument("--thumbs-root", default="",
                        help="TubeSync media dir, env TS_THUMBS_ROOT (default: next to the db)")
    parser.add_argument("--target", default="", help="export root, env TS_EXPORT_TARGET")
    parser.add_argument("--mode", choices=("auto", "copy", "hardlink"), default="auto",
                        help="auto = hardlink with copy fallback (default)")
    parser.add_argument("--metadata-only", action="store_true",
                        help="write metadata, archives and manifests but no video files")
    parser.add_argument("--dry-run", action="store_true", help="write nothing, only report")
    parser.add_argument("--playlist", action="append", metavar="PLAYLIST_ID",
                        help="export only this playlist (repeatable)")
    parser.add_argument("--no-archives", action="store_true", help="skip archives/<pid>.txt files")
    parser.add_argument("-q", "--quiet", action="store_true", help="only print the final table")
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point; returns the process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    env_file = args.env_file or str(Path(__file__).resolve().parents[1] / ".env")
    for key, value in load_dotenv(env_file).items():
        os.environ.setdefault(key, value)

    db = args.db or _env("TS_DB")
    media_root = args.media_root or _env("TS_MEDIA_ROOT")
    thumbs_root = args.thumbs_root or _env("TS_THUMBS_ROOT")
    target = args.target or _env("TS_EXPORT_TARGET")
    required = [name for name, value in (
        ("--db/TS_DB", db),
        ("--target/TS_EXPORT_TARGET", target),
        ("--media-root/TS_MEDIA_ROOT", media_root),
    ) if not value]
    if required:
        parser.error("missing required options: " + ", ".join(required))

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(levelname)s: %(message)s",
    )
    progress = (lambda msg: print(msg, flush=True)) if not args.quiet else None

    try:
        report = export(
            db=db,
            media_root=media_root,
            target=target,
            thumbs_root=thumbs_root or None,
            mode=args.mode,
            metadata_only=args.metadata_only,
            dry_run=args.dry_run,
            with_archives=not args.no_archives,
            playlist_ids=args.playlist,
            progress=progress,
        )
    except (TsError, OSError, ValueError) as exc:
        log.error("%s", exc)
        return 2

    print_report(report, target)
    return 1 if report.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
