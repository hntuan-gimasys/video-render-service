"""Test cho app/models.py."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models import (
    JobStatus,
    RenderOptions,
    SubtitleOptions,
    TextEffect,
)


def test_empty_options_is_valid_and_matches_spec_defaults() -> None:
    opts = RenderOptions.model_validate({})
    assert opts.subtitle.enabled is True
    assert opts.subtitle.mode == "burn"
    assert opts.subtitle.font_name == "Liberation Serif"
    # None = tự co theo bề ngang video, giá trị thật do resolve_font_size tính.
    assert opts.subtitle.font_size is None
    assert opts.subtitle.font_size_ratio == 0.04
    assert opts.subtitle.primary_color == "#FFFFFF"
    assert opts.subtitle.outline_color == "#000000"
    assert opts.subtitle.back_color == "#80000000"
    assert opts.subtitle.border_style == 1
    assert opts.subtitle.outline is None
    assert opts.subtitle.shadow == 0
    assert opts.subtitle.alignment == 2
    # None = tự co theo CHIỀU CAO video (margin_v_ratio), giống font_size.
    assert opts.subtitle.margin_v is None
    assert opts.subtitle.margin_v_ratio == 0.14
    assert opts.subtitle.italic is True
    assert opts.subtitle.effect is TextEffect.FADE
    assert opts.intro.text is None
    assert opts.intro.effect is TextEffect.NONE
    assert opts.clips == []
    assert opts.subtitle.bold is True  # Bold serif giong caption phim/quang cao
    assert opts.subtitle.offset_seconds == 0.0

    assert opts.music.enabled is True
    assert opts.music.volume == 0.18
    # None = tự quyết: có nhạc nền thì bỏ tiếng gốc, không có thì giữ nguyên.
    assert opts.music.original_volume is None
    assert opts.music.loop is True
    assert (opts.music.fade_in, opts.music.fade_out) == (2.0, 3.0)
    assert opts.music.ducking is False
    assert opts.music.start_offset == 0.0

    assert opts.output.filename == "output.mp4"
    assert opts.output.video_codec == "libx264"
    assert opts.output.crf == 23
    assert opts.output.preset == "veryfast"
    assert opts.output.audio_codec == "aac"
    assert opts.output.audio_bitrate == "192k"
    assert opts.output.resolution is None
    assert opts.output.fps is None
    assert opts.output.faststart is True
    assert opts.output.copy_video_if_possible is True

    # None = tự quyết, cùng mẫu với music.original_volume ở trên: có
    # DRIVE_OUTPUT_FOLDER_ID lúc deploy thì đẩy output lên Drive, không có thì
    # thôi. Cố tình lệch bảng SPEC §7 (ghi mặc định là false) — mặc định cứng
    # false buộc MỌI request phải nhắc lại cấu hình của cả service, mà bên gọi
    # thì không nên biết service được deploy thế nào. Quan trọng: deploy không
    # đặt env đó thì hành vi y như trước, nên chỗ lệch này không tự kích hoạt.
    assert opts.delivery.upload_to_drive is None
    assert opts.delivery.drive_folder_id is None


def test_partial_options_keeps_other_defaults() -> None:
    opts = RenderOptions.model_validate({"subtitle": {"font_size": 28}, "music": {"volume": 0.15}})
    assert opts.subtitle.font_size == 28
    assert opts.subtitle.font_name == "Liberation Serif"
    assert opts.music.volume == 0.15
    assert opts.music.fade_out == 3.0


@pytest.mark.parametrize("crf", [-1, 52])
def test_crf_range(crf: int) -> None:
    with pytest.raises(ValidationError):
        RenderOptions.model_validate({"output": {"crf": crf}})


def test_crf_bounds_accepted() -> None:
    assert RenderOptions.model_validate({"output": {"crf": 0}}).output.crf == 0
    assert RenderOptions.model_validate({"output": {"crf": 51}}).output.crf == 51


@pytest.mark.parametrize("volume", [-0.1, 2.1])
def test_volume_range(volume: float) -> None:
    with pytest.raises(ValidationError):
        RenderOptions.model_validate({"music": {"volume": volume}})
    with pytest.raises(ValidationError):
        RenderOptions.model_validate({"music": {"original_volume": volume}})


def test_preset_whitelist() -> None:
    assert RenderOptions.model_validate({"output": {"preset": "medium"}}).output.preset == "medium"
    with pytest.raises(ValidationError, match="preset"):
        RenderOptions.model_validate({"output": {"preset": "turbo"}})


@pytest.mark.parametrize("resolution", ["1920x1080", "640x360"])
def test_resolution_accepted(resolution: str) -> None:
    assert RenderOptions.model_validate({"output": {"resolution": resolution}}).output.resolution


@pytest.mark.parametrize("resolution", ["1920*1080", "1920 x 1080", "hd", "1920x", "x1080"])
def test_resolution_rejected(resolution: str) -> None:
    with pytest.raises(ValidationError):
        RenderOptions.model_validate({"output": {"resolution": resolution}})


@pytest.mark.parametrize("alignment", [0, 10])
def test_alignment_range(alignment: int) -> None:
    with pytest.raises(ValidationError):
        SubtitleOptions(alignment=alignment)


def test_alignment_bounds_accepted() -> None:
    assert SubtitleOptions(alignment=1).alignment == 1
    assert SubtitleOptions(alignment=9).alignment == 9


@pytest.mark.parametrize("color", ["#FFF", "FFFFFF", "#GGGGGG", "#FFFFFFFFF"])
def test_color_pattern_rejected(color: str) -> None:
    with pytest.raises(ValidationError):
        SubtitleOptions(primary_color=color)


def test_eight_digit_color_accepted() -> None:
    assert SubtitleOptions(primary_color="#80FF8800").primary_color == "#80FF8800"


def test_unknown_field_is_rejected() -> None:
    # extra="forbid": gõ sai tên field phải báo lỗi thay vì bị bỏ qua âm thầm.
    with pytest.raises(ValidationError):
        RenderOptions.model_validate({"subtitle": {"fontsize": 30}})


@pytest.mark.parametrize("filename", ["../escape.mp4", "sub/dir.mp4", "", "-flag.mp4"])
def test_output_filename_rejects_paths(filename: str) -> None:
    with pytest.raises(ValidationError):
        RenderOptions.model_validate({"output": {"filename": filename}})


def test_audio_bitrate_pattern() -> None:
    assert RenderOptions.model_validate({"output": {"audio_bitrate": "128k"}})
    with pytest.raises(ValidationError):
        RenderOptions.model_validate({"output": {"audio_bitrate": "128"}})


def test_job_status_values_and_terminal_flag() -> None:
    assert [s.value for s in JobStatus] == [
        "queued",
        "downloading",
        "merging",
        "probing",
        "rendering",
        "uploading",
        "succeeded",
        "failed",
        "cancelled",
    ]
    assert JobStatus.SUCCEEDED.is_terminal
    assert JobStatus.FAILED.is_terminal
    assert JobStatus.CANCELLED.is_terminal
    assert not JobStatus.RENDERING.is_terminal
    assert not JobStatus.QUEUED.is_terminal
    # StrEnum -> serialise thẳng thành chuỗi trong JSON response.
    assert f"{JobStatus.QUEUED}" == "queued"
