"""Test cho app/drive_folder.py — quét thư mục Drive lấy danh sách video."""

from __future__ import annotations

from typing import Any

import pytest

from app.drive import DriveFileMeta
from app.drive_folder import is_video, list_folder_videos, parse_folder_id
from app.utils import DriveDownloadFailed, InvalidDriveUrl

FOLDER_ID = "1AbCdEfGhIjKlMnOpQrStUvWxYz012345"


# --------------------------------------------------------------------------- #
# parse_folder_id
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "url",
    [
        f"https://drive.google.com/drive/folders/{FOLDER_ID}",
        f"https://drive.google.com/drive/folders/{FOLDER_ID}?usp=sharing",
        f"https://drive.google.com/drive/u/0/folders/{FOLDER_ID}",
        f"https://drive.google.com/open?id={FOLDER_ID}",
        FOLDER_ID,
    ],
)
def test_parse_folder_id_accepts_the_usual_link_shapes(url: str) -> None:
    assert parse_folder_id(url) == FOLDER_ID


@pytest.mark.parametrize(
    "url",
    [
        "",
        "   ",
        "https://example.com/thu-muc",
        "khong-phai-link",
    ],
)
def test_parse_folder_id_rejects_nonsense(url: str) -> None:
    with pytest.raises(InvalidDriveUrl):
        parse_folder_id(url)


def test_link_to_a_single_file_is_not_a_folder_link() -> None:
    # /file/d/<id> là MỘT file; nhận nhầm thành thư mục thì quét ra rỗng.
    with pytest.raises(InvalidDriveUrl):
        parse_folder_id(f"https://drive.google.com/file/d/{FOLDER_ID}/view")


# --------------------------------------------------------------------------- #
# is_video
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("name", "mime", "expected"),
    [
        ("a.mp4", "video/mp4", True),
        ("a.mov", "video/quicktime", True),
        # Drive hay gán octet-stream cho file tải lên từ máy -> phải xét đuôi.
        ("a.mp4", "application/octet-stream", True),
        ("a.MKV", "application/octet-stream", True),
        ("nhac.mp3", "audio/mpeg", False),
        ("ghi-chu.txt", "text/plain", False),
        ("khong-duoi", "application/octet-stream", False),
        ("thu-muc-con", "application/vnd.google-apps.folder", False),
        # Thư mục con đặt tên kết thúc bằng .mp4 vẫn không phải video.
        ("ky-la.mp4", "application/vnd.google-apps.folder", False),
    ],
)
def test_is_video(name: str, mime: str, expected: bool) -> None:
    assert is_video(name, mime) is expected


# --------------------------------------------------------------------------- #
# list_folder_videos
# --------------------------------------------------------------------------- #
class _FakeFiles:
    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self._pages = pages
        self.queries: list[dict[str, Any]] = []

    def list(self, **kwargs: Any) -> Any:
        self.queries.append(kwargs)
        page = self._pages[len(self.queries) - 1]
        return type("Req", (), {"execute": lambda _self: page})()


class _FakeService:
    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self._files = _FakeFiles(pages)

    def files(self) -> _FakeFiles:
        return self._files


def patch_service(monkeypatch: pytest.MonkeyPatch, pages: list[dict[str, Any]]) -> _FakeService:
    from app import drive_folder

    service = _FakeService(pages)
    monkeypatch.setattr(drive_folder, "get_drive_service", lambda: service)
    return service


async def test_lists_only_videos(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_service(
        monkeypatch,
        [
            {
                "files": [
                    {"id": "1", "name": "canh-rung.mp4", "size": "100", "mimeType": "video/mp4"},
                    {"id": "2", "name": "nhac.mp3", "size": "50", "mimeType": "audio/mpeg"},
                    {"id": "3", "name": "ho-boi.mov", "size": "200", "mimeType": "video/quicktime"},
                    {
                        "id": "4",
                        "name": "anh-bia",
                        "size": "10",
                        "mimeType": "application/vnd.google-apps.folder",
                    },
                ]
            }
        ],
    )
    files = await list_folder_videos(FOLDER_ID)
    assert [f.name for f in files] == ["canh-rung.mp4", "ho-boi.mov"]
    assert files[0].size_bytes == 100


async def test_query_scopes_to_the_folder_and_skips_trash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = patch_service(
        monkeypatch,
        [{"files": [{"id": "1", "name": "a.mp4", "size": "1", "mimeType": "video/mp4"}]}],
    )
    await list_folder_videos(FOLDER_ID)
    query = service.files().queries[0]
    assert query["q"] == f"'{FOLDER_ID}' in parents and trashed = false"
    # Thiếu hai cờ này là không đọc được thư mục nằm trên Shared Drive.
    assert query["supportsAllDrives"] is True
    assert query["includeItemsFromAllDrives"] is True
    # Sắp theo tên để số hiệu video luôn trỏ đúng một chỗ giữa các lần chạy.
    assert query["orderBy"] == "name_natural"


async def test_follows_pagination(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_service(
        monkeypatch,
        [
            {
                "files": [{"id": "1", "name": "a.mp4", "size": "1", "mimeType": "video/mp4"}],
                "nextPageToken": "trang-2",
            },
            {"files": [{"id": "2", "name": "b.mp4", "size": "1", "mimeType": "video/mp4"}]},
        ],
    )
    assert [f.name for f in await list_folder_videos(FOLDER_ID)] == ["a.mp4", "b.mp4"]


async def test_empty_folder_is_an_error_not_an_empty_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Trả list rỗng thì job chết ở tận bước ghép với thông báo tối nghĩa.
    patch_service(monkeypatch, [{"files": []}])
    with pytest.raises(DriveDownloadFailed, match="không có video"):
        await list_folder_videos(FOLDER_ID)


async def test_folder_with_only_non_videos_is_also_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_service(
        monkeypatch,
        [{"files": [{"id": "1", "name": "ghi-chu.txt", "size": "1", "mimeType": "text/plain"}]}],
    )
    with pytest.raises(DriveDownloadFailed):
        await list_folder_videos(FOLDER_ID)


async def test_api_error_is_wrapped_with_a_hint_about_sharing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import drive_folder

    def _boom() -> Any:
        raise RuntimeError("403 Forbidden")

    monkeypatch.setattr(drive_folder, "get_drive_service", _boom)
    with pytest.raises(DriveDownloadFailed) as caught:
        await list_folder_videos(FOLDER_ID)
    assert "403" in (caught.value.detail or "")


async def test_bad_size_field_does_not_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    # File Google Docs không có "size"; đừng để nó làm chết cả lượt quét.
    patch_service(
        monkeypatch,
        [{"files": [{"id": "1", "name": "a.mp4", "mimeType": "video/mp4"}]}],
    )
    assert (await list_folder_videos(FOLDER_ID))[0].size_bytes == 0


async def test_dead_socket_on_the_first_scan_is_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Job đầu tiên sau khi instance nằm im: socket keep-alive đã bị Google đóng.

    Đây là ca thật đã gặp trên Cloud Run — job trước chạy xong, 30 phút sau job
    kế tiếp fail ngay sau 27 ms với BrokenPipeError.
    """
    from app import drive_folder

    calls: list[int] = []

    def _flaky(_folder_id: str) -> Any:
        calls.append(1)
        if len(calls) == 1:
            raise BrokenPipeError(32, "Broken pipe")
        return [
            DriveFileMeta(file_id="1", name="a.mp4", size_bytes=1, mime_type="video/mp4")
        ]

    monkeypatch.setattr(drive_folder, "_list_blocking", _flaky)
    files = await list_folder_videos(FOLDER_ID)
    assert len(calls) == 2
    assert [item.name for item in files] == ["a.mp4"]
