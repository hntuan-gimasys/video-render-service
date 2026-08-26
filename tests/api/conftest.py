"""Fixtures riêng cho test API (chỉ áp dụng trong thư mục này)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest

from app import jobs as jobs_mod
from app import main as main_mod
from app import merge as merge_mod
from app import prepare as prepare_mod
from app.clips import MERGED_NAME
from app.config import get_settings
from app.drive import DriveFileMeta
from app.ffmpeg_runner import ProbeResult
from app.utils import DriveDownloadFailed
from tests.helpers import API_KEY, FOLDER_VIDEOS, PROBE_OK


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    monkeypatch.setenv("API_KEY", API_KEY)
    monkeypatch.setenv("WORK_DIR", str(tmp_path / "jobs"))
    monkeypatch.setenv("LOG_LEVEL", "WARNING")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
async def _lifespan(_env: None) -> AsyncIterator[None]:
    """ASGITransport không chạy lifespan -> tự vào lifespan_context."""
    async with main_mod.app.router.lifespan_context(main_mod.app):
        yield


@pytest.fixture(autouse=True)
def fake_drive(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Thư mục Drive giả: liệt kê được, tải được, không đụng mạng.

    Test đổi ``state["names"]`` trước khi gọi API để dựng nội dung thư mục khác;
    ``state["downloaded"]`` cho biết service đã tải đúng những file nào — đó là
    cách kiểm "chỉ tải video mà kịch bản dùng tới".
    """
    state: dict[str, Any] = {"names": list(FOLDER_VIDEOS), "downloaded": [], "folder_ids": []}

    def _meta(name: str, size: int = 1024) -> DriveFileMeta:
        return DriveFileMeta(
            file_id=f"id-{name}", name=name, size_bytes=size, mime_type="video/mp4"
        )

    async def _list(folder_id: str) -> list[DriveFileMeta]:
        state["folder_ids"].append(folder_id)
        if not state["names"]:
            raise DriveDownloadFailed(f"Thư mục Drive {folder_id} không có video nào")
        return [_meta(name) for name in state["names"]]

    async def _download(
        file_id: str, dest: Path, on_progress: Any = None, *, max_bytes: int = 0
    ) -> DriveFileMeta:
        name = file_id.removeprefix("id-")
        dest.write_bytes(b"v" * 64)
        state["downloaded"].append(name)
        if on_progress:
            on_progress(100.0)
        return _meta(name, size=64)

    monkeypatch.setattr(prepare_mod, "list_folder_videos", _list)
    monkeypatch.setattr(prepare_mod, "download_file", _download)
    return state


@pytest.fixture
def fake_render(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """ffmpeg giả: ghi ra output.mp4 rồi trả 0."""
    recorded: dict[str, Any] = {}

    async def _fake_probe(_path: Path) -> ProbeResult:
        return PROBE_OK

    async def _fake_run(
        cmd: list[str],
        total_duration: float,
        on_progress: Any = None,
        stderr_buf: Any = None,
        *,
        cwd: Path | None = None,
        on_start: Any = None,
    ) -> int:
        recorded["cmd"] = cmd
        if cwd is not None:
            (Path(cwd) / "output.mp4").write_bytes(b"MP4DATA" * 100)
        return 0

    async def _fake_merge(
        cmd: list[str],
        total_duration: float,
        on_progress: Any = None,
        stderr_buf: Any = None,
        *,
        cwd: Path | None = None,
        on_start: Any = None,
    ) -> int:
        recorded["merge_cmd"] = cmd
        recorded["merge_duration"] = total_duration
        if cwd is not None:
            (Path(cwd) / MERGED_NAME).write_bytes(b"MERGED" * 100)
        return 0

    monkeypatch.setattr(prepare_mod, "probe", _fake_probe)
    monkeypatch.setattr(jobs_mod, "probe", _fake_probe)
    monkeypatch.setattr(jobs_mod, "run_ffmpeg", _fake_run)
    monkeypatch.setattr(merge_mod, "probe", _fake_probe)
    monkeypatch.setattr(merge_mod, "run_ffmpeg", _fake_merge)
    return recorded
