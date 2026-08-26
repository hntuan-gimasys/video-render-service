"""Job state trong RAM + vòng đời job — docs/SPEC.md §2, §8.

Tách khỏi ``app.jobs`` để giữ mỗi file dưới 400 dòng: ở đây là mô hình dữ liệu,
kho lưu và các tác vụ vòng đời (janitor, cancel, shutdown); phần pipeline render
nằm ở ``app.jobs`` và re-export lại toàn bộ tên công khai của module này.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from app.config import Settings
from app.models import JobError, JobOutput, JobStatus, RenderOptions
from app.utils import bind_job, safe_rmtree, utcnow

logger = logging.getLogger(__name__)

JANITOR_INTERVAL_SECONDS = 300
CANCEL_GRACE_SECONDS = 5.0

__all__ = [
    "JobSources",
    "Job",
    "JobStore",
    "JANITOR_INTERVAL_SECONDS",
    "CANCEL_GRACE_SECONDS",
    "janitor_loop",
    "cancel_job",
    "shutdown_jobs",
]


@dataclass(slots=True)
class JobSources:
    """Nguồn input người dùng gửi lên (file đã lưu hoặc link Drive).

    ``video_path`` là file THẬT SỰ đem đi render — sau bước ghép clip thì nó
    trỏ tới file đã ghép, không còn là file nguồn nào nữa. Danh sách nguồn để
    ghép nằm ở ``video_paths``/``video_urls``, đánh số từ 1 theo đúng thứ tự
    này (``options.clips[].source`` tham chiếu vào đó).
    """

    video_path: Path | None = None
    video_url: str | None = None
    # Link thư mục Drive: quét ra danh sách video rồi chỉ tải những cái mà kịch
    # bản dựng thật sự dùng tới (xem app/prepare.py).
    video_folder_url: str | None = None
    video_paths: list[Path] = field(default_factory=list)
    video_urls: list[str] = field(default_factory=list)
    # Tên file GỐC của từng nguồn, cùng thứ tự với danh sách nguồn cuối cùng
    # (upload trước, Drive sau). Dùng để khớp clips[].source khai bằng tên file
    # thay vì số hiệu — tên trên Drive chỉ biết được sau khi tải về.
    video_labels: list[str] = field(default_factory=list)
    srt_path: Path | None = None
    srt_url: str | None = None
    music_path: Path | None = None
    music_url: str | None = None


@dataclass(slots=True)
class Job:
    id: str
    workspace: Path
    options: RenderOptions
    sources: JobSources
    status: JobStatus = JobStatus.QUEUED
    progress: float = 0.0
    stage_message: str = ""
    created_at: datetime = field(default_factory=utcnow)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    output: JobOutput | None = None
    error: JobError | None = None
    process: asyncio.subprocess.Process | None = None
    task: asyncio.Task[None] | None = None
    cancel_requested: bool = False

    @property
    def is_terminal(self) -> bool:
        return self.status.is_terminal

    @property
    def output_path(self) -> Path:
        return self.workspace / self.options.output.filename


class JobStore:
    """dict job + asyncio.Lock. Sống trong RAM của đúng một process."""

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = asyncio.Lock()

    async def create(self, job: Job) -> Job:
        async with self._lock:
            self._jobs[job.id] = job
        return job

    async def get(self, job_id: str) -> Job | None:
        async with self._lock:
            return self._jobs.get(job_id)

    async def delete(self, job_id: str) -> Job | None:
        async with self._lock:
            return self._jobs.pop(job_id, None)

    async def all_jobs(self) -> list[Job]:
        async with self._lock:
            return list(self._jobs.values())

    async def count_active(self) -> int:
        """Số job chưa ở trạng thái cuối — dùng để chặn QUEUE_FULL."""
        async with self._lock:
            return sum(1 for job in self._jobs.values() if not job.status.is_terminal)

    async def update(self, job_id: str, **fields: object) -> Job | None:
        async with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            for key, value in fields.items():
                setattr(job, key, value)
            return job

    async def list_expired(self, ttl_seconds: int) -> list[Job]:
        """Job đã xong và quá TTL kể từ ``finished_at``."""
        now = utcnow()
        async with self._lock:
            return [
                job
                for job in self._jobs.values()
                if job.status.is_terminal
                and job.finished_at is not None
                and (now - job.finished_at).total_seconds() > ttl_seconds
            ]



# --------------------------------------------------------------------------- #
# Vòng đời job: janitor, cancel, shutdown
# --------------------------------------------------------------------------- #
async def janitor_loop(
    store: JobStore, settings: Settings, interval: float = JANITOR_INTERVAL_SECONDS
) -> None:
    """Mỗi 300s xoá workspace của job đã xong quá TTL."""
    while True:
        try:
            await asyncio.sleep(interval)
            expired = await store.list_expired(settings.job_ttl_seconds)
            for job in expired:
                # Xoá FILE trước, record sau. Nếu làm ngược lại mà bị cancel
                # giữa hai bước thì workspace thành mồ côi: không còn record nào
                # trỏ tới nó, mà /tmp là RAM nên chỗ đó mất luôn tới khi
                # container chết. Thứ tự này tệ nhất chỉ để lại một record rỗng,
                # vòng janitor sau dọn tiếp.
                await asyncio.to_thread(safe_rmtree, job.workspace)
                await store.delete(job.id)
                bind_job(logger, job.id).info(
                    "Janitor dọn job quá TTL", extra={"ttl": settings.job_ttl_seconds}
                )
        except asyncio.CancelledError:
            logger.info("Janitor dừng")
            raise
        except Exception:  # noqa: BLE001 - janitor không được chết vì một job lỗi
            logger.exception("Janitor gặp lỗi, vẫn tiếp tục chạy")


async def cancel_job(job: Job, *, remove_workspace: bool = True) -> None:
    """Terminate ffmpeg, chờ 5s, kill nếu cần, rồi xoá workspace."""
    log = bind_job(logger, job.id)
    job.cancel_requested = True
    process = job.process

    if process is not None and process.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=CANCEL_GRACE_SECONDS)
        except (TimeoutError, asyncio.TimeoutError):
            log.warning("ffmpeg không tự thoát sau 5s -> kill")
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            with contextlib.suppress(Exception):
                await process.wait()

    task = job.task
    if task is not None and not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    if not job.status.is_terminal:
        job.status = JobStatus.CANCELLED
        job.finished_at = job.finished_at or utcnow()
        job.stage_message = "Đã huỷ"

    if remove_workspace:
        await asyncio.to_thread(safe_rmtree, job.workspace)
    log.info("Đã huỷ job", extra={"status": job.status.value})


async def shutdown_jobs(store: JobStore, timeout: float = 8.0) -> None:
    """SIGTERM: Cloud Run cho 10s -> huỷ mọi job đang chạy trong 8s rồi thoát."""
    jobs = [job for job in await store.all_jobs() if not job.status.is_terminal]
    if not jobs:
        return
    logger.warning("Nhận SIGTERM, huỷ job đang chạy", extra={"count": len(jobs)})
    with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
        await asyncio.wait_for(
            asyncio.gather(*(cancel_job(job) for job in jobs), return_exceptions=True),
            timeout=timeout,
        )
    logger.warning("Đã dọn xong job trước khi thoát")
