"""Test cho app/drive.py — 5 dạng link ở SPEC §6 + supportsAllDrives."""

from __future__ import annotations

import itertools
import logging
import ssl
import threading
from pathlib import Path
from typing import Any

import pytest

from app import drive
from app.drive import DriveFileMeta, download_file, parse_drive_id, upload_file
from app.utils import DriveDownloadFailed, DriveUploadFailed, FileTooLarge, InvalidDriveUrl

FILE_ID = "1AbCdEfGhIjKlMnOpQrStUvWxYz012345"


# --------------------------------------------------------------------------- #
# parse_drive_id — cả 5 dạng SPEC §6 liệt kê
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "url",
    [
        f"https://drive.google.com/file/d/{FILE_ID}/view?usp=sharing",
        f"https://drive.google.com/open?id={FILE_ID}",
        f"https://drive.google.com/uc?id={FILE_ID}&export=download",
        f"https://docs.google.com/document/d/{FILE_ID}/edit#gid=0",
        FILE_ID,
    ],
)
def test_parse_drive_id_all_spec_forms(url: str) -> None:
    assert parse_drive_id(url) == FILE_ID


@pytest.mark.parametrize(
    "url",
    [
        f"https://drive.google.com/file/d/{FILE_ID}",
        f"  https://drive.google.com/file/d/{FILE_ID}/view  ",
        f"https://drive.google.com/drive/folders/{FILE_ID}",
        f"https://docs.google.com/spreadsheets/d/{FILE_ID}/edit",
        f"https://drive.google.com/uc?export=download&id={FILE_ID}",
    ],
)
def test_parse_drive_id_extra_forms(url: str) -> None:
    assert parse_drive_id(url) == FILE_ID


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "https://example.com/video.mp4",
        "not a url",
        "short-id",
        "https://drive.google.com/",
    ],
)
def test_parse_drive_id_rejects_invalid(bad: str) -> None:
    with pytest.raises(InvalidDriveUrl) as exc:
        parse_drive_id(bad)
    assert exc.value.code == "INVALID_DRIVE_URL"
    assert exc.value.http_status == 400


# --------------------------------------------------------------------------- #
# Drive API giả lập — kiểm tra supportsAllDrives được truyền
# --------------------------------------------------------------------------- #
class _FakeRequest:
    def __init__(self, calls: list[tuple[str, dict[str, Any]]], name: str, result: Any) -> None:
        self._calls = calls
        self._name = name
        self._result = result

    def execute(self) -> Any:
        return self._result


class _FakeFiles:
    def __init__(self, calls: list[tuple[str, dict[str, Any]]], meta: dict[str, Any]) -> None:
        self._calls = calls
        self._meta = meta

    def get(self, **kwargs: Any) -> _FakeRequest:
        self._calls.append(("get", kwargs))
        return _FakeRequest(self._calls, "get", self._meta)

    def get_media(self, **kwargs: Any) -> object:
        self._calls.append(("get_media", kwargs))
        return object()

    def create(self, **kwargs: Any) -> Any:
        self._calls.append(("create", kwargs))

        class _Resumable:
            def next_chunk(self) -> tuple[None, dict[str, str]]:
                return None, {
                    "id": "newfile123",
                    "name": "output.mp4",
                    "webViewLink": "https://drive.google.com/file/d/newfile123/view",
                }

        return _Resumable()


class _FakeService:
    def __init__(self, calls: list[tuple[str, dict[str, Any]]], meta: dict[str, Any]) -> None:
        self._files = _FakeFiles(calls, meta)

    def files(self) -> _FakeFiles:
        return self._files


@pytest.fixture
def drive_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    calls: list[tuple[str, dict[str, Any]]] = []
    meta = {"id": FILE_ID, "name": "video.mp4", "size": "1048576", "mimeType": "video/mp4"}
    monkeypatch.setattr(drive, "get_drive_service", lambda: _FakeService(calls, meta))
    return calls


async def test_download_file_passes_supports_all_drives(
    drive_calls: list[tuple[str, dict[str, Any]]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    written: list[float] = []

    def _fake_download(file_id: str, dest: Path, on_progress: Any) -> None:
        # Gọi lại đường thật của get_media để kiểm tra tham số truyền vào.
        drive.get_drive_service().files().get_media(
            fileId=file_id, supportsAllDrives=True
        )
        dest.write_bytes(b"video-bytes")
        if on_progress:
            on_progress(100.0)

    monkeypatch.setattr(drive, "_download_blocking", _fake_download)

    dest = tmp_path / "input.mp4"
    meta = await download_file(FILE_ID, dest, on_progress=written.append)

    assert meta == DriveFileMeta(
        file_id=FILE_ID, name="video.mp4", size_bytes=1048576, mime_type="video/mp4"
    )
    assert dest.read_bytes() == b"video-bytes"
    assert written == [100.0]

    get_kwargs = next(kwargs for name, kwargs in drive_calls if name == "get")
    assert get_kwargs["supportsAllDrives"] is True
    assert get_kwargs["fields"] == "id,name,size,mimeType"
    media_kwargs = next(kwargs for name, kwargs in drive_calls if name == "get_media")
    assert media_kwargs["supportsAllDrives"] is True


async def test_download_file_rejects_oversize_before_downloading(
    drive_calls: list[tuple[str, dict[str, Any]]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _must_not_run(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("không được tải khi file vượt giới hạn")

    monkeypatch.setattr(drive, "_download_blocking", _must_not_run)
    with pytest.raises(FileTooLarge):
        await download_file(FILE_ID, tmp_path / "input.mp4", max_bytes=1024)


async def test_download_file_wraps_api_error_and_cleans_partial(
    drive_calls: list[tuple[str, dict[str, Any]]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dest = tmp_path / "input.mp4"

    def _boom(file_id: str, target: Path, on_progress: Any) -> None:
        target.write_bytes(b"partial")
        raise RuntimeError("HttpError 404")

    monkeypatch.setattr(drive, "_download_blocking", _boom)
    with pytest.raises(DriveDownloadFailed) as exc:
        await download_file(FILE_ID, dest)
    assert exc.value.code == "DRIVE_DOWNLOAD_FAILED"
    assert exc.value.http_status == 502
    assert "HttpError 404" in (exc.value.detail or "")
    assert not dest.exists()  # file tải dở phải bị xoá, /tmp là RAM


async def test_download_file_wraps_metadata_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom() -> None:
        raise RuntimeError("insufficient permissions")

    monkeypatch.setattr(drive, "get_drive_service", _boom)
    with pytest.raises(DriveDownloadFailed):
        await download_file(FILE_ID, tmp_path / "input.mp4")


async def test_upload_file_passes_supports_all_drives(
    drive_calls: list[tuple[str, dict[str, Any]]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    src = tmp_path / "output.mp4"
    src.write_bytes(b"out")

    class _FakeMedia:
        def __init__(self, *_args: Any, **kwargs: Any) -> None:
            self.kwargs = kwargs

    import googleapiclient.http as gac_http

    monkeypatch.setattr(gac_http, "MediaFileUpload", _FakeMedia)

    result = await upload_file(src, folder_id="folder-1")

    assert result.file_id == "newfile123"
    assert result.web_view_link == "https://drive.google.com/file/d/newfile123/view"
    create_kwargs = next(kwargs for name, kwargs in drive_calls if name == "create")
    assert create_kwargs["supportsAllDrives"] is True
    assert create_kwargs["body"] == {"name": "output.mp4", "parents": ["folder-1"]}
    assert create_kwargs["fields"] == "id,name,webViewLink"


async def test_upload_file_without_folder_omits_parents(
    drive_calls: list[tuple[str, dict[str, Any]]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    src = tmp_path / "output.mp4"
    src.write_bytes(b"out")
    import googleapiclient.http as gac_http

    monkeypatch.setattr(gac_http, "MediaFileUpload", lambda *a, **k: object())
    await upload_file(src)
    create_kwargs = next(kwargs for name, kwargs in drive_calls if name == "create")
    assert "parents" not in create_kwargs["body"]


async def test_upload_file_wraps_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src = tmp_path / "output.mp4"
    src.write_bytes(b"out")

    def _boom(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("quota exceeded")

    monkeypatch.setattr(drive, "_upload_blocking", _boom)
    with pytest.raises(DriveUploadFailed) as exc:
        await upload_file(src, "folder-1")
    assert exc.value.http_status == 502
    assert "quota exceeded" in (exc.value.detail or "")


async def test_blocking_calls_run_in_thread(
    drive_calls: list[tuple[str, dict[str, Any]]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mọi call googleapiclient phải đi qua asyncio.to_thread (SPEC §6)."""
    import threading

    main_thread = threading.current_thread().name
    seen: list[str] = []

    def _record(file_id: str, dest: Path, on_progress: Any) -> None:
        seen.append(threading.current_thread().name)
        dest.write_bytes(b"x")

    monkeypatch.setattr(drive, "_download_blocking", _record)
    await download_file(FILE_ID, tmp_path / "input.mp4")
    assert seen and seen[0] != main_thread


# --------------------------------------------------------------------------- #
# Log ở mức INFO không được crash job — bug thật: extra={"name": ...} /
# extra={"filename": ...} đụng thuộc tính có sẵn của LogRecord, logging raise
# KeyError ngay tại logger.info(), job nhận INTERNAL dù Drive tải/upload OK.
#
# pytest mặc định chạy root logger ở WARNING nên logger.info() không thực sự
# gọi tới Logger.makeRecord() nếu không ép mức log — phải dùng
# caplog.at_level(logging.INFO, ...) để bài test này thật sự chạy qua nhánh
# code đã crash, nếu không test sẽ pass giả tạo như 2 test cũ từng pass.
# --------------------------------------------------------------------------- #
async def test_download_file_logs_at_info_without_crashing(
    drive_calls: list[tuple[str, dict[str, Any]]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def _fake_download(file_id: str, dest: Path, on_progress: Any) -> None:
        dest.write_bytes(b"video-bytes")

    monkeypatch.setattr(drive, "_download_blocking", _fake_download)

    with caplog.at_level(logging.INFO, logger="app.drive"):
        await download_file(FILE_ID, tmp_path / "input.mp4")

    assert any("Bắt đầu tải file Drive" in r.message for r in caplog.records)


async def test_upload_file_logs_at_info_without_crashing(
    drive_calls: list[tuple[str, dict[str, Any]]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    src = tmp_path / "output.mp4"
    src.write_bytes(b"out")

    class _FakeMedia:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

    import googleapiclient.http as gac_http

    monkeypatch.setattr(gac_http, "MediaFileUpload", _FakeMedia)

    with caplog.at_level(logging.INFO, logger="app.drive"):
        await upload_file(src, "folder-1")

    assert any("Bắt đầu upload lên Drive" in r.message for r in caplog.records)


def test_no_extra_key_collides_with_logrecord_attrs() -> None:
    """Chặn hồi quy tĩnh: quét toàn bộ extra={...} trong app/, không cho dùng
    lại tên thuộc tính có sẵn của LogRecord (name, filename, module, ...)."""
    import re
    from pathlib import Path as _Path

    reserved = set(logging.LogRecord("x", 0, "", 0, "", None, None).__dict__.keys())
    reserved |= {"message", "asctime"}
    pattern = re.compile(r"extra=\{([^}]*)\}")
    key_pattern = re.compile(r'"([a-zA-Z_]+)"\s*:')

    offenders: list[str] = []
    for path in _Path("app").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for match in pattern.finditer(text):
            keys = key_pattern.findall(match.group(1))
            bad = [k for k in keys if k in reserved]
            if bad:
                line_no = text[: match.start()].count("\n") + 1
                offenders.append(f"{path}:{line_no} -> {bad}")

    assert not offenders, "extra= dùng key đụng LogRecord:\n" + "\n".join(offenders)


# --------------------------------------------------------------------------- #
# Client Drive cache theo THREAD — googleapiclient/httplib2 không thread-safe
# --------------------------------------------------------------------------- #
def _fake_google(monkeypatch: pytest.MonkeyPatch) -> None:
    """Chặn google.auth + discovery.build để dựng client không cần mạng.

    Hai module này được import BÊN TRONG get_drive_service nên phải patch trên
    chính module, không patch được qua tên trong app.drive.
    """
    import google.auth
    from googleapiclient import discovery

    counter = itertools.count()
    monkeypatch.setattr(google.auth, "default", lambda scopes=None: (object(), "proj"))
    monkeypatch.setattr(
        discovery, "build", lambda *_a, **_kw: "service-%d" % next(counter)
    )


def test_each_thread_gets_its_own_drive_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hai thread phải nhận HAI client khác nhau, cùng thread thì tái dùng.

    Dùng chung một client giữa các thread là nguyên nhân container chết bằng
    SIGSEGV trên production (xem chú thích trong app/drive.py): httplib2 giữ một
    socket TLS không có khoá nào bảo vệ.
    """
    _fake_google(monkeypatch)
    got: dict[str, object] = {}

    def grab(name: str) -> None:
        drive.reset_drive_service()
        got[name] = drive.get_drive_service()
        got[name + "-lan2"] = drive.get_drive_service()

    threads = [threading.Thread(target=grab, args=(n,)) for n in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert got["a"] != got["b"], "hai thread đang dùng CHUNG một client"
    assert got["a"] == got["a-lan2"], "cùng thread phải tái dùng client, không dựng lại"
    assert got["b"] == got["b-lan2"]


def test_reset_only_touches_the_calling_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    """reset_drive_service không được chạm tới client của thread khác.

    Bỏ client mà thread khác đang dùng chính là thứ vừa gây SIGSEGV.
    """
    _fake_google(monkeypatch)
    drive.reset_drive_service()
    mine = drive.get_drive_service()
    seen: dict[str, object] = {}

    def other() -> None:
        seen["before"] = drive.get_drive_service()
        drive.reset_drive_service()
        seen["after"] = drive.get_drive_service()

    thread = threading.Thread(target=other)
    thread.start()
    thread.join()

    assert seen["before"] != seen["after"], "thread kia phải được client mới sau reset"
    assert drive.get_drive_service() == mine, "client của thread này bị bỏ oan"


# --------------------------------------------------------------------------- #
# call_drive — socket keep-alive chết vì không dùng một lúc
# --------------------------------------------------------------------------- #
class _ResetCounter:
    """Đứng thay reset_drive_service để đếm số lần client bị bỏ."""

    def __init__(self) -> None:
        self.count = 0

    def __call__(self) -> None:
        self.count += 1


async def test_call_drive_retries_in_the_same_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cả hai lần gọi phải ở CÙNG một thread.

    Client cache theo thread, nên nếu lần thử lại rơi sang thread khác thì việc
    bỏ client vừa rồi chẳng liên quan gì tới client sắp được dùng.
    """
    threads: list[int] = []

    def _flaky() -> str:
        threads.append(threading.get_ident())
        if len(threads) == 1:
            raise BrokenPipeError(32, "Broken pipe")
        return "ok"

    assert await drive.call_drive(_flaky) == "ok"
    assert len(threads) == 2
    assert len(set(threads)) == 1, "lần thử lại chạy ở thread khác"


async def test_call_drive_rebuilds_client_after_a_dead_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reset = _ResetCounter()
    monkeypatch.setattr(drive, "reset_drive_service", reset)
    calls: list[int] = []

    def _flaky() -> str:
        calls.append(1)
        if len(calls) == 1:
            raise BrokenPipeError(32, "Broken pipe")
        return "ok"

    assert await drive.call_drive(_flaky) == "ok"
    assert len(calls) == 2
    assert reset.count == 1, "phải bỏ client của thread đó trước khi thử lại"


async def test_call_drive_retries_a_dead_tls_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Socket TLS chết giữa đường ra SSLEOFError, không thuộc ConnectionError.
    reset = _ResetCounter()
    monkeypatch.setattr(drive, "reset_drive_service", reset)
    calls: list[int] = []

    def _flaky() -> str:
        calls.append(1)
        if len(calls) == 1:
            raise ssl.SSLEOFError("EOF occurred in violation of protocol")
        return "ok"

    assert await drive.call_drive(_flaky) == "ok"
    assert reset.count == 1


async def test_call_drive_does_not_retry_a_permission_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reset = _ResetCounter()
    monkeypatch.setattr(drive, "reset_drive_service", reset)
    calls: list[int] = []

    def _forbidden() -> str:
        calls.append(1)
        raise RuntimeError("403 Forbidden")

    with pytest.raises(RuntimeError):
        await drive.call_drive(_forbidden)
    assert len(calls) == 1, "lỗi quyền không được che bằng retry"
    assert reset.count == 0


async def test_call_drive_gives_up_after_one_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reset = _ResetCounter()
    monkeypatch.setattr(drive, "reset_drive_service", reset)
    calls: list[int] = []

    def _always_dead() -> str:
        calls.append(1)
        raise BrokenPipeError(32, "Broken pipe")

    with pytest.raises(BrokenPipeError):
        await drive.call_drive(_always_dead)
    assert len(calls) == 2, "chỉ thử lại đúng một lần"


async def test_upload_retries_a_dead_socket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Upload cũng phải retry — đo được trên production, không phải phòng xa.

    Job 1756ba62296e tải nguồn xong lúc 10:16:30.695 mà không cần retry lần nào,
    rồi upload chết lúc 10:16:33.172 bằng BrokenPipeError, chỉ 3 giây sau. Lần ghi
    đầu của resumable upload vào keep-alive Google vừa đóng là đủ để EPIPE.
    """
    src = tmp_path / "out.mp4"
    src.write_bytes(b"x")
    calls: list[int] = []

    class _Result:
        file_id = "id-1"
        name = "out.mp4"
        web_view_link = "https://drive.google.com/file/d/id-1/view"

    def _flaky(*_args: Any, **_kwargs: Any) -> Any:
        calls.append(1)
        if len(calls) == 1:
            raise BrokenPipeError(32, "Broken pipe")
        return _Result()

    monkeypatch.setattr(drive, "_upload_blocking", _flaky)
    result = await upload_file(src)
    assert len(calls) == 2
    assert result.file_id == "id-1"


async def test_upload_gives_up_after_one_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "out.mp4"
    src.write_bytes(b"x")
    calls: list[int] = []

    def _dead(*_args: Any, **_kwargs: Any) -> Any:
        calls.append(1)
        raise BrokenPipeError(32, "Broken pipe")

    monkeypatch.setattr(drive, "_upload_blocking", _dead)
    with pytest.raises(DriveUploadFailed):
        await upload_file(src)
    assert len(calls) == 2
