"""Tải/đẩy file Google Drive bằng Service Account — docs/SPEC.md §6.

Mọi call của googleapiclient là blocking (HTTP đồng bộ) nên đều phải bọc
``asyncio.to_thread`` để không chặn event loop.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Final

from app.utils import (
    MB,
    DriveDownloadFailed,
    DriveUploadFailed,
    FileTooLarge,
    InvalidDriveUrl,
)

logger = logging.getLogger(__name__)

DRIVE_SCOPES: Final[tuple[str, ...]] = ("https://www.googleapis.com/auth/drive",)
CHUNK_BYTES: Final[int] = 8 * MB

# Các dạng link Drive ở SPEC §6, thử theo thứ tự cụ thể -> tổng quát.
_ID_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"/file/d/([A-Za-z0-9_-]{10,})"),
    re.compile(r"/document/d/([A-Za-z0-9_-]{10,})"),
    re.compile(r"/(?:spreadsheets|presentation)/d/([A-Za-z0-9_-]{10,})"),
    re.compile(r"/folders/([A-Za-z0-9_-]{10,})"),
    re.compile(r"/d/([A-Za-z0-9_-]{10,})"),
    re.compile(r"[?&]id=([A-Za-z0-9_-]{10,})"),
)
_BARE_ID: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_-]{25,50}$")

__all__ = [
    "DRIVE_SCOPES",
    "CHUNK_BYTES",
    "DriveFileMeta",
    "DriveUploadResult",
    "parse_drive_id",
    "get_drive_service",
    "download_file",
    "upload_file",
]


@dataclass(frozen=True, slots=True)
class DriveFileMeta:
    file_id: str
    name: str
    size_bytes: int
    mime_type: str


@dataclass(frozen=True, slots=True)
class DriveUploadResult:
    file_id: str
    name: str
    web_view_link: str | None


def parse_drive_id(url: str) -> str:
    """Lấy file_id từ link Drive (hoặc chính chuỗi ID).

    Raise :class:`InvalidDriveUrl` nếu không nhận diện được.
    """
    candidate = (url or "").strip()
    if not candidate:
        raise InvalidDriveUrl("Link Drive rỗng")

    if _BARE_ID.match(candidate):
        return candidate

    for pattern in _ID_PATTERNS:
        match = pattern.search(candidate)
        if match:
            return match.group(1)

    raise InvalidDriveUrl(f"Không nhận diện được file_id từ: {candidate[:200]}")


@lru_cache(maxsize=1)
def get_drive_service() -> Any:
    """Drive v3 client dùng ADC (Cloud Run) hoặc GOOGLE_APPLICATION_CREDENTIALS.

    Cache lại vì việc dựng client khá đắt. ``cache_clear()`` được trong test.
    """
    import google.auth
    from googleapiclient.discovery import build

    credentials, _project = google.auth.default(scopes=list(DRIVE_SCOPES))
    # cache_discovery=False: tránh ghi cache lên đĩa read-only của container.
    return build("drive", "v3", credentials=credentials, cache_discovery=False)


def _get_metadata(file_id: str) -> DriveFileMeta:
    """Blocking — luôn gọi qua asyncio.to_thread."""
    service = get_drive_service()
    payload = (
        service.files()
        .get(fileId=file_id, fields="id,name,size,mimeType", supportsAllDrives=True)
        .execute()
    )
    try:
        size = int(payload.get("size") or 0)
    except (TypeError, ValueError):
        size = 0
    return DriveFileMeta(
        file_id=payload.get("id") or file_id,
        name=payload.get("name") or file_id,
        size_bytes=size,
        mime_type=payload.get("mimeType") or "application/octet-stream",
    )


def _download_blocking(
    file_id: str,
    dest: Path,
    on_progress: Callable[[float], None] | None,
) -> None:
    """Blocking — luôn gọi qua asyncio.to_thread."""
    from googleapiclient.http import MediaIoBaseDownload

    service = get_drive_service()
    # supportsAllDrives=True bắt buộc, thiếu là fail với file trên Shared Drive.
    request = service.files().get_media(fileId=file_id, supportsAllDrives=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("wb") as handle:
        downloader = MediaIoBaseDownload(handle, request, chunksize=CHUNK_BYTES)
        done = False
        while not done:
            status, done = downloader.next_chunk()
            if status is not None and on_progress is not None:
                on_progress(min(100.0, status.progress() * 100))


async def download_file(
    file_id: str,
    dest: Path,
    on_progress: Callable[[float], None] | None = None,
    *,
    max_bytes: int = 0,
) -> DriveFileMeta:
    """Tải file Drive về ``dest``.

    Kiểm tra ``size`` trước khi tải để không nạp đầy /tmp (là RAM) rồi mới lỗi.
    """
    try:
        meta = await asyncio.to_thread(_get_metadata, file_id)
    except Exception as exc:  # noqa: BLE001 - gói mọi lỗi googleapiclient
        raise DriveDownloadFailed(
            f"Không đọc được metadata file Drive {file_id}",
            detail=_short(exc),
        ) from exc

    if max_bytes > 0 and meta.size_bytes > max_bytes:
        raise FileTooLarge(
            f"File Drive {meta.size_bytes // MB} MB vượt giới hạn {max_bytes // MB} MB"
        )

    logger.info(
        "Bắt đầu tải file Drive",
        # "name" là thuộc tính có sẵn của LogRecord (tên logger) -> logging
        # raise KeyError nếu extra cũng dùng đúng key này, nên phải đặt tên khác.
        extra={"drive_file_id": file_id, "size_bytes": meta.size_bytes, "drive_name": meta.name},
    )
    try:
        await asyncio.to_thread(_download_blocking, file_id, dest, on_progress)
    except Exception as exc:  # noqa: BLE001
        # Xoá file tải dở, /tmp là RAM nên không được để rác lại.
        await asyncio.to_thread(_unlink_quiet, dest)
        raise DriveDownloadFailed(
            f"Tải file Drive {file_id} thất bại", detail=_short(exc)
        ) from exc
    return meta


def _upload_blocking(src: Path, folder_id: str | None, mime_type: str) -> DriveUploadResult:
    """Blocking — luôn gọi qua asyncio.to_thread."""
    from googleapiclient.http import MediaFileUpload

    service = get_drive_service()
    body: dict[str, Any] = {"name": src.name}
    if folder_id:
        body["parents"] = [folder_id]
    media = MediaFileUpload(
        str(src), mimetype=mime_type, resumable=True, chunksize=CHUNK_BYTES
    )
    request = service.files().create(
        body=body,
        media_body=media,
        fields="id,name,webViewLink",
        supportsAllDrives=True,
    )
    response = None
    while response is None:
        _status, response = request.next_chunk()
    return DriveUploadResult(
        file_id=response["id"],
        name=response.get("name") or src.name,
        web_view_link=response.get("webViewLink"),
    )


async def upload_file(
    src: Path,
    folder_id: str | None = None,
    *,
    mime_type: str = "video/mp4",
) -> DriveUploadResult:
    """Upload ``src`` lên Drive (resumable, chunk 8 MiB) và trả về link xem."""
    logger.info(
        "Bắt đầu upload lên Drive",
        # "filename" cũng là thuộc tính có sẵn của LogRecord, tương tự "name".
        extra={"drive_folder_id": folder_id, "upload_filename": src.name},
    )
    try:
        return await asyncio.to_thread(_upload_blocking, src, folder_id, mime_type)
    except Exception as exc:  # noqa: BLE001
        raise DriveUploadFailed(
            f"Upload {src.name} lên Drive thất bại", detail=_short(exc)
        ) from exc


def _unlink_quiet(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:  # pragma: no cover
        logger.warning("Không xoá được file tải dở %s", path)


def _short(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:2000]
