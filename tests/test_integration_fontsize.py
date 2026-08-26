"""Test tích hợp riêng cho cỡ chữ phụ đề: đo trên frame ffmpeg thật.

Tách khỏi tests/test_integration.py để mỗi file test dưới 400 dòng.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest

from app.config import Settings
from app.ffmpeg_runner import probe
from app.job_store import Job, JobSources, JobStore
from app.jobs import run_job
from app.models import RenderOptions, SubtitleOptions
from app.subtitles import build_force_style

pytestmark = [
    pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="cần ffmpeg trong PATH"),
    pytest.mark.skipif(shutil.which("ffprobe") is None, reason="cần ffprobe trong PATH"),
]


async def _run(*args: str, cwd: Path | None = None) -> None:
    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(cwd) if cwd else None,
    )
    _out, err = await process.communicate()
    assert process.returncode == 0, err.decode("utf-8", "replace")[-2000:]


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        API_KEY="k",
        WORK_DIR=str(tmp_path / "jobs"),
        FONTS_DIR=str(Path("fonts").resolve()),
        _env_file=None,  # type: ignore[call-arg]
    )


# --------------------------------------------------------------------------- #
# Cỡ chữ tự động: đo trên frame thật do ffmpeg render
# --------------------------------------------------------------------------- #
async def _text_bbox(frame: Path, tmp_path: Path) -> tuple[int, int]:
    """(bề ngang, chiều cao) vùng chữ trắng trên frame nền xám."""
    raw = tmp_path / "gray.rawvideo"
    process = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(frame), "-pix_fmt", "gray", "-f", "rawvideo", str(raw),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    await process.communicate()
    probe_result = await probe(frame)
    data = raw.read_bytes()
    width, height = probe_result.width, probe_result.height
    cols: list[int] = []
    rows: list[int] = []
    for y in range(height):
        row = data[y * width : (y + 1) * width]
        hits = [x for x, value in enumerate(row) if value > 200]
        if hits:
            rows.append(y)
            cols.extend(hits)
    if not cols:
        return 0, 0
    return max(cols) - min(cols) + 1, max(rows) - min(rows) + 1


@pytest.mark.parametrize(("width", "height"), [(1280, 720), (720, 1280)])
async def test_auto_font_size_keeps_text_within_frame(
    tmp_path: Path, width: int, height: int
) -> None:
    """Dù ngang hay dọc, một dòng phụ đề phải nằm gọn trong khung.

    Bug cũ (FontSize=24 cố định) làm video dọc bị chữ tràn phải xuống dòng —
    test này đo trực tiếp bề ngang vùng chữ trên frame ffmpeg xuất ra.
    """
    srt = tmp_path / "subs.srt"
    srt.write_text(
        "1\n00:00:00,000 --> 00:00:05,000\nXin chào các bạn, đây là phụ đề mẫu\n",
        encoding="utf-8",
    )
    style = build_force_style(SubtitleOptions(), width, height)
    frame = tmp_path / f"f_{width}x{height}.png"
    await _run(
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"color=c=0x404040:s={width}x{height}:d=2",
        "-vf", f"subtitles=subs.srt:force_style='{style}'",
        "-frames:v", "1", str(frame),
        cwd=tmp_path,
    )
    text_width, text_height = await _text_bbox(frame, tmp_path)
    assert text_width > 0, "không thấy chữ nào được vẽ"
    # Chiếm 40–85% bề ngang: đọc được mà không tràn.
    assert 0.40 <= text_width / width <= 0.85, f"chữ rộng {text_width}/{width}"
    # Một dòng duy nhất: cao chưa tới 12% bề ngang (nếu wrap 2+ dòng sẽ vượt).
    assert text_height / width < 0.12, f"chữ bị xuống dòng, cao {text_height}"


async def test_old_fixed_font_size_wrapped_vertical_video(tmp_path: Path) -> None:
    """Chốt lại bug cũ: cỡ chữ cố định trên video dọc bị wrap nhiều dòng.

    Bug gốc là ``FontSize=24`` theo ĐƠN VỊ ASS: libass nhân nó với
    ``chiều_cao/288`` nên trên khung 720x1280 ra chữ cao ~107px, tràn thành ba
    dòng. Nay ``font_size`` khai bằng pixel nên phải viết thẳng 107 để dựng lại
    đúng tình huống cũ.
    """
    srt = tmp_path / "subs.srt"
    srt.write_text(
        "1\n00:00:00,000 --> 00:00:05,000\nXin chào các bạn, đây là phụ đề mẫu\n",
        encoding="utf-8",
    )
    frame_old = tmp_path / "old.png"
    frame_new = tmp_path / "new.png"
    for frame, opts in (
        (frame_old, SubtitleOptions(font_size=round(24 * 1280 / 288), outline=9)),
        (frame_new, SubtitleOptions()),
    ):
        await _run(
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "color=c=0x404040:s=720x1280:d=2",
            "-vf", f"subtitles=subs.srt:force_style='{build_force_style(opts, 720, 1280)}'",
            "-frames:v", "1", str(frame),
            cwd=tmp_path,
        )
    _w_old, h_old = await _text_bbox(frame_old, tmp_path)
    _w_new, h_new = await _text_bbox(frame_new, tmp_path)
    # Cỡ cũ cao gấp nhiều lần vì phải wrap thành 3 dòng chữ khổng lồ.
    assert h_old > h_new * 3, f"cũ {h_old}px vs mới {h_new}px"


async def test_rotated_phone_video_gets_correct_font_size(
    settings: Settings, tmp_path: Path
) -> None:
    """Video xoay 90° (kiểu điện thoại): chữ phải vừa khung DỌC thật.

    ffprobe báo kích thước coded ngang (1280x720) nhưng ffmpeg tự áp Display
    Matrix nên frame thật là 720x1280. Nếu tính cỡ chữ theo số ffprobe trả về
    thì chữ to gấp ~1.8 lần và bị wrap.
    """
    # Nền xám phẳng, KHÔNG dùng testsrc: hoa văn testsrc có vùng trắng nên
    # _text_bbox sẽ đo lẫn cả hoa văn thay vì chỉ chữ phụ đề trắng.
    landscape = tmp_path / "landscape.mp4"
    await _run(
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "color=c=0x404040:s=320x240:d=3:r=25",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(landscape),
    )
    rotated = tmp_path / "rotated.mp4"
    await _run(
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-display_rotation", "90", "-i", str(landscape), "-c", "copy", str(rotated),
    )

    probe_result = await probe(rotated)
    # ffprobe raw là 320x240; sau khi tính rotation phải thành 240x320.
    assert (probe_result.width, probe_result.height) == (240, 320)
    assert probe_result.rotation == 90

    workspace = settings.work_dir / "rotjob"
    workspace.mkdir(parents=True, exist_ok=True)
    video = workspace / "input.mp4"
    video.write_bytes(rotated.read_bytes())
    raw = workspace / "subs_raw.srt"
    raw.write_text(
        "1\n00:00:00,000 --> 00:00:03,000\nXin chào các bạn, phụ đề mẫu\n", encoding="utf-8"
    )

    store = JobStore()
    job = await store.create(
        Job(
            id="rotjob",
            workspace=workspace,
            options=RenderOptions(),
            sources=JobSources(video_path=video, srt_path=raw),
        )
    )
    await run_job("rotjob", store, settings)

    assert job.error is None, job.error
    out = await probe(job.output_path)
    # Output là khung dọc thật
    assert (out.width, out.height) == (240, 320)

    frame = tmp_path / "rot_frame.png"
    await _run(
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-ss", "1", "-i", str(job.output_path), "-frames:v", "1", str(frame),
    )
    text_width, text_height = await _text_bbox(frame, tmp_path)
    assert text_width > 0, "không thấy chữ"
    # Vừa trong khung 240px, không tràn sát biên.
    assert text_width / 240 <= 0.92, f"chữ rộng {text_width}/240"
