r"""Test cho text bìa đầu video (app/overlay.py) — ảnh bìa TikTok.

Tách khỏi tests/test_overlay.py để mỗi file test dưới 400 dòng.

Điểm khác lời thoại quan trọng nhất: text bìa phải hiện ĐỦ ngay tại frame 0
(TikTok lấy frame đầu tiên làm ảnh bìa), và co nhỏ chữ cho vừa thay vì tự bẻ
thêm dòng — người dùng đã tự chia dòng theo ý họ khi soạn bìa.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.models import RenderOptions, SubtitleOptions, TextEffect
from app.overlay import (
    INTRO_ASS_NAME,
    STYLED_ASS_NAME,
    build_intro,
    plan_burn,
    split_intro_lines,
)

LONG_SRT = (
    "1\n00:00:00,500 --> 00:00:03,000\n"
    "Lafesta Phu Quoc chao mung nam moi voi phao hoa ruc ro tren bau troi\n\n"
    "2\n00:00:03,000 --> 00:00:06,000\nMoi villa 1 trai nghiem\n"
)


def write_srt(workspace: Path, text: str = LONG_SRT) -> str:
    (workspace / "subs.srt").write_text(text, encoding="utf-8")
    return "subs.srt"


def styles_of(document: str) -> list[str]:
    return [line for line in document.splitlines() if line.startswith("Style: ")]


def events_of(document: str) -> list[str]:
    return [line for line in document.splitlines() if line.startswith("Dialogue: ")]


# --------------------------------------------------------------------------- #
# Text bìa
# --------------------------------------------------------------------------- #
def test_split_intro_lines_drops_blank_lines() -> None:
    assert split_intro_lines(" A \n\n B \n") == ["A", "B"]


def test_intro_none_without_text() -> None:
    assert build_intro(RenderOptions().intro, 720, 1280) is None


def test_intro_is_merged_into_styled_ass(tmp_workspace: Path) -> None:
    write_srt(tmp_workspace)
    opts = RenderOptions.model_validate(
        {"intro": {"text": "2tr9/nguoi\n3N2D tai GARRYA MU CANG CHAI"}}
    )
    plan = plan_burn(tmp_workspace, "subs.srt", opts, 720, 1280)
    assert plan.overlay is None  # gộp chung một file, không cần filter thứ hai
    document = (tmp_workspace / STYLED_ASS_NAME).read_text(encoding="utf-8")
    assert len(styles_of(document)) == 2
    intro = [line for line in events_of(document) if ",Bia," in line]
    assert len(intro) == 1
    # Neo giữa khối + ép toạ độ: 44% chiều cao tính từ đỉnh.
    assert "\\an5\\pos(360,563)" in intro[0]
    # Dòng đầu to hơn hẳn phần còn lại (headline_scale mặc định 1.55). Không
    # chốt số tuyệt đối vì cỡ chữ text bìa còn tự co cho vừa khung, xem
    # test_intro_shrinks_to_keep_each_line_on_one_row.
    sizes = [float(value) for value in re.findall(r"\\fs([\d.]+)", intro[0])]
    assert len(sizes) == 2
    assert sizes[0] == pytest.approx(sizes[1] * 1.55, rel=0.01)


def test_intro_shrinks_to_keep_each_line_on_one_row() -> None:
    """Text bìa CO CHỮ cho vừa khung thay vì tự bẻ thêm dòng.

    Người dùng đã tự chia dòng theo ý họ khi soạn ảnh bìa; bẻ thêm dòng là phá
    bố cục đó. Khác hẳn lời thoại — ở đó xuống dòng mới là việc đúng.
    """
    text = "2tr9/người\n3N2Đ tại GARRYA MÙ CANG CHẢI\n(Free nâng hạng Villa Hồ Bơi riêng)"
    opts = RenderOptions.model_validate({"intro": {"text": text}})
    result = build_intro(opts.intro, 1080, 1920)
    assert result is not None
    style, events = result
    assert events[0].text.count("\\N") == 2, "vẫn đúng ba dòng như người dùng gõ"
    # Đã co nhỏ hơn cỡ mặc định (1080 × 0.062) để dòng dài nhất vừa khung.
    assert style.font_px < 1080 * opts.intro.font_size_ratio


def test_intro_keeps_full_size_when_everything_already_fits() -> None:
    opts = RenderOptions.model_validate({"intro": {"text": "2tr9"}})
    result = build_intro(opts.intro, 1080, 1920)
    assert result is not None
    assert result[0].font_px == pytest.approx(1080 * opts.intro.font_size_ratio)


def test_intro_stops_shrinking_and_wraps_when_line_is_absurdly_long() -> None:
    # Co mãi thì chữ nhỏ tới mức không đọc nổi trên điện thoại -> chạm đáy 50%
    # rồi đành xuống dòng.
    opts = RenderOptions.model_validate({"intro": {"text": "uu dai " * 40}})
    result = build_intro(opts.intro, 1080, 1920)
    assert result is not None
    style, events = result
    assert style.font_px == pytest.approx(1080 * opts.intro.font_size_ratio * 0.5)
    assert "\\N" in events[0].text


def test_intro_never_breaks_a_single_unbreakable_word() -> None:
    # Không có khoảng trắng nào để ngắt (URL, tên riêng dài) -> để nguyên còn
    # hơn cắt ngang giữa từ.
    opts = RenderOptions.model_validate({"intro": {"text": "x" * 400}})
    result = build_intro(opts.intro, 1080, 1920)
    assert result is not None
    assert "\\N" not in result[1][0].text


def test_intro_alone_becomes_separate_overlay_file(tmp_workspace: Path) -> None:
    # Không có phụ đề nào -> vẫn phải burn được text bìa.
    opts = RenderOptions.model_validate({"intro": {"text": "Xem ngay"}})
    plan = plan_burn(tmp_workspace, None, opts, 720, 1280)
    assert plan.subs is None
    assert plan.overlay == INTRO_ASS_NAME
    assert (tmp_workspace / INTRO_ASS_NAME).exists()


def test_intro_overlays_user_ass_as_second_file(tmp_workspace: Path) -> None:
    (tmp_workspace / "subs.ass").write_text(
        "[Events]\nDialogue: 0,0:00:01.00,0:00:03.00,Default,,0,0,0,,Xin chao\n",
        encoding="utf-8",
    )
    opts = RenderOptions.model_validate({"intro": {"text": "Uu dai"}})
    plan = plan_burn(tmp_workspace, "subs.ass", opts, 720, 1280)
    assert plan.subs == "subs.ass" and plan.overlay == INTRO_ASS_NAME


def test_intro_starts_at_frame_zero_with_no_effect_by_default() -> None:
    """TikTok lấy frame ĐẦU TIÊN làm ảnh bìa: text phải hiện đủ ngay tại t=0.

    Nên mặc định là ``start=0`` và ``effect=none`` — fade-in sẽ làm frame 0
    trong suốt và ảnh bìa ra trống trơn.
    """
    opts = RenderOptions.model_validate({"intro": {"text": "Bia"}})
    assert opts.intro.start == 0.0
    assert opts.intro.effect is TextEffect.NONE
    result = build_intro(opts.intro, 720, 1280)
    assert result is not None
    _style, events = result
    assert events[0].start == 0.0
    assert "\\fad" not in events[0].text


def test_intro_disappears_after_its_duration() -> None:
    opts = RenderOptions.model_validate({"intro": {"text": "Bia", "duration": 1.5}})
    result = build_intro(opts.intro, 720, 1280)
    assert result is not None
    assert result[1][0].end == pytest.approx(1.5)


def test_intro_sits_above_dialogue_layer() -> None:
    opts = RenderOptions.model_validate({"intro": {"text": "Bia"}})
    result = build_intro(opts.intro, 720, 1280)
    assert result is not None
    assert result[1][0].layer == 1


def test_intro_long_line_is_wrapped_to_fit(tmp_workspace: Path) -> None:
    opts = RenderOptions.model_validate(
        {"intro": {"text": "Uu dai cuc lon cho khach dat phong som trong thang nay nhe"}}
    )
    result = build_intro(opts.intro, 720, 1280)
    assert result is not None
    assert "\\N" in result[1][0].text


def test_subtitle_font_name_is_sanitised_in_style_line(tmp_workspace: Path) -> None:
    # Dấu phẩy trong tên font sẽ phá vỡ dòng Style (các trường ngăn bằng phẩy).
    write_srt(tmp_workspace)
    opts = RenderOptions.model_validate({"subtitle": {"font_name": "Ev'il, Font: Name"}})
    plan_burn(tmp_workspace, "subs.srt", opts, 720, 1280)
    style = styles_of((tmp_workspace / STYLED_ASS_NAME).read_text(encoding="utf-8"))[0]
    assert style.removeprefix("Style: ").split(",")[1] == "Evil Font Name"


def test_explicit_pixel_options_land_verbatim_in_style(tmp_workspace: Path) -> None:
    write_srt(tmp_workspace)
    opts = RenderOptions.model_validate(
        {"subtitle": {"font_size": 64, "margin_h": 30, "margin_v": 200, "outline": 5}}
    )
    plan_burn(tmp_workspace, "subs.srt", opts, 720, 1280)
    parts = (
        styles_of((tmp_workspace / STYLED_ASS_NAME).read_text(encoding="utf-8"))[0]
        .removeprefix("Style: ")
        .split(",")
    )
    assert parts[2] == "64"  # Fontsize (pixel)
    assert parts[16] == "5"  # Outline (pixel)
    assert parts[19:22] == ["30", "30", "200"]  # MarginL, MarginR, MarginV


def test_border_style_4_writes_back_colour(tmp_workspace: Path) -> None:
    write_srt(tmp_workspace)
    opts = RenderOptions.model_validate(
        {"subtitle": {"border_style": 4, "back_color": "#80000000"}}
    )
    plan_burn(tmp_workspace, "subs.srt", opts, 720, 1280)
    parts = (
        styles_of((tmp_workspace / STYLED_ASS_NAME).read_text(encoding="utf-8"))[0]
        .removeprefix("Style: ")
        .split(",")
    )
    assert parts[15] == "4"  # BorderStyle
    assert parts[6] == "&H80000000"  # BackColour


def test_subtitle_options_alignment_reaches_style(tmp_workspace: Path) -> None:
    write_srt(tmp_workspace)
    opts = RenderOptions(subtitle=SubtitleOptions(alignment=8))
    plan_burn(tmp_workspace, "subs.srt", opts, 720, 1280)
    parts = (
        styles_of((tmp_workspace / STYLED_ASS_NAME).read_text(encoding="utf-8"))[0]
        .removeprefix("Style: ")
        .split(",")
    )
    assert parts[18] == "8"
