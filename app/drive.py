"""Tải/đẩy file Google Drive bằng Service Account — docs/SPEC.md §6.

Mọi call của googleapiclient là blocking (HTTP đồng bộ) nên đều phải bọc
``asyncio.to_thread`` để không chặn event loop.
"""

from __future__ import annotations

import asyncio
import logging
import re
import ssl
import threading
from collections.abc import Callable
from dataclasses import dataclass
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
    "drive_direct_url",
    "get_drive_service",
    "reset_drive_service",
    "call_drive",
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


def drive_direct_url(file_id: str) -> str:
    """Link lấy thẳng bytes của một file Drive.

    Khác ``webViewLink`` (trang xem của Drive, trả HTML): dạng ``uc?export=
    download`` trả đúng nội dung file. Dùng cho MÁY gọi lấy bytes; link đưa cho
    người bấm thì là ``webViewLink``, xem docstring của
    :class:`app.job_models.JobOutput`.

    Link này KHÔNG mở công khai: người/máy gọi vẫn phải có quyền đọc file. File
    do service account tạo trong thư mục đích nên nó thừa hưởng quyền của thư
    mục đó — ai xem được thư mục thì tải được, còn một downstream không mang
    credential Google sẽ nhận trang đăng nhập chứ không phải video.
    """
    return f"https://drive.google.com/uc?id={file_id}&export=download"


# Client Drive được cache THEO TỪNG THREAD, cố tình không dùng chung cả process.
#
# googleapiclient/httplib2 KHÔNG thread-safe: một ``Http`` giữ một socket TLS và
# không có khoá nào bảo vệ. Mà mọi call Drive đều chạy trong thread của pool
# (``asyncio.to_thread``), nên một client dùng chung nghĩa là hai thread cùng ghi
# vào một socket.
#
# Hậu quả đã gặp thật trên production, không phải lo xa: container chết bằng
# "Container terminated on signal 11" (SIGSEGV) đúng vào lúc job thứ hai bắt đầu
# quét Drive trong khi job thứ nhất còn đang tải file — 09:05:41 job mới tạo,
# 09:05:43 crash; lần thứ hai 09:06:59 và 09:07:00 hai POST cách nhau một giây,
# 09:07:01 crash. Crash xoá sạch RAM nên MỌI job đang chạy biến mất một lúc, và
# client đang poll nhận 404 cho job vừa được nhận 202 vài giây trước.
#
# Lưu ý cái bẫy đã làm chuyện này khó thấy: ``MAX_CONCURRENT_JOBS`` KHÔNG tuần tự
# hoá giai đoạn tải — semaphore trong app/jobs.py chỉ bao bước render, còn
# ``prepare_inputs`` nằm ngoài. Nên đặt nó bằng 1 vẫn có nhiều thread gọi Drive
# cùng lúc.
#
# Cache theo thread giữ nguyên cái lợi ban đầu (dựng client khá đắt, và thread
# pool tái dùng thread nên không phải dựng lại mỗi lượt) mà không chia sẻ socket.
_thread_local = threading.local()


def get_drive_service() -> Any:
    """Drive v3 client CỦA RIÊNG thread đang gọi, dựng lần đầu rồi giữ lại.

    Dùng ADC (Cloud Run) hoặc ``GOOGLE_APPLICATION_CREDENTIALS``.
    """
    service = getattr(_thread_local, "service", None)
    if service is None:
        import google.auth
        from googleapiclient.discovery import build

        credentials, _project = google.auth.default(scopes=list(DRIVE_SCOPES))
        # cache_discovery=False: tránh ghi cache lên đĩa read-only của container.
        service = build("drive", "v3", credentials=credentials, cache_discovery=False)
        _thread_local.service = service
    return service


def reset_drive_service() -> None:
    """Bỏ client của RIÊNG thread đang gọi; lần sau nó tự dựng lại.

    Cố ý không có cách nào xoá client của thread khác: làm vậy là chạm vào đối
    tượng mà thread đó đang dùng, đúng thứ vừa gây SIGSEGV.
    """
    _thread_local.service = None


# Client giữ socket keep-alive, và Google đóng socket đó sau một lúc không dùng.
# Lần ghi kế tiếp chết ngay bằng BrokenPipeError — đã gặp thật trên service: job 09:26 chạy xong, job 09:56
# fail sau 27 ms với "BrokenPipeError: [Errno 32] Broken pipe", cùng thư mục Drive
# và cùng revision.
#
# Connection chết đó nằm lại trong pool nên instance bị nhiễm vĩnh viễn, không tự
# khỏi: job kế tiếp lúc 10:03 fail sau 6,6 ms, lần này ra "SSLError: [SSL:
# UNEXPECTED_EOF_WHILE_READING]" — cùng một socket chết, khác triệu chứng.
#
# num_retries của googleapiclient KHÔNG chữa được ca này: httplib2 raise EPIPE
# trong _conn_request mà không conn.close(), còn Http.request chỉ lấy connection
# từ self.connections và không có nhánh nào loại bỏ connection hỏng -> conn.sock
# vẫn khác None nên mọi lần retry dùng lại đúng socket đã chết. (Đường stale hay
# gặp hơn — server đóng, getresponse() ra BadStatusLine — thì httplib2 tự close +
# connect lại và hồi phục, nên chỉ nhánh ghi-lỗi này mới rò ra ngoài.)
#
# Nên cách duy nhất chắc chắn là bỏ client của thread đó: dựng lại đồng nghĩa có
# httplib2.Http mới với pool connection rỗng.
_TRANSPORT_ERRORS: Final[tuple[type[BaseException], ...]] = (
    # BrokenPipeError/ConnectionResetError/RemoteDisconnected đều là ConnectionError.
    ConnectionError,
    # SSLError KHÔNG thuộc ConnectionError nên phải kể riêng, nếu không triệu
    # chứng thứ hai đo được ở trên sẽ lọt qua retry.
    ssl.SSLError,
)


async def call_drive(func: Callable[..., Any], *args: Any) -> Any:
    """Chạy một call blocking của Drive trong thread, thử lại nếu socket đã chết.

    Chỉ thử lại đúng một lần và chỉ với lỗi tầng transport: mọi lỗi khác (403 sai
    quyền chia sẻ, 404 sai id, file quá lớn...) phải nổi lên ngay chứ không được
    che bằng retry.
    """
    return await asyncio.to_thread(_call_with_retry, func, *args)


def _call_with_retry(func: Callable[..., Any], *args: Any) -> Any:
    """Chạy trong thread pool, và thử lại NGAY TRONG CÙNG THREAD.

    Việc thử lại buộc phải nằm bên trong thread chứ không ở ``call_drive``: client
    được cache theo thread, nên nếu bỏ client từ thread của event loop thì vừa bỏ
    sai client (client của thread khác) vừa chạm vào đối tượng thread kia đang
    dùng. Ngoài ra ``asyncio.to_thread`` không hứa xếp lần gọi sau vào đúng thread
    cũ, nên tách hai lần gọi thành hai lượt to_thread là mất luôn quan hệ
    "client vừa bỏ chính là client sắp dựng lại".
    """
    try:
        return func(*args)
    except _TRANSPORT_ERRORS as exc:
        logger.warning(
            "Kết nối Drive đã chết, dựng lại client rồi thử lại",
            extra={"drive_transport_error": _short(exc)},
        )
        reset_drive_service()
        return func(*args)


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
        meta = await call_drive(_get_metadata, file_id)
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
        await call_drive(_download_blocking, file_id, dest, on_progress)
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
    # Upload PHẢI đi qua call_drive. Từng để nó ngoài với lý do "socket chết chỉ
    # đập vào call Drive đầu tiên của job, tới lúc upload thì kết nối vừa dùng
    # liên tục" — lý do đó SAI, đo được trên production: job 1756ba62296e tải
    # video nguồn xong lúc 10:16:30.695 không cần retry lần nào, rồi upload chết
    # lúc 10:16:33.172 bằng "BrokenPipeError: [Errno 32] Broken pipe", chỉ 3 giây
    # sau. Lần ghi đầu của resumable upload vào một keep-alive mà Google vừa đóng
    # là đủ để EPIPE, không cần nằm im lâu.
    #
    # Đánh đổi còn lại: nếu lần đầu đã commit chunk cuối mà response bị mất thì
    # lần thử lại tạo thêm một file nữa trên Drive. Cửa sổ đó rất hẹp (resumable
    # upload chỉ hiện file khi hoàn tất), và một file trùng vẫn nhẹ hơn nhiều so
    # với việc bỏ trắng cả lượt render đã xong.
    try:
        return await call_drive(_upload_blocking, src, folder_id, mime_type)
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
