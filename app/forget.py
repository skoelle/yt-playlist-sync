# Copyright (c) 2026 Stefan Koelle (https://stefankoelle.de)
# Licensed under the MIT License. See LICENSE file in project root for details.
"""Forget one or more playlists: database rows, download archives and folders.

A manual, offline maintenance command for a **stopped** application. Per playlist
it removes the ``playlists`` row together with its ``playlist_entries`` and
``jobs``, the download archives in ``config_dir/archives`` and
``data_dir/archives``, and moves the playlist folder out of the data dir into
``data_dir/.quarantine`` (``--keep-folder`` leaves it, ``--delete-folder``
together with ``--yes`` removes it). Deleting rows is the one documented
exception to the project rule "never delete" (SPEC section 15): it happens only
through this CLI, never on a runtime path, and only after a backup.

Use it before re-importing the same playlist from another source: an upsert
would keep the stale listing rows, the old download archive would point at files
that are gone, and a new export would write next to the old ones. Everything is
refused while a playlist is ``queued``/``running`` or still has an open job.

``python -m app.forget --playlist <id>`` only reports; pass ``--apply`` to act.
"""
from __future__ import annotations

import argparse
import logging
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import delete, func, select

from .backup import backup_archives, backup_database
from .config import Settings, ensure_dirs
from .db import init_engine, migrate, session_scope
from .models import Job, Playlist, PlaylistEntry

log = logging.getLogger(__name__)

QUARANTINE_NAME = ".quarantine"
FOLDER_MODES = ("quarantine", "keep", "delete")
OPEN_STATES = frozenset({"queued", "running"})
OPEN_JOB_STATUSES = frozenset({"queued", "running"})


class ForgetError(RuntimeError):
    """One playlist could not be forgotten (guard failed, folder in the way)."""


@dataclass
class ForgetTarget:
    """Read-only snapshot of one requested playlist (also used when no row exists)."""

    playlist_id: str
    exists: bool = False
    title: str = ""
    state: str = ""
    folder: str = ""
    entries: int = 0
    jobs: int = 0
    open_jobs: int = 0
    unsafe_folder: bool = False
    folders: list[Path] = field(default_factory=list)
    archives: list[Path] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return self.state in OPEN_STATES or self.open_jobs > 0

    @property
    def worth_doing(self) -> bool:
        return self.exists or bool(self.folders) or bool(self.archives)


@dataclass
class PlaylistReport:
    playlist_id: str
    title: str
    folder: str
    state: str = ""
    action: str = ""  # remove | blocked | missing | error
    folder_action: str = ""  # quarantine | delete | keep | none
    entries: int = 0
    jobs: int = 0
    folders: int = 0
    archives: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass
class ForgetReport:
    dry_run: bool = True
    playlists: list[PlaylistReport] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    backups: list[str] = field(default_factory=list)


def _archive_paths(settings: Settings, playlist_id: str) -> list[Path]:
    """Both archive locations of one playlist; only existing files are returned."""
    name = f"{playlist_id}.txt"
    found = [p for p in (settings.archives_dir / name, settings.data_dir / "archives" / name) if p.is_file()]
    return found


def _candidate_folders(settings: Settings, target: ForgetTarget) -> list[Path]:
    """Existing playlist folders of ``target``, all of them below ``data_dir``.

    The database folder name comes first, then every ``*/<playlist_id>`` folder
    below ``data_dir`` (that is how folders are found when the row is already
    gone). A folder name resolving outside ``data_dir`` is never returned.
    """
    base = settings.data_dir.resolve()
    names: list[Path] = []
    if target.folder:
        path = (base / target.folder).resolve()
        if path == base or not path.is_relative_to(base):
            target.unsafe_folder = True
        elif path.is_dir():
            names.append(path)
    try:
        children = sorted(settings.data_dir.iterdir())
    except OSError as exc:
        log.warning("cannot list %s: %s", settings.data_dir, exc)
        children = []
    suffix = f"[{target.playlist_id}]"
    for child in children:
        if not child.is_dir() or not child.name.endswith(suffix):
            continue
        path = child.resolve()
        if path == base or not path.is_relative_to(base):
            continue
        if path not in names:
            names.append(path)
    return names


def collect(settings: Settings, pids: list[str]) -> tuple[list[ForgetTarget], list[str]]:
    """Read-only snapshot for every requested playlist id (unknown ids are reported)."""
    targets: list[ForgetTarget] = []
    warnings: list[str] = []
    for pid in dict.fromkeys(pids):
        target = ForgetTarget(playlist_id=pid)
        with session_scope() as s:
            pl = s.scalar(select(Playlist).where(Playlist.playlist_id == pid))
            if pl is not None:
                target.exists = True
                target.title = pl.title
                target.state = pl.state
                target.folder = pl.folder_name or ""
                target.entries = int(
                    s.scalar(
                        select(func.count())
                        .select_from(PlaylistEntry)
                        .where(PlaylistEntry.playlist_id == pl.id)
                    )
                    or 0
                )
                target.jobs = int(
                    s.scalar(
                        select(func.count()).select_from(Job).where(Job.playlist_id == pl.id)
                    )
                    or 0
                )
                target.open_jobs = int(
                    s.scalar(
                        select(func.count())
                        .select_from(Job)
                        .where(
                            Job.playlist_id == pl.id, Job.status.in_(tuple(OPEN_JOB_STATUSES))
                        )
                    )
                    or 0
                )
        target.folders = _candidate_folders(settings, target)
        target.archives = _archive_paths(settings, pid)
        if not target.exists:
            warnings.append(f"playlist {pid} has no database row (folder/archives only)")
        targets.append(target)
    return targets, warnings


def _folder_action(target: ForgetTarget, mode: str, quarantine: Path) -> int:
    """Move or delete every folder of the target; returns the number acted on."""
    if mode == "keep":
        return len(target.folders)
    done = 0
    for path in target.folders:
        dest = quarantine / path.name
        if mode == "delete":
            shutil.rmtree(path)
            done += 1
            continue
        if dest.exists():
            raise ForgetError(f"quarantine target already exists: {dest}")
        quarantine.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(dest))
        done += 1
    return done


def _forget_one(
    settings: Settings,
    target: ForgetTarget,
    *,
    folder_mode: str,
    quarantine: Path,
    dry_run: bool,
    report: ForgetReport,
) -> None:
    """Forget one playlist: folder first, then archives, then the database rows."""
    prow = PlaylistReport(
        playlist_id=target.playlist_id,
        title=target.title or target.playlist_id,
        folder=target.folder or (target.folders[0].name if target.folders else ""),
        state=target.state,
    )
    report.playlists.append(prow)

    if target.unsafe_folder:
        prow.action = "error"
        msg = f"folder name {target.folder!r} points outside {settings.data_dir}"
        prow.errors.append(msg)
        report.errors.append(f"{target.playlist_id}: {msg}")
        return
    if target.blocked:
        prow.action = "blocked"
        why = "open job" if target.open_jobs else f"state {target.state}"
        msg = f"not forgotten while the playlist has an {why}; stop the application and retry"
        prow.errors.append(msg)
        report.errors.append(f"{target.playlist_id}: {msg}")
        return

    if dry_run:
        prow.action = "remove" if target.exists else "missing"
        prow.folder_action = folder_mode if target.folders else "none"
        prow.entries = target.entries
        prow.jobs = target.jobs
        prow.folders = len(target.folders)
        prow.archives = len(target.archives)
        if folder_mode == "keep" and target.folders:
            report.warnings.append(f"{target.playlist_id}: folder kept, expect duplicate files")
        return

    # 1) folders: move them away before anything else, so a failure here leaves
    #    the playlist completely untouched and can simply be retried.
    try:
        prow.folders = _folder_action(target, folder_mode, quarantine)
    except (OSError, ForgetError) as exc:
        prow.action = "error"
        msg = f"folder {folder_mode} failed: {exc}"
        prow.errors.append(msg)
        report.errors.append(f"{target.playlist_id}: {msg}")
        return
    prow.folder_action = folder_mode if target.folders else "none"
    if folder_mode == "keep" and target.folders:
        report.warnings.append(f"{target.playlist_id}: folder kept, expect duplicate files")

    # 2) archives: both locations, they are append-only and never rewritten.
    for path in target.archives:
        try:
            path.unlink()
            prow.archives += 1
        except OSError as exc:
            prow.action = "error"
            msg = f"archive {path}: {exc}"
            prow.errors.append(msg)
            report.errors.append(f"{target.playlist_id}: {msg}")
            return

    # 3) database rows, foreign keys first (SQLite runs with foreign_keys=ON).
    with session_scope() as s:
        pl = s.scalar(select(Playlist).where(Playlist.playlist_id == target.playlist_id))
        if pl is None:
            prow.action = "missing"
            return
        open_jobs = int(
            s.scalar(
                select(func.count())
                .select_from(Job)
                .where(Job.playlist_id == pl.id, Job.status.in_(tuple(OPEN_JOB_STATUSES)))
            )
            or 0
        )
        if pl.state in OPEN_STATES or open_jobs:
            prow.action = "blocked"
            why = "open job" if open_jobs else f"state {pl.state}"
            msg = f"an {why} appeared while forgetting; run again with the application stopped"
            prow.errors.append(msg)
            report.errors.append(f"{target.playlist_id}: {msg}")
            return
        prow.entries = int(
            s.execute(delete(PlaylistEntry).where(PlaylistEntry.playlist_id == pl.id)).rowcount
        )
        prow.jobs = int(s.execute(delete(Job).where(Job.playlist_id == pl.id)).rowcount)
        s.delete(pl)
        prow.action = "removed"


def apply_forget(
    settings: Settings,
    pids: list[str],
    *,
    folder_mode: str = "quarantine",
    quarantine: Path | None = None,
    dry_run: bool = True,
    make_backup: bool = True,
) -> ForgetReport:
    """Forget all requested playlists; every playlist runs in its own transaction."""
    if folder_mode not in FOLDER_MODES:
        raise ForgetError(f"unknown folder mode: {folder_mode}")
    report = ForgetReport(dry_run=dry_run)
    quarantine = quarantine or (settings.data_dir / QUARANTINE_NAME)
    targets, warnings = collect(settings, pids)
    report.warnings.extend(warnings)

    # One backup for the whole run, before the first change.
    if not dry_run and make_backup and any(t.worth_doing and not t.blocked for t in targets):
        db_backup = backup_database(settings)
        if db_backup:
            report.backups.append(db_backup)
        else:
            report.warnings.append("database backup was not created (see log)")
        archives_backup = backup_archives(settings)
        if archives_backup:
            report.backups.append(archives_backup)
        else:
            report.warnings.append("archives backup was not created (see log)")

    for target in targets:
        _forget_one(
            settings,
            target,
            folder_mode=folder_mode,
            quarantine=quarantine,
            dry_run=dry_run,
            report=report,
        )
    return report


def load_settings() -> Settings:
    """Settings from the environment; the channel plays no role for a forget run."""
    os.environ.setdefault("YOUTUBE_CHANNEL", "@beispielkanal")
    return Settings()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.forget",
        description="Remove playlist rows, download archives and folders (stopped app only).",
    )
    parser.add_argument(
        "--playlist",
        action="append",
        required=True,
        metavar="PLAYLIST_ID",
        help="playlist to forget (repeatable)",
    )
    parser.add_argument(
        "--quarantine",
        default="",
        metavar="DIR",
        help=f"target for moved folders (default: <data>/{QUARANTINE_NAME})",
    )
    folder = parser.add_mutually_exclusive_group()
    folder.add_argument(
        "--keep-folder",
        action="store_true",
        help="leave the folder in the data dir (duplicate files on the next import)",
    )
    folder.add_argument(
        "--delete-folder",
        action="store_true",
        help="delete the folder instead of quarantining it (requires --yes)",
    )
    parser.add_argument(
        "--no-backup", action="store_true", help="skip the database/archives backup before --apply"
    )
    parser.add_argument("--yes", action="store_true", help="confirm --delete-folder")
    parser.add_argument(
        "--apply", action="store_true", help="actually change things (default: report only)"
    )
    return parser


def _print_report(report: ForgetReport, quarantine: Path) -> None:
    print()
    header = (
        f"{'playlist':34} {'state':<7} {'ents':>5} {'jobs':>4} "
        f"{'arch':>4} {'folder':<11} {'action':<8}"
    )
    print(header)
    print("-" * len(header))
    for prow in report.playlists:
        folder_cell = prow.folder_action or "-"
        if prow.folders > 1:
            folder_cell = f"{prow.folder_action}({prow.folders})"
        print(
            f"{prow.title[:34]:34} {prow.state[:7]:<7} {prow.entries:>5} "
            f"{prow.jobs:>4} {prow.archives:>4} "
            f"{folder_cell:<11} {prow.action:<8}"
        )
        for err in prow.errors[:10]:
            print(f"    ! {err}")
    suffix = " (dry-run)" if report.dry_run else ""
    print("-" * len(header))
    print(f"quarantine={quarantine}{suffix}")
    removed = sum(1 for p in report.playlists if p.action in {"removed", "remove"})
    blocked = sum(1 for p in report.playlists if p.action == "blocked")
    print(
        f"playlists={len(report.playlists)} removed={removed} blocked={blocked} "
        f"entries={sum(p.entries for p in report.playlists)} "
        f"jobs={sum(p.jobs for p in report.playlists)} "
        f"archives={sum(p.archives for p in report.playlists)} "
        f"folders={sum(p.folders for p in report.playlists)}"
    )
    for path in report.backups:
        print(f"backup={path}")
    for msg in report.warnings:
        print(f"    ! {msg}")
    for msg in report.errors:
        print(f"    x {msg}")


def main(argv: list[str] | None = None) -> int:
    """CLI entry point; returns the process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    if args.delete_folder and not args.yes:
        parser.error("--delete-folder requires --yes")

    try:
        settings = load_settings()
        ensure_dirs(settings)
        init_engine(settings.db_url)
        migrate(settings.db_url)
    except Exception as exc:  # noqa: BLE001
        log.error("could not open the application database: %s", exc)
        return 2

    folder_mode = "keep" if args.keep_folder else "delete" if args.delete_folder else "quarantine"
    quarantine = Path(args.quarantine) if args.quarantine else settings.data_dir / QUARANTINE_NAME
    try:
        report = apply_forget(
            settings,
            args.playlist,
            folder_mode=folder_mode,
            quarantine=quarantine,
            dry_run=not args.apply,
            make_backup=not args.no_backup,
        )
    except Exception as exc:  # noqa: BLE001
        log.error("forget failed: %s", exc)
        return 2

    _print_report(report, quarantine)
    return 1 if report.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
