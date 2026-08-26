"""Test cho app/jobs.py — JobStore và pipeline run_job.

Không dùng ffmpeg thật: patch `probe` và `run_ffmpeg` của module jobs.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from app import jobs as jobs_mod
from app.config import Settings
from app.ffmpeg_runner import ProbeResult
from app.jobs import JobStore, run_job
from app.models import JobStatus, RenderOptions
from app.utils import utcnow
from tests.helpers import make_job as _make_job
from tests.helpers import patch_render as _patch_render


# --------------------------------------------------------------------------- #
# JobStore
# --------------------------------------------------------------------------- #
async def test_store_crud(settings: Settings) -> None:
    store = JobStore()
    job = _make_job(settings)
    await store.create(job)
    assert (await store.get("job1")) is job
    assert await store.count_active() == 1

    await store.update("job1", status=JobStatus.SUCCEEDED, progress=100.0)
    fetched = await store.get("job1")
    assert fetched is not None and fetched.status is JobStatus.SUCCEEDED
    assert await store.count_active() == 0

    assert await store.delete("job1") is job
    assert await store.get("job1") is None
    assert await store.update("job1", progress=1.0) is None


async def test_count_active_counts_all_non_terminal(settings: Settings) -> None:
    store = JobStore()
    for index, status in enumerate(
        [
            JobStatus.QUEUED,
            JobStatus.DOWNLOADING,
            JobStatus.PROBING,
            JobStatus.RENDERING,
            JobStatus.UPLOADING,
            JobStatus.SUCCEEDED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
        ]
    ):
        job = _make_job(settings)
        job.id = f"job{index}"
        job.status = status
        await store.create(job)
    # 5 trạng thái chưa terminal -> QUEUE_FULL đếm theo con số này (quyết định B7)
    assert await store.count_active() == 5


async def test_list_expired_only_finished_jobs(settings: Settings) -> None:
    store = JobStore()
    old = _make_job(settings)
    old.id = "old"
    old.status = JobStatus.SUCCEEDED
    old.finished_at = utcnow() - timedelta(seconds=3600)
    fresh = _make_job(settings)
    fresh.id = "fresh"
    fresh.status = JobStatus.SUCCEEDED
    fresh.finished_at = utcnow()
    running = _make_job(settings)
    running.id = "running"
    running.status = JobStatus.RENDERING
    for job in (old, fresh, running):
        await store.create(job)

    expired = await store.list_expired(ttl_seconds=60)
    assert [job.id for job in expired] == ["old"]


# --------------------------------------------------------------------------- #
# run_job — đường thành công
# --------------------------------------------------------------------------- #
async def test_run_job_success(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded = _patch_render(monkeypatch, progress=[(50.0, 2.5)])
    store = JobStore()
    job = await store.create(_make_job(settings))

    await run_job("job1", store, settings)

    assert job.status is JobStatus.SUCCEEDED
    assert job.progress == 100.0
    assert job.error is None
    assert job.output is not None
    assert job.output.filename == "output.mp4"
    assert job.output.size_bytes == len(b"rendered-output")
    assert job.output.download_url == "/api/jobs/job1/download"
    assert job.output.duration_seconds == 5.0
    assert job.started_at is not None and job.finished_at is not None
    # ffmpeg phải chạy với cwd = workspace (SPEC §5.3.4)
    assert recorded["cwd"] == job.workspace
    assert recorded["total_duration"] == 5.0
    # Output còn lại, input đã bị dọn
    assert job.output_path.exists()
    assert not (job.workspace / "input.mp4").exists()


async def test_run_job_stage_message_and_progress_during_render(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """stage_message phải đúng định dạng SPEC §3.2 lúc đang render."""
    snapshots: list[tuple[JobStatus, float, str]] = []
    store = JobStore()

    async def _fake_probe(_path: Path) -> ProbeResult:
        return ProbeResult(duration=194.0, has_video=True, has_audio=True, video_codec="h264")

    async def _fake_run(
        _cmd: list[str],
        total_duration: float,
        on_progress: Any = None,
        stderr_buf: Any = None,
        *,
        cwd: Path | None = None,
        on_start: Any = None,
    ) -> int:
        job = await store.get("job1")
        assert job is not None
        on_progress(42.7, 83.0)
        snapshots.append((job.status, job.progress, job.stage_message))
        (Path(cwd or ".") / "output.mp4").write_bytes(b"o")
        return 0

    from app import prepare as prepare_mod

    monkeypatch.setattr(prepare_mod, "probe", _fake_probe)
    monkeypatch.setattr(jobs_mod, "probe", _fake_probe)
    monkeypatch.setattr(jobs_mod, "run_ffmpeg", _fake_run)
    job = await store.create(_make_job(settings))

    await run_job("job1", store, settings)

    assert snapshots == [(JobStatus.RENDERING, 42.7, "Rendering 00:01:23 / 00:03:14")]
    assert (job.status, job.progress, job.stage_message) == (
        JobStatus.SUCCEEDED,
        100.0,
        "Hoàn tất",
    )


async def test_run_job_normalizes_srt_into_workspace(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, sample_srt_content: str
) -> None:
    recorded = _patch_render(monkeypatch)
    store = JobStore()
    job = _make_job(settings)
    raw_srt = job.workspace / "raw.srt"
    raw_srt.write_bytes(("﻿" + sample_srt_content.replace("\n", "\r\n")).encode("utf-8"))
    job.sources.srt_path = raw_srt
    await store.create(job)

    await run_job("job1", store, settings)

    assert job.status is JobStatus.SUCCEEDED
    # File .srt được chuẩn hoá thành subs.srt rồi dựng lại thành styled.ass có
    # PlayRes đúng khung hình (xem app/overlay.py) — đó mới là file đem burn.
    assert "[0:v]subtitles=styled.ass" in " ".join(recorded["cmd"])
    # File tự sinh đã có style sẵn -> không kèm force_style nữa.
    assert "force_style" not in " ".join(recorded["cmd"])


async def test_run_job_skips_subs_when_disabled(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, sample_srt_content: str
) -> None:
    recorded = _patch_render(monkeypatch)
    store = JobStore()
    job = _make_job(settings, RenderOptions.model_validate({"subtitle": {"enabled": False}}))
    raw = job.workspace / "raw.srt"
    raw.write_text(sample_srt_content, encoding="utf-8")
    job.sources.srt_path = raw
    await store.create(job)

    await run_job("job1", store, settings)
    assert "subtitles=" not in " ".join(recorded["cmd"])


async def test_run_job_probes_music_when_loop_disabled(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded = _patch_render(monkeypatch)
    store = JobStore()
    job = _make_job(settings, RenderOptions.model_validate({"music": {"loop": False}}))
    music = job.workspace / "music.mp3"
    music.write_bytes(b"fake-music")
    job.sources.music_path = music
    await store.create(job)

    await run_job("job1", store, settings)
    # probe được gọi cho video, cho nhạc (tính fade-out) và cho output
    assert "music.mp3" in recorded["probed"]
