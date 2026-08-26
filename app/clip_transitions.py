"""Chuyển cảnh (crossfade) giữa các đoạn khi ghép nhiều clip — app/clips.py.

Gom cả SCHEMA (``TransitionOptions``) lẫn phần dựng filter ``xfade``/
``acrossfade`` vào một file: cả hai đều chỉ phục vụ đúng một tính năng, tách
theo "loại code" (schema riêng, hàm riêng) ra hai file sẽ chỉ làm khó tìm hơn.
Tách khỏi ``app/models.py``/``app/clips.py`` để giữ hai file đó dưới 400 dòng.

Mặc định BẬT: cắt cứng giữa hai cảnh là đúng thứ gây cảm giác giật.
``xfade``/``acrossfade`` làm khung hình cũ mờ dần trong khi khung hình mới
hiện dần lên, chồng lên nhau ``duration`` giây, thay vì đổi đột ngột.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Final

from pydantic import BaseModel, ConfigDict, Field, field_validator

if TYPE_CHECKING:  # pragma: no cover
    from app.clips import ResolvedClip

__all__ = [
    "TRANSITION_STYLE_WHITELIST",
    "TransitionOptions",
    "pair_transition_seconds",
    "total_overlap_seconds",
    "video_xfade_chain",
    "audio_xfade_chain",
]

# Tên hiệu ứng của filter xfade (ffmpeg). Chỉ liệt kê những kiểu đã kiểm chứng
# chạy được trên ffmpeg của container (bản đầy đủ còn nhiều kiểu khác nhưng
# một số kén phiên bản libavfilter).
TRANSITION_STYLE_WHITELIST: Final[frozenset[str]] = frozenset(
    {
        "fade",
        "fadeblack",
        "fadewhite",
        "dissolve",
        "wipeleft",
        "wiperight",
        "wipeup",
        "wipedown",
        "slideleft",
        "slideright",
        "slideup",
        "slidedown",
        "smoothleft",
        "smoothright",
        "smoothup",
        "smoothdown",
        "circlecrop",
        "circleopen",
        "circleclose",
        "radial",
        "distance",
        "zoomin",
        "hblur",
        "pixelize",
    }
)


class TransitionOptions(BaseModel):
    """Hiệu ứng chuyển cảnh giữa các đoạn khi ghép nhiều clip.

    Mặc định BẬT: cắt cứng giữa các cảnh (hành vi cũ) là thứ gây cảm giác
    giật — người xem thấy khung hình đổi đột ngột không có gì báo trước.
    Crossfade (mặc định "fade") làm khung hình cũ mờ dần trong khi khung hình
    mới hiện dần lên, chồng lên nhau trong ``duration`` giây.

    Không có tác dụng khi chỉ có một đoạn (không có gì để chuyển cảnh).
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    enabled: bool = True
    duration: Annotated[float, Field(gt=0, le=5.0)] = 0.5
    style: str = "fade"

    @field_validator("style")
    @classmethod
    def _check_style(cls, value: str) -> str:
        if value not in TRANSITION_STYLE_WHITELIST:
            raise ValueError(f"style phải thuộc {sorted(TRANSITION_STYLE_WHITELIST)}")
        return value

# Sàn dưới của một lần crossfade: đủ để xfade/acrossfade luôn nhận giá trị
# dương hợp lệ, kể cả với đoạn cực ngắn (dưới cả sàn này thì gần như không
# nhìn thấy chuyển cảnh nữa, chỉ còn ý nghĩa "không làm ffmpeg lỗi").
_MIN_TRANSITION_SECONDS: Final[float] = 0.05
# Không cho một lần chuyển cảnh ăn quá nửa đoạn NGẮN HƠN trong hai đoạn liền kề
# — nếu không hiệu ứng nuốt gần hết một đoạn ngắn, nhìn còn giật hơn cắt cứng.
_TRANSITION_CLIP_FRACTION: Final[float] = 0.5


def pair_transition_seconds(left: float, right: float, configured: float) -> float:
    """Độ dài crossfade giữa hai đoạn liền kề, đã kẹp cho an toàn.

    Cố tình tính theo ĐÚNG HAI ĐOẠN GỐC liền kề, không theo tổng thời lượng đã
    ghép trước đó: xfade/acrossfade chỉ thật sự lấy từ đuôi đoạn trước + đầu
    đoạn sau, và với clip vài giây trở lên (trường hợp thường gặp) công thức
    này luôn an toàn. Theo dõi chặt hơn phần đuôi đã bị lần chuyển cảnh TRƯỚC
    đó "ăn" mất chỉ có ích khi ghép liên tiếp nhiều đoạn ngắn hơn 1 giây — biên
    quá hẹp để đáng bỏ công.
    """
    cap = min(left, right) * _TRANSITION_CLIP_FRACTION
    return round(max(_MIN_TRANSITION_SECONDS, min(configured, cap)), 3)


def total_overlap_seconds(clips: list[ResolvedClip], configured: float) -> float:
    """Tổng số giây bị chồng lấn qua tất cả các lần chuyển cảnh.

    Dùng để suy ra độ dài THẬT của file sau khi ghép (xem
    ``app.clips.merged_duration``): mỗi lần crossfade làm tổng thời lượng ngắn
    đi đúng bằng ``duration`` của lần đó.
    """
    return sum(
        pair_transition_seconds(clips[i - 1].duration, clips[i].duration, configured)
        for i in range(1, len(clips))
    )


def video_xfade_chain(clips: list[ResolvedClip], style: str, configured_duration: float) -> str:
    """Chuỗi filter nối [v0]..[v(n-1)] bằng xfade, output cuối luôn tên ``[v]``.

    xfade cần ``offset``: mốc TRONG STREAM ĐẦU (đã cộng dồn) mà transition bắt
    đầu. Mỗi lần ghép làm tổng thời lượng cộng dồn ngắn đi đúng ``duration``
    giây (hai đoạn chồng lên nhau), nên phải trừ dần khi tính offset cho lần
    kế tiếp — không thì transition sau lại bắt đầu trễ hơn thực tế.
    """
    chains: list[str] = []
    prev_label = "v0"
    running = clips[0].duration
    last = len(clips) - 1
    for index in range(1, len(clips)):
        duration = pair_transition_seconds(
            clips[index - 1].duration, clips[index].duration, configured_duration
        )
        offset = max(0.0, running - duration)
        out_label = "v" if index == last else f"vx{index}"
        chains.append(
            f"[{prev_label}][v{index}]xfade=transition={style}:duration={_fmt(duration)}:"
            f"offset={_fmt(offset)}[{out_label}]"
        )
        running = running + clips[index].duration - duration
        prev_label = out_label
    return ";".join(chains)


def audio_xfade_chain(clips: list[ResolvedClip], configured_duration: float) -> str:
    """Chuỗi filter nối [a0]..[a(n-1)] bằng acrossfade, output cuối tên ``[a]``.

    ``acrossfade`` tự chồng đuôi luồng trước với đầu luồng sau, không cần
    offset — miễn ``duration`` mỗi cặp khớp với bên video (cùng công thức
    :func:`pair_transition_seconds`) thì hình và tiếng luôn đồng bộ, vì cả hai
    bên rút ngắn tổng thời lượng đúng bằng nhau ở mỗi lần ghép.
    """
    chains: list[str] = []
    prev_label = "a0"
    last = len(clips) - 1
    for index in range(1, len(clips)):
        duration = pair_transition_seconds(
            clips[index - 1].duration, clips[index].duration, configured_duration
        )
        out_label = "a" if index == last else f"ax{index}"
        chains.append(f"[{prev_label}][a{index}]acrossfade=d={_fmt(duration)}[{out_label}]")
        prev_label = out_label
    return ";".join(chains)


def _fmt(value: float) -> str:
    """Số cho argv/filter: bỏ '.0' cho gọn và khớp snapshot test."""
    if float(value).is_integer():
        return str(int(value))
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    return text or "0"
