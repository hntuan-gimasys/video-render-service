"""Dựng file ASS đầy đủ với PlayRes = ĐÚNG kích thước khung hình.

Vì sao phải tự sinh ASS thay vì để ffmpeg tự chuyển .srt:
ffmpeg hardcode ``PlayResX: 384, PlayResY: 288`` khi chuyển SRT sang ASS. Với
video dọc 720x1280 thì hệ toạ độ ảo đó không khớp khung thật, nên mọi giá trị
tính theo đơn vị ASS (FontSize, Margin, Outline) phải quy đổi vòng vèo và
ngưỡng tự xuống dòng của libass trở nên khó đoán.

Đặt ``PlayResX/PlayResY`` đúng bằng khung hình thì 1 đơn vị ASS = 1 pixel:
cỡ chữ, margin, viền đều là số pixel thật; libass vẽ chữ ở đúng độ phân giải
output nên nét nhất có thể (chữ là vector, không hề upscale từ bitmap).

``ScaleX``/``ScaleY`` luôn ghi 100/100 và mọi hiệu ứng phóng to đều dùng
``\fscx`` = ``\fscy`` -> tỉ lệ ngang/dọc không bao giờ lệch nhau, chữ không méo.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from app.subtitle_style import hex_to_ass_color

__all__ = [
    "AssStyle",
    "AssEvent",
    "format_ass_time",
    "style_colour",
    "num",
    "build_ass_document",
    "STYLE_FORMAT",
    "EVENT_FORMAT",
]

STYLE_FORMAT: Final[str] = (
    "Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
    "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, "
    "Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding"
)
EVENT_FORMAT: Final[str] = (
    "Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"
)


def style_colour(hex_color: str) -> str:
    """``#RRGGBB`` -> ``&H00BBGGRR`` (dòng Style không có ``&`` đóng)."""
    return hex_to_ass_color(hex_color).rstrip("&")


def format_ass_time(seconds: float) -> str:
    """``1.5`` -> ``'0:00:01.50'`` (ASS dùng centisecond, giờ không đệm 0)."""
    centis_total = int(round(max(0.0, seconds) * 100))
    hours, remainder = divmod(centis_total, 360_000)
    minutes, remainder = divmod(remainder, 6_000)
    secs, centis = divmod(remainder, 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{centis:02d}"


def _flag(value: bool) -> str:
    """ASS dùng -1 cho bật, 0 cho tắt (không phải 1/0)."""
    return "-1" if value else "0"


def num(value: float) -> str:
    """Số cho file ASS: bỏ ".0" thừa, làm tròn 2 chữ số thập phân."""
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.2f}".rstrip("0").rstrip(".")


_num = num


@dataclass(frozen=True, slots=True)
class AssStyle:
    """Một dòng ``Style:`` — mọi số đo đã là PIXEL vì PlayRes = khung hình."""

    name: str
    font_name: str
    font_px: float
    primary: str = "#FFFFFF"
    outline_color: str = "#000000"
    back_color: str = "#80000000"
    # Màu "chưa tô" của karaoke. Hiệu ứng typewriter đặt alpha = FF (trong
    # suốt) để ký tự chưa tới lượt bị ẩn hẳn thay vì đổi màu.
    secondary: str = "#000000FF"
    bold: bool = True
    italic: bool = False
    border_style: int = 1
    outline: float = 2.0
    shadow: float = 0.0
    alignment: int = 2
    margin_l: int = 0
    margin_r: int = 0
    margin_v: int = 0
    spacing: float = 0.0

    def to_line(self) -> str:
        fields = [
            self.name,
            self.font_name,
            _num(self.font_px),
            style_colour(self.primary),
            style_colour(self.secondary),
            style_colour(self.outline_color),
            style_colour(self.back_color),
            _flag(self.bold),
            _flag(self.italic),
            "0",  # Underline
            "0",  # StrikeOut
            "100",  # ScaleX — luôn 100, không bao giờ bóp ngang
            "100",  # ScaleY — luôn 100, không bao giờ bóp dọc
            _num(self.spacing),
            "0",  # Angle
            str(self.border_style),
            _num(self.outline),
            _num(self.shadow),
            str(self.alignment),
            str(self.margin_l),
            str(self.margin_r),
            str(self.margin_v),
            "1",  # Encoding: 1 = Default, libass tự nhận UTF-8
        ]
        return "Style: " + ",".join(fields)


@dataclass(frozen=True, slots=True)
class AssEvent:
    r"""Một dòng ``Dialogue:``. ``text`` đã gồm override tag và ``\N``."""

    start: float
    end: float
    style: str
    text: str
    layer: int = 0

    def to_line(self) -> str:
        return (
            f"Dialogue: {self.layer},{format_ass_time(self.start)},"
            f"{format_ass_time(self.end)},{self.style},,0,0,0,,{self.text}"
        )


def build_ass_document(
    width: int,
    height: int,
    styles: list[AssStyle],
    events: list[AssEvent],
    *,
    wrap_style: int = 0,
) -> str:
    """Ghép thành nội dung file .ass hoàn chỉnh.

    ``wrap_style=0`` là kiểu "smart wrap" của libass: nó chỉ có tác dụng như
    lưới an toàn, vì phần chữ đã được tự ngắt dòng trước (xem
    ``app.subtitle_wrap``). Giữ nó bật để câu nào lỡ dài hơn ước lượng thì vẫn
    tự xuống dòng chứ không tràn ra ngoài khung.
    """
    lines: list[str] = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        f"WrapStyle: {wrap_style}",
        # yes = viền/bóng co giãn cùng hệ toạ độ. PlayRes đã bằng khung hình
        # nên hệ số co giãn là 1: giá trị ghi ra đúng bằng pixel thật.
        "ScaledBorderAndShadow: yes",
        "YCbCr Matrix: TV.709",
        "",
        "[V4+ Styles]",
        f"Format: {STYLE_FORMAT}",
    ]
    lines += [style.to_line() for style in styles]
    lines += ["", "[Events]", f"Format: {EVENT_FORMAT}"]
    lines += [event.to_line() for event in events]
    return "\n".join(lines) + "\n"
