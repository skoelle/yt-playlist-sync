# Copyright (c) 2026 Stefan Koelle (https://stefankoelle.de)
# Licensed under the MIT License. See LICENSE file in project root for details.
"""Shared pieces of the TubeArchivist and TubeSync exporters.

Both exporters write the same artefacts into ``target``: folders built with
``sanitize_folder_name`` that hold ``NN - <title> [<video id>].<ext>`` files
with sidecars, a ``manifest.json`` for ``app.ta_import`` and append-only
download archives. File placement (atomic over ``.part``, hardlink with copy
fallback, size based repair), the report rows and the small CLI helpers live
here so both exporters stay behaviourally identical.
"""
from __future__ import annotations

import errno
import logging
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

MANIFEST_NAME = "manifest.json"
THUMB_EXTS = (".jpg", ".jpeg", ".png", ".webp")
MODES = ("auto", "copy", "hardlink")


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


def check_mode(mode: str) -> None:
    """Reject unknown placement modes before anything is written."""
    if mode not in MODES:
        raise ValueError(f"unsupported mode: {mode}")


def as_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def as_float(value: object) -> float | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def safe_id(playlist_id: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in playlist_id) or "unknown"


def same_device(src: Path, dst_dir: Path) -> bool:
    """True when ``src`` and ``dst_dir`` share a filesystem, so hardlinking can work.

    Unknown (a stat that fails) counts as True: the real ``os.link`` decides then.
    """
    try:
        return src.stat().st_dev == dst_dir.stat().st_dev
    except OSError:
        return True


def place_file(src: Path, dst: Path, mode: str, dry_run: bool,
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
            linkable = same_device(src, dst.parent)
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


def write_file(path: Path, data: bytes, dry_run: bool) -> bool:
    """Create ``path`` only when it does not exist yet; True when it was written."""
    if path.exists():
        return False
    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.part")
        tmp.write_bytes(data)
        os.replace(tmp, path)
    return True


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


def human(size: float) -> str:
    for unit in ("B", "K", "M", "G", "T"):
        if size < 1024 or unit == "T":
            return f"{size:.1f}{unit}" if unit != "B" else f"{int(size)}B"
        size /= 1024
    return f"{size:.1f}T"


def print_report(report: ExportReport, target: str) -> None:
    """Print the shared per-playlist table plus the totals line."""
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
          f"bytes={human(totals['bytes'])}")
