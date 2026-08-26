"""In-memory job store + worker chạy nền — docs/SPEC.md §2, §8."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import deque

from app.config import Settings
from app.drive import upload_file
from app.ffmpeg_runner import (
    SUBS_ASS_NAME,
    SUBS_SRT_NAME,
    ProbeResult,
    RenderInputs,
    build_ffmpeg_command,
    probe,
    resolve_canvas_size,
    run_ffmpeg,
)
from app.job_store import (
    Job,
    JobSources,
    JobStore,
    cancel_job,
    janitor_loop,
    shutdown_jobs,
)
from app.models import JobError, JobOutput, JobStatus
from app.prepare import check_tmp_space, prepare_inputs, probe_stage
from app.overlay import INTRO_ASS_NAME, STYLED_ASS_NAME, BurnPlan, plan_burn
from app.subtitle_style import resolve_font_px, resolve_margin_h_px
from app.subtitle_wrap import resolve_max_chars_per_line, rewrap_ass_file
from app.utils import (
    AppError,
    FfmpegFailed,
    InternalError,
    bind_job,
    format_hms,
    safe_rmtree,
    utcnow,
)

__all__ = [
    "Job",
    "JobSources",
    "JobStore",
    "get_semaphore",
    "run_job",
    "janitor_loop",
    "cancel_job",
    "shutdown_jobs",
]

logger = logging.getLogger(__name__)

STDERR_TAIL_LINES = 200
# Ước lượng chỗ cần cho output: input + nhạc, nhân 2.5 vì /tmp là RAM và ffmpeg
# còn cần buffer. Thiếu -> báo INSUFFICIENT_TMP_SPACE trước khi bắt đầu render.
TMP_SPACE_FACTOR = 2.5

# --------------------------------------------------------------------------- #
# Worker
# --------------------------------------------------------------------------- #
_semaphores: dict[int, asyncio.Semaphore] = {}


def get_semaphore(settings: Settings) -> asyncio.Semaphore:
    """Semaphore dùng chung giới hạn số ffmpeg chạy song song (SPEC §2)."""
    limit = settings.max_concurrent_jobs
    if limit not in _semaphores:
        _semaphores[limit] = asyncio.Semaphore(limit)
    return _semaphores[limit]


async def run_job(
    job_id: str,
    store: JobStore,
    settings: Settings,
    semaphore: asyncio.Semaphore | None = None,
) -> None:
    """Pipeline đầy đủ: download -> probe -> render -> (upload) -> succeeded.

    Mọi exception được map về ``JobError``; file input luôn bị dọn trong
    ``finally`` kể cả khi fail hay bị cancel.
    """
    job = await store.get(job_id)
    if job is None:
        return
    log = bind_job(logger, job_id)
    job.started_at = utcnow()
    gate = semaphore if semaphore is not None else get_semaphore(settings)

    try:
        inputs = await prepare_inputs(job, settings, log)
        probe_result = await probe_stage(job, log)
        await check_tmp_space(job, settings, log)
        async with gate:
            await _render_stage(job, settings, inputs, probe_result, log)
        await _finalize(job, probe_result, log)
    except asyncio.CancelledError:
        job.status = JobStatus.CANCELLED
        job.finished_at = utcnow()
        job.stage_message = "Đã huỷ"
        log.warning("Job bị huỷ", extra={"status": job.status.value})
        raise
    except AppError as exc:
        _mark_failed(job, exc, log)
    except Exception as exc:  # noqa: BLE001 - không nuốt, chỉ đổi sang AppError
        _mark_failed(job, InternalError(str(exc) or type(exc).__name__), log, exc_info=exc)
    finally:
        job.process = None
        # /tmp là RAM: xoá mọi file input ngay, chỉ giữ output khi job thành công.
        await _cleanup_inputs(job, log)
        if job.status is not JobStatus.SUCCEEDED:
            await asyncio.to_thread(safe_rmtree, job.workspace)



async def _render_stage(
    job: Job,
    settings: Settings,
    inputs: RenderInputs,
    probe_result: ProbeResult,
    log: logging.LoggerAdapter,
) -> None:
    job.status = JobStatus.RENDERING
    # Progress của render là 0..99 theo đúng công thức SPEC §5.4, nên reset lại
    # sau giai đoạn download (client thấy status đổi nên không bị nhầm).
    job.progress = 0.0
    total = probe_result.duration

    # Nhạc không lặp: cần độ dài thật của file nhạc để tính mốc fade-out.
    music_duration: float | None = None
    if inputs.music and not job.options.music.loop:
        with contextlib.suppress(AppError):
            music_probe = await probe(job.workspace / inputs.music)
            music_duration = music_probe.duration
    canvas_width, canvas_height = resolve_canvas_size(probe_result, job.options)
    plan = await _plan_subtitles(job, inputs.subs, canvas_width, canvas_height, log)
    inputs = RenderInputs(
        video=inputs.video,
        music=inputs.music,
        subs=plan.subs,
        music_duration=music_duration,
        subs_pre_styled=plan.pre_styled,
        play_res_y=plan.play_res_y,
        overlay=plan.overlay,
    )

    cmd = build_ffmpeg_command(
        inputs,
        probe_result,
        job.options,
        job.workspace,
        threads=settings.ffmpeg_threads,
        fonts_dir=settings.fonts_dir,
    )
    log.info("Chạy ffmpeg", extra={"argv": cmd})

    stderr_buf: deque[str] = deque(maxlen=STDERR_TAIL_LINES)

    def _on_progress(percent: float, seconds: float) -> None:
        job.progress = round(percent, 1)
        job.stage_message = f"Rendering {format_hms(seconds)} / {format_hms(total)}"

    def _on_start(process: asyncio.subprocess.Process) -> None:
        job.process = process

    exit_code = await run_ffmpeg(
        cmd,
        total_duration=total,
        on_progress=_on_progress,
        stderr_buf=stderr_buf,
        cwd=job.workspace,
        on_start=_on_start,
    )
    job.process = None

    if job.cancel_requested:
        raise asyncio.CancelledError
    if exit_code != 0:
        raise FfmpegFailed(
            f"ffmpeg thoát với mã {exit_code}",
            detail="\n".join(stderr_buf),
        )
    if not job.output_path.exists():
        raise FfmpegFailed("ffmpeg báo thành công nhưng không có file output")


async def _plan_subtitles(
    job: Job,
    subs_name: str | None,
    canvas_width: int,
    canvas_height: int,
    log: logging.LoggerAdapter,
) -> BurnPlan:
    """Dựng file .ass cần thiết và quyết định phụ đề đi vào ffmpeg thế nào.

    File .srt (đường đi thường gặp) được dựng lại thành .ass có PlayRes đúng
    khung hình: cỡ chữ/lề tính thẳng bằng pixel, hiệu ứng chữ chỉ là override
    tag, và chính ta tự chèn dấu xuống dòng. File .ass do người dùng đưa lên
    thì giữ nguyên style của họ, chỉ ghi đè bằng force_style như trước.
    """
    plan = await asyncio.to_thread(
        plan_burn, job.workspace, subs_name, job.options, canvas_width, canvas_height
    )
    if (
        plan.subs is not None
        and not plan.pre_styled
        and job.options.subtitle.mode == "burn"
        and job.options.subtitle.enabled
    ):
        # File ASS của người dùng: không dựng lại được nhưng vẫn tự xuống dòng
        # theo khung, vì force_style đã ép cỡ chữ về cỡ của ta.
        font_px = resolve_font_px(job.options.subtitle, canvas_width, canvas_height)
        margin_h = resolve_margin_h_px(job.options.subtitle, canvas_width)
        max_chars = resolve_max_chars_per_line(font_px, margin_h, canvas_width)
        await asyncio.to_thread(
            rewrap_ass_file, job.workspace / plan.subs, max_chars
        )
        log.debug("Đã tự xuống dòng phụ đề ASS", extra={"max_chars_per_line": max_chars})
    log.info(
        "Kế hoạch phụ đề",
        extra={
            "subs_file": plan.subs,
            "pre_styled": plan.pre_styled,
            "overlay_file": plan.overlay,
            "effect": job.options.subtitle.effect.value,
        },
    )
    return plan


async def _finalize(
    job: Job,
    probe_result: ProbeResult,
    log: logging.LoggerAdapter,
) -> None:
    output_path = job.output_path
    size_bytes = await asyncio.to_thread(lambda: output_path.stat().st_size)
    out_probe = None
    with contextlib.suppress(AppError):
        out_probe = await probe(output_path)

    output = JobOutput(
        filename=output_path.name,
        size_bytes=size_bytes,
        duration_seconds=out_probe.duration if out_probe else probe_result.duration,
        download_url=f"/api/jobs/{job.id}/download",
    )

    if job.options.delivery.upload_to_drive:
        job.status = JobStatus.UPLOADING
        job.stage_message = "Đang upload lên Google Drive"
        result = await upload_file(output_path, job.options.delivery.drive_folder_id)
        output = output.model_copy(
            update={"drive_file_id": result.file_id, "drive_view_url": result.web_view_link}
        )
        log.info("Upload Drive xong", extra={"drive_file_id": result.file_id})

    job.output = output
    job.progress = 100.0
    job.status = JobStatus.SUCCEEDED
    job.stage_message = "Hoàn tất"
    job.finished_at = utcnow()
    log.info("Job thành công", extra={"size_bytes": size_bytes})


def _mark_failed(
    job: Job,
    exc: AppError,
    log: logging.LoggerAdapter,
    exc_info: BaseException | None = None,
) -> None:
    job.status = JobStatus.FAILED
    job.finished_at = utcnow()
    job.stage_message = "Thất bại"
    job.error = JobError(code=exc.code, message=exc.message, detail=exc.detail)
    log.error(
        "Job thất bại",
        extra={"error_code": exc.code, "error_message": exc.message},
        exc_info=exc_info,
    )


async def _cleanup_inputs(job: Job, log: logging.LoggerAdapter) -> None:
    """Xoá file input ngay khi không cần nữa — /tmp là RAM (SPEC §8)."""
    sources = job.sources
    targets = [
        sources.video_path,
        *sources.video_paths,
        sources.music_path,
        sources.srt_path,
        job.workspace / SUBS_SRT_NAME,
        job.workspace / SUBS_ASS_NAME,
        job.workspace / STYLED_ASS_NAME,
        job.workspace / INTRO_ASS_NAME,
    ]

    def _remove() -> None:
        for path in targets:
            if path is None:
                continue
            try:
                path.unlink(missing_ok=True)
            except OSError:
                log.warning("Không xoá được file input", extra={"path": str(path)})

    await asyncio.to_thread(_remove)
