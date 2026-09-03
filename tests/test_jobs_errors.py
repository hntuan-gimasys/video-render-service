"""Đường lỗi của run_job: map exception về JobError đúng mã SPEC §3.6.

Tách khỏi tests/test_jobs.py để mỗi file test dưới 400 dòng.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import pytest

from app import jobs as jobs_mod
from app import prepare as prepare_mod
from app.config import Settings
from app.ffmpeg_runner import ProbeResult
from app.jobs import JobStore, run_job
from app.models import JobStatus, RenderOptions
from app.utils import DriveDownloadFailed
from tests.helpers import PROBE_OK
from tests.helpers import make_job as _make_job
from tests.helpers import patch_render as _patch_render


# --------------------------------------------------------------------------- #
# run_job — đường lỗi
# --------------------------------------------------------------------------- #
async def test_run_job_ffmpeg_failure_maps_to_error_code(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_render(monkeypatch, exit_code=1, write_output=False)
    store = JobStore()
    job = await store.create(_make_job(settings))

    await run_job("job1", store, settings)

    assert job.status is JobStatus.FAILED
    assert job.error is not None
    assert job.error.code == "FFMPEG_FAILED"
    assert "x264 [error]" in (job.error.detail or "")
    assert job.finished_at is not None
    # Workspace phải bị xoá sạch khi fail (SPEC §8)
    assert not job.workspace.exists()


async def test_run_job_missing_output_is_failure(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_render(monkeypatch, exit_code=0, write_output=False)
    store = JobStore()
    job = await store.create(_make_job(settings))
    await run_job("job1", store, settings)
    assert job.status is JobStatus.FAILED
    assert job.error is not None and job.error.code == "FFMPEG_FAILED"


async def test_run_job_drive_error_keeps_code(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_render(monkeypatch)

    async def _boom(*_args: Any, **_kwargs: Any) -> None:
        raise DriveDownloadFailed("không tải được", detail="HttpError 404")

    monkeypatch.setattr(prepare_mod, "download_file", _boom)
    store = JobStore()
    job = _make_job(settings)
    job.sources.video_path = None
    job.sources.video_url = "https://drive.google.com/file/d/1AbCdEfGhIjKlMnOpQrStUvWxYz012345/view"
    await store.create(job)

    await run_job("job1", store, settings)
    assert job.status is JobStatus.FAILED
    assert job.error is not None and job.error.code == "DRIVE_DOWNLOAD_FAILED"


async def test_run_job_unexpected_error_becomes_internal(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _boom(_path: Path) -> ProbeResult:
        raise ZeroDivisionError("bug")

    monkeypatch.setattr(prepare_mod, "probe", _boom)
    store = JobStore()
    job = await store.create(_make_job(settings))
    await run_job("job1", store, settings)
    assert job.status is JobStatus.FAILED
    assert job.error is not None and job.error.code == "INTERNAL"


async def test_run_job_insufficient_tmp_space(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_render(monkeypatch)
    monkeypatch.setattr(prepare_mod, "free_space_mb", lambda _path: 0.0)
    store = JobStore()
    job = _make_job(settings)
    (job.workspace / "input.mp4").write_bytes(b"x" * (2 * 1024 * 1024))
    await store.create(job)

    await run_job("job1", store, settings)
    assert job.status is JobStatus.FAILED
    assert job.error is not None and job.error.code == "INSUFFICIENT_TMP_SPACE"


async def test_run_job_unknown_id_is_noop(settings: Settings) -> None:
    await run_job("khong-ton-tai", JobStore(), settings)


# --------------------------------------------------------------------------- #
# Drive upload
# --------------------------------------------------------------------------- #
async def test_run_job_uploads_to_drive(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_render(monkeypatch)

    class _Result:
        file_id = "drive-id-1"
        name = "output.mp4"
        web_view_link = "https://drive.google.com/file/d/drive-id-1/view"

    seen: list[Any] = []

    async def _fake_upload(src: Path, folder_id: str | None = None, **_kw: Any) -> _Result:
        seen.append((src.name, folder_id))
        return _Result()

    monkeypatch.setattr(jobs_mod, "upload_file", _fake_upload)
    store = JobStore()
    job = _make_job(
        settings,
        RenderOptions.model_validate(
            {"delivery": {"upload_to_drive": True, "drive_folder_id": "folder-9"}}
        ),
    )
    await store.create(job)

    await run_job("job1", store, settings)

    assert seen == [("output.mp4", "folder-9")]
    assert job.output is not None
    assert job.output.drive_file_id == "drive-id-1"
    assert job.output.drive_view_url == "https://drive.google.com/file/d/drive-id-1/view"
    # download_url bị THAY HẲN bằng link Drive lấy thẳng bytes, để bước sau đọc
    # đúng field cũ là có link video mới mà không phải sửa gì.
    direct = "https://drive.google.com/uc?id=drive-id-1&export=download"
    assert job.output.download_url == direct
    assert job.output.drive_download_url == direct
    # Và bản trong /tmp (là RAM) được xoá ngay, không giữ tới hết JOB_TTL_SECONDS.
    assert not job.output_path.exists()


# --------------------------------------------------------------------------- #
# Semaphore
# --------------------------------------------------------------------------- #
async def test_semaphore_serialises_renders(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    concurrent = 0
    peak = 0

    async def _fake_probe(_path: Path) -> ProbeResult:
        return PROBE_OK

    async def _fake_run(*_args: Any, **kwargs: Any) -> int:
        nonlocal concurrent, peak
        concurrent += 1
        peak = max(peak, concurrent)
        await asyncio.sleep(0.02)
        concurrent -= 1
        cwd = kwargs.get("cwd")
        if cwd is not None:
            (Path(cwd) / "output.mp4").write_bytes(b"o")
        return 0

    monkeypatch.setattr(prepare_mod, "probe", _fake_probe)
    monkeypatch.setattr(jobs_mod, "probe", _fake_probe)
    monkeypatch.setattr(jobs_mod, "run_ffmpeg", _fake_run)

    store = JobStore()
    semaphore = asyncio.Semaphore(1)
    for index in range(3):
        job = _make_job(settings)
        job.id = f"job{index}"
        job.workspace = settings.work_dir / job.id
        job.workspace.mkdir(parents=True, exist_ok=True)
        video = job.workspace / "input.mp4"
        video.write_bytes(b"v")
        job.sources.video_path = video
        await store.create(job)

    await asyncio.gather(
        *(run_job(f"job{index}", store, settings, semaphore) for index in range(3))
    )
    assert peak == 1  # MAX_CONCURRENT_JOBS=1 -> không bao giờ 2 ffmpeg cùng lúc


async def test_run_job_ass_subtitle_uses_ass_filename(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SPEC §3.1 nhận .ass — lệnh phải trỏ subs.ass, không phải subs.srt."""
    recorded = _patch_render(monkeypatch)
    store = JobStore()
    job = _make_job(settings)
    raw = job.workspace / "subs_raw.ass"
    raw.write_text(
        "[Script Info]\n[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        "Dialogue: 0,0:00:01.00,0:00:03.00,Default,,0,0,0,,Xin chào\n",
        encoding="utf-8",
    )
    job.sources.srt_path = raw
    await store.create(job)

    await run_job("job1", store, settings)

    assert job.status is JobStatus.SUCCEEDED, job.error
    assert "[0:v]subtitles=subs.ass" in " ".join(recorded["cmd"])


async def test_run_job_cleans_both_subtitle_files(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_render(monkeypatch)
    store = JobStore()
    job = _make_job(settings)
    raw = job.workspace / "subs_raw.ass"
    raw.write_text(
        "[Events]\nDialogue: 0,0:00:01.00,0:00:03.00,Default,,0,0,0,,x\n", encoding="utf-8"
    )
    job.sources.srt_path = raw
    await store.create(job)

    await run_job("job1", store, settings)

    # Chỉ output được giữ lại, mọi file phụ đề trung gian phải bị dọn (/tmp là RAM)
    assert not (job.workspace / "subs.ass").exists()
    assert not (job.workspace / "subs.srt").exists()
    assert not raw.exists()
    assert job.output_path.exists()


async def test_failure_log_carries_the_error_detail(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """detail phải có trong log, không chỉ trong response của GET /api/jobs/{id}.

    Nhiều mã lỗi dùng chung một message nên message một mình không đủ để chẩn
    đoán, mà record trong store thì mất sau JOB_TTL_SECONDS.
    """

    async def _boom(*_args: Any, **_kwargs: Any) -> None:
        raise DriveDownloadFailed(
            "Không đọc được thư mục Drive abc",
            detail="BrokenPipeError: [Errno 32] Broken pipe",
        )

    monkeypatch.setattr(jobs_mod, "prepare_inputs", _boom)
    store = JobStore()
    job = await store.create(_make_job(settings))

    with caplog.at_level(logging.ERROR, logger="app.jobs"):
        await run_job("job1", store, settings)

    assert job.status is JobStatus.FAILED
    failures = [r for r in caplog.records if r.message == "Job thất bại"]
    assert failures, "không thấy log lỗi nào"
    assert getattr(failures[-1], "error_detail", None) == (
        "BrokenPipeError: [Errno 32] Broken pipe"
    )
