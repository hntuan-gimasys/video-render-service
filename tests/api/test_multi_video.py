"""API: quét thư mục Drive, cắt & ghép theo kịch bản, text bìa.

Điểm quan trọng nhất được chốt ở đây: chỉ TẢI những video mà kịch bản thật sự
dùng tới. Thư mục 20 video mà kịch bản dùng 4 thì tải cả 20 vừa chậm vừa dễ hết
RAM (``/tmp`` trên Cloud Run là tmpfs).
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from tests.helpers import AUTH
from tests.helpers import api_client as _client
from tests.helpers import job_form as _form
from tests.helpers import run_job_to_end as _run_job

WAKE = "FSave.com_Facebook_Wake-up-to-birdsong_Media_1344428744539226_001_1080p.mp4"
FLAVORS = "FSave.com_Reels_Flavors-drawn-from-the-land_Media_1286880296972124_001_720p.mp4"
CONCEALED = "FSave.com_Facebook_Concealed-behind-layers_Media_2095031028023211_001_720p.mp4"

# Mốc thời gian nằm gọn trong 5s — đúng độ dài mà probe giả (PROBE_OK) báo.
EDIT_SCRIPT = [
    {"source_video": WAKE, "start": "00:00", "end": "00:04"},
    {"source_video": CONCEALED, "start": "00:00", "end": "00:01"},
    {"source_video": WAKE, "start": "00:04", "end": "00:05"},
]


def real_folder(fake_drive: dict[str, Any]) -> None:
    """Bày thư mục có 4 video tên thật, trong đó 1 video kịch bản không dùng."""
    fake_drive["names"] = [CONCEALED, FLAVORS, WAKE, "khong-dung-toi.mp4"]


def assert_merged_clip_count(merge_cmd: str, count: int) -> None:
    """Có đúng ``count`` đoạn được ghép, bất kể ghép bằng concat hay xfade.

    Chuyển cảnh (xfade/acrossfade) là mặc định (xem tests/test_transitions.py
    cho phần kiểm chi tiết), nên các test API ở đây chỉ cần biết SỐ ĐOẠN đã
    ghép, không quan tâm ghép bằng filter nào.
    """
    if count <= 1:
        assert f"concat=n={count}:v=1:a=1" in merge_cmd
        return
    assert merge_cmd.count("xfade=transition=") == count - 1
    assert merge_cmd.count("acrossfade=") == count - 1


# --------------------------------------------------------------------------- #
# Chỉ tải video thật sự dùng tới
# --------------------------------------------------------------------------- #
async def test_only_the_videos_used_by_the_script_are_downloaded(
    fake_render: dict[str, Any], fake_drive: dict[str, Any]
) -> None:
    real_folder(fake_drive)
    async with await _client() as client:
        final = await _run_job(client, clips=json.dumps(EDIT_SCRIPT))
    assert final["status"] == "succeeded", final.get("error")

    # FLAVORS và "khong-dung-toi" không có trong kịch bản -> không tải.
    assert sorted(fake_drive["downloaded"]) == sorted([CONCEALED, WAKE])
    # Ba đoạn, đúng thứ tự đã liệt kê (WAKE, CONCEALED, WAKE).
    merge_cmd = " ".join(fake_render["merge_cmd"])
    assert_merged_clip_count(merge_cmd, 3)


async def test_clips_keep_declared_order_not_folder_order(
    fake_render: dict[str, Any], fake_drive: dict[str, Any]
) -> None:
    real_folder(fake_drive)
    script = [
        {"source_video": FLAVORS, "start": 0, "end": 1},
        {"source_video": CONCEALED, "start": 0, "end": 1},
    ]
    async with await _client() as client:
        final = await _run_job(client, clips=json.dumps(script))
    assert final["status"] == "succeeded", final.get("error")

    merge_cmd = " ".join(fake_render["merge_cmd"])
    # src1/src2 được đánh theo thứ tự TẢI (thứ tự thư mục), còn thứ tự GHÉP do
    # kịch bản quyết định -> đoạn của FLAVORS phải xuất hiện trước.
    flavors_slot = fake_drive["downloaded"].index(FLAVORS) + 1
    concealed_slot = fake_drive["downloaded"].index(CONCEALED) + 1
    assert merge_cmd.index(f"src{flavors_slot}.mp4") < merge_cmd.index(
        f"src{concealed_slot}.mp4"
    )


async def test_whole_previous_pipeline_response_can_be_pasted(
    fake_render: dict[str, Any], fake_drive: dict[str, Any]
) -> None:
    real_folder(fake_drive)
    payload = {
        "job_id": "abc",
        "status": "succeeded",
        "output": {
            "video_edit_script": EDIT_SCRIPT,
            "video_srt": "1\n00:00:00,000 --> 00:00:04,000\nRoi xa on ao do thi\n",
        },
    }
    async with await _client() as client:
        final = await _run_job(client, clips=json.dumps(payload))
    assert final["status"] == "succeeded", final.get("error")
    assert "subtitles=styled.ass" in " ".join(fake_render["cmd"])


async def test_compact_clip_syntax_still_works(
    fake_render: dict[str, Any], fake_drive: dict[str, Any]
) -> None:
    # Ô clips nhận cả cú pháp gọn theo SỐ HIỆU video (thứ tự tên file trong thư mục).
    async with await _client() as client:
        final = await _run_job(client, clips="1 00:00-00:02; 3 0:01-0:03")
    assert final["status"] == "succeeded", final.get("error")
    assert sorted(fake_drive["downloaded"]) == ["c1.mp4", "c3.mp4"]
    assert_merged_clip_count(" ".join(fake_render["merge_cmd"]), 2)


async def test_single_clip_from_one_video_still_merges(
    fake_render: dict[str, Any], fake_drive: dict[str, Any]
) -> None:
    # Cắt một đoạn từ một video vẫn phải qua bước ghép, nếu không thì không cắt.
    async with await _client() as client:
        final = await _run_job(client, clips="2 0-3")
    assert final["status"] == "succeeded", final.get("error")
    assert fake_drive["downloaded"] == ["c2.mp4"]
    assert_merged_clip_count(" ".join(fake_render["merge_cmd"]), 1)


async def test_options_json_wins_over_the_clips_field(
    fake_render: dict[str, Any], fake_drive: dict[str, Any]
) -> None:
    options = json.dumps({"clips": [{"source": 1, "start": 0, "end": 1}]})
    async with await _client() as client:
        await _run_job(client, options=options, clips="1 0-1; 2 0-1; 3 0-1")
    assert_merged_clip_count(" ".join(fake_render["merge_cmd"]), 1)


# --------------------------------------------------------------------------- #
# Lỗi khai đoạn
# --------------------------------------------------------------------------- #
async def test_unknown_filename_fails_the_job_and_lists_what_is_there(
    fake_render: dict[str, Any], fake_drive: dict[str, Any]
) -> None:
    """Tên file chỉ đối chiếu được sau khi quét thư mục, nên đây là lỗi của job.

    Nhưng phải xảy ra TRƯỚC khi tải byte nào — sai tên mà tải xong vài GB rồi
    mới báo là phí.
    """
    real_folder(fake_drive)
    script = [{"source_video": "khong-co-that.mp4", "start": 0, "end": 1}]
    async with await _client() as client:
        final = await _run_job(client, clips=json.dumps(script))
    assert final["status"] == "failed"
    assert final["error"]["code"] == "INVALID_OPTIONS"
    assert "khong-co-that.mp4" in final["error"]["message"]
    assert WAKE in (final["error"]["detail"] or "")
    assert fake_drive["downloaded"] == []


async def test_ambiguous_filename_is_rejected_instead_of_guessing(
    fake_render: dict[str, Any], fake_drive: dict[str, Any]
) -> None:
    real_folder(fake_drive)
    # "FSave.com" khớp cả ba video -> đoán bừa là ghép nhầm cảnh.
    script = [{"source_video": "FSave.com", "start": 0, "end": 1}]
    async with await _client() as client:
        final = await _run_job(client, clips=json.dumps(script))
    assert final["status"] == "failed"
    assert "nhiều video" in final["error"]["message"]


async def test_clip_number_past_the_end_of_the_folder_fails(
    fake_render: dict[str, Any], fake_drive: dict[str, Any]
) -> None:
    async with await _client() as client:
        final = await _run_job(client, clips="9 0-1")
    assert final["status"] == "failed"
    assert final["error"]["code"] == "INVALID_OPTIONS"
    assert "video số 9" in final["error"]["message"]


async def test_unparseable_clip_line_is_rejected_immediately() -> None:
    async with await _client() as client:
        response = await client.post(
            "/api/jobs", headers=AUTH, data=_form(clips="lay doan dau")
        )
    # Cú pháp sai thì biết ngay, không cần quét thư mục.
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_OPTIONS"


async def test_clips_json_that_is_not_a_script_is_rejected_immediately() -> None:
    async with await _client() as client:
        response = await client.post("/api/jobs", headers=AUTH, data=_form(clips="{}"))
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_OPTIONS"


async def test_too_many_videos_is_refused_before_downloading(
    fake_render: dict[str, Any],
    fake_drive: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config import get_settings

    monkeypatch.setenv("MAX_FOLDER_VIDEOS", "2")
    get_settings.cache_clear()
    fake_drive["names"] = [f"v{index}.mp4" for index in range(5)]
    async with await _client() as client:
        final = await _run_job(client)
    assert final["status"] == "failed"
    assert final["error"]["code"] == "INVALID_OPTIONS"
    assert fake_drive["downloaded"] == []


# --------------------------------------------------------------------------- #
# Text bìa & hiệu ứng
# --------------------------------------------------------------------------- #
async def test_intro_text_form_field_is_burned(fake_render: dict[str, Any]) -> None:
    async with await _client() as client:
        final = await _run_job(
            client, intro_text="2tr9/nguoi|3N2D tai GARRYA MU CANG CHAI", clips="1 0-2"
        )
    assert final["status"] == "succeeded", final.get("error")
    # Không có phụ đề nào -> text bìa đi thành một file .ass riêng.
    assert "subtitles=intro.ass" in " ".join(fake_render["cmd"])


async def test_intro_text_and_subtitles_share_one_ass_file(
    fake_render: dict[str, Any],
) -> None:
    srt = "1\n00:00:01,000 --> 00:00:03,000\nMoi villa 1 trai nghiem\n"
    async with await _client() as client:
        final = await _run_job(
            client, intro_text="2tr9/nguoi", srt_text=srt, clips="1 0-2"
        )
    assert final["status"] == "succeeded", final.get("error")
    argv = " ".join(fake_render["cmd"])
    assert "subtitles=styled.ass" in argv
    assert "intro.ass" not in argv  # gộp chung, không cần filter thứ hai


async def test_intro_text_alone_still_forces_a_re_encode(
    fake_render: dict[str, Any],
) -> None:
    """Chữ vẽ đè lên hình thì không thể dùng đường copy stream nữa."""
    async with await _client() as client:
        final = await _run_job(client, intro_text="Xem ngay", clips="1 0-2")
    assert final["status"] == "succeeded", final.get("error")
    argv = " ".join(fake_render["cmd"])
    assert "-c:v copy" not in argv
    assert "libx264" in argv


async def test_effect_choice_reaches_the_render(fake_render: dict[str, Any]) -> None:
    srt = "1\n00:00:01,000 --> 00:00:03,000\nMoi villa 1 trai nghiem\n"
    async with await _client() as client:
        final = await _run_job(
            client,
            srt_text=srt,
            options=json.dumps({"subtitle": {"effect": "typewriter"}}),
        )
    assert final["status"] == "succeeded", final.get("error")
    assert "subtitles=styled.ass" in " ".join(fake_render["cmd"])


async def test_unknown_effect_is_rejected() -> None:
    async with await _client() as client:
        response = await client.post(
            "/api/jobs",
            headers=AUTH,
            data=_form(options=json.dumps({"subtitle": {"effect": "nhay-mua"}})),
        )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_OPTIONS"
