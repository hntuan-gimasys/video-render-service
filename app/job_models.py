"""Schema trạng thái job và response API — docs/SPEC.md §3.2-3.5.

Tách khỏi ``app/models.py`` để giữ mỗi file dưới 400 dòng: bên kia là schema
INPUT (``RenderOptions`` và các phần trong nó), ở đây là schema OUTPUT (job
đang ở đâu, trả về gì).
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Final

from pydantic import BaseModel

__all__ = [
    "JobStatus",
    "JobOutput",
    "JobError",
    "JobResponse",
    "CreateJobResponse",
    "HealthResponse",
    "ErrorEnvelope",
]


class JobStatus(StrEnum):
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    MERGING = "merging"
    PROBING = "probing"
    RENDERING = "rendering"
    UPLOADING = "uploading"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL_STATUSES


_TERMINAL_STATUSES: Final[frozenset[JobStatus]] = frozenset(
    {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED}
)


class JobOutput(BaseModel):
    """Kết quả của job.

    ``download_url`` là LINK ĐỂ ĐƯA CHO NGƯỜI DÙNG, và nội dung của nó đổi theo
    cách giao hàng: không upload thì là endpoint ``/api/jobs/{id}/download`` của
    service, còn upload lên Drive thành công thì là link XEM của Drive (bấm vào
    mở trình phát, không tải file về). Cố ý dùng lại đúng field này thay vì thêm
    field mới, để bước sau không phải sửa gì khi bật upload — đây là chỗ lệch
    SPEC §3.2, nơi ``download_url`` được mô tả là endpoint nội bộ trả file.

    Cần lấy BYTES bằng máy thì dùng ``drive_download_url``, đừng dùng
    ``download_url``: link xem trả HTML.
    """

    filename: str
    size_bytes: int
    duration_seconds: float | None = None
    download_url: str
    drive_file_id: str | None = None
    # Cùng giá trị với download_url khi upload thành công, chỉ khác là tên field
    # nói rõ nó là link xem. Giữ cả hai: download_url để bên gọi cũ không phải
    # sửa, drive_view_url để bên gọi mới đọc đúng tên.
    drive_view_url: str | None = None
    # Link lấy thẳng bytes, cho máy gọi. Xem drive_direct_url trong app/drive.py.
    drive_download_url: str | None = None


class JobError(BaseModel):
    code: str
    message: str
    detail: str | None = None


class JobResponse(BaseModel):
    job_id: str
    status: JobStatus
    progress: float = 0.0
    stage_message: str = ""
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    output: JobOutput | None = None
    error: JobError | None = None


class CreateJobResponse(BaseModel):
    job_id: str
    status: JobStatus
    created_at: datetime


class HealthResponse(BaseModel):
    status: str = "ok"
    ffmpeg: str | None = None
    active_jobs: int = 0
    tmp_free_mb: float = 0.0


class ErrorEnvelope(BaseModel):
    """Body của mọi HTTP error: ``{"error": {"code": ..., "message": ...}}``."""

    error: JobError
