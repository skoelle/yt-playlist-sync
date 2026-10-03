# Copyright (c) 2026 Stefan Koelle (https://stefankoelle.de)
# Licensed under the MIT License. See LICENSE file in project root for details.
import sqlite3
from pathlib import Path

import pytest

pytest.importorskip("pydantic_settings")
pytest.importorskip("apscheduler")

from pydantic import ValidationError  # noqa: E402

from app.backup import backup_database, sqlite_path  # noqa: E402
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
