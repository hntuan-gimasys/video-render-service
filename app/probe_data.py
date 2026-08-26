"""Kết quả ffprobe dạng dữ liệu thuần — docs/SPEC.md §5.2.

Tách khỏi ``app.ffmpeg_cmd`` để giữ mỗi file dưới 400 dòng. Ở đây chỉ có
dataclass và hàm parse JSON, không dựng lệnh và không I/O.
``ffmpeg_cmd``/``ffmpeg_runner`` re-export lại nên import cũ vẫn chạy.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = ["ProbeResult", "parse_probe_json"]


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """Kết quả ffprobe. ``video_codec`` cần cho quyết định fast path §5.1.

    ``width``/``height`` là kích thước **hiển thị** (đã tính rotation), tức
    đúng kích thước frame mà filter graph nhận được — xem ``parse_probe_json``.
    """

    duration: float = 0.0
    width: int = 0
    height: int = 0
    fps: float = 0.0
    has_audio: bool = False
    has_video: bool = False
    video_codec: str | None = None
    audio_codec: str | None = None
    rotation: int = 0

def _parse_fps(rate: str | None) -> float:
    """'30000/1001' -> 29.97. Chuỗi lạ -> 0.0."""
    if not rate:
        return 0.0
    try:
        if "/" in rate:
            num, den = rate.split("/", 1)
            return float(num) / float(den) if float(den) else 0.0
        return float(rate)
    except (TypeError, ValueError):
        return 0.0


def _parse_rotation(video: dict[str, Any]) -> int:
    """Góc xoay của stream, chuẩn hoá về 0/90/180/270.

    Video quay bằng điện thoại thường lưu kích thước "coded" ngang kèm Display
    Matrix xoay 90°, ffprobe báo 1280x720 nhưng frame thật khi qua filter là
    720x1280 (ffmpeg tự áp rotation, ``-autorotate`` bật sẵn).
    """
    raw: Any = None
    for side_data in video.get("side_data_list") or []:
        if side_data.get("rotation") is not None:
            raw = side_data["rotation"]
            break
    if raw is None:
        # Dạng cũ: tags.rotate (chuỗi), vẫn gặp ở file mp4 xuất từ tool cũ.
        raw = (video.get("tags") or {}).get("rotate")
    try:
        return int(round(float(raw))) % 360 if raw is not None else 0
    except (TypeError, ValueError):
        return 0


def parse_probe_json(payload: dict[str, Any]) -> ProbeResult:
    """Chuyển JSON của ffprobe thành :class:`ProbeResult` (hàm thuần).

    ``width``/``height`` trả về là kích thước ĐÃ xoay: mọi phép tính phía sau
    (nhất là cỡ chữ phụ đề) phải khớp với frame mà filter graph thực sự nhận.
    """
    streams = payload.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    duration = 0.0
    for candidate in ((payload.get("format") or {}).get("duration"), (video or {}).get("duration")):
        try:
            duration = float(candidate)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if duration > 0:
            break

    width = int((video or {}).get("width") or 0)
    height = int((video or {}).get("height") or 0)
    rotation = _parse_rotation(video or {})
    if rotation in (90, 270):
        width, height = height, width

    return ProbeResult(
        duration=max(0.0, duration),
        width=width,
        height=height,
        fps=_parse_fps((video or {}).get("r_frame_rate")),
        has_audio=audio is not None,
        has_video=video is not None,
        video_codec=(video or {}).get("codec_name"),
        audio_codec=(audio or {}).get("codec_name"),
        rotation=rotation,
    )
