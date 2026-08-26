"""Test tích hợp bằng ffmpeg thật cho ghép clip và nhạc nền.

Tách khỏi tests/test_integration_overlay.py để mỗi file test dưới 400 dòng.

Mọi kiểm chứng ở đây đo trên FILE THẬT do ffmpeg dựng ra — độ dài, kích thước
khung, viền đen, âm lượng — chứ không chỉ so chuỗi argv: nhầm một label filter
là kết quả sai hẳn mà argv vẫn trông đúng.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest

from app.clips import (
    SourceVideo,
    build_concat_command,
    parse_clip_lines,
    resolve_clips,
    resolve_concat_canvas,
)
from app.ffmpeg_runner import RenderInputs, build_ffmpeg_command, probe
from app.models import RenderOptions

pytestmark = [
    pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="cần ffmpeg trong PATH"),
    pytest.mark.skipif(shutil.which("ffprobe") is None, reason="cần ffprobe trong PATH"),
]


async def run_ok(*args: str, cwd: Path | None = None) -> None:
    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(cwd) if cwd else None,
    )
    _out, err = await process.communicate()
    assert process.returncode == 0, err.decode("utf-8", "replace")[-3000:]


async def gray_pixels(frame: Path, width: int, height: int, tmp_path: Path) -> bytes:
    """Frame về dạng thang xám thô, mỗi byte là một pixel."""
    raw = tmp_path / f"{frame.stem}.gray"
    await run_ok(
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(frame), "-pix_fmt", "gray", "-f", "rawvideo", str(raw),
    )
    data = raw.read_bytes()
    assert len(data) >= width * height
    return data[: width * height]


def bright_bbox(
    data: bytes, width: int, height: int, threshold: int = 170
) -> tuple[int, int, int, int] | None:
    """(x_min, y_min, x_max, y_max) của vùng sáng, None nếu không có gì."""
    xs: list[int] = []
    ys: list[int] = []
    for y in range(height):
        row = data[y * width : (y + 1) * width]
        hits = [x for x, value in enumerate(row) if value > threshold]
        if hits:
            ys.append(y)
            xs.extend(hits)
    if not xs:
        return None
    return min(xs), min(ys), max(xs), max(ys)


# --------------------------------------------------------------------------- #
# Ghép clip
# --------------------------------------------------------------------------- #
async def make_source(
    tmp_path: Path, name: str, *, size: str, rate: int, seconds: int, audio: bool
) -> Path:
    path = tmp_path / name
    args = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"testsrc2=size={size}:rate={rate}:duration={seconds}",
    ]
    if audio:
        args += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}"]
    args += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
    args += ["-c:a", "aac", "-shortest"] if audio else ["-an"]
    args.append(str(path))
    await run_ok(*args)
    return path


async def test_concat_produces_exact_total_duration_and_canvas(tmp_path: Path) -> None:
    await make_source(tmp_path, "src1.mp4", size="720x1280", rate=30, seconds=6, audio=True)
    await make_source(tmp_path, "src2.mp4", size="1920x1080", rate=30, seconds=6, audio=False)

    sources = [
        SourceVideo("src1.mp4", await probe(tmp_path / "src1.mp4")),
        SourceVideo("src2.mp4", await probe(tmp_path / "src2.mp4")),
    ]
    clips = resolve_clips(parse_clip_lines("1 0-2; 2 1-4; 1 4-5"), sources)
    width, height, fps = resolve_concat_canvas(clips, sources, RenderOptions())
    # Tắt transition: test này kiểm CẮT CỨNG (không chồng hình) — phần crossfade
    # có bộ test riêng ở tests/test_transitions.py.
    no_transition = RenderOptions.model_validate({"transition": {"enabled": False}})
    await run_ok(
        *build_concat_command(clips, width, height, fps, no_transition), cwd=tmp_path
    )

    merged = await probe(tmp_path / "merged.mp4")
    assert merged.duration == pytest.approx(6.0, abs=0.2)  # 2 + 3 + 1
    # Khung lấy theo video của ĐOẠN ĐẦU TIÊN (src1, dọc).
    assert (merged.width, merged.height) == (720, 1280)
    # Đoạn giữa không có tiếng vẫn phải ra file có audio track liền mạch.
    assert merged.has_audio


async def test_concat_letterboxes_landscape_clip_without_stretching(
    tmp_path: Path,
) -> None:
    """Đoạn ngang ghép vào khung dọc phải được chèn viền, KHÔNG bị kéo giãn.

    Đo bằng chính viền đen: ảnh 16:9 thu vừa bề ngang 720 chỉ cao 405px, phần
    còn lại của khung 1280 phải là viền đen.
    """
    await make_source(tmp_path, "src1.mp4", size="720x1280", rate=30, seconds=4, audio=True)
    await make_source(tmp_path, "src2.mp4", size="1920x1080", rate=30, seconds=4, audio=True)
    sources = [
        SourceVideo("src1.mp4", await probe(tmp_path / "src1.mp4")),
        SourceVideo("src2.mp4", await probe(tmp_path / "src2.mp4")),
    ]
    clips = resolve_clips(parse_clip_lines("1 0-2; 2 0-2"), sources)
    width, height, fps = resolve_concat_canvas(clips, sources, RenderOptions())
    await run_ok(
        *build_concat_command(clips, width, height, fps, RenderOptions()), cwd=tmp_path
    )

    frame = tmp_path / "clip2.png"
    await run_ok(
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-ss", "3", "-i", str(tmp_path / "merged.mp4"), "-frames:v", "1", str(frame),
    )
    data = await gray_pixels(frame, width, height, tmp_path)
    content = bright_bbox(data, width, height, threshold=8)
    assert content is not None
    content_height = content[3] - content[1] + 1
    # 720 / (16/9) = 405px. Kéo giãn cho đầy khung thì con số này sẽ là 1280.
    assert content_height == pytest.approx(405, abs=12), f"cao {content_height}px"

# --------------------------------------------------------------------------- #
# Nhạc nền thay tiếng gốc — đo bằng âm lượng thật của file output
# --------------------------------------------------------------------------- #
async def mean_volume(path: Path) -> float:
    """Âm lượng trung bình (dBFS) do ffmpeg volumedetect đo."""
    process = await asyncio.create_subprocess_exec(
        "ffmpeg", "-hide_banner", "-i", str(path),
        "-af", "volumedetect", "-f", "null", "-",
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    _out, err = await process.communicate()
    for line in err.decode("utf-8", "replace").splitlines():
        if "mean_volume:" in line:
            return float(line.split("mean_volume:")[1].strip().split()[0])
    raise AssertionError("volumedetect không trả về mean_volume")


async def render_with_music(tmp_path: Path, opts: RenderOptions, name: str) -> Path:
    """Video có tiếng SỐNG + nhạc nền IM LẶNG -> output ồn hay im là biết ngay."""
    await run_ok(
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "color=c=black:s=320x240:rate=25:d=3",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-shortest", str(tmp_path / "input.mp4"),
    )
    await run_ok(
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
        "-t", "5", "-c:a", "libmp3lame", str(tmp_path / "music.mp3"),
    )
    probe_result = await probe(tmp_path / "input.mp4")
    cmd = build_ffmpeg_command(
        RenderInputs(video="input.mp4", music="music.mp3"),
        probe_result,
        opts,
        tmp_path,
    )
    cmd[-1] = name
    await run_ok(*cmd, cwd=tmp_path)
    return tmp_path / name


async def test_music_really_removes_the_original_audio(tmp_path: Path) -> None:
    """Mặc định: nhạc nền (ở đây là im lặng) thay hẳn tiếng sine của video.

    Đo trên file thật chứ không chỉ so chuỗi argv — nhầm một label filter là
    tiếng gốc lọt ra mà argv vẫn trông đúng.
    """
    out = await render_with_music(tmp_path, RenderOptions(), "replaced.mp4")
    # Nhạc im lặng -> output phải gần như câm. Còn tiếng sine thì cỡ -10 dB.
    assert await mean_volume(out) < -60, "tiếng gốc vẫn lọt ra output"


async def test_explicit_original_volume_keeps_the_original_audio(
    tmp_path: Path,
) -> None:
    opts = RenderOptions.model_validate({"music": {"original_volume": 1.0}})
    out = await render_with_music(tmp_path, opts, "mixed.mp4")
    assert await mean_volume(out) > -30, "khai rõ original_volume mà vẫn mất tiếng gốc"


# --------------------------------------------------------------------------- #
# Chuyển cảnh (crossfade) — kiểm bằng pixel/âm lượng thật, không chỉ so argv
# --------------------------------------------------------------------------- #
async def make_solid_source(
    tmp_path: Path, name: str, *, color: str, seconds: float, tone_hz: int
) -> Path:
    """Video một màu phẳng + một tần số âm thuần — dễ đo bằng pixel/RMS."""
    path = tmp_path / name
    await run_ok(
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"color=c={color}:size=320x240:rate=30:duration={seconds}",
        "-f", "lavfi", "-i", f"sine=frequency={tone_hz}:duration={seconds}",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-shortest", str(path),
    )
    return path


async def mean_frame_value(video: Path, at: float, tmp_path: Path) -> float:
    """Độ sáng trung bình (kênh xám) của frame tại mốc ``at`` giây."""
    frame = tmp_path / f"frame_{at}.png"
    await run_ok(
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-ss", str(at), "-i", str(video), "-frames:v", "1", str(frame),
    )
    data = await gray_pixels(frame, 320, 240, tmp_path)
    return sum(data) / len(data)


async def test_crossfade_actually_blends_two_frames_not_a_hard_cut(
    tmp_path: Path,
) -> None:
    """Giữa lúc chuyển cảnh, frame phải là MÀU PHA TRỘN chứ không phải một
    trong hai màu gốc — đây chính là khác biệt "mượt" so với cắt cứng."""
    await make_solid_source(tmp_path, "src1.mp4", color="black", seconds=3, tone_hz=300)
    await make_solid_source(tmp_path, "src2.mp4", color="white", seconds=3, tone_hz=300)
    sources = [
        SourceVideo("src1.mp4", await probe(tmp_path / "src1.mp4")),
        SourceVideo("src2.mp4", await probe(tmp_path / "src2.mp4")),
    ]
    clips = resolve_clips([], sources)
    opts = RenderOptions.model_validate({"transition": {"duration": 1.0}})
    await run_ok(*build_concat_command(clips, 320, 240, 30.0, opts), cwd=tmp_path)

    # Transition: offset = 3 - 1 = 2s, kéo dài 1s (tới 3s trong output).
    before = await mean_frame_value(tmp_path / "merged.mp4", 1.0, tmp_path)
    middle = await mean_frame_value(tmp_path / "merged.mp4", 2.5, tmp_path)
    after = await mean_frame_value(tmp_path / "merged.mp4", 4.0, tmp_path)

    assert before < 30, f"frame trước chuyển cảnh phải gần đen, đo được {before:.0f}"
    assert after > 220, f"frame sau chuyển cảnh phải gần trắng, đo được {after:.0f}"
    # Giữa chừng phải là xám (pha trộn) — không phải đen (cắt cứng còn giữ cảnh
    # cũ) cũng không phải trắng (đã nhảy hẳn sang cảnh mới).
    assert 60 < middle < 200, f"frame giữa chuyển cảnh phải là xám pha trộn, đo được {middle:.0f}"


async def test_crossfade_merged_file_has_the_expected_shorter_duration(
    tmp_path: Path,
) -> None:
    await make_solid_source(tmp_path, "src1.mp4", color="red", seconds=3, tone_hz=300)
    await make_solid_source(tmp_path, "src2.mp4", color="blue", seconds=3, tone_hz=300)
    await make_solid_source(tmp_path, "src3.mp4", color="green", seconds=3, tone_hz=300)
    sources = [
        SourceVideo(f"src{i}.mp4", await probe(tmp_path / f"src{i}.mp4")) for i in (1, 2, 3)
    ]
    clips = resolve_clips([], sources)
    opts = RenderOptions.model_validate({"transition": {"duration": 0.8}})
    await run_ok(*build_concat_command(clips, 320, 240, 30.0, opts), cwd=tmp_path)

    merged = await probe(tmp_path / "merged.mp4")
    # 3 doan 3s, 2 lan chuyen canh 0.8s -> 9 - 1.6 = 7.4s.
    assert merged.duration == pytest.approx(7.4, abs=0.15)


async def test_crossfade_survives_sources_with_different_native_frame_rates(
    tmp_path: Path,
) -> None:
    """Ghép các nguồn có FPS GỐC khác nhau (rất hay gặp — video tải từ nhiều
    nền tảng/máy khác nhau) với transition bật vẫn phải ra file bình thường.

    Đây chính là bug thật đã gặp trên container thật (ffmpeg 7.1.5, không tái
    hiện được bằng ffmpeg Windows cục bộ): ``xfade`` từ chối với lỗi "current
    rate of 1/0" nếu filter ``fps=`` không phải bước CUỐI trong chuỗi chuẩn hoá
    mỗi đoạn — xem tests/test_transitions.py cho phần kiểm cấu trúc filter.
    """
    for name, rate, color in (
        ("src1.mp4", "30000/1001", "red"),  # 29.97fps — kiểu điện thoại quay
        ("src2.mp4", "25", "green"),  # 25fps — kiểu camera châu Âu
        ("src3.mp4", "24000/1001", "blue"),  # 23.976fps — kiểu điện ảnh
    ):
        await run_ok(
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", f"color=c={color}:size=640x360:rate={rate}:duration=3",
            "-f", "lavfi", "-i", "sine=frequency=300:duration=3",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest", str(tmp_path / name),
        )
    sources = [
        SourceVideo(f"src{i}.mp4", await probe(tmp_path / f"src{i}.mp4")) for i in (1, 2, 3)
    ]
    clips = resolve_clips([], sources)
    width, height, fps = resolve_concat_canvas(clips, sources, RenderOptions())
    await run_ok(*build_concat_command(clips, width, height, fps, RenderOptions()), cwd=tmp_path)

    merged = await probe(tmp_path / "merged.mp4")
    assert merged.duration == pytest.approx(8.0, abs=0.2)  # 9 - 2 * 0.5


async def test_disabling_transition_produces_a_hard_cut_at_the_boundary(
    tmp_path: Path,
) -> None:
    """Tắt transition thì đúng như trước: đổi màu ĐỘT NGỘT, không có pha trộn."""
    await make_solid_source(tmp_path, "src1.mp4", color="black", seconds=3, tone_hz=300)
    await make_solid_source(tmp_path, "src2.mp4", color="white", seconds=3, tone_hz=300)
    sources = [
        SourceVideo("src1.mp4", await probe(tmp_path / "src1.mp4")),
        SourceVideo("src2.mp4", await probe(tmp_path / "src2.mp4")),
    ]
    clips = resolve_clips([], sources)
    opts = RenderOptions.model_validate({"transition": {"enabled": False}})
    await run_ok(*build_concat_command(clips, 320, 240, 30.0, opts), cwd=tmp_path)

    merged = await probe(tmp_path / "merged.mp4")
    assert merged.duration == pytest.approx(6.0, abs=0.1)  # không hụt giây nào

    just_before = await mean_frame_value(tmp_path / "merged.mp4", 2.9, tmp_path)
    just_after = await mean_frame_value(tmp_path / "merged.mp4", 3.05, tmp_path)
    assert just_before < 30
    assert just_after > 220
