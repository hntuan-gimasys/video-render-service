"""Test API: download + HTTP Range, DELETE, healthz — SPEC §3.3-3.5."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from app import jobs as jobs_mod
from app import prepare as prepare_mod
from app import main as main_mod
from app.ffmpeg_runner import ProbeResult
from app.models import JobStatus
from app.streaming import build_download_response, parse_range
from tests.helpers import AUTH, PROBE_OK
from tests.helpers import api_client as _client
from tests.helpers import job_form as _form
from tests.helpers import wait_terminal as _wait_terminal


# --------------------------------------------------------------------------- #
# GET /api/jobs/{id}/download
# --------------------------------------------------------------------------- #
async def test_download_not_ready_returns_409(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _never_ending(*_args: Any, **_kwargs: Any) -> None:
        await asyncio.sleep(30)

    monkeypatch.setattr(main_mod, "run_job", _never_ending)
    async with await _client() as client:
        created = await client.post("/api/jobs", headers=AUTH, data=_form())
        job_id = created.json()["job_id"]
        response = await client.get(f"/api/jobs/{job_id}/download", headers=AUTH)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "JOB_NOT_READY"


async def test_download_full_file(fake_render: dict[str, Any]) -> None:
    expected = b"MP4DATA" * 100
    async with await _client() as client:
        created = await client.post("/api/jobs", headers=AUTH, data=_form())
        job_id = created.json()["job_id"]
        await _wait_terminal(client, job_id)
        response = await client.get(f"/api/jobs/{job_id}/download", headers=AUTH)

    assert response.status_code == 200
    assert response.content == expected
    assert response.headers["content-type"] == "video/mp4"
    assert response.headers["content-length"] == str(len(expected))
    assert response.headers["content-disposition"] == 'attachment; filename="output.mp4"'
    assert response.headers["accept-ranges"] == "bytes"


async def test_download_range_returns_206(fake_render: dict[str, Any]) -> None:
    expected = b"MP4DATA" * 100
    async with await _client() as client:
        created = await client.post("/api/jobs", headers=AUTH, data=_form())
        job_id = created.json()["job_id"]
        await _wait_terminal(client, job_id)
        response = await client.get(
            f"/api/jobs/{job_id}/download", headers={**AUTH, "Range": "bytes=10-19"}
        )

    assert response.status_code == 206
    assert response.content == expected[10:20]
    assert response.headers["content-length"] == "10"
    assert response.headers["content-range"] == f"bytes 10-19/{len(expected)}"


async def test_download_open_ended_range(fake_render: dict[str, Any]) -> None:
    expected = b"MP4DATA" * 100
    async with await _client() as client:
        created = await client.post("/api/jobs", headers=AUTH, data=_form())
        job_id = created.json()["job_id"]
        await _wait_terminal(client, job_id)
        response = await client.get(
            f"/api/jobs/{job_id}/download", headers={**AUTH, "Range": "bytes=690-"}
        )
    assert response.status_code == 206
    assert response.content == expected[690:]


# --------------------------------------------------------------------------- #
# parse_range
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("header", "size", "expected"),
    [
        (None, 1000, (0, 999)),
        ("bytes=0-99", 1000, (0, 99)),
        ("bytes=100-", 1000, (100, 999)),
        ("bytes=-200", 1000, (800, 999)),
        ("bytes=0-99999", 1000, (0, 999)),  # kẹp về cuối file
        ("bytes=999-0", 1000, (999, 999)),  # end < start -> tới cuối file
        ("bytes=-", 1000, (0, 999)),
        ("rubbish", 1000, (0, 999)),
        ("bytes=0-10", 0, (0, 0)),
    ],
)
def test_parse_range(header: str | None, size: int, expected: tuple[int, int]) -> None:
    assert parse_range(header, size) == expected


# --------------------------------------------------------------------------- #
# Body lớn phải bỏ Content-Length để uvicorn gửi Transfer-Encoding: chunked,
# nếu không Cloud Run đổi response thành 500 (xem chú thích trong
# app/streaming.py). Gọi thẳng builder: StreamingResponse đọc file lười nên
# total_size 40 MB không cần file thật trên đĩa.
# --------------------------------------------------------------------------- #
def test_large_response_omits_content_length() -> None:
    response = build_download_response(Path("output.mp4"), 40_000_000, None)
    assert response.status_code == 200
    assert "content-length" not in response.headers
    assert response.headers["accept-ranges"] == "bytes"


def test_small_response_keeps_content_length() -> None:
    response = build_download_response(Path("output.mp4"), 1_000, None)
    assert response.headers["content-length"] == "1000"


def test_large_range_slice_also_omits_content_length() -> None:
    """Giới hạn 32 MiB áp cho cả 206, không chỉ 200."""
    response = build_download_response(Path("output.mp4"), 40_000_000, "bytes=1-")
    assert response.status_code == 206
    assert response.headers["content-range"] == "bytes 1-39999999/40000000"
    assert "content-length" not in response.headers


# --------------------------------------------------------------------------- #
# DELETE /api/jobs/{id}
# --------------------------------------------------------------------------- #
async def test_delete_job_cancels_and_keeps_record(
    monkeypatch: pytest.MonkeyPatch, fake_drive: dict[str, Any]
) -> None:
    # Thư mục một video -> bỏ qua bước ghép, treo đúng ở bước render để huỷ.
    fake_drive["names"] = ["only.mp4"]
    started = asyncio.Event()

    async def _fake_probe(_path: Path) -> ProbeResult:
        return PROBE_OK

    async def _fake_run(*_args: Any, **_kwargs: Any) -> int:
        started.set()
        await asyncio.sleep(30)
        return 0

    monkeypatch.setattr(prepare_mod, "probe", _fake_probe)
    monkeypatch.setattr(jobs_mod, "probe", _fake_probe)
    monkeypatch.setattr(jobs_mod, "run_ffmpeg", _fake_run)

    async with await _client() as client:
        created = await client.post("/api/jobs", headers=AUTH, data=_form())
        job_id = created.json()["job_id"]
        await asyncio.wait_for(started.wait(), timeout=5)

        response = await client.delete(f"/api/jobs/{job_id}", headers=AUTH)
        assert response.status_code == 204
        assert response.content == b""

        # Quyết định B8: record còn lại với status=cancelled, file đã bị xoá
        status = await client.get(f"/api/jobs/{job_id}", headers=AUTH)
        assert status.status_code == 200
        assert status.json()["status"] == JobStatus.CANCELLED.value

        download = await client.get(f"/api/jobs/{job_id}/download", headers=AUTH)
        assert download.status_code == 409


async def test_delete_unknown_job_returns_404() -> None:
    async with await _client() as client:
        response = await client.delete("/api/jobs/khongtontai", headers=AUTH)
    assert response.status_code == 404


async def test_delete_finished_job_removes_record(fake_render: dict[str, Any]) -> None:
    async with await _client() as client:
        created = await client.post("/api/jobs", headers=AUTH, data=_form())
        job_id = created.json()["job_id"]
        await _wait_terminal(client, job_id)
        assert (await client.delete(f"/api/jobs/{job_id}", headers=AUTH)).status_code == 204
        # Job đã xong -> xoá luôn record, không để lại "succeeded" trỏ tới file
        # đã bị xoá.
        status = await client.get(f"/api/jobs/{job_id}", headers=AUTH)
        download = await client.get(f"/api/jobs/{job_id}/download", headers=AUTH)
    assert status.status_code == 404
    assert download.status_code == 404
    assert download.json()["error"]["code"] == "JOB_NOT_FOUND"


# --------------------------------------------------------------------------- #
# healthz
# --------------------------------------------------------------------------- #
async def test_healthz_counts_active_jobs(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _never_ending(*_args: Any, **_kwargs: Any) -> None:
        await asyncio.sleep(30)

    monkeypatch.setattr(main_mod, "run_job", _never_ending)
    async with await _client() as client:
        await client.post("/api/jobs", headers=AUTH, data=_form())
        payload = (await client.get("/healthz")).json()
    assert payload["active_jobs"] == 1


# --------------------------------------------------------------------------- #
# Range ngoài kích thước file -> 416 (RFC 7233), không được kẹp về byte cuối
# --------------------------------------------------------------------------- #
async def test_range_beyond_file_returns_416(fake_render: dict[str, Any]) -> None:
    size = len(b"MP4DATA" * 100)
    async with await _client() as client:
        created = await client.post("/api/jobs", headers=AUTH, data=_form())
        job_id = created.json()["job_id"]
        await _wait_terminal(client, job_id)
        response = await client.get(
            f"/api/jobs/{job_id}/download", headers={**AUTH, "Range": f"bytes={size}-"}
        )
    assert response.status_code == 416
    assert response.headers["content-range"] == f"bytes */{size}"
    assert response.json()["error"]["code"] == "RANGE_NOT_SATISFIABLE"


async def test_zero_length_suffix_range_returns_416(fake_render: dict[str, Any]) -> None:
    async with await _client() as client:
        created = await client.post("/api/jobs", headers=AUTH, data=_form())
        job_id = created.json()["job_id"]
        await _wait_terminal(client, job_id)
        response = await client.get(
            f"/api/jobs/{job_id}/download", headers={**AUTH, "Range": "bytes=-0"}
        )
    assert response.status_code == 416


async def test_resume_from_exact_offset_has_no_duplicate_byte(
    fake_render: dict[str, Any],
) -> None:
    """Kịch bản resume thật: tải 500 byte đầu rồi xin phần còn lại."""
    expected = b"MP4DATA" * 100
    async with await _client() as client:
        created = await client.post("/api/jobs", headers=AUTH, data=_form())
        job_id = created.json()["job_id"]
        await _wait_terminal(client, job_id)
        first = await client.get(
            f"/api/jobs/{job_id}/download", headers={**AUTH, "Range": "bytes=0-499"}
        )
        rest = await client.get(
            f"/api/jobs/{job_id}/download", headers={**AUTH, "Range": "bytes=500-"}
        )
    assert first.status_code == 206 and rest.status_code == 206
    assert first.content + rest.content == expected


# --------------------------------------------------------------------------- #
# /download mở công khai: link phải dùng được mà không gắn header Authorization
# (thẻ <video>, trình duyệt, người nhận link) — job_id 48 bit là thứ bảo vệ duy nhất
# --------------------------------------------------------------------------- #
async def test_download_needs_no_api_key(fake_render: dict[str, Any]) -> None:
    expected = b"MP4DATA" * 100
    async with await _client() as client:
        created = await client.post("/api/jobs", headers=AUTH, data=_form())
        job_id = created.json()["job_id"]
        await _wait_terminal(client, job_id)
        # KHÔNG truyền headers=AUTH
        response = await client.get(f"/api/jobs/{job_id}/download")
    assert response.status_code == 200
    assert response.content == expected
    assert response.headers["content-type"] == "video/mp4"


async def test_download_ignores_a_wrong_token(fake_render: dict[str, Any]) -> None:
    # Token sai cũng không được chặn: endpoint này không xét Authorization nữa.
    async with await _client() as client:
        created = await client.post("/api/jobs", headers=AUTH, data=_form())
        job_id = created.json()["job_id"]
        await _wait_terminal(client, job_id)
        response = await client.get(
            f"/api/jobs/{job_id}/download", headers={"Authorization": "Bearer sai-be-bet"}
        )
    assert response.status_code == 200


async def test_download_range_needs_no_api_key(fake_render: dict[str, Any]) -> None:
    expected = b"MP4DATA" * 100
    async with await _client() as client:
        created = await client.post("/api/jobs", headers=AUTH, data=_form())
        job_id = created.json()["job_id"]
        await _wait_terminal(client, job_id)
        response = await client.get(
            f"/api/jobs/{job_id}/download", headers={"Range": "bytes=10-19"}
        )
    assert response.status_code == 206
    assert response.content == expected[10:20]


async def test_unknown_job_download_is_404_not_401() -> None:
    # Không có auth nữa thì job lạ phải ra 404, không phải 401.
    async with await _client() as client:
        response = await client.get("/api/jobs/khongtontai/download")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "JOB_NOT_FOUND"


async def test_other_endpoints_still_require_api_key_after_opening_download() -> None:
    """Mở /download không được làm lỏng các endpoint còn lại."""
    async with await _client() as client:
        assert (await client.get("/api/jobs/abc")).status_code == 401
        assert (await client.delete("/api/jobs/abc")).status_code == 401
        assert (
            await client.post("/api/jobs", data=_form())
        ).status_code == 401


async def test_openapi_marks_download_as_public() -> None:
    async with await _client() as client:
        spec = (await client.get("/openapi.json")).json()
    download = spec["paths"]["/api/jobs/{job_id}/download"]["get"]
    assert "security" not in download, "Swagger vẫn coi /download là cần token"
    assert "security" in spec["paths"]["/api/jobs/{job_id}"]["get"]
