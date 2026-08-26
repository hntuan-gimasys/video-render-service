"""Quy đổi option phụ đề thành số đo THẬT (pixel) và chuỗi force_style.

Mốc quy chiếu duy nhất là PIXEL của khung hình output:

* File .ass do service tự sinh (đường đi chính, xem app/overlay.py) có
  ``PlayResX``/``PlayResY`` đúng bằng khung hình -> 1 đơn vị ASS = 1 pixel,
  dùng thẳng các hàm ``resolve_*_px`` bên dưới.
* File .ass do người dùng tự đưa lên thì giữ nguyên style của họ, ta chỉ ghi đè
  bằng ``force_style``; lúc đó phải quy đổi pixel sang hệ toạ độ riêng của file
  đó (``PlayResY`` của chính nó, mặc định 288 giống ffmpeg) — việc mà
  ``build_force_style`` làm.

Nhờ vậy ``font_size``/``margin_*``/``outline`` trong API luôn có CÙNG một ý
nghĩa (pixel) dù phụ đề vào bằng đường nào.
"""

from __future__ import annotations

import re
from typing import Final

from app.models import SubtitleOptions

# ffmpeg hardcode giá trị này khi tự chuyển .srt sang ASS, và libass cũng lấy
# nó làm mặc định cho file ASS thiếu PlayResY.
ASS_PLAY_RES_Y: Final[int] = 288
# Cỡ chữ và lề đáy (đơn vị ASS) dùng khi không biết kích thước video — giữ
# đúng giá trị mặc định cũ để hành vi không đổi ở ca không probe được.
_FALLBACK_FONT_SIZE: Final[int] = 24
_FALLBACK_MARGIN_V: Final[int] = 40
# Viền chữ mặc định = 8% cỡ chữ, đủ đọc trên nền sáng mà không bệt.
_OUTLINE_RATIO: Final[float] = 0.08

__all__ = [
    "ASS_PLAY_RES_Y",
    "hex_to_ass_color",
    "resolve_font_px",
    "resolve_margin_h_px",
    "resolve_margin_v_px",
    "resolve_outline_px",
    "resolve_font_size",
    "resolve_margin_h",
    "resolve_margin_v",
    "resolve_outline",
    "build_force_style",
    "sanitise_font_name",
]


def hex_to_ass_color(hex_color: str) -> str:
    """``#RRGGBB`` / ``#AARRGGBB`` -> ``&HAABBGGRR&`` (ASS đảo thứ tự RGB).

    >>> hex_to_ass_color("#FFFFFF")
    '&H00FFFFFF&'
    >>> hex_to_ass_color("#FF8800")
    '&H000088FF&'

    Alpha giữ nguyên byte đầu vào (ASS: 00 = đục, FF = trong suốt), thiếu alpha
    thì mặc định ``00`` = đục hoàn toàn.
    """
    value = hex_color.strip().lstrip("#").lstrip("&Hh").rstrip("&")
    if len(value) == 6:
        alpha = "00"
        rgb = value
    elif len(value) == 8:
        alpha = value[:2]
        rgb = value[2:]
    else:
        raise ValueError(f"Màu không hợp lệ: {hex_color!r}")

    try:
        int(value, 16)
    except ValueError as exc:
        raise ValueError(f"Màu không hợp lệ: {hex_color!r}") from exc

    red, green, blue = rgb[0:2], rgb[2:4], rgb[4:6]
    return f"&H{alpha}{blue}{green}{red}&".upper()


def sanitise_font_name(name: str) -> str:
    """Bỏ ký tự phá cú pháp force_style / dòng Style (dấu phẩy, hai chấm, nháy)."""
    cleaned = name.replace("'", "")
    return re.sub(r"[,:\s]+", " ", cleaned).strip()


# --------------------------------------------------------------------------- #
# Số đo thật, tính bằng pixel của khung hình output
# --------------------------------------------------------------------------- #
def resolve_font_px(opts: SubtitleOptions, width: int = 0, height: int = 0) -> float:
    """Chiều cao chữ tính bằng pixel.

    Cỡ chữ tự động bám theo BỀ NGANG chứ không phải chiều cao: bề ngang mới là
    cạnh quyết định một dòng phụ đề có vừa khung hay không. Bám chiều cao thì
    video dọc 1080x1920 ra chữ to gần gấp đôi video ngang cùng bề ngang.
    """
    if opts.font_size is not None:
        return float(opts.font_size)
    if width <= 0 or height <= 0:
        # Không biết khung hình: quy _FALLBACK_FONT_SIZE (đơn vị ASS) về pixel.
        return float(_FALLBACK_FONT_SIZE) * height / ASS_PLAY_RES_Y if height > 0 else 0.0
    return width * opts.font_size_ratio


def resolve_margin_h_px(opts: SubtitleOptions, width: int = 0) -> int:
    """Lề trái/phải tính bằng pixel, tự co theo bề ngang."""
    if opts.margin_h is not None:
        return opts.margin_h
    if width <= 0:
        return 0
    return round(width * opts.margin_h_ratio)


def resolve_margin_v_px(opts: SubtitleOptions, height: int = 0) -> int:
    """Lề đáy (hoặc đỉnh) tính bằng pixel, tự co theo chiều cao."""
    if opts.margin_v is not None:
        return opts.margin_v
    if height <= 0:
        return 0
    return round(height * opts.margin_v_ratio)


def resolve_outline_px(opts: SubtitleOptions, font_px: float) -> float:
    """Độ dày viền chữ tính bằng pixel, mặc định co theo cỡ chữ."""
    if opts.outline is not None:
        return float(opts.outline)
    return round(font_px * _OUTLINE_RATIO, 2)


# --------------------------------------------------------------------------- #
# Quy đổi sang đơn vị ASS của file phụ đề người dùng tự đưa lên
# --------------------------------------------------------------------------- #
def _to_ass_units(px: float, height: int, play_res_y: int) -> float:
    """Pixel -> đơn vị của file ASS có ``PlayResY = play_res_y``.

    libass co giãn mọi số đo theo ``chiều_cao_video / PlayResY``, nên muốn ra
    đúng ``px`` pixel thì phải ghi ``px × PlayResY / chiều_cao_video``.
    """
    if height <= 0:
        return px
    return px * play_res_y / height


def resolve_font_size(
    opts: SubtitleOptions, width: int = 0, height: int = 0, play_res_y: int = ASS_PLAY_RES_Y
) -> float:
    """Cỡ chữ theo đơn vị ASS của file có ``PlayResY = play_res_y``."""
    if width <= 0 or height <= 0:
        return float(_FALLBACK_FONT_SIZE)
    return _to_ass_units(resolve_font_px(opts, width, height), height, play_res_y)


def resolve_margin_h(
    opts: SubtitleOptions, width: int = 0, height: int = 0, play_res_y: int = ASS_PLAY_RES_Y
) -> int:
    """Lề ngang theo đơn vị ASS của file có ``PlayResY = play_res_y``."""
    if width <= 0 or height <= 0:
        return opts.margin_h if opts.margin_h is not None else 0
    return round(_to_ass_units(resolve_margin_h_px(opts, width), height, play_res_y))


def resolve_margin_v(
    opts: SubtitleOptions, height: int = 0, play_res_y: int = ASS_PLAY_RES_Y
) -> int:
    """Lề dọc theo đơn vị ASS của file có ``PlayResY = play_res_y``."""
    if height <= 0:
        return opts.margin_v if opts.margin_v is not None else _FALLBACK_MARGIN_V
    return round(_to_ass_units(resolve_margin_v_px(opts, height), height, play_res_y))


def resolve_outline(opts: SubtitleOptions, font_size: float) -> float:
    """Độ dày viền theo cùng đơn vị với ``font_size`` truyền vào."""
    if opts.outline is not None:
        return float(opts.outline)
    return round(font_size * _OUTLINE_RATIO, 2)


def build_force_style(
    opts: SubtitleOptions,
    width: int = 0,
    height: int = 0,
    play_res_y: int = ASS_PLAY_RES_Y,
) -> str:
    """Chuỗi ``force_style`` cho filter ``subtitles=`` — chỉ dùng cho file ASS
    do NGƯỜI DÙNG đưa lên (file service tự sinh đã có style sẵn, xem
    app/overlay.py).

    ``play_res_y`` là ``PlayResY`` đọc được từ chính file đó; mặc định 288 là
    giá trị libass/ffmpeg dùng khi file không khai.

    Dấu phẩy phân tách các cặp key=value nên không được để lọt dấu phẩy hay
    dấu nháy từ input người dùng (font_name là chỗ duy nhất có thể).
    """
    font_size = resolve_font_size(opts, width, height, play_res_y)
    if width > 0 and height > 0:
        outline_px = resolve_outline_px(opts, resolve_font_px(opts, width, height))
        outline = round(_to_ass_units(outline_px, height, play_res_y), 2)
        shadow = round(_to_ass_units(opts.shadow, height, play_res_y), 2)
    else:
        outline = resolve_outline(opts, font_size)
        shadow = opts.shadow
    parts: list[str] = [
        f"FontName={sanitise_font_name(opts.font_name)}",
        f"FontSize={_num(font_size)}",
        f"PrimaryColour={hex_to_ass_color(opts.primary_color)}",
        f"OutlineColour={hex_to_ass_color(opts.outline_color)}",
        f"BorderStyle={opts.border_style}",
        f"Outline={_num(outline)}",
        f"Shadow={_num(shadow)}",
        f"Alignment={opts.alignment}",
        f"MarginV={resolve_margin_v(opts, height, play_res_y)}",
        f"MarginL={resolve_margin_h(opts, width, height, play_res_y)}",
        f"MarginR={resolve_margin_h(opts, width, height, play_res_y)}",
        f"Bold={-1 if opts.bold else 0}",
        f"Italic={-1 if opts.italic else 0}",
    ]
    # BorderStyle=4 vẽ hộp nền -> lúc đó BackColour mới có tác dụng.
    if opts.border_style == 4:
        parts.insert(4, f"BackColour={hex_to_ass_color(opts.back_color)}")
    return ",".join(parts)


def _num(value: float) -> str:
    """2.0 -> '2' để chuỗi style gọn và khớp snapshot test."""
    if float(value).is_integer():
        return str(int(value))
    # Làm tròn 2 số: cỡ chữ tự tính hay ra số lẻ dài (20.4999999997).
    return f"{value:.2f}".rstrip("0").rstrip(".")
