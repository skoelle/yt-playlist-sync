# Copyright (c) 2026 Stefan Koelle (https://stefankoelle.de)
# Licensed under the MIT License. See LICENSE file in project root for details.
"""Runs one yt-dlp download for one playlist. No database access here."""
from __future__ import annotations

import asyncio
import shlex
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import ytdlp


@dataclass
class RunParams:
    ytdlp_bin: str
    playlist_id: str
    folder: str
    data_dir: Path
    config_dir: Path
    log_path: Path
    sleep_min: int = 3
    sleep_max: int = 10
    extra_args: str = ""
    dry_run: bool = False


@dataclass
class RunResult:
    success: bool = False
    cancelled: bool = False
    rate_limited: bool = False
    exit_code: int | None = None
    total: int = 0
    archived: int = 0
    new_count: int = 0
    skipped: int = 0
    failed: int = 0
    error_summary: str | None = None
    errors: list[dict[str, Any]] = field(default_factory=list)


class ProcessHandle:
    """Lets other tasks cancel or terminate the running yt-dlp process."""

    def __init__(self, grace: float = 30.0) -> None:
        self.proc: asyncio.subprocess.Process | None = None
        self.cancelled = False
        self.grace = grace

    def attach(self, proc: asyncio.subprocess.Process) -> None:
        self.proc = proc
        if self.cancelled:
            self.terminate()

    def terminate(self) -> None:
        proc = self.proc
        if proc is None or proc.returncode is not None:
            return
        proc.terminate()
        try:
            asyncio.get_running_loop().call_later(self.grace, self._kill)
        except RuntimeError:
            pass

    def _kill(self) -> None:
        if self.proc is not None and self.proc.returncode is None:
            self.proc.kill()

    def cancel(self) -> None:
        self.cancelled = True
        self.terminate()


async def run_playlist(
    p: RunParams, on_event: Callable[[dict[str, Any]], None], handle: ProcessHandle
) -> RunResult:
    env = ytdlp.ytdlp_env(p.config_dir)
    archive = p.config_dir / "archives" / f"{p.playlist_id}.txt"
    archive.parent.mkdir(parents=True, exist_ok=True)
    p.log_path.parent.mkdir(parents=True, exist_ok=True)
    result = RunResult()

    with open(p.log_path, "a", encoding="utf-8") as log:

        def write(text: str) -> None:
            log.write(text.rstrip("\n") + "\n")
            log.flush()

        try:
            entries = await ytdlp.list_playlist_entries(p.ytdlp_bin, p.playlist_id, env)
        except Exception as exc:  # noqa: BLE001
            write(f"Could not list playlist: {exc}")
            result.error_summary = f"Could not list playlist: {str(exc)[:300]}"
            return result

        before = ytdlp.read_archive(archive)
        cmd = ytdlp.build_download_command(
            ytdlp_bin=p.ytdlp_bin, playlist_id=p.playlist_id, folder=p.folder, data_dir=p.data_dir,
            archive_path=archive, sleep_min=p.sleep_min, sleep_max=p.sleep_max,
            extra_args=p.extra_args, dry_run=p.dry_run,
        )
        write("$ " + shlex.join(cmd))
        if not p.dry_run:
            (p.data_dir / p.folder).mkdir(parents=True, exist_ok=True)

        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=env, limit=1 << 20
        )
        handle.attach(proc)
        assert proc.stdout is not None
        async for raw in proc.stdout:
            line = raw.decode("utf-8", errors="replace")
            if not line.startswith(ytdlp.PROGRESS_PREFIX):
                write(line)
            event = ytdlp.parse_line(line)
            if event is None:
                continue
            if event["type"] == "error":
                result.errors.append(event)
                if event["ratelimit"] and not result.rate_limited:
                    result.rate_limited = True
                    write("Rate limit or bot check detected, stopping this job.")
                    handle.terminate()
            on_event(event)
        result.exit_code = await proc.wait()

    after = ytdlp.read_archive(archive)
    ev = ytdlp.evaluate(entries, after, result.errors, p.dry_run)
    result.total = ev.total
    result.archived = ev.archived
    result.skipped = ev.unavailable
    result.failed = len(ev.missing)
    result.new_count = ev.simulated if p.dry_run else len(after - before)

    if handle.cancelled:
        result.cancelled = True
        result.error_summary = "Cancelled by user"
        return result
    if result.rate_limited:
        result.error_summary = "Rate limited by YouTube, queue paused for a while"
        return result
    result.success = not ev.missing
    if ev.missing:
        msgs: list[str] = []
        for e in result.errors:
            m = e["message"][:200]
            if m not in msgs:
                msgs.append(m)
        detail = "; ".join(msgs[:3]) or f"exit code {result.exit_code}"
        result.error_summary = f"{len(ev.missing)} video(s) missing: {detail}"
    return result
