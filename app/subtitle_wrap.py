r"""Tự tính và chèn xuống dòng cho phụ đề theo khung hình thật.

Tách khỏi app/subtitles.py để giữ mỗi file dưới 400 dòng.

Vì sao không phó mặc auto-wrap của libass: đã đo thực nghiệm trên container
thật (ffmpeg/Debian, Liberation Serif) và thấy ngưỡng wrap của nó không ổn
định — cùng một câu dài, lề ngang 6% bề rộng thì KHÔNG xuống dòng (chữ tràn
sát mép), 25% thì xuống dòng cân đối. Nên tự tính số chữ vừa một dòng và tự
chèn dấu ngắt. ``WrapStyle: 0`` trong file .ass tự sinh (app/ass_doc.py) chỉ
còn là lưới an toàn cho ca ước lượng hụt.

Bề rộng chữ được ước lượng theo TỪNG LOẠI KÝ TỰ chứ không phải một con số
trung bình duy nhất: chữ hoa rộng gần gấp đôi ``i``/``l``, nên câu viết hoa
toàn bộ (rất hay gặp ở text quảng cáo) sẽ tràn khung nếu tính bình quân.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

# Bề rộng ký tự theo tỉ lệ so với cỡ chữ (pixel). Đo bằng cách render từng
# chuỗi 20 ký tự giống nhau trên canvas rộng 7680px — phải rộng như vậy để
# libass không tự xuống dòng và làm hỏng phép đo. Kết quả đo được lần lượt là
# 0.244 / 0.493 / 0.644 / 0.740; các con số dưới đây cộng thêm ~8% để luôn
# ƯỚC LƯỢNG DƯ: xuống dòng sớm một chữ chỉ hơi phí chỗ, còn ước lượng hụt là
# chữ tràn ra ngoài khung.
#
# Đo lại trên chính frame do container thật render (Liberation Serif nghiêng
# 43.2px, video 1080x1920): ước lượng dư 9–12% so với bề rộng thật, và với
# Liberation Sans đậm thì dư 7%. Tức là đang sai về phía an toàn ở cả hai font
# mặc định. Cố tình KHÔNG siết lại: hai font đó rộng hẹp khác nhau, mà mép sai
# số 7–12% chính là chỗ hấp thụ khác biệt đó — siết sát quá thì font này vừa,
# font kia tràn.
_NARROW_RATIO: Final[float] = 0.26  # i l j f t r I, dấu câu và dấu cách
_LOWER_RATIO: Final[float] = 0.53  # chữ thường, kể cả chữ có dấu tiếng Việt
_UPPER_RATIO: Final[float] = 0.70  # chữ hoa và chữ số
_WIDE_RATIO: Final[float] = 0.80  # m w M W @ %
# Mốc quy đổi sang đơn vị "ký tự trung bình". Giá trị cụ thể KHÔNG ảnh hưởng
# tới chỗ ngắt dòng: nó vừa nhân trong resolve_max_chars_per_line vừa chia
# trong text_units nên triệt tiêu — chỉ bốn tỉ lệ trên mới quyết định. Giữ 0.42
# để con số max_chars vẫn đọc được như "số ký tự thường mỗi dòng".
_AVG_CHAR_WIDTH_RATIO: Final[float] = 0.42
# Không rõ kích thước (chưa probe được) -> không wrap, giữ hành vi cũ.
_NO_WRAP: Final[int] = 0
# Chặn vòng lặp cân bằng dòng, phòng ca hội tụ chậm bất thường.
_BALANCE_STEPS: Final[int] = 40

_NARROW_CHARS: Final[frozenset[str]] = frozenset("ijltfrI.,;:!'\"|()[]{}-‘’“”·` ")
_WIDE_CHARS: Final[frozenset[str]] = frozenset("mwMW@%—–")

__all__ = [
    "char_units",
    "text_units",
    "estimate_px_width",
    "resolve_line_capacity",
    "resolve_max_chars_per_line",
    "wrap_line",
    "rewrap_srt_file",
    "rewrap_ass_file",
]


def char_units(char: str) -> float:
    """Bề rộng một ký tự, tính bằng bội số của "ký tự trung bình"."""
    if char in _NARROW_CHARS:
        ratio = _NARROW_RATIO
    elif char in _WIDE_CHARS:
        ratio = _WIDE_RATIO
    elif char.isupper() or char.isdigit():
        ratio = _UPPER_RATIO
    else:
        ratio = _LOWER_RATIO
    return ratio / _AVG_CHAR_WIDTH_RATIO


def text_units(text: str) -> float:
    """Bề rộng cả chuỗi, cùng đơn vị với ``max_chars``."""
    return sum(char_units(char) for char in text)


def estimate_px_width(text: str, font_px: float) -> float:
    """Bề rộng ước lượng của chuỗi khi vẽ ở cỡ ``font_px``, tính bằng pixel."""
    return text_units(text) * _AVG_CHAR_WIDTH_RATIO * font_px


def resolve_line_capacity(font_px: float, margin_h_px: float, width: int) -> float:
    """Sức chứa một dòng theo đơn vị "ký tự trung bình", giữ nguyên phần lẻ.

    Mọi tham số đều tính bằng PIXEL của khung hình output (xem
    ``subtitle_style.resolve_font_px`` / ``resolve_margin_h_px``) — truyền vào
    thay vì tự tính lại để không lệ thuộc thứ tự gọi hàm.

    Bên gọi nào cần một con số nguyên để hiển thị/ghi log thì dùng
    ``resolve_max_chars_per_line``; còn để QUYẾT ĐỊNH ngắt dòng thì phải dùng
    bản số thực này: làm tròn xuống 38.9 thành 38 sẽ bẻ đôi một dòng dài 38.1
    dù nó thừa sức vừa khung.
    """
    if width <= 0 or font_px <= 0:
        return float(_NO_WRAP)
    avail_px = width - 2 * margin_h_px
    char_px = font_px * _AVG_CHAR_WIDTH_RATIO
    if avail_px <= 0 or char_px <= 0:
        return float(_NO_WRAP)
    return max(1.0, avail_px / char_px)


def resolve_max_chars_per_line(font_px: float, margin_h_px: float, width: int) -> int:
    """Như ``resolve_line_capacity`` nhưng làm tròn xuống số nguyên."""
    return int(resolve_line_capacity(font_px, margin_h_px, width))


def _greedy(words: list[str], limit: float) -> list[str]:
    """Xếp từ vào dòng theo lối tham lam, không bao giờ cắt ngang một từ.

    Từ đơn lẻ dài hơn ``limit`` (ví dụ một URL) vẫn được để nguyên trên dòng
    riêng — cắt ngang giữa từ còn xấu hơn là để nó hơi tràn.
    """
    lines: list[str] = []
    current: list[str] = []
    current_units = 0.0
    space_units = char_units(" ")
    for word in words:
        word_units = text_units(word)
        extra = word_units if not current else word_units + space_units
        if current and current_units + extra > limit:
            lines.append(" ".join(current))
            current, current_units = [word], word_units
            continue
        current.append(word)
        current_units += extra
    if current:
        lines.append(" ".join(current))
    return lines


def wrap_line(text: str, max_chars: float) -> list[str]:
    """Chia một dòng phụ đề dài thành nhiều dòng CÂN ĐỐI theo từ.

    ``max_chars<=0`` (không biết kích thước khung) -> giữ nguyên, không wrap.

    Sau khi biết cần bao nhiêu dòng thì hạ dần ngưỡng để các dòng dài xấp xỉ
    nhau: xếp tham lam đơn thuần hay cho ra một dòng gần đầy và một dòng cụt
    lủn hai chữ, nhìn rất lệch.
    """
    if max_chars <= 0 or text_units(text) <= max_chars:
        return [text]
    words = text.split()
    if not words:
        return [text]

    best = _greedy(words, float(max_chars))
    target = len(best)
    for _ in range(_BALANCE_STEPS):
        if target <= 1:
            break
        limit = max(text_units(line) for line in best) - 0.01
        candidate = _greedy(words, limit)
        if len(candidate) != target:
            break
        best = candidate
    return best


# 1\n00:00:01,000 --> 00:00:03,000\nNội dung (có thể nhiều dòng)\n\n2\n...
_SRT_BLOCK: Final[re.Pattern[str]] = re.compile(
    r"(?P<num>\d+)\n(?P<timing>[^\n]+)\n(?P<text>.*?)(?=\n\n\d+\n|\Z)", re.DOTALL
)


def rewrap_srt_file(path: Path, max_chars: float) -> None:
    """Ghi đè lại các dòng chữ trong file SRT đã chuẩn hoá, xuống dòng lại.

    Nối các dòng con hiện có của mỗi block thành một câu liền (SPEC không cam
    kết giữ line-break gốc khi ``mode=burn``, vì gốc thường chỉ là AI tự chia
    dòng không biết trước khung hình thật) rồi wrap lại theo ``max_chars``.
    """
    if max_chars <= 0:
        return
    text = path.read_text(encoding="utf-8")

    def _rewrap(match: re.Match[str]) -> str:
        joined = " ".join(
            line.strip() for line in match.group("text").splitlines() if line.strip()
        )
        wrapped = "\n".join(wrap_line(joined, max_chars))
        return f"{match.group('num')}\n{match.group('timing')}\n{wrapped}"

    path.write_text(_SRT_BLOCK.sub(_rewrap, text), encoding="utf-8")


# Dialogue: 0,0:00:01.00,0:00:03.50,Default,,0,0,0,,Nội dung\Ndòng hai
_ASS_TEXT: Final[re.Pattern[str]] = re.compile(
    r"^((?:Dialogue|Comment)\s*:\s*(?:[^,]*,){9})(.*)$"
)


def rewrap_ass_file(path: Path, max_chars: float) -> None:
    r"""Như ``rewrap_srt_file`` nhưng cho ASS: ngắt dòng bằng ``\N``."""
    if max_chars <= 0:
        return
    out_lines: list[str] = []
    for line in path.read_text(encoding="utf-8").split("\n"):
        match = _ASS_TEXT.match(line)
        if match is None:
            out_lines.append(line)
            continue
        head, body = match.group(1), match.group(2)
        # r"\N" trong regex bị Python 3.12+ hiểu là escape \N{...} (tên ký tự
        # Unicode) và raise "missing {" nếu thiếu -> phải escape thành r"\\N"
        # để khớp ĐÚNG 2 ký tự literal backslash+N mà ASS dùng để ngắt dòng.
        joined = " ".join(part for part in re.split(r"\\N|\n", body) if part.strip())
        out_lines.append(head + r"\N".join(wrap_line(joined, max_chars)))
    path.write_text("\n".join(out_lines), encoding="utf-8")
