"""Các nhánh còn lại của build_ffmpeg_command và can_copy_video.

Tách khỏi tests/test_ffmpeg_command.py (chỉ giữ 6 snapshot bắt buộc ở SPEC §9)
để mỗi file test dưới 400 dòng.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from app.ffmpeg_cmd import escape_filter_value
from app.ffmpeg_runner import ProbeResult, RenderInputs, build_ffmpeg_command, can_copy_video
from app.models import RenderOptions
from tests.helpers import FONTS, STYLE
from tests.helpers import build as _build


# --------------------------------------------------------------------------- #
# Các nhánh còn lại
# --------------------------------------------------------------------------- #
def test_resolution_and_fps_override_disable_copy(
    fake_probe_result: ProbeResult, tmp_workspace: Path
) -> None:
    opts = RenderOptions.model_validate({"output": {"resolution": "1280x720", "fps": 25}})
    cmd = _build(RenderInputs(video="input.mp4"), fake_probe_result, opts, tmp_workspace)
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert graph == "[0:v]scale=1280:720[v]"
    assert cmd[cmd.index("-c:v") + 1] == "libx264"
    assert cmd[cmd.index("-r") + 1] == "25"


def test_scale_and_burn_in_same_chain(
    fake_probe_result: ProbeResult, tmp_workspace: Path
) -> None:
    opts = RenderOptions.model_validate({"output": {"resolution": "1280x720"}})
    cmd = _build(
        RenderInputs(video="input.mp4", subs="subs.srt"), fake_probe_result, opts, tmp_workspace
    )
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert graph == (
        f"[0:v]scale=1280:720,subtitles=subs.srt:fontsdir={FONTS}:force_style='{STYLE}'[v]"
    )


def test_soft_subs_map_index_without_music(
    fake_probe_result: ProbeResult, tmp_workspace: Path
) -> None:
    # Không có nhạc -> phụ đề là input 1, KHÔNG phải 2 như ví dụ trong SPEC.
    opts = RenderOptions.model_validate({"subtitle": {"mode": "soft"}})
    cmd = _build(
        RenderInputs(video="input.mp4", subs="subs.srt"), fake_probe_result, opts, tmp_workspace
    )
    assert cmd[cmd.index("-i") + 1] == "input.mp4"
    assert "-map" in cmd and "1:s:0" in cmd
    assert cmd[cmd.index("-c:s") + 1] == "mov_text"
    assert "subtitles=" not in " ".join(cmd)
    assert cmd[cmd.index("-c:v") + 1] == "copy"  # soft-sub vẫn giữ fast path


def test_soft_subs_map_index_with_music(
    fake_probe_result: ProbeResult, tmp_workspace: Path
) -> None:
    opts = RenderOptions.model_validate({"subtitle": {"mode": "soft"}})
    cmd = _build(
        RenderInputs(video="input.mp4", music="music.mp3", subs="subs.srt"),
        fake_probe_result,
        opts,
        tmp_workspace,
    )
    assert "2:s:0" in cmd


def test_soft_subs_index_after_anullsrc(
    fake_probe_result: ProbeResult, tmp_workspace: Path
) -> None:
    silent = dataclasses.replace(fake_probe_result, has_audio=False)
    # original_volume khai rõ để vẫn phải trộn tiếng gốc -> vẫn cần anullsrc,
    # đúng tình huống mà test này muốn kiểm (thứ tự index của input phụ đề).
    opts = RenderOptions.model_validate(
        {"subtitle": {"mode": "soft"}, "music": {"original_volume": 1.0}}
    )
    cmd = _build(
        RenderInputs(video="input.mp4", music="music.mp3", subs="subs.srt"),
        silent,
        opts,
        tmp_workspace,
    )
    # input 0 video, 1 nhạc, 2 anullsrc, 3 phụ đề
    assert "3:s:0" in cmd


def test_ducking_uses_asplit_so_a0_consumed_once(
    fake_probe_result: ProbeResult, tmp_workspace: Path
) -> None:
    # Ducking là né tiếng gốc, nên phải có tiếng gốc mà né.
    opts = RenderOptions.model_validate(
        {"music": {"ducking": True, "original_volume": 1.0}}
    )
    cmd = _build(
        RenderInputs(video="input.mp4", music="music.mp3"), fake_probe_result, opts, tmp_workspace
    )
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert "asplit=2[a0m][a0sc]" in graph
    assert "[a1][a0sc]sidechaincompress=threshold=0.05:ratio=8:attack=20:release=300[duck]" in graph
    assert "[a0m][duck]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[a]" in graph
    # Mỗi label chỉ được tiêu thụ đúng một lần, nếu không ffmpeg báo lỗi pad.
    for label in ("[a0m]", "[a0sc]", "[duck]"):
        assert graph.count(label) == 2  # một lần tạo, một lần dùng


def test_no_loop_omits_stream_loop_and_uses_music_duration(
    fake_probe_result: ProbeResult, tmp_workspace: Path
) -> None:
    opts = RenderOptions.model_validate({"music": {"loop": False, "start_offset": 5}})
    cmd = _build(
        RenderInputs(video="input.mp4", music="music.mp3", music_duration=60.0),
        fake_probe_result,
        opts,
        tmp_workspace,
    )
    assert "-stream_loop" not in cmd
    assert cmd[cmd.index("-ss") + 1] == "5"
    graph = cmd[cmd.index("-filter_complex") + 1]
    # Nhạc phát được 60-5=55s -> fade out bắt đầu ở 55-3=52s, không phải 191.2s
    assert "afade=t=out:st=52:d=3" in graph


def test_zero_fades_are_omitted(fake_probe_result: ProbeResult, tmp_workspace: Path) -> None:
    opts = RenderOptions.model_validate({"music": {"fade_in": 0, "fade_out": 0}})
    cmd = _build(
        RenderInputs(video="input.mp4", music="music.mp3"), fake_probe_result, opts, tmp_workspace
    )
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert "afade" not in graph


def test_original_volume_only_still_builds_audio_chain(
    fake_probe_result: ProbeResult, tmp_workspace: Path
) -> None:
    opts = RenderOptions.model_validate({"music": {"original_volume": 0.5}})
    cmd = _build(RenderInputs(video="input.mp4"), fake_probe_result, opts, tmp_workspace)
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert graph == (
        "[0:a]volume=0.5,aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo[a]"
    )
    assert "-shortest" not in cmd  # không có nhạc -> không cần chặn độ dài


def test_music_disabled_ignores_music_file(
    fake_probe_result: ProbeResult, tmp_workspace: Path
) -> None:
    opts = RenderOptions.model_validate({"music": {"enabled": False}})
    cmd = _build(
        RenderInputs(video="input.mp4", music="music.mp3"), fake_probe_result, opts, tmp_workspace
    )
    assert "music.mp3" not in cmd
    assert "amix" not in " ".join(cmd)


def test_faststart_can_be_disabled(
    fake_probe_result: ProbeResult, tmp_workspace: Path
) -> None:
    opts = RenderOptions.model_validate({"output": {"faststart": False}})
    cmd = _build(RenderInputs(video="input.mp4"), fake_probe_result, opts, tmp_workspace)
    assert "-movflags" not in cmd


def test_threads_option_is_passed(
    fake_probe_result: ProbeResult, default_options: RenderOptions, tmp_workspace: Path
) -> None:
    cmd = build_ffmpeg_command(
        RenderInputs(video="input.mp4"),
        fake_probe_result,
        default_options,
        tmp_workspace,
        threads=4,
    )
    assert cmd[cmd.index("-threads") + 1] == "4"
    assert build_ffmpeg_command(
        RenderInputs(video="input.mp4"), fake_probe_result, default_options, tmp_workspace
    ).count("-threads") == 0


def test_custom_output_filename_and_codec(
    fake_probe_result: ProbeResult, tmp_workspace: Path
) -> None:
    opts = RenderOptions.model_validate(
        {
            "subtitle": {"enabled": False},
            "output": {
                "filename": "final_video.mp4",
                "video_codec": "libx265",
                "crf": 18,
                "preset": "slow",
                "audio_codec": "libmp3lame",
                "audio_bitrate": "320k",
                "copy_video_if_possible": False,
            },
        }
    )
    cmd = _build(RenderInputs(video="input.mp4"), fake_probe_result, opts, tmp_workspace)
    assert cmd[-1] == "final_video.mp4"
    assert cmd[cmd.index("-c:v") + 1] == "libx265"
    assert cmd[cmd.index("-crf") + 1] == "18"
    assert cmd[cmd.index("-preset") + 1] == "slow"
    assert cmd[cmd.index("-c:a") + 1] == "libmp3lame"
    assert cmd[cmd.index("-b:a") + 1] == "320k"


def test_command_is_pure_argv_never_shell_string(
    fake_probe_result: ProbeResult, default_options: RenderOptions, tmp_workspace: Path
) -> None:
    cmd = _build(
        RenderInputs(video="input.mp4", music="music.mp3", subs="subs.srt"),
        fake_probe_result,
        default_options,
        tmp_workspace,
    )
    assert all(isinstance(part, str) for part in cmd)
    # Đường dẫn là phần tử argv riêng, không nối chuỗi, không có shell metachar.
    assert cmd.count("input.mp4") == 1
    assert not any(part.startswith("ffmpeg ") for part in cmd)
    assert not any("&&" in part or "|" in part or ">" in part for part in cmd)


# --------------------------------------------------------------------------- #
# can_copy_video
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("codec", "expected"),
    [
        ("h264", True),
        # hevc + video_codec mặc định (libx264) là ĐỔI codec -> không được copy,
        # nếu không client xin libx264 lại nhận về hevc.
        ("hevc", False),
        ("vp9", False),
        ("prores", False),
        (None, False),
    ],
)
def test_can_copy_video_depends_on_input_codec(
    fake_probe_result: ProbeResult, default_options: RenderOptions, codec: str | None, expected: bool
) -> None:
    probe_result = dataclasses.replace(fake_probe_result, video_codec=codec)
    assert can_copy_video(probe_result, default_options, has_subs=False) is expected


@pytest.mark.parametrize(
    ("codec", "requested", "expected"),
    [
        ("h264", "libx264", True),
        ("h264", "libx265", False),  # đổi codec -> phải encode lại
        ("hevc", "libx265", True),
        ("hevc", "libx264", False),
    ],
)
def test_can_copy_video_requires_matching_codec(
    fake_probe_result: ProbeResult, codec: str, requested: str, expected: bool
) -> None:
    probe_result = dataclasses.replace(fake_probe_result, video_codec=codec)
    opts = RenderOptions.model_validate({"output": {"video_codec": requested}})
    assert can_copy_video(probe_result, opts, has_subs=False) is expected


def test_requesting_libx265_on_h264_input_re_encodes(
    fake_probe_result: ProbeResult, tmp_workspace: Path
) -> None:
    opts = RenderOptions.model_validate(
        {"subtitle": {"enabled": False}, "output": {"video_codec": "libx265"}}
    )
    cmd = _build(RenderInputs(video="input.mp4"), fake_probe_result, opts, tmp_workspace)
    assert cmd[cmd.index("-c:v") + 1] == "libx265"
    assert "copy" not in cmd


def test_can_copy_video_false_when_flag_off(
    fake_probe_result: ProbeResult,
) -> None:
    opts = RenderOptions.model_validate({"output": {"copy_video_if_possible": False}})
    assert can_copy_video(fake_probe_result, opts, has_subs=False) is False


def test_vp9_input_is_re_encoded(fake_probe_result: ProbeResult, tmp_workspace: Path) -> None:
    vp9 = dataclasses.replace(fake_probe_result, video_codec="vp9")
    opts = RenderOptions.model_validate({"subtitle": {"enabled": False}})
    cmd = _build(RenderInputs(video="input.webm"), vp9, opts, tmp_workspace)
    assert cmd[cmd.index("-c:v") + 1] == "libx264"
    assert "-pix_fmt" in cmd


# --------------------------------------------------------------------------- #
# escape_filter_value — từng dạng dưới đây đã được chạy thử với ffmpeg thật
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Không có ký tự đặc biệt -> giữ nguyên, lệnh sinh ra trên Linux giống
        # hệt ví dụ trong SPEC §5.3.
        ("/app/fonts", "/app/fonts"),
        ("fonts", "fonts"),
        # Windows: ':' phân tách option của filter -> bọc nháy đơn + escape ':'
        ("C:\\Users\\tuan\\fonts", "'C\\:/Users/tuan/fonts'"),
        ("C:\\Program Files\\my fonts", "'C\\:/Program Files/my fonts'"),
        ("C:\\a,b\\fonts", "'C\\:/a,b/fonts'"),
        # Nháy đơn bên trong: phải đóng nháy - escape - mở nháy lại
        ("C:\\we'ird\\fonts", "'C\\:/we'\\''ird/fonts'"),
        # Backslash đơn thuần cũng đổi sang '/'
        ("relative\\path", "'relative/path'"),
    ],
)
def test_escape_filter_value(raw: str, expected: str) -> None:
    assert escape_filter_value(raw) == expected


def test_windows_fontsdir_is_escaped_in_command(
    fake_probe_result: ProbeResult, tmp_workspace: Path
) -> None:
    cmd = build_ffmpeg_command(
        RenderInputs(video="input.mp4", subs="subs.srt"),
        fake_probe_result,
        RenderOptions(),
        tmp_workspace,
        fonts_dir="C:\\Users\\tuan\\fonts",
    )
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert "fontsdir='C\\:/Users/tuan/fonts'" in graph
    # subs.srt là tên tương đối cố định trong workspace nên không cần escape
    assert "subtitles=subs.srt:" in graph


# --------------------------------------------------------------------------- #
# Chặn độ dài output: -t thay cho -shortest
# --------------------------------------------------------------------------- #
def test_music_uses_t_not_shortest(
    fake_probe_result: ProbeResult, default_options: RenderOptions, tmp_workspace: Path
) -> None:
    """-shortest cắt theo stream ngắn nhất -> stream phụ đề mov_text làm mất
    phần cuối video (đo được: 6s -> 2s). Dùng -t <duration> thay thế."""
    cmd = _build(
        RenderInputs(video="input.mp4", music="music.mp3"),
        fake_probe_result,
        default_options,
        tmp_workspace,
    )
    assert "-shortest" not in cmd
    assert cmd[cmd.index("-t") + 1] == "194.2"


def test_soft_subs_plus_music_never_uses_shortest(
    fake_probe_result: ProbeResult, tmp_workspace: Path
) -> None:
    opts = RenderOptions.model_validate({"subtitle": {"mode": "soft"}})
    cmd = _build(
        RenderInputs(video="input.mp4", music="music.mp3", subs="subs.srt"),
        fake_probe_result,
        opts,
        tmp_workspace,
    )
    assert "-shortest" not in cmd
    assert "-t" in cmd


def test_unknown_duration_falls_back_to_shortest(tmp_workspace: Path) -> None:
    # Không probe được duration -> không có -t, đành dùng -shortest.
    unknown = ProbeResult(duration=0.0, has_video=True, has_audio=True, video_codec="h264")
    cmd = _build(
        RenderInputs(video="input.mp4", music="music.mp3"),
        unknown,
        RenderOptions(),
        tmp_workspace,
    )
    assert "-shortest" in cmd
    assert "-t" not in cmd


# --------------------------------------------------------------------------- #
# _fmt: không được dùng ký hiệu khoa học
# --------------------------------------------------------------------------- #
def test_long_duration_is_not_scientific_notation(tmp_workspace: Path) -> None:
    # %g biến 1234567.5 thành "1.23457e+06" mà ffmpeg không parse được.
    long_video = ProbeResult(
        duration=1_234_567.5, has_video=True, has_audio=True, video_codec="h264"
    )
    cmd = _build(
        RenderInputs(video="input.mp4", music="music.mp3"),
        long_video,
        RenderOptions(),
        tmp_workspace,
    )
    duration_arg = cmd[cmd.index("-t") + 1]
    assert duration_arg == "1234567.5"
    assert "e+" not in duration_arg
    assert "e+" not in " ".join(cmd)
