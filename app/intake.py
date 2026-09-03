"""Chuẩn hoá input từ multipart form — docs/SPEC.md §3.1, §4.

Tách khỏi ``app.main`` để giữ mỗi file dưới 400 dòng: ở đây chỉ có hàm thuần
(và một hàm stat) biến dữ liệu người dùng gửi lên thành giá trị tin được.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Final

from pydantic import ValidationError

from app.clips import parse_clip_lines
from app.config import Settings
from app.drive_folder import parse_folder_id
from app.job_store import JobSources
from app.models import ClipSpec, IntroTextOptions, RenderOptions
from app.utils import InvalidOptions

__all__ = [
    "file_size",
    "clean",
    "parse_options",
    "normalize_multiline",
    "apply_form_overrides",
    "parse_edit_script",
    "parse_clips_field",
    "resolve_drive_output_folder",
    "normalize_pasted_subtitle",
    "save_sources",
]

# Khoá mà pipeline sinh kịch bản dựng đặt tên. Nhận thẳng để khỏi phải đổi tên
# khoá thủ công giữa hai bước.
_SCRIPT_KEYS: Final[tuple[str, ...]] = ("video_edit_script", "edit_script", "clips")
_SRT_KEYS: Final[tuple[str, ...]] = ("video_srt", "srt", "srt_text")

def file_size(path: Path) -> int | None:
    """Kích thước file, None nếu không còn tồn tại (janitor/DELETE đã dọn)."""
    try:
        return path.stat().st_size
    except OSError:
        return None


def clean(value: str | None) -> str | None:
    stripped = (value or "").strip()
    return stripped or None


def resolve_drive_output_folder(options: RenderOptions, settings: Settings) -> str | None:
    """Thư mục Drive sẽ nhận output. ``None`` nghĩa là không upload.

    Ưu tiên ``delivery.drive_folder_id`` của request, sau đó tới env
    ``DRIVE_OUTPUT_FOLDER_ID``.

    Nhận cả LINK thư mục (dán nguyên link vào ô này là chuyện rất dễ xảy ra) và
    tự rút id ra. Ngược lại, KHÔNG kiểm dạng của id trần: id thư mục thường ~33
    ký tự nhưng id của chính một Shared Drive chỉ ~19, và ``root`` cũng là một
    parent hợp lệ — siết theo độ dài như ``parse_folder_id`` làm cho link nguồn
    sẽ chặn oan những ca dùng thật. Id sai thì Drive trả 404 và message đó đã
    đủ rõ.

    Bật ``upload_to_drive`` mà không có id nào là fail CHẮC CHẮN, không phải "có
    thể": không truyền ``parents`` thì file rơi vào My Drive của chính service
    account, mà service account KHÔNG có quota Drive nên Google trả
    ``403 storageQuotaExceeded``. Cùng lý do đó, thư mục đích phải nằm trên
    Shared Drive: file trong My Drive của người thật vẫn do service account sở
    hữu nên vẫn tính vào quota (bằng 0) của nó, share quyền ghi cũng không cứu
    được. Chặn ngay tại đây để thông báo nói đúng nguyên nhân.
    """
    wanted = options.delivery.upload_to_drive
    if wanted is False:
        return None
    raw = clean(options.delivery.drive_folder_id) or clean(settings.drive_output_folder_id)
    if wanted is None:
        # Chưa khai: có thư mục cấu hình sẵn thì đẩy lên Drive, không thì thôi.
        # Không báo lỗi ở nhánh này — người gọi chưa yêu cầu gì cả.
        return raw or None
    if not raw:
        raise InvalidOptions(
            "delivery.upload_to_drive=true nhưng không có thư mục Drive để đẩy vào",
            detail=(
                "Khai delivery.drive_folder_id, hoặc đặt env DRIVE_OUTPUT_FOLDER_ID. "
                "Thư mục phải nằm trên Shared Drive và được chia sẻ quyền ghi cho "
                "service account đang chạy service: service account không có quota "
                "Drive nên đẩy vào My Drive của người thật sẽ bị 403 "
                "storageQuotaExceeded."
            ),
        )
    if raw.startswith(("http://", "https://")) or "/" in raw:
        return parse_folder_id(raw)
    return raw


def parse_options(raw: str | None) -> RenderOptions:
    if not clean(raw):
        return RenderOptions()
    try:
        payload = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        raise InvalidOptions(f"options không phải JSON hợp lệ: {exc}") from exc
    if not isinstance(payload, dict):
        raise InvalidOptions("options phải là một object JSON")
    try:
        return RenderOptions.model_validate(payload)
    except ValidationError as exc:
        raise InvalidOptions(
            "options không đúng schema", detail=_short_validation_error(exc)
        ) from exc


def _short_validation_error(exc: ValidationError) -> str:
    problems = [
        f"{'.'.join(str(part) for part in err['loc'])}: {err['msg']}" for err in exc.errors()[:10]
    ]
    return "; ".join(problems)[:2000]


def normalize_multiline(value: str | None) -> str | None:
    r"""Khôi phục dấu xuống dòng cho ô nhập một dòng.

    Swagger UI (và nhiều form HTML) chỉ cho gõ một dòng, người dùng dán text
    nhiều dòng vào là mất hết newline. Nên chấp nhận thêm hai cách gõ tay:
    ký tự ``|`` và chuỗi hai ký tự ``\n``.
    """
    text = clean(value)
    if text is None:
        return None
    return text.replace(r"\n", "\n").replace("|", "\n").strip() or None


def parse_edit_script(raw: str | None) -> tuple[list[ClipSpec], str | None]:
    """Đọc kịch bản dựng của pipeline trước, trả ``(danh sách đoạn, phụ đề)``.

    Nhận cả ba dạng cho tiện dán:

    * mảng đoạn: ``[{"source_video": "a.mp4", "start": "00:00", "end": "00:04"}]``
    * object có khoá ``video_edit_script`` (kèm ``video_srt`` nếu có)
    * nguyên response của bước trước: ``{"output": {...}}``

    Mỗi đoạn trỏ tới video nguồn bằng ``source_video`` (tên file) hoặc
    ``source`` (số hiệu) — xem :class:`app.models.ClipSpec`.
    """
    text = clean(raw)
    if text is None:
        return [], None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise InvalidOptions(f"edit_script không phải JSON hợp lệ: {exc}") from exc

    subtitle: str | None = None
    if isinstance(payload, dict):
        # Dán nguyên response của bước trước cũng chạy được.
        if isinstance(payload.get("output"), dict):
            payload = payload["output"]
        subtitle = next(
            (payload[key] for key in _SRT_KEYS if isinstance(payload.get(key), str)), None
        )
        payload = next(
            (payload[key] for key in _SCRIPT_KEYS if isinstance(payload.get(key), list)), None
        )
        if payload is None:
            raise InvalidOptions(
                "edit_script thiếu danh sách đoạn",
                detail="Cần một mảng, hoặc object có khoá " + " / ".join(_SCRIPT_KEYS),
            )
    if not isinstance(payload, list):
        raise InvalidOptions("edit_script phải là mảng các đoạn cần ghép")

    try:
        specs = [ClipSpec.model_validate(item) for item in payload]
    except ValidationError as exc:
        raise InvalidOptions(
            "edit_script không đúng schema", detail=_short_validation_error(exc)
        ) from exc
    return specs, clean(subtitle)


def parse_clips_field(raw: str | None) -> tuple[list[ClipSpec], str | None]:
    """Ô ``clips`` nhận cả hai cú pháp, tự nhận ra dạng nào.

    * JSON (mở đầu bằng ``[`` hoặc ``{``) — kịch bản dựng của pipeline khác,
      xem :func:`parse_edit_script`; trả kèm ``video_srt`` nếu có.
    * Cú pháp gọn mỗi dòng một đoạn (``1 00:00-00:05``), xem
      :func:`app.clips.parse_clip_lines`.

    Một ô cho cả hai vì đằng nào cũng chỉ khai được một kiểu — tách hai ô chỉ
    làm form dài ra và người dùng phải chọn.
    """
    text = clean(raw)
    if text is None:
        return [], None
    if text.lstrip()[:1] in "[{":
        return parse_edit_script(text)
    return parse_clip_lines(text), None


def normalize_pasted_subtitle(raw: str | None) -> str | None:
    r"""Phụ đề dán từ JSON: đổi ``\n`` gõ tay thành dấu xuống dòng thật.

    Giá trị ``video_srt`` trong JSON là một chuỗi dài chứa ``\n``; copy ra khỏi
    JSON rồi dán vào ô một dòng thì những ``\n`` đó còn nguyên dạng hai ký tự.

    KHÔNG đụng vào file ASS: ở đó ``\n`` là tag ngắt dòng của libass nằm giữa
    dòng Dialogue, đổi thành newline thật là hỏng cấu trúc file.
    """
    text = clean(raw)
    if text is None:
        return None
    if "[Script Info]" in text or "[Events]" in text or "[V4+ Styles]" in text:
        return text
    return text.replace(r"\n", "\n")


def apply_form_overrides(
    options: RenderOptions,
    intro_text: str | None,
    clips_text: str | None,
) -> tuple[RenderOptions, str | None]:
    """Gộp các ô tiện dụng trên Swagger vào ``options``.

    Trả kèm phần phụ đề đọc được từ ``edit_script`` (khoá ``video_srt``), để
    nơi gọi dùng làm ``srt_text`` khi người dùng không tự dán phụ đề.

    Mấy ô này chỉ là lối tắt cho ``options.intro.text`` và ``options.clips``;
    khai trong JSON ``options`` thì JSON thắng, vì đó là cách khai chi tiết
    hơn và người dùng phải cố ý mới gõ được.
    """
    text = normalize_multiline(intro_text)
    if text and not options.intro.text:
        try:
            options.intro = IntroTextOptions.model_validate(
                {**options.intro.model_dump(), "text": text}
            )
        except ValidationError as exc:
            raise InvalidOptions(
                "intro_text không hợp lệ", detail=_short_validation_error(exc)
            ) from exc

    specs, script_srt = parse_clips_field(clips_text)
    if specs and not options.clips:
        options.clips = specs
    return options, script_srt


async def save_sources(
    workspace: Path,
    settings: Settings,
    log: logging.LoggerAdapter,
    *,
    folder_url: str,
    srt_text: str | None,
    music_url: str | None,
) -> JobSources:
    """Chốt lại nguồn cho job: thư mục video, phụ đề dán tay, link nhạc.

    Video và nhạc đều tải ở phía server (xem app/prepare.py) nên ở đây chỉ ghi
    mỗi phụ đề xuống đĩa. Cloud Run chặn cứng 32 MiB cho toàn bộ một request,
    nên upload thẳng video không dùng được cho việc ghép nhiều clip.
    """
    sources = JobSources(video_folder_url=folder_url, music_url=clean(music_url))
    pasted = normalize_pasted_subtitle(srt_text)
    if pasted:
        dest = workspace / "subs_raw.srt"
        await asyncio.to_thread(dest.write_text, pasted, encoding="utf-8", newline="\n")
        sources.srt_path = dest
        log.info("Đã nhận phụ đề dạng text", extra={"chars": len(pasted)})
    return sources
