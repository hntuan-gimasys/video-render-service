# Video Render Service

Backend FastAPI nhận video (upload hoặc link Google Drive) + phụ đề `.srt` + nhạc nền,
dùng FFmpeg ghép lại và trả về một video duy nhất. Chạy trên Cloud Run,
**không dùng database, không dùng Cloud Storage**.

Làm được:

- **Quét cả thư mục Google Drive**, rồi cắt & ghép các đoạn theo kịch bản —
  video khác tỉ lệ được chèn viền chứ không bao giờ bị kéo giãn, và các đoạn
  được **chuyển cảnh mượt (crossfade)** thay vì cắt cứng.
- **Burn phụ đề** với cỡ chữ, lề, viền tự co theo khung hình, tự xuống dòng cân
  đối, và **6 hiệu ứng chữ** chọn được trước khi render.
- **Text bìa** hiện tích tắc ở đầu video để làm ảnh bìa TikTok (nền tảng này lấy
  frame đầu tiên làm bìa).
- **Nhạc nền** có fade in/out, lặp, và ducking. Ghép nhạc vào là **bỏ hẳn tiếng
  gốc** của video (đặt `music.original_volume` nếu muốn giữ lại).

## Trạng thái

Đã cài đặt xong và chạy thật trên Cloud Run. `docs/SPEC.md` là nguồn sự thật
duy nhất về hành vi hệ thống; những chỗ code lệch khỏi SPEC đều có comment giải
thích tại chỗ kèm lý do đo được bằng ffmpeg thật.

## Cách dùng

Sáu ô, một job:

| Ô | Nội dung |
|---|---|
| `video_folder_url` | Link **thư mục** Drive chứa video nguồn |
| `clips` | Các đoạn cần cắt (JSON hoặc cú pháp gọn) |
| `srt_text` | Phụ đề dán thẳng |
| `intro_text` | Text bìa đầu video |
| `music_url` | Link Drive tới nhạc nền |
| `options` | Tinh chỉnh (§4 của SPEC) |

Nhớ chia sẻ thư mục Drive cho service account, nếu không lượt quét sẽ báo lỗi.

## Cắt & ghép

Ô `clips` nhận **kịch bản JSON** — trỏ video bằng **tên file** trong thư mục:

```json
{"video_edit_script": [{"source_video": "canh-rung.mp4", "start": "00:00", "end": "00:04"},
                       {"source_video": "ho-boi.mp4",   "start": "00:02", "end": "00:06"}],
 "video_srt": "1\n00:00:00,000 --> 00:00:05,000\nNoi thoi gian cham lai\n"}
```

Dán nguyên cả response của bước trước (`{"output": {...}}`) cũng chạy; có
`video_srt` thì nó thành phụ đề luôn.

Hoặc **cú pháp gọn** theo số hiệu video (thứ tự tên file trong thư mục):

```
1 00:00-00:05
2 0:10-0:18
3
```

Service chỉ tải những video kịch bản dùng tới. Sai tên file thì job dừng ngay
kèm danh sách video có thật, chứ không ghép nhầm cảnh. Không khai `clips` =
ghép trọn cả thư mục theo thứ tự tên file.

Chi tiết: `docs/SPEC.md` §4.2.

## Chuyển cảnh mượt giữa các đoạn

Mặc định BẬT: các đoạn được nối bằng crossfade (`xfade`/`acrossfade`) thay vì
cắt cứng — khung hình cũ mờ dần trong khi khung hình mới hiện dần lên. Tắt
hoặc đổi kiểu qua `options.transition`:

```json
{"transition": {"duration": 0.8, "style": "wipeleft"}}
```

`enabled: false` để quay về cắt cứng như trước. Đoạn ngắn thì `duration` tự hạ
xuống (tối đa 50% đoạn ngắn hơn) để hiệu ứng không nuốt gần hết một đoạn.

Chi tiết: `docs/SPEC.md` §4.2.

## Text bìa & hiệu ứng chữ

Ô `intro_text` là text hiện tích tắc ở đầu video, dùng làm ảnh bìa TikTok. Dùng
`|` làm dấu xuống dòng; **dòng đầu tự động to hơn hẳn**, và cả khối tự co nhỏ
cho vừa khung thay vì bị bẻ dòng.

Hiệu ứng cho lời thoại chọn qua `options.subtitle.effect`: `none`, `fade` (mặc
định), `pop`, `slide_up`, `typewriter`, `glow`. Tất cả đều do libass vẽ từ
vector nên chữ luôn nét, và mọi phép phóng to giữ tỉ lệ ngang = dọc nên **không
hiệu ứng nào làm méo chữ** — đã đo bằng frame thật: cùng `font_size=80`, glyph
"O" ra đúng 54×53 px trên mọi tỉ lệ khung từ 9:16 tới 21:9.

Chi tiết: `docs/SPEC.md` §4.1, §4.3, §4.4.

## Gửi phụ đề

Ba đường, ưu tiên từ trên xuống nếu gửi nhiều cùng lúc:

| Cách | Field | Khi nào dùng |
|---|---|---|
| Upload file | `srt_file` | Có sẵn file `.srt`/`.ass` |
| **Dán text** | `srt_text` | Đoạn AI sinh ra — dán thẳng, kể cả còn ```` ```srt ```` bọc ngoài |
| Link Drive | `srt_url` | File nằm trên Drive |

```bash
curl -X POST "$URL/api/jobs" -H "Authorization: Bearer $API_KEY" \
  -F "video_file=@clip.mp4" \
  --form-string 'srt_text=1
00:00:01,000 --> 00:00:03,000
Xin chào các bạn'
```

Cỡ chữ mặc định tự co theo bề ngang video nên video dọc (9:16) và ngang (16:9)
đều cho chữ cân đối — chi tiết ở `docs/SPEC.md` §4.1.

## Cấu trúc

```
.
├── app/
│   ├── main.py            # FastAPI routes, auth, lifespan
│   ├── config.py          # Settings từ env
│   ├── models.py          # Pydantic schemas
│   ├── jobs.py            # Job store in-memory + worker nền
│   ├── ffmpeg_runner.py   # Dựng lệnh ffmpeg + parse progress  ← lõi
│   ├── drive.py           # Google Drive download/upload
│   ├── subtitles.py       # Chuẩn hoá .srt, màu ASS
│   └── utils.py           # Logging, exception, file helper
├── docs/SPEC.md           # ĐỌC FILE NÀY TRƯỚC
├── fonts/                 # Font cho burn-in phụ đề
├── scripts/deploy.sh      # Deploy Cloud Run
├── scripts/smoke_test.sh
├── tests/
├── Dockerfile
└── PROMPT.md              # Prompt cho Claude Code
```

## Chạy local

```bash
make install
cp .env.example .env        # sửa API_KEY
make dev
```

Hoặc bằng Docker (giống môi trường production hơn, có sẵn ffmpeg):

```bash
make build && make run-docker
```

## Deploy

```bash
# tạo secret chứa API key
echo -n "$(openssl rand -hex 32)" | gcloud secrets create video-render-api-key --data-file=-

PROJECT_ID=my-project REGION=asia-southeast1 make deploy
```

Sau khi deploy, **share thư mục Google Drive** cho email service account
(script sẽ in ra ở cuối).

## Những đánh đổi cần biết trước

Vì bỏ database và Cloud Storage, hệ thống có các giới hạn sau — đây là hệ quả tất
yếu, không phải bug:

1. **Chỉ chạy được 1 instance.** Job state nằm trong RAM. Scale ngang là không thể
   trừ khi thêm Redis/Firestore.
2. **Job mất khi instance restart.** Cloud Run có thể thay instance bất cứ lúc nào.
   Client nên poll và sẵn sàng gửi lại.
3. **`/tmp` là RAM.** 16Gi memory không có nghĩa là 16GB chỗ trống cho file —
   phải trừ đi phần Python và buffer của ffmpeg. Thực tế an toàn: tổng file
   (input + nhạc + output) ≤ ~60% memory.
4. **Upload trực tiếp giới hạn 32 MiB.** Cloud Run chặn body > 32 MiB trên
   HTTP/1, và chặn ở tầng của nó nên app không thấy request. Không lách được
   bằng `--use-http2`: uvicorn chỉ nói HTTP/1.1, bật cờ đó là **toàn bộ request
   trả 502 Bad Gateway** (đã đo). Video lớn hơn 32 MiB phải dùng `video_url`
   (link Google Drive).
5. **Output phải tải về trước khi TTL hết** (mặc định 1 giờ), hoặc bật
   `delivery.upload_to_drive` để đẩy ngược lên Drive.

Nếu về sau khối lượng tăng, đường nâng cấp tự nhiên là: thêm GCS cho file +
Cloud Tasks cho hàng đợi, giữ nguyên phần `ffmpeg_runner.py`.
