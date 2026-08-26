# SPEC — Video Render Service

Tài liệu này là **nguồn sự thật duy nhất** cho việc implement. Mọi module trong `app/`
phải tuân thủ đúng chữ ký hàm và hợp đồng dữ liệu mô tả ở đây.

---

## 1. Bối cảnh & ràng buộc

Service nhận: 1 video (upload trực tiếp **hoặc** link Google Drive), 1 file `.srt`
(tuỳ chọn), 1 file nhạc nền (tuỳ chọn) → dùng FFmpeg ghép → trả về 1 video duy nhất.

**Ràng buộc bắt buộc:**

| Ràng buộc | Hệ quả thiết kế |
|---|---|
| Không dùng database | Job state lưu trong RAM (`dict`) của process → service **phải** chạy `--max-instances=1` |
| Không dùng Cloud Storage | File trung gian nằm ở `/tmp`; output trả về qua HTTP stream hoặc upload ngược lên Google Drive |
| `/tmp` trên Cloud Run là **tmpfs (RAM)** | Dung lượng file tính vào memory. Memory phải ≥ (video_in + music + video_out) × 1.3 |
| Cloud Run giới hạn request body 32 MiB với HTTP/1 | Bật `--use-http2` để upload file lớn, hoặc khuyến nghị dùng Drive link |
| CPU bị throttle ngoài request | Bắt buộc `--no-cpu-throttling` (CPU always allocated) để worker nền chạy được |
| Request timeout tối đa 3600s | Render dài phải chạy **async** (job model), không render đồng bộ trong request |

---

## 2. Kiến trúc

```
Client
  │
  ├── POST /api/jobs ────────► FastAPI (validate, lưu file vào /tmp/jobs/<id>/)
  │                              │
  │                              └─► asyncio.create_task(run_job)
  │                                     ├─ drive.download()  (nếu là link)
  │                                     ├─ ffprobe (lấy duration, có audio hay không)
  │                                     ├─ ffmpeg -progress pipe:1  → cập nhật % vào JobStore
  │                                     └─ (tuỳ chọn) drive.upload() output
  │
  ├── GET  /api/jobs/{id} ───► trạng thái + progress
  ├── GET  /api/jobs/{id}/download ─► StreamingResponse file mp4
  └── DELETE /api/jobs/{id} ─► xoá workspace
```

Một `JanitorTask` chạy nền mỗi 5 phút, xoá job đã xong quá `JOB_TTL_SECONDS`.
Một `asyncio.Semaphore(MAX_CONCURRENT_JOBS)` giới hạn số ffmpeg chạy song song.

---

## 3. API Contract

Tất cả endpoint (trừ `/healthz` và `GET /api/jobs/{id}/download`) yêu cầu header:
`Authorization: Bearer <API_KEY>` — trả `401` nếu sai.

### 3.1 `POST /api/jobs`

`Content-Type: multipart/form-data`

| Field | Kiểu | Bắt buộc | Ghi chú |
|---|---|---|---|
| `video_folder_url` | string | ✅ | Link **thư mục** Google Drive chứa toàn bộ video nguồn |
| `clips` | string | không | Các đoạn cần cắt — JSON hoặc cú pháp gọn (xem §4.2) |
| `srt_text` | string | không | Nội dung phụ đề (SRT hoặc ASS) dán thẳng |
| `intro_text` | string | không | Text bìa hiện tích tắc đầu video (xem §4.3) |
| `music_url` | string | không | Link Drive tới file nhạc nền |
| `options` | string (JSON) | không | Xem §4. Thiếu field nào thì dùng default |

**Vì sao chỉ có thư mục Drive, không có upload.** Cloud Run chặn cứng **32 MiB
cho TOÀN BỘ body của một request** (đo thực tế: 31,5 MiB qua được, 32,0 MiB bị
`413` từ Google Frontend). Ghép nhiều clip là vượt ngay, nên đường upload không
dùng được cho việc này. Qua Drive thì trần là `MAX_DOWNLOAD_MB` (mặc định 4 GB)
cho mỗi file.

Nhớ **chia sẻ thư mục cho service account** (xem §6), nếu không lượt quét trả
về `DRIVE_DOWNLOAD_FAILED`.

**Số hiệu video** mà `options.clips[].source` trỏ tới được đánh từ 1 theo thứ
tự tên file trong thư mục (Drive sắp bằng `name_natural`). Nhưng cách chắc ăn
hơn là trỏ bằng **tên file** — xem §4.2.

Validate:
- Thiếu `video_folder_url` → `400 NO_VIDEO_SOURCE`
- Link không phải link thư mục Drive → `400 INVALID_DRIVE_URL`
- `music_url` không phải link Drive → `400 INVALID_DRIVE_URL`
- `options` / `clips` sai cú pháp → `422 INVALID_OPTIONS`
- Thư mục rỗng hoặc không đọc được → job `failed` với `DRIVE_DOWNLOAD_FAILED`
- Tên file trong `clips` không có trong thư mục → job `failed` với
  `INVALID_OPTIONS` kèm danh sách video có thật (kiểm TRƯỚC khi tải byte nào)

Response `202`:
```json
{ "job_id": "b3f1c2a4", "status": "queued", "created_at": "2026-08-22T09:00:00Z" }
```

### 3.2 `GET /api/jobs/{job_id}`

```json
{
  "job_id": "b3f1c2a4",
  "status": "queued|downloading|merging|probing|rendering|uploading|succeeded|failed|cancelled",
  "progress": 42.7,
  "stage_message": "Rendering 00:01:23 / 00:03:14",
  "created_at": "...",
  "started_at": "...",
  "finished_at": null,
  "output": {
    "filename": "output.mp4",
    "size_bytes": 128374651,
    "duration_seconds": 194.2,
    "download_url": "/api/jobs/b3f1c2a4/download",
    "drive_file_id": null,
    "drive_view_url": null
  },
  "error": null
}
```

Khi `status = "failed"`:
```json
"error": { "code": "FFMPEG_FAILED", "message": "...", "detail": "<200 dòng cuối stderr>" }
```

`404 JOB_NOT_FOUND` nếu id không tồn tại (hoặc đã bị janitor dọn).

### 3.3 `GET /api/jobs/{job_id}/download`

- **Không cần API_KEY.** Link tải phải dùng được trực tiếp từ trình duyệt hoặc
  thẻ `<video>`, những nơi không gắn được header `Authorization`. Thứ bảo vệ
  duy nhất là `job_id` không đoán được (uuid4, 48 bit) cộng với việc job tự hết
  hạn sau `JOB_TTL_SECONDS`. Coi link tải như mật khẩu dùng một lần: ai có link
  là tải được.
- `409 JOB_NOT_READY` nếu status ≠ `succeeded`
- Trả `StreamingResponse` chunk 1 MiB, `Content-Type: video/mp4`,
  `Content-Disposition: attachment; filename="output.mp4"`, `Content-Length` chính xác.
- **Phải** hỗ trợ HTTP Range request (`206 Partial Content`) để client resume được.

### 3.4 `DELETE /api/jobs/{job_id}`
Huỷ job đang chạy (kill process ffmpeg), xoá workspace. Trả `204`.

### 3.5 `GET /healthz`
Không cần auth. Trả `{"status":"ok","ffmpeg":"7.x","active_jobs":1,"tmp_free_mb":6144}`.

### 3.6 Bảng mã lỗi

| HTTP | code | Ý nghĩa |
|---|---|---|
| 400 | `NO_VIDEO_SOURCE` / `BOTH_VIDEO_SOURCES` / `INVALID_DRIVE_URL` | Input sai |
| 401 | `UNAUTHORIZED` | Sai API key |
| 404 | `JOB_NOT_FOUND` | |
| 409 | `JOB_NOT_READY` | Tải file khi chưa render xong |
| 413 | `FILE_TOO_LARGE` | File trên Drive vượt `MAX_DOWNLOAD_MB` |
| 422 | `INVALID_OPTIONS` / `INVALID_SRT` | |
| 429 | `QUEUE_FULL` | Vượt `MAX_QUEUED_JOBS` |
| 502 | `DRIVE_DOWNLOAD_FAILED` / `DRIVE_UPLOAD_FAILED` | |
| 500 | `FFMPEG_FAILED` / `PROBE_FAILED` / `INTERNAL` | |
| 507 | `INSUFFICIENT_TMP_SPACE` | `/tmp` không đủ chỗ trước khi bắt đầu |

---

## 4. Options schema (JSON)

```jsonc
{
  // MỌI số đo dưới đây tính bằng PIXEL của khung hình output (xem §4.1).
  "subtitle": {
    "enabled": true,
    "mode": "burn",              // "burn" = hardsub | "soft" = mov_text track
    "font_name": "Liberation Serif", // có sẵn trong image; font khác phải bỏ
                                     // file .ttf vào /app/fonts
    "font_size": null,           // px. null = tự co theo BỀ NGANG video
    "font_size_ratio": 0.04,     // cỡ chữ = 4% bề ngang khi font_size = null
    "primary_color": "#FFFFFF",
    "outline_color": "#000000",
    "back_color": "#80000000",   // AARRGGBB, dùng khi border_style=4
    "border_style": 1,           // 1 = viền chữ, 4 = hộp nền
    "outline": null,             // px. null = 8% cỡ chữ
    "shadow": 0,                 // px
    "alignment": 2,              // numpad ASS: 2 = giữa-dưới
    "margin_v": null,            // px. null = 14% chiều cao (chừa thanh nút TikTok)
    "margin_v_ratio": 0.14,
    "margin_h": null,            // px. null = 6% bề ngang
    "margin_h_ratio": 0.06,
    "bold": true,
    "italic": true,              // serif nghiêng, kiểu caption du lịch
    "effect": "fade",            // none|fade|pop|slide_up|typewriter|glow (§4.4)
    "offset_seconds": 0.0        // dịch toàn bộ timing phụ đề
  },
  // Text bìa hiện tích tắc ở đầu video — TikTok lấy frame ĐẦU làm ảnh bìa.
  "intro": {
    "enabled": true,
    "text": null,                // nhiều dòng; null = không có text bìa
    "start": 0.0,                // phải là 0 nếu muốn làm ảnh bìa
    "duration": 2.0,
    "font_name": "Liberation Sans",
    "font_size": null,           // px. null = 6.2% bề ngang
    "font_size_ratio": 0.062,
    "headline_scale": 1.55,      // dòng ĐẦU to gấp bấy nhiêu lần
    "primary_color": "#FFF200",
    "outline_color": "#000000",
    "back_color": "#80000000",
    "border_style": 1,
    "outline": null,
    "shadow": 0,
    "position_ratio": 0.44,      // 0 = sát đỉnh, 1 = sát đáy
    "margin_h": null,
    "margin_h_ratio": 0.05,
    "bold": true,
    "italic": false,
    "effect": "none"             // giữ "none" để frame 0 hiện đủ chữ
  },
  // Cắt & ghép nhiều đoạn. Rỗng = dùng nguyên video nguồn (§4.2).
  // source nhận số hiệu video HOẶC tên file; source_video là tên gọi khác.
  "clips": [
    { "source": 1, "start": "00:00", "end": "00:05" },
    { "source_video": "canh-rung.mp4", "start": 10, "duration": 8 }
  ],
  "music": {
    "enabled": true,
    "volume": 0.18,              // 0.0–2.0, âm lượng nhạc nền
    "original_volume": null,     // null = tự quyết (xem §4.5); số = ép cứng
    "loop": true,                // lặp nhạc cho đủ độ dài video
    "fade_in": 2.0,
    "fade_out": 3.0,
    "ducking": false,            // true → dùng sidechaincompress
    "start_offset": 0.0          // bỏ qua N giây đầu của file nhạc
  },
  "output": {
    "filename": "output.mp4",
    "video_codec": "libx264",
    "crf": 23,
    "preset": "veryfast",
    "audio_codec": "aac",
    "audio_bitrate": "192k",
    "resolution": null,          // vd "1920x1080" hoặc null = giữ nguyên
    "fps": null,
    "faststart": true,
    "copy_video_if_possible": true  // xem §5.1
  },
  "delivery": {
    "upload_to_drive": false,
    "drive_folder_id": null
  }
}
```

Dùng Pydantic v2, mọi field có default → client gửi `{}` vẫn chạy được.
Validate: `crf` 0–51, `volume` 0–2, `preset` thuộc whitelist, `resolution` khớp
`^\d+x\d+$` **và phải là số chẵn** (libx264/libx265 với `yuv420p` từ chối kích
thước lẻ, báo 422 sớm còn hơn để ffmpeg chết giữa đường).

### 4.1 Hệ toạ độ: mọi số đo là PIXEL

Nếu để ffmpeg tự chuyển `.srt` sang ASS thì nó hardcode `PlayResX: 384,
PlayResY: 288`. Hệ toạ độ ảo đó không khớp khung hình thật, nên mọi giá trị
khai trong API phải quy đổi vòng vèo và ngưỡng tự xuống dòng của libass trở nên
khó đoán.

Vì thế với phụ đề `.srt` (đường đi thường gặp) service **tự dựng file `.ass`**
có `PlayResX`/`PlayResY` đúng bằng khung hình sẽ render. Khi đó 1 đơn vị ASS =
1 pixel: `font_size`, `margin_*`, `outline`, `shadow` khai bao nhiêu ra bấy
nhiêu. Dòng `Style` luôn ghi `ScaleX=100,ScaleY=100`, và mọi hiệu ứng phóng to
đều giữ `\fscx` bằng đúng `\fscy` → **chữ không bao giờ bị méo**. Đã đo bằng
frame thật: cùng `font_size=80`, glyph "O" ra đúng 54×53 px trên cả 720x1280,
1280x720, 1080x1080, 1920x816 và 540x960.

Cỡ chữ tự động bám theo **BỀ NGANG** chứ không phải chiều cao, vì bề ngang mới
là cạnh quyết định một dòng có vừa hay không:

```
font_px = bề_ngang × font_size_ratio          (mặc định 4%)
```

Bám chiều cao thì video dọc 1080x1920 ra chữ to gần gấp đôi video ngang cùng bề
ngang — đúng lỗi đã gặp trước đây.

File `.ass` do **người dùng tự đưa lên** thì giữ nguyên style riêng của họ, chỉ
ghi đè bằng `force_style`; lúc đó service đọc `PlayResY` khai trong chính file
đó rồi quy đổi từ pixel sang hệ toạ độ của nó, nên `font_size` vẫn mang đúng
một ý nghĩa dù phụ đề vào bằng đường nào.

Nếu client đổi `output.resolution` thì mọi số đo tính theo kích thước **sau khi
scale**, vì filter `scale` chạy trước `subtitles` trong cùng chuỗi filter.

**Tự xuống dòng.** Không phó mặc auto-wrap của libass (đã đo: ngưỡng kích hoạt
không ổn định — cùng một câu, lề 6% bề ngang thì không xuống dòng, 25% thì có).
Service tự ước lượng bề rộng theo **từng loại ký tự** — chữ hoa rộng gần gấp
đôi `i`/`l`, nên câu VIẾT HOA (rất hay gặp ở text quảng cáo) sẽ tràn khung nếu
tính bình quân — rồi tự chèn dấu ngắt và cân bằng độ dài các dòng.
`WrapStyle: 0` trong file tự sinh chỉ còn là lưới an toàn.

### 4.2 `clips` — cắt & ghép nhiều đoạn

Mỗi phần tử là một đoạn; **thứ tự liệt kê chính là thứ tự ghép**, và một video
nguồn được phép dùng lại nhiều lần.

| Field | Mặc định | Ghi chú |
|---|---|---|
| `source` | 1 | **Số hiệu** video (đánh từ 1 theo thứ tự gửi lên, §3.1) **hoặc tên file** |
| `source_video` | — | Tên gọi khác của `source`, để nhận thẳng `video_edit_script` |
| `start` | 0 | Giây, hoặc `"MM:SS"`, hoặc `"HH:MM:SS.mmm"` |
| `end` | null | Mốc kết thúc. null = tới hết video nguồn |
| `duration` | null | Thay cho `end`. Không được đặt cả hai |

**Field lạ bị bỏ qua, không báo lỗi.** Khác mọi options khác trong tài liệu này
(chúng đều `extra="forbid"` — gõ sai tên field là lỗi ngay), một đoạn trong
`clips` cho phép field bất kỳ ngoài bảng trên. Kịch bản dựng do pipeline AI
khác sinh ra hay đính kèm ghi chú riêng trên từng đoạn (ví dụ `vibe_note` mô tả
ý đồ dựng cảnh) — field đó vô hại và không cần lọc trước khi dán vào `clips`.

**Trỏ video bằng tên file.** Pipeline sinh kịch bản dựng chỉ biết tên file nó đã
xem, không biết ta sẽ nhận các video theo thứ tự nào — nên `source` nhận cả tên
file, khớp với tên file upload hoặc tên file trên Drive. Dò dần từ chặt tới
lỏng: khớp đúng → không phân biệt hoa thường → bỏ phần thư mục → chứa trong
tên. Tên khớp vào **nhiều** video thì báo lỗi chứ không đoán bừa (đoán sai là
ghép nhầm cảnh). Tên file chỉ đối chiếu được ở bước ghép — tên trên Drive phải
tải về mới biết — nên sai tên là job `failed`, không phải 4xx lúc gửi.

Ô `clips` trên form là lối tắt gõ nhanh, mỗi dòng một đoạn (ngăn bằng xuống
dòng hoặc `;` vì ô nhập một dòng của Swagger bóp mất newline):

```
1 00:00-00:05
2 0:10-0:18
1 65-72.5
3                # chỉ số hiệu = lấy trọn video đó
```

Khai trong `options.clips` thì JSON thắng lối tắt. Gửi **nhiều video mà không
khai `clips`** = ghép trọn cả loạt theo thứ tự đã gửi.

**Ô `clips` nhận cả hai cú pháp**, tự nhận ra dạng nào: mở đầu bằng `[` hoặc
`{` thì hiểu là JSON, còn lại là cú pháp gọn ở trên. Dạng JSON nhận thẳng kịch
bản của pipeline khác, ba kiểu đều được:

```jsonc
// 1. mảng đoạn
[{"source_video": "canh-rung.mp4", "start": "00:00", "end": "00:04"}]

// 2. object có video_edit_script (kèm video_srt nếu có)
{"video_edit_script": [...], "video_srt": "1\n00:00:00,000 --> ..."}

// 3. nguyên response của bước trước
{"job_id": "...", "status": "succeeded", "output": {"video_edit_script": [...]}}
```

Có `video_srt` trong đó thì nó được dùng làm phụ đề luôn, trừ khi bạn tự dán
`srt_text`. Khai trong `options.clips` thì JSON đó thắng ô `clips`.

**Chỉ tải video thật sự dùng tới.** Service quét thư mục ra danh sách tên, đối
chiếu với kịch bản, rồi chỉ tải đúng những video có mặt trong đó — thư mục 20
video mà kịch bản dùng 4 thì tải cả 20 vừa chậm vừa dễ hết RAM (`/tmp` trên
Cloud Run là tmpfs). Đối chiếu xong mới tải, nên sai tên là biết ngay chứ không
phải sau khi đã tải vài GB. Không khai `clips` = ghép trọn cả thư mục theo thứ
tự tên file.

Ghép bằng **một lệnh ffmpeg duy nhất**. Không dùng concat demuxer (nó đòi mọi
đoạn cùng codec/độ phân giải/fps — trong khi mục đích ở đây chính là ghép
video quay bằng máy khác nhau). Từng đoạn được chuẩn hoá ngay trong lệnh đó,
đỡ được một vòng ghi file trung gian (`/tmp` là RAM).

- **Khung hình**: lấy đúng khung của video chứa **đoạn đầu tiên**; khai
  `output.resolution` thì theo giá trị đó.
- **Không bao giờ kéo giãn hình**: `scale=...:force_original_aspect_ratio=decrease`
  kèm `pad` → đoạn khác tỉ lệ được thu vừa khung rồi chèn viền đen. `setsar=1`
  chốt pixel vuông.
- **fps**: cao nhất trong các nguồn được dùng (tối đa 60) để đoạn quay mượt
  không bị ép xuống theo đoạn quay giật.
- Đoạn **không có tiếng** được cấp một luồng `anullsrc` đúng độ dài, nếu không
  các filter ghép lệch số luồng giữa các đoạn và chết.
- Ghép là vòng encode ĐẦU; nếu sau đó còn burn phụ đề thì video bị encode lần
  hai, nên vòng đầu hạ `crf` đi 3 bậc để lần hai không ăn vào chất lượng.
- Xoá file nguồn ngay sau khi ghép xong — `/tmp` là RAM.

Video ghép xong tải về được luôn (không khai phụ đề/nhạc), hoặc chạy tiếp sang
bước ghép chữ và nhạc nền trong cùng một job.

#### `transition` — chuyển cảnh mượt giữa các đoạn

**Mặc định BẬT.** Cắt cứng giữa hai cảnh là đúng thứ gây cảm giác giật, nên
service tự dùng `xfade` (hình) + `acrossfade` (tiếng) để khung hình cũ mờ dần
trong khi khung hình mới hiện dần lên, chồng nhau `duration` giây — chỉ lùi về
`concat` (cắt cứng) khi tắt hiệu ứng hoặc khi có đúng một đoạn.

```jsonc
"transition": {
  "enabled": true,      // false = cắt cứng như trước
  "duration": 0.5,       // giây mỗi lần chồng hình, 0 < x ≤ 5.0
  "style": "fade"         // xem danh sách kiểu bên dưới
}
```

Kiểu (`style`) hỗ trợ: `fade`, `fadeblack`, `fadewhite`, `dissolve`,
`wipeleft`/`wiperight`/`wipeup`/`wipedown`,
`slideleft`/`slideright`/`slideup`/`slidedown`,
`smoothleft`/`smoothright`/`smoothup`/`smoothdown`,
`circlecrop`, `circleopen`, `circleclose`, `radial`, `distance`, `zoomin`,
`hblur`, `pixelize`.

**Không ăn quá nửa một đoạn ngắn.** `duration` bị kẹp lại còn tối đa 50% đoạn
NGẮN HƠN trong hai đoạn liền kề — đoạn 1 giây mà xin crossfade 2 giây thì tự
hạ xuống 0.5 giây, để hiệu ứng không nuốt gần hết một đoạn ngắn (nhìn còn giật
hơn cắt cứng). Vì mỗi lần chồng hình làm tổng thời lượng ngắn đi đúng
`duration` giây, %% tiến độ hiển thị trong lúc ghép cũng tính theo độ dài THẬT
đã trừ phần chồng — không tính theo tổng cộng dồn đơn thuần.

### 4.3 `intro` — text bìa cho TikTok

TikTok lấy **frame đầu tiên** của video làm ảnh bìa, nên mặc định `start = 0`
và `effect = "none"`: chữ phải hiện đủ ngay tại frame 0, hiệu ứng fade-in sẽ
làm ảnh bìa ra trống trơn.

Text nhiều dòng; **dòng đầu** được phóng to `headline_scale` lần (con số/lời
chào phải đập vào mắt trước). Ô `intro_text` trên form nhận thêm `|` và `\n` gõ
tay làm dấu xuống dòng, vì Swagger chỉ cho gõ một dòng.

Khác lời thoại ở một điểm quan trọng: text bìa **co nhỏ chữ cho vừa** thay vì
tự bẻ thêm dòng — người dùng đã tự chia dòng theo ý họ khi soạn ảnh bìa, bẻ
thêm là phá bố cục đó. Chỉ khi co tới 50% mà vẫn không vừa thì mới đành xuống
dòng.

Có phụ đề `.srt` thì text bìa được gộp luôn vào cùng file `.ass` (thêm một
`Style` riêng, `Layer: 1` để luôn nằm trên). Chỉ khi phụ đề là file `.ass` của
người dùng — không dựng lại được — thì text bìa mới tách ra `intro.ass` và
chồng lên bằng một filter `subtitles=` thứ hai.

### 4.4 `subtitle.effect` — hiệu ứng chữ

Chọn được trước khi render. Tất cả đều là override tag của ASS nên libass vẽ
chữ từ vector ở đúng độ phân giải output — không có bước phóng to bitmap nào,
chữ không bao giờ rỗ hay nhoè.

| Giá trị | Mô tả |
|---|---|
| `none` | Hiện/tắt thẳng, không hiệu ứng |
| `fade` | Mờ dần vào/ra (**mặc định** cho lời thoại) |
| `pop` | Nảy 70% → 104% → 100% rồi về đúng cỡ |
| `slide_up` | Trượt từ dưới lên đúng chỗ mà libass vốn dĩ đặt chữ |
| `typewriter` | Hiện dần từng ký tự như đang gõ |
| `glow` | Viền dày làm mờ thành quầng sáng |

`fade` kẹp lại tối đa 1/3 thời gian hiển thị mỗi đầu để câu ngắn không bị nuốt
mất. `typewriter` dùng `\k` (karaoke) nên style phải có `SecondaryColour` trong
suốt, nếu không ký tự chưa tới lượt vẫn hiện (chỉ khác màu).

---

### 4.5 `music.original_volume` — nhạc nền thay tiếng gốc

Ghép nhạc nền vào là **bỏ hẳn tiếng gốc của video**, không trộn chung: video
nguồn thường có tiếng gió, tiếng người nói chuyện, tiếng xe — trộn vào chỉ làm
bẩn nhạc.

`original_volume = null` (mặc định) nghĩa là "để service tự quyết":

| Tình huống | Tiếng gốc |
|---|---|
| Có ghép nhạc nền | bỏ hẳn (0.0) |
| Không ghép nhạc nền | giữ nguyên (1.0) |

Phân biệt `null` với `0` là cần thiết: nếu để mặc định thẳng bằng `0` thì video
KHÔNG kèm nhạc cũng bị tắt tiếng oan.

Khai số cụ thể để ép cứng — ví dụ `0.3` nếu muốn giữ lại chút tiếng hiện
trường. Chỉ khi `original_volume > 0` thì lệnh mới có `amix`, mới cần nguồn
`anullsrc` cho video câm, và `ducking` mới có tác dụng (không còn tiếng gốc thì
chẳng có gì để né).

---

## 5. Pipeline FFmpeg

### 5.1 Fast path (rất quan trọng cho hiệu năng)

Nếu **không burn phụ đề** và **không đổi resolution/fps/codec**
(`copy_video_if_possible = true`) → dùng `-c:v copy`, chỉ encode lại audio.
Render nhanh gấp 10–50 lần. Chỉ khi cần hardsub mới encode lại video.

### 5.2 Probe trước

Luôn chạy `ffprobe -v quiet -print_format json -show_format -show_streams` để lấy:
- `duration` (dùng tính % progress)
- có stream audio hay không → nếu **không có**, phải chèn `anullsrc` làm tiếng gốc,
  nếu không `amix` sẽ lỗi.
- `width`, `height`, `r_frame_rate`
- **`rotation`** ở `streams[].side_data_list[].rotation` (bản cũ: `tags.rotate`)

⚠️ Video quay bằng điện thoại thường lưu kích thước "coded" NGANG kèm Display
Matrix xoay 90°: ffprobe báo `1280x720` nhưng ffmpeg tự áp rotation
(`-autorotate` bật sẵn) nên frame thật đi vào filter graph là `720x1280`. Với
rotation 90 hoặc 270 phải **đảo `width`/`height`** trước khi dùng, nếu không cỡ
chữ phụ đề tính theo khung ngang sẽ ra chữ to gần gấp đôi trên video dọc.

### 5.3 Lệnh mẫu (đủ cả 3 thành phần)

```bash
ffmpeg -y -hide_banner -nostdin -loglevel error -progress pipe:1 -nostats \
  -i input.mp4 \
  -stream_loop -1 -ss 0 -i music.mp3 \
  -filter_complex "\
[0:v]subtitles=subs.srt:fontsdir=/app/fonts:force_style='FontName=Liberation Sans,FontSize=20.48,PrimaryColour=&H00FFFFFF&,OutlineColour=&H00000000&,BorderStyle=1,Outline=1.64,Shadow=0,Alignment=2,MarginV=40'[v];\
[0:a]volume=1.0,aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo[a0];\
[1:a]volume=0.18,afade=t=in:st=0:d=2,afade=t=out:st=191:d=3,aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo[a1];\
[a0][a1]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[a]" \
  -map "[v]" -map "[a]" \
  -c:v libx264 -preset veryfast -crf 23 -pix_fmt yuv420p \
  -c:a aac -b:a 192k -ar 48000 \
  -movflags +faststart -shortest \
  output.mp4
```

**Các điểm bắt buộc lưu ý:**

1. `amix` mặc định `normalize=1` sẽ làm nhỏ tiếng gốc đi một nửa → **luôn đặt
   `normalize=0`** rồi tự chỉnh bằng `volume`.
2. `afade=t=out:st=<duration - fade_out>` — phải tính từ duration lấy được ở ffprobe.
3. `-shortest` + `duration=first` để nhạc lặp không kéo dài video.
4. Đường dẫn trong filter `subtitles=` phải **escape**: `:` → `\:`, `'` → `\'`,
   `\` → `\\\\`. An toàn nhất: `chdir` vào workspace và dùng tên file tương đối
   thuần chữ (`subs.srt`).
5. Màu ASS là **BGR** ngược, định dạng `&HAABBGGRR&`. Phải viết hàm
   `hex_to_ass_color("#FF8800") -> "&H000088FF&"`.
6. `-pix_fmt yuv420p` bắt buộc để phát được trên Safari/QuickTime.
7. Nếu `ducking=true`: thay `amix` bằng
   `[a1][a0]sidechaincompress=threshold=0.05:ratio=8:attack=20:release=300[duck];[a0][duck]amix=...`
8. Nếu `mode="soft"`: bỏ filter subtitles, thêm `-i subs.srt -map 2:s -c:s mov_text`.

### 5.4 Parse progress

Đọc `stdout` của ffmpeg theo dòng, tìm `out_time_ms=<micro giây>`
(lưu ý: tên là `ms` nhưng đơn vị thực tế là **microsecond**).
`progress = min(99.0, out_time_ms / 1_000_000 / total_duration * 100)`.
Dòng `progress=end` báo kết thúc. Cập nhật `JobStore` (throttle 1 lần/giây).

`stderr` giữ trong `collections.deque(maxlen=200)` để đưa vào `error.detail`.

---

## 6. Google Drive

- Auth bằng **Service Account** (`GOOGLE_APPLICATION_CREDENTIALS` hoặc default
  credentials của Cloud Run). Người dùng phải share file/folder cho email của SA.
- Scope: `https://www.googleapis.com/auth/drive`
- Parse `file_id` từ các dạng link:
  - `https://drive.google.com/file/d/<ID>/view?usp=sharing`
  - `https://drive.google.com/open?id=<ID>`
  - `https://drive.google.com/uc?id=<ID>&export=download`
  - `https://docs.google.com/.../d/<ID>/...`
  - Chuỗi ID thuần (25–50 ký tự `[A-Za-z0-9_-]`)
- Download: `MediaIoBaseDownload` ghi thẳng ra file, chunk 8 MiB, cập nhật progress.
  **Luôn** truyền `supportsAllDrives=True` để hỗ trợ Shared Drive.
- Trước khi tải: gọi `files().get(fields="size,name,mimeType")`, nếu `size` >
  `MAX_DOWNLOAD_MB` → lỗi `413`.
- Upload: `MediaFileUpload(resumable=True, chunksize=8MiB)`, sau đó lấy
  `webViewLink`.

---

## 7. Biến môi trường

| Tên | Default | Ghi chú |
|---|---|---|
| `API_KEY` | *(bắt buộc)* | Bearer token |
| `WORK_DIR` | `/tmp/jobs` | |
| `MAX_FOLDER_VIDEOS` | 30 | Trần số video tải về từ một thư mục |
| `MAX_DOWNLOAD_MB` | `4096` | |
| `MAX_CONCURRENT_JOBS` | `1` | Semaphore cho ffmpeg |
| `MAX_QUEUED_JOBS` | `10` | |
| `JOB_TTL_SECONDS` | `3600` | Janitor xoá job xong sau N giây |
| `FFMPEG_THREADS` | `0` | 0 = auto |
| `FONTS_DIR` | `/app/fonts` | |
| `GOOGLE_APPLICATION_CREDENTIALS` | *(rỗng)* | Trên Cloud Run để trống, dùng ADC |
| `LOG_LEVEL` | `INFO` | |
| `PORT` | `8080` | Cloud Run inject |

---

## 8. Cấu trúc workspace mỗi job

```
/tmp/jobs/<job_id>/
├── input.<ext>        # một nguồn duy nhất
├── src1.<ext>         # hoặc nhiều nguồn để ghép, đánh số theo §3.1
├── src2.<ext>
├── merged.mp4         # kết quả ghép clip, thay input.<ext> làm đầu vào render
├── music.<ext>
├── subs_raw.<ext>     # phụ đề người dùng gửi lên, chưa chuẩn hoá
├── subs.srt           # đã chuẩn hoá encoding/newline/timing
├── styled.ass         # tự sinh: lời thoại + text bìa, PlayRes = khung hình
├── intro.ass          # chỉ khi phụ đề là file .ass của người dùng
└── output.mp4
```

File nguồn bị xoá ngay sau khi ghép xong, và mọi file input bị xoá ngay khi
render xong — `/tmp` là RAM.

Xoá toàn bộ thư mục khi job bị DELETE, hoặc khi janitor dọn quá TTL,
hoặc ngay khi job `failed` (giữ lại log trong RAM, không giữ file).

---

## 9. Yêu cầu code

- Python 3.12, FastAPI, `asyncio.create_subprocess_exec` (không dùng `subprocess.run`
  chặn event loop), không dùng `shell=True`.
- Type hints đầy đủ; Pydantic v2 cho mọi schema.
- Logging JSON một dòng ra stdout (Cloud Logging tự parse), luôn kèm `job_id`.
- Xử lý `SIGTERM`: Cloud Run cho 10s grace → huỷ job đang chạy, ghi log.
- `tests/` dùng pytest, mock `ffmpeg`. Bắt buộc test:
  - `parse_drive_id()` với cả 5 dạng link
  - `hex_to_ass_color()`
  - `build_ffmpeg_command()` snapshot cho 6 tổ hợp: video-only, video+srt,
    video+music, đủ ba, không có audio track, fast-path copy
  - `parse_progress_line()`
