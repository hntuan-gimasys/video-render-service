"""FastAPI app: routes, auth, lifespan — docs/SPEC.md §3."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import signal
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Form, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import ValidationError

from app.config import Settings, get_settings
from app.drive import parse_drive_id
from app.drive_folder import parse_folder_id
from app.intake import (
    apply_form_overrides,
    clean,
    file_size,
    parse_options,
    resolve_drive_output_folder,
    save_sources,
)
from app.jobs import Job, JobStore, get_semaphore, janitor_loop, run_job
from app.job_store import cancel_job, shutdown_jobs
from app.models import CreateJobResponse, HealthResponse, JobResponse, JobStatus
from app.streaming import build_download_response
from app.utils import (
    AppError,
    InvalidOptions,
    JobNotFound,
    JobNotReady,
    NoVideoSource,
    QueueFull,
    Unauthorized,
    bind_job,
    free_space_mb,
    new_job_id,
    safe_rmtree,
    setup_logging,
)

logger = logging.getLogger(__name__)

_bearer = HTTPBearer(auto_error=False)


# --------------------------------------------------------------------------- #
# Lifespan
# --------------------------------------------------------------------------- #
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    setup_logging(settings.log_level)
    store = JobStore()
    app.state.store = store
    app.state.settings = settings
    app.state.semaphore = get_semaphore(settings)
    app.state.ffmpeg_version = await _ffmpeg_version()

    janitor = asyncio.create_task(janitor_loop(store, settings), name="janitor")
    app.state.janitor = janitor
    _install_sigterm_handler(store)

    logger.info(
        "Service khởi động",
        extra={
            "work_dir": str(settings.work_dir),
            "max_concurrent_jobs": settings.max_concurrent_jobs,
            "ffmpeg": app.state.ffmpeg_version,
        },
    )
    try:
        yield
    finally:
        janitor.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await janitor
        await shutdown_jobs(store)
        logger.info("Service đã dừng")


def _install_sigterm_handler(store: JobStore) -> None:
    """Dự phòng SIGTERM khi chạy ngoài uvicorn.

    Cloud Run chỉ cho 10s sau SIGTERM. Đường chính là uvicorn nhận SIGTERM ->
    shutdown graceful -> lifespan chạy ``finally`` -> ``shutdown_jobs``. Vì thế
    KHÔNG được ghi đè handler của uvicorn: ``loop.add_signal_handler`` thay thế
    handler cũ, làm uvicorn không bao giờ thấy tín hiệu và bị SIGKILL sau 10s.
    Chỉ tự đăng ký khi tín hiệu vẫn còn ở mức mặc định (chạy bằng script khác).
    """
    loop = asyncio.get_running_loop()

    def _handle() -> None:
        logger.warning("Nhận SIGTERM, bắt đầu dọn dẹp")
        loop.create_task(shutdown_jobs(store, timeout=8.0))

    for sig in (signal.SIGTERM, signal.SIGINT):
        if signal.getsignal(sig) is not signal.SIG_DFL:
            logger.debug("Signal %s đã có handler (uvicorn) -> không ghi đè", sig)
            continue
        try:
            loop.add_signal_handler(sig, _handle)
        except (NotImplementedError, RuntimeError, ValueError):
            # Windows không hỗ trợ add_signal_handler cho SIGTERM.
            logger.debug("Không đăng ký được signal handler cho %s", sig)


async def _ffmpeg_version() -> str | None:
    """Lấy version ffmpeg cho /healthz. Không có ffmpeg -> None."""
    try:
        process = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except (OSError, FileNotFoundError):
        return None
    stdout, _ = await process.communicate()
    first_line = stdout.decode("utf-8", "replace").splitlines()[:1]
    if not first_line:
        return None
    match = re.search(r"ffmpeg version (\S+)", first_line[0])
    return match.group(1) if match else first_line[0][:100]


app = FastAPI(
    title="Video Render Service",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs",
)


# --------------------------------------------------------------------------- #
# Auth & dependency
# --------------------------------------------------------------------------- #
async def require_api_key(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> None:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise Unauthorized("Thiếu header Authorization: Bearer <API_KEY>")
    if not _constant_time_equals(credentials.credentials, settings.api_key):
        raise Unauthorized()


def _constant_time_equals(left: str, right: str) -> bool:
    import hmac

    return hmac.compare_digest(left.encode(), right.encode())


def get_store(request: Request) -> JobStore:
    return request.app.state.store  # type: ignore[no-any-return]


StoreDep = Annotated[JobStore, Depends(get_store)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


# --------------------------------------------------------------------------- #
# Exception handler
# --------------------------------------------------------------------------- #
@app.exception_handler(AppError)
async def app_error_handler(_request: Request, exc: AppError) -> JSONResponse:
    payload: dict[str, Any] = {"error": exc.to_dict()}
    headers = {"WWW-Authenticate": "Bearer"} if exc.http_status == 401 else None
    return JSONResponse(status_code=exc.http_status, content=payload, headers=headers)


@app.exception_handler(ValidationError)
async def validation_error_handler(_request: Request, exc: ValidationError) -> JSONResponse:
    # options JSON sai schema -> 422 INVALID_OPTIONS theo bảng §3.6
    err = InvalidOptions(detail=str(exc)[:2000])
    return JSONResponse(status_code=err.http_status, content={"error": err.to_dict()})


@app.exception_handler(RequestValidationError)
async def request_validation_error_handler(
    _request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Form/field sai kiểu: FastAPI mặc định trả {"detail": [...]}, khác hẳn
    envelope {"error": {code, message}} mà SPEC §3.2/§3.6 quy định."""
    err = InvalidOptions("Dữ liệu form không hợp lệ", detail=str(exc.errors())[:2000])
    return JSONResponse(status_code=err.http_status, content={"error": err.to_dict()})


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@app.post(
    "/api/jobs",
    status_code=202,
    response_model=CreateJobResponse,
    dependencies=[Depends(require_api_key)],
)
async def create_job(
    request: Request,
    store: StoreDep,
    settings: SettingsDep,
    # Link THƯ MỤC Google Drive chứa toàn bộ video nguồn. Service tự quét thư
    # mục, đối chiếu tên file với kịch bản rồi chỉ tải những video thật sự dùng.
    # Đi đường Drive chứ không upload thẳng vì Cloud Run chặn cứng 32 MiB cho
    # TOÀN BỘ một request — ghép nhiều clip là vượt ngay.
    # example="" để Swagger UI prefill ô trống; không có nó Swagger tự điền chữ
    # "string" vào mọi ô rồi gửi kèm, job chết vì link Drive rác.
    video_folder_url: Annotated[str, Form(json_schema_extra={"example": ""})] = "",
    # Các đoạn cần cắt, nhận HAI cú pháp (xem intake.parse_clips_field):
    #   JSON  [{"source_video": "<tên file>", "start": "00:00", "end": "00:04"}]
    #         hoặc nguyên object có video_edit_script/video_srt của bước trước;
    #   gọn   mỗi dòng một đoạn: "1 00:00-00:05".
    clips: Annotated[str | None, Form(json_schema_extra={"example": ""})] = None,
    # Nội dung phụ đề (SRT hoặc ASS) dán thẳng. Bỏ trống mà kịch bản JSON có
    # video_srt thì lấy luôn từ đó. Code fence ```...``` được tự bỏ.
    srt_text: Annotated[str | None, Form(json_schema_extra={"example": ""})] = None,
    # Text bìa hiện tích tắc đầu video (ảnh bìa TikTok). "|" = xuống dòng.
    intro_text: Annotated[str | None, Form(json_schema_extra={"example": ""})] = None,
    music_url: Annotated[str | None, Form(json_schema_extra={"example": ""})] = None,
    options: Annotated[str | None, Form(json_schema_extra={"example": "{}"})] = None,
) -> CreateJobResponse:
    folder_url = clean(video_folder_url)
    if not folder_url:
        raise NoVideoSource("Cần link thư mục Google Drive chứa video nguồn")

    if await store.count_active() >= settings.max_queued_jobs:
        raise QueueFull(f"Đang có {settings.max_queued_jobs} job chưa xong")

    render_options, script_srt = apply_form_overrides(
        parse_options(options), intro_text, clips
    )
    # video_srt trong kịch bản chỉ dùng khi người dùng không tự dán phụ đề.
    subtitle_text = clean(srt_text) or script_srt

    # Link Drive sai dạng là lỗi 400 trong bảng §3.6 -> chặn ngay trong request,
    # không để job nhận 202 rồi mới chết vài giây sau. Riêng TÊN FILE trong kịch
    # bản thì phải quét thư mục mới đối chiếu được, nên kiểm ở bước chuẩn bị.
    parse_folder_id(folder_url)
    if clean(music_url):
        parse_drive_id(music_url or "")
    # Cùng lý do: bật upload mà không có thư mục đích thì chắc chắn chết ở bước
    # cuối, sau khi đã tải và render xong. Chặn trong request để client biết
    # ngay chứ không mất một lượt render.
    resolve_drive_output_folder(render_options, settings)

    job_id = new_job_id()
    log = bind_job(logger, job_id)
    workspace = settings.work_dir / job_id
    await asyncio.to_thread(workspace.mkdir, parents=True, exist_ok=True)

    try:
        sources = await save_sources(
            workspace,
            settings,
            log,
            folder_url=folder_url,
            srt_text=subtitle_text,
            music_url=music_url,
        )
    except BaseException:
        await asyncio.to_thread(safe_rmtree, workspace)
        raise

    job = Job(id=job_id, workspace=workspace, options=render_options, sources=sources)
    await store.create(job)
    job.task = asyncio.create_task(
        run_job(job_id, store, settings, request.app.state.semaphore), name=f"job-{job_id}"
    )
    log.info("Tạo job", extra={"status": job.status.value})
    return CreateJobResponse(job_id=job.id, status=job.status, created_at=job.created_at)


@app.get(
    "/api/jobs/{job_id}",
    response_model=JobResponse,
    dependencies=[Depends(require_api_key)],
)
async def get_job(job_id: str, store: StoreDep) -> JobResponse:
    job = await store.get(job_id)
    if job is None:
        raise JobNotFound()
    return JobResponse(
        job_id=job.id,
        status=job.status,
        progress=job.progress,
        stage_message=job.stage_message,
        created_at=job.created_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
        output=job.output,
        error=job.error,
    )


# KHÔNG có require_api_key: link tải phải dùng được trực tiếp (thẻ <video>,
# trình duyệt, người khác mở link) mà không gắn được header Authorization.
# Thứ bảo vệ duy nhất còn lại là job_id không đoán được: uuid4 48 bit, và job
# tự hết hạn sau JOB_TTL_SECONDS. Ai có link là tải được, nên coi link tải như
# một mật khẩu dùng một lần.
@app.get("/api/jobs/{job_id}/download")
async def download_job(job_id: str, request: Request, store: StoreDep) -> Response:
    job = await store.get(job_id)
    if job is None:
        raise JobNotFound()
    if job.status is not JobStatus.SUCCEEDED:
        raise JobNotReady(f"Job đang ở trạng thái {job.status.value}")

    # Job đã đẩy lên Drive thì bản trong /tmp bị xoá NGAY sau upload, trước cả
    # lúc job được đánh dấu succeeded (xem _finalize trong app/jobs.py) — nên ở
    # đây không còn file nào để stream. Chuyển hướng sang link Drive thay vì trả
    # 404: bản thân endpoint này vẫn là "link tới video", ai bấm vào vẫn tới
    # được trình phát, và bên gọi đang trỏ vào đây không phải sửa gì.
    #
    # Dùng thẳng output.download_url chứ không tự chọn lại giữa link xem và link
    # tải: _finalize đã quyết định một lần (link XEM, có fallback về link tải khi
    # Drive không trả webViewLink), giữ một nguồn quyết định duy nhất.
    #
    # Cố tình lệch SPEC §3.3, nơi endpoint này được mô tả là trả
    # StreamingResponse kèm Content-Length: khi output nằm trên Drive thì không
    # có gì để stream nữa. Job KHÔNG upload (deploy không đặt
    # DRIVE_OUTPUT_FOLDER_ID) vẫn đi đúng đường cũ bên dưới.
    if job.output is not None and job.output.drive_file_id:
        # Hai kiểu client cần hai thứ TRÁI NGƯỢC nhau ở đây, nên phải phân theo
        # Accept chứ không có một câu trả lời vừa cả hai:
        #
        # - Người bấm vào link (browser điều hướng, Accept: text/html,...) cần
        #   302 để tới thẳng trình phát Drive.
        # - Client gọi bằng fetch/XHR — kể cả nút Execute của Swagger, nó gửi
        #   Accept: application/json — thì KHÔNG đi qua được redirect này: fetch
        #   tự đi theo Location sang drive.google.com, một origin khác cần phiên
        #   đăng nhập Google, nên nó chỉ báo "Failed to fetch". Đã đo: curl -L
        #   không mang credential nhận 401 từ Drive. Với chúng thì phải trả link
        #   trong BODY.
        #
        # Body trả nguyên object output (schema JobOutput) chứ không bọc thêm tên
        # khoá mới: bên gọi đã đọc download_url từ GET /api/jobs/{id} thì dùng lại
        # đúng code phân tích đó được, không phải học thêm khoá nào.
        accept = request.headers.get("accept") or ""
        if "application/json" in accept:
            return JSONResponse(job.output.model_dump(mode="json"))
        # 302 chứ không 301: link này gắn với một job cụ thể và job sẽ hết hạn,
        # không được để client hay proxy cache lại như một chuyển hướng vĩnh viễn.
        return RedirectResponse(job.output.download_url, status_code=302)

    path = job.output_path
    total_size = await asyncio.to_thread(file_size, path)
    if total_size is None:
        raise JobNotFound("File output đã bị dọn")
    return build_download_response(path, total_size, request.headers.get("range"))


@app.delete("/api/jobs/{job_id}", status_code=204, dependencies=[Depends(require_api_key)])
async def delete_job(job_id: str, store: StoreDep) -> Response:
    job = await store.get(job_id)
    if job is None:
        raise JobNotFound()

    was_running = not job.status.is_terminal
    await cancel_job(job)
    if not was_running:
        # Job đã kết thúc: xoá luôn record, nếu giữ lại thì GET vẫn báo
        # "succeeded" kèm output đã bị xoá -> thông tin sai.
        await store.delete(job_id)
    # Job đang chạy: giữ record với status=cancelled để client quan sát được,
    # janitor sẽ dọn record sau TTL.
    return Response(status_code=204)


@app.get("/healthz", response_model=HealthResponse)
async def healthz(request: Request, store: StoreDep, settings: SettingsDep) -> HealthResponse:
    return HealthResponse(
        status="ok",
        ffmpeg=getattr(request.app.state, "ffmpeg_version", None),
        active_jobs=await store.count_active(),
        tmp_free_mb=round(await asyncio.to_thread(free_space_mb, settings.work_dir), 1),
    )
