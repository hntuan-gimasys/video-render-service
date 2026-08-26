"""Test cho app/clips.py — cắt & ghép nhiều đoạn từ nhiều video nguồn."""

from __future__ import annotations

import pytest

from app.clips import (
    MERGED_NAME,
    SourceVideo,
    build_concat_command,
    parse_clip_lines,
    resolve_clips,
    resolve_concat_canvas,
    total_duration,
)
from app.models import ClipSpec, RenderOptions, parse_time_value
from app.probe_data import ProbeResult
from app.utils import InvalidOptions


def source(name: str, **kwargs: object) -> SourceVideo:
    defaults = {
        "duration": 60.0,
        "width": 1080,
        "height": 1920,
        "fps": 30.0,
        "has_audio": True,
        "has_video": True,
        "video_codec": "h264",
    }
    probe = ProbeResult(**{**defaults, **kwargs})  # type: ignore[arg-type]
    return SourceVideo(name=name, probe=probe)


# --------------------------------------------------------------------------- #
# parse_time_value
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (12, 12.0),
        (7.5, 7.5),
        ("12", 12.0),
        ("1:05", 65.0),
        ("00:01:02.5", 62.5),
        ("0:00", 0.0),
        ("1:02:03", 3723.0),
        ("9,5", 9.5),
    ],
)
def test_parse_time_value_accepts_common_formats(raw: object, expected: float) -> None:
    assert parse_time_value(raw) == pytest.approx(expected)  # type: ignore[arg-type]


@pytest.mark.parametrize("raw", ["", "abc", "1:99", "-5", "  "])
def test_parse_time_value_rejects_nonsense(raw: str) -> None:
    with pytest.raises(ValueError):
        parse_time_value(raw)


# --------------------------------------------------------------------------- #
# parse_clip_lines
# --------------------------------------------------------------------------- #
def test_parse_clip_lines_basic() -> None:
    specs = parse_clip_lines("1 00:00-00:05\n2 0:10-0:18")
    assert [(s.source, s.start, s.end) for s in specs] == [
        (1, 0.0, 5.0),
        (2, 10.0, 18.0),
    ]


def test_parse_clip_lines_accepts_semicolon_and_comments() -> None:
    # Ô nhập một dòng của Swagger bóp mất newline -> phải nhận ';'.
    specs = parse_clip_lines("# lay hai doan; 1 0-5; 2 3-9")
    assert [(s.source, s.start, s.end) for s in specs] == [(1, 0.0, 5.0), (2, 3.0, 9.0)]


def test_parse_clip_lines_source_only_means_whole_video() -> None:
    specs = parse_clip_lines("3")
    assert specs == [ClipSpec(source=3)]
    assert specs[0].stop_at() is None


def test_parse_clip_lines_accepts_space_separated_and_arrow() -> None:
    assert parse_clip_lines("2 0:05 0:12")[0].end == 12.0
    assert parse_clip_lines("2 0:05->0:12")[0].end == 12.0


def test_parse_clip_lines_rejects_garbage() -> None:
    with pytest.raises(InvalidOptions):
        parse_clip_lines("lay doan dau tien nhe")


def test_parse_clip_lines_rejects_end_before_start() -> None:
    with pytest.raises(InvalidOptions):
        parse_clip_lines("1 00:10-00:04")


def test_clip_spec_rejects_end_and_duration_together() -> None:
    with pytest.raises(ValueError):
        ClipSpec(source=1, start=0, end=5, duration=5)


# --------------------------------------------------------------------------- #
# resolve_clips
# --------------------------------------------------------------------------- #
def test_resolve_clips_without_specs_takes_every_source_whole() -> None:
    sources = [source("src1.mp4", duration=10.0), source("src2.mp4", duration=4.0)]
    clips = resolve_clips([], sources)
    assert [(c.name, c.start, c.duration) for c in clips] == [
        ("src1.mp4", 0.0, 10.0),
        ("src2.mp4", 0.0, 4.0),
    ]
    assert total_duration(clips) == 14.0


def test_resolve_clips_keeps_declared_order_and_allows_reuse() -> None:
    sources = [source("src1.mp4", duration=60.0), source("src2.mp4", duration=60.0)]
    specs = parse_clip_lines("2 0-3; 1 10-12; 2 20-21")
    assert [c.name for c in resolve_clips(specs, sources)] == [
        "src2.mp4",
        "src1.mp4",
        "src2.mp4",
    ]


def test_resolve_clips_clamps_end_to_real_duration() -> None:
    clips = resolve_clips(parse_clip_lines("1 0-99"), [source("src1.mp4", duration=7.0)])
    assert clips[0].duration == 7.0


def test_resolve_clips_supports_duration_instead_of_end() -> None:
    clips = resolve_clips(
        [ClipSpec(source=1, start=5, duration=2.5)], [source("src1.mp4", duration=60.0)]
    )
    assert (clips[0].start, clips[0].duration) == (5.0, 2.5)


def test_resolve_clips_rejects_source_out_of_range() -> None:
    with pytest.raises(InvalidOptions, match="video số 3"):
        resolve_clips(parse_clip_lines("3 0-1"), [source("src1.mp4")])


def test_resolve_clips_rejects_empty_range_after_clamping() -> None:
    # start nằm sau khi video đã hết -> đoạn rỗng, báo lỗi thay vì ghép hụt.
    with pytest.raises(InvalidOptions, match="rỗng"):
        resolve_clips(parse_clip_lines("1 30-40"), [source("src1.mp4", duration=10.0)])


def test_resolve_clips_requires_end_when_duration_unknown() -> None:
    with pytest.raises(InvalidOptions, match="độ dài"):
        resolve_clips([], [source("src1.mp4", duration=0.0)])


def test_resolve_clips_rejects_no_sources() -> None:
    with pytest.raises(InvalidOptions):
        resolve_clips([], [])


# --------------------------------------------------------------------------- #
# resolve_concat_canvas
# --------------------------------------------------------------------------- #
def test_canvas_follows_first_clip_source() -> None:
    sources = [
        source("src1.mp4", width=1920, height=1080, fps=24.0),
        source("src2.mp4", width=720, height=1280, fps=30.0),
    ]
    clips = resolve_clips(parse_clip_lines("2 0-2; 1 0-2"), sources)
    width, height, _fps = resolve_concat_canvas(clips, sources, RenderOptions())
    # Đoạn ĐẦU TIÊN là của src2 (dọc) -> khung dọc.
    assert (width, height) == (720, 1280)


def test_canvas_takes_highest_fps_among_used_sources() -> None:
    sources = [source("src1.mp4", fps=24.0), source("src2.mp4", fps=60.0)]
    clips = resolve_clips([], sources)
    assert resolve_concat_canvas(clips, sources, RenderOptions())[2] == 60.0


def test_canvas_caps_fps_at_60() -> None:
    sources = [source("src1.mp4", fps=240.0)]
    clips = resolve_clips([], sources)
    assert resolve_concat_canvas(clips, sources, RenderOptions())[2] == 60.0


def test_explicit_resolution_overrides_source_canvas() -> None:
    sources = [source("src1.mp4", width=1920, height=1080)]
    clips = resolve_clips([], sources)
    opts = RenderOptions.model_validate({"output": {"resolution": "1080x1920", "fps": 25}})
    assert resolve_concat_canvas(clips, sources, opts) == (1080, 1920, 25.0)


def test_canvas_rounds_odd_source_size_down_to_even() -> None:
    # yuv420p/H.264 từ chối cạnh lẻ -> phải làm chẵn trước khi đưa cho ffmpeg.
    sources = [source("src1.mp4", width=721, height=1281)]
    clips = resolve_clips([], sources)
    assert resolve_concat_canvas(clips, sources, RenderOptions())[:2] == (720, 1280)


# --------------------------------------------------------------------------- #
# build_concat_command
# --------------------------------------------------------------------------- #
#: Ba test snapshot dưới đây kiểm phần dựng lệnh KHÔNG liên quan tới hiệu ứng
#: chuyển cảnh, nên tắt transition cho gọn graph — xem tests/test_transitions.py
#: cho phần xfade/acrossfade (mặc định bật).
NO_TRANSITION = RenderOptions.model_validate({"transition": {"enabled": False}})


def test_concat_command_seeks_per_clip_and_maps_concat_output() -> None:
    sources = [source("src1.mp4"), source("src2.mp4")]
    clips = resolve_clips(parse_clip_lines("1 5-10; 2 0-3"), sources)
    cmd = build_concat_command(clips, 1080, 1920, 30.0, NO_TRANSITION)

    assert cmd[:2] == ["ffmpeg", "-y"]
    # -ss ĐỨNG TRƯỚC -i mới là seek nhanh; đứng sau là đọc tuần tự từ đầu file.
    assert cmd[cmd.index("-i") - 4 : cmd.index("-i") + 2] == [
        "-ss", "5", "-t", "5", "-i", "src1.mp4",
    ]
    assert cmd[-1] == MERGED_NAME
    assert cmd[cmd.index("-map") + 1] == "[v]"
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert "concat=n=2:v=1:a=1[v][a]" in graph


def test_concat_letterboxes_instead_of_stretching() -> None:
    clips = resolve_clips([], [source("src1.mp4", width=1920, height=1080)])
    graph = build_concat_command(clips, 1080, 1920, 30.0, NO_TRANSITION)[
        build_concat_command(clips, 1080, 1920, 30.0, NO_TRANSITION).index(
            "-filter_complex"
        )
        + 1
    ]
    # force_original_aspect_ratio=decrease + pad = thu vừa khung rồi chèn viền.
    # Thiếu nó thì scale kéo giãn ảnh cho vừa -> hình méo.
    assert "force_original_aspect_ratio=decrease" in graph
    assert "pad=1080:1920:(ow-iw)/2:(oh-ih)/2" in graph
    assert "setsar=1" in graph


def test_concat_adds_silence_input_for_clip_without_audio() -> None:
    sources = [source("src1.mp4"), source("src2.mp4", has_audio=False)]
    clips = resolve_clips(parse_clip_lines("1 0-2; 2 0-3"), sources)
    cmd = build_concat_command(clips, 1080, 1920, 30.0, RenderOptions())
    # Thiếu luồng im lặng thì concat lệch số luồng giữa các đoạn và chết.
    assert "anullsrc=channel_layout=stereo:sample_rate=48000" in cmd
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert "[2:a]" in graph  # đoạn 2 lấy tiếng từ input im lặng (index 2)


def test_concat_uses_lower_crf_than_final_output() -> None:
    # Ghép là vòng encode đầu; burn phụ đề sau đó encode lần hai.
    clips = resolve_clips([], [source("src1.mp4")])
    opts = RenderOptions.model_validate({"output": {"crf": 23}})
    cmd = build_concat_command(clips, 1080, 1920, 30.0, opts)
    assert cmd[cmd.index("-crf") + 1] == "20"


def test_concat_rejects_empty_clip_list() -> None:
    with pytest.raises(InvalidOptions):
        build_concat_command([], 1080, 1920, 30.0, RenderOptions())
