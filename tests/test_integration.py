"""Test tích hợp: chạy ffmpeg thật từ đầu tới cuối.

Bị skip khi máy không có ffmpeg/ffprobe trong PATH (ví dụ CI tối giản hoặc máy
dev Windows). Trong image Docker thì luôn chạy được.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest

from app.config import Settings
from app.ffmpeg_cmd import RenderInputs, build_ffmpeg_command
from app.ffmpeg_runner import probe, run_ffmpeg
from app.job_store import Job, JobSources, JobStore
from app.jobs import run_job
from app.models import JobStatus, RenderOptions
from app.subtitles import normalize_srt

pytestmark = [
    pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="cần ffmpeg trong PATH"),
    pytest.mark.skipif(shutil.which("ffprobe") is None, reason="cần ffprobe trong PATH"),
]

SRT = (
    "1\n"
    "00:00:00,500 --> 00:00:02,000\n"
    "Dòng phụ đề thứ nhất\n"
    "\n"
    "2\n"
    "00:00:02,500 --> 00:00:04,500\n"
    "Dòng phụ đề thứ hai\n"
)


async def _run(*args: str, cwd: Path | None = None) -> None:
    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(cwd) if cwd else None,
    )
    _out, err = await process.communicate()
    assert process.returncode == 0, err.decode("utf-8", "replace")[-2000:]


async def _make_video(path: Path, *, with_audio: bool = True, duration: int = 5) -> None:
    args = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"testsrc=duration={duration}:size=320x240:rate=25",
    ]
    if with_audio:
        args += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}"]
    args += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-t", str(duration)]
    if with_audio:
        args += ["-c:a", "aac", "-shortest"]
    args.append(str(path))
    await _run(*args)


async def _make_music(path: Path, duration: int = 3) -> None:
    await _run(
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency=220:duration={duration}",
        str(path),
    )


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        API_KEY="k",
        WORK_DIR=str(tmp_path / "jobs"),
        FONTS_DIR=str(Path("fonts").resolve()),
        _env_file=None,  # type: ignore[call-arg]
    )


async def _make_job(
    settings: Settings,
    *,
    options: RenderOptions | None = None,
    with_audio: bool = True,
    srt: bool = False,
    music: bool = False,
) -> Job:
    workspace = settings.work_dir / "itjob"
    workspace.mkdir(parents=True, exist_ok=True)
    video = workspace / "input.mp4"
    await _make_video(video, with_audio=with_audio)
    sources = JobSources(video_path=video)
    if srt:
        raw = workspace / "subs_raw.srt"
        raw.write_bytes(("﻿" + SRT.replace("\n", "\r\n")).encode("utf-8"))
        sources.srt_path = raw
    if music:
        music_path = workspace / "music.mp3"
        await _make_music(music_path)
        sources.music_path = music_path
    return Job(
        id="itjob",
        workspace=workspace,
        options=options or RenderOptions(),
        sources=sources,
    )


# --------------------------------------------------------------------------- #
# Pipeline thật, đủ ba thành phần
# --------------------------------------------------------------------------- #
async def test_full_pipeline_burns_subs_and_mixes_music(settings: Settings) -> None:
    store = JobStore()
    job = await store.create(await _make_job(settings, srt=True, music=True))

    await run_job("itjob", store, settings)

    assert job.error is None, job.error
    assert job.status is JobStatus.SUCCEEDED
    assert job.progress == 100.0
    output = job.output_path
    assert output.exists() and output.stat().st_size > 0

    result = await probe(output)
    assert result.duration == pytest.approx(5.0, abs=0.5)
    assert result.has_video and result.has_audio
    assert (result.width, result.height) == (320, 240)
    assert job.output is not None
    assert job.output.duration_seconds == pytest.approx(5.0, abs=0.5)
    # File input phải bị dọn, chỉ còn output (SPEC §8)
    assert not (job.workspace / "input.mp4").exists()
    assert not (job.workspace / "subs.srt").exists()


async def test_video_without_audio_track_still_mixes_music(settings: Settings) -> None:
    """Bẫy §5.2: không có audio track thì amix chết nếu thiếu anullsrc."""
    store = JobStore()
    job = await store.create(await _make_job(settings, with_audio=False, music=True))

    await run_job("itjob", store, settings)

    assert job.error is None, job.error
    assert job.status is JobStatus.SUCCEEDED
    result = await probe(job.output_path)
    assert result.has_audio  # nhạc nền đã được ghép vào video vốn không có tiếng
    assert result.duration == pytest.approx(5.0, abs=0.5)


async def test_fast_path_copy_produces_playable_file(settings: Settings) -> None:
    store = JobStore()
    job = await store.create(
        await _make_job(
            settings,
            options=RenderOptions.model_validate({"subtitle": {"enabled": False}}),
            music=True,
        )
    )

    await run_job("itjob", store, settings)

    assert job.error is None, job.error
    result = await probe(job.output_path)
    assert result.video_codec == "h264"  # -c:v copy giữ nguyên codec
    assert result.has_audio


async def test_resolution_and_soft_subs(settings: Settings) -> None:
    store = JobStore()
    job = await store.create(
        await _make_job(
            settings,
            options=RenderOptions.model_validate(
                {"subtitle": {"mode": "soft"}, "output": {"resolution": "160x120"}}
            ),
            srt=True,
        )
    )

    await run_job("itjob", store, settings)

    assert job.error is None, job.error
    result = await probe(job.output_path)
    assert (result.width, result.height) == (160, 120)


# --------------------------------------------------------------------------- #
# run_ffmpeg + progress trên ffmpeg thật
# --------------------------------------------------------------------------- #
async def test_run_ffmpeg_reports_progress_in_seconds(tmp_path: Path) -> None:
    source = tmp_path / "input.mp4"
    await _make_video(source, duration=5)

    probe_result = await probe(source)
    cmd = build_ffmpeg_command(
        RenderInputs(video="input.mp4"),
        probe_result,
        RenderOptions.model_validate(
            {"subtitle": {"enabled": False}, "output": {"copy_video_if_possible": False}}
        ),
        tmp_path,
    )
    seen: list[tuple[float, float]] = []
    exit_code = await run_ffmpeg(
        cmd,
        total_duration=probe_result.duration,
        on_progress=lambda pct, secs: seen.append((pct, secs)),
        cwd=tmp_path,
    )

    assert exit_code == 0
    assert seen, "phải có ít nhất một lần cập nhật progress"
    # out_time_ms là microsecond: giây cuối cùng phải xấp xỉ 5, không phải 5000
    assert seen[-1][1] == pytest.approx(5.0, abs=1.0)
    assert 0 < seen[-1][0] <= 99.0


async def test_normalize_srt_output_is_readable_by_libass(tmp_path: Path) -> None:
    """Phụ đề CRLF + BOM sau khi chuẩn hoá phải burn được thật."""
    source = tmp_path / "input.mp4"
    await _make_video(source, duration=2)
    raw = tmp_path / "raw.srt"
    raw.write_bytes(("﻿" + SRT.replace("\n", "\r\n")).encode("utf-8"))
    normalize_srt(raw, tmp_path / "subs.srt")

    probe_result = await probe(source)
    cmd = build_ffmpeg_command(
        RenderInputs(video="input.mp4", subs="subs.srt"),
        probe_result,
        RenderOptions(),
        tmp_path,
        fonts_dir=str(Path("fonts").resolve()),
    )
    from collections import deque

    stderr: deque[str] = deque(maxlen=200)
    exit_code = await run_ffmpeg(
        cmd, total_duration=probe_result.duration, stderr_buf=stderr, cwd=tmp_path
    )
    assert exit_code == 0, "\n".join(stderr)
    assert (tmp_path / "output.mp4").exists()


# --------------------------------------------------------------------------- #
# Hồi quy: -shortest + soft-sub từng cắt mất phần cuối video
# --------------------------------------------------------------------------- #
async def test_soft_subs_plus_music_keeps_full_duration(settings: Settings) -> None:
    """Phụ đề kết thúc ở 2s nhưng video 5s -> output phải vẫn đủ 5s.

    Trước khi sửa, lệnh dùng -shortest và ffmpeg cắt output theo stream phụ đề
    mov_text: video 5s ra file 2s. Test này chạy ffmpeg thật nên sẽ đỏ ngay nếu
    -shortest bị thêm lại.
    """
    store = JobStore()
    workspace = settings.work_dir / "itjob"
    workspace.mkdir(parents=True, exist_ok=True)
    video = workspace / "input.mp4"
    await _make_video(video, with_audio=True, duration=5)
    music_path = workspace / "music.mp3"
    await _make_music(music_path, duration=3)
    short_srt = workspace / "subs_raw.srt"
    short_srt.write_text(
        "1\n00:00:00,500 --> 00:00:02,000\nket thuc som\n", encoding="utf-8"
    )

    job = await store.create(
        Job(
            id="itjob",
            workspace=workspace,
            options=RenderOptions.model_validate({"subtitle": {"mode": "soft"}}),
            sources=JobSources(
                video_path=video, music_path=music_path, srt_path=short_srt
            ),
        )
    )

    await run_job("itjob", store, settings)

    assert job.error is None, job.error
    result = await probe(job.output_path)
    assert result.duration == pytest.approx(5.0, abs=0.4), (
        f"output bị cắt còn {result.duration}s — -shortest đã quay lại?"
    )


async def test_ass_subtitle_is_burned(settings: Settings) -> None:
    """SPEC §3.1 nhận cả .ass — libass đọc trực tiếp, không được ép về SRT."""
    store = JobStore()
    workspace = settings.work_dir / "itjob"
    workspace.mkdir(parents=True, exist_ok=True)
    video = workspace / "input.mp4"
    await _make_video(video, with_audio=True, duration=3)
    raw = workspace / "subs_raw.ass"
    raw.write_text(
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, Alignment, MarginV, Encoding\n"
        "Style: Default,Arial,28,&H00FFFFFF,2,40,1\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        "Dialogue: 0,0:00:00.50,0:00:02.00,Default,,0,0,0,,Xin chào từ ASS\n",
        encoding="utf-8",
    )

    job = await store.create(
        Job(
            id="itjob",
            workspace=workspace,
            options=RenderOptions(),
            sources=JobSources(video_path=video, srt_path=raw),
        )
    )

    await run_job("itjob", store, settings)

    assert job.error is None, job.error
    assert job.status is JobStatus.SUCCEEDED
    assert (await probe(job.output_path)).has_video
