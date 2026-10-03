# Copyright (c) 2026 Stefan Koelle (https://stefankoelle.de)
# Licensed under the MIT License. See LICENSE file in project root for details.
import asyncio
import sqlite3
import tarfile
from pathlib import Path

import pytest

pytest.importorskip("pydantic_settings")
pytest.importorskip("apscheduler")

from pydantic import ValidationError  # noqa: E402

from app.backup import (  # noqa: E402
    ARCHIVES_NAME_GLOB,
    BACKUP_NAME_GLOB,
    backup_archives,
    backup_database,
    sqlite_path,
)
from app.config import Settings  # noqa: E402


def make_settings(tmp_path, **kw):
    config = tmp_path / "config"
    config.mkdir(exist_ok=True)
    return Settings(
        youtube_channel="@x", config_dir=config, backup_dir=tmp_path / "backup", **kw
    )


def make_db(settings, rows=3) -> None:
    con = sqlite3.connect(settings.config_dir / "app.db")
    con.execute("CREATE TABLE pl (id INTEGER PRIMARY KEY, name TEXT)")
    for i in range(rows):
        con.execute("INSERT INTO pl (name) VALUES (?)", (f"p{i}",))
    con.commit()
    con.close()


def make_archives(settings, count=2) -> None:
    archives = settings.archives_dir
    archives.mkdir(parents=True, exist_ok=True)
    for i in range(count):
        (archives / f"PL{i:04d}.txt").write_text(f"youtube vid{i:08d}\n", encoding="utf-8")


def count(db_file) -> int:
    con = sqlite3.connect(db_file)
    try:
        return con.execute("SELECT count(*) FROM pl").fetchone()[0]
    finally:
        con.close()


def test_sqlite_path_parsing():
    assert str(sqlite_path("sqlite:////data/app.db")) == "/data/app.db"
    assert str(sqlite_path("sqlite:///app.db")) == "app.db"
    assert str(sqlite_path("sqlite+pysqlite:////data/app.db")) == "/data/app.db"
    assert sqlite_path("mysql+pymysql://u:p@h/db") is None


def test_backup_creates_consistent_copy(tmp_path):
    s = make_settings(tmp_path)
    make_db(s, rows=3)
    out = backup_database(s)
    assert out is not None
    dest = Path(out)
    assert dest.is_file()
    assert dest.name.startswith("app-") and dest.name.endswith(".db")
    assert count(dest) == 3
    con = sqlite3.connect(dest)
    assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    con.close()
    # no WAL sidecar left next to the backup
    assert not dest.with_name(dest.name + "-wal").exists()


def test_backup_includes_open_wal_writes(tmp_path):
    s = make_settings(tmp_path)
    make_db(s, rows=2)
    con = sqlite3.connect(s.config_dir / "app.db")
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("INSERT INTO pl (name) VALUES ('open-writer')")
    con.commit()
    out = backup_database(s)
    con.close()
    assert out is not None
    assert count(Path(out)) == 3


def test_backup_rotates_old_files(tmp_path):
    s = make_settings(tmp_path, backup_keep=2)
    make_db(s)
    bdir = tmp_path / "backup"
    bdir.mkdir()
    for day in range(1, 6):
        (bdir / f"app-2025010{day}-003000.db").write_bytes(b"old")
    (bdir / "unrelated.db").write_bytes(b"keep-me")
    (bdir / "app-foo.db").write_bytes(b"keep-me")
    out = backup_database(s)
    assert out is not None
    names = sorted(p.name for p in bdir.glob("app-????????-??????.db"))
    assert names == ["app-20250105-003000.db", Path(out).name]
    assert (bdir / "unrelated.db").is_file()
    assert (bdir / "app-foo.db").is_file()


def test_backup_keep_zero_keeps_everything(tmp_path):
    s = make_settings(tmp_path, backup_keep=0)
    make_db(s)
    bdir = tmp_path / "backup"
    bdir.mkdir()
    for day in range(1, 6):
        (bdir / f"app-2025010{day}-003000.db").write_bytes(b"old")
    assert backup_database(s) is not None
    assert len(list(bdir.glob("app-????????-??????.db"))) == 6


def test_backup_skips_non_sqlite(tmp_path):
    s = make_settings(tmp_path, database_url="mysql+pymysql://u:p@h/db")
    assert backup_database(s) is None
    assert not (tmp_path / "backup").exists()


def test_backup_missing_source(tmp_path):
    s = make_settings(tmp_path)
    assert backup_database(s) is None


def test_backup_unwritable_target(tmp_path):
    s = make_settings(tmp_path)
    make_db(s)
    blocker = tmp_path / "backup"
    blocker.write_bytes(b"")  # a file where the directory should be
    assert backup_database(s) is None


def test_backup_keep_validation():
    with pytest.raises(ValidationError):
        Settings(youtube_channel="@x", backup_keep=-1)


def test_archives_backup_creates_tarball(tmp_path):
    s = make_settings(tmp_path)
    make_archives(s, count=3)
    out = backup_archives(s)
    assert out is not None
    dest = Path(out)
    assert dest.is_file()
    assert dest.name.startswith("archives-") and dest.name.endswith(".tar.gz")
    with tarfile.open(dest, "r:gz") as tar:
        names = tar.getnames()
        assert sorted(names) == [
            "archives", "archives/PL0000.txt", "archives/PL0001.txt", "archives/PL0002.txt",
        ]
        content = tar.extractfile("archives/PL0001.txt").read().decode()
    assert content == "youtube vid00000001\n"


def test_archives_backup_empty_dir_is_ok(tmp_path):
    s = make_settings(tmp_path)
    s.archives_dir.mkdir(parents=True, exist_ok=True)
    out = backup_archives(s)
    assert out is not None
    with tarfile.open(out, "r:gz") as tar:
        assert tar.getnames() == ["archives"]


def test_archives_backup_missing_dir(tmp_path):
    s = make_settings(tmp_path)
    assert backup_archives(s) is None
    assert not (tmp_path / "backup").exists()


def test_archives_backup_unwritable_target(tmp_path):
    s = make_settings(tmp_path)
    make_archives(s)
    blocker = tmp_path / "backup"
    blocker.write_bytes(b"")  # a file where the directory should be
    assert backup_archives(s) is None
    assert not (tmp_path / "backup" / "archives-x.tar.gz").exists()


def test_archives_backup_rotates_old_files(tmp_path):
    s = make_settings(tmp_path, backup_keep=2)
    make_archives(s)
    bdir = tmp_path / "backup"
    bdir.mkdir()
    for day in range(1, 6):
        (bdir / f"archives-2025010{day}-003000.tar.gz").write_bytes(b"old")
    (bdir / "unrelated.tar.gz").write_bytes(b"keep-me")
    (bdir / "archives-foo.tar.gz").write_bytes(b"keep-me")
    out = backup_archives(s)
    assert out is not None
    names = sorted(p.name for p in bdir.glob(ARCHIVES_NAME_GLOB))
    assert names == ["archives-20250105-003000.tar.gz", Path(out).name]
    assert (bdir / "unrelated.tar.gz").is_file()
    assert (bdir / "archives-foo.tar.gz").is_file()


def test_prune_rotates_both_backup_types(tmp_path):
    s = make_settings(tmp_path, backup_keep=1)
    make_db(s, rows=1)
    make_archives(s)
    bdir = tmp_path / "backup"
    bdir.mkdir()
    (bdir / "app-20250101-003000.db").write_bytes(b"old")
    (bdir / "archives-20250101-003000.tar.gz").write_bytes(b"old")
    assert backup_database(s) is not None
    assert backup_archives(s) is not None
    assert len(list(bdir.glob(BACKUP_NAME_GLOB))) == 1
    assert len(list(bdir.glob(ARCHIVES_NAME_GLOB))) == 1


def test_archives_backup_keep_zero_keeps_everything(tmp_path):
    s = make_settings(tmp_path, backup_keep=0)
    make_archives(s)
    bdir = tmp_path / "backup"
    bdir.mkdir()
    for day in range(1, 6):
        (bdir / f"archives-2025010{day}-003000.tar.gz").write_bytes(b"old")
    assert backup_archives(s) is not None
    assert len(list(bdir.glob(ARCHIVES_NAME_GLOB))) == 6


def test_scheduler_backup_run_writes_both(tmp_path):
    pytest.importorskip("sqlalchemy")
    from app.scheduler import AppScheduler

    s = make_settings(tmp_path)
    make_db(s, rows=1)
    make_archives(s)
    sched = AppScheduler(s, None)  # type: ignore[arg-type]
    asyncio.run(sched.backup_run())
    bdir = tmp_path / "backup"
    assert len(list(bdir.glob(BACKUP_NAME_GLOB))) == 1
    assert len(list(bdir.glob(ARCHIVES_NAME_GLOB))) == 1
