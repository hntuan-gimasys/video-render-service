"""API: phụ đề gửi bằng ô ``srt_text`` (đoạn AI sinh ra).

Tách khỏi tests/api/test_endpoints.py để mỗi file test dưới 400 dòng.
"""

from __future__ import annotations

import json
from typing import Any

from tests.helpers import api_client as _client
from tests.helpers import run_job_to_end as _run_job

AI_SRT = (
    "1\n00:00:01,000 --> 00:00:03,000\nXin chào các bạn\n\n"
    "2\n00:00:03,500 --> 00:00:05,000\nDòng thứ hai\n"
)


async def test_srt_text_is_burned(fake_render: dict[str, Any]) -> None:
    async with await _client() as client:
        final = await _run_job(client, srt_text=AI_SRT)
    assert final["status"] == "succeeded", final.get("error")
    assert "subtitles=styled.ass" in " ".join(fake_render["cmd"])


async def test_srt_text_with_markdown_fence_from_ai(fake_render: dict[str, Any]) -> None:
    # AI hầu như luôn bọc output trong ```srt ... ``` khi người dùng copy.
    async with await _client() as client:
        final = await _run_job(client, srt_text=f"```srt\n{AI_SRT}```")
    assert final["status"] == "succeeded", final.get("error")


async def test_srt_text_accepts_ass_content(fake_render: dict[str, Any]) -> None:
    ass = (
        "[Script Info]\n[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        "Dialogue: 0,0:00:01.00,0:00:03.00,Default,,0,0,0,,Xin chào\n"
    )
    async with await _client() as client:
        final = await _run_job(client, srt_text=ass)
    assert final["status"] == "succeeded", final.get("error")
    assert "subtitles=subs.ass" in " ".join(fake_render["cmd"])


async def test_backslash_n_pasted_from_json_becomes_real_line_breaks(
    fake_render: dict[str, Any],
) -> None:
    r"""``video_srt`` trong JSON là chuỗi dài chứa ``\n``.

    Copy giá trị đó ra khỏi JSON rồi dán vào ô một dòng thì những ``\n`` còn
    nguyên dạng hai ký tự — phải đổi lại thành dấu xuống dòng thật, nếu không
    cả file phụ đề nằm gọn trên một dòng và không có block nào hợp lệ.
    """
    pasted = AI_SRT.replace("\n", "\\n")
    assert "\\n" in pasted and "\n" not in pasted
    async with await _client() as client:
        final = await _run_job(client, srt_text=pasted)
    assert final["status"] == "succeeded", final.get("error")
    assert "subtitles=styled.ass" in " ".join(fake_render["cmd"])


async def test_ass_content_keeps_its_backslash_n_tags(fake_render: dict[str, Any]) -> None:
    r"""Trong ASS, ``\n`` là tag ngắt dòng của libass nằm GIỮA dòng Dialogue.

    Đổi nó thành newline thật là tách đôi dòng Dialogue, hỏng cấu trúc file.
    """
    ass = (
        "[Script Info]\n[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        "Dialogue: 0,0:00:01.00,0:00:03.00,Default,,0,0,0,,Dòng một\\nDòng hai\n"
    )
    async with await _client() as client:
        final = await _run_job(client, srt_text=ass)
    assert final["status"] == "succeeded", final.get("error")


async def test_garbage_srt_text_fails_with_invalid_srt(fake_render: dict[str, Any]) -> None:
    async with await _client() as client:
        final = await _run_job(client, srt_text="chỉ là văn bản, không có mốc thời gian")
    assert final["status"] == "failed"
    assert final["error"]["code"] == "INVALID_SRT"


async def test_video_srt_inside_the_clips_json_is_used_as_subtitle(
    fake_render: dict[str, Any],
) -> None:
    """Dán nguyên kịch bản của bước trước là có luôn phụ đề, khỏi copy hai lần."""
    script = {
        "video_edit_script": [{"source_video": "c1.mp4", "start": 0, "end": 2}],
        "video_srt": AI_SRT,
    }
    async with await _client() as client:
        final = await _run_job(client, clips=json.dumps(script))
    assert final["status"] == "succeeded", final.get("error")
    assert "subtitles=styled.ass" in " ".join(fake_render["cmd"])


async def test_pasted_srt_text_wins_over_video_srt_in_the_script(
    fake_render: dict[str, Any],
) -> None:
    # Tự dán phụ đề nghĩa là cố ý muốn dùng bản đó.
    script = {
        "video_edit_script": [{"source_video": "c1.mp4", "start": 0, "end": 2}],
        "video_srt": "chỉ là văn bản, không có mốc thời gian",
    }
    async with await _client() as client:
        final = await _run_job(client, clips=json.dumps(script), srt_text=AI_SRT)
    assert final["status"] == "succeeded", final.get("error")
