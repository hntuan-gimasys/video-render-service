"""Cấu hình đọc từ biến môi trường — docs/SPEC.md §7."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.utils import MB


class Settings(BaseSettings):
    """Tên field khớp đúng biến môi trường trong bảng SPEC §7."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        populate_by_name=True,
    )

    api_key: str = Field(default="", alias="API_KEY")
    work_dir_path: str = Field(default="/tmp/jobs", alias="WORK_DIR")
    max_download_mb: Annotated[int, Field(ge=1)] = Field(default=4096, alias="MAX_DOWNLOAD_MB")
    max_concurrent_jobs: Annotated[int, Field(ge=1)] = Field(
        default=1, alias="MAX_CONCURRENT_JOBS"
    )
    max_queued_jobs: Annotated[int, Field(ge=1)] = Field(default=10, alias="MAX_QUEUED_JOBS")
    # Trần số video tải về từ MỘT thư mục Drive. /tmp trên Cloud Run là RAM nên
    # thư mục vài chục video là đủ để hết bộ nhớ giữa chừng.
    max_folder_videos: Annotated[int, Field(ge=1, le=200)] = Field(
        default=30, alias="MAX_FOLDER_VIDEOS"
    )
    job_ttl_seconds: Annotated[int, Field(ge=1)] = Field(default=3600, alias="JOB_TTL_SECONDS")
    ffmpeg_threads: Annotated[int, Field(ge=0)] = Field(default=0, alias="FFMPEG_THREADS")
    fonts_dir: str = Field(default="/app/fonts", alias="FONTS_DIR")
    google_application_credentials: str = Field(
        default="", alias="GOOGLE_APPLICATION_CREDENTIALS"
    )
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    port: int = Field(default=8080, alias="PORT")

    @field_validator("api_key")
    @classmethod
    def _api_key_required(cls, value: str) -> str:
        # Fail fast: service không có API key thì mọi endpoint đều mở.
        if not value.strip():
            raise ValueError("API_KEY là bắt buộc, không được để rỗng")
        return value.strip()

    @field_validator("log_level")
    @classmethod
    def _valid_log_level(cls, value: str) -> str:
        level = value.strip().upper()
        allowed = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"}
        if level not in allowed:
            raise ValueError(f"LOG_LEVEL phải thuộc {sorted(allowed)}")
        return level

    @property
    def work_dir(self) -> Path:
        """Thư mục gốc chứa workspace của job, tự tạo nếu chưa có."""
        path = Path(self.work_dir_path)
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def fonts_path(self) -> Path:
        return Path(self.fonts_dir)

    @property
    def max_download_bytes(self) -> int:
        return self.max_download_mb * MB


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Settings dạng singleton (cache) — dùng làm FastAPI dependency."""
    return Settings()
