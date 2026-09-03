"""Thư mục Drive nhận output: quy tắc chọn, và chặn sớm khi thiếu.

Đường upload chưa từng chạy trên production (30 ngày log không có dòng nào), nên
cái bẫy đắt nhất không phải lỗi code mà là phía Drive: service account KHÔNG có
quota Drive, đẩy file vào My Drive của người thật là 403 storageQuotaExceeded dù
đã share quyền ghi. Thư mục đích phải nằm trên Shared Drive.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.config import Settings
from app.intake import resolve_drive_output_folder
from app.models import RenderOptions
from app.utils import InvalidDriveUrl, InvalidOptions

FOLDER_ID = "137_mgDlICb0Hgm4oa1WBMHbf_iV7sv2Q"
OTHER_ID = "1AbCdEfGhIjKlMnOpQrStUvWxYz012345"


def _settings(folder: str = "") -> Settings:
    return Settings(
        API_KEY="k",
        DRIVE_OUTPUT_FOLDER_ID=folder,
        _env_file=None,  # type: ignore[call-arg]
    )


def _options(**delivery: Any) -> RenderOptions:
    return RenderOptions.model_validate({"delivery": delivery} if delivery else {})


# --------------------------------------------------------------------------- #
# resolve_drive_output_folder
# --------------------------------------------------------------------------- #
def test_explicit_false_beats_the_configured_folder() -> None:
    # Khai rõ false thì kể cả có env cũng không upload: giá trị trả về chính là
    # cờ quyết định. Nhờ vậy client cũ đang gửi false giữ nguyên hành vi.
    options = _options(upload_to_drive=False)
    assert resolve_drive_output_folder(options, _settings(FOLDER_ID)) is None


def test_unset_uploads_when_a_folder_is_configured() -> None:
    """Chưa khai + có env = đẩy lên Drive, không cần gửi option nào.

    Đây là điểm để bên gọi không phải sửa request khi service bật giao hàng qua
    Drive.
    """
    assert resolve_drive_output_folder(_options(), _settings(FOLDER_ID)) == FOLDER_ID


def test_unset_without_a_configured_folder_does_not_upload() -> None:
    # Chưa khai gì và cũng không cấu hình gì -> không upload, và KHÔNG báo lỗi:
    # người gọi chưa yêu cầu gì cả.
    assert resolve_drive_output_folder(_options(), _settings()) is None


def test_request_folder_is_used() -> None:
    options = _options(upload_to_drive=True, drive_folder_id=FOLDER_ID)
    assert resolve_drive_output_folder(options, _settings()) == FOLDER_ID


def test_env_folder_is_the_fallback() -> None:
    # Mục đích của env: khỏi phải gửi lại id trong từng request.
    options = _options(upload_to_drive=True)
    assert resolve_drive_output_folder(options, _settings(FOLDER_ID)) == FOLDER_ID


def test_request_folder_wins_over_env() -> None:
    options = _options(upload_to_drive=True, drive_folder_id=OTHER_ID)
    assert resolve_drive_output_folder(options, _settings(FOLDER_ID)) == OTHER_ID


def test_a_folder_link_is_accepted_and_normalised() -> None:
    # Dán nguyên link thư mục vào ô này là chuyện rất dễ xảy ra; để nguyên link
    # làm parents thì Drive trả 404 chứ không nói gì về nguyên nhân.
    options = _options(
        upload_to_drive=True,
        drive_folder_id=f"https://drive.google.com/drive/folders/{FOLDER_ID}?usp=sharing",
    )
    assert resolve_drive_output_folder(options, _settings()) == FOLDER_ID


def test_a_malformed_link_is_rejected() -> None:
    options = _options(upload_to_drive=True, drive_folder_id="https://example.com/thu-muc")
    with pytest.raises(InvalidDriveUrl):
        resolve_drive_output_folder(options, _settings())


@pytest.mark.parametrize("folder", ["0AKz9wCVfB2XyUk9PVA", "root", "folder-9"])
def test_short_ids_are_accepted_as_is(folder: str) -> None:
    """KHÔNG kiểm độ dài id trần.

    Id của chính một Shared Drive chỉ ~19 ký tự và ``root`` cũng là parent hợp
    lệ, nên siết theo độ dài (như parse_folder_id làm với link nguồn) sẽ chặn oan
    đúng những ca dùng thật. Id sai thì để Drive trả 404.
    """
    options = _options(upload_to_drive=True, drive_folder_id=folder)
    assert resolve_drive_output_folder(options, _settings()) == folder


def test_upload_without_any_folder_is_refused_upfront() -> None:
    """Thiếu thư mục là fail CHẮC CHẮN, nên phải chặn chứ không được thử.

    Không truyền ``parents`` thì file rơi vào My Drive của service account, mà
    service account không có quota nên Google trả 403 storageQuotaExceeded.
    """
    with pytest.raises(InvalidOptions) as caught:
        resolve_drive_output_folder(_options(upload_to_drive=True), _settings())
    detail = caught.value.detail or ""
    assert "DRIVE_OUTPUT_FOLDER_ID" in detail
    assert "Shared Drive" in detail
    assert "quota" in detail


def test_blank_env_folder_counts_as_missing() -> None:
    # Env đặt thành chuỗi trắng (rất dễ xảy ra khi CI truyền biến rỗng) phải
    # được coi như không có, chứ không được gửi " " làm parents.
    with pytest.raises(InvalidOptions):
        resolve_drive_output_folder(_options(upload_to_drive=True), _settings("   "))
