"""Test API: auth, validate input, tạo job, đọc trạng thái — SPEC §3.1-3.2."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from app import main as main_mod
from app.config import get_settings
from tests.helpers import AUTH, DRIVE_URL, FOLDER_URL
from tests.helpers import api_client as _client
from tests.helpers import job_form as _form
from tests.helpers import run_job_to_end as _run_job


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #
async def test_healthz_needs_no_auth() -> None:
    async with await _client() as client:
        response = await client.get("/healthz")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["active_jobs"] == 0
    assert payload["tmp_free_mb"] > 0
    assert "ffmpeg" in payload


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer wrong"}, {"Authorization": "Basic abc"}, {"Authorization": ""}],
)
async def test_endpoints_require_api_key(headers: dict[str, str]) -> None:
    # /download KHÔNG nằm trong danh sách này: nó cố ý mở, xem
    # test_download_needs_no_api_key.
    async with await _client() as client:
        for method, url in [
            ("get", "/api/jobs/abc"),
            ("delete", "/api/jobs/abc"),
        ]:
            response = await getattr(client, method)(url, headers=headers)
            assert response.status_code == 401, (method, url)
            assert response.json()["error"]["code"] == "UNAUTHORIZED"


async def test_create_job_requires_api_key() -> None:
    async with await _client() as client:
        response = await client.post("/api/jobs", data=_form())
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"


# --------------------------------------------------------------------------- #
# POST /api/jobs — validate
# --------------------------------------------------------------------------- #
async def test_no_video_source() -> None:
    async with await _client() as client:
        response = await client.post("/api/jobs", headers=AUTH, data={"options": "{}"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "NO_VIDEO_SOURCE"


async def test_folder_link_must_look_like_a_drive_folder() -> None:
    async with await _client() as client:
        response = await client.post(
            "/api/jobs", headers=AUTH, data={"video_folder_url": "https://example.com/abc"}
        )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_DRIVE_URL"


async def test_file_link_is_not_a_folder_link() -> None:
    # Link tới MỘT file khác link thư mục; báo ngay còn hơn quét rỗng rồi mới lỗi.
    async with await _client() as client:
        response = await client.post(
            "/api/jobs", headers=AUTH, data={"video_folder_url": DRIVE_URL}
        )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_DRIVE_URL"


@pytest.mark.parametrize(
    "options",
    ['{"output":{"crf":99}}', '{"output":{"preset":"turbo"}}', '{"music":{"volume":5}}'],
)
async def test_invalid_options_returns_422(options: str) -> None:
    async with await _client() as client:
        response = await client.post("/api/jobs", headers=AUTH, data=_form(options=options))
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_OPTIONS"


async def test_malformed_options_json_returns_422() -> None:
    async with await _client() as client:
        response = await client.post("/api/jobs", headers=AUTH, data=_form(options="{not json"))
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_OPTIONS"


async def test_workspace_removed_when_validation_fails(tmp_path: Path) -> None:
    async with await _client() as client:
        await client.post("/api/jobs", headers=AUTH, data=_form(options="{not json"))
    work_dir = tmp_path / "jobs"
    # Lỗi trước khi tạo workspace -> không để lại rác trong /tmp.
    assert not work_dir.exists() or list(work_dir.iterdir()) == []


async def test_queue_full(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAX_QUEUED_JOBS", "1")
    get_settings.cache_clear()

    async def _never_ending(*_args: Any, **_kwargs: Any) -> None:
        await asyncio.sleep(30)

    monkeypatch.setattr(main_mod, "run_job", _never_ending)
    async with await _client() as client:
        first = await client.post("/api/jobs", headers=AUTH, data=_form())
        assert first.status_code == 202
        second = await client.post("/api/jobs", headers=AUTH, data=_form())
    assert second.status_code == 429
    assert second.json()["error"]["code"] == "QUEUE_FULL"


# --------------------------------------------------------------------------- #
# POST /api/jobs — đường thành công
# --------------------------------------------------------------------------- #
async def test_create_job_returns_202_and_runs(fake_render: dict[str, Any]) -> None:
    async with await _client() as client:
        response = await client.post(
            "/api/jobs",
            headers=AUTH,
            data=_form(options=json.dumps({"subtitle": {"enabled": False}})),
        )
        assert response.status_code == 202
        created = response.json()
        assert created["status"] == "queued"
        assert len(created["job_id"]) == 12
        assert created["created_at"]

        final = await _run_job(client, options=json.dumps({"subtitle": {"enabled": False}}))

    assert final["status"] == "succeeded"
    assert final["progress"] == 100.0
    assert final["error"] is None
    assert final["output"]["filename"] == "output.mp4"
    assert final["output"]["size_bytes"] == len(b"MP4DATA" * 100)
    assert final["output"]["download_url"].endswith("/download")
    assert final["output"]["drive_file_id"] is None
    assert final["started_at"] and final["finished_at"]


async def test_whole_folder_is_merged_when_no_clips_declared(
    fake_render: dict[str, Any], fake_drive: dict[str, Any]
) -> None:
    async with await _client() as client:
        final = await _run_job(client)
    assert final["status"] == "succeeded", final.get("error")
    # Không khai clips = ghép trọn cả thư mục theo thứ tự tên file.
    assert fake_drive["downloaded"] == ["c1.mp4", "c2.mp4", "c3.mp4"]
    # Ba đoạn -> hai lần chuyển cảnh (xfade/acrossfade là mặc định, xem
    # tests/test_transitions.py).
    merge_cmd = " ".join(fake_render["merge_cmd"])
    assert merge_cmd.count("xfade=transition=") == 2


async def test_create_job_with_srt_and_music(
    monkeypatch: pytest.MonkeyPatch, fake_render: dict[str, Any]
) -> None:
    from app import prepare as prepare_mod

    original = prepare_mod.download_file

    async def _fake_download(file_id: str, dest: Path, *args: Any, **kwargs: Any) -> Any:
        if dest.name.startswith("music"):
            dest.write_bytes(b"m" * 32)
            return None
        return await original(file_id, dest, *args, **kwargs)

    monkeypatch.setattr(prepare_mod, "download_file", _fake_download)

    srt = "1\n00:00:01,000 --> 00:00:02,000\nXin chào\n"
    async with await _client() as client:
        final = await _run_job(
            client,
            srt_text=srt,
            music_url=DRIVE_URL,
            options=json.dumps({"subtitle": {"font_size": 28}}),
        )

    assert final["status"] == "succeeded", final.get("error")
    argv = " ".join(fake_render["cmd"])
    # .srt được dựng lại thành styled.ass có PlayRes đúng khung hình. Cỡ chữ
    # nay nằm trong dòng Style của chính file đó chứ không còn trong argv, nên
    # argv không được kèm force_style nữa (xem tests/test_overlay.py).
    assert "subtitles=styled.ass" in argv
    assert "force_style" not in argv
    # Nhạc nền thay hẳn tiếng gốc (mặc định) nên không còn amix; xem
    # tests/test_ffmpeg_branches.py cho cả hai nhánh.
    assert "[1:a]volume=0.18" in argv
    assert "amix" not in argv


async def test_bad_music_link_is_rejected_in_the_request() -> None:
    async with await _client() as client:
        response = await client.post(
            "/api/jobs", headers=AUTH, data=_form(music_url="https://example.com/nhac.mp3")
        )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_DRIVE_URL"


async def test_empty_folder_fails_the_job_with_a_clear_message(
    fake_render: dict[str, Any], fake_drive: dict[str, Any]
) -> None:
    fake_drive["names"] = []
    async with await _client() as client:
        final = await _run_job(client)
    assert final["status"] == "failed"
    assert final["error"]["code"] == "DRIVE_DOWNLOAD_FAILED"


# --------------------------------------------------------------------------- #
# GET / DELETE
# --------------------------------------------------------------------------- #
async def test_get_unknown_job_returns_404() -> None:
    async with await _client() as client:
        response = await client.get("/api/jobs/khongcothat", headers=AUTH)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "JOB_NOT_FOUND"


async def test_delete_finished_job_removes_the_record(
    fake_render: dict[str, Any],
) -> None:
    async with await _client() as client:
        final = await _run_job(client)
        assert final["status"] == "succeeded"
        job_id = final["job_id"]

        deleted = await client.delete(f"/api/jobs/{job_id}", headers=AUTH)
        assert deleted.status_code == 204
        assert (await client.get(f"/api/jobs/{job_id}", headers=AUTH)).status_code == 404


async def test_delete_unknown_job_returns_404() -> None:
    async with await _client() as client:
        response = await client.delete("/api/jobs/khongcothat", headers=AUTH)
    assert response.status_code == 404


async def test_folder_url_reaches_the_drive_client(
    fake_render: dict[str, Any], fake_drive: dict[str, Any]
) -> None:
    async with await _client() as client:
        await _run_job(client)
    # ID thư mục được bóc đúng từ link đầy đủ.
    assert fake_drive["folder_ids"] == [FOLDER_URL.rsplit("/", 1)[-1]]
