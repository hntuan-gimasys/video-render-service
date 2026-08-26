"""Cú pháp gọn cho ô ``clips`` trên form — tách khỏi app/clips.py (§4.2 SPEC).

Tách riêng để giữ mỗi file dưới 400 dòng: ở đây chỉ có phần ĐỌC CÚ PHÁP, không
đụng gì tới việc ghép/chuyển cảnh.
"""

from __future__ import annotations

import re
from typing import Final

from app.models import ClipSpec
from app.utils import InvalidOptions

__all__ = ["parse_clip_lines"]

_SOURCE_TOKEN: Final[re.Pattern[str]] = re.compile(r"^(\d{1,2})[:.)]?$")
_LINE_SPLIT: Final[re.Pattern[str]] = re.compile(r"[\n;]+")


def parse_clip_lines(text: str) -> list[ClipSpec]:
    """Đọc cú pháp gọn một dòng một đoạn, tiện gõ thẳng trong Swagger::

        1 00:00-00:05
        2 0:10-0:18
        1 65-72.5
        3

    Số đầu dòng là SỐ HIỆU VIDEO (đánh số từ 1 theo thứ tự tải lên). Dòng chỉ
    có mỗi số hiệu nghĩa là lấy trọn video đó. Ngăn cách bằng xuống dòng hoặc
    dấu ``;`` (ô nhập một dòng của Swagger bóp mất newline). Dòng bắt đầu bằng
    ``#`` là ghi chú.
    """
    specs: list[ClipSpec] = []
    for raw in _LINE_SPLIT.split(text):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = line.replace("->", "-").replace("–", "-").replace("—", "-")
        tokens = line.split()
        source = 1
        if len(tokens) > 1 and _SOURCE_TOKEN.match(tokens[0]):
            source = int(_SOURCE_TOKEN.match(tokens[0]).group(1))  # type: ignore[union-attr]
            tokens = tokens[1:]
        rest = " ".join(tokens)

        if "-" in rest:
            start_text, _, end_text = rest.partition("-")
        elif len(tokens) == 2:
            start_text, end_text = tokens
        elif len(tokens) == 1 and (only := _SOURCE_TOKEN.match(tokens[0])):
            specs.append(ClipSpec(source=int(only.group(1))))
            continue
        else:
            raise InvalidOptions(
                f"Không hiểu dòng clip {raw.strip()!r}",
                detail="Dạng đúng: '<số hiệu video> <bắt đầu>-<kết thúc>', ví dụ '2 00:10-00:18'",
            )
        try:
            specs.append(
                ClipSpec(source=source, start=start_text.strip(), end=end_text.strip())
            )
        except ValueError as exc:
            raise InvalidOptions(f"Đoạn {raw.strip()!r} không hợp lệ", detail=str(exc)) from exc
    return specs
