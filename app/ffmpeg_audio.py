"""Chuỗi filter audio: trộn nhạc nền, fade, ducking — docs/SPEC.md §5.3.1, §4.5.

Tách khỏi ``app/ffmpeg_cmd.py`` để giữ mỗi file dưới 400 dòng. Ở đây vẫn là hàm
thuần: chỉ biến options thành chuỗi filter, không chạy gì và không I/O.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from app.models import MusicOptions, RenderOptions
from app.probe_data import ProbeResult

if TYPE_CHECKING:  # pragma: no cover
    # Chỉ dùng để chú thích kiểu; import thật sẽ tạo vòng với ffmpeg_cmd.
    from app.ffmpeg_cmd import RenderInputs

ANULLSRC: Final[str] = "anullsrc=channel_layout=stereo:sample_rate=48000"
AFORMAT: Final[str] = "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo"

__all__ = ["ANULLSRC", "AFORMAT", "resolve_original_volume", "build_audio_filter"]


def resolve_original_volume(music_opts: MusicOptions, use_music: bool) -> float:
    """Âm lượng thật của tiếng gốc video.

    ``None`` nghĩa là "để service tự quyết": ghép nhạc nền vào thì bỏ hẳn tiếng
    gốc, không ghép thì giữ nguyên. Phân biệt None với 0 là cần thiết — nếu để
    mặc định thẳng bằng 0 thì video KHÔNG có nhạc nền cũng bị tắt tiếng oan.
    """
    if music_opts.original_volume is not None:
        return float(music_opts.original_volume)
    return 0.0 if use_music else 1.0


def build_audio_filter(
    inputs: RenderInputs,
    probe_result: ProbeResult,
    opts: RenderOptions,
    use_music: bool,
    music_index: int | None,
    silence_index: int | None,
) -> tuple[str, str | None]:
    """Chuỗi filter audio + label output ('[a]') hoặc (rỗng, None)."""
    music_opts = opts.music
    original_volume = resolve_original_volume(music_opts, use_music)
    if not use_music or music_index is None:
        # Không trộn nhạc: chỉ đổi âm lượng tiếng gốc nếu khác 1.0.
        if probe_result.has_audio and original_volume != 1.0:
            return (
                f"[0:a]volume={_fmt(original_volume)},{AFORMAT}[a]",
                "[a]",
            )
        return "", None

    music_steps = [f"volume={_fmt(music_opts.volume)}"]
    if music_opts.fade_in > 0:
        music_steps.append(f"afade=t=in:st=0:d={_fmt(music_opts.fade_in)}")
    fade_out_start = _fade_out_start(inputs, probe_result, opts)
    if fade_out_start is not None:
        music_steps.append(
            f"afade=t=out:st={_fmt(fade_out_start)}:d={_fmt(music_opts.fade_out)}"
        )
    music_steps.append(AFORMAT)
    music_chain = f"[{music_index}:a]{','.join(music_steps)}"

    if original_volume <= 0:
        # Nhạc nền thay hẳn tiếng gốc (mặc định): khỏi amix, khỏi cả nguồn im
        # lặng — nhạc đi thẳng ra output. Cũng là lý do ducking bị bỏ qua ở đây,
        # không còn tiếng gốc nào để mà né.
        return f"{music_chain}[a]", "[a]"

    original_src = "[0:a]" if probe_result.has_audio else f"[{silence_index}:a]"
    chains: list[str] = []

    # Ducking cần dùng tiếng gốc hai lần (sidechain + mix) nên phải asplit;
    # một label output của filter chỉ được tiêu thụ đúng một lần.
    if music_opts.ducking:
        chains.append(
            f"{original_src}volume={_fmt(original_volume)},{AFORMAT},"
            f"asplit=2[a0m][a0sc]"
        )
    else:
        chains.append(
            f"{original_src}volume={_fmt(original_volume)},{AFORMAT}[a0]"
        )
    chains.append(f"{music_chain}[a1]")

    if music_opts.ducking:
        chains.append(
            "[a1][a0sc]sidechaincompress=threshold=0.05:ratio=8:attack=20:release=300[duck]"
        )
        chains.append("[a0m][duck]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[a]")
    else:
        # normalize=0: mặc định normalize=1 sẽ chia đôi âm lượng tiếng gốc (§5.3.1).
        chains.append("[a0][a1]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[a]")

    return ";".join(chains), "[a]"


def _fade_out_start(
    inputs: RenderInputs, probe_result: ProbeResult, opts: RenderOptions
) -> float | None:
    """st của afade=t=out. None nếu không fade được (thiếu duration).

    Nhạc lặp -> fade theo độ dài video. Không lặp -> fade theo thời điểm nhạc
    thật sự hết (đã trừ start_offset), nếu không fade sẽ rơi vào đoạn đã im.
    """
    music_opts = opts.music
    if music_opts.fade_out <= 0:
        return None

    reference = probe_result.duration
    if not music_opts.loop and inputs.music_duration:
        playable = max(0.0, inputs.music_duration - music_opts.start_offset)
        if playable > 0:
            reference = min(reference, playable) if reference > 0 else playable
    if reference <= 0:
        return None
    return max(0.0, reference - music_opts.fade_out)


def _fmt(value: float) -> str:
    """Số cho argv/filter: bỏ '.0' cho gọn và khớp snapshot test."""
    if float(value).is_integer():
        return str(int(value))
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    return text or "0"
