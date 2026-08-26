r"""Test cho app/overlay.py + app/ass_doc.py — file .ass tự sinh.

Điểm mấu chốt được chốt ở đây: ``PlayResX``/``PlayResY`` phải ĐÚNG bằng khung
hình. Sai chỗ này là mọi số đo (cỡ chữ, lề, viền) lệch theo, và đó chính là
gốc của việc chữ trông méo/tràn mép trước đây.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.ass_doc import AssStyle, build_ass_document, format_ass_time, style_colour
from app.models import RenderOptions, TextEffect
from app.overlay import (
    STYLED_ASS_NAME,
    parse_srt_events,
    plan_burn,
    read_play_res_y,
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
# ass_doc
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(0, "0:00:00.00"), (1.5, "0:00:01.50"), (61.25, "0:01:01.25"), (3661.0, "1:01:01.00")],
)
def test_format_ass_time(seconds: float, expected: str) -> None:
    assert format_ass_time(seconds) == expected


def test_format_ass_time_clamps_negative_to_zero() -> None:
    assert format_ass_time(-5.0) == "0:00:00.00"


def test_style_colour_drops_trailing_ampersand() -> None:
    # Dòng Style dùng dạng &HAABBGGRR (không có & đóng), khác force_style.
    assert style_colour("#FFFFFF") == "&H00FFFFFF"
    assert style_colour("#FF8800") == "&H000088FF"


def test_style_line_always_declares_scale_100() -> None:
    """ScaleX/ScaleY luôn 100/100 — đây là thứ chặn chữ bị méo ngang/dọc."""
    fields = AssStyle(name="S", font_name="Liberation Serif", font_px=40).to_line()
    parts = fields.removeprefix("Style: ").split(",")
    assert parts[11] == "100"  # ScaleX
    assert parts[12] == "100"  # ScaleY


def test_style_line_uses_minus_one_for_bold_and_italic() -> None:
    line = AssStyle(name="S", font_name="F", font_px=10, bold=True, italic=True).to_line()
    parts = line.removeprefix("Style: ").split(",")
    assert (parts[7], parts[8]) == ("-1", "-1")
    off = AssStyle(name="S", font_name="F", font_px=10, bold=False, italic=False).to_line()
    assert off.removeprefix("Style: ").split(",")[7:9] == ["0", "0"]


def test_document_playres_matches_canvas_exactly() -> None:
    document = build_ass_document(720, 1280, [], [])
    assert "PlayResX: 720" in document
    assert "PlayResY: 1280" in document
    # WrapStyle 0 = lưới an toàn: câu nào lỡ dài hơn ước lượng vẫn tự xuống dòng.
    assert "WrapStyle: 0" in document


# --------------------------------------------------------------------------- #
# parse_srt_events
# --------------------------------------------------------------------------- #
def test_parse_srt_events_reads_timing_and_text(tmp_workspace: Path) -> None:
    write_srt(tmp_workspace)
    events = parse_srt_events(tmp_workspace / "subs.srt")
    assert len(events) == 2
    assert events[0][0] == pytest.approx(0.5)
    assert events[0][1] == pytest.approx(3.0)
    assert events[1][2] == ["Moi villa 1 trai nghiem"]


def test_parse_srt_events_keeps_multiline_body(tmp_workspace: Path) -> None:
    write_srt(tmp_workspace, "1\n00:00:01,000 --> 00:00:02,000\nDong mot\nDong hai\n")
    assert parse_srt_events(tmp_workspace / "subs.srt")[0][2] == ["Dong mot", "Dong hai"]


def test_parse_srt_events_ignores_block_numbers(tmp_workspace: Path) -> None:
    write_srt(tmp_workspace, "7\n00:00:01,000 --> 00:00:02,000\nNoi dung\n")
    assert parse_srt_events(tmp_workspace / "subs.srt")[0][2] == ["Noi dung"]


# --------------------------------------------------------------------------- #
# read_play_res_y
# --------------------------------------------------------------------------- #
def test_read_play_res_y_from_user_ass(tmp_workspace: Path) -> None:
    path = tmp_workspace / "u.ass"
    path.write_text("[Script Info]\nPlayResX: 384\nPlayResY: 1080\n", encoding="utf-8")
    assert read_play_res_y(path) == 1080


def test_read_play_res_y_defaults_to_288_when_absent(tmp_workspace: Path) -> None:
    # libass/ffmpeg cũng mặc định 288 khi file không khai.
    path = tmp_workspace / "u.ass"
    path.write_text("[Script Info]\nScriptType: v4.00+\n", encoding="utf-8")
    assert read_play_res_y(path) == 288


def test_read_play_res_y_ignores_missing_file(tmp_workspace: Path) -> None:
    assert read_play_res_y(tmp_workspace / "khong-co.ass") == 288


# --------------------------------------------------------------------------- #
# plan_burn — đường .srt (đường đi chính)
# --------------------------------------------------------------------------- #
def test_plan_burn_builds_styled_ass_from_srt(tmp_workspace: Path) -> None:
    write_srt(tmp_workspace)
    plan = plan_burn(tmp_workspace, "subs.srt", RenderOptions(), 720, 1280)

    assert plan.subs == STYLED_ASS_NAME
    assert plan.pre_styled is True  # có style sẵn -> không cần force_style
    assert plan.overlay is None

    document = (tmp_workspace / STYLED_ASS_NAME).read_text(encoding="utf-8")
    assert "PlayResX: 720" in document and "PlayResY: 1280" in document
    # Cỡ chữ = 720 × 0.04 = 28.8px, lề ngang = 720 × 0.06 = 43px,
    # lề đáy = 1280 × 0.14 = 179px — tất cả là pixel thật.
    style = styles_of(document)[0]
    assert "Liberation Serif,28.8," in style
    assert style.endswith("2,43,43,179,1")


def test_styled_ass_wraps_long_line_itself(tmp_workspace: Path) -> None:
    write_srt(tmp_workspace)
    plan_burn(tmp_workspace, "subs.srt", RenderOptions(), 720, 1280)
    document = (tmp_workspace / STYLED_ASS_NAME).read_text(encoding="utf-8")
    first = events_of(document)[0]
    assert "\\N" in first, "câu dài phải được tự chèn dấu xuống dòng"
    assert events_of(document)[1].count("\\N") == 0  # câu ngắn thì không


def test_styled_ass_default_font_is_bold_italic_serif(tmp_workspace: Path) -> None:
    # Đúng kiểu chữ trong ảnh mẫu: serif nghiêng, đậm, trắng viền đen.
    write_srt(tmp_workspace)
    plan_burn(tmp_workspace, "subs.srt", RenderOptions(), 720, 1280)
    parts = (
        styles_of((tmp_workspace / STYLED_ASS_NAME).read_text(encoding="utf-8"))[0]
        .removeprefix("Style: ")
        .split(",")
    )
    assert parts[1] == "Liberation Serif"
    assert parts[3] == "&H00FFFFFF"  # PrimaryColour trắng
    assert parts[7] == "-1"  # Bold
    assert parts[8] == "-1"  # Italic


@pytest.mark.parametrize(
    ("effect", "marker"),
    [
        (TextEffect.NONE, None),
        (TextEffect.FADE, "\\fad("),
        (TextEffect.POP, "\\fscx70\\fscy70"),
        (TextEffect.SLIDE_UP, "\\move("),
        (TextEffect.GLOW, "\\blur"),
        (TextEffect.TYPEWRITER, "\\k"),
    ],
)
def test_effect_choice_lands_in_dialogue_line(
    tmp_workspace: Path, effect: TextEffect, marker: str | None
) -> None:
    write_srt(tmp_workspace)
    opts = RenderOptions.model_validate({"subtitle": {"effect": effect.value}})
    plan_burn(tmp_workspace, "subs.srt", opts, 720, 1280)
    line = events_of((tmp_workspace / STYLED_ASS_NAME).read_text(encoding="utf-8"))[0]
    if marker is None:
        assert "{" not in line
    else:
        assert marker in line


def test_typewriter_hides_secondary_colour(tmp_workspace: Path) -> None:
    """``\\k`` vốn để ĐỔI MÀU; muốn nó thành máy chữ thì màu chưa tô phải trong
    suốt, nếu không ký tự chưa tới lượt vẫn hiện (chỉ khác màu)."""
    write_srt(tmp_workspace)
    opts = RenderOptions.model_validate({"subtitle": {"effect": "typewriter"}})
    plan_burn(tmp_workspace, "subs.srt", opts, 720, 1280)
    parts = (
        styles_of((tmp_workspace / STYLED_ASS_NAME).read_text(encoding="utf-8"))[0]
        .removeprefix("Style: ")
        .split(",")
    )
    assert parts[4].upper().startswith("&HFF")  # SecondaryColour alpha = FF


def test_soft_mode_leaves_subtitle_file_untouched(tmp_workspace: Path) -> None:
    write_srt(tmp_workspace)
    opts = RenderOptions.model_validate({"subtitle": {"mode": "soft"}})
    plan = plan_burn(tmp_workspace, "subs.srt", opts, 720, 1280)
    assert plan.subs == "subs.srt"
    assert plan.pre_styled is False
    assert not (tmp_workspace / STYLED_ASS_NAME).exists()


def test_disabled_subtitle_is_not_restyled(tmp_workspace: Path) -> None:
    write_srt(tmp_workspace)
    opts = RenderOptions.model_validate({"subtitle": {"enabled": False}})
    plan = plan_burn(tmp_workspace, "subs.srt", opts, 720, 1280)
    assert plan.pre_styled is False
    assert not (tmp_workspace / STYLED_ASS_NAME).exists()


# --------------------------------------------------------------------------- #
# plan_burn — file .ass do người dùng đưa lên
# --------------------------------------------------------------------------- #
def test_user_ass_keeps_own_styles_and_reports_play_res(tmp_workspace: Path) -> None:
    (tmp_workspace / "subs.ass").write_text(
        "[Script Info]\nPlayResY: 720\n[Events]\n"
        "Dialogue: 0,0:00:01.00,0:00:03.00,Default,,0,0,0,,Xin chao\n",
        encoding="utf-8",
    )
    plan = plan_burn(tmp_workspace, "subs.ass", RenderOptions(), 720, 1280)
    assert plan.subs == "subs.ass"
    assert plan.pre_styled is False  # cần force_style
    assert plan.play_res_y == 720
    assert not (tmp_workspace / STYLED_ASS_NAME).exists()
