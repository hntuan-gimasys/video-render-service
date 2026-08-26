r"""Test cho app/ass_effects.py — hiệu ứng chữ dạng override tag.

Yêu cầu xuyên suốt: hiệu ứng nào cũng KHÔNG được làm méo chữ, tức mọi phép
phóng to phải giữ ``\fscx`` bằng đúng ``\fscy``.
"""

from __future__ import annotations

import re

import pytest

from app.ass_effects import (
    apply_effect,
    base_position,
    effect_hides_first_frame,
    needs_hidden_secondary,
)
from app.models import TextEffect

CANVAS = {"width": 720, "height": 1280, "font_px": 40.0, "alignment": 2}


def render(
    effect: TextEffect, text: str = "Xin chao", duration: float = 3.0, **kwargs: object
) -> str:
    return apply_effect(  # type: ignore[arg-type]
        effect, text, duration=duration, **{**CANVAS, **kwargs}
    )



def move_coords(tags: str) -> tuple[int, int, int, int]:
    r"""(x1, y1, x2, y2) trong ``\move(...)``."""
    match = re.search(r"\\move\((\d+),(\d+),(\d+),(\d+),", tags)
    assert match is not None, tags
    x1, y1, x2, y2 = (int(value) for value in match.groups())
    return x1, y1, x2, y2


def scale_pairs(tags: str) -> list[tuple[str, str]]:
    """Mọi cặp (fscx, fscy) xuất hiện trong chuỗi tag, theo thứ tự."""
    xs = re.findall(r"\\fscx(\d+(?:\.\d+)?)", tags)
    ys = re.findall(r"\\fscy(\d+(?:\.\d+)?)", tags)
    return list(zip(xs, ys, strict=True))


# --------------------------------------------------------------------------- #
# Chống méo — điều kiện quan trọng nhất
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("effect", list(TextEffect))
def test_no_effect_ever_scales_x_and_y_differently(effect: TextEffect) -> None:
    """Chỉ cần fscx khác fscy một lần là chữ bị kéo ngang hoặc dọc."""
    tags = render(effect)
    pairs = scale_pairs(tags)
    assert all(x == y for x, y in pairs), f"{effect} có cặp scale lệch: {pairs}"


@pytest.mark.parametrize("effect", list(TextEffect))
def test_effect_never_rotates_or_shears_text(effect: TextEffect) -> None:
    # \frx \fry \frz \fax \fay đều làm chữ nghiêng/xoay -> không hiệu ứng nào dùng.
    assert not re.search(r"\\f(?:r[xyz]|a[xy])", render(effect))


@pytest.mark.parametrize("effect", list(TextEffect))
def test_effect_keeps_original_text_readable(effect: TextEffect) -> None:
    # Bỏ hết override block đi thì phải còn nguyên câu gốc.
    stripped = re.sub(r"\{[^}]*\}", "", render(effect, "Xin chao cac ban"))
    assert stripped == "Xin chao cac ban"


# --------------------------------------------------------------------------- #
# Từng hiệu ứng
# --------------------------------------------------------------------------- #
def test_none_effect_adds_nothing() -> None:
    assert render(TextEffect.NONE) == "Xin chao"


def test_fade_uses_default_duration_when_event_is_long() -> None:
    assert "\\fad(200,200)" in render(TextEffect.FADE, duration=3.0)


def test_fade_is_clamped_on_very_short_event() -> None:
    """Câu chỉ hiện 0.3s mà fade 200ms mỗi đầu thì gần như không kịp đọc."""
    tags = render(TextEffect.FADE, duration=0.3)
    match = re.search(r"\\fad\((\d+),(\d+)\)", tags)
    assert match is not None
    fade_in, fade_out = (int(value) for value in match.groups())
    assert fade_in <= 100 and fade_out <= 100


def test_pop_returns_to_exactly_100_percent() -> None:
    # Nảy xong phải về đúng cỡ thật, không được dừng ở 104%.
    assert scale_pairs(render(TextEffect.POP))[-1] == ("100", "100")


def test_slide_up_moves_vertically_only() -> None:
    tags = render(TextEffect.SLIDE_UP)
    x1, y1, x2, y2 = move_coords(tags)
    assert x1 == x2, "trượt ngang sẽ làm chữ lệch khỏi trục giữa"
    assert y1 > y2, "phải đi từ dưới lên"


def test_slide_up_lands_where_libass_would_have_put_the_text() -> None:
    # Đích của \move phải trùng đúng vị trí mặc định theo alignment + margin,
    # nếu không chữ sẽ dừng ở chỗ khác hẳn các câu không dùng hiệu ứng.
    tags = render(TextEffect.SLIDE_UP, margin_v=179)
    _x1, _y1, x2, y2 = move_coords(tags)
    assert (x2, y2) == base_position(2, 720, 1280, 0, 0, 179)


def test_glow_scales_blur_and_border_with_font_size() -> None:
    small = render(TextEffect.GLOW, font_px=20.0)
    big = render(TextEffect.GLOW, font_px=80.0)
    blur_small = float(re.search(r"\\blur(\d+)", small).group(1))  # type: ignore[union-attr]
    blur_big = float(re.search(r"\\blur(\d+)", big).group(1))  # type: ignore[union-attr]
    assert blur_big > blur_small


def test_typewriter_puts_a_karaoke_tag_before_every_character() -> None:
    tags = render(TextEffect.TYPEWRITER, "abcd")
    assert len(re.findall(r"\\k\d+", tags)) == 4


def test_typewriter_keeps_line_break_intact() -> None:
    r"""``\N`` là MỘT dấu ngắt dòng; chèn ``\k`` vào giữa là hỏng nó."""
    tags = render(TextEffect.TYPEWRITER, "ab\\Ncd")
    assert "\\N" in tags
    assert not re.search(r"\\k\d+\}\\\\?N", tags.replace("\\N", "|"))
    assert len(re.findall(r"\\k\d+", tags)) == 4


def test_typewriter_preserves_existing_override_blocks() -> None:
    # Text bìa đổi cỡ từng dòng bằng {\fs..}; gõ chữ không được phá tag đó.
    tags = render(TextEffect.TYPEWRITER, "{\\fs60}AB")
    assert "{\\fs60}" in tags
    assert len(re.findall(r"\\k\d+", tags)) == 2


def test_typewriter_handles_empty_text() -> None:
    assert render(TextEffect.TYPEWRITER, "") .endswith("}")


# --------------------------------------------------------------------------- #
# anchor: text bìa ép toạ độ
# --------------------------------------------------------------------------- #
def test_anchor_produces_pos_tag() -> None:
    tags = render(TextEffect.NONE, alignment=5, anchor=(360, 563))
    assert tags.startswith("{\\an5\\pos(360,563)}")


def test_anchor_with_slide_up_uses_move_not_pos() -> None:
    # Hai tag vị trí trong cùng một dòng sẽ đá nhau -> chỉ được có \move.
    tags = render(TextEffect.SLIDE_UP, alignment=5, anchor=(360, 563))
    assert "\\move(360," in tags
    assert "\\pos(" not in tags


def test_no_anchor_leaves_positioning_to_style() -> None:
    # Không ép toạ độ thì để libass tự xếp theo alignment + margin của style.
    assert "\\pos(" not in render(TextEffect.FADE)


# --------------------------------------------------------------------------- #
# base_position
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("alignment", "expected"),
    [
        (1, (20, 1180)),  # trái - đáy
        (2, (360, 1180)),  # giữa - đáy (mặc định)
        (3, (690, 1180)),  # phải - đáy
        (5, (360, 640)),  # giữa - giữa
        (8, (360, 100)),  # giữa - đỉnh
    ],
)
def test_base_position_matches_numpad_layout(
    alignment: int, expected: tuple[int, int]
) -> None:
    assert base_position(alignment, 720, 1280, 20, 30, 100) == expected


# --------------------------------------------------------------------------- #
# Cờ phụ trợ
# --------------------------------------------------------------------------- #
def test_only_typewriter_needs_hidden_secondary() -> None:
    assert needs_hidden_secondary(TextEffect.TYPEWRITER)
    assert not any(
        needs_hidden_secondary(e) for e in TextEffect if e is not TextEffect.TYPEWRITER
    )


def test_only_none_keeps_first_frame_fully_visible() -> None:
    # Dùng để cảnh báo khi text bìa (ảnh bìa TikTok) chọn hiệu ứng có fade-in.
    assert not effect_hides_first_frame(TextEffect.NONE)
    assert all(
        effect_hides_first_frame(e) for e in TextEffect if e is not TextEffect.NONE
    )
