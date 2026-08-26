"""Test cho app/job_store.py — cancel_job, janitor_loop, shutdown_jobs."""

from __future__ import annotations

import asyncio
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from app import jobs as jobs_mod
from app import prepare as prepare_mod
from app.config import Settings
from app.ffmpeg_runner import ProbeResult
from app.job_store import Job, JobStore, cancel_job, janitor_loop, shutdown_jobs
from app.jobs import run_job
from app.models import JobStatus
from app.utils import utcnow
from tests.helpers import PROBE_OK
from tests.helpers import make_job as _make_job

# --------------------------------------------------------------------------- #
# cancel_job
# --------------------------------------------------------------------------- #
async def test_cancel_job_terminates_process_and_removes_workspace(
    settings: Settings,
) -> None:
    store = JobStore()
    job = await store.create(_make_job(settings))
    job.status = JobStatus.RENDERING
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import time; time.sleep(30)",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    job.process = process

    await cancel_job(job)

    assert process.returncode is not None  # đã thoát
    assert job.status is JobStatus.CANCELLED
    assert job.finished_at is not None
    assert job.cancel_requested is True
    assert not job.workspace.exists()


async def test_cancel_job_cancels_task(settings: Settings) -> None:
    store = JobStore()
    job = await store.create(_make_job(settings))
    job.status = JobStatus.RENDERING

    async def _long() -> None:
        await asyncio.sleep(30)

    job.task = asyncio.create_task(_long())
    await asyncio.sleep(0)

    await cancel_job(job)

    assert job.task.cancelled()
    assert job.status is JobStatus.CANCELLED


async def test_cancel_job_keeps_terminal_status(settings: Settings) -> None:
    job = _make_job(settings)
    job.status = JobStatus.SUCCEEDED
    job.finished_at = utcnow()
    await cancel_job(job, remove_workspace=False)
    assert job.status is JobStatus.SUCCEEDED  # không ghi đè trạng thái đã kết thúc


async def test_run_job_cancelled_mid_render_cleans_up(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = asyncio.Event()

    async def _fake_probe(_path: Path) -> ProbeResult:
        return PROBE_OK

    async def _fake_run(*_args: Any, **_kwargs: Any) -> int:
        started.set()
        await asyncio.sleep(30)
        return 0

    monkeypatch.setattr(prepare_mod, "probe", _fake_probe)
    monkeypatch.setattr(jobs_mod, "probe", _fake_probe)
    monkeypatch.setattr(jobs_mod, "run_ffmpeg", _fake_run)

    store = JobStore()
    job = await store.create(_make_job(settings))
    task = asyncio.create_task(run_job("job1", store, settings))
    job.task = task
    await started.wait()

    await cancel_job(job)

    assert job.status is JobStatus.CANCELLED
    assert not job.workspace.exists()  # file /tmp được dọn kể cả khi bị huỷ


# --------------------------------------------------------------------------- #
# Janitor & shutdown
# --------------------------------------------------------------------------- #
async def test_janitor_removes_expired_jobs(settings: Settings) -> None:
    store = JobStore()
    job = await store.create(_make_job(settings))
    job.status = JobStatus.SUCCEEDED
    job.finished_at = utcnow() - timedelta(seconds=10)

    task = asyncio.create_task(janitor_loop(store, settings, interval=0.01))
    for _ in range(100):
        await asyncio.sleep(0.01)
        if await store.get("job1") is None:
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert await store.get("job1") is None
    assert not job.workspace.exists()


async def test_janitor_survives_errors(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = JobStore()
    calls: list[int] = []

    async def _boom(_ttl: int) -> list[Job]:
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError("tạm lỗi")
        return []

    monkeypatch.setattr(store, "list_expired", _boom)
    task = asyncio.create_task(janitor_loop(store, settings, interval=0.01))
    for _ in range(100):
        await asyncio.sleep(0.01)
        if len(calls) >= 3:
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(calls) >= 3  # lỗi một vòng không làm janitor chết


async def test_shutdown_jobs_cancels_running(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = JobStore()
    job = await store.create(_make_job(settings))
    job.status = JobStatus.RENDERING

    async def _long() -> None:
        await asyncio.sleep(30)

    job.task = asyncio.create_task(_long())
    await asyncio.sleep(0)

    await shutdown_jobs(store, timeout=2.0)

    assert job.status is JobStatus.CANCELLED
    assert job.task.cancelled()


async def test_shutdown_jobs_noop_when_idle(settings: Settings) -> None:
    store = JobStore()
    job = await store.create(_make_job(settings))
    job.status = JobStatus.SUCCEEDED
    await shutdown_jobs(store)
    assert job.status is JobStatus.SUCCEEDED
