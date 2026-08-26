"""Fixtures dùng chung cho test."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings
from app.ffmpeg_runner import ProbeResult
from app.models import RenderOptions

SAMPLE_SRT = (
    "1\n"
    "00:00:01,000 --> 00:00:03,500\n"
    "Xin chào thế giới\n"
    "\n"
    "2\n"
    "00:00:04,000 --> 00:00:06,000\n"
    "Dòng thứ hai\n"
)


@pytest.fixture
def default_options() -> RenderOptions:
    """Options mặc định theo SPEC §4 (client gửi `{}`)."""
    return RenderOptions()


@pytest.fixture
def tmp_workspace(tmp_path: Path) -> Path:
    """Workspace giả của một job, đúng layout SPEC §8."""
    workspace = tmp_path / "jobs" / "testjob01"
    workspace.mkdir(parents=True)
    return workspace


@pytest.fixture
def sample_srt_content() -> str:
    return SAMPLE_SRT


@pytest.fixture
def fake_probe_result() -> ProbeResult:
    """Video 194.2s, 1920x1080@30, có audio track, H.264 (copy được)."""
    return ProbeResult(
        duration=194.2,
        width=1920,
        height=1080,
        fps=30.0,
        has_audio=True,
        has_video=True,
        video_codec="h264",
        audio_codec="aac",
    )


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Settings trỏ WORK_DIR vào tmp_path, TTL ngắn để test janitor."""
    return Settings(
        API_KEY="k",
        WORK_DIR=str(tmp_path / "jobs"),
        JOB_TTL_SECONDS=1,
        _env_file=None,  # type: ignore[call-arg]
    )
