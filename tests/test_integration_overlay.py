"""Test tích hợp bằng ffmpeg thật cho text bìa, hiệu ứng chữ và ghép clip.

Toàn bộ kiểm chứng ở đây đo trên PIXEL của frame do ffmpeg dựng ra, không chỉ
so chuỗi argv: những thứ như "chữ có bị méo không", "frame đầu tiên có đủ chữ
để làm ảnh bìa không" chỉ nhìn thấy được trên ảnh thật.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest

from app.models import RenderOptions
from app.overlay import plan_burn

pytestmark = [
    pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="cần ffmpeg trong PATH"),
    pytest.mark.skipif(shutil.which("ffprobe") is None, reason="cần ffprobe trong PATH"),
]

INTRO_TEXT = "2tr9/nguoi\n3N2D tai GARRYA MU CANG CHAI\n(Free nang hang Villa Ho Boi rieng)"


async def run_ok(*args: str, cwd: Path | None = None) -> None:
    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(cwd) if cwd else None,
    )
    _out, err = await process.communicate()
    assert process.returncode == 0, err.decode("utf-8", "replace")[-3000:]


async def gray_pixels(frame: Path, width: int, height: int, tmp_path: Path) -> bytes:
    """Frame về dạng thang xám thô, mỗi byte là một pixel."""
    raw = tmp_path / f"{frame.stem}.gray"
    await run_ok(
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(frame), "-pix_fmt", "gray", "-f", "rawvideo", str(raw),
    )
    data = raw.read_bytes()
    assert len(data) >= width * height
    return data[: width * height]


def bright_bbox(
    data: bytes, width: int, height: int, threshold: int = 170
) -> tuple[int, int, int, int] | None:
    """(x_min, y_min, x_max, y_max) của vùng sáng, None nếu không có gì.

    Nền test luôn là màu đen tuyền nên mọi pixel sáng đều là chữ — dùng nền
    nhiều màu (test pattern) thì các dải màu sáng sẽ bị nhận nhầm thành chữ.
    """
    xs: list[int] = []
    ys: list[int] = []
    for y in range(height):
        row = data[y * width : (y + 1) * width]
        hits = [x for x, value in enumerate(row) if value > threshold]
        if hits:
            ys.append(y)
            xs.extend(hits)
    if not xs:
        return None
    return min(xs), min(ys), max(xs), max(ys)


async def render_frame(
    tmp_path: Path,
    subs_filter: str,
    width: int,
    height: int,
    *,
    at: float = 0.0,
    name: str = "frame.png",
) -> bytes:
    """Dựng một frame nền đen có burn phụ đề, trả về pixel thang xám."""
    frame = tmp_path / name
    await run_ok(
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"color=c=black:s={width}x{height}:d=6",
        "-vf", subs_filter, "-ss", str(at), "-frames:v", "1", str(frame),
        cwd=tmp_path,
    )
    return await gray_pixels(frame, width, height, tmp_path)


def write_srt(workspace: Path, body: str, end: str = "00:00:05,000") -> str:
    (workspace / "subs.srt").write_text(
        f"1\n00:00:00,000 --> {end}\n{body}\n", encoding="utf-8"
    )
    return "subs.srt"


# --------------------------------------------------------------------------- #
# Chữ KHÔNG bị méo — yêu cầu chính
# --------------------------------------------------------------------------- #
async def test_glyph_shape_is_identical_on_every_aspect_ratio(tmp_path: Path) -> None:
    """Cùng cỡ chữ pixel -> cùng hình dạng chữ, bất kể tỉ lệ khung hình.

    Đây là phép đo trực tiếp cho yêu cầu "không méo". Nếu hệ toạ độ ASS lệch
    tỉ lệ so với khung hình (đúng thứ xảy ra khi PlayResX/Y bị cố định 384x288
    như ffmpeg tự sinh) thì chữ bị kéo ngang hoặc dọc, và tỉ lệ ngang/cao của
    CÙNG một glyph sẽ khác nhau giữa khung dọc, khung vuông và khung siêu rộng.
    """
    write_srt(tmp_path, "OOOO")
    opts = RenderOptions.model_validate(
        {"subtitle": {"font_size": 80, "effect": "none", "italic": False}}
    )
    ratios: dict[str, float] = {}
    for width, height in [(720, 1280), (1280, 720), (1080, 1080), (1920, 816)]:
        plan = plan_burn(tmp_path, "subs.srt", opts, width, height)
        data = await render_frame(
            tmp_path, f"subtitles={plan.subs}", width, height, name=f"g{width}x{height}.png"
        )
        box = bright_bbox(data, width, height)
        assert box is not None, f"{width}x{height}: không thấy chữ nào được vẽ"
        glyph_width = (box[2] - box[0] + 1) / 4  # bốn chữ O liền nhau
        glyph_height = box[3] - box[1] + 1
        ratios[f"{width}x{height}"] = glyph_width / glyph_height

    spread = max(ratios.values()) - min(ratios.values())
    assert spread < 0.03, f"hình dạng chữ lệch giữa các tỉ lệ khung: {ratios}"


async def test_glyph_height_matches_requested_pixel_size(tmp_path: Path) -> None:
    """font_size khai bằng pixel thì chữ phải cao đúng chừng đó pixel.

    Trước đây cỡ chữ đi qua hệ toạ độ ảo PlayResY=288 nên con số khai trong API
    chẳng liên quan gì tới pixel thật; giờ PlayRes = khung hình nên khai bao
    nhiêu ra bấy nhiêu.
    """
    write_srt(tmp_path, "HHHH")
    opts = RenderOptions.model_validate(
        {"subtitle": {"font_size": 100, "effect": "none", "italic": False}}
    )
    plan = plan_burn(tmp_path, "subs.srt", opts, 720, 1280)
    data = await render_frame(tmp_path, f"subtitles={plan.subs}", 720, 1280)
    box = bright_bbox(data, 720, 1280)
    assert box is not None
    cap_height = box[3] - box[1] + 1
    # Chữ hoa KHÔNG cao bằng cỡ chữ — nó chỉ là một phần của cỡ chữ, và phần đó
    # bao nhiêu thì tuỳ font lẫn cách libass quy FontSize ra pixel:
    #
    #   libass gọi FreeType với FT_SIZE_REQUEST_TYPE_REAL_DIM, nghĩa là FontSize
    #   ứng với (ascender - descender) chứ KHÔNG phải ô em.
    #   Liberation Serif: capHeight=1341, em=2048, ascender-descender=2268
    #     -> real_dim: 1341/2268 = 0.591   (đo thật trên CI: 0.590)
    #     -> nominal : 1341/2048 = 0.655   (nếu libass đổi sang quy theo em)
    #
    # Băng dưới đây phủ CẢ HAI quy ước để đổi bản libass không làm đỏ CI vô cớ,
    # nhưng vẫn bắt được đúng thứ test này sinh ra để canh: hệ toạ độ sai. Rơi
    # lại PlayResY=288 trên khung 1280 là lệch 4.4 lần (ratio ~2.6 hoặc ~0.13),
    # cách băng này rất xa. Đo lại bằng scripts/diagnose_glyph_size.py.
    ratio = cap_height / 100
    assert 0.55 <= ratio <= 0.72, f"chữ hoa cao {cap_height}px trên cỡ 100px (ratio {ratio})"


# --------------------------------------------------------------------------- #
# Lề và tự xuống dòng
# --------------------------------------------------------------------------- #
async def test_long_subtitle_stays_inside_margins(tmp_path: Path) -> None:
    width, height = 720, 1280
    write_srt(
        tmp_path,
        "Lafesta Phu Quoc chao mung nam moi voi phao hoa ruc ro tren bau troi dem nay",
    )
    plan = plan_burn(tmp_path, "subs.srt", RenderOptions(), width, height)
    # Hiệu ứng mặc định là fade: tại t=0 chữ còn trong suốt hoàn toàn.
    data = await render_frame(tmp_path, f"subtitles={plan.subs}", width, height, at=2.0)

    box = bright_bbox(data, width, height)
    assert box is not None
    x_min, _y_min, x_max, _y_max = box
    # Lề tự tính = 720 × 0.06 = 43px; cho phép viền chữ ăn ra vài pixel.
    assert x_min >= 35, f"lề trái chỉ {x_min}px"
    assert width - 1 - x_max >= 35, f"lề phải chỉ {width - 1 - x_max}px"


async def test_all_caps_subtitle_also_stays_inside_margins(tmp_path: Path) -> None:
    """Câu VIẾT HOA rộng hơn nhiều so với cùng số ký tự viết thường.

    Đây là ca mà cách ước lượng bằng một bề rộng ký tự trung bình duy nhất tính
    hụt và để chữ tràn mép.
    """
    width, height = 720, 1280
    write_srt(tmp_path, "KHUYEN MAI CUC LON CHO KHACH DAT PHONG SOM TRONG THANG NAY")
    plan = plan_burn(tmp_path, "subs.srt", RenderOptions(), width, height)
    data = await render_frame(tmp_path, f"subtitles={plan.subs}", width, height, at=2.0)

    box = bright_bbox(data, width, height)
    assert box is not None
    assert box[0] >= 25 and width - 1 - box[2] >= 25, f"bbox {box}"


async def test_subtitle_sits_above_the_bottom_ui_area(tmp_path: Path) -> None:
    # Lề đáy mặc định 14% chiều cao để không bị thanh nút TikTok/Reels che.
    width, height = 720, 1280
    write_srt(tmp_path, "Moi villa 1 trai nghiem")
    plan = plan_burn(tmp_path, "subs.srt", RenderOptions(), width, height)
    data = await render_frame(tmp_path, f"subtitles={plan.subs}", width, height, at=2.0)
    box = bright_bbox(data, width, height)
    assert box is not None
    assert height - 1 - box[3] >= 150, f"đáy chữ chỉ cách mép {height - 1 - box[3]}px"


# --------------------------------------------------------------------------- #
# Text bìa cho TikTok
# --------------------------------------------------------------------------- #
async def test_intro_text_is_fully_visible_on_the_very_first_frame(
    tmp_path: Path,
) -> None:
    """TikTok lấy frame ĐẦU TIÊN làm ảnh bìa nên chữ phải hiện đủ ngay tại t=0."""
    width, height = 720, 1280
    opts = RenderOptions.model_validate({"intro": {"text": INTRO_TEXT, "duration": 2.0}})
    plan = plan_burn(tmp_path, None, opts, width, height)
    assert plan.overlay is not None

    first = await render_frame(
        tmp_path, f"subtitles={plan.overlay}", width, height, at=0.0, name="f0.png"
    )
    box = bright_bbox(first, width, height)
    assert box is not None, "frame đầu tiên không có chữ -> ảnh bìa TikTok sẽ trống"
    # Ba dòng chữ chiếm một khối cao rõ rệt quanh giữa khung.
    assert box[3] - box[1] > height * 0.06
    assert box[1] > height * 0.2 and box[3] < height * 0.8


async def test_intro_text_disappears_after_its_duration(tmp_path: Path) -> None:
    width, height = 720, 1280
    opts = RenderOptions.model_validate({"intro": {"text": INTRO_TEXT, "duration": 1.0}})
    plan = plan_burn(tmp_path, None, opts, width, height)
    later = await render_frame(
        tmp_path, f"subtitles={plan.overlay}", width, height, at=3.0, name="f3.png"
    )
    assert bright_bbox(later, width, height) is None, "text bìa vẫn còn sau khi hết hạn"


async def test_intro_headline_is_larger_than_the_other_lines(tmp_path: Path) -> None:
    """Dòng đầu (con số/lời chào) phải đập vào mắt trước — như ảnh mẫu."""
    width, height = 720, 1280
    opts = RenderOptions.model_validate({"intro": {"text": "2tr9\nmot dong nho hon"}})
    plan = plan_burn(tmp_path, None, opts, width, height)
    data = await render_frame(tmp_path, f"subtitles={plan.overlay}", width, height)

    box = bright_bbox(data, width, height)
    assert box is not None
    y_min, y_max = box[1], box[3]
    middle = (y_min + y_max) // 2
    top_rows = _row_heights(data, width, y_min, middle)
    bottom_rows = _row_heights(data, width, middle, y_max)
    assert top_rows > bottom_rows, "dòng đầu phải cao hơn dòng sau"


def _row_heights(data: bytes, width: int, y_from: int, y_to: int) -> int:
    """Số dòng pixel có chữ trong khoảng [y_from, y_to]."""
    return sum(
        1
        for y in range(y_from, y_to + 1)
        if any(value > 170 for value in data[y * width : (y + 1) * width])
    )


async def test_intro_and_dialogue_burn_together_from_one_file(tmp_path: Path) -> None:
    width, height = 720, 1280
    write_srt(tmp_path, "Moi villa 1 trai nghiem", end="00:00:06,000")
    opts = RenderOptions.model_validate({"intro": {"text": "2tr9/nguoi", "duration": 2.0}})
    plan = plan_burn(tmp_path, "subs.srt", opts, width, height)
    assert plan.overlay is None  # gộp chung, chỉ một filter subtitles=

    # t=1.0: text bìa còn hiện (2s) và lời thoại đã fade-in xong.
    data = await render_frame(tmp_path, f"subtitles={plan.subs}", width, height, at=1.0)
    box = bright_bbox(data, width, height)
    assert box is not None
    # Text bìa nằm quanh 44% chiều cao, lời thoại nằm sát đáy -> khối chữ trải
    # từ trên giữa xuống gần đáy.
    assert box[1] < height * 0.5 < box[3]


# --------------------------------------------------------------------------- #
# Hiệu ứng chữ chạy được thật trên libass
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "effect", ["none", "fade", "pop", "slide_up", "typewriter", "glow"]
)
async def test_every_effect_renders_visible_text(tmp_path: Path, effect: str) -> None:
    """Tag sai cú pháp thì libass âm thầm bỏ qua cả dòng -> frame trống trơn."""
    width, height = 720, 1280
    write_srt(tmp_path, "Moi villa 1 trai nghiem", end="00:00:06,000")
    opts = RenderOptions.model_validate({"subtitle": {"effect": effect}})
    plan = plan_burn(tmp_path, "subs.srt", opts, width, height)
    # Lấy frame ở giữa: mọi hiệu ứng đều đã chạy xong phần vào.
    data = await render_frame(
        tmp_path, f"subtitles={plan.subs}", width, height, at=3.0, name=f"{effect}.png"
    )
    box = bright_bbox(data, width, height)
    assert box is not None, f"hiệu ứng {effect} làm chữ biến mất"
    assert box[2] - box[0] > 100, f"hiệu ứng {effect} chỉ vẽ được {box[2] - box[0]}px chữ"
