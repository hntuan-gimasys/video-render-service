"""Nhạc nền: thay tiếng gốc, trộn, ducking — docs/SPEC.md §4.5, §5.3.1.

Tách khỏi tests/test_ffmpeg_branches.py để mỗi file test dưới 400 dòng.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

from app.ffmpeg_runner import ProbeResult, RenderInputs
from app.models import RenderOptions
from tests.helpers import PROBE_OK
from tests.helpers import build as _build


# --------------------------------------------------------------------------- #
# Nhạc nền THAY HẲN tiếng gốc (mặc định)
# --------------------------------------------------------------------------- #
def test_music_replaces_original_audio_by_default(
    fake_probe_result: ProbeResult, default_options: RenderOptions, tmp_workspace: Path
) -> None:
    """Ghép nhạc nền vào là bỏ hẳn tiếng gốc, không trộn chung.

    Video nguồn thường có tiếng gió/tiếng người/tiếng xe, trộn vào chỉ làm bẩn
    nhạc. Không amix nghĩa là cũng nhẹ hơn một filter.
    """
    cmd = _build(
        RenderInputs(video="input.mp4", music="music.mp3"),
        fake_probe_result,
        default_options,
        tmp_workspace,
    )
    argv = " ".join(cmd)
    assert "amix" not in argv
    assert "[0:a]" not in argv  # tiếng gốc không hề được đụng tới
    assert "[1:a]volume=0.18" in argv
    assert cmd[cmd.index("-map") + 1] == "0:v:0"
    assert "[a]" in cmd  # audio output là nhạc


def test_music_without_original_needs_no_silence_source(
    default_options: RenderOptions, tmp_workspace: Path
) -> None:
    # Video câm + nhạc nền: chẳng có gì để trộn nên khỏi cần anullsrc.
    probe_result = dataclasses.replace(PROBE_OK, has_audio=False)
    cmd = _build(
        RenderInputs(video="input.mp4", music="music.mp3"),
        probe_result,
        default_options,
        tmp_workspace,
    )
    assert "anullsrc" not in " ".join(cmd)


def test_explicit_original_volume_brings_the_mix_back(
    fake_probe_result: ProbeResult, tmp_workspace: Path
) -> None:
    opts = RenderOptions.model_validate({"music": {"original_volume": 0.3}})
    argv = " ".join(
        _build(
            RenderInputs(video="input.mp4", music="music.mp3"),
            fake_probe_result,
            opts,
            tmp_workspace,
        )
    )
    assert "[0:a]volume=0.3" in argv
    assert "amix=inputs=2" in argv


def test_no_music_keeps_original_audio_untouched(
    fake_probe_result: ProbeResult, default_options: RenderOptions, tmp_workspace: Path
) -> None:
    """Không có nhạc nền thì tiếng gốc phải nguyên vẹn.

    Đây là lý do ``original_volume`` mặc định là None chứ không phải 0: để mặc
    định thẳng bằng 0 thì video không kèm nhạc cũng bị tắt tiếng oan.
    """
    cmd = _build(
        RenderInputs(video="input.mp4"), fake_probe_result, default_options, tmp_workspace
    )
    argv = " ".join(cmd)
    assert "volume=" not in argv
    assert cmd[cmd.index("-map") + 1] == "0:v:0"
    assert "0:a:0" in cmd
