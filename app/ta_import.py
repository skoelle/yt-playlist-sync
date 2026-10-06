# Copyright (c) 2026 Stefan Koelle (https://stefankoelle.de)
# Licensed under the MIT License. See LICENSE file in project root for details.
"""Import an exported playlist tree into the application database (standalone, offline).

Reads ``<source>/<folder>/manifest.json`` plus the playlist folders that live under
``data_dir`` and upserts playlists and playlist entries: nothing is ever deleted,
``reason``/``unavailable`` of existing entry rows are never overwritten, and the
download archives in ``config_dir/archives`` receive an append-only line for every
local video file. Every imported playlist becomes a ``oneshot`` playlist so no
nightly sync picks it up by accident. Local files decide everything: a playlist is
``done`` only when every manifest entry has a file on disk.

``python -m app.ta_import --dry-run`` reports what would change without touching
playlists, entries or archives. Move the folders into place first, then import.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select

from .config import Settings, ensure_dirs
from .db import init_engine, migrate, session_scope, utcnow
from .models import Playlist, PlaylistEntry
from .ytdlp import append_archive, folder_size, read_video_entries

log = logging.getLogger(__name__)

MANIFEST_NAME = "manifest.json"


class TaImportError(RuntimeError):
    """One manifest could not be used (bad JSON, missing fields)."""


@dataclass
class Manifest:
    """The part of a ``manifest.json`` the importer needs."""

    playlist_id: str
    title: str
    folder_name: str
    entries: list[dict[str, Any]]


@dataclass
class PlaylistReport:
    playlist_id: str
    title: str
    folder: str
    entries: int
    files: int
    state: str
    action: str = "updated"  # new | updated | skipped
    archive_merged: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass
class ImportReport:
    dry_run: bool = False
    playlists: list[PlaylistReport] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def load_manifest(path: Path) -> Manifest:
    """Parse one ``manifest.json``; raises :class:`TaImportError` when unusable."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TaImportError(f"{path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise TaImportError(f"{path}: not a JSON object")
    playlist_id = str(raw.get("playlist_id") or "").strip()
    entries = raw.get("entries")
    if not playlist_id or not isinstance(entries, list):
        raise TaImportError(f"{path}: missing playlist_id or entries")
    return Manifest(
        playlist_id=playlist_id,
        title=str(raw.get("title") or playlist_id)[:500],
        folder_name=str(raw.get("folder_name") or path.parent.name)[:300],
        entries=[e for e in entries if isinstance(e, dict) and e.get("video_id")],
    )


def scan_manifests(source: Path, errors: list[str] | None = None) -> list[Manifest]:
    """Load every ``<source>/<folder>/manifest.json``, sorted by folder name."""
    manifests: list[Manifest] = []
    seen: set[str] = set()
    for path in sorted(source.glob(f"*/{MANIFEST_NAME}")):
        try:
            manifest = load_manifest(path)
        except TaImportError as exc:
            log.error("%s", exc)
            if errors is not None:
                errors.append(str(exc))
            continue
        if manifest.playlist_id in seen:
            log.warning(
                "duplicate playlist %s in %s, keeping the first", manifest.playlist_id, path.parent
            )
            continue
        seen.add(manifest.playlist_id)
        manifests.append(manifest)
    return manifests


def select_manifests(
    manifests: list[Manifest], only: list[str] | None, skip: list[str] | None
) -> tuple[list[Manifest], list[str]]:
    """Apply ``--playlist`` (wins over ``--skip``) and ``--skip``.

    Returns ``(selected, unknown playlist ids)``.
    """
    wanted = list(dict.fromkeys(only or []))
    if wanted:
        by_id = {m.playlist_id: m for m in manifests}
        return [by_id[pid] for pid in wanted if pid in by_id], [pid for pid in wanted if pid not in by_id]
    skip_ids = set(skip or [])
    return [m for m in manifests if m.playlist_id not in skip_ids], []


def resolve_folder(
    data_dir: Path, current: str | None, manifest: Manifest
) -> tuple[str, Path, str | None]:
    """Pick the playlist folder below ``data_dir``.

    An existing database folder keeps its name when it is on disk, otherwise the
    manifest folder is used. Returns ``(folder_name, path, warning)``.
    """
    for name in dict.fromkeys(n for n in (current, manifest.folder_name) if n):
        path = data_dir / name
        if path.is_dir():
            warning: str | None = None
            if current and name == current and current != manifest.folder_name:
                warning = f"manifest folder {manifest.folder_name} differs, keeping {current}"
            elif current and name == manifest.folder_name != current:
                warning = f"using folder {name}, the database had {current}"
            return name, path, warning
    name = current or manifest.folder_name
    return name, data_dir / name, f"folder {name} not found in {data_dir}"


def _duration(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _import_one(
    manifest: Manifest,
    *,
    data_dir: Path,
    config_dir: Path,
    merge_archives: bool,
    dry_run: bool,
    report: ImportReport,
    prow: PlaylistReport,
) -> None:
    """Upsert one playlist (and its entries and archive) inside its own session."""
    now = utcnow()
    with session_scope() as s:
        pl = s.scalar(select(Playlist).where(Playlist.playlist_id == manifest.playlist_id))
        if pl is not None and pl.state in {"queued", "running"}:
            prow.action = "skipped"
            report.warnings.append(f"{manifest.folder_name}: has an open job, skipped")
            return

        current = pl.folder_name if pl is not None else None
        folder_name, folder, warning = resolve_folder(data_dir, current, manifest)
        if warning:
            report.warnings.append(f"{manifest.folder_name}: {warning}")
        folder_ok = folder.is_dir()
        gallery = read_video_entries(folder, manifest.playlist_id)
        files = int(gallery["video_count"])

        if folder_ok:
            state = "done" if manifest.entries and files >= len(manifest.entries) else "idle"
        elif pl is not None:
            state = pl.state  # nothing on disk: leave the state as it is
        else:
            state = "idle"
        prow.folder = folder_name
        prow.files = files
        prow.state = state

        merged = 0
        if merge_archives and folder_ok:
            ids = [v["video_id"] for v in gallery["videos"] if v["video_id"]]
            merged = append_archive(config_dir / "archives" / f"{manifest.playlist_id}.txt", ids, dry_run)
        prow.archive_merged = merged
        if dry_run:
            prow.action = "new" if pl is None else "updated"
            return

        if pl is None:
            pl = Playlist(
                playlist_id=manifest.playlist_id,
                title=manifest.title,
                type="oneshot",
                folder_name=folder_name,
                state=state,
                remote_status="active",
                ignored=False,
                remote_item_count=len(manifest.entries),
                downloaded_count=files if folder_ok else 0,
                size_bytes=folder_size(folder) if folder_ok else 0,
                first_seen_at=now,
                last_seen_at=now,
                first_downloaded_at=now if files else None,
                completed_at=now if state == "done" else None,
            )
            s.add(pl)
            s.flush()
            prow.action = "new"
        else:
            pl.title = manifest.title
            pl.type = "oneshot"
            pl.folder_name = folder_name
            pl.state = state
            pl.last_seen_at = now
            pl.remote_item_count = len(manifest.entries)
            if folder_ok:
                pl.downloaded_count = files
                pl.size_bytes = folder_size(folder)
                if files and pl.first_downloaded_at is None:
                    pl.first_downloaded_at = now
                if state == "done" and pl.completed_at is None:
                    pl.completed_at = now
            prow.action = "updated"

        for entry in manifest.entries:
            video_id = str(entry["video_id"])
            row = s.scalar(
                select(PlaylistEntry).where(
                    PlaylistEntry.playlist_id == pl.id, PlaylistEntry.video_id == video_id
                )
            )
            if row is None:
                s.add(
                    PlaylistEntry(
                        playlist_id=pl.id,
                        video_id=video_id,
                        position=int(entry.get("position") or 0),
                        title=str(entry.get("title") or video_id)[:500],
                        duration_s=_duration(entry.get("duration_s")),
                        unavailable=False,
                        remote_present=True,
                        last_seen_at=now,
                    )
                )
                continue
            if entry.get("position"):
                row.position = int(entry["position"])
            if entry.get("title"):
                row.title = str(entry["title"])[:500]
            duration = _duration(entry.get("duration_s"))
            if duration is not None:
                row.duration_s = duration
            # reason/unavailable record what earlier sync runs learned from YouTube:
            # an import never overwrites them, and no row is ever removed.
            row.remote_present = True
            row.last_seen_at = now


def apply_import(
    manifests: list[Manifest],
    *,
    data_dir: Path,
    config_dir: Path,
    merge_archives: bool = True,
    dry_run: bool = False,
) -> ImportReport:
    """Import all given manifests; each playlist runs in its own transaction."""
    report = ImportReport(dry_run=dry_run)
    for manifest in manifests:
        prow = PlaylistReport(
            playlist_id=manifest.playlist_id,
            title=manifest.title,
            folder=manifest.folder_name,
            entries=len(manifest.entries),
            files=0,
            state="idle",
        )
        try:
            _import_one(
                manifest,
                data_dir=data_dir,
                config_dir=config_dir,
                merge_archives=merge_archives,
                dry_run=dry_run,
                report=report,
                prow=prow,
            )
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
            if "locked" in msg.lower():
                msg += " (is the app running? stop it while importing)"
            log.error("import of %s failed: %s", manifest.playlist_id, msg)
            prow.errors.append(msg[:300])
            report.errors.append(f"{manifest.playlist_id}: {msg}")
        report.playlists.append(prow)
    return report


def load_settings() -> Settings:
    """Settings from the environment; the channel plays no role for an import."""
    os.environ.setdefault("YOUTUBE_CHANNEL", "@beispielkanal")
    return Settings()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.ta_import",
        description="Import an exported playlist tree (manifest.json folders) into the database.",
    )
    parser.add_argument("--source", default="", help="export root to scan (default: data dir)")
    parser.add_argument("--playlist", action="append", metavar="PLAYLIST_ID",
                        help="import only this playlist (repeatable, wins over --skip)")
    parser.add_argument("--skip", action="append", metavar="PLAYLIST_ID",
                        help="leave out this playlist (repeatable)")
    parser.add_argument("--no-archives", action="store_true",
                        help="do not append local files to config/archives/<pid>.txt")
    parser.add_argument("--dry-run", action="store_true",
                        help="report only, no playlist/entry/archive changes")
    return parser


def _print_report(report: ImportReport, source: Path) -> None:
    print()
    header = f"{'folder':34} {'ents':>5} {'files':>5} {'state':<6} {'action':<8} {'arch':>4}"
    print(header)
    print("-" * len(header))
    for prow in report.playlists:
        print(
            f"{prow.folder[:34]:34} {prow.entries:>5} {prow.files:>5} {prow.state:<6} "
            f"{prow.action:<8} {prow.archive_merged:>4}"
        )
        for err in prow.errors[:10]:
            print(f"    ! {err}")
    suffix = " (dry-run)" if report.dry_run else ""
    print("-" * len(header))
    print(f"source={source}{suffix}")
    print(
        f"playlists={len(report.playlists)} entries={sum(p.entries for p in report.playlists)} "
        f"files={sum(p.files for p in report.playlists)} "
        f"archive+={sum(p.archive_merged for p in report.playlists)}"
    )
    for msg in report.warnings:
        print(f"    ! {msg}")
    for msg in report.errors:
        print(f"    x {msg}")


def main(argv: list[str] | None = None) -> int:
    """CLI entry point; returns the process exit code."""
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    try:
        settings = load_settings()
        ensure_dirs(settings)
        init_engine(settings.db_url)
        migrate(settings.db_url)
    except Exception as exc:  # noqa: BLE001
        log.error("could not open the application database: %s", exc)
        return 2

    source = Path(args.source) if args.source else settings.data_dir
    if not source.is_dir():
        log.error("source folder does not exist: %s", source)
        return 2

    scan_errors: list[str] = []
    manifests = scan_manifests(source, scan_errors)
    if not manifests:
        log.error("no %s found below %s", MANIFEST_NAME, source)
        return 2
    selected, unknown = select_manifests(manifests, args.playlist, args.skip)
    if not selected:
        log.error("nothing to import (unknown playlists: %s)", ", ".join(unknown))
        return 2

    try:
        report = apply_import(
            selected,
            data_dir=settings.data_dir,
            config_dir=settings.config_dir,
            merge_archives=not args.no_archives,
            dry_run=args.dry_run,
        )
    except Exception as exc:  # noqa: BLE001
        log.error("import failed: %s", exc)
        return 2
    report.errors.extend(scan_errors)
    report.warnings.extend(f"no manifest for playlist {pid}" for pid in unknown)

    _print_report(report, source)
    return 1 if report.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
