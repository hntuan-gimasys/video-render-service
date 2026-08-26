"""Các bước chuẩn bị của pipeline: tải nguồn, ghép clip, probe, kiểm tra /tmp.

Tách khỏi ``app/jobs.py`` để giữ mỗi file dưới 400 dòng. Ở đây là phần "lấy đủ
nguyên liệu và biết rõ mình đang xử lý cái gì"; phần render nằm lại ``app/jobs``.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from app.clips import select_sources
from app.config import Settings
from app.drive import DriveFileMeta, download_file, parse_drive_id
from app.drive_folder import list_folder_videos, parse_folder_id
from app.ffmpeg_runner import MUSIC_STEM, ProbeResult, RenderInputs, probe
from app.job_store import Job
from app.merge import merge_sources, needs_merge
from app.models import JobStatus
from app.subtitles import normalize_subtitle
from app.utils import (
    MB,
    InsufficientTmpSpace,
    InternalError,
    InvalidOptions,
    free_space_mb,
)

__all__ = ["prepare_inputs", "probe_stage", "check_tmp_space", "TMP_SPACE_FACTOR"]

# Ước lượng chỗ cần cho output: input + nhạc, nhân 2.5 vì /tmp là RAM và ffmpeg
# còn cần buffer. Thiếu -> báo INSUFFICIENT_TMP_SPACE trước khi bắt đầu render.
TMP_SPACE_FACTOR = 2.5


async def _download_videos(
    job: Job, settings: Settings, log: logging.LoggerAdapter
) -> list[Path]:
    """Danh sách video nguồn đã nằm trong workspace, ĐÚNG thứ tự người dùng gửi.

    Thứ tự này chính là "số hiệu video" mà ``options.clips[].source`` trỏ tới:
    file upload trước (theo thứ tự upload), rồi tới link Drive.
    """
    sources = job.sources
    paths = list(sources.video_paths)
    if not paths and sources.video_path is not None:
        paths = [sources.video_path]
    # Nhãn của phần upload do intake.save_sources ghi sẵn; dựng lại cho đủ và
    # đúng thứ tự với paths, rồi bên dưới nối thêm nhãn của từng file Drive.
    labels = list(sources.video_labels[: len(paths)])
    labels += [path.name for path in paths[len(labels) :]]
    urls = list(sources.video_urls)
    if not urls and sources.video_url:
        urls = [sources.video_url]
    if not paths and not urls:
        raise InternalError("Job không có file video")

    total = len(paths) + len(urls)
    for offset, url in enumerate(urls):
        position = len(paths) + 1
        job.stage_message = (
            "Đang tải video từ Google Drive"
            if total == 1
            else f"Đang tải video {position}/{total} từ Google Drive"
        )
        # Tên file cố định: ffmpeg nhận dạng định dạng theo nội dung, không theo
        # đuôi file, nên không cần đoán ext từ tên trên Drive.
        dest = job.workspace / ("input.mp4" if total == 1 else f"src{position}.mp4")

        def _on_video_progress(percent: float, index: int = offset) -> None:
            # Nhiều video: chia đều thanh tiến độ cho từng file.
            job.progress = round((index * 100 + percent) / len(urls), 1)

        meta = await download_file(
            parse_drive_id(url), dest, _on_video_progress, max_bytes=settings.max_download_bytes
        )
        log.info(
            "Tải video xong",
            extra={
                "size_bytes": meta.size_bytes,
                "video_index": position,
                "drive_name": meta.name,
            },
        )
        paths.append(dest)
        labels.append(meta.name or dest.name)
    sources.video_labels = labels
    return paths


async def _download_folder(
    job: Job, settings: Settings, log: logging.LoggerAdapter
) -> list[Path]:
    """Quét thư mục Drive rồi tải về ĐÚNG những video kịch bản dùng tới.

    Thư mục 20 video mà kịch bản chỉ dùng 4 thì tải cả 20 là phí thời gian và
    phí RAM (``/tmp`` trên Cloud Run là tmpfs). Đối chiếu tên file ngay trên
    metadata của Drive, trước khi tải một byte nào — nhờ vậy sai tên trong kịch
    bản cũng lộ ra ngay lập tức thay vì sau khi đã tải xong vài GB.

    Sau bước này ``options.clips`` được quy hết về TÊN FILE: bỏ bớt nguồn không
    dùng thì số hiệu cũ không còn trỏ đúng chỗ nữa.
    """
    sources = job.sources
    folder_id = parse_folder_id(sources.video_folder_url or "")
    job.stage_message = "Đang quét thư mục Google Drive"
    available = await list_folder_videos(folder_id)
    log.info(
        "Thư mục Drive",
        extra={"drive_folder_id": folder_id, "video_count": len(available)},
    )

    labels = [meta.name for meta in available]
    used, rewritten = select_sources(job.options.clips, labels)
    if rewritten:
        job.options.clips = rewritten
    _check_folder_budget([available[index] for index in used], settings, len(available))

    paths: list[Path] = []
    picked: list[str] = []
    for order, index in enumerate(used, start=1):
        meta = available[index]
        job.stage_message = f"Đang tải video {order}/{len(used)}: {meta.name}"
        dest = job.workspace / f"src{order}.mp4"

        def _on_progress(percent: float, done: int = order - 1) -> None:
            job.progress = round((done * 100 + percent) / len(used), 1)

        await download_file(
            meta.file_id, dest, _on_progress, max_bytes=settings.max_download_bytes
        )
        log.info(
            "Tải video xong",
            extra={
                "size_bytes": meta.size_bytes,
                "video_index": order,
                "drive_name": meta.name,
            },
        )
        paths.append(dest)
        picked.append(meta.name)

    sources.video_labels = picked
    return paths


def _check_folder_budget(
    needed: list[DriveFileMeta], settings: Settings, total_in_folder: int
) -> None:
    """Chặn trước khi tải: quá nhiều video hoặc quá nặng thì /tmp không chứa nổi."""
    if len(needed) > settings.max_folder_videos:
        raise InvalidOptions(
            f"Kịch bản dùng tới {len(needed)} video, quá giới hạn "
            f"{settings.max_folder_videos} video mỗi lần ghép",
            detail=f"Thư mục có {total_in_folder} video. Chỉnh MAX_FOLDER_VIDEOS nếu cần.",
        )
    total_bytes = sum(meta.size_bytes for meta in needed)
    if total_bytes > settings.max_download_bytes:
        raise InvalidOptions(
            f"Tổng dung lượng video cần tải là {total_bytes / MB:.0f} MB, "
            f"quá giới hạn {settings.max_download_bytes / MB:.0f} MB",
            detail="Bớt số đoạn, hoặc nâng MAX_DOWNLOAD_MB.",
        )


async def prepare_inputs(job: Job, settings: Settings, log: logging.LoggerAdapter) -> RenderInputs:
    """Tải file Drive (nếu có), ghép clip, chuẩn hoá .srt. Trả tên file tương đối."""
    job.status = JobStatus.DOWNLOADING
    sources = job.sources
    if sources.video_folder_url:
        videos = await _download_folder(job, settings, log)
    else:
        videos = await _download_videos(job, settings, log)

    # Chỉ tải khi thật sự dùng: /tmp là RAM, tải file rồi bỏ là lãng phí.
    if sources.srt_url and sources.srt_path is None and job.options.subtitle.enabled:
        job.stage_message = "Đang tải phụ đề từ Google Drive"
        dest = job.workspace / "subs_raw.srt"
        await download_file(
            parse_drive_id(sources.srt_url), dest, max_bytes=settings.max_download_bytes
        )
        sources.srt_path = dest

    if sources.music_url and sources.music_path is None and job.options.music.enabled:
        job.stage_message = "Đang tải nhạc từ Google Drive"
        dest = job.workspace / f"{MUSIC_STEM}.mp3"
        await download_file(
            parse_drive_id(sources.music_url), dest, max_bytes=settings.max_download_bytes
        )
        sources.music_path = dest

    # Ghép clip TRƯỚC khi render: từ đây trở đi pipeline chỉ còn một file video,
    # nên mọi bước sau (probe, tính cỡ chữ, render) không cần biết đã có ghép
    # hay không. merge_sources tự đặt status = MERGING và xoá file nguồn sau khi
    # xong; bước probe ngay sau đây sẽ đổi status tiếp.
    sources.video_paths = videos
    if needs_merge(job, videos):
        sources.video_path = await merge_sources(job, settings, videos, log)
    else:
        sources.video_path = videos[0]

    subs_name: str | None = None
    if sources.srt_path is not None and job.options.subtitle.enabled:
        # Trả về tên thật đã ghi: subs.srt hoặc subs.ass (SPEC §3.1 nhận cả hai).
        subs_name = await asyncio.to_thread(
            normalize_subtitle,
            sources.srt_path,
            job.workspace,
            job.options.subtitle.offset_seconds,
        )
        log.info("Chuẩn hoá phụ đề xong", extra={"subs_file": subs_name})

    music_name = sources.music_path.name if sources.music_path is not None else None
    return RenderInputs(
        video=sources.video_path.name,
        music=music_name,
        subs=subs_name,
    )


async def probe_stage(job: Job, log: logging.LoggerAdapter) -> ProbeResult:
    job.status = JobStatus.PROBING
    job.stage_message = "Đang đọc thông tin video"
    if job.sources.video_path is None:
        raise InternalError("Job không có file video để probe")
    result = await probe(job.sources.video_path)
    log.info(
        "Probe xong",
        extra={
            "duration": result.duration,
            "has_audio": result.has_audio,
            "video_codec": result.video_codec,
            "resolution": f"{result.width}x{result.height}",
        },
    )
    return result


async def check_tmp_space(job: Job, settings: Settings, log: logging.LoggerAdapter) -> None:
    """/tmp trên Cloud Run là tmpfs (RAM) nên phải chắc còn chỗ trước khi render."""
    def _measure() -> tuple[int, float]:
        # stat() + disk_usage() là syscall blocking -> gom vào một lần to_thread.
        total = 0
        for path in (job.sources.video_path, job.sources.music_path):
            if path is None:
                continue
            try:
                total += path.stat().st_size
            except OSError:
                continue
        return total, free_space_mb(job.workspace)

    sizes, free_mb = await asyncio.to_thread(_measure)
    need_mb = sizes / MB * TMP_SPACE_FACTOR
    if free_mb < need_mb:
        raise InsufficientTmpSpace(
            f"/tmp còn {free_mb:.0f} MB, cần khoảng {need_mb:.0f} MB"
        )
    log.debug("Kiểm tra dung lượng /tmp", extra={"free_mb": free_mb, "need_mb": need_mb})
