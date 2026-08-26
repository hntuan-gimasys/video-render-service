r"""Hiệu ứng chữ dạng override tag của ASS/libass.

Mọi hiệu ứng ở đây đều là tag mà libass diễn giải khi VẼ chữ, nên chữ luôn
được rasterise từ vector ở đúng độ phân giải output — không có bước phóng to
ảnh bitmap nào, chữ không bao giờ bị rỗ hay nhoè.

Nguyên tắc chống méo: mọi phép phóng to đều đặt ``\fscx`` BẰNG ĐÚNG ``\fscy``.
Chỉ cần hai giá trị đó lệch nhau là chữ bị kéo ngang/dọc — đúng thứ cần tránh.
"""

from __future__ import annotations

import re
from typing import Final

from app.models import TextEffect

__all__ = [
    "apply_effect",
    "base_position",
    "effect_hides_first_frame",
    "needs_hidden_secondary",
]

# Fade mặc định (ms). Ngắn để phụ đề không "chậm chạp" so với lời thoại.
_FADE_IN_MS: Final[int] = 200
_FADE_OUT_MS: Final[int] = 200
# Chữ hiện dần tối đa 1.5s, dài hơn thì người xem sốt ruột.
_TYPEWRITER_MAX_CS: Final[int] = 150
_TYPEWRITER_SHARE: Final[float] = 0.6
_SLIDE_MS: Final[int] = 260
_LINE_BREAK: Final[str] = "\\N"
# Override block đã có sẵn trong chuỗi, phải giữ nguyên khi chèn karaoke tag.
_TAG_BLOCK: Final[re.Pattern[str]] = re.compile(r"(\{[^}]*\})")


def needs_hidden_secondary(effect: TextEffect) -> bool:
    r"""Hiệu ứng nào cần SecondaryColour trong suốt?

    ``\k`` (karaoke) vốn để ĐỔI MÀU chữ từ SecondaryColour sang PrimaryColour.
    Muốn nó thành máy chữ (ký tự chưa tới lượt thì chưa hiện) thì
    SecondaryColour phải trong suốt hoàn toàn — việc đó do bên dựng style lo.
    """
    return effect is TextEffect.TYPEWRITER


def effect_hides_first_frame(effect: TextEffect) -> bool:
    """Hiệu ứng có làm frame đầu tiên chưa hiện đủ chữ không?

    Quan trọng với text bìa: TikTok lấy frame ĐẦU TIÊN làm ảnh bìa, nên text
    bìa mà fade-in hay chạy chữ thì ảnh bìa ra trống trơn.
    """
    return effect is not TextEffect.NONE


def base_position(
    alignment: int, width: int, height: int, margin_l: int, margin_r: int, margin_v: int
) -> tuple[int, int]:
    r"""Toạ độ neo mà libass sẽ tự đặt chữ vào, theo alignment kiểu bàn phím số.

    Cần cho ``\move``: tag đó đòi toạ độ tuyệt đối, nên phải tự tính lại đúng
    chỗ mà chữ vốn dĩ sẽ nằm, rồi cho nó trượt TỚI đó.
    """
    column = (alignment - 1) % 3  # 0 = trái, 1 = giữa, 2 = phải
    row = (alignment - 1) // 3  # 0 = đáy, 1 = giữa, 2 = đỉnh
    x = (margin_l, width // 2, width - margin_r)[column]
    y = (height - margin_v, height // 2, margin_v)[row]
    return x, y


def _fade(duration: float, fade_in: int = _FADE_IN_MS, fade_out: int = _FADE_OUT_MS) -> str:
    r"""``\fad`` đã kẹp lại để hai đầu fade không nuốt hết thời gian hiển thị."""
    budget = max(0, int(duration * 1000))
    limit = budget // 3
    return f"\\fad({min(fade_in, limit)},{min(fade_out, limit)})"


def _typewriter(text: str, duration: float) -> str:
    r"""Chèn ``\k`` trước từng ký tự để chữ hiện dần như đang gõ.

    ``\N`` là MỘT dấu ngắt dòng chứ không phải hai ký tự rời, nên phải tách ra
    rồi ghép lại, tránh chèn ``\k`` vào giữa làm hỏng dấu ngắt.
    """
    # Chuỗi có thể đã chứa sẵn override block (ví dụ {\fs45} đổi cỡ từng dòng
    # của text bìa). Chèn \k vào giữa những block đó là hỏng tag, nên tách ra
    # giữ nguyên rồi chỉ gõ phần chữ thật.
    chunks = [_TAG_BLOCK.split(segment) for segment in text.split(_LINE_BREAK)]
    total_chars = sum(
        len(part) for parts in chunks for part in parts if not part.startswith("{")
    )
    if total_chars == 0:
        return text
    reveal_cs = min(_TYPEWRITER_MAX_CS, max(1, int(duration * 100 * _TYPEWRITER_SHARE)))
    per_char = max(1, reveal_cs // total_chars)
    typed = [
        "".join(
            part
            if part.startswith("{")
            else "".join(f"{{\\k{per_char}}}{char}" for char in part)
            for part in parts
        )
        for parts in chunks
    ]
    return _LINE_BREAK.join(typed)


def apply_effect(
    effect: TextEffect,
    text: str,
    *,
    duration: float,
    width: int,
    height: int,
    font_px: float,
    alignment: int,
    margin_l: int = 0,
    margin_r: int = 0,
    margin_v: int = 0,
    anchor: tuple[int, int] | None = None,
) -> str:
    r"""Bọc ``text`` bằng override tag của hiệu ứng đã chọn.

    ``anchor`` = toạ độ ép cứng cho khối chữ (text bìa cần đặt đúng chỗ giữa
    khung). Có ``anchor`` thì hàm này tự sinh luôn ``\pos``/``\move``, nơi gọi
    KHÔNG được thêm tag vị trí nữa — hai tag vị trí trong cùng một dòng sẽ đá
    nhau. Không có ``anchor`` thì để libass tự xếp theo alignment + margin của
    style, chỉ ``slide_up`` mới cần toạ độ và nó tự suy ra từ alignment.
    """
    if anchor is not None:
        anchor_x, anchor_y = anchor
    else:
        anchor_x, anchor_y = base_position(
            alignment, width, height, margin_l, margin_r, margin_v
        )

    if effect is TextEffect.SLIDE_UP:
        rise = max(8, round(font_px * 0.8))
        motion = (
            f"\\an{alignment}\\move({anchor_x},{anchor_y + rise},"
            f"{anchor_x},{anchor_y},0,{_SLIDE_MS})"
        )
    elif anchor is not None:
        motion = f"\\an{alignment}\\pos({anchor_x},{anchor_y})"
    else:
        motion = ""

    body = text
    if effect is TextEffect.NONE:
        extra = ""
    elif effect is TextEffect.FADE:
        extra = _fade(duration)
    elif effect is TextEffect.POP:
        # 70% -> 104% -> 100%: nảy nhẹ rồi về đúng cỡ. fscx == fscy ở MỌI mốc
        # nên chỉ là phóng to đều, không bao giờ méo.
        extra = (
            f"{_fade(duration, 80, 120)}\\fscx70\\fscy70"
            "\\t(0,150,\\fscx104\\fscy104)\\t(150,260,\\fscx100\\fscy100)"
        )
    elif effect is TextEffect.SLIDE_UP:
        extra = _fade(duration, 160, 140)
    elif effect is TextEffect.GLOW:
        # \blur làm mềm viền -> viền dày + blur = quầng sáng quanh chữ. Ruột
        # chữ vẫn sắc nét vì blur chỉ tác động lên biên và bóng.
        blur = max(1, round(font_px * 0.09))
        border = max(1, round(font_px * 0.14))
        extra = f"\\bord{border}\\blur{blur}{_fade(duration)}"
    elif effect is TextEffect.TYPEWRITER:
        extra = _fade(duration, 0, 150)
        body = _typewriter(text, duration)
    else:  # pragma: no cover - StrEnum đã chặn giá trị lạ từ API
        extra = ""

    tags = f"{motion}{extra}"
    return f"{{{tags}}}{body}" if tags else body
