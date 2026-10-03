# Copyright (c) 2026 Stefan Koelle (https://stefankoelle.de)
# Licensed under the MIT License. See LICENSE file in project root for details.
"""Settings loaded from environment variables."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from apscheduler.triggers.cron import CronTrigger
from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    youtube_channel: str
    oneshot_keyword: str = "setlist"
    discovery_cron: str = "0 * * * *"
    sync_cron: str = "0 3 * * *"
    ytdlp_update_cron: str = "30 2 * * *"
    tz: str = "Europe/Berlin"
    data_dir: Path = Path("/data")
    config_dir: Path = Path("/config")
    database_url: str | None = None
    puid: int = 1000
    pgid: int = 1000
    sleep_min: int = 3
    sleep_max: int = 10
    ytdlp_extra_args: str = ""
    ytdlp_bin: str = "yt-dlp"
    hc_discovery_url: str = ""
    hc_sync_url: str = ""
    log_level: str = "INFO"
    log_retention_days: int = 30
    dry_run: bool = False

    @field_validator("discovery_cron", "sync_cron", "ytdlp_update_cron")
    @classmethod
    def _check_cron(cls, value: str) -> str:
        CronTrigger.from_crontab(value)
        return value

    @field_validator("tz")
    @classmethod
    def _check_tz(cls, value: str) -> str:
        ZoneInfo(value)
        return value

    @field_validator("youtube_channel")
    @classmethod
    def _check_channel(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("YOUTUBE_CHANNEL must not be empty")
        return value.strip()

    @model_validator(mode="after")
    def _check_sleep(self) -> Settings:
        if self.sleep_min > self.sleep_max:
            raise ValueError("SLEEP_MIN must be <= SLEEP_MAX")
        return self

    @property
    def db_url(self) -> str:
        return self.database_url or f"sqlite:///{self.config_dir / 'app.db'}"

    @property
    def tzinfo(self) -> ZoneInfo:
        return ZoneInfo(self.tz)

    @property
    def archives_dir(self) -> Path:
        return self.config_dir / "archives"

    @property
    def logs_dir(self) -> Path:
        return self.config_dir / "logs"

    def data_writable(self) -> bool:
        probe = self.data_dir / ".yt-playlist-sync-write-test"
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            probe.write_text("ok")
            probe.unlink()
            return True
        except OSError:
            return False


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


def ensure_dirs(settings: Settings) -> None:
    for d in (settings.config_dir, settings.archives_dir, settings.logs_dir):
        os.makedirs(d, exist_ok=True)
