"""Bước ghép clip trong pipeline job — phần có I/O của app/clips.py.

Tách riêng để ``app/jobs.py`` không phình quá 400 dòng và để phần dựng lệnh
(``app/clips.py``, hàm thuần) test được mà không cần chạy ffmpeg.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from pathlib import Path

from app.clips import (
    MERGED_NAME,
    SourceVideo,
    build_concat_command,
    merged_duration,
    resolve_clips,
    resolve_concat_canvas,
)
from app.config import Settings
from app.ffmpeg_runner import probe, run_ffmpeg
from app.job_store import Job
from app.models import JobStatus
from app.utils import FfmpegFailed, format_hms

__all__ = ["merge_sources", "needs_merge"]

logger = logging.getLogger(__name__)

_STDERR_TAIL_LINES = 200


def needs_merge(job: Job, sources: list[Path]) -> bool:
    """Có phải chạy bước ghép không?

    Khai ``clips`` là chắc chắn có (kể cả khi chỉ cắt một đoạn từ một video).
    Gửi nhiều video mà không khai gì thì hiểu là "ghép trọn cả loạt theo thứ
    tự đã gửi" — đó là việc người dùng muốn, chứ không ai gửi 5 video lên để
    rồi chỉ dùng video đầu.
    """
    return bool(job.options.clips) or len(sources) > 1


async def merge_sources(
    job: Job, settings: Settings, sources: list[Path], log: logging.LoggerAdapter
) -> Path:
    """Ghép các đoạn theo ``job.options.clips``, trả về file kết quả.

    Xoá luôn file nguồn sau khi ghép xong: ``/tmp`` trên Cloud Run là RAM, giữ
    lại cả loạt video gốc trong lúc render là cách nhanh nhất để hết bộ nhớ.
    """
    job.status = JobStatus.MERGING
    job.stage_message = "Đang đọc thông tin các video nguồn"
    job.progress = 0.0

    labels = job.sources.video_labels
    probed = [
        SourceVideo(
            name=path.name,
            probe=await probe(path),
            label=labels[index] if index < len(labels) else path.name,
        )
        for index, path in enumerate(sources)
    ]
    clips = resolve_clips(job.options.clips, probed)
    width, height, fps = resolve_concat_canvas(clips, probed, job.options)
    # Độ dài THẬT của file sau khi ghép, đã trừ phần chồng do crossfade — dùng
    # con số này để theo dõi tiến độ mới khớp % thật với ffmpeg đang chạy.
    total = merged_duration(clips, job.options)
    log.info(
        "Ghép clip",
        extra={
            "clips": len(clips),
            "sources": len(probed),
            "canvas": f"{width}x{height}",
            "fps": fps,
            "total_seconds": round(total, 2),
        },
    )

    cmd = build_concat_command(
        clips, width, height, fps, job.options, threads=settings.ffmpeg_threads
    )
    log.info("Chạy ffmpeg ghép clip", extra={"argv": cmd})

    stderr_buf: deque[str] = deque(maxlen=_STDERR_TAIL_LINES)

    def _on_progress(percent: float, seconds: float) -> None:
        job.progress = round(percent, 1)
        job.stage_message = f"Ghép clip {format_hms(seconds)} / {format_hms(total)}"

    def _on_start(process: asyncio.subprocess.Process) -> None:
        # Giữ handle để DELETE /api/jobs/{id} huỷ được ngay cả khi đang ghép.
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
    if exit_code != 0:
        raise FfmpegFailed(
            f"Ghép clip thất bại, ffmpeg thoát với mã {exit_code}",
            detail="\n".join(stderr_buf),
        )

    merged = job.workspace / MERGED_NAME
    if not merged.exists():
        raise FfmpegFailed("ffmpeg báo ghép xong nhưng không có file kết quả")

    for path in sources:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            log.warning("Không xoá được video nguồn", extra={"path": str(path)})
    job.progress = 100.0
    log.info("Ghép clip xong", extra={"size_bytes": merged.stat().st_size})
    return merged
