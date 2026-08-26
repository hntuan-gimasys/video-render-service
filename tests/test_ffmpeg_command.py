"""Snapshot argv của build_ffmpeg_command — 6 tổ hợp bắt buộc ở SPEC §9.

Không test nào ở đây gọi ffmpeg thật: build_ffmpeg_command là hàm thuần nên chỉ
cần so sánh list argv sinh ra với giá trị kỳ vọng. Các nhánh phụ (ducking,
soft-sub, resolution...) nằm ở tests/test_ffmpeg_branches.py.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

from app.ffmpeg_runner import ProbeResult, RenderInputs, can_copy_video
from app.models import RenderOptions
from tests.helpers import FONTS, HEAD, STYLE
from tests.helpers import build as _build



# --------------------------------------------------------------------------- #
# 1/6 — video-only (không srt, không nhạc) -> fast path copy
# --------------------------------------------------------------------------- #
def test_video_only(
    fake_probe_result: ProbeResult, default_options: RenderOptions, tmp_workspace: Path
) -> None:
    cmd = _build(RenderInputs(video="input.mp4"), fake_probe_result, default_options, tmp_workspace)
    assert cmd == HEAD + [
        "-i",
        "input.mp4",
        "-map",
        "0:v:0",
        "-map",
        "0:a:0",
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-ar",
        "48000",
        "-movflags",
        "+faststart",
        "output.mp4",
    ]


# --------------------------------------------------------------------------- #
# 2/6 — video + srt (hardsub) -> phải encode lại, có pix_fmt
# --------------------------------------------------------------------------- #
def test_video_plus_srt(
    fake_probe_result: ProbeResult, default_options: RenderOptions, tmp_workspace: Path
) -> None:
    cmd = _build(
        RenderInputs(video="input.mp4", subs="subs.srt"),
        fake_probe_result,
        default_options,
        tmp_workspace,
    )
    assert cmd == HEAD + [
        "-i",
        "input.mp4",
        "-filter_complex",
        f"[0:v]subtitles=subs.srt:fontsdir={FONTS}:force_style='{STYLE}'[v]",
        "-map",
        "[v]",
        "-map",
        "0:a:0",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "23",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-ar",
        "48000",
        "-movflags",
        "+faststart",
        "output.mp4",
    ]


# --------------------------------------------------------------------------- #
# 3/6 — video + nhạc (không srt) -> fast path copy + amix normalize=0
# --------------------------------------------------------------------------- #
def test_video_plus_music(
    fake_probe_result: ProbeResult, default_options: RenderOptions, tmp_workspace: Path
) -> None:
    cmd = _build(
        RenderInputs(video="input.mp4", music="music.mp3"),
        fake_probe_result,
        default_options,
        tmp_workspace,
    )
    assert cmd == HEAD + [
        "-i",
        "input.mp4",
        "-stream_loop",
        "-1",
        "-i",
        "music.mp3",
        "-filter_complex",
        # Nhạc nền thay hẳn tiếng gốc (mặc định) -> không amix, không đụng [0:a].
        "[1:a]volume=0.18,afade=t=in:st=0:d=2,afade=t=out:st=191.2:d=3,"
        "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo[a]",
        "-map",
        "0:v:0",
        "-map",
        "[a]",
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-ar",
        "48000",
        "-movflags",
        "+faststart",
        "-t",
        "194.2",
        "output.mp4",
    ]


# --------------------------------------------------------------------------- #
# 4/6 — đủ ba thành phần (giống lệnh mẫu SPEC §5.3)
# --------------------------------------------------------------------------- #
def test_all_three(
    fake_probe_result: ProbeResult, default_options: RenderOptions, tmp_workspace: Path
) -> None:
    cmd = _build(
        RenderInputs(video="input.mp4", music="music.mp3", subs="subs.srt"),
        fake_probe_result,
        default_options,
        tmp_workspace,
    )
    assert cmd == HEAD + [
        "-i",
        "input.mp4",
        "-stream_loop",
        "-1",
        "-i",
        "music.mp3",
        "-filter_complex",
        f"[0:v]subtitles=subs.srt:fontsdir={FONTS}:force_style='{STYLE}'[v]"
        ";[1:a]volume=0.18,afade=t=in:st=0:d=2,afade=t=out:st=191.2:d=3,"
        "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo[a]",
        "-map",
        "[v]",
        "-map",
        "[a]",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "23",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-ar",
        "48000",
        "-movflags",
        "+faststart",
        "-t",
        "194.2",
        "output.mp4",
    ]


# --------------------------------------------------------------------------- #
# 5/6 — video KHÔNG có audio track + nhạc -> phải chèn anullsrc
# --------------------------------------------------------------------------- #
def test_no_audio_track_uses_anullsrc(
    fake_probe_result: ProbeResult, default_options: RenderOptions, tmp_workspace: Path
) -> None:
    silent = dataclasses.replace(fake_probe_result, has_audio=False, audio_codec=None)
    # anullsrc chỉ cần khi thật sự phải TRỘN tiếng gốc vào nhạc; mặc định nhạc
    # thay hẳn tiếng gốc nên phải khai rõ original_volume mới vào nhánh này.
    opts = RenderOptions.model_validate({"music": {"original_volume": 1.0}})
    cmd = _build(
        RenderInputs(video="input.mp4", music="music.mp3"),
        silent,
        opts,
        tmp_workspace,
    )
    assert cmd == HEAD + [
        "-i",
        "input.mp4",
        "-stream_loop",
        "-1",
        "-i",
        "music.mp3",
        "-f",
        "lavfi",
        "-i",
        "anullsrc=channel_layout=stereo:sample_rate=48000",
        "-filter_complex",
        "[2:a]volume=1,aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo[a0];"
        "[1:a]volume=0.18,afade=t=in:st=0:d=2,afade=t=out:st=191.2:d=3,"
        "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo[a1];"
        "[a0][a1]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[a]",
        "-map",
        "0:v:0",
        "-map",
        "[a]",
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-ar",
        "48000",
        "-movflags",
        "+faststart",
        "-t",
        "194.2",
        "output.mp4",
    ]


def test_no_audio_no_music_has_no_audio_output(
    fake_probe_result: ProbeResult, default_options: RenderOptions, tmp_workspace: Path
) -> None:
    silent = dataclasses.replace(fake_probe_result, has_audio=False, audio_codec=None)
    cmd = _build(RenderInputs(video="input.mp4"), silent, default_options, tmp_workspace)
    assert "-c:a" not in cmd
    assert "0:a:0" not in cmd
    assert cmd[-1] == "output.mp4"


# --------------------------------------------------------------------------- #
# 6/6 — fast-path copy khi tắt phụ đề dù có file .srt
# --------------------------------------------------------------------------- #
def test_fast_path_copy_when_subtitle_disabled(
    fake_probe_result: ProbeResult, tmp_workspace: Path
) -> None:
    opts = RenderOptions.model_validate({"subtitle": {"enabled": False}})
    cmd = _build(
        RenderInputs(video="input.mp4", subs="subs.srt", music="music.mp3"),
        fake_probe_result,
        opts,
        tmp_workspace,
    )
    assert "-c:v" in cmd and cmd[cmd.index("-c:v") + 1] == "copy"
    assert "subtitles=" not in " ".join(cmd)
    assert "-pix_fmt" not in cmd

def test_copy_kept_when_resolution_matches_the_file(fake_probe_result: ProbeResult) -> None:
    """Xin đúng kích thước đang có = không phải đổi gì -> vẫn copy được.

    Hay gặp sau bước ghép clip: bước đó đã scale sẵn về ``output.resolution``,
    ép encode lại lần nữa chỉ tổ mất chất lượng và thời gian.
    """
    opts = RenderOptions.model_validate({"output": {"resolution": "1920x1080"}})
    assert can_copy_video(fake_probe_result, opts, has_subs=False) is True


def test_copy_dropped_when_resolution_really_differs(
    fake_probe_result: ProbeResult,
) -> None:
    opts = RenderOptions.model_validate({"output": {"resolution": "1280x720"}})
    assert can_copy_video(fake_probe_result, opts, has_subs=False) is False


def test_copy_kept_when_fps_matches_the_file(fake_probe_result: ProbeResult) -> None:
    opts = RenderOptions.model_validate({"output": {"fps": 30}})
    assert can_copy_video(fake_probe_result, opts, has_subs=False) is True


def test_copy_dropped_when_fps_really_differs(fake_probe_result: ProbeResult) -> None:
    opts = RenderOptions.model_validate({"output": {"fps": 24}})
    assert can_copy_video(fake_probe_result, opts, has_subs=False) is False


def test_redundant_scale_filter_is_not_emitted(
    fake_probe_result: ProbeResult, tmp_workspace: Path
) -> None:
    # resolution trùng kích thước thật -> không có filter scale nào cả.
    opts = RenderOptions.model_validate(
        {"output": {"resolution": "1920x1080"}, "subtitle": {"enabled": False}}
    )
    cmd = _build(RenderInputs(video="input.mp4"), fake_probe_result, opts, tmp_workspace)
    assert "scale=" not in " ".join(cmd)
    assert "-c:v" in cmd and cmd[cmd.index("-c:v") + 1] == "copy"
