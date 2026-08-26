"""Test cho app/config.py."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings, get_settings
from app.utils import MB


def _settings(**overrides: object) -> Settings:
    # _env_file=None: bỏ qua .env của máy dev để test luôn tất định.
    return Settings(API_KEY="secret", _env_file=None, **overrides)  # type: ignore[arg-type]


def test_defaults_match_spec_table(tmp_path: Path) -> None:
    settings = _settings(WORK_DIR=str(tmp_path / "jobs"))
    assert settings.max_folder_videos == 30
    assert settings.max_download_mb == 4096
    assert settings.max_concurrent_jobs == 1
    assert settings.max_queued_jobs == 10
    assert settings.job_ttl_seconds == 3600
    assert settings.ffmpeg_threads == 0
    assert settings.fonts_dir == "/app/fonts"
    assert settings.log_level == "INFO"
    assert settings.port == 8080
    assert settings.google_application_credentials == ""


def test_empty_api_key_fails_fast() -> None:
    with pytest.raises(ValidationError, match="API_KEY"):
        Settings(API_KEY="   ", _env_file=None)  # type: ignore[call-arg]


def test_work_dir_is_created(tmp_path: Path) -> None:
    target = tmp_path / "deep" / "jobs"
    settings = _settings(WORK_DIR=str(target))
    assert not target.exists()
    assert settings.work_dir == target
    assert target.is_dir()


def test_byte_helpers_and_log_level_normalisation(tmp_path: Path) -> None:
    settings = _settings(WORK_DIR=str(tmp_path), LOG_LEVEL="debug", MAX_UPLOAD_MB=10)
    assert settings.log_level == "DEBUG"
    assert settings.max_download_bytes == 4096 * MB
    assert settings.fonts_path == Path("/app/fonts")


def test_invalid_values_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        _settings(WORK_DIR=str(tmp_path), LOG_LEVEL="LOUD")
    with pytest.raises(ValidationError):
        _settings(WORK_DIR=str(tmp_path), MAX_CONCURRENT_JOBS=0)


def test_settings_read_from_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("API_KEY", "from-env")
    monkeypatch.setenv("WORK_DIR", str(tmp_path / "w"))
    monkeypatch.setenv("MAX_QUEUED_JOBS", "3")
    get_settings.cache_clear()
    try:
        settings = get_settings()
        assert settings.api_key == "from-env"
        assert settings.max_queued_jobs == 3
        assert get_settings() is settings  # cache
    finally:
        get_settings.cache_clear()
