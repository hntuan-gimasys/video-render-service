"""Pydantic v2 schemas — docs/SPEC.md §3 và §4.

Mọi field trong nhóm options đều có default nên client gửi ``{}`` (hoặc không
gửi gì) vẫn tạo được ``RenderOptions()`` hợp lệ.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.clip_transitions import TransitionOptions

# Whitelist preset của x264/x265 (SPEC §4).
PRESET_WHITELIST: Final[frozenset[str]] = frozenset(
    {
        "ultrafast",
        "superfast",
        "veryfast",
        "faster",
        "fast",
        "medium",
        "slow",
        "slower",
        "veryslow",
        "placebo",
    }
)

VIDEO_CODEC_WHITELIST: Final[frozenset[str]] = frozenset({"libx264", "libx265"})
AUDIO_CODEC_WHITELIST: Final[frozenset[str]] = frozenset({"aac", "libmp3lame", "libopus"})

_HEX_COLOR = r"^#(?:[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$"
_RESOLUTION = r"^\d+x\d+$"
_AUDIO_BITRATE = r"^\d{1,4}k$"
_FILENAME = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$"

# "12", "1:05", "01:02:03.5" -> giây. Cho phép nhập mốc thời gian kiểu người
# dùng quen tay thay vì bắt quy đổi ra giây.
_CLOCK: Final[re.Pattern[str]] = re.compile(
    r"^(?:(?:(\d+):)?([0-5]?\d):)?([0-5]?\d(?:[.,]\d{1,3})?)$"
)


def parse_time_value(value: float | int | str) -> float:
    """Số giây, nhận cả ``90``, ``"1:30"``, ``"00:01:30.5"``.

    Raise ``ValueError`` nếu không hiểu — Pydantic sẽ đổi thành 422.
    """
    if isinstance(value, (int, float)):
        seconds = float(value)
    else:
        text = str(value).strip()
        if not text:
            raise ValueError("mốc thời gian rỗng")
        match = _CLOCK.match(text)
        if match is None:
            try:
                seconds = float(text)
            except ValueError as exc:
                raise ValueError(
                    f"mốc thời gian không hợp lệ: {value!r} (dùng giây, MM:SS hoặc HH:MM:SS)"
                ) from exc
        else:
            hours, minutes, secs = match.groups()
            seconds = (
                int(hours or 0) * 3600
                + int(minutes or 0) * 60
                + float((secs or "0").replace(",", "."))
            )
    if seconds < 0:
        raise ValueError("mốc thời gian không được âm")
    if seconds > 86400:
        raise ValueError("mốc thời gian vượt quá 24 giờ")
    return seconds


class TextEffect(StrEnum):
    """Hiệu ứng chữ chọn được trước khi render (xem app/ass_effects.py).

    Tất cả đều do libass vẽ từ vector nên chữ luôn nét; mọi phép phóng to đều
    giữ tỉ lệ ngang = dọc nên không hiệu ứng nào làm méo chữ.
    """

    NONE = "none"  # hiện/tắt thẳng, không hiệu ứng
    FADE = "fade"  # mờ dần vào/ra (mặc định cho lời thoại)
    POP = "pop"  # nảy lên rồi về đúng cỡ
    SLIDE_UP = "slide_up"  # trượt từ dưới lên
    TYPEWRITER = "typewriter"  # hiện dần từng ký tự như đang gõ
    GLOW = "glow"  # viền dày làm mờ thành quầng sáng


class _Strict(BaseModel):
    """Base cho mọi options: chặn field lạ để lỗi chính tả không bị bỏ qua."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class SubtitleOptions(_Strict):
    enabled: bool = True
    mode: Literal["burn", "soft"] = "burn"
    # Liberation Serif có sẵn trong image (gói fonts-liberation2), chữ có
    # chân đậm kiểu caption phim/quảng cáo, đủ dấu tiếng Việt — đã kiểm chứng
    # bằng ảnh render thật trên container. Trước đây "Roboto" thì fonts/
    # trống nên libass âm thầm fallback sang DejaVu Sans (nét khác hẳn).
    font_name: str = Field(default="Liberation Serif", min_length=1, max_length=100)
    # MỌI số đo dưới đây tính bằng PIXEL của khung hình output. File .ass do
    # service tự sinh có PlayResX/PlayResY đúng bằng khung hình (xem
    # app/ass_doc.py) nên 1 đơn vị ASS = 1 pixel, không phải quy đổi gì.
    # None = tự tính theo BỀ NGANG video (xem subtitle_style.resolve_font_px).
    font_size: Annotated[int, Field(ge=6, le=400)] | None = None
    # Cỡ chữ tự động = font_size_ratio × bề ngang video (0.04 ≈ 4%).
    font_size_ratio: Annotated[float, Field(gt=0, le=0.5)] = 0.04
    primary_color: Annotated[str, Field(pattern=_HEX_COLOR)] = "#FFFFFF"
    outline_color: Annotated[str, Field(pattern=_HEX_COLOR)] = "#000000"
    back_color: Annotated[str, Field(pattern=_HEX_COLOR)] = "#80000000"
    border_style: Literal[1, 4] = 1  # SPEC §4: 1 = viền chữ, 4 = hộp nền
    # None = tự tính theo cỡ chữ (8%). Viền cố định thì video dọc (chữ to) bị
    # viền mảnh như không có, còn video nhỏ thì viền bệt vào ruột chữ.
    outline: Annotated[float, Field(ge=0, le=40)] | None = None
    shadow: Annotated[float, Field(ge=0, le=40)] = 0
    alignment: Annotated[int, Field(ge=1, le=9)] = 2
    # None = tự tính theo CHIỀU CAO video. 0.14 chừa đủ chỗ cho thanh nút của
    # TikTok/Reels che mất đáy khung.
    margin_v: Annotated[int, Field(ge=0, le=4000)] | None = None
    margin_v_ratio: Annotated[float, Field(gt=0, le=0.45)] = 0.14
    # None = tự tính theo BỀ NGANG video. Không có margin trái/phải thì chữ
    # dài kéo sát mép — đã dựng frame kiểm chứng: câu dài kéo từ x=8 tới x=690
    # trên khung rộng 720, chỉ còn 8px mỗi bên.
    margin_h: Annotated[int, Field(ge=0, le=4000)] | None = None
    margin_h_ratio: Annotated[float, Field(gt=0, le=0.3)] = 0.06
    bold: bool = True
    # Nghiêng: kiểu chữ serif nghiêng của caption du lịch/nghỉ dưỡng.
    italic: bool = True
    effect: TextEffect = TextEffect.FADE
    offset_seconds: Annotated[float, Field(ge=-86400, le=86400)] = 0.0


class IntroTextOptions(_Strict):
    """Text giới thiệu hiện tích tắc ở đầu video, dùng làm ảnh bìa TikTok.

    TikTok lấy FRAME ĐẦU TIÊN của video làm ảnh bìa, nên mặc định ``start=0``
    và ``effect=none``: chữ phải hiện đủ ngay từ frame 0, hiệu ứng fade-in sẽ
    làm ảnh bìa ra trống trơn.
    """

    enabled: bool = True
    # Nhiều dòng: xuống dòng thật, hoặc "|", hoặc "\n" gõ tay (ô nhập một dòng
    # của Swagger bóp mất newline thật — xem app/intake.py).
    text: str | None = Field(default=None, max_length=2000)
    start: Annotated[float, Field(ge=0, le=86400)] = 0.0
    duration: Annotated[float, Field(gt=0, le=60)] = 2.0
    font_name: str = Field(default="Liberation Sans", min_length=1, max_length=100)
    font_size: Annotated[int, Field(ge=6, le=400)] | None = None
    font_size_ratio: Annotated[float, Field(gt=0, le=0.5)] = 0.062
    # Dòng đầu to hơn phần còn lại (kiểu "2tr9/người" ở trên, chi tiết bên dưới).
    headline_scale: Annotated[float, Field(ge=0.5, le=4.0)] = 1.55
    primary_color: Annotated[str, Field(pattern=_HEX_COLOR)] = "#FFF200"
    outline_color: Annotated[str, Field(pattern=_HEX_COLOR)] = "#000000"
    back_color: Annotated[str, Field(pattern=_HEX_COLOR)] = "#80000000"
    border_style: Literal[1, 4] = 1
    outline: Annotated[float, Field(ge=0, le=40)] | None = None
    shadow: Annotated[float, Field(ge=0, le=40)] = 0
    # Vị trí khối chữ theo chiều dọc, 0 = sát đỉnh, 1 = sát đáy.
    position_ratio: Annotated[float, Field(ge=0.0, le=1.0)] = 0.44
    margin_h: Annotated[int, Field(ge=0, le=4000)] | None = None
    margin_h_ratio: Annotated[float, Field(gt=0, le=0.3)] = 0.05
    bold: bool = True
    italic: bool = False
    effect: TextEffect = TextEffect.NONE


class ClipSpec(BaseModel):
    """Một đoạn cắt ra từ một video nguồn.

    ``source`` nhận HAI dạng:

    * **số** — số hiệu video, đánh từ 1 theo thứ tự gửi lên;
    * **tên file** — khớp với tên file gốc của video nguồn (tên file upload,
      hoặc tên file trên Drive). Tiện khi kịch bản dựng do một pipeline khác
      sinh ra: nó chỉ biết tên file chứ không biết thứ tự ta nhận được.

    ``source_video`` là tên gọi khác của ``source``, để nhận thẳng kịch bản dạng
    ``video_edit_script`` mà không phải đổi tên khoá.

    Cố tình KHÔNG kế thừa ``_Strict`` (khác mọi options khác trong file này):
    kịch bản dựng do một pipeline AI khác sinh ra thường đính kèm ghi chú riêng
    của nó trên từng đoạn (từng thấy ``vibe_note`` mô tả ý đồ dựng cảnh) — field
    đó vô hại, chặn thẳng bằng ``extra="forbid"`` chỉ bắt người dùng phải tự lọc
    JSON trước mỗi lần gửi. Field lạ ở đây nhiều khả năng là chú thích của
    pipeline khác, không phải lỗi gõ chính tả như với các options người dùng tự
    gõ tay.

    Thứ tự các phần tử trong ``options.clips`` chính là thứ tự ghép.
    """

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    source: int | str = 1
    start: float = 0.0
    end: float | None = None
    duration: float | None = None

    @model_validator(mode="before")
    @classmethod
    def _accept_source_video_alias(cls, data: object) -> object:
        # extra="forbid" nên phải BỎ hẳn khoá cũ, không chỉ copy sang tên mới.
        if isinstance(data, dict) and "source_video" in data:
            renamed = dict(data)
            alias = renamed.pop("source_video")
            renamed.setdefault("source", alias)
            return renamed
        return data

    @field_validator("source", mode="before")
    @classmethod
    def _clean_source(cls, value: object) -> object:
        if isinstance(value, str):
            text = value.strip()
            if not text:
                raise ValueError("source rỗng")
            # "2" gõ dưới dạng chuỗi vẫn là số hiệu video, không phải tên file.
            return int(text) if text.isdigit() else text
        return value

    @field_validator("source")
    @classmethod
    def _check_source(cls, value: int | str) -> int | str:
        if isinstance(value, int) and not 1 <= value <= 99:
            raise ValueError("source phải từ 1 đến 99, hoặc là tên file video nguồn")
        return value

    @field_validator("start", "end", "duration", mode="before")
    @classmethod
    def _parse_time(cls, value: object) -> object:
        if value is None or isinstance(value, bool):
            return value
        if isinstance(value, (int, float, str)):
            return parse_time_value(value)
        return value

    @model_validator(mode="after")
    def _check_range(self) -> ClipSpec:
        if self.end is not None and self.duration is not None:
            raise ValueError("chỉ được đặt một trong hai: end hoặc duration")
        if self.end is not None and self.end <= self.start:
            raise ValueError(f"end ({self.end}) phải lớn hơn start ({self.start})")
        if self.duration is not None and self.duration <= 0:
            raise ValueError("duration phải lớn hơn 0")
        return self

    def stop_at(self) -> float | None:
        """Mốc kết thúc tính bằng giây, None = tới hết video nguồn."""
        if self.end is not None:
            return self.end
        if self.duration is not None:
            return self.start + self.duration
        return None


class MusicOptions(_Strict):
    enabled: bool = True
    volume: Annotated[float, Field(ge=0.0, le=2.0)] = 0.18
    # None = để service tự quyết: CÓ ghép nhạc nền thì tiếng gốc bị bỏ hẳn (0.0),
    # không ghép nhạc thì giữ nguyên tiếng gốc (1.0). Video nguồn thường có tiếng
    # gió, tiếng người nói chuyện, tiếng xe — trộn vào nhạc nền chỉ làm bẩn nhạc.
    # Đặt số cụ thể để ép cứng, ví dụ 0.3 nếu muốn giữ lại chút tiếng hiện trường.
    original_volume: Annotated[float, Field(ge=0.0, le=2.0)] | None = None
    loop: bool = True
    fade_in: Annotated[float, Field(ge=0.0, le=600.0)] = 2.0
    fade_out: Annotated[float, Field(ge=0.0, le=600.0)] = 3.0
    ducking: bool = False
    start_offset: Annotated[float, Field(ge=0.0, le=86400.0)] = 0.0


class OutputOptions(_Strict):
    filename: Annotated[str, Field(pattern=_FILENAME)] = "output.mp4"
    video_codec: str = "libx264"
    crf: Annotated[int, Field(ge=0, le=51)] = 23
    preset: str = "veryfast"
    audio_codec: str = "aac"
    audio_bitrate: Annotated[str, Field(pattern=_AUDIO_BITRATE)] = "192k"
    resolution: Annotated[str, Field(pattern=_RESOLUTION)] | None = None
    fps: Annotated[float, Field(gt=0, le=240)] | None = None
    faststart: bool = True
    copy_video_if_possible: bool = True

    @field_validator("resolution")
    @classmethod
    def _check_even_resolution(cls, value: str | None) -> str | None:
        # libx264/libx265 với pix_fmt yuv420p từ chối kích thước lẻ
        # ("width not divisible by 2") -> báo 422 ngay còn hơn để ffmpeg chạy
        # rồi chết giữa đường với FFMPEG_FAILED.
        if value is None:
            return value
        width, height = (int(part) for part in value.split("x", 1))
        if width % 2 or height % 2:
            raise ValueError("resolution phải là số chẵn (yêu cầu của yuv420p/H.264)")
        if width == 0 or height == 0:
            raise ValueError("resolution phải lớn hơn 0")
        return value

    @field_validator("preset")
    @classmethod
    def _check_preset(cls, value: str) -> str:
        if value not in PRESET_WHITELIST:
            raise ValueError(f"preset phải thuộc {sorted(PRESET_WHITELIST)}")
        return value

    @field_validator("video_codec")
    @classmethod
    def _check_video_codec(cls, value: str) -> str:
        if value not in VIDEO_CODEC_WHITELIST:
            raise ValueError(f"video_codec phải thuộc {sorted(VIDEO_CODEC_WHITELIST)}")
        return value

    @field_validator("audio_codec")
    @classmethod
    def _check_audio_codec(cls, value: str) -> str:
        if value not in AUDIO_CODEC_WHITELIST:
            raise ValueError(f"audio_codec phải thuộc {sorted(AUDIO_CODEC_WHITELIST)}")
        return value


class DeliveryOptions(_Strict):
    upload_to_drive: bool = False
    drive_folder_id: str | None = None


class RenderOptions(_Strict):
    subtitle: SubtitleOptions = Field(default_factory=SubtitleOptions)
    intro: IntroTextOptions = Field(default_factory=IntroTextOptions)
    music: MusicOptions = Field(default_factory=MusicOptions)
    output: OutputOptions = Field(default_factory=OutputOptions)
    delivery: DeliveryOptions = Field(default_factory=DeliveryOptions)
    # Định nghĩa & whitelist nằm ở app/clip_transitions.py — dùng chung schema
    # với phần dựng filter xfade/acrossfade, tránh tách hai nửa của cùng một
    # tính năng ra hai file.
    transition: TransitionOptions = Field(default_factory=TransitionOptions)
    # Rỗng = dùng nguyên video nguồn. Có phần tử = cắt & ghép theo đúng thứ tự
    # liệt kê (xem app/clips.py). Nhiều video nguồn mà không khai clips thì
    # service tự ghép trọn vẹn từng video theo thứ tự nhận được.
    clips: Annotated[list[ClipSpec], Field(max_length=100)] = Field(default_factory=list)


# Re-export: nhiều chỗ trong codebase quen ``from app.models import JobStatus``
# (và các schema response khác). Bản định nghĩa thật đã chuyển sang
# app/job_models.py để giữ file này dưới 400 dòng — chỉ còn schema INPUT
# (RenderOptions và các phần trong nó) ở đây.
from app.job_models import (  # noqa: E402
    CreateJobResponse,
    ErrorEnvelope,
    HealthResponse,
    JobError,
    JobOutput,
    JobResponse,
    JobStatus,
)

__all__ = [
    "JobStatus",
    "JobOutput",
    "JobError",
    "JobResponse",
    "CreateJobResponse",
    "HealthResponse",
    "ErrorEnvelope",
]
