"""Test cho app/subtitles.py — trọng tâm là hex_to_ass_color (BGR đảo)."""

from __future__ import annotations

import unicodedata
from pathlib import Path

import pytest

from app.models import SubtitleOptions
from app.subtitles import (
    build_force_style,
    decode_bytes,
    hex_to_ass_color,
    normalize_srt,
    normalize_subtitle,
    shift_ass_timestamp,
    shift_timestamp,
)
from app.utils import InvalidSubtitle


# --------------------------------------------------------------------------- #
# hex_to_ass_color
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("hex_color", "expected"),
    [
        # Hai ví dụ chốt trong SPEC §5.3.5
        ("#FFFFFF", "&H00FFFFFF&"),
        ("#FF8800", "&H000088FF&"),
        # Đen, và các màu thuần để thấy rõ việc đảo RGB -> BGR
        ("#000000", "&H00000000&"),
        ("#FF0000", "&H000000FF&"),  # đỏ -> BB=00 GG=00 RR=FF
        ("#00FF00", "&H0000FF00&"),  # xanh lá -> giữa nguyên vị trí
        ("#0000FF", "&H00FF0000&"),  # xanh dương -> BB=FF
        ("#123456", "&H00563412&"),
        # Dạng 8 ký tự #AARRGGBB: alpha giữ nguyên byte đầu
        ("#80000000", "&H80000000&"),
        ("#80FF8800", "&H800088FF&"),
        ("#FF112233", "&HFF332211&"),
        # Chữ thường và khoảng trắng vẫn nhận
        ("#ff8800", "&H000088FF&"),
        ("  #FF8800  ", "&H000088FF&"),
        # Không có dấu # cũng nhận
        ("FF8800", "&H000088FF&"),
    ],
)
def test_hex_to_ass_color(hex_color: str, expected: str) -> None:
    assert hex_to_ass_color(hex_color) == expected


@pytest.mark.parametrize("bad", ["#FFF", "#FFFFF", "", "#GGGGGG", "#12345", "xyz"])
def test_hex_to_ass_color_rejects_bad_input(bad: str) -> None:
    with pytest.raises(ValueError):
        hex_to_ass_color(bad)


# --------------------------------------------------------------------------- #
# build_force_style
# --------------------------------------------------------------------------- #
def test_build_force_style_default_without_dimensions() -> None:
    # Không truyền kích thước (không probe được) -> giữ cỡ mặc định cũ là 24,
    # và margin ngang = 0 (không có bề ngang để tính theo).
    assert build_force_style(SubtitleOptions()) == (
        "FontName=Liberation Serif,FontSize=24,PrimaryColour=&H00FFFFFF&,"
        "OutlineColour=&H00000000&,BorderStyle=1,Outline=1.92,Shadow=0,"
        "Alignment=2,MarginV=40,MarginL=0,MarginR=0,Bold=-1,Italic=-1"
    )


def test_build_force_style_box_includes_back_colour() -> None:
    style = build_force_style(SubtitleOptions(border_style=4, back_color="#80000000", bold=True))
    assert "BackColour=&H80000000&" in style
    assert "BorderStyle=4" in style
    assert "Bold=-1" in style  # ASS dùng -1 cho true
    # BackColour phải đứng trước BorderStyle để chuỗi style đọc được như SPEC
    assert style.index("BackColour") < style.index("BorderStyle")


def test_build_force_style_no_back_colour_when_border_style_1() -> None:
    assert "BackColour" not in build_force_style(SubtitleOptions(border_style=1))


def test_build_force_style_sanitises_font_name() -> None:
    # Dấu phẩy / nháy đơn trong font_name sẽ phá vỡ chuỗi force_style.
    style = build_force_style(SubtitleOptions(font_name="Ev'il, Font: Name"))
    assert style.startswith("FontName=Evil Font Name,FontSize=")
    assert style.count(",") == 12  # 13 field (kể cả MarginL/R và Italic) -> 12 dấu phẩy


def test_build_force_style_keeps_fractional_outline() -> None:
    assert "Outline=1.5" in build_force_style(SubtitleOptions(outline=1.5))


# --------------------------------------------------------------------------- #
# shift_timestamp
# --------------------------------------------------------------------------- #
def test_shift_timestamp_positive_offset() -> None:
    line = "00:00:01,000 --> 00:00:03,500"
    assert shift_timestamp(line, 2.5) == "00:00:03,500 --> 00:00:06,000"


def test_shift_timestamp_clamps_to_zero() -> None:
    line = "00:00:01,000 --> 00:00:03,500"
    assert shift_timestamp(line, -10.0) == "00:00:00,000 --> 00:00:00,000"


def test_shift_timestamp_keeps_position_suffix() -> None:
    line = "00:00:01,000 --> 00:00:03,500 X1:200 X2:300 Y1:400 Y2:500"
    assert shift_timestamp(line, 0.0).endswith("X1:200 X2:300 Y1:400 Y2:500")


def test_shift_timestamp_accepts_dot_millis() -> None:
    assert shift_timestamp("00:00:01.000 --> 00:00:02.000", 0.0) == (
        "00:00:01,000 --> 00:00:02,000"
    )


def test_shift_timestamp_ignores_non_timing_lines() -> None:
    assert shift_timestamp("1", 1.0) is None
    assert shift_timestamp("Xin chào", 1.0) is None


# --------------------------------------------------------------------------- #
# decode_bytes / normalize_srt
# --------------------------------------------------------------------------- #
def test_decode_bytes_prefers_utf8_sig() -> None:
    text, encoding = decode_bytes("Xin chào".encode("utf-8-sig"))
    assert text == "Xin chào"
    assert encoding == "utf-8-sig"


def test_decode_bytes_falls_back_to_cp1258() -> None:
    # 0xFD trong CP1258 là "ư" (CP1252 đọc thành "ý") và chuỗi byte này không
    # phải UTF-8 hợp lệ -> chứng minh đúng nhánh CP1258 được chọn.
    raw = "Đưa".encode("cp1258")
    assert raw == b"\xd0\xfda"
    text, encoding = decode_bytes(raw)
    assert encoding == "cp1258"
    assert unicodedata.normalize("NFC", text) == "Đưa"


def test_normalize_srt_utf8_bom_crlf(tmp_path: Path, sample_srt_content: str) -> None:
    src = tmp_path / "in.srt"
    src.write_bytes(("﻿" + sample_srt_content.replace("\n", "\r\n")).encode("utf-8"))
    dst = tmp_path / "out" / "subs.srt"

    normalize_srt(src, dst)

    raw = dst.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")  # BOM đã bị xoá
    assert b"\r" not in raw  # CRLF -> LF
    text = raw.decode("utf-8")
    assert "Xin chào thế giới" in text
    assert text.endswith("\n")


def test_normalize_srt_cp1258_source(tmp_path: Path) -> None:
    src = tmp_path / "in.srt"
    src.write_bytes("1\r\n00:00:01,000 --> 00:00:02,000\r\nĐưa tôi đi\r\n".encode("cp1258"))
    dst = tmp_path / "subs.srt"
    normalize_srt(src, dst)
    # Đọc lại phải ra đúng tiếng Việt dạng dựng sẵn (NFC), không còn dấu rời.
    assert dst.read_text(encoding="utf-8").splitlines()[2] == "Đưa tôi đi"


def test_normalize_srt_applies_offset(tmp_path: Path, sample_srt_content: str) -> None:
    src = tmp_path / "in.srt"
    src.write_text(sample_srt_content, encoding="utf-8")
    dst = tmp_path / "subs.srt"

    normalize_srt(src, dst, offset_seconds=1.25)

    lines = dst.read_text(encoding="utf-8").splitlines()
    assert lines[1] == "00:00:02,250 --> 00:00:04,750"
    assert lines[5] == "00:00:05,250 --> 00:00:07,250"


def test_normalize_srt_negative_offset_clamped(tmp_path: Path, sample_srt_content: str) -> None:
    src = tmp_path / "in.srt"
    src.write_text(sample_srt_content, encoding="utf-8")
    dst = tmp_path / "subs.srt"
    normalize_srt(src, dst, offset_seconds=-2.0)
    assert dst.read_text(encoding="utf-8").splitlines()[1] == "00:00:00,000 --> 00:00:01,500"


def test_normalize_srt_rejects_garbage(tmp_path: Path) -> None:
    src = tmp_path / "in.srt"
    src.write_text("đây không phải phụ đề\nchỉ là văn bản\n", encoding="utf-8")
    with pytest.raises(InvalidSubtitle):
        normalize_srt(src, tmp_path / "subs.srt")


def test_normalize_srt_rejects_empty(tmp_path: Path) -> None:
    src = tmp_path / "in.srt"
    src.write_bytes(b"   \n")
    with pytest.raises(InvalidSubtitle):
        normalize_srt(src, tmp_path / "subs.srt")


def test_normalize_srt_missing_file(tmp_path: Path) -> None:
    with pytest.raises(InvalidSubtitle):
        normalize_srt(tmp_path / "nope.srt", tmp_path / "subs.srt")


# --------------------------------------------------------------------------- #
# normalize_subtitle — SPEC §3.1 nhận cả .srt và .ass
# --------------------------------------------------------------------------- #
ASS_SOURCE = (
    "[Script Info]\n"
    "ScriptType: v4.00+\n"
    "[V4+ Styles]\n"
    "Format: Name, Fontname, Fontsize\n"
    "Style: Default,Arial,28\n"
    "[Events]\n"
    "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    "Dialogue: 0,0:00:01.00,0:00:03.50,Default,,0,0,0,,Xin chào\n"
    "Dialogue: 0,0:00:04.00,0:00:06.00,Default,,0,0,0,,Dòng hai\n"
)


def test_normalize_subtitle_srt_returns_srt_name(tmp_path: Path, sample_srt_content: str) -> None:
    src = tmp_path / "in.srt"
    src.write_text(sample_srt_content, encoding="utf-8")
    name = normalize_subtitle(src, tmp_path)
    assert name == "subs.srt"
    assert (tmp_path / "subs.srt").exists()


def test_normalize_subtitle_detects_ass(tmp_path: Path) -> None:
    src = tmp_path / "in.ass"
    src.write_text(ASS_SOURCE, encoding="utf-8")
    name = normalize_subtitle(src, tmp_path)
    assert name == "subs.ass"
    written = (tmp_path / "subs.ass").read_text(encoding="utf-8")
    # Giữ nguyên phần style của ASS, không được ép về SRT
    assert "[V4+ Styles]" in written
    assert "Dialogue: 0,0:00:01.00,0:00:03.50" in written


def test_normalize_subtitle_ass_detected_even_with_srt_extension(tmp_path: Path) -> None:
    # Nhận diện theo NỘI DUNG, không theo đuôi file người dùng đặt.
    src = tmp_path / "mislabeled.srt"
    src.write_text(ASS_SOURCE, encoding="utf-8")
    assert normalize_subtitle(src, tmp_path) == "subs.ass"


def test_normalize_subtitle_ass_bom_crlf_cp1258(tmp_path: Path) -> None:
    src = tmp_path / "in.ass"
    src.write_bytes(("﻿" + ASS_SOURCE.replace("\n", "\r\n")).encode("utf-8"))
    normalize_subtitle(src, tmp_path)
    raw = (tmp_path / "subs.ass").read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    assert b"\r" not in raw
    assert "Xin chào" in raw.decode("utf-8")


def test_normalize_subtitle_ass_applies_offset(tmp_path: Path) -> None:
    src = tmp_path / "in.ass"
    src.write_text(ASS_SOURCE, encoding="utf-8")
    normalize_subtitle(src, tmp_path, offset_seconds=1.5)
    written = (tmp_path / "subs.ass").read_text(encoding="utf-8")
    assert "Dialogue: 0,0:00:02.50,0:00:05.00,Default,,0,0,0,,Xin chào" in written
    assert "Dialogue: 0,0:00:05.50,0:00:07.50,Default,,0,0,0,,Dòng hai" in written


def test_normalize_subtitle_ass_negative_offset_clamped(tmp_path: Path) -> None:
    src = tmp_path / "in.ass"
    src.write_text(ASS_SOURCE, encoding="utf-8")
    normalize_subtitle(src, tmp_path, offset_seconds=-10)
    written = (tmp_path / "subs.ass").read_text(encoding="utf-8")
    assert "0:00:00.00,0:00:00.00" in written


def test_normalize_subtitle_ass_without_dialogue_is_rejected(tmp_path: Path) -> None:
    src = tmp_path / "in.ass"
    src.write_text("[Script Info]\nScriptType: v4.00+\n[Events]\n", encoding="utf-8")
    with pytest.raises(InvalidSubtitle):
        normalize_subtitle(src, tmp_path)


@pytest.mark.parametrize(
    ("line", "offset", "expected"),
    [
        (
            "Dialogue: 0,0:00:01.00,0:00:03.50,Default,,0,0,0,,Xin chào",
            2.0,
            "Dialogue: 0,0:00:03.00,0:00:05.50,Default,,0,0,0,,Xin chào",
        ),
        (
            "Comment: 0,1:02:03.45,1:02:04.00,Default,,0,0,0,,ghi chú",
            0.55,
            "Comment: 0,1:02:04.00,1:02:04.55,Default,,0,0,0,,ghi chú",
        ),
    ],
)
def test_shift_ass_timestamp(line: str, offset: float, expected: str) -> None:
    assert shift_ass_timestamp(line, offset) == expected


def test_shift_ass_timestamp_ignores_other_lines() -> None:
    assert shift_ass_timestamp("[Events]", 1.0) is None
    assert shift_ass_timestamp("Style: Default,Arial,28", 1.0) is None
