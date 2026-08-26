"""Test cho hiệu ứng chuyển cảnh (crossfade) khi ghép nhiều clip.

Mặc định BẬT: cắt cứng giữa hai cảnh là đúng thứ gây cảm giác giật (yêu cầu
gốc: "chuyển cảnh mượt mà hơn, không bị giật"). Dùng ``xfade``/``acrossfade``
thay cho ``concat`` — hai khung hình chồng lên nhau ``duration`` giây thay vì
đổi đột ngột.
"""

from __future__ import annotations

import re

import pytest

from app.clips import (
    ResolvedClip,
    SourceVideo,
    build_concat_command,
    merged_duration,
    resolve_clips,
    resolve_concat_canvas,
    total_duration,
)
from app.models import RenderOptions, TransitionOptions
from app.probe_data import ProbeResult
from app.utils import InvalidOptions


def source(name: str, duration: float = 60.0) -> SourceVideo:
    return SourceVideo(
        name=name,
        probe=ProbeResult(
            duration=duration,
            width=1080,
            height=1920,
            fps=30.0,
            has_audio=True,
            has_video=True,
            video_codec="h264",
        ),
    )


def clip(name: str, duration: float) -> ResolvedClip:
    return ResolvedClip(name=name, start=0.0, duration=duration, has_audio=True)


def graph_of(clips: list[ResolvedClip], opts: RenderOptions) -> str:
    cmd = build_concat_command(clips, 1080, 1920, 30.0, opts)
    return cmd[cmd.index("-filter_complex") + 1]


# --------------------------------------------------------------------------- #
# TransitionOptions — validate
# --------------------------------------------------------------------------- #
def test_default_transition_is_fade_and_enabled() -> None:
    opts = TransitionOptions()
    assert opts.enabled is True
    assert opts.style == "fade"
    assert opts.duration == 0.5


def test_unknown_style_is_rejected() -> None:
    with pytest.raises(ValueError):
        TransitionOptions(style="khong-ton-tai")


@pytest.mark.parametrize("duration", [0, -1, 5.01, 100])
def test_duration_out_of_range_is_rejected(duration: float) -> None:
    with pytest.raises(ValueError):
        TransitionOptions(duration=duration)


def test_duration_bounds_accepted() -> None:
    assert TransitionOptions(duration=0.01).duration == 0.01
    assert TransitionOptions(duration=5.0).duration == 5.0


# --------------------------------------------------------------------------- #
# build_concat_command — chọn filter đúng theo cấu hình
# --------------------------------------------------------------------------- #
def test_single_clip_never_needs_a_transition() -> None:
    # Một đoạn thì không có gì để chuyển cảnh -> vẫn dùng concat (rẻ hơn).
    clips = [clip("src1.mp4", 5.0)]
    graph = graph_of(clips, RenderOptions())
    assert "concat=n=1:v=1:a=1[v][a]" in graph
    assert "xfade" not in graph


def test_two_clips_use_xfade_and_acrossfade_by_default() -> None:
    clips = [clip("src1.mp4", 5.0), clip("src2.mp4", 5.0)]
    graph = graph_of(clips, RenderOptions())
    assert "xfade=transition=fade" in graph
    assert "acrossfade=" in graph
    assert "concat=" not in graph


def test_disabling_transition_falls_back_to_hard_concat() -> None:
    clips = [clip("src1.mp4", 5.0), clip("src2.mp4", 5.0)]
    opts = RenderOptions.model_validate({"transition": {"enabled": False}})
    graph = graph_of(clips, opts)
    assert "concat=n=2:v=1:a=1[v][a]" in graph
    assert "xfade" not in graph
    assert "acrossfade" not in graph


def test_chosen_style_reaches_the_xfade_filter() -> None:
    clips = [clip("src1.mp4", 5.0), clip("src2.mp4", 5.0)]
    opts = RenderOptions.model_validate({"transition": {"style": "wipeleft"}})
    assert "xfade=transition=wipeleft:" in graph_of(clips, opts)


def test_final_output_labels_are_always_v_and_a() -> None:
    # -map [v] -map [a] không đổi dù ghép bằng concat hay xfade.
    for opts in (RenderOptions(), RenderOptions.model_validate({"transition": {"enabled": False}})):
        clips = [clip("src1.mp4", 5.0), clip("src2.mp4", 5.0), clip("src3.mp4", 5.0)]
        cmd = build_concat_command(clips, 1080, 1920, 30.0, opts)
        assert cmd[cmd.index("-map") + 1] == "[v]"
        assert cmd[cmd.index("-map", cmd.index("-map") + 1) + 1] == "[a]"


# --------------------------------------------------------------------------- #
# Ba đoạn trở lên: chuỗi xfade phải nối đúng và offset tính đúng
# --------------------------------------------------------------------------- #
def test_three_clips_chain_two_transitions_in_order() -> None:
    clips = [clip("src1.mp4", 5.0), clip("src2.mp4", 5.0), clip("src3.mp4", 5.0)]
    graph = graph_of(clips, RenderOptions())

    # Đúng 2 lần chuyển cảnh cho 3 đoạn (n-1), theo thứ tự v0->v1 rồi ->v2.
    assert graph.count("xfade=transition=") == 2
    assert graph.count("acrossfade=") == 2
    assert "[v0][v1]xfade=" in graph
    assert "[vx1][v2]xfade=" in graph
    assert "[a0][a1]acrossfade=" in graph
    assert "[ax1][a2]acrossfade=" in graph
    # Output cuối cùng luôn đặt tên "v"/"a", không phải "vx2"/"ax2".
    assert graph.rstrip(";").split(";")[-1].endswith("[a]")


def test_xfade_offset_accounts_for_previous_overlap() -> None:
    """Offset của lần chuyển cảnh SAU phải trừ đi phần đã chồng ở lần TRƯỚC.

    3 đoạn 5s, duration=1.0: lần 1 offset=5-1=4; sau lần 1 tổng còn 5+5-1=9,
    lần 2 offset=9-1=8 — KHÔNG phải 5+5-1=9 hay 10 (sai nếu quên trừ chồng lấn).
    """
    clips = [clip("src1.mp4", 5.0), clip("src2.mp4", 5.0), clip("src3.mp4", 5.0)]
    opts = RenderOptions.model_validate({"transition": {"duration": 1.0}})
    graph = graph_of(clips, opts)

    offsets = [float(value) for value in re.findall(r"offset=([\d.]+)", graph)]
    assert offsets == [4.0, 8.0]


def test_merged_duration_subtracts_every_overlap() -> None:
    clips = [clip("src1.mp4", 5.0), clip("src2.mp4", 5.0), clip("src3.mp4", 5.0)]
    opts = RenderOptions.model_validate({"transition": {"duration": 1.0}})
    assert total_duration(clips) == 15.0
    assert merged_duration(clips, opts) == 13.0  # 15 - 2 * 1.0


def test_merged_duration_equals_total_when_disabled() -> None:
    clips = [clip("src1.mp4", 5.0), clip("src2.mp4", 5.0)]
    opts = RenderOptions.model_validate({"transition": {"enabled": False}})
    assert merged_duration(clips, opts) == total_duration(clips)


def test_merged_duration_ignores_transition_for_a_single_clip() -> None:
    clips = [clip("src1.mp4", 5.0)]
    assert merged_duration(clips, RenderOptions()) == 5.0


# --------------------------------------------------------------------------- #
# Đoạn ngắn: không để crossfade nuốt hết một đoạn
# --------------------------------------------------------------------------- #
def test_transition_duration_is_capped_for_a_short_clip() -> None:
    """Đoạn 1s mà xin crossfade 0.5s theo mặc định thì vẫn ổn (đúng nửa đoạn).

    Nhưng xin 5s (trần cho phép của TransitionOptions) thì phải bị hạ xuống,
    không được ăn hết cả đoạn 1 giây.
    """
    clips = [clip("src1.mp4", 5.0), clip("src2.mp4", 1.0)]
    opts = RenderOptions.model_validate({"transition": {"duration": 5.0}})
    graph = graph_of(clips, opts)
    duration = float(re.search(r"xfade=transition=fade:duration=([\d.]+)", graph).group(1))  # type: ignore[union-attr]
    assert duration < 1.0
    assert duration == pytest.approx(1.0 * 0.5)  # kẹp còn 50% đoạn ngắn hơn


def test_transition_duration_never_reaches_zero_or_negative() -> None:
    clips = [clip("src1.mp4", 0.2), clip("src2.mp4", 0.2)]
    graph = graph_of(clips, RenderOptions())
    duration = float(re.search(r"xfade=transition=fade:duration=([\d.]+)", graph).group(1))  # type: ignore[union-attr]
    assert duration > 0


def test_fps_filter_is_the_last_step_before_the_label() -> None:
    r"""Regression: ``fps=`` phải đứng SAU ``setpts``, không phải trước.

    Đã đo bằng ffmpeg thật trên container (7.1.5): ``setpts=PTS-STARTPTS``
    không truyền tiếp metadata ``frame_rate`` của link cho filter kế tiếp. Ghép
    các nguồn có fps GỐC khác nhau (rất hay gặp — video tải từ nhiều nền
    tảng/máy khác nhau) thì filter ``xfade`` phía sau nhận link "current rate
    of 1/0" và từ chối toàn bộ (ffmpeg thoát mã 234), dù mỗi đoạn riêng lẻ hay
    ghép bằng ``concat`` (không cần biết fps) vẫn ra file bình thường — nên bug
    này chỉ lộ ra khi có transition, và chỉ trên ffmpeg thật của container chứ
    không phải bản Windows dùng để chạy test này. Kiểm THEO CẤU TRÚC chuỗi
    filter là cách duy nhất bắt được regression này bằng test nhanh, không cần
    ffmpeg thật lẫn nguồn có fps lệch nhau.
    """
    clips = [clip("src1.mp4", 5.0), clip("src2.mp4", 5.0)]
    graph = graph_of(clips, RenderOptions())
    for position in (0, 1):
        segment = re.search(rf"\[{position}:v\][^;]+\[v{position}\]", graph)
        assert segment is not None, f"không thấy chuỗi chuẩn hoá cho [v{position}]"
        assert re.search(r"fps=[\d.]+\[v\d\]$", segment.group(0)), (
            f"fps phải là filter CUỐI CÙNG ngay trước nhãn: {segment.group(0)}"
        )


def test_video_and_audio_use_the_same_per_pair_duration() -> None:
    # Lệch nhau dù chỉ vài chục mili giây cũng làm tiếng và hình mất đồng bộ.
    clips = [clip("src1.mp4", 5.0), clip("src2.mp4", 1.0), clip("src3.mp4", 5.0)]
    graph = graph_of(clips, RenderOptions())
    video_durations = re.findall(r"xfade=transition=fade:duration=([\d.]+)", graph)
    audio_durations = re.findall(r"acrossfade=d=([\d.]+)", graph)
    assert video_durations == audio_durations


# --------------------------------------------------------------------------- #
# resolve_clips vẫn hoạt động bình thường (transition không ảnh hưởng bước này)
# --------------------------------------------------------------------------- #
def test_resolve_clips_unaffected_by_transition_settings() -> None:
    sources = [source("src1.mp4", 10.0), source("src2.mp4", 10.0)]
    clips = resolve_clips([], sources)
    width, height, fps = resolve_concat_canvas(clips, sources, RenderOptions())
    assert (width, height, fps) == (1080, 1920, 30.0)


def test_empty_clip_list_still_rejected_with_transition_enabled() -> None:
    with pytest.raises(InvalidOptions):
        build_concat_command([], 1080, 1920, 30.0, RenderOptions())
