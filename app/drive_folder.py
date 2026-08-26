"""Quét một thư mục Google Drive để lấy danh sách video trong đó.

Tách khỏi ``app/drive.py`` để giữ mỗi file dưới 400 dòng: ở đây chỉ có phần
liệt kê thư mục, phần tải/đẩy từng file vẫn nằm bên kia.

Mọi call của googleapiclient là blocking (HTTP đồng bộ) nên đều phải bọc
``asyncio.to_thread`` để không chặn event loop.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Final

from app.drive import DriveFileMeta, call_drive, get_drive_service
from app.utils import DriveDownloadFailed, InvalidDriveUrl

logger = logging.getLogger(__name__)

__all__ = [
    "VIDEO_EXTENSIONS",
    "is_video",
    "parse_folder_id",
    "list_folder_videos",
]

# Đuôi file dùng làm lưới vớt khi Drive báo mimeType chung chung
# (application/octet-stream hay gặp với file tải lên từ máy).
VIDEO_EXTENSIONS: Final[frozenset[str]] = frozenset(
    {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi", ".wmv", ".flv", ".mpeg", ".mpg", ".3gp"}
)
_PAGE_SIZE: Final[int] = 200
# Chặn vòng phân trang, phòng thư mục khổng lồ hoặc API trả cursor lặp.
_MAX_PAGES: Final[int] = 20

_FOLDER_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"/folders/([A-Za-z0-9_-]{10,})"),
    re.compile(r"/drive/u/\d+/folders/([A-Za-z0-9_-]{10,})"),
    re.compile(r"[?&]id=([A-Za-z0-9_-]{10,})"),
)
_BARE_ID: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_-]{25,50}$")


def is_video(name: str, mime_type: str) -> bool:
    """File này có phải video không?

    Tin ``mimeType`` trước; nhiều file tải lên từ máy bị Drive gán
    ``application/octet-stream`` nên phải xét thêm đuôi file.
    """
    if mime_type.startswith("video/"):
        return True
    if mime_type == "application/vnd.google-apps.folder":
        return False
    suffix = name.rsplit(".", 1)
    return len(suffix) == 2 and f".{suffix[1].lower()}" in VIDEO_EXTENSIONS


def parse_folder_id(url: str) -> str:
    """Lấy folder_id từ link thư mục Drive (hoặc chính chuỗi ID).

    Raise :class:`InvalidDriveUrl` nếu không nhận diện được.
    """
    text = (url or "").strip()
    if not text:
        raise InvalidDriveUrl("Thiếu link thư mục Drive")
    for pattern in _FOLDER_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(1)
    if _BARE_ID.match(text):
        return text
    raise InvalidDriveUrl(
        f"Không nhận ra link thư mục Drive: {text[:120]}",
        detail="Dạng đúng: https://drive.google.com/drive/folders/<id>",
    )


def _list_blocking(folder_id: str) -> list[DriveFileMeta]:
    """Blocking — luôn gọi qua asyncio.to_thread."""
    service = get_drive_service()
    files: list[DriveFileMeta] = []
    page_token: str | None = None
    for _page in range(_MAX_PAGES):
        payload: dict[str, Any] = (
            service.files()
            .list(
                q=f"'{folder_id}' in parents and trashed = false",
                fields="nextPageToken, files(id, name, size, mimeType)",
                pageSize=_PAGE_SIZE,
                pageToken=page_token,
                # Bắt buộc để đọc được thư mục nằm trên Shared Drive.
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
                # Sắp theo tên cho kết quả ổn định giữa các lần chạy: số hiệu
                # video (khi không khai tên file) phải luôn trỏ đúng một chỗ.
                orderBy="name_natural",
            )
            .execute()
        )
        for item in payload.get("files") or []:
            name = item.get("name") or item.get("id") or ""
            mime_type = item.get("mimeType") or "application/octet-stream"
            if not is_video(name, mime_type):
                continue
            try:
                size = int(item.get("size") or 0)
            except (TypeError, ValueError):
                size = 0
            files.append(
                DriveFileMeta(
                    file_id=item.get("id") or "",
                    name=name,
                    size_bytes=size,
                    mime_type=mime_type,
                )
            )
        page_token = payload.get("nextPageToken")
        if not page_token:
            break
    return files


async def list_folder_videos(folder_id: str) -> list[DriveFileMeta]:
    """Danh sách video trong thư mục, đã sắp theo tên.

    Raise :class:`DriveDownloadFailed` nếu không đọc được thư mục (sai quyền
    chia sẻ là ca hay gặp nhất), hoặc nếu thư mục không có video nào.
    """
    try:
        files = await call_drive(_list_blocking, folder_id)
    except Exception as exc:  # noqa: BLE001 - gói mọi lỗi googleapiclient
        raise DriveDownloadFailed(
            f"Không đọc được thư mục Drive {folder_id}",
            detail=_short(exc),
        ) from exc

    if not files:
        raise DriveDownloadFailed(
            f"Thư mục Drive {folder_id} không có video nào",
            detail="Kiểm tra lại link, và nhớ chia sẻ thư mục cho service account",
        )
    logger.info(
        "Quét thư mục Drive",
        extra={"drive_folder_id": folder_id, "video_count": len(files)},
    )
    return files


def _short(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:500]
