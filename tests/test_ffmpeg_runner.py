"""Test parse_progress_line, parse_probe_json và run_ffmpeg.

run_ffmpeg được test bằng một process Python giả lập output của
``ffmpeg -progress pipe:1`` nên không cần ffmpeg thật.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections import deque
from pathlib import Path
from typing import Any

import pytest

from app.ffmpeg_runner import (
    RenderInputs,
    build_ffmpeg_command,
    parse_probe_json,
    parse_progress_line,
    run_ffmpeg,
)
from app.utils import FfmpegFailed, ProbeFailed

# --------------------------------------------------------------------------- #
# parse_progress_line
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("out_time_ms=1234567\n", {"out_time_ms": "1234567"}),
        ("progress=continue", {"progress": "continue"}),
        ("progress=end\r\n", {"progress": "end"}),
        ("frame=120", {"frame": "120"}),
        ("bitrate= 1500.2kbits/s", {"bitrate": "1500.2kbits/s"}),
        ("speed=1.02x", {"speed": "1.02x"}),
        ("out_time=00:00:05.000000", {"out_time": "00:00:05.000000"}),
        ("out_time_ms=N/A", {"out_time_ms": "N/A"}),
        ("", {}),
        ("   \n", {}),
        ("no-equals-sign", {}),
        ("=orphan", {}),
    ],
)
def test_parse_progress_line(line: str, expected: dict[str, str]) -> None:
    assert parse_progress_line(line) == expected


# --------------------------------------------------------------------------- #
# parse_probe_json
# --------------------------------------------------------------------------- #
def test_parse_probe_json_full() -> None:
    payload = {
        "format": {"duration": "194.203000"},
        "streams": [
            {
                "codec_type": "video",
                "codec_name": "h264",
                "width": 1920,
                "height": 1080,
                "r_frame_rate": "30000/1001",
            },
            {"codec_type": "audio", "codec_name": "aac"},
        ],
    }
    result = parse_probe_json(payload)
    assert result.duration == pytest.approx(194.203)
    assert (result.width, result.height) == (1920, 1080)
    assert result.fps == pytest.approx(29.97, abs=0.01)
    assert result.has_audio is True
    assert result.has_video is True
    assert result.video_codec == "h264"
    assert result.audio_codec == "aac"


def test_parse_probe_json_video_without_audio() -> None:
    payload = {
        "format": {"duration": "5.0"},
        "streams": [{"codec_type": "video", "codec_name": "vp9", "r_frame_rate": "25/1"}],
    }
    result = parse_probe_json(payload)
    assert result.has_audio is False
    assert result.has_video is True
    assert result.fps == 25.0
    assert result.video_codec == "vp9"
    assert result.audio_codec is None


def test_parse_probe_json_falls_back_to_stream_duration() -> None:
    payload = {
        "format": {"duration": "N/A"},
        "streams": [{"codec_type": "video", "duration": "12.5", "r_frame_rate": "0/0"}],
    }
    result = parse_probe_json(payload)
    assert result.duration == 12.5
    assert result.fps == 0.0  # 0/0 không chia được -> 0, không crash


def test_parse_probe_json_empty() -> None:
    result = parse_probe_json({})
    assert result.duration == 0.0
    assert result.has_video is False
    assert result.has_audio is False


# --------------------------------------------------------------------------- #
# run_ffmpeg — dùng process Python giả lập
# --------------------------------------------------------------------------- #
def _fake_ffmpeg(script: str) -> list[str]:
    return [sys.executable, "-c", script]


async def test_run_ffmpeg_parses_microseconds_not_millis(tmp_path: Path) -> None:
    # out_time_ms=5_000_000 micro = 5 giây. Nếu ai đó hiểu là millisecond thì
    # progress sẽ ra 5000s và test này fail ngay.
    script = (
        "import sys\n"
        "for us in (1_000_000, 3_000_000, 5_000_000):\n"
        "    print(f'out_time_ms={us}')\n"
        "    print('progress=continue')\n"
        "print('progress=end')\n"
    )
    seen: list[tuple[float, float]] = []
    code = await run_ffmpeg(
        _fake_ffmpeg(script),
        total_duration=10.0,
        on_progress=lambda pct, secs: seen.append((pct, secs)),
        cwd=tmp_path,
    )
    assert code == 0
    assert seen[-1] == (50.0, 5.0)


async def test_run_ffmpeg_throttles_to_one_per_second(tmp_path: Path) -> None:
    script = (
        "for i in range(1, 501):\n"
        "    print(f'out_time_ms={i * 10_000}')\n"
        "print('progress=end')\n"
    )
    seen: list[float] = []
    await run_ffmpeg(
        _fake_ffmpeg(script),
        total_duration=60.0,
        on_progress=lambda pct, secs: seen.append(pct),
        cwd=tmp_path,
    )
    # 501 dòng trong chưa tới 1 giây -> chỉ dòng đầu và progress=end được đẩy ra.
    assert len(seen) <= 3


async def test_run_ffmpeg_progress_end_keeps_last_value(tmp_path: Path) -> None:
    script = "print('out_time_ms=2000000')\nprint('progress=end')\n"
    seen: list[tuple[float, float]] = []
    await run_ffmpeg(
        _fake_ffmpeg(script),
        total_duration=4.0,
        on_progress=lambda pct, secs: seen.append((pct, secs)),
        cwd=tmp_path,
    )
    # progress=end không kèm out_time -> không được reset về 0.
    assert seen[-1] == (50.0, 2.0)


async def test_run_ffmpeg_caps_progress_at_99(tmp_path: Path) -> None:
    script = "print('out_time_ms=99000000')\nprint('progress=end')\n"
    seen: list[float] = []
    await run_ffmpeg(
        _fake_ffmpeg(script),
        total_duration=10.0,
        on_progress=lambda pct, _s: seen.append(pct),
        cwd=tmp_path,
    )
    assert max(seen) == 99.0


async def test_run_ffmpeg_collects_stderr_tail(tmp_path: Path) -> None:
    script = (
        "import sys\n"
        "for i in range(500):\n"
        "    print(f'error line {i}', file=sys.stderr)\n"
        "sys.exit(1)\n"
    )
    buf: deque[str] = deque(maxlen=200)
    code = await run_ffmpeg(_fake_ffmpeg(script), total_duration=1.0, stderr_buf=buf, cwd=tmp_path)
    assert code == 1
    assert len(buf) == 200  # chỉ giữ 200 dòng cuối cho error.detail
    assert buf[-1] == "error line 499"


async def test_run_ffmpeg_on_start_exposes_process(tmp_path: Path) -> None:
    captured: list[object] = []
    await run_ffmpeg(
        _fake_ffmpeg("print('progress=end')"),
        total_duration=1.0,
        cwd=tmp_path,
        on_start=captured.append,
    )
    assert captured and hasattr(captured[0], "terminate")


async def test_run_ffmpeg_handles_zero_duration(tmp_path: Path) -> None:
    seen: list[float] = []
    code = await run_ffmpeg(
        _fake_ffmpeg("print('out_time_ms=1000000')\nprint('progress=end')"),
        total_duration=0.0,
        on_progress=lambda pct, _s: seen.append(pct),
        cwd=tmp_path,
    )
    assert code == 0
    assert seen == [0.0] or seen == [0.0, 0.0]  # không chia cho 0


async def test_run_ffmpeg_missing_binary_raises_apperror(tmp_path: Path) -> None:
    with pytest.raises(FfmpegFailed):
        await run_ffmpeg(["ffmpeg-khong-ton-tai-xyz"], total_duration=1.0, cwd=tmp_path)


async def test_probe_missing_binary_raises_probe_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app import ffmpeg_runner

    async def _boom(*_args: object, **_kwargs: object) -> None:
        raise FileNotFoundError("ffprobe")

    monkeypatch.setattr(ffmpeg_runner.asyncio, "create_subprocess_exec", _boom)
    with pytest.raises(ProbeFailed):
        await ffmpeg_runner.probe(tmp_path / "input.mp4")


async def test_probe_rejects_file_without_video(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app import ffmpeg_runner

    payload = json.dumps({"format": {"duration": "3"}, "streams": [{"codec_type": "audio"}]})

    class _FakeProc:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return payload.encode(), b""

    async def _fake_exec(*_args: object, **_kwargs: object) -> _FakeProc:
        return _FakeProc()

    monkeypatch.setattr(ffmpeg_runner.asyncio, "create_subprocess_exec", _fake_exec)
    with pytest.raises(ProbeFailed, match="stream video"):
        await ffmpeg_runner.probe(tmp_path / "audio.mp3")


# --------------------------------------------------------------------------- #
# run_ffmpeg không được bỏ rơi tiến trình ffmpeg
# --------------------------------------------------------------------------- #
async def test_run_ffmpeg_kills_process_when_pump_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pump lỗi -> ffmpeg phải bị kill, không được để mồ côi ăn CPU."""
    from app import ffmpeg_runner

    captured: list[Any] = []

    async def _boom(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("readline vượt giới hạn 64 KiB")

    monkeypatch.setattr(ffmpeg_runner, "_pump_stderr", _boom)

    with pytest.raises(RuntimeError):
        await run_ffmpeg(
            _fake_ffmpeg("import time; time.sleep(30)"),
            total_duration=1.0,
            cwd=tmp_path,
            on_start=captured.append,
        )

    assert captured, "phải bắt được process qua on_start"
    process = captured[0]
    assert process.returncode is not None, "tiến trình bị bỏ rơi, vẫn đang chạy"


async def test_run_ffmpeg_kills_process_on_cancel(tmp_path: Path) -> None:
    captured: list[Any] = []
    task = asyncio.create_task(
        run_ffmpeg(
            _fake_ffmpeg("import time; time.sleep(30)"),
            total_duration=1.0,
            cwd=tmp_path,
            on_start=captured.append,
        )
    )
    while not captured:
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert captured[0].returncode is not None, "cancel xong ffmpeg vẫn còn sống"


# --------------------------------------------------------------------------- #
# Rotation: ffprobe báo kích thước "coded", filter graph lại nhận frame đã xoay
# --------------------------------------------------------------------------- #
def _video_payload(width: int, height: int, **extra: Any) -> dict[str, Any]:
    stream: dict[str, Any] = {
        "codec_type": "video",
        "codec_name": "h264",
        "width": width,
        "height": height,
        "r_frame_rate": "30/1",
    }
    stream.update(extra)
    return {"format": {"duration": "10"}, "streams": [stream]}


@pytest.mark.parametrize("rotation", [90, 270, -90, -270])
def test_rotation_swaps_width_and_height(rotation: int) -> None:
    """Video điện thoại: coded 1280x720 + xoay 90° -> frame thật 720x1280."""
    payload = _video_payload(
        1280, 720, side_data_list=[{"side_data_type": "Display Matrix", "rotation": rotation}]
    )
    result = parse_probe_json(payload)
    assert (result.width, result.height) == (720, 1280)
    assert result.rotation == rotation % 360


@pytest.mark.parametrize("rotation", [0, 180, -180, 360])
def test_rotation_without_swap(rotation: int) -> None:
    payload = _video_payload(
        1280, 720, side_data_list=[{"side_data_type": "Display Matrix", "rotation": rotation}]
    )
    result = parse_probe_json(payload)
    assert (result.width, result.height) == (1280, 720)


def test_legacy_tags_rotate_is_honoured() -> None:
    # File mp4 cũ lưu góc xoay ở tags.rotate dạng chuỗi.
    result = parse_probe_json(_video_payload(1920, 1080, tags={"rotate": "270"}))
    assert (result.width, result.height) == (1080, 1920)
    assert result.rotation == 270


def test_no_rotation_metadata_keeps_dimensions() -> None:
    result = parse_probe_json(_video_payload(1920, 1080))
    assert (result.width, result.height) == (1920, 1080)
    assert result.rotation == 0


@pytest.mark.parametrize("bad", ["abc", None, {}, []])
def test_malformed_rotation_is_ignored(bad: Any) -> None:
    payload = _video_payload(1920, 1080, side_data_list=[{"rotation": bad}])
    result = parse_probe_json(payload)
    assert (result.width, result.height) == (1920, 1080)
    assert result.rotation == 0


def test_rotated_video_gets_font_size_for_real_canvas() -> None:
    """Chốt bug: không xử lý rotation thì cỡ chữ tính theo khung ngang, ra chữ
    to gấp ~3 lần trên video dọc thật."""
    from app.models import RenderOptions
    from app.subtitles import ASS_PLAY_RES_Y

    rotated = parse_probe_json(
        _video_payload(1280, 720, side_data_list=[{"rotation": 90}])
    )
    cmd = build_ffmpeg_command(
        RenderInputs(video="input.mp4", subs="subs.srt"),
        rotated,
        RenderOptions(),
        Path("/tmp/ws"),
    )
    graph = cmd[cmd.index("-filter_complex") + 1]
    size = float(graph.split("FontSize=")[1].split(",")[0])
    # Khung thật là 720x1280 -> chữ phải cao 4% của 720 = 28.8px
    assert size * 1280 / ASS_PLAY_RES_Y == pytest.approx(0.04 * 720, rel=1e-6)
