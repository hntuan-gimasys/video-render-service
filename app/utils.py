"""Tiện ích chung: exception, logging JSON một dòng, helper file.

Module nền móng — không import module nào khác trong ``app`` để tránh vòng lặp
import.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import shutil
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

MB: Final[int] = 1024 * 1024
CHUNK_SIZE: Final[int] = 1 * MB

__all__ = [
    "MB",
    "CHUNK_SIZE",
    "AppError",
    "NoVideoSource",
    "InvalidDriveUrl",
    "Unauthorized",
    "JobNotFound",
    "JobNotReady",
    "FileTooLarge",
    "InvalidOptions",
    "InvalidSubtitle",
    "QueueFull",
    "RangeNotSatisfiable",
    "DriveDownloadFailed",
    "DriveUploadFailed",
    "FfmpegFailed",
    "ProbeFailed",
    "InsufficientTmpSpace",
    "InternalError",
    "setup_logging",
    "bind_job",
    "new_job_id",
    "free_space_mb",
    "safe_rmtree",
    "utcnow",
    "format_hms",
]


# --------------------------------------------------------------------------- #
# Exceptions — khớp bảng mã lỗi docs/SPEC.md §3.6
# --------------------------------------------------------------------------- #
class AppError(Exception):
    """Lỗi nghiệp vụ có mã ổn định.

    Cùng một exception dùng cho hai đường: trả HTTP response (lỗi phát sinh
    trong request) và ghi vào ``JobError`` (lỗi phát sinh trong worker nền —
    lúc đó ``http_status`` chỉ mang tính tham khảo).
    """

    code: str = "INTERNAL"
    http_status: int = 500
    message: str = "Internal error"

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        http_status: int | None = None,
        detail: str | None = None,
    ) -> None:
        self.message = message or self.__class__.message
        if code is not None:
            self.code = code
        if http_status is not None:
            self.http_status = http_status
        self.detail = detail
        super().__init__(self.message)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.detail:
            payload["detail"] = self.detail
        return payload

    def __repr__(self) -> str:  # pragma: no cover - tiện debug
        return f"{type(self).__name__}(code={self.code!r}, message={self.message!r})"


class NoVideoSource(AppError):
    code, http_status, message = "NO_VIDEO_SOURCE", 400, "Cần video_file hoặc video_url"


class InvalidDriveUrl(AppError):
    code, http_status, message = "INVALID_DRIVE_URL", 400, "Link Google Drive không hợp lệ"


class Unauthorized(AppError):
    code, http_status, message = "UNAUTHORIZED", 401, "API key không hợp lệ"


class JobNotFound(AppError):
    code, http_status, message = "JOB_NOT_FOUND", 404, "Job không tồn tại"


class JobNotReady(AppError):
    code, http_status, message = "JOB_NOT_READY", 409, "Job chưa render xong"


class FileTooLarge(AppError):
    code, http_status, message = "FILE_TOO_LARGE", 413, "File vượt quá giới hạn cho phép"


class InvalidOptions(AppError):
    code, http_status, message = "INVALID_OPTIONS", 422, "options không hợp lệ"


class InvalidSubtitle(AppError):
    code, http_status, message = "INVALID_SRT", 422, "File phụ đề không đọc được"


class RangeNotSatisfiable(AppError):
    """Không có trong bảng §3.6 nhưng RFC 7233 bắt buộc: Range ngoài kích thước
    file phải trả 416, nếu kẹp về byte cuối thì client resume sẽ ghi trùng byte."""

    code, http_status, message = (
        "RANGE_NOT_SATISFIABLE",
        416,
        "Range yêu cầu nằm ngoài kích thước file",
    )


class QueueFull(AppError):
    code, http_status, message = "QUEUE_FULL", 429, "Hàng đợi đã đầy, thử lại sau"


class DriveDownloadFailed(AppError):
    code, http_status, message = (
        "DRIVE_DOWNLOAD_FAILED",
        502,
        "Tải file từ Google Drive thất bại",
    )


class DriveUploadFailed(AppError):
    code, http_status, message = (
        "DRIVE_UPLOAD_FAILED",
        502,
        "Upload file lên Google Drive thất bại",
    )


class FfmpegFailed(AppError):
    code, http_status, message = "FFMPEG_FAILED", 500, "FFmpeg thất bại"


class ProbeFailed(AppError):
    code, http_status, message = "PROBE_FAILED", 500, "ffprobe không đọc được file"


class InsufficientTmpSpace(AppError):
    code, http_status, message = "INSUFFICIENT_TMP_SPACE", 507, "Không đủ dung lượng /tmp"


class InternalError(AppError):
    code, http_status, message = "INTERNAL", 500, "Lỗi nội bộ"


# --------------------------------------------------------------------------- #
# Logging JSON một dòng ra stdout (Cloud Logging tự parse)
# --------------------------------------------------------------------------- #
_LOG_RECORD_BUILTIN: Final[frozenset[str]] = frozenset(
    logging.LogRecord("", 0, "", 0, "", None, None).__dict__.keys()
) | {"message", "asctime", "taskName"}

_SEVERITY_MAP: Final[dict[int, str]] = {
    logging.DEBUG: "DEBUG",
    logging.INFO: "INFO",
    logging.WARNING: "WARNING",
    logging.ERROR: "ERROR",
    logging.CRITICAL: "CRITICAL",
}


class JsonFormatter(logging.Formatter):
    """Format mỗi record thành đúng một dòng JSON."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "severity": _SEVERITY_MAP.get(record.levelno, record.levelname),
            "message": record.getMessage(),
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "logger": record.name,
        }
        # Mọi field truyền qua extra= được đưa thẳng lên top-level để Cloud
        # Logging index được (quan trọng nhất là job_id).
        for key, value in record.__dict__.items():
            if key not in _LOG_RECORD_BUILTIN and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_logging(level: str = "INFO") -> None:
    """Cấu hình logging JSON ra stdout. Gọi một lần lúc khởi động."""
    # Message tiếng Việt sẽ làm StreamHandler raise UnicodeEncodeError nếu
    # stdout không phải UTF-8 (console Windows mặc định cp1252) -> mất log.
    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    for existing in root.handlers[:]:
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level.upper())

    # uvicorn tự cài handler riêng -> bỏ đi để log không bị nhân đôi và không
    # lọt ra dạng text thuần.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uv = logging.getLogger(name)
        uv.handlers.clear()
        uv.propagate = True


class _JobAdapter(logging.LoggerAdapter):
    """LoggerAdapter gộp extra thay vì ghi đè (bản mặc định ghi đè)."""

    def process(self, msg: Any, kwargs: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
        merged = dict(self.extra or {})
        merged.update(kwargs.get("extra") or {})
        kwargs["extra"] = merged
        return msg, kwargs


def bind_job(logger: logging.Logger, job_id: str, **fields: Any) -> logging.LoggerAdapter:
    """Trả về logger luôn kèm ``job_id`` — SPEC §9 bắt buộc field này."""
    return _JobAdapter(logger, {"job_id": job_id, **fields})


# --------------------------------------------------------------------------- #
# Helper
# --------------------------------------------------------------------------- #
def new_job_id() -> str:
    return uuid.uuid4().hex[:12]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def format_hms(seconds: float) -> str:
    """3661.4 -> '01:01:01' (dùng cho stage_message §3.2)."""
    total = max(0, int(seconds))
    return f"{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}"


def _unlink_quiet(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:  # pragma: no cover - chỉ xảy ra khi FS lỗi
        logging.getLogger(__name__).warning("Không xoá được file %s", path)


def free_space_mb(path: Path) -> float:
    """Dung lượng trống (MB) của filesystem chứa ``path``.

    ``path`` có thể chưa tồn tại -> đi ngược lên parent gần nhất đang tồn tại.
    """
    probe = path
    while not probe.exists():
        parent = probe.parent
        if parent == probe:
            break
        probe = parent
    return shutil.disk_usage(probe).free / MB


def safe_rmtree(path: Path) -> None:
    """Xoá cây thư mục, không bao giờ raise.

    Hàm blocking — nơi gọi phải bọc ``asyncio.to_thread``.
    """
    if not path.exists():
        return
    failed: list[str] = []

    def _onexc(func: Any, target: Any, exc: BaseException) -> None:
        failed.append(str(target))

    shutil.rmtree(path, onexc=_onexc)
    if failed:
        logging.getLogger(__name__).warning(
            "Xoá workspace chưa sạch",
            extra={"path": str(path), "failed_paths": failed[:5]},
        )


_SAFE_EXT: Final[re.Pattern[str]] = re.compile(r"^\.[A-Za-z0-9][A-Za-z0-9]{0,7}$")


