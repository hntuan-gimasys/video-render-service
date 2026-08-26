"""Chạy FFmpeg/ffprobe bất đồng bộ và parse progress — docs/SPEC.md §5.2, §5.4.

Phần dựng argv (hàm thuần) nằm ở ``app.ffmpeg_cmd`` và được re-export ở đây để
hợp đồng trong stub vẫn đúng: ``from app.ffmpeg_runner import build_ffmpeg_command``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from app.ffmpeg_cmd import (
    INPUT_STEM,
    MUSIC_STEM,
    SUBS_ASS_NAME,
    SUBS_NAME,
    SUBS_SRT_NAME,
    ProbeResult,
    RenderInputs,
    build_ffmpeg_command,
    can_copy_video,
    parse_probe_json,
    resolve_canvas_size,
)
from app.utils import FfmpegFailed, ProbeFailed

if TYPE_CHECKING:  # pragma: no cover
    from collections import deque

logger = logging.getLogger(__name__)

__all__ = [
    "INPUT_STEM",
    "MUSIC_STEM",
    "SUBS_NAME",
    "SUBS_ASS_NAME",
    "SUBS_SRT_NAME",
    "ProbeResult",
    "RenderInputs",
    "build_ffmpeg_command",
    "can_copy_video",
    "parse_probe_json",
    "resolve_canvas_size",
    "parse_progress_line",
    "probe",
    "run_ffmpeg",
]


async def probe(path: Path) -> ProbeResult:
    """Chạy ffprobe và trả về metadata. Raise :class:`ProbeFailed` nếu lỗi."""
    cmd = [
        "ffprobe",
        "-v",
        "quiet",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (OSError, FileNotFoundError) as exc:
        raise ProbeFailed(f"Không chạy được ffprobe: {exc}") from exc

    stdout, stderr = await process.communicate()
    if process.returncode != 0:
        raise ProbeFailed(
            f"ffprobe thất bại (exit {process.returncode})",
            detail=stderr.decode("utf-8", "replace")[-2000:],
        )
    try:
        payload = json.loads(stdout.decode("utf-8", "replace") or "{}")
    except json.JSONDecodeError as exc:
        raise ProbeFailed(f"ffprobe trả JSON không hợp lệ: {exc}") from exc

    result = parse_probe_json(payload)
    if not result.has_video:
        raise ProbeFailed("File không có stream video")
    return result



def parse_progress_line(line: str) -> dict[str, str]:
    """'out_time_ms=1234' -> {'out_time_ms': '1234'}. Dòng lạ -> {}."""
    stripped = line.strip()
    if not stripped or "=" not in stripped:
        return {}
    key, _, value = stripped.partition("=")
    key = key.strip()
    if not key:
        return {}
    return {key: value.strip()}


async def run_ffmpeg(
    cmd: list[str],
    total_duration: float,
    on_progress: Callable[[float, float], None] | None = None,
    stderr_buf: deque[str] | None = None,
    *,
    cwd: Path | None = None,
    on_start: Callable[[asyncio.subprocess.Process], None] | None = None,
) -> int:
    """Chạy ffmpeg, đọc song song stdout (progress) và stderr (log lỗi).

    ``on_progress(percent, out_time_seconds)`` bị throttle còn 1 lần/giây.
    Trả về exit code; không raise khi ffmpeg trả mã lỗi (nơi gọi tự quyết định).
    """
    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
            cwd=str(cwd) if cwd else None,
        )
    except (OSError, FileNotFoundError) as exc:
        raise FfmpegFailed(f"Không chạy được ffmpeg: {exc}") from exc

    if on_start is not None:
        on_start(process)

    try:
        await asyncio.gather(
            _pump_progress(process.stdout, total_duration, on_progress),
            _pump_stderr(process.stderr, stderr_buf),
        )
    except BaseException:
        # Pump lỗi (vd. readline gặp dòng > 64 KiB) hoặc task bị cancel giữa
        # đường: không được bỏ rơi tiến trình ffmpeg, nếu không nó sống tiếp và
        # ăn CPU/RAM của instance duy nhất cho tới khi container chết.
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
        with contextlib.suppress(Exception):
            await process.wait()
        raise
    return await process.wait()


async def _pump_progress(
    stream: asyncio.StreamReader | None,
    total_duration: float,
    on_progress: Callable[[float, float], None] | None,
) -> None:
    if stream is None:
        return
    loop = asyncio.get_running_loop()
    last_emit = 0.0
    last_seconds = 0.0
    while True:
        raw = await stream.readline()
        if not raw:
            break
        fields = parse_progress_line(raw.decode("utf-8", "replace"))
        if not fields:
            continue
        # out_time_ms mang tên "ms" nhưng đơn vị thực tế là MICROsecond (§5.4).
        micros = fields.get("out_time_ms") or fields.get("out_time_us")
        finished = fields.get("progress") == "end"
        if micros is None and not finished:
            continue
        if micros is not None:
            try:
                # ffmpeg có thể ghi out_time_ms=N/A ở frame đầu.
                last_seconds = max(0.0, int(micros) / 1_000_000)
            except ValueError:
                continue
        # Dòng "progress=end" không kèm out_time -> giữ mốc cuối, đừng reset về 0.
        percent = min(99.0, last_seconds / total_duration * 100) if total_duration > 0 else 0.0
        now = loop.time()
        if on_progress is not None and (finished or now - last_emit >= 1.0):
            last_emit = now
            on_progress(percent, last_seconds)


async def _pump_stderr(stream: asyncio.StreamReader | None, buf: deque[str] | None) -> None:
    if stream is None:
        return
    while True:
        raw = await stream.readline()
        if not raw:
            break
        line = raw.decode("utf-8", "replace").rstrip()
        if not line:
            continue
        if buf is not None:
            buf.append(line)
        logger.debug("ffmpeg: %s", line)
