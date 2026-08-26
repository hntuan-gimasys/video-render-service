"""Cỡ chữ tự động, viền, và làm sạch text dán từ AI.

Tách khỏi tests/test_subtitles.py để mỗi file test dưới 400 dòng.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from app.models import SubtitleOptions
from app.subtitles import (
    ASS_PLAY_RES_Y,
    build_force_style,
    normalize_subtitle,
    resolve_font_px,
    resolve_font_size,
    resolve_margin_h,
    resolve_margin_h_px,
    restore_srt_line_breaks,
    resolve_outline,
    strip_code_fence,
)

ASS_SOURCE = (
    "[Script Info]\n"
    "ScriptType: v4.00+\n"
    "[V4+ Styles]\n"
    "Format: Name, Fontname, Fontsize\n"
    "Style: Default,Arial,28\n"
    "[Events]\n"
    "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    "Dialogue: 0,0:00:01.00,0:00:03.50,Default,,0,0,0,,Xin chào\n"
)


# --------------------------------------------------------------------------- #
# resolve_font_size / resolve_outline — cỡ chữ co theo BỀ NGANG video
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("width", "height", "expected_px_ratio"),
    [
        (1920, 1080, 0.04),  # 16:9 ngang
        (1280, 720, 0.04),
        (640, 360, 0.04),
        (1080, 1920, 0.04),  # 9:16 dọc (short video)
        (720, 1280, 0.04),
        (1080, 1080, 0.04),  # 1:1
        (2560, 1080, 0.04),  # 21:9 siêu rộng
    ],
)
def test_font_size_renders_at_constant_fraction_of_width(
    width: int, height: int, expected_px_ratio: float
) -> None:
    """Bất kể tỉ lệ khung, chữ phải cao đúng 4% BỀ NGANG khi libass vẽ ra.

    libass vẽ ở px = FontSize × height / 288, nên đây là phép kiểm tra ngược
    lại toàn bộ công thức.
    """
    size = resolve_font_size(SubtitleOptions(), width, height)
    rendered_px = size * height / ASS_PLAY_RES_Y
    assert rendered_px / width == pytest.approx(expected_px_ratio, rel=1e-6)


def test_vertical_video_font_is_far_smaller_than_old_fixed_size() -> None:
    # Bug cũ: FontSize=24 cố định -> video dọc 1080x1920 ra chữ 160px trên
    # khung rộng 1080 (15% bề ngang), tràn thành nhiều dòng.
    old_px = 24 * 1920 / ASS_PLAY_RES_Y
    new_px = resolve_font_size(SubtitleOptions(), 1080, 1920) * 1920 / ASS_PLAY_RES_Y
    assert old_px == pytest.approx(160.0)
    assert new_px == pytest.approx(43.2)
    assert new_px < old_px / 3


def test_explicit_font_size_overrides_auto() -> None:
    # font_size khai bằng PIXEL của khung hình (xem app/subtitle_style.py).
    opts = SubtitleOptions(font_size=30)
    assert resolve_font_px(opts, 1920, 1080) == 30.0
    # File ASS của người dùng có hệ toạ độ riêng (PlayResY=288) nên force_style
    # phải quy đổi: 30px trên khung cao 1080 = 30 × 288/1080 = 8 đơn vị ASS.
    assert resolve_font_size(opts, 1920, 1080) == pytest.approx(8.0)
    assert "FontSize=8," in build_force_style(opts, 1920, 1080)


def test_font_size_ratio_is_adjustable() -> None:
    small = resolve_font_size(SubtitleOptions(font_size_ratio=0.02), 1920, 1080)
    big = resolve_font_size(SubtitleOptions(font_size_ratio=0.08), 1920, 1080)
    assert big == pytest.approx(small * 4)


def test_missing_dimensions_falls_back_to_24() -> None:
    assert resolve_font_size(SubtitleOptions(), 0, 0) == 24.0
    assert resolve_font_size(SubtitleOptions(), 1920, 0) == 24.0


def test_outline_scales_with_font_size() -> None:
    # Viền cố định 2 trên video dọc = 13px trên chữ 43px -> chữ bệt thành khối.
    auto = resolve_outline(SubtitleOptions(), resolve_font_size(SubtitleOptions(), 1080, 1920))
    assert auto == pytest.approx(0.52, abs=0.01)
    landscape = resolve_outline(
        SubtitleOptions(), resolve_font_size(SubtitleOptions(), 1920, 1080)
    )
    assert landscape == pytest.approx(1.64, abs=0.01)


def test_explicit_outline_overrides_auto() -> None:
    assert resolve_outline(SubtitleOptions(outline=3), 20.0) == 3.0


# --------------------------------------------------------------------------- #
# strip_code_fence — phụ đề dán từ AI hay kèm ```
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("```srt\n1\n00:00:01,000 --> 00:00:02,000\nHi\n```", "1\n00:00:01,000 --> 00:00:02,000\nHi"),
        ("```\nHello\n```", "Hello"),
        ("```SRT\nHello\n```", "Hello"),
        ("```plaintext\nHello\n```", "Hello"),
        ("  ```srt\nHello\n```  ", "Hello"),
        # Không có fence -> giữ nguyên
        ("1\n00:00:01,000 --> 00:00:02,000\nHi", "1\n00:00:01,000 --> 00:00:02,000\nHi"),
        # Fence lửng (thiếu đóng) -> không cắt, để parser phía sau báo lỗi rõ
        ("```srt\nHello", "```srt\nHello"),
        # ``` nằm giữa nội dung -> không phải fence bọc ngoài
        ("Hello ``` world", "Hello ``` world"),
    ],
)
def test_strip_code_fence(raw: str, expected: str) -> None:
    assert strip_code_fence(raw) == expected


def test_normalize_subtitle_accepts_ai_output_with_fence(tmp_path: Path) -> None:
    src = tmp_path / "ai.srt"
    src.write_text(
        "```srt\n1\n00:00:01,000 --> 00:00:03,000\nXin chào\n\n"
        "2\n00:00:03,500 --> 00:00:05,000\nDòng hai\n```\n",
        encoding="utf-8",
    )
    assert normalize_subtitle(src, tmp_path) == "subs.srt"
    written = (tmp_path / "subs.srt").read_text(encoding="utf-8")
    assert "```" not in written
    assert "Xin chào" in written and "Dòng hai" in written


def test_normalize_subtitle_accepts_ass_with_fence(tmp_path: Path) -> None:
    src = tmp_path / "ai.ass"
    src.write_text("```ass\n" + ASS_SOURCE + "```\n", encoding="utf-8")
    assert normalize_subtitle(src, tmp_path) == "subs.ass"
    assert "```" not in (tmp_path / "subs.ass").read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# Tự đánh số thứ tự: ffmpeg từ chối cả file SRT nếu thiếu dòng số
# --------------------------------------------------------------------------- #
def test_srt_without_sequence_numbers_gets_renumbered(tmp_path: Path) -> None:
    src = tmp_path / "raw.srt"
    src.write_text(
        "00:00:01,000 --> 00:00:03,000\nXin chào\n\n"
        "00:00:03,500 --> 00:00:06,000\nDòng hai\n",
        encoding="utf-8",
    )
    normalize_subtitle(src, tmp_path)
    lines = (tmp_path / "subs.srt").read_text(encoding="utf-8").splitlines()
    assert lines[0] == "1"
    assert lines[1] == "00:00:01,000 --> 00:00:03,000"
    assert lines[2] == "Xin chào"
    assert lines[4] == "2"
    assert lines[5] == "00:00:03,500 --> 00:00:06,000"


def test_wrong_sequence_numbers_are_fixed(tmp_path: Path) -> None:
    # AI hay đánh số lặp hoặc nhảy số; ffmpeg bỏ qua nhưng cứ chuẩn hoá lại.
    src = tmp_path / "raw.srt"
    src.write_text(
        "7\n00:00:01,000 --> 00:00:02,000\nMột\n\n"
        "7\n00:00:02,000 --> 00:00:03,000\nHai\n\n"
        "99\n00:00:03,000 --> 00:00:04,000\nBa\n",
        encoding="utf-8",
    )
    normalize_subtitle(src, tmp_path)
    lines = (tmp_path / "subs.srt").read_text(encoding="utf-8").splitlines()
    assert [lines[0], lines[4], lines[8]] == ["1", "2", "3"]


def test_multi_line_text_block_is_preserved(tmp_path: Path) -> None:
    src = tmp_path / "raw.srt"
    src.write_text(
        "1\n00:00:01,000 --> 00:00:04,000\nDòng trên\nDòng dưới\n", encoding="utf-8"
    )
    normalize_subtitle(src, tmp_path)
    lines = (tmp_path / "subs.srt").read_text(encoding="utf-8").splitlines()
    assert lines[2:4] == ["Dòng trên", "Dòng dưới"]


def test_extra_blank_lines_do_not_create_empty_blocks(tmp_path: Path) -> None:
    src = tmp_path / "raw.srt"
    src.write_text(
        "\n\n1\n00:00:01,000 --> 00:00:02,000\nMột\n\n\n\n"
        "2\n00:00:02,000 --> 00:00:03,000\nHai\n\n\n",
        encoding="utf-8",
    )
    normalize_subtitle(src, tmp_path)
    text = (tmp_path / "subs.srt").read_text(encoding="utf-8")
    assert text == (
        "1\n00:00:01,000 --> 00:00:02,000\nMột\n\n"
        "2\n00:00:02,000 --> 00:00:03,000\nHai\n"
    )


# --------------------------------------------------------------------------- #
# Dán vào ô nhập MỘT DÒNG (Swagger UI) -> newline bị bóp thành khoảng trắng
# --------------------------------------------------------------------------- #
def test_crammed_single_line_paste_is_recovered(tmp_path: Path) -> None:
    """Đúng ca người dùng gặp: dán SRT hợp lệ vào Swagger vẫn bị INVALID_SRT."""
    src = tmp_path / "raw.srt"
    src.write_text(
        "1 00:00:01,000 --> 00:00:03,000 Xin chào các bạn  "
        "2 00:00:03,500 --> 00:00:06,000 Đây là dòng phụ đề thứ hai",
        encoding="utf-8",
    )
    normalize_subtitle(src, tmp_path)
    assert (tmp_path / "subs.srt").read_text(encoding="utf-8") == (
        "1\n00:00:01,000 --> 00:00:03,000\nXin chào các bạn\n\n"
        "2\n00:00:03,500 --> 00:00:06,000\nĐây là dòng phụ đề thứ hai\n"
    )


def test_crammed_single_line_without_sequence_numbers(tmp_path: Path) -> None:
    src = tmp_path / "raw.srt"
    src.write_text(
        "00:00:01,000 --> 00:00:03,000 Xin chào  00:00:03,500 --> 00:00:06,000 Dòng hai",
        encoding="utf-8",
    )
    normalize_subtitle(src, tmp_path)
    lines = (tmp_path / "subs.srt").read_text(encoding="utf-8").splitlines()
    assert lines == [
        "1",
        "00:00:01,000 --> 00:00:03,000",
        "Xin chào",
        "",
        "2",
        "00:00:03,500 --> 00:00:06,000",
        "Dòng hai",
    ]


def test_crammed_single_block(tmp_path: Path) -> None:
    # Số thứ tự dính TRƯỚC mốc thời gian: _TIMING_LINE neo ^ nên không khớp,
    # phải phát hiện bằng phần text đứng trước mốc.
    src = tmp_path / "raw.srt"
    src.write_text("1 00:00:01,000 --> 00:00:03,000 Chỉ một dòng", encoding="utf-8")
    normalize_subtitle(src, tmp_path)
    assert (tmp_path / "subs.srt").read_text(encoding="utf-8") == (
        "1\n00:00:01,000 --> 00:00:03,000\nChỉ một dòng\n"
    )


def test_crammed_text_ending_with_a_number_is_kept(tmp_path: Path) -> None:
    # Chỉ cắt số thứ tự khi nó đúng bằng số block kế tiếp, để "Chương 5" còn nguyên.
    src = tmp_path / "raw.srt"
    src.write_text(
        "1 00:00:01,000 --> 00:00:03,000 Xem Chương 5  "
        "2 00:00:03,500 --> 00:00:06,000 Hết",
        encoding="utf-8",
    )
    normalize_subtitle(src, tmp_path)
    text = (tmp_path / "subs.srt").read_text(encoding="utf-8")
    assert "Xem Chương 5" in text
    assert "Hết" in text


def test_normal_srt_is_untouched_by_the_recovery(tmp_path: Path) -> None:
    """Chạy hàm phục hồi trên file bình thường không được làm sai lệch gì."""
    original = (
        "1\n00:00:01,000 --> 00:00:03,000\nXin chào\n\n"
        "2\n00:00:03,500 --> 00:00:06,000\nDòng hai\n"
    )
    assert restore_srt_line_breaks(original) == original
    src = tmp_path / "raw.srt"
    src.write_text(original, encoding="utf-8")
    normalize_subtitle(src, tmp_path)
    assert (tmp_path / "subs.srt").read_text(encoding="utf-8") == original


def test_srt_position_extension_is_not_treated_as_crammed(tmp_path: Path) -> None:
    # "X1:200 X2:300 Y1:400 Y2:500" là phần mở rộng hợp lệ, không phải nội dung.
    src = tmp_path / "raw.srt"
    src.write_text(
        "1\n00:00:01,000 --> 00:00:03,000 X1:200 X2:300 Y1:400 Y2:500\nXin chào\n",
        encoding="utf-8",
    )
    normalize_subtitle(src, tmp_path)
    lines = (tmp_path / "subs.srt").read_text(encoding="utf-8").splitlines()
    assert lines[1] == "00:00:01,000 --> 00:00:03,000 X1:200 X2:300 Y1:400 Y2:500"
    assert lines[2] == "Xin chào"


def test_crammed_paste_still_applies_offset(tmp_path: Path) -> None:
    src = tmp_path / "raw.srt"
    src.write_text(
        "1 00:00:01,000 --> 00:00:03,000 Xin chào", encoding="utf-8"
    )
    normalize_subtitle(src, tmp_path, offset_seconds=2.0)
    assert "00:00:03,000 --> 00:00:05,000" in (
        tmp_path / "subs.srt"
    ).read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# resolve_margin_h — margin ngang để libass tự xuống dòng, không tự viết wrap
# --------------------------------------------------------------------------- #
def test_margin_h_scales_with_width_like_font_size() -> None:
    # Cùng công thức với resolve_font_size (PlayResY=288 cố định), làm tròn về
    # int vì MarginL/MarginR trong ASS là số nguyên.
    assert resolve_margin_h(SubtitleOptions(), 1920, 1080) == round(1920 * 0.06 * 288 / 1080)
    assert resolve_margin_h(SubtitleOptions(), 720, 1280) == round(720 * 0.06 * 288 / 1280)


def test_margin_h_zero_without_dimensions() -> None:
    assert resolve_margin_h(SubtitleOptions()) == 0


def test_explicit_margin_h_overrides_auto() -> None:
    # margin_h khai bằng PIXEL; force_style quy đổi sang đơn vị ASS như font.
    opts = SubtitleOptions(margin_h=60)
    assert resolve_margin_h_px(opts, 1920) == 60
    assert resolve_margin_h(opts, 1920, 1080) == round(60 * 288 / 1080)


def test_margin_h_ratio_is_adjustable() -> None:
    small = resolve_margin_h(SubtitleOptions(margin_h_ratio=0.03), 1920, 1080)
    big = resolve_margin_h(SubtitleOptions(margin_h_ratio=0.12), 1920, 1080)
    # So le vi lam tron int, chi kiem tra ti le xap xi 4 lan.
    assert big == pytest.approx(small * 4, abs=2)


def test_force_style_includes_margin_l_and_r() -> None:
    style = build_force_style(SubtitleOptions(), 1920, 1080)
    assert "MarginL=31" in style
    assert "MarginR=31" in style
    # Phải đứng trước Bold, sau MarginV, để force_style vẫn đọc được như SPEC.
    assert style.index("MarginV") < style.index("MarginL") < style.index("Bold")


def test_default_font_is_bold_serif() -> None:
    """SPEC mới: mặc định chữ đậm có chân (serif) giống caption phim/quảng cáo."""
    opts = SubtitleOptions()
    assert opts.font_name == "Liberation Serif"
    assert opts.bold is True
    assert "Bold=-1" in build_force_style(opts, 1920, 1080)


# --------------------------------------------------------------------------- #
# Hồi quy bằng ffmpeg thật: câu dài trên video dọc phải xuống dòng, không kéo
# sát mép — bug thật đã đo được (8px mỗi bên trên khung 720) trước khi có
# margin_h. Bị skip nếu máy không có ffmpeg.
# --------------------------------------------------------------------------- #
def test_long_line_wraps_with_margin_on_portrait_video(tmp_path: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("cần ffmpeg trong PATH")

    width, height = 720, 1280
    (tmp_path / "s.srt").write_text(
        "1\n00:00:00,000 --> 00:00:05,000\n"
        "Lafesta Phu Quoc chao mung nam moi voi phao hoa ruc ro\n",
        encoding="utf-8",
    )
    style = build_force_style(SubtitleOptions(), width, height)
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
    # Trước khi có margin_h: câu này kéo tới cách mép chỉ ~8px mỗi bên (đã đo
    # bằng ảnh thật). Giờ margin phải đủ rộng, chứng minh libass đã xuống dòng
    # thay vì kéo sát mép.
    assert left_margin > 20, f"lề trái chỉ {left_margin}px — có vẻ chưa xuống dòng"
    assert right_margin > 20, f"lề phải chỉ {right_margin}px — có vẻ chưa xuống dòng"
