import pytest

pytest.importorskip("pydantic_settings")
pytest.importorskip("apscheduler")

from pydantic import ValidationError  # noqa: E402

from app.config import Settings  # noqa: E402


def test_defaults(monkeypatch):
    for k in ("ONESHOT_KEYWORD", "DISCOVERY_CRON", "SYNC_CRON", "DRY_RUN", "DATABASE_URL"):
        monkeypatch.delenv(k, raising=False)
    s = Settings(youtube_channel="@beispielkanal")
    assert s.oneshot_keyword == "setlist"
    assert s.discovery_cron == "0 * * * *" and s.sync_cron == "0 3 * * *"
    assert s.dry_run is False
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
