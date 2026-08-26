"""Helper dùng chung cho nhiều file test (không phải fixture)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.config import Settings
from app.ffmpeg_cmd import ProbeResult, RenderInputs, build_ffmpeg_command
from app.job_store import Job, JobSources
from app.models import RenderOptions

API_KEY = "test-key-123"
AUTH = {"Authorization": f"Bearer {API_KEY}"}
DRIVE_URL = "https://drive.google.com/file/d/1AbCdEfGhIjKlMnOpQrStUvWxYz012345/view"

#: Video 5s 320x240 có audio, H.264 (copy được) — dùng cho test pipeline.
PROBE_OK = ProbeResult(
    duration=5.0,
    width=320,
    height=240,
    fps=25.0,
    has_audio=True,
    has_video=True,
    video_codec="h264",
    audio_codec="aac",
)

FONTS = "/app/fonts"
#: force_style kỳ vọng cho khung 16:9 (fake_probe_result 1920x1080, và cả
#: 1280x720 vì cùng tỉ lệ): cỡ chữ tự tính = 1920 × 0.04 × 288 / 1080 = 20.48,
#: viền = 20.48 × 0.08 = 1.64, margin ngang = 1920 × 0.06 × 288 / 1080 = 30.72 -> 31.
STYLE = (
    "FontName=Liberation Serif,FontSize=20.48,PrimaryColour=&H00FFFFFF&,"
    "OutlineColour=&H00000000&,BorderStyle=1,Outline=1.64,Shadow=0,"
    "Alignment=2,MarginV=40,MarginL=31,MarginR=31,Bold=-1,Italic=-1"
)
#: Phần đầu argv luôn giống nhau ở mọi tổ hợp.
HEAD = [
    "ffmpeg",
    "-y",
    "-hide_banner",
    "-nostdin",
    "-loglevel",
    "error",
    "-progress",
    "pipe:1",
    "-nostats",
]


def build(
    inputs: RenderInputs,
    probe_result: ProbeResult,
    opts: RenderOptions,
    workspace: Path,
) -> list[str]:
    """build_ffmpeg_command với fonts_dir cố định cho snapshot test."""
    return build_ffmpeg_command(inputs, probe_result, opts, workspace, fonts_dir=FONTS)


def make_job(settings: Settings, options: RenderOptions | None = None, **kwargs: Any) -> Job:
    """Job có workspace thật và input.mp4 giả trong đó."""
    workspace = settings.work_dir / "job1"
    workspace.mkdir(parents=True, exist_ok=True)
    video = workspace / "input.mp4"
    video.write_bytes(b"fake-video")
    sources = JobSources(video_path=video, **kwargs)
    return Job(
        id="job1",
        workspace=workspace,
        options=options or RenderOptions(),
        sources=sources,
    )


def patch_render(
    monkeypatch: pytest.MonkeyPatch,
    *,
    exit_code: int = 0,
    write_output: bool = True,
    probe_result: ProbeResult = PROBE_OK,
    progress: list[tuple[float, float]] | None = None,
) -> dict[str, Any]:
    """Thay probe/run_ffmpeg trong app.jobs bằng bản giả.

    Trả về dict ghi lại argv, cwd, total_duration của lần gọi cuối.
    """
    from app import jobs as jobs_mod

    recorded: dict[str, Any] = {}

    async def _fake_probe(path: Path) -> ProbeResult:
        recorded.setdefault("probed", []).append(Path(path).name)
        return probe_result

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
        recorded["cwd"] = cwd
        recorded["total_duration"] = total_duration
        for pct, secs in progress or []:
            if on_progress:
                on_progress(pct, secs)
        if stderr_buf is not None and exit_code != 0:
            stderr_buf.append("x264 [error]: something broke")
        if write_output and cwd is not None:
            (Path(cwd) / "output.mp4").write_bytes(b"rendered-output")
        return exit_code

    from app import merge as merge_mod
    from app import prepare as prepare_mod

    # probe/run_ffmpeg nay nằm ở ba module của pipeline (chuẩn bị, ghép,
    # render) nên phải thay cả ba, nếu không test sẽ gọi ffmpeg thật.
    for module in (jobs_mod, prepare_mod, merge_mod):
        monkeypatch.setattr(module, "probe", _fake_probe, raising=False)
        monkeypatch.setattr(module, "run_ffmpeg", _fake_run, raising=False)
    return recorded


#: Thư mục Drive giả dùng cho mọi test API (xem fixture fake_drive).
FOLDER_URL = "https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOpQrStUvWxYz012345"
#: Tên các video mà fixture fake_drive bày sẵn trong thư mục đó.
FOLDER_VIDEOS = ["c1.mp4", "c2.mp4", "c3.mp4"]


def job_form(**extra: str) -> dict[str, str]:
    """Form tối thiểu để tạo job: link thư mục + những ô muốn thêm."""
    return {"video_folder_url": FOLDER_URL, **extra}


async def create_job(client: httpx.AsyncClient, **form: str) -> str:
    """Tạo job, khẳng định 202, trả về job_id."""
    response = await client.post("/api/jobs", headers=AUTH, data=job_form(**form))
    assert response.status_code == 202, response.text
    return str(response.json()["job_id"])


async def run_job_to_end(client: httpx.AsyncClient, **form: str) -> dict[str, Any]:
    """Tạo job rồi chờ tới trạng thái cuối, trả về body GET /api/jobs/{id}."""
    return await wait_terminal(client, await create_job(client, **form))


async def api_client() -> httpx.AsyncClient:
    """Client gọi thẳng ASGI app, không mở socket thật."""
    from app import main as main_mod

    transport = httpx.ASGITransport(app=main_mod.app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def wait_terminal(
    client: httpx.AsyncClient, job_id: str, timeout: float = 5.0
) -> dict[str, Any]:
    """Poll tới khi job vào trạng thái cuối, trả về body của GET /api/jobs/{id}."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        response = await client.get(f"/api/jobs/{job_id}", headers=AUTH)
        payload = response.json()
        if payload["status"] in {"succeeded", "failed", "cancelled"}:
            return payload
        await asyncio.sleep(0.01)
    raise AssertionError(f"job {job_id} không kết thúc trong {timeout}s")
