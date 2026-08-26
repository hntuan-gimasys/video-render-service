"""Kiểm tra & chuẩn hoá file phụ đề, sinh force_style cho libass."""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from typing import Final

from app.subtitle_style import (
    ASS_PLAY_RES_Y,
    build_force_style,
    hex_to_ass_color,
    resolve_font_px,
    resolve_font_size,
    resolve_margin_h,
    resolve_margin_h_px,
    resolve_margin_v,
    resolve_margin_v_px,
    resolve_outline,
    resolve_outline_px,
)
from app.utils import InvalidSubtitle

# Cho tương thích ngược: các module khác (ffmpeg_cmd, jobs, test cũ) import
# ASS_PLAY_RES_Y/hex_to_ass_color/build_force_style/resolve_* từ đây — phần
# tính style thật đã tách sang app/subtitle_style.py để giữ file dưới 400
# dòng, ở đây chỉ re-export (khai báo trong __all__ bên dưới).

# Thứ tự dò encoding: BOM trước, rồi utf-8, rồi các bảng mã 1 byte hay gặp với
# file .srt tiếng Việt. latin-1 luôn decode được nên đứng cuối làm chốt.
ENCODING_CANDIDATES: Final[tuple[str, ...]] = ("utf-8-sig", "utf-8", "cp1258", "cp1252", "latin-1")

# Tên file cố định trong workspace của job (SPEC §8). Dùng tên tương đối thuần
# chữ để filter subtitles= không phải escape gì.
SUBS_SRT_NAME: Final[str] = "subs.srt"
SUBS_ASS_NAME: Final[str] = "subs.ass"

# 00:01:02,500  hoặc  00:01:02.500 (một số tool xuất dấu chấm)
_TS = r"(\d{1,3}):([0-5]?\d):([0-5]?\d)[,.](\d{1,3})"
# Bản không capture, để tìm mốc timing nằm giữa dòng (xem
# restore_srt_line_breaks) mà không làm rối group của _TIMING_LINE.
_TS_PLAIN = r"\d{1,3}:[0-5]?\d:[0-5]?\d[,.]\d{1,3}"
_INLINE_TIMING: Final[re.Pattern[str]] = re.compile(
    rf"{_TS_PLAIN}\s*-->\s*{_TS_PLAIN}"
)
_TIMING_LINE: Final[re.Pattern[str]] = re.compile(
    rf"^\s*{_TS}\s*-->\s*{_TS}\s*(.*)$",
)
# Phần mở rộng toạ độ hợp lệ của SRT, đứng sau mốc thời gian:
# "X1:200 X2:300 Y1:400 Y2:500". Khác thế thì là nội dung bị dồn vào.
_SRT_POSITION: Final[re.Pattern[str]] = re.compile(r"^(?:[XY][12]:-?\d+\s*)+$")

__all__ = [
    "ENCODING_CANDIDATES",
    "SUBS_SRT_NAME",
    "SUBS_ASS_NAME",
    "ASS_PLAY_RES_Y",
    "decode_bytes",
    "normalize_srt",
    "normalize_subtitle",
    "hex_to_ass_color",
    "build_force_style",
    "resolve_font_size",
    "resolve_font_px",
    "resolve_outline",
    "resolve_outline_px",
    "resolve_margin_h",
    "resolve_margin_h_px",
    "resolve_margin_v",
    "resolve_margin_v_px",
    "strip_code_fence",
    "restore_srt_line_breaks",
    "shift_timestamp",
    "shift_ass_timestamp",
]

# Dialogue: 0,0:00:01.00,0:00:03.50,Default,,0,0,0,,Nội dung
_ASS_EVENT: Final[re.Pattern[str]] = re.compile(
    r"^(?P<head>(?:Dialogue|Comment)\s*:\s*[^,]*,)(?P<start>[^,]+),(?P<end>[^,]+),(?P<rest>.*)$"
)
_ASS_TS: Final[re.Pattern[str]] = re.compile(r"^\s*(\d+):([0-5]?\d):([0-5]?\d)[.,](\d{1,3})\s*$")

# ```srt \n ... \n ``` — fence AI hay bọc quanh nội dung phụ đề khi trả lời.
_CODE_FENCE: Final[re.Pattern[str]] = re.compile(
    r"^```[a-zA-Z0-9_-]*[ \t]*\n(?P<body>.*?)\n?[ \t]*```$",
    re.DOTALL,
)


def decode_bytes(raw: bytes) -> tuple[str, str]:
    """Dò encoding của nội dung .srt, trả về ``(text, encoding_đã_dùng)``.

    File .srt thực tế thường có BOM, CRLF, hoặc được lưu bằng CP1258/CP1252
    thay vì UTF-8 nên phải thử lần lượt.
    """
    for encoding in ENCODING_CANDIDATES:
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    # latin-1 không bao giờ raise nên nhánh này chỉ tới được khi raw rỗng/khác.
    raise InvalidSubtitle("Không dò được encoding của file phụ đề")


def _parse_ts(hours: str, minutes: str, seconds: str, millis: str) -> float:
    return (
        int(hours) * 3600
        + int(minutes) * 60
        + int(seconds)
        + int(millis.ljust(3, "0")) / 1000.0
    )


def _format_ts(total_seconds: float) -> str:
    # Clamp về 0: offset âm có thể đẩy timestamp xuống dưới 00:00:00,000.
    clamped = max(0.0, total_seconds)
    millis_total = int(round(clamped * 1000))
    hours, remainder = divmod(millis_total, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def shift_timestamp(line: str, offset_seconds: float) -> str | None:
    """Dịch một dòng timing ``--> `` theo offset. Không phải timing -> None."""
    match = _TIMING_LINE.match(line)
    if match is None:
        return None
    start = _parse_ts(*match.group(1, 2, 3, 4)) + offset_seconds
    end = _parse_ts(*match.group(5, 6, 7, 8)) + offset_seconds
    trailing = match.group(9).strip()
    shifted = f"{_format_ts(start)} --> {_format_ts(end)}"
    return f"{shifted} {trailing}" if trailing else shifted


def strip_code_fence(text: str) -> str:
    """Bỏ khối ```...``` bọc ngoài nếu có.

    Phụ đề dán trực tiếp từ AI hầu như luôn kèm code fence (```srt ... ```).
    Để nguyên thì dòng ``` lọt vào file và libass hiển thị nó như một dòng
    phụ đề, hoặc phá luôn block cuối.
    """
    match = _CODE_FENCE.match(text.strip())
    return match.group("body") if match else text


def _is_crammed(text: str) -> bool:
    """Có dòng nào bị dồn cả timing lẫn nội dung vào chung không?

    Không thể chỉ hỏi "_TIMING_LINE có khớp dòng nào không": pattern đó cố tình
    có nhóm ``(.*)$`` để đỡ toạ độ SRT (``X1:200 X2:300``), nên nó khớp luôn cả
    dòng dồn cục và làm ta tưởng file đã bình thường.
    """
    for line in text.split("\n"):
        matches = list(_INLINE_TIMING.finditer(line))
        if len(matches) >= 2:
            return True  # nhiều mốc thời gian trên cùng một dòng
        if not matches:
            continue
        match = matches[0]
        # Không dùng _TIMING_LINE ở đây: nó neo ^ nên không khớp dòng có số thứ
        # tự dính phía trước ("1 00:00:01,000 --> ..."), đúng ca cần phát hiện.
        before = line[: match.start()].strip()
        after = line[match.end() :].strip()
        if before:
            return True  # số thứ tự (hoặc chữ) dính trước mốc thời gian
        if after and not _SRT_POSITION.match(after):
            return True  # nội dung phụ đề dính ngay sau mốc thời gian
    return False


def restore_srt_line_breaks(text: str) -> str:
    """Tách lại block SRT khi nội dung bị mất dấu ngắt dòng.

    Ô nhập một dòng (Swagger UI, nhiều form HTML) bóp newline thành khoảng
    trắng khi người dùng dán, nên server nhận đúng nội dung nhưng nằm trên một
    dòng dài -> job bị INVALID_SRT dù text hoàn toàn hợp lệ. Ở đây cắt lại tại
    từng mốc ``-->`` rồi tự đánh số.

    Chạy trên file SRT bình thường thì kết quả không đổi, nên an toàn kể cả khi
    bị gọi oan.
    """
    if not _is_crammed(text):
        return text
    matches = list(_INLINE_TIMING.finditer(text))
    if not matches:
        return text

    blocks: list[str] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[match.end() : end].strip()
        # Số thứ tự của block kế tiếp dính vào cuối phần chữ. Chỉ cắt khi nó
        # đúng bằng số mong đợi, để câu kết thúc bằng số (vd "Chương 3") không
        # bị mất chữ số oan.
        body = re.sub(rf"\s+0*{index + 2}$", "", body).strip()
        blocks.append(f"{index + 1}\n{match.group(0)}\n{body}")
    return "\n\n".join(blocks) + "\n"


def _prepare_text(raw: bytes) -> str:
    """Dò encoding -> bỏ BOM -> CRLF về LF -> bỏ code fence -> NFC."""
    text, _encoding = decode_bytes(raw)
    # Bỏ BOM còn sót (file utf-8 có BOM nhưng decode bằng cp1258) + chuẩn hoá
    # CRLF/CR về LF.
    text = text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")
    text = strip_code_fence(text)
    text = restore_srt_line_breaks(text)
    # CP1258 lưu tiếng Việt dạng "chữ gốc + dấu tổ hợp" (NFD). libass ghép dấu
    # rời rất xấu nên đưa hết về dạng dựng sẵn NFC.
    return unicodedata.normalize("NFC", text)


def normalize_srt(src: Path, dst: Path, offset_seconds: float = 0.0) -> None:
    """Đọc ``src`` (encoding tuỳ ý) và ghi ``dst`` dạng UTF-8/LF đã dịch timing.

    Hàm blocking (I/O đĩa) — nơi gọi phải bọc ``asyncio.to_thread``.
    Raise :class:`InvalidSubtitle` nếu không tìm được block timing nào.
    """
    try:
        raw = src.read_bytes()
    except OSError as exc:
        raise InvalidSubtitle(f"Không đọc được file phụ đề: {exc}") from exc

    if not raw.strip():
        raise InvalidSubtitle("File phụ đề rỗng")

    _write_srt(_prepare_text(raw), dst, offset_seconds)


def _write_srt(text: str, dst: Path, offset_seconds: float) -> None:
    """Dựng lại file SRT sạch: mỗi block đúng dạng số / timing / chữ.

    Ghi lại từ đầu thay vì sửa tại chỗ để tự **đánh số thứ tự lại**: phụ đề do
    AI sinh ra hay thiếu dòng số, mà ffmpeg từ chối luôn cả file khi thiếu
    ("Unable to open subs.srt") — người dùng chỉ nhận được FFMPEG_FAILED tối
    nghĩa. Số cũ (nếu có) bị bỏ, nên file lệch số cũng được vá.
    """
    blocks: list[tuple[str, list[str]]] = []
    timing: str | None = None
    body_lines: list[str] = []
    text_done = False

    for raw_line in text.split("\n"):
        line = raw_line.rstrip()
        shifted = shift_timestamp(line, offset_seconds)
        if shifted is not None:
            if timing is not None:
                blocks.append((timing, body_lines))
            timing, body_lines, text_done = shifted, [], False
            continue
        if timing is None:
            continue  # số thứ tự / rác trước block đầu tiên
        if not line:
            # Dòng trống kết thúc phần chữ; dòng số của block sau sẽ bị bỏ.
            text_done = bool(body_lines)
            continue
        if text_done:
            continue
        body_lines.append(line)

    if timing is not None:
        blocks.append((timing, body_lines))
    if not blocks:
        raise InvalidSubtitle("Không tìm thấy block phụ đề hợp lệ nào")

    out_lines: list[str] = []
    for index, (timing_line, lines) in enumerate(blocks, start=1):
        out_lines.append(str(index))
        out_lines.append(timing_line)
        out_lines.extend(lines)
        out_lines.append("")

    body = "\n".join(out_lines).strip("\n") + "\n"
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(body, encoding="utf-8", newline="\n")


def _format_ass_ts(total_seconds: float) -> str:
    """0.5 -> '0:00:00.50' (ASS dùng centisecond, giờ không đệm 0)."""
    clamped = max(0.0, total_seconds)
    centis_total = int(round(clamped * 100))
    hours, remainder = divmod(centis_total, 360_000)
    minutes, remainder = divmod(remainder, 6_000)
    seconds, centis = divmod(remainder, 100)
    return f"{hours}:{minutes:02d}:{seconds:02d}.{centis:02d}"


def shift_ass_timestamp(line: str, offset_seconds: float) -> str | None:
    """Dịch một dòng ``Dialogue:``/``Comment:`` của ASS. Không phải event -> None."""
    match = _ASS_EVENT.match(line)
    if match is None:
        return None
    shifted: list[str] = []
    for field in ("start", "end"):
        ts = _ASS_TS.match(match.group(field))
        if ts is None:
            return None
        hours, minutes, seconds, fraction = ts.groups()
        # ASS dùng centisecond (2 chữ số) nên đệm phải cho đủ 2 rồi chia 100.
        value = (
            int(hours) * 3600
            + int(minutes) * 60
            + int(seconds)
            + int(fraction.ljust(2, "0")[:2]) / 100.0
        )
        shifted.append(_format_ass_ts(value + offset_seconds))
    return f"{match.group('head')}{shifted[0]},{shifted[1]},{match.group('rest')}"


def normalize_subtitle(src: Path, workspace: Path, offset_seconds: float = 0.0) -> str:
    """Chuẩn hoá file phụ đề vào workspace, trả về TÊN FILE tương đối đã ghi.

    SPEC §3.1 nhận cả ``.srt`` và ``.ass``. libass đọc ASS trực tiếp qua filter
    ``subtitles=`` nên với ASS chỉ cần chuẩn hoá encoding/newline và dịch timing
    của các dòng ``Dialogue:``, không được ép về định dạng SRT (sẽ mất style).

    Hàm blocking (I/O đĩa) — nơi gọi phải bọc ``asyncio.to_thread``.
    """
    try:
        raw = src.read_bytes()
    except OSError as exc:
        raise InvalidSubtitle(f"Không đọc được file phụ đề: {exc}") from exc
    if not raw.strip():
        raise InvalidSubtitle("File phụ đề rỗng")

    text = _prepare_text(raw)
    if "[Script Info]" in text or "[V4+ Styles]" in text or "[Events]" in text:
        dst = workspace / SUBS_ASS_NAME
        _write_ass(text, dst, offset_seconds)
        return SUBS_ASS_NAME

    dst = workspace / SUBS_SRT_NAME
    _write_srt(text, dst, offset_seconds)
    return SUBS_SRT_NAME


def _write_ass(text: str, dst: Path, offset_seconds: float) -> None:
    out_lines: list[str] = []
    events = 0
    for line in text.split("\n"):
        shifted = shift_ass_timestamp(line, offset_seconds) if offset_seconds else None
        if shifted is not None:
            events += 1
            out_lines.append(shifted)
            continue
        if _ASS_EVENT.match(line):
            events += 1
        out_lines.append(line.rstrip())
    if events == 0:
        raise InvalidSubtitle("File ASS không có dòng Dialogue nào")
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text("\n".join(out_lines).strip("\n") + "\n", encoding="utf-8", newline="\n")
