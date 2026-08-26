"""Chẩn đoán: font_size khai bằng pixel thì chữ ra cao bao nhiêu pixel thật?

Dùng để trả lời dứt điểm vì sao
``tests/test_integration_overlay.py::test_glyph_height_matches_requested_pixel_size``
thất bại: do libass chọn NHẦM FONT, hay do NGƯỠNG trong test sai ngay từ đầu.

Đọc kết quả:

* ``ratio`` (cap_height / font_size) **không đổi** giữa các cỡ chữ
  -> đây là quy ước quy đổi cố định của libass, code không hỏng, chỉ có ngưỡng
  trong test cần chỉnh lại cho đúng.
* ``ratio`` **thay đổi** theo cỡ chữ -> có gì đó hỏng thật, phải sửa code.
* ``ratio`` khớp ``expect_real_dim`` -> libass dùng FT_SIZE_REQUEST_TYPE_REAL_DIM
  (quy FontSize theo ascender-descender). Khớp ``expect_nominal`` -> quy theo em.

Script chỉ ĐO và IN, không assert gì — nó là công cụ chẩn đoán, không phải test.
Xoá đi khi đã chốt được ngưỡng đúng.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from app.models import RenderOptions
from app.overlay import plan_burn
from tests.test_integration_overlay import bright_bbox, render_frame, write_srt

WIDTH, HEIGHT = 720, 1280
SIZES = (50, 100, 200)
# Ngưỡng của chính test (170) và một ngưỡng thấp hơn nhiều: chênh lệch giữa hai
# con số cho biết bao nhiêu pixel bị mất vì khử răng cưa ở mép chữ.
THRESHOLDS = (170, 40)


def _run(*args: str) -> str:
    try:
        out = subprocess.run(args, capture_output=True, text=True, check=False)
    except OSError as exc:  # fc-match không có trong PATH
        print(f"  (không chạy được {args[0]}: {exc})")
        return ""
    return out.stdout.strip()


def report_font(family: str) -> dict[str, float] | None:
    """In font mà fontconfig thực sự trả về + metric bên trong file font."""
    path = _run("fc-match", "-f", "%{file}", family)
    name = _run("fc-match", "-f", "%{family} / %{style}", family)
    print(f"  fc-match {family!r} -> {name}")
    print(f"  file: {path}")
    if not path:
        return None

    try:
        from fontTools.ttLib import TTFont
    except ImportError:
        print("  (không có fontTools -> bỏ qua metric)")
        return None

    font = TTFont(path, fontNumber=0, lazy=True)
    upem = font["head"].unitsPerEm
    ascender = font["hhea"].ascender
    descender = font["hhea"].descender
    # OS/2 chỉ khai sCapHeight từ version 2 trở lên -> luôn phải có đường lui.
    os2 = font["OS/2"] if "OS/2" in font else None
    cap = getattr(os2, "sCapHeight", 0) or 0
    font.close()

    span = ascender - descender
    print(f"  unitsPerEm={upem} ascender={ascender} descender={descender} capHeight={cap}")
    if not cap:
        print("  (font không khai sCapHeight -> không tính được kỳ vọng)")
        return None

    # Hai mô hình quy FontSize -> pixel mà libass/FreeType có thể dùng.
    nominal = cap / upem
    real_dim = cap / span if span else 0.0
    print(f"  expect_nominal  (FontSize = em)              -> ratio {nominal:.3f}")
    print(f"  expect_real_dim (FontSize = asc-desc)        -> ratio {real_dim:.3f}")
    return {"nominal": nominal, "real_dim": real_dim}


async def measure(workspace: Path, size: int, *, bold: bool) -> None:
    """Render 'HHHH' ở một cỡ chữ rồi in chiều cao thật đo được."""
    write_srt(workspace, "HHHH")
    opts = RenderOptions.model_validate(
        {"subtitle": {"font_size": size, "effect": "none", "italic": False, "bold": bold}}
    )
    plan = plan_burn(workspace, "subs.srt", opts, WIDTH, HEIGHT)

    # In đúng dòng Style trong .ass để chắc chắn FontSize ghi ra là con số ta nghĩ.
    style = next(
        (
            line
            for line in (workspace / str(plan.subs)).read_text(encoding="utf-8").splitlines()
            if line.startswith("Style:")
        ),
        "(không thấy dòng Style)",
    )

    data = await render_frame(
        workspace, f"subtitles={plan.subs}", WIDTH, HEIGHT, name=f"d{size}{bold}.png"
    )
    print(f"  font_size={size} bold={bold}")
    print(f"    {style}")
    for threshold in THRESHOLDS:
        box = bright_bbox(data, WIDTH, HEIGHT, threshold=threshold)
        if box is None:
            print(f"    threshold={threshold}: KHÔNG thấy chữ nào")
            continue
        cap_height = box[3] - box[1] + 1
        print(
            f"    threshold={threshold}: cap_height={cap_height}px"
            f"  ratio={cap_height / size:.3f}  bbox={box}"
        )


async def main() -> int:
    print("=== Font mà libass sẽ dùng ===")
    report_font("Liberation Serif")
    print()
    print("=== Chiều cao chữ thật theo từng cỡ ===")
    with TemporaryDirectory() as tmp:
        workspace = Path(tmp)
        for size in SIZES:
            await measure(workspace, size, bold=True)
        # bold=False để tách ảnh hưởng của việc in đậm (mặc định SPEC là bold).
        await measure(workspace, 100, bold=False)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
