"""Nạp kịch bản dựng của pipeline khác: clips trỏ video bằng TÊN FILE.

Pipeline sinh nội dung chỉ biết tên file nó đã xem, không biết ta sẽ nhận các
video theo thứ tự nào — nên ``clips[].source`` phải nhận cả tên file, và
``video_edit_script`` phải dán thẳng vào được.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from app.clips import (
    SourceVideo,
    resolve_clips,
    resolve_source_index,
    source_labels,
)
from app.intake import apply_form_overrides, parse_clips_field, parse_edit_script
from app.models import ClipSpec, RenderOptions
from app.probe_data import ProbeResult
from app.utils import InvalidOptions

WAKE = "FSave.com_Facebook_Wake-up-to-birdsong_Media_1344428744539226_001_1080p.mp4"
FLAVORS = "FSave.com_Reels_Flavors-drawn-from-the-land_Media_1286880296972124_001_720p.mp4"

PREVIOUS_OUTPUT = {
    "video_srt": "1\n00:00:00,000 --> 00:00:05,000\nRoi xa on ao do thi\n",
    "video_edit_script": [
        {"source_video": WAKE, "start": "00:00", "end": "00:04"},
        {"source_video": FLAVORS, "start": "00:02", "end": "00:06"},
        {"source_video": WAKE, "start": "00:11", "end": "00:12"},
    ],
}


def source(name: str, label: str, duration: float = 60.0) -> SourceVideo:
    return SourceVideo(
        name=name,
        probe=ProbeResult(
            duration=duration,
            width=1080,
            height=1920,
            fps=30.0,
            has_audio=True,
            has_video=True,
            video_codec="h264",
        ),
        label=label,
    )


def two_sources() -> list[SourceVideo]:
    return [source("src1.mp4", WAKE), source("src2.mp4", FLAVORS)]


def two_labels() -> list[str]:
    return source_labels(two_sources())


# --------------------------------------------------------------------------- #
# ClipSpec nhận tên file
# --------------------------------------------------------------------------- #
def test_source_video_is_accepted_as_alias_of_source() -> None:
    spec = ClipSpec.model_validate({"source_video": WAKE, "start": "00:02", "end": "00:06"})
    assert spec.source == WAKE
    assert (spec.start, spec.end) == (2.0, 6.0)


def test_numeric_string_source_is_still_an_index() -> None:
    # "2" là số hiệu video chứ không phải tên file tên là "2".
    assert ClipSpec.model_validate({"source": "2"}).source == 2


def test_index_source_still_range_checked() -> None:
    with pytest.raises(ValueError):
        ClipSpec.model_validate({"source": 0})


def test_empty_source_name_rejected() -> None:
    with pytest.raises(ValueError):
        ClipSpec.model_validate({"source_video": "   "})


def test_extra_pipeline_annotations_are_ignored_not_rejected() -> None:
    """Kịch bản của pipeline AI khác hay đính kèm ghi chú riêng trên mỗi đoạn
    (đã gặp thật: ``vibe_note`` mô tả ý đồ dựng cảnh). Field đó vô hại — khác
    với các options người dùng tự gõ tay, field lạ ở đây nhiều khả năng là chú
    thích của pipeline khác chứ không phải lỗi chính tả, nên không được chặn.
    """
    spec = ClipSpec.model_validate(
        {
            "source_video": "a.mp4",
            "start": "00:28",
            "end": "00:31",
            "vibe_note": "HOOK - Cảnh toàn khu nghỉ dưỡng ẩn mình giữa ngàn mây",
        }
    )
    assert (spec.source, spec.start, spec.end) == ("a.mp4", 28.0, 31.0)
    assert not hasattr(spec, "vibe_note")


# --------------------------------------------------------------------------- #
# Khớp tên file
# --------------------------------------------------------------------------- #
def test_exact_filename_match() -> None:
    assert resolve_source_index(FLAVORS, two_labels(), 1) == 1


def test_match_ignores_letter_case() -> None:
    assert resolve_source_index(FLAVORS.upper(), two_labels(), 1) == 1


def test_match_ignores_leading_directories() -> None:
    # Kịch bản của pipeline khác hay kèm cả đường dẫn nơi nó đọc file.
    assert resolve_source_index(f"/data/videos/{WAKE}", two_labels(), 1) == 0
    assert resolve_source_index(f"C:\\media\\{WAKE}", two_labels(), 1) == 0


def test_match_falls_back_to_substring() -> None:
    assert resolve_source_index("Flavors-drawn-from-the-land", two_labels(), 1) == 1


def test_ambiguous_name_is_rejected_instead_of_guessing() -> None:
    # "FSave.com" khớp cả hai -> đoán bừa là ghép nhầm cảnh, thà báo lỗi.
    with pytest.raises(InvalidOptions, match="nhiều video"):
        resolve_source_index("FSave.com", two_labels(), 3)


def test_unknown_name_lists_what_was_uploaded() -> None:
    with pytest.raises(InvalidOptions) as caught:
        resolve_source_index("khong-co-that.mp4", two_labels(), 2)
    assert WAKE in (caught.value.detail or "")


def test_index_out_of_range_still_reported_clearly() -> None:
    with pytest.raises(InvalidOptions, match="video số 5"):
        resolve_source_index(5, two_labels(), 1)


def test_name_matching_falls_back_to_workspace_name_without_label() -> None:
    # Nguồn không có nhãn (test cũ dựng JobSources tay) vẫn khớp theo tên file.
    plain = [SourceVideo("src1.mp4", two_sources()[0].probe)]
    assert resolve_source_index("src1.mp4", source_labels(plain), 1) == 0


# --------------------------------------------------------------------------- #
# resolve_clips với kịch bản đầy đủ
# --------------------------------------------------------------------------- #
def test_whole_edit_script_resolves_in_order() -> None:
    specs = [ClipSpec.model_validate(item) for item in PREVIOUS_OUTPUT["video_edit_script"]]
    clips = resolve_clips(specs, two_sources())
    assert [(c.name, c.start, c.duration) for c in clips] == [
        ("src1.mp4", 0.0, 4.0),
        ("src2.mp4", 2.0, 4.0),
        ("src1.mp4", 11.0, 1.0),
    ]


def test_script_may_mix_names_and_indexes() -> None:
    specs = [
        ClipSpec.model_validate({"source_video": FLAVORS, "start": 0, "end": 1}),
        ClipSpec.model_validate({"source": 1, "start": 0, "end": 1}),
    ]
    assert [c.name for c in resolve_clips(specs, two_sources())] == ["src2.mp4", "src1.mp4"]


# --------------------------------------------------------------------------- #
# parse_edit_script
# --------------------------------------------------------------------------- #
def test_parse_accepts_bare_array() -> None:
    specs, srt = parse_edit_script(json.dumps(PREVIOUS_OUTPUT["video_edit_script"]))
    assert len(specs) == 3
    assert srt is None


def test_parse_accepts_output_object_with_srt() -> None:
    specs, srt = parse_edit_script(json.dumps(PREVIOUS_OUTPUT))
    assert len(specs) == 3
    assert srt is not None and "Roi xa on ao do thi" in srt


def test_parse_accepts_whole_previous_response() -> None:
    # Dán nguyên response của bước trước, khỏi phải bóc lấy phần output.
    payload = {"job_id": "abc", "status": "succeeded", "output": PREVIOUS_OUTPUT}
    specs, srt = parse_edit_script(json.dumps(payload))
    assert len(specs) == 3 and srt is not None


def test_parse_empty_input_is_not_an_error() -> None:
    assert parse_edit_script(None) == ([], None)
    assert parse_edit_script("   ") == ([], None)


@pytest.mark.parametrize(
    "raw", ["khong-phai-json", "{}", '{"video_edit_script": "chuoi"}', "[1, 2]"]
)
def test_parse_rejects_bad_shapes(raw: str) -> None:
    with pytest.raises(InvalidOptions):
        parse_edit_script(raw)


# --------------------------------------------------------------------------- #
# Gộp vào options
# --------------------------------------------------------------------------- #
def test_edit_script_fills_clips_and_subtitles() -> None:
    options, srt = apply_form_overrides(
        RenderOptions(), None, json.dumps(PREVIOUS_OUTPUT)
    )
    assert [spec.source for spec in options.clips] == [WAKE, FLAVORS, WAKE]
    assert srt is not None


def test_options_json_still_wins_over_edit_script() -> None:
    options = RenderOptions.model_validate({"clips": [{"source": 1, "start": 0, "end": 1}]})
    merged, _srt = apply_form_overrides(options, None, json.dumps(PREVIOUS_OUTPUT))
    assert [spec.source for spec in merged.clips] == [1]


def test_clips_field_picks_the_syntax_from_the_content() -> None:
    # Một ô cho cả hai cú pháp: JSON thì là kịch bản, còn lại là cú pháp gọn.
    specs, srt = parse_clips_field(json.dumps(PREVIOUS_OUTPUT))
    assert [spec.source for spec in specs] == [WAKE, FLAVORS, WAKE]
    assert srt is not None

    specs, srt = parse_clips_field("1 0-2; 2 0-3")
    assert [spec.source for spec in specs] == [1, 2]
    assert srt is None


# --------------------------------------------------------------------------- #
# start_seconds/end_seconds: mốc chính xác của pipeline, thắng start/end
# --------------------------------------------------------------------------- #
def test_start_seconds_wins_over_the_rounded_start() -> None:
    """Pipeline phát ra cả hai: start/end làm tròn giây chẵn, *_seconds chính xác.

    Lấy bản làm tròn là cắt lệch tới hơn nửa giây so với ý đồ dựng.
    """
    spec = ClipSpec.model_validate(
        {
            "source_video": WAKE,
            "start": "00:23",
            "end": "00:26",
            "start_seconds": 23.61,
            "end_seconds": 26.52,
            "vibe_note": "POINT 1",
        }
    )
    assert (spec.start, spec.end) == (23.61, 26.52)


def test_seconds_fields_work_without_start_and_end() -> None:
    spec = ClipSpec.model_validate(
        {"source_video": WAKE, "start_seconds": 0.5, "end_seconds": 2.5}
    )
    assert (spec.start, spec.end) == (0.5, 2.5)


def test_null_seconds_falls_back_to_start_and_end() -> None:
    # Pipeline không tính được mốc chính xác thì vẫn phải dùng được bản thô.
    spec = ClipSpec.model_validate(
        {
            "source_video": WAKE,
            "start": "00:04",
            "end": "00:06",
            "start_seconds": None,
            "end_seconds": None,
        }
    )
    assert (spec.start, spec.end) == (4.0, 6.0)


def test_seconds_fields_accept_string_form_too() -> None:
    spec = ClipSpec.model_validate(
        {"source_video": WAKE, "start_seconds": "00:04.25", "end_seconds": "6.5"}
    )
    assert (spec.start, spec.end) == (4.25, 6.5)


def test_bad_seconds_field_is_reported_not_ignored() -> None:
    with pytest.raises(ValidationError):
        ClipSpec.model_validate(
            {"source_video": WAKE, "start_seconds": "khong-phai-so", "end": "00:06"}
        )


def test_source_video_still_defers_to_an_explicit_source() -> None:
    # Đối xứng NGƯỢC với *_seconds, và phải giữ nguyên hành vi cũ.
    spec = ClipSpec.model_validate(
        {"source": 2, "source_video": WAKE, "start": 0, "end": 1}
    )
    assert spec.source == 2


def test_full_pipeline_payload_with_seconds_fields() -> None:
    """Dán nguyên mảng của pipeline, gồm cả start/end thô lẫn *_seconds."""
    payload = json.dumps(
        [
            {
                "source_video": WAKE,
                "start": "00:00",
                "end": "00:02",
                "start_seconds": 0.5,
                "end_seconds": 2.5,
                "vibe_note": "HOOK",
            },
            {
                "source_video": FLAVORS,
                "start": "00:17",
                "end": "00:22",
                "start_seconds": 17.5,
                "end_seconds": 22.5,
                "vibe_note": "CTA",
            },
        ]
    )
    specs, srt = parse_clips_field(payload)
    assert srt is None
    assert [(s.start, s.end) for s in specs] == [(0.5, 2.5), (17.5, 22.5)]
