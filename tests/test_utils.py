"""Test cho app/utils.py."""

from __future__ import annotations

import io
import json
import logging
from pathlib import Path

import pytest

from app.utils import (
    MB,
    AppError,
    InvalidSubtitle,
    JsonFormatter,
    bind_job,
    format_hms,
    free_space_mb,
    new_job_id,
    safe_rmtree,
    setup_logging,
)


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(0, "00:00:00"), (61.9, "00:01:01"), (3661.4, "01:01:01"), (-5, "00:00:00")],
)
def test_format_hms(seconds: float, expected: str) -> None:
    assert format_hms(seconds) == expected


def test_app_error_to_dict_omits_empty_detail() -> None:
    # Envelope lỗi (SPEC §3.6) chỉ có code/message; detail là tuỳ chọn.
    assert AppError("hỏng").to_dict() == {"code": "INTERNAL", "message": "hỏng"}
    assert AppError("hỏng", detail="vì abc").to_dict()["detail"] == "vì abc"


def test_app_error_subclass_keeps_its_own_code_and_status() -> None:
    error = InvalidSubtitle()
    assert (error.code, error.http_status) == ("INVALID_SRT", 422)
    # Ghi đè message mà vẫn giữ nguyên code/status của lớp.
    custom = InvalidSubtitle("File phụ đề rỗng")
    assert custom.code == "INVALID_SRT" and custom.message == "File phụ đề rỗng"


def test_new_job_id_is_short_and_unique() -> None:
    ids = {new_job_id() for _ in range(200)}
    assert len(ids) == 200
    assert all(len(job_id) == 12 for job_id in ids)


def test_mb_constant_is_a_real_mebibyte() -> None:
    assert MB == 1024 * 1024


def test_json_formatter_one_line_with_extras() -> None:
    record = logging.LogRecord(
        name="app.jobs",
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg="rendering %s",
        args=("clip",),
        exc_info=None,
    )
    record.job_id = "abc123"
    line = JsonFormatter().format(record)
    assert "\n" not in line
    payload = json.loads(line)
    assert payload["severity"] == "WARNING"
    assert payload["message"] == "rendering clip"
    assert payload["job_id"] == "abc123"
    assert payload["logger"] == "app.jobs"
    assert payload["timestamp"].endswith("+00:00")


def test_setup_logging_survives_non_utf8_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Log tiếng Việt không được làm handler chết trên stdout cp1252."""
    buffer = io.BytesIO()
    stream = io.TextIOWrapper(buffer, encoding="cp1252", errors="strict", write_through=True)
    monkeypatch.setattr("sys.stdout", stream)

    setup_logging("INFO")
    logging.getLogger("app.test").info("Đã huỷ job", extra={"job_id": "j1"})
    stream.flush()

    payload = json.loads(buffer.getvalue().decode("utf-8"))
    assert payload["message"] == "Đã huỷ job"
    assert payload["job_id"] == "j1"


def test_bind_job_merges_extra(caplog: pytest.LogCaptureFixture) -> None:
    logger = bind_job(logging.getLogger("app.test"), "job-1")
    with caplog.at_level(logging.INFO, logger="app.test"):
        logger.info("hi", extra={"stage": "probing"})
    record = caplog.records[-1]
    assert record.job_id == "job-1"
    assert record.stage == "probing"


def test_free_space_mb_walks_up_to_existing_parent(tmp_path: Path) -> None:
    assert free_space_mb(tmp_path / "does" / "not" / "exist") > 0


def test_safe_rmtree_is_idempotent(tmp_path: Path) -> None:
    target = tmp_path / "ws"
    (target / "sub").mkdir(parents=True)
    (target / "sub" / "f.bin").write_bytes(b"data")
    safe_rmtree(target)
    assert not target.exists()
    safe_rmtree(target)  # không raise khi đã biến mất
