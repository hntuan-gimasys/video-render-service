"""Dựng argv cho FFmpeg — phần THUẦN của docs/SPEC.md §5.

Tách khỏi ``ffmpeg_runner`` để giữ mỗi file dưới 400 dòng: ở đây không có I/O,
không subprocess, chỉ biến đổi dữ liệu -> test được bằng snapshot argv.
``ffmpeg_runner`` re-export lại toàn bộ tên công khai nên hợp đồng trong stub
(``build_ffmpeg_command`` nằm trong ffmpeg_runner) vẫn đúng.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final

from app.ffmpeg_audio import (
    ANULLSRC,
    build_audio_filter,
    resolve_original_volume,
)
from app.models import RenderOptions
from app.probe_data import ProbeResult, parse_probe_json
from app.subtitles import (
    ASS_PLAY_RES_Y,
    SUBS_ASS_NAME,
    SUBS_SRT_NAME,
    build_force_style,
)

# Tên file cố định trong workspace. ffmpeg luôn chạy với cwd = workspace nên
# filter subtitles= chỉ thấy tên tương đối thuần chữ -> không phải escape
# ":", "'", "\" (SPEC §5.3.4).
INPUT_STEM: Final[str] = "input"
MUSIC_STEM: Final[str] = "music"
# Giữ SUBS_NAME cho tương thích; tên thật do subtitles.normalize_subtitle quyết
# định (.srt hay .ass tuỳ định dạng client gửi lên — SPEC §3.1 nhận cả hai).
SUBS_NAME: Final[str] = SUBS_SRT_NAME

# Encoder sinh ra codec nào. Fast path -c:v copy chỉ hợp lệ khi codec sẵn có
# trong file input ĐÚNG BẰNG codec được yêu cầu ở output — SPEC §5.1 ghi rõ điều
# kiện là "không đổi resolution/fps/codec". Thiếu phép so này thì client xin
# libx265 trên input h264 vẫn nhận về h264 mà không có cảnh báo nào.
_ENCODER_FOR_CODEC: Final[dict[str, str]] = {
    "h264": "libx264",
    "hevc": "libx265",
}
# fps đọc từ ffprobe là phân số (30000/1001) nên so bằng dấu bằng là hỏng.
_FPS_TOLERANCE: Final[float] = 0.01

__all__ = [
    "ProbeResult",
    "RenderInputs",
    "parse_probe_json",
    "build_ffmpeg_command",
    "can_copy_video",
    "escape_filter_value",
    "resolve_canvas_size",
    "resolve_original_volume",
    "INPUT_STEM",
    "MUSIC_STEM",
    "SUBS_NAME",
    "SUBS_ASS_NAME",
    "SUBS_SRT_NAME",
]


def escape_filter_value(value: str) -> str:
    """Escape một giá trị tuỳ ý để nhét vào option của filter_complex.

    ``:`` phân tách các option trong một filter nên đường dẫn tuyệt đối kiểu
    Windows (``C:\\Users\\...``) sẽ phá vỡ cú pháp. Cách duy nhất chạy đúng
    (đã thử với ffmpeg thật): bọc nháy đơn **và** escape ``:`` thành ``\\:``.
    Dấu nháy đơn bên trong phải đóng nháy - escape - mở nháy lại (``'\\''``)
    vì ``\\'`` bên trong vùng nháy đơn không được ffmpeg hiểu.

    Giá trị không chứa ký tự đặc biệt thì trả về nguyên trạng, để lệnh sinh ra
    trên Linux (``/app/fonts``) vẫn giống hệt ví dụ trong SPEC §5.3.
    """
    if not any(char in value for char in ":'\\"):
        return value
    # ffmpeg trên Windows hiểu cả dấu / nên đổi hết về / cho đơn giản.
    escaped = value.replace("\\", "/").replace(":", r"\:")
    escaped = escaped.replace("'", r"'\''")
    return f"'{escaped}'"



@dataclass(frozen=True, slots=True)
class RenderInputs:
    """Các file đã nằm sẵn trong workspace (đường dẫn tương đối)."""

    video: str
    music: str | None = None
    subs: str | None = None
    music_duration: float | None = None
    # True = ``subs`` là file .ass do service tự sinh, đã có sẵn style và
    # PlayRes đúng khung hình -> KHÔNG kèm force_style (xem app/overlay.py).
    subs_pre_styled: bool = False
    # ``PlayResY`` của file .ass do người dùng đưa lên, để quy đổi force_style.
    play_res_y: int = ASS_PLAY_RES_Y
    # File .ass phụ chồng thêm (text bìa) khi phụ đề chính không do ta sinh.
    overlay: str | None = None


def can_copy_video(probe_result: ProbeResult, opts: RenderOptions, has_subs: bool) -> bool:
    """Fast path §5.1: chỉ copy khi không phải encode lại vì bất cứ lý do gì."""
    output = opts.output
    if not output.copy_video_if_possible:
        return False
    if has_subs and opts.subtitle.enabled and opts.subtitle.mode == "burn":
        return False  # hardsub buộc phải vẽ lại từng frame
    # Xin đúng kích thước/fps mà file đang có thì chẳng phải đổi gì cả. Hay gặp
    # sau bước ghép clip: bước đó đã scale sẵn về output.resolution rồi, ép
    # encode lại lần nữa chỉ tổ mất chất lượng và thời gian.
    if resolve_canvas_size(probe_result, opts) != (probe_result.width, probe_result.height):
        return False
    if output.fps is not None and abs(output.fps - probe_result.fps) > _FPS_TOLERANCE:
        return False
    # Không biết codec input thì không dám copy (copy VP9 vào .mp4 ra file hỏng),
    # và codec input phải trùng đúng codec output đang được yêu cầu.
    return _ENCODER_FOR_CODEC.get(probe_result.video_codec or "") == output.video_codec


def build_ffmpeg_command(
    inputs: RenderInputs,
    probe_result: ProbeResult,
    opts: RenderOptions,
    workspace: Path,
    *,
    threads: int = 0,
    fonts_dir: str | None = None,
) -> list[str]:
    """Trả về argv đầy đủ cho ffmpeg. Hàm thuần — không chạy gì, không I/O.

    ``workspace`` chỉ dùng để dựng đường dẫn output; ffmpeg sẽ được chạy với
    ``cwd=workspace`` nên mọi input/output trong argv là tên tương đối.
    """
    sub = opts.subtitle
    music_opts = opts.music
    out = opts.output

    burn_subs = bool(inputs.subs) and sub.enabled and sub.mode == "burn"
    soft_subs = bool(inputs.subs) and sub.enabled and sub.mode == "soft"
    use_music = bool(inputs.music) and music_opts.enabled
    # Text bìa cũng là chữ vẽ đè lên hình -> chặn luôn fast path copy.
    copy_video = not inputs.overlay and can_copy_video(
        probe_result, opts, bool(inputs.subs)
    )

    cmd: list[str] = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "error",
        "-progress",
        "pipe:1",
        "-nostats",
    ]
    if threads > 0:
        cmd += ["-threads", str(threads)]

    # ---------------- inputs ----------------
    cmd += ["-i", inputs.video]
    index = 1
    music_index: int | None = None
    silence_index: int | None = None
    subs_index: int | None = None

    if use_music:
        if music_opts.loop:
            cmd += ["-stream_loop", "-1"]
        if music_opts.start_offset > 0:
            cmd += ["-ss", _fmt(music_opts.start_offset)]
        cmd += ["-i", inputs.music or ""]
        music_index = index
        index += 1

    # Video không có audio track mà vẫn phải trộn tiếng gốc vào: amix sẽ lỗi
    # nếu thiếu luồng -> chèn nguồn im lặng đóng vai tiếng gốc. Khi nhạc thay
    # hẳn tiếng gốc thì chẳng có gì để trộn, khỏi cần luồng này.
    mixes_original = resolve_original_volume(music_opts, use_music) > 0
    if use_music and not probe_result.has_audio and mixes_original:
        cmd += ["-f", "lavfi", "-i", ANULLSRC]
        silence_index = index
        index += 1

    if soft_subs:
        cmd += ["-i", inputs.subs or ""]
        subs_index = index
        index += 1

    # ---------------- filter graph ----------------
    video_chain = _build_video_filter(inputs, probe_result, opts, burn_subs, fonts_dir)
    audio_chain, audio_label = build_audio_filter(
        inputs, probe_result, opts, use_music, music_index, silence_index
    )
    filter_parts = [part for part in (video_chain, audio_chain) if part]

    if filter_parts:
        cmd += ["-filter_complex", ";".join(filter_parts)]

    # ---------------- mapping ----------------
    cmd += ["-map", "[v]"] if video_chain else ["-map", "0:v:0"]
    if audio_label:
        cmd += ["-map", audio_label]
    elif probe_result.has_audio:
        cmd += ["-map", "0:a:0"]
    if subs_index is not None:
        cmd += ["-map", f"{subs_index}:s:0"]

    # ---------------- codec ----------------
    if copy_video and not video_chain:
        cmd += ["-c:v", "copy"]
    else:
        cmd += ["-c:v", out.video_codec, "-preset", out.preset, "-crf", str(out.crf)]
        # yuv420p bắt buộc, thiếu là Safari/QuickTime không phát được (§5.3.6).
        cmd += ["-pix_fmt", "yuv420p"]
        if out.fps is not None:
            cmd += ["-r", _fmt(out.fps)]

    has_audio_output = audio_label is not None or probe_result.has_audio
    if has_audio_output:
        cmd += ["-c:a", out.audio_codec, "-b:a", out.audio_bitrate, "-ar", "48000"]
    if subs_index is not None:
        cmd += ["-c:s", "mov_text"]

    # ---------------- muxer ----------------
    if out.faststart:
        cmd += ["-movflags", "+faststart"]
    if use_music:
        # Nhạc lặp vô hạn (hoặc anullsrc vô hạn) -> phải chặn độ dài output.
        # KHÔNG dùng -shortest như ví dụ SPEC §5.3.3: -shortest cắt theo stream
        # NGẮN NHẤT, mà stream phụ đề mov_text (§5.3.8) kết thúc ngay sau dòng
        # cuối -> đã đo bằng ffmpeg thật: video 6s bị cắt còn 2s. -t không có
        # lỗi đó và cho đúng độ dài ở mọi tổ hợp đã thử.
        if probe_result.duration > 0:
            cmd += ["-t", _fmt(probe_result.duration)]
        else:
            # Không probe được duration -> đành dựa vào -shortest.
            cmd += ["-shortest"]

    cmd.append(out.filename)
    return cmd


def resolve_canvas_size(probe_result: ProbeResult, opts: RenderOptions) -> tuple[int, int]:
    """Kích thước khung mà libass sẽ vẽ lên (SAU khi scale, nếu client đổi
    resolution) — dùng để tính cỡ chữ, margin, và số ký tự vừa một dòng.

    Tách khỏi ``_build_video_filter`` để ``app.jobs`` tính wrap theo đúng
    cùng một khung hình, không phải tính lại logic này lần thứ hai.
    """
    if opts.output.resolution is not None:
        width, height = opts.output.resolution.split("x", 1)
        return int(width), int(height)
    return probe_result.width, probe_result.height


def _build_video_filter(
    inputs: RenderInputs,
    probe_result: ProbeResult,
    opts: RenderOptions,
    burn_subs: bool,
    fonts_dir: str | None,
) -> str:
    """Chuỗi filter video, rỗng nếu không cần xử lý gì (giữ đường copy)."""
    steps: list[str] = []
    # Kích thước khung mà libass sẽ vẽ lên: nếu client đổi resolution thì filter
    # scale chạy TRƯỚC subtitles, nên cỡ chữ phải tính theo khung đã scale.
    canvas_width, canvas_height = resolve_canvas_size(probe_result, opts)
    if (canvas_width, canvas_height) != (probe_result.width, probe_result.height):
        # Xin đúng kích thước đang có (hay gặp sau bước ghép clip, vì bước đó
        # đã scale sẵn về output.resolution) thì scale lại chỉ tốn công.
        steps.append(f"scale={canvas_width}:{canvas_height}")
    if burn_subs and inputs.subs:
        steps.append(_subtitles_filter(inputs.subs, fonts_dir))
        if not inputs.subs_pre_styled:
            # File .ass do service sinh đã mang sẵn style đúng pixel; chỉ file
            # của người dùng mới cần ghi đè, và phải quy đổi theo PlayResY của
            # chính file đó.
            style = build_force_style(
                opts.subtitle, canvas_width, canvas_height, inputs.play_res_y
            )
            steps[-1] += f":force_style='{style}'"
    if inputs.overlay:
        # Lớp text bìa nằm trong file .ass riêng, vẽ sau nên luôn nằm trên.
        steps.append(_subtitles_filter(inputs.overlay, fonts_dir))
    if not steps:
        return ""
    return f"[0:v]{','.join(steps)}[v]"


def _subtitles_filter(name: str, fonts_dir: str | None) -> str:
    """``subtitles=<file>[:fontsdir=...]``.

    ``name`` luôn là tên tương đối thuần chữ trong workspace (subs.srt,
    styled.ass...) nên không cần escape; ``fonts_dir`` thì có thể là đường dẫn
    tuyệt đối chứa ':' và '\\' -> buộc phải escape.
    """
    parts = [f"subtitles={name}"]
    if fonts_dir:
        parts.append(f"fontsdir={escape_filter_value(fonts_dir)}")
    return ":".join(parts)


def _fmt(value: float) -> str:
    """Số cho argv/filter: bỏ '.0' cho gọn và khớp snapshot test.

    Không dùng ``%g``: từ 1e6 trở lên nó chuyển sang ký hiệu khoa học
    (``1.23457e+06``) và ffmpeg không parse được giá trị đó.
    """
    if float(value).is_integer():
        return str(int(value))
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    return text or "0"
