import asyncio
import time

from app.runner import ProcessHandle, RunParams, run_playlist


def params(tmp_path, stub, pid="PLaaa", dry_run=False):
    return RunParams(
        ytdlp_bin=stub.bin, playlist_id=pid, folder=f"Folder [{pid}]",
        data_dir=tmp_path / "data", config_dir=tmp_path / "config",
        log_path=tmp_path / "config" / "logs" / "1.log", sleep_min=0, sleep_max=0, dry_run=dry_run,
    )


def run(p, events=None, handle=None):
    events = events if events is not None else []
    return asyncio.run(run_playlist(p, events.append, handle or ProcessHandle()))


def test_success_creates_files_and_archive(tmp_path, stub):
    events = []
    res = run(params(tmp_path, stub), events)
    assert res.success and res.new_count == 3 and res.failed == 0 and res.total == 3
    assert len(list((tmp_path / "data" / "Folder [PLaaa]").glob("*.mp4"))) == 3
    assert (tmp_path / "config" / "archives" / "PLaaa.txt").read_text().count("youtube") == 3
    assert any(e["type"] == "progress" for e in events)
    assert (tmp_path / "config" / "logs" / "1.log").exists()


def test_second_run_downloads_nothing(tmp_path, stub):
    run(params(tmp_path, stub))
    res = run(params(tmp_path, stub))
    assert res.success and res.new_count == 0


def test_temporary_failure_then_retry_only_missing(tmp_path, stub):
    stub.data_ref["fail"] = {"vid00000002": "temporary"}
    stub.save()
    res = run(params(tmp_path, stub))
    assert not res.success and res.failed == 1 and res.new_count == 2
    assert "missing" in res.error_summary
    stub.data_ref["fail"] = {}
    stub.save()
    res = run(params(tmp_path, stub))
    assert res.success and res.new_count == 1


def test_permanent_unavailable_counts_as_skipped(tmp_path, stub):
    stub.data_ref["fail"] = {"vid00000002": "unavailable"}
    stub.save()
    res = run(params(tmp_path, stub))
    assert res.success and res.skipped == 1 and res.new_count == 2 and res.failed == 0


def test_private_marker_in_listing_is_skipped(tmp_path, stub):
    stub.data_ref["playlists"]["PLaaa"][1]["title"] = "[Private video]"
    stub.data_ref["fail"] = {"vid00000002": "unavailable"}
    stub.save()
    res = run(params(tmp_path, stub))
    assert res.success and res.skipped == 1


def test_dry_run_writes_nothing(tmp_path, stub):
    res = run(params(tmp_path, stub, dry_run=True))
    assert res.success and res.new_count == 3
    assert not (tmp_path / "data").exists()
    assert not (tmp_path / "config" / "archives" / "PLaaa.txt").exists()


def test_rate_limit_stops_job_quickly(tmp_path, stub):
    stub.data_ref["fail"] = {"vid00000001": "ratelimit"}
    stub.save()
    t0 = time.time()
    res = run(params(tmp_path, stub))
    assert res.rate_limited and not res.success
    assert time.time() - t0 < 20


def test_cancel_running_job(tmp_path, stub):
    stub.data_ref["slow"] = 2
    stub.save()

    async def scenario():
        handle = ProcessHandle()
        task = asyncio.create_task(run_playlist(params(tmp_path, stub), lambda e: None, handle))
        await asyncio.sleep(1.5)
        handle.cancel()
        return await asyncio.wait_for(task, 20)

    res = asyncio.run(scenario())
    assert res.cancelled and not res.success


def test_listing_failure_returns_failed_result(tmp_path, stub):
    res = run(params(tmp_path, stub, pid="PLunknown"))
    assert not res.success and "Could not list playlist" in res.error_summary
