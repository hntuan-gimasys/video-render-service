"""Test cho app/subtitle_wrap.py — tự tính và chèn xuống dòng theo khung hình.

Không trông chờ vào auto-wrap của libass (đã đo thực nghiệm là không ổn định
— xem docstring module), nên phần này tự chịu trách nhiệm chia dòng đúng.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from app.models import SubtitleOptions
from app.subtitle_style import resolve_font_px, resolve_margin_h_px
from app.subtitle_wrap import (
    char_units,
    resolve_max_chars_per_line,
    rewrap_ass_file,
    rewrap_srt_file,
    text_units,
    wrap_line,
)


def max_chars_for(opts: SubtitleOptions, width: int, height: int) -> int:
    """Số ký tự mỗi dòng đúng như pipeline tính (mọi số đo bằng pixel)."""
    return resolve_max_chars_per_line(
        resolve_font_px(opts, width, height), resolve_margin_h_px(opts, width), width
    )


# --------------------------------------------------------------------------- #
# resolve_max_chars_per_line
# --------------------------------------------------------------------------- #
def test_max_chars_zero_without_dimensions() -> None:
    assert resolve_max_chars_per_line(24.0, 0, 0) == 0


def test_max_chars_zero_when_font_size_zero() -> None:
    assert resolve_max_chars_per_line(0.0, 10, 1920) == 0


def test_max_chars_invariant_across_resolutions_with_same_ratios() -> None:
    """font_size và margin_h đều tự co theo BỀ NGANG cùng công thức, nên số ký
    tự vừa một dòng chỉ phụ thuộc font_size_ratio/margin_h_ratio — KHÔNG phụ
    thuộc độ phân giải cụ thể (720p dọc hay 1080p ngang cho cùng kết quả).
    Đây là tính chất thiết kế mong muốn, không phải trùng hợp."""
    opts = SubtitleOptions()
    values = {
        max_chars_for(opts, w, h)
        for w, h in [(720, 1280), (1920, 1080), (1080, 1080), (2560, 1440)]
    }
    assert len(values) == 1, f"max_chars phải giống nhau ở mọi độ phân giải: {values}"
    assert values.pop() > 0


def test_max_chars_increases_with_smaller_font_ratio() -> None:
    # font_size_ratio nhỏ hơn -> chữ nhỏ hơn -> nhiều chữ vừa một dòng hơn.
    w, h = 1080, 1920
    more_chars = max_chars_for(SubtitleOptions(font_size_ratio=0.02), w, h)
    fewer_chars = max_chars_for(SubtitleOptions(font_size_ratio=0.08), w, h)
    assert more_chars > fewer_chars > 0


def test_max_chars_never_negative_when_margin_too_big() -> None:
    # margin_h ép cứng lớn hơn nửa bề ngang -> avail_px âm, không phải crash.
    assert max_chars_for(SubtitleOptions(margin_h=500), 720, 1280) == 0


# --------------------------------------------------------------------------- #
# Đo bề rộng theo TỪNG LOẠI ký tự
# --------------------------------------------------------------------------- #
def test_char_units_orders_narrow_lower_upper_wide() -> None:
    # Thứ tự này là lý do không dùng một con số trung bình duy nhất.
    assert char_units("i") < char_units("a") < char_units("A") < char_units("W")


def test_char_units_treats_vietnamese_accents_like_base_letter() -> None:
    # Dấu tiếng Việt không làm chữ rộng thêm — đừng để nó bị tính như chữ hoa.
    assert char_units("ữ") == char_units("u")
    assert char_units("Ữ") == char_units("U")


def test_uppercase_sentence_wraps_earlier_than_lowercase() -> None:
    """Câu VIẾT HOA rộng hơn hẳn cùng số ký tự viết thường.

    Đây chính là ca mà cách đếm ký tự đơn thuần tính hụt và làm chữ tràn khung
    — text quảng cáo hầu như luôn có dòng viết hoa toàn bộ.
    """
    sentence = "khuyen mai cuc lon cho khach dat som hom nay"
    limit = int(text_units(sentence)) + 1  # vừa khít bản viết thường
    assert wrap_line(sentence, limit) == [sentence]
    assert len(wrap_line(sentence.upper(), limit)) > 1


def test_wrap_line_balances_line_lengths() -> None:
    # Xếp tham lam đơn thuần cho ra một dòng gần đầy + một dòng cụt lủn.
    lines = wrap_line(
        "Lafesta Phu Quoc chao mung nam moi voi phao hoa ruc ro tren bau troi", 52
    )
    assert len(lines) == 2
    shortest, longest = sorted(text_units(line) for line in lines)
    assert longest - shortest < 8, f"hai dòng lệch nhau quá nhiều: {lines}"


# --------------------------------------------------------------------------- #
# wrap_line
# --------------------------------------------------------------------------- #
def test_wrap_line_short_text_unchanged() -> None:
    assert wrap_line("Xin chào", 20) == ["Xin chào"]


def test_wrap_line_no_wrap_when_max_chars_zero() -> None:
    # max_chars=0 nghĩa là "không biết khung hình" -> giữ nguyên, không đoán.
    long_text = "Lafesta Phu Quoc chao mung nam moi voi phao hoa ruc ro"
    assert wrap_line(long_text, 0) == [long_text]


def test_wrap_line_splits_long_sentence() -> None:
    lines = wrap_line("Lafesta Phu Quoc chao mung nam moi voi phao hoa ruc ro", 26)
    assert len(lines) >= 2
    assert all(text_units(line) <= 26 or " " not in line for line in lines)
    assert " ".join(lines) == "Lafesta Phu Quoc chao mung nam moi voi phao hoa ruc ro"


def test_wrap_line_keeps_single_long_word_intact() -> None:
    # Một "từ" dài hơn max_chars (URL, tên riêng) không bị cắt ngang.
    assert wrap_line("supercalifragilisticexpialidocious", 10) == [
        "supercalifragilisticexpialidocious"
    ]


# --------------------------------------------------------------------------- #
# rewrap_srt_file
# --------------------------------------------------------------------------- #
def test_rewrap_srt_file_wraps_long_line(tmp_path: Path) -> None:
    src = tmp_path / "subs.srt"
    src.write_text(
        "1\n00:00:01,000 --> 00:00:05,000\n"
        "Lafesta Phu Quoc chao mung nam moi voi phao hoa ruc ro\n",
        encoding="utf-8",
    )
    rewrap_srt_file(src, 26)
    text = src.read_text(encoding="utf-8")
    lines = text.splitlines()
    assert lines[0] == "1"
    assert lines[1] == "00:00:01,000 --> 00:00:05,000"
    body_lines = lines[2:]
    assert len(body_lines) >= 2
    assert all(text_units(line) <= 26 for line in body_lines)
    assert " ".join(body_lines) == "Lafesta Phu Quoc chao mung nam moi voi phao hoa ruc ro"


def test_rewrap_srt_file_noop_when_max_chars_zero(tmp_path: Path) -> None:
    src = tmp_path / "subs.srt"
    original = "1\n00:00:01,000 --> 00:00:03,000\nCâu ngắn\n"
    src.write_text(original, encoding="utf-8")
    rewrap_srt_file(src, 0)
    assert src.read_text(encoding="utf-8") == original


def test_rewrap_srt_file_short_line_unchanged(tmp_path: Path) -> None:
    src = tmp_path / "subs.srt"
    src.write_text("1\n00:00:01,000 --> 00:00:03,000\nXin chào\n", encoding="utf-8")
    rewrap_srt_file(src, 40)
    lines = src.read_text(encoding="utf-8").splitlines()
    assert lines == ["1", "00:00:01,000 --> 00:00:03,000", "Xin chào"]


def test_rewrap_srt_file_handles_multiple_blocks(tmp_path: Path) -> None:
    src = tmp_path / "subs.srt"
    src.write_text(
        "1\n00:00:01,000 --> 00:00:03,000\nMột dòng ngắn\n\n"
        "2\n00:00:03,500 --> 00:00:06,000\n"
        "Lafesta Phu Quoc chao mung nam moi voi phao hoa ruc ro\n",
        encoding="utf-8",
    )
    rewrap_srt_file(src, 26)
    text = src.read_text(encoding="utf-8")
    blocks = text.strip("\n").split("\n\n")
    assert len(blocks) == 2
    assert blocks[0].splitlines()[0] == "1"
    assert blocks[1].splitlines()[0] == "2"
    second_body = blocks[1].splitlines()[2:]
    assert len(second_body) >= 2


def test_rewrap_srt_file_joins_pre_existing_line_breaks(tmp_path: Path) -> None:
    # Dòng gốc đã có \n do AI tự chia (không biết khung hình thật) -> nối lại
    # thành một câu rồi wrap lại theo max_chars mới, không giữ chỗ ngắt cũ.
    src = tmp_path / "subs.srt"
    src.write_text(
        "1\n00:00:01,000 --> 00:00:05,000\nLafesta Phu Quoc\nchao mung nam moi\n",
        encoding="utf-8",
    )
    rewrap_srt_file(src, 100)  # đủ rộng để gộp lại thành 1 dòng
    lines = src.read_text(encoding="utf-8").splitlines()
    assert lines[2:] == ["Lafesta Phu Quoc chao mung nam moi"]


# --------------------------------------------------------------------------- #
# rewrap_ass_file
# --------------------------------------------------------------------------- #
def test_rewrap_ass_file_wraps_long_dialogue(tmp_path: Path) -> None:
    src = tmp_path / "subs.ass"
    src.write_text(
        "[Script Info]\n[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        "Dialogue: 0,0:00:01.00,0:00:05.00,Default,,0,0,0,,"
        "Lafesta Phu Quoc chao mung nam moi voi phao hoa ruc ro\n",
        encoding="utf-8",
    )
    rewrap_ass_file(src, 26)
    text = src.read_text(encoding="utf-8")
    dialogue = next(line for line in text.splitlines() if line.startswith("Dialogue:"))
    body = dialogue.split(",", 9)[-1]
    parts = body.split("\\N")
    assert len(parts) >= 2
    assert all(len(part) <= 26 for part in parts)


def test_rewrap_ass_file_preserves_non_dialogue_lines(tmp_path: Path) -> None:
    src = tmp_path / "subs.ass"
    original = "[Script Info]\nScriptType: v4.00+\n[Events]\n"
    src.write_text(original, encoding="utf-8")
    rewrap_ass_file(src, 20)
    assert src.read_text(encoding="utf-8") == original


def test_rewrap_ass_file_joins_existing_hard_breaks(tmp_path: Path) -> None:
    src = tmp_path / "subs.ass"
    src.write_text(
        "[Events]\n"
        "Dialogue: 0,0:00:01.00,0:00:05.00,Default,,0,0,0,,Lafesta Phu Quoc\\Nchao mung\n",
        encoding="utf-8",
    )
    rewrap_ass_file(src, 100)  # đủ rộng để gộp lại thành 1 dòng
    text = src.read_text(encoding="utf-8")
    dialogue = next(line for line in text.splitlines() if line.startswith("Dialogue:"))
    assert dialogue.endswith("Lafesta Phu Quoc chao mung")


def test_rewrap_ass_file_noop_when_max_chars_zero(tmp_path: Path) -> None:
    src = tmp_path / "subs.ass"
    original = "[Events]\nDialogue: 0,0:00:01.00,0:00:03.00,Default,,0,0,0,,Câu ngắn\n"
    src.write_text(original, encoding="utf-8")
    rewrap_ass_file(src, 0)
    assert src.read_text(encoding="utf-8") == original


# --------------------------------------------------------------------------- #
# Hồi quy bằng ffmpeg thật: tự chèn xuống dòng phải làm chữ KHÔNG kéo sát mép
# — bug thật đã đo được trước khi có module này (chỉ 8px mỗi bên trên khung
# 720). Dùng chính wrap_line + rewrap_srt_file, không phải force_style margin.
# --------------------------------------------------------------------------- #
def test_manual_wrap_prevents_edge_to_edge_text(tmp_path: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("cần ffmpeg trong PATH")

    from app.subtitle_style import build_force_style

    width, height = 720, 1280
    opts = SubtitleOptions()
    max_chars = max_chars_for(opts, width, height)

    srt = tmp_path / "s.srt"
    srt.write_text(
        "1\n00:00:00,000 --> 00:00:05,000\n"
        "Lafesta Phu Quoc chao mung nam moi voi phao hoa ruc ro\n",
        encoding="utf-8",
    )
    rewrap_srt_file(srt, max_chars)

    style = build_force_style(opts, width, height)
    png = tmp_path / "frame.png"
    result = subprocess.run(
        [
            ffmpeg, "-y", "-v", "error",
            "-f", "lavfi", "-i", f"color=c=black:s={width}x{height}:d=1",
            "-vf", f"subtitles=s.srt:force_style='{style}'",
            "-frames:v", "1", str(png),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr

    raw = subprocess.run(
        [ffmpeg, "-v", "error", "-i", str(png), "-f", "rawvideo", "-pix_fmt", "gray", "-"],
        capture_output=True, check=True,
    ).stdout
    xs = [x for i, v in enumerate(raw) if v > 60 for x in [i % width]]
    assert xs, "không thấy chữ nào được vẽ"
    left_margin, right_margin = min(xs), width - 1 - max(xs)
    assert left_margin > 20, f"lề trái chỉ {left_margin}px"
    assert right_margin > 20, f"lề phải chỉ {right_margin}px"
