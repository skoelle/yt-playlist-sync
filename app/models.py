# Copyright (c) 2026 Stefan Koelle (https://stefankoelle.de)
# Licensed under the MIT License. See LICENSE file in project root for details.
"""ORM models (see SPEC section 7)."""
from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base

PLAYLIST_TYPES = ("sync", "oneshot")
PLAYLIST_STATES = ("new", "queued", "running", "done", "failed", "idle")
JOB_STATUSES = ("queued", "running", "success", "failed", "interrupted", "cancelled")
JOB_TRIGGERS = ("discovery", "nightly", "manual", "retry", "full_rerun")


def _in(column: str, values: tuple) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class Playlist(Base):
    __tablename__ = "playlists"
    __table_args__ = (
        CheckConstraint(_in("type", PLAYLIST_TYPES), name="ck_playlists_type"),
        CheckConstraint(_in("state", PLAYLIST_STATES), name="ck_playlists_state"),
        CheckConstraint(_in("remote_status", ("active", "removed")), name="ck_playlists_remote"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    playlist_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(500))
    type: Mapped[str] = mapped_column(String(16))
    folder_name: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    remote_item_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    downloaded_count: Mapped[int] = mapped_column(Integer, default=0)
    skipped_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    state: Mapped[str] = mapped_column(String(16), default="new")
    remote_status: Mapped[str] = mapped_column(String(16), default="active")
    ignored: Mapped[bool] = mapped_column(Boolean, default=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime)
    first_downloaded_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_sync_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        CheckConstraint(_in("status", JOB_STATUSES), name="ck_jobs_status"),
        CheckConstraint(_in("trigger", JOB_TRIGGERS), name="ck_jobs_trigger"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    playlist_id: Mapped[int] = mapped_column(ForeignKey("playlists.id"), index=True)
    trigger: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    priority: Mapped[int] = mapped_column(Integer, default=2)
    queued_at: Mapped[datetime] = mapped_column(DateTime)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    current_item: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    progress_percent: Mapped[Optional[float]] = mapped_column(nullable=True)
    items_new: Mapped[int] = mapped_column(Integer, default=0)
    items_skipped: Mapped[int] = mapped_column(Integer, default=0)
    items_failed: Mapped[int] = mapped_column(Integer, default=0)
    exit_code: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    error_summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    log_path: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)


class Run(Base):
    __tablename__ = "runs"
    __table_args__ = (
        CheckConstraint(_in("kind", ("discovery", "sync")), name="ck_runs_kind"),
        CheckConstraint(_in("status", ("running", "success", "failed")), name="ck_runs_status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="running")
    message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
