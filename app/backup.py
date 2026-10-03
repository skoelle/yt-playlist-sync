# Copyright (c) 2026 Stefan Koelle (https://stefankoelle.de)
# Licensed under the MIT License. See LICENSE file in project root for details.
"""Nightly SQLite database backup with rotation.

Uses the sqlite3 backup API instead of copying files: the database runs in
WAL mode, so a plain file copy can miss WAL content and produce a torn read.
The backup API writes a consistent snapshot that is a standalone database
(no sidecar -wal file), safe while the app is writing.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime
from pathlib import Path

from .config import Settings

log = logging.getLogger(__name__)

BACKUP_NAME_GLOB = "app-????????-??????.db"


def sqlite_path(url: str) -> Path | None:
    """Filesystem path of a sqlite database URL, or None for other backends."""
    if not url.startswith("sqlite") or "://" not in url:
        return None
    raw = url.split("://", 1)[1]
    if raw.startswith("//"):
        # sqlite:////abs/path (SQLAlchemy's four-slash absolute form) keeps
        # two slashes after splitting; normalise to a single leading slash
        return Path("/") / raw.lstrip("/")
    if raw.startswith("/"):
        # sqlite:///relative.db keeps one slash after splitting
        return Path(raw[1:])
    return Path(raw)


def _verify(dest: Path) -> bool:
    try:
        con = sqlite3.connect(dest, timeout=10)
        try:
            ok = con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        finally:
            con.close()
    except sqlite3.Error:
        ok = False
    if not ok:
        log.error("db backup integrity check failed: %s", dest)
    return ok


def _prune(settings: Settings) -> None:
    if settings.backup_keep <= 0:
        return
    try:
        files = sorted(settings.backup_dir.glob(BACKUP_NAME_GLOB))
        for old in files[: len(files) - settings.backup_keep]:
            try:
                old.unlink()
            except OSError:
                pass
    except OSError:
        pass


def backup_database(settings: Settings) -> str | None:
    """Write one backup of the SQLite database into backup_dir.

    Returns the created file path, or None if the backup was skipped or
    failed. Never raises: all errors are logged (availability over strictness).
    """
    src_path = sqlite_path(settings.db_url)
    if src_path is None:
        log.warning("db backup skipped: DATABASE_URL is not SQLite")
        return None
    if not src_path.is_file():
        log.warning("db backup skipped: source database not found: %s", src_path)
        return None
    stamp = datetime.now(settings.tzinfo).strftime("%Y%m%d-%H%M%S")
    dest_path = settings.backup_dir / f"app-{stamp}.db"
    try:
        settings.backup_dir.mkdir(parents=True, exist_ok=True)
        src = sqlite3.connect(src_path, timeout=10)
        try:
            dst = sqlite3.connect(dest_path, timeout=10)
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
    except (OSError, sqlite3.Error) as exc:
        log.warning("db backup failed: %s", exc)
        try:
            if dest_path.exists():
                dest_path.unlink()
        except OSError:
            pass
        return None
    _verify(dest_path)
    _prune(settings)
    return str(dest_path)
