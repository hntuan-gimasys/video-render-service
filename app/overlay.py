r"""Sinh file .ass hoàn chỉnh cho lời thoại + text bìa đầu video.

Đây là đường đi CHÍNH của phụ đề: thay vì để ffmpeg tự chuyển .srt sang ASS
với ``PlayResX/Y`` cố định 384x288, service tự dựng file .ass có PlayRes đúng
bằng khung hình. Đổi lại được ba thứ:

* Cỡ chữ, lề, viền khai bằng PIXEL thật -> không phải quy đổi, không sai số.
* Chữ vẽ ở đúng độ phân giải output nên nét nhất có thể, ``ScaleX=ScaleY=100``
  nên không đời nào méo.
* Hiệu ứng chữ (fade/pop/slide/typewriter/glow) chỉ là override tag, chọn được
  ngay trong options mà không đụng tới ffmpeg.

File .ass do người dùng tự đưa lên thì KHÔNG bị dựng lại (giữ style riêng của
họ), lúc đó text bìa được tách thành ``intro.ass`` chồng lên bằng một filter
``subtitles=`` thứ hai.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from app.ass_doc import AssEvent, AssStyle, build_ass_document, num
from app.ass_effects import apply_effect, needs_hidden_secondary
from app.models import IntroTextOptions, RenderOptions, SubtitleOptions
from app.subtitle_style import (
    ASS_PLAY_RES_Y,
    resolve_font_px,
    resolve_margin_h_px,
    resolve_margin_v_px,
    resolve_outline_px,
    sanitise_font_name,
)
from app.subtitle_wrap import estimate_px_width, resolve_line_capacity, wrap_line

__all__ = [
    "BurnPlan",
    "STYLED_ASS_NAME",
    "INTRO_ASS_NAME",
    "DIALOGUE_STYLE",
    "INTRO_STYLE",
    "parse_srt_events",
    "read_play_res_y",
    "plan_burn",
]

STYLED_ASS_NAME: Final[str] = "styled.ass"
INTRO_ASS_NAME: Final[str] = "intro.ass"
DIALOGUE_STYLE: Final[str] = "Loithoai"
INTRO_STYLE: Final[str] = "Bia"
# Alpha FF = trong suốt: ký tự chưa tới lượt của hiệu ứng typewriter.
_HIDDEN: Final[str] = "#FF000000"
# Text bìa neo giữa khối (bàn phím số: 5) rồi ép toạ độ bằng \pos.
_INTRO_ALIGNMENT: Final[int] = 5
# Co chữ text bìa tối đa 50%: nhỏ hơn nữa thì đằng nào cũng không đọc nổi trên
# điện thoại, thà xuống dòng còn hơn.
_MIN_INTRO_SCALE: Final[float] = 0.5
_LINE_BREAK: Final[str] = "\\N"

_SRT_TIMING: Final[re.Pattern[str]] = re.compile(
    r"^\s*(\d{1,3}):([0-5]?\d):([0-5]?\d)[,.](\d{1,3})\s*-->\s*"
    r"(\d{1,3}):([0-5]?\d):([0-5]?\d)[,.](\d{1,3})"
)
_PLAY_RES_Y: Final[re.Pattern[str]] = re.compile(
    r"^\s*PlayResY\s*:\s*(\d+)\s*$", re.IGNORECASE | re.MULTILINE
)


@dataclass(frozen=True, slots=True)
class BurnPlan:
    """Kết quả quyết định phụ đề sẽ đi vào ffmpeg như thế nào."""

    # File phụ đề chính: burn qua filter subtitles=, hoặc map thành stream mềm.
    subs: str | None = None
    # True = file đã có style sẵn (do service sinh) -> KHÔNG kèm force_style.
    pre_styled: bool = False
    # PlayResY của file người dùng, để quy đổi force_style từ pixel sang đúng
    # hệ toạ độ của file đó.
    play_res_y: int = ASS_PLAY_RES_Y
    # File .ass phụ chồng thêm (text bìa), burn bằng một filter subtitles= nữa.
    overlay: str | None = None


def _timestamp(match: re.Match[str], offset: int) -> float:
    hours, minutes, seconds, millis = match.group(offset, offset + 1, offset + 2, offset + 3)
    return (
        int(hours) * 3600
        + int(minutes) * 60
        + int(seconds)
        + int(millis.ljust(3, "0")) / 1000.0
    )


def parse_srt_events(path: Path) -> list[tuple[float, float, list[str]]]:
    """Đọc file SRT ĐÃ chuẩn hoá thành ``[(start, end, [dòng chữ])]``.

    Chỉ chạy sau ``subtitles.normalize_srt`` nên file chắc chắn đúng dạng
    ``số / timing / chữ / dòng trống``; ở đây không cần dò encoding hay vá lỗi.
    """
    events: list[tuple[float, float, list[str]]] = []
    start = end = 0.0
    lines: list[str] = []
    open_block = False

    def _flush() -> None:
        if open_block and lines:
            events.append((start, end, list(lines)))

    for raw in path.read_text(encoding="utf-8").split("\n"):
        match = _SRT_TIMING.match(raw)
        if match is not None:
            _flush()
            start, end = _timestamp(match, 1), _timestamp(match, 5)
            lines, open_block = [], True
            continue
        text = raw.strip()
        if not open_block or not text or text.isdigit():
            continue
        lines.append(text)
    _flush()
    return events


def read_play_res_y(path: Path) -> int:
    """``PlayResY`` khai trong file ASS của người dùng; thiếu -> mặc định 288."""
    try:
        head = path.read_text(encoding="utf-8", errors="replace")[:4000]
    except OSError:
        return ASS_PLAY_RES_Y
    match = _PLAY_RES_Y.search(head)
    if match is None:
        return ASS_PLAY_RES_Y
    value = int(match.group(1))
    return value if value > 0 else ASS_PLAY_RES_Y


# --------------------------------------------------------------------------- #
# Lời thoại
# --------------------------------------------------------------------------- #
def build_dialogue(
    events: list[tuple[float, float, list[str]]],
    opts: SubtitleOptions,
    width: int,
    height: int,
) -> tuple[AssStyle, list[AssEvent]]:
    """Style + danh sách Dialogue cho lời thoại, đã tự xuống dòng theo khung."""
    font_px = resolve_font_px(opts, width, height)
    margin_h = resolve_margin_h_px(opts, width)
    margin_v = resolve_margin_v_px(opts, height)
    style = AssStyle(
        name=DIALOGUE_STYLE,
        font_name=sanitise_font_name(opts.font_name),
        font_px=font_px,
        primary=opts.primary_color,
        outline_color=opts.outline_color,
        back_color=opts.back_color,
        secondary=_HIDDEN if needs_hidden_secondary(opts.effect) else opts.primary_color,
        bold=opts.bold,
        italic=opts.italic,
        border_style=opts.border_style,
        outline=resolve_outline_px(opts, font_px),
        shadow=opts.shadow,
        alignment=opts.alignment,
        margin_l=margin_h,
        margin_r=margin_h,
        margin_v=margin_v,
    )
    max_chars = resolve_line_capacity(font_px, margin_h, width)
    out: list[AssEvent] = []
    for start, end, lines in events:
        # Gộp lại rồi tự chia: dấu ngắt dòng gốc thường do AI đặt khi chưa biết
        # khung hình thật, giữ nguyên thì ra dòng dài dòng ngắn so le.
        joined = " ".join(line for line in lines if line)
        text = _LINE_BREAK.join(wrap_line(joined, max_chars))
        out.append(
            AssEvent(
                start=start,
                end=end,
                style=DIALOGUE_STYLE,
                text=apply_effect(
                    opts.effect,
                    text,
                    duration=max(0.0, end - start),
                    width=width,
                    height=height,
                    font_px=font_px,
                    alignment=opts.alignment,
                    margin_l=margin_h,
                    margin_r=margin_h,
                    margin_v=margin_v,
                ),
            )
        )
    return style, out


# --------------------------------------------------------------------------- #
# Text bìa
# --------------------------------------------------------------------------- #
def split_intro_lines(text: str) -> list[str]:
    """Tách text bìa thành từng dòng, bỏ dòng trống."""
    return [line.strip() for line in text.split("\n") if line.strip()]


def _fit_scale(
    lines: list[str], base_px: float, headline_scale: float, avail_px: int
) -> float:
    """Hệ số co chữ để MỖI dòng text bìa vừa đúng một dòng.

    Text bìa khác lời thoại ở chỗ người dùng đã tự chia dòng theo ý họ khi soạn
    ảnh bìa; tự bẻ dòng thêm sẽ phá bố cục đó. Nên ở đây co nhỏ chữ lại cho vừa
    thay vì xuống dòng — chỉ khi co tới đáy (``_MIN_INTRO_SCALE``) mà vẫn không
    vừa thì mới đành xuống dòng.
    """
    if avail_px <= 0 or base_px <= 0:
        return 1.0
    widest = max(
        (
            estimate_px_width(line, base_px * (headline_scale if index == 0 else 1.0))
            for index, line in enumerate(lines)
        ),
        default=0.0,
    )
    if widest <= avail_px:
        return 1.0
    # Chừa lại 2%: co vừa khít đúng bằng avail_px thì bước xuống dòng phía sau
    # (làm tròn xuống số ký tự mỗi dòng) vẫn bẻ đôi dòng đó vì lệch vài phần
    # nghìn — mất công co chữ mà vẫn xuống dòng.
    return max(_MIN_INTRO_SCALE, avail_px * 0.98 / widest)


def build_intro(
    opts: IntroTextOptions, width: int, height: int
) -> tuple[AssStyle, list[AssEvent]] | None:
    """Style + Dialogue cho text bìa. ``None`` nếu người dùng không nhập gì."""
    if not opts.enabled or not opts.text or not opts.text.strip():
        return None
    lines = split_intro_lines(opts.text)
    if not lines:
        return None

    base_px = float(opts.font_size) if opts.font_size is not None else width * opts.font_size_ratio
    margin_h = opts.margin_h if opts.margin_h is not None else round(width * opts.margin_h_ratio)
    base_px *= _fit_scale(lines, base_px, opts.headline_scale, width - 2 * margin_h)
    style = AssStyle(
        name=INTRO_STYLE,
        font_name=sanitise_font_name(opts.font_name),
        font_px=base_px,
        primary=opts.primary_color,
        outline_color=opts.outline_color,
        back_color=opts.back_color,
        secondary=_HIDDEN if needs_hidden_secondary(opts.effect) else opts.primary_color,
        bold=opts.bold,
        italic=opts.italic,
        border_style=opts.border_style,
        outline=(
            float(opts.outline) if opts.outline is not None else round(base_px * 0.08, 2)
        ),
        shadow=opts.shadow,
        alignment=_INTRO_ALIGNMENT,
        margin_l=margin_h,
        margin_r=margin_h,
        margin_v=0,
    )

    pieces: list[str] = []
    for index, line in enumerate(lines):
        # Dòng đầu to hơn hẳn: đó là con số/lời chào phải đập vào mắt trước.
        size = base_px * opts.headline_scale if index == 0 else base_px
        for wrapped in wrap_line(line, resolve_line_capacity(size, margin_h, width)):
            pieces.append(f"{{\\fs{num(size)}}}{wrapped}")

    text = apply_effect(
        opts.effect,
        _LINE_BREAK.join(pieces),
        duration=opts.duration,
        width=width,
        height=height,
        font_px=base_px,
        alignment=_INTRO_ALIGNMENT,
        margin_l=margin_h,
        margin_r=margin_h,
        anchor=(width // 2, round(height * opts.position_ratio)),
    )
    # layer=1: text bìa luôn nằm trên lời thoại nếu hai bên lỡ trùng thời điểm.
    event = AssEvent(
        start=opts.start,
        end=opts.start + opts.duration,
        style=INTRO_STYLE,
        text=text,
        layer=1,
    )
    return style, [event]


# --------------------------------------------------------------------------- #
# Quyết định đường đi
# --------------------------------------------------------------------------- #
def plan_burn(
    workspace: Path, subs_name: str | None, opts: RenderOptions, width: int, height: int
) -> BurnPlan:
    """Dựng file .ass cần thiết trong ``workspace`` và trả về kế hoạch burn.

    Hàm blocking (ghi file) — nơi gọi phải bọc ``asyncio.to_thread``.
    """
    sub_opts = opts.subtitle
    burn_dialogue = bool(subs_name) and sub_opts.enabled and sub_opts.mode == "burn"
    intro = build_intro(opts.intro, width, height)

    if burn_dialogue and subs_name is not None and not subs_name.endswith(".ass"):
        styles: list[AssStyle] = []
        events: list[AssEvent] = []
        dialogue_style, dialogue_events = build_dialogue(
            parse_srt_events(workspace / subs_name), sub_opts, width, height
        )
        styles.append(dialogue_style)
        events += dialogue_events
        if intro is not None:
            styles.append(intro[0])
            events += intro[1]
        (workspace / STYLED_ASS_NAME).write_text(
            build_ass_document(width, height, styles, events), encoding="utf-8", newline="\n"
        )
        return BurnPlan(subs=STYLED_ASS_NAME, pre_styled=True)

    overlay: str | None = None
    if intro is not None:
        (workspace / INTRO_ASS_NAME).write_text(
            build_ass_document(width, height, [intro[0]], intro[1]),
            encoding="utf-8",
            newline="\n",
        )
        overlay = INTRO_ASS_NAME

    play_res_y = ASS_PLAY_RES_Y
    if subs_name is not None and subs_name.endswith(".ass"):
        play_res_y = read_play_res_y(workspace / subs_name)
    return BurnPlan(subs=subs_name, play_res_y=play_res_y, overlay=overlay)
