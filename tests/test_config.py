# Copyright (c) 2026 Stefan Koelle (https://stefankoelle.de)
# Licensed under the MIT License. See LICENSE file in project root for details.
import pytest

pytest.importorskip("pydantic_settings")
pytest.importorskip("apscheduler")

from pathlib import Path  # noqa: E402

from pydantic import ValidationError  # noqa: E402

from app.config import Settings  # noqa: E402


def test_defaults(monkeypatch):
    for k in ("ONESHOT_KEYWORD", "DISCOVERY_INTERVAL_MIN", "DISCOVERY_INTERVAL_MAX", "SYNC_CRON",
              "DRY_RUN", "DATABASE_URL", "BACKUP_DIR", "BACKUP_KEEP"):
        monkeypatch.delenv(k, raising=False)
    s = Settings(youtube_channel="@beispielkanal")
    assert s.oneshot_keyword == "setlist"
    assert s.sync_cron == "0 3 * * *" and s.ytdlp_update_cron == "30 2 * * *"
    assert s.discovery_interval_min == 50 and s.discovery_interval_max == 70
    assert s.dry_run is False
    assert s.backup_keep == 7 and s.backup_dir == Path("/backup")
    assert s.job_gap_min == 1 and s.job_gap_max == 10
    assert s.db_url.startswith("sqlite:///") and s.db_url.endswith("app.db")


def test_env_override(monkeypatch):
    monkeypatch.setenv("YOUTUBE_CHANNEL", "@x")
    monkeypatch.setenv("ONESHOT_KEYWORD", "live")
    monkeypatch.setenv("DRY_RUN", "1")
    s = Settings()
    assert s.oneshot_keyword == "live" and s.dry_run is True


def test_missing_channel(monkeypatch):
    monkeypatch.delenv("YOUTUBE_CHANNEL", raising=False)
    with pytest.raises(ValidationError):
        Settings()


def test_invalid_cron_and_sleep():
    with pytest.raises(ValidationError):
        Settings(youtube_channel="@x", sync_cron="not a cron")
    with pytest.raises(ValidationError):
        Settings(youtube_channel="@x", sleep_min=10, sleep_max=1)
    with pytest.raises(ValidationError):
        Settings(youtube_channel="@x", job_gap_min=5, job_gap_max=1)
    with pytest.raises(ValidationError):
        Settings(youtube_channel="@x", job_gap_min=-1)
    with pytest.raises(ValidationError):
        Settings(youtube_channel="@x", discovery_interval_min=70, discovery_interval_max=50)
    with pytest.raises(ValidationError):
        Settings(youtube_channel="@x", discovery_interval_min=0)
