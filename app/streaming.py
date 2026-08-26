"""Stream file output + hỗ trợ HTTP Range — docs/SPEC.md §3.3.

Tách khỏi ``app.main`` để giữ mỗi file dưới 400 dòng.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Final

from fastapi import Response
from fastapi.responses import JSONResponse, StreamingResponse

from app.utils import CHUNK_SIZE, RangeNotSatisfiable

_RANGE_HEADER: Final[re.Pattern[str]] = re.compile(r"^bytes=(\d*)-(\d*)$")

# Cloud Run đổi response non-streaming lớn hơn 32 MiB thành "500 Internal Server
# Error" ở Google Frontend, dù app đã trả 200. Đã đo trên service thật với output
# 39.969.429 byte: plain GET -> 500 + log "Response size was too large", trong
# khi uvicorn.access cùng lúc ghi 200; Range 33.000.001 byte -> 206 OK, Range
# 33.554.431 byte -> 500. Tài liệu Cloud Run: "Maximum HTTP/1 response size:
# 32 MiB per response. Limit applies if not using Transfer-Encoding: chunked or
# streaming."
#
# Nên body lớn KHÔNG được gửi Content-Length: thiếu header đó, uvicorn tự dùng
# Transfer-Encoding: chunked và response ra khỏi diện bị giới hạn. Đây là chỗ cố
# tình lệch SPEC §3.3 ("Content-Length chính xác"): giữ đúng SPEC nghĩa là mọi
# video trên 32 MiB đều không tải được. Ngưỡng đặt dưới mốc đo được một quãng vì
# giới hạn của Cloud Run tính cả header chứ không chỉ body.
_MAX_SIZED_RESPONSE: Final[int] = 32_000_000

__all__ = ["parse_range", "stream_file", "build_download_response"]


def parse_range(header: str | None, file_size: int) -> tuple[int, int]:
    """Trả về ``(start, end)`` đã kẹp trong ``[0, file_size-1]``.

    Header thiếu hoặc sai cú pháp -> trả toàn bộ file (200 thay vì 206).
    Range nằm ngoài file -> raise :class:`RangeNotSatisfiable` (416): nếu kẹp về
    byte cuối thì client đang resume sẽ nhận lại một byte đã có và ghi trùng vào
    file đang tải.
    """
    if not header or file_size == 0:
        return 0, max(0, file_size - 1)
    match = _RANGE_HEADER.match(header.strip())
    if match is None:
        return 0, file_size - 1

    raw_start, raw_end = match.group(1), match.group(2)
    if not raw_start and not raw_end:
        return 0, file_size - 1
    if not raw_start:
        # "bytes=-500" nghĩa là 500 byte cuối file.
        suffix = int(raw_end)
        if suffix == 0:
            raise RangeNotSatisfiable(f"Range '{header}' không hợp lệ")
        length = min(suffix, file_size)
        return file_size - length, file_size - 1

    start = int(raw_start)
    if start >= file_size:
        raise RangeNotSatisfiable(f"Range '{header}' vượt quá kích thước {file_size} byte")
    end = min(int(raw_end), file_size - 1) if raw_end else file_size - 1
    if end < start:
        end = file_size - 1
    return start, end


async def stream_file(path: Path, start: int, length: int) -> AsyncIterator[bytes]:
    """Đọc file theo chunk 1 MiB, mỗi lần đọc chạy trong thread riêng.

    Đọc file lớn từ /tmp là blocking I/O nên không được làm thẳng trong event
    loop, nếu không toàn bộ API sẽ đứng trong lúc client tải video.
    """

    def _chunks() -> Iterator[bytes]:
        with path.open("rb") as handle:
            handle.seek(start)
            remaining = length
            while remaining > 0:
                chunk = handle.read(min(CHUNK_SIZE, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    iterator = _chunks()
    while True:
        chunk = await asyncio.to_thread(next, iterator, b"")
        if not chunk:
            break
        yield chunk


def build_download_response(path: Path, total_size: int, range_header: str | None) -> Response:
    """Response tải file có hỗ trợ Range (206) và 416 đúng chuẩn RFC 7233."""
    try:
        start, end = parse_range(range_header, total_size)
    except RangeNotSatisfiable as exc:
        # RFC 7233: 416 bắt buộc kèm Content-Range cho biết kích thước thật.
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": exc.to_dict()},
            headers={"Content-Range": f"bytes */{total_size}"},
        )
    length = end - start + 1
    headers = {
        "Content-Disposition": f'attachment; filename="{path.name}"',
        "Accept-Ranges": "bytes",
    }
    if length <= _MAX_SIZED_RESPONSE:
        headers["Content-Length"] = str(length)
    status_code = 200
    if start != 0 or length != total_size:
        headers["Content-Range"] = f"bytes {start}-{end}/{total_size}"
        status_code = 206

    return StreamingResponse(
        stream_file(path, start, length),
        status_code=status_code,
        media_type="video/mp4",
        headers=headers,
    )
