# Prompt cho Claude Code

Mở terminal tại thư mục gốc dự án, chạy `claude`, rồi dán toàn bộ phần trong khung
dưới đây.

---

```
Bạn đang ở thư mục gốc của một dự án backend Python đã được scaffold sẵn. Nhiệm vụ
của bạn là implement toàn bộ code thật.

## Bước 0 — Đọc trước khi viết bất cứ dòng nào

Đọc `docs/SPEC.md` từ đầu đến cuối. Đó là nguồn sự thật duy nhất: API contract,
options schema, pipeline FFmpeg, bảng mã lỗi, biến môi trường. Sau đó đọc tất cả
file trong `app/` — mỗi file là một stub chứa docstring mô tả chính xác những gì
cần có trong file đó. Đọc luôn `Dockerfile` và `scripts/deploy.sh` để biết môi
trường runtime.

Sau khi đọc xong, tóm tắt lại cho tôi trong 10 dòng bạn hiểu hệ thống làm gì và
liệt kê những chỗ trong SPEC mà bạn thấy mơ hồ hoặc mâu thuẫn. ĐỪNG viết code ở
bước này. Chờ tôi xác nhận.

## Bối cảnh hệ thống

Backend FastAPI nhận: 1 video (upload multipart hoặc link Google Drive), 1 file
`.srt` tuỳ chọn, 1 file nhạc nền tuỳ chọn. Dùng FFmpeg burn phụ đề vào video và
trộn nhạc nền với tiếng gốc, xuất ra 1 file mp4. Deploy trên Cloud Run với ràng
buộc cứng: KHÔNG database, KHÔNG Cloud Storage. Do đó job state nằm trong RAM của
process duy nhất, file trung gian nằm ở `/tmp` (là tmpfs — tức RAM), service chạy
`--max-instances=1 --no-cpu-throttling`.

## Thứ tự implement

Làm theo đúng thứ tự này, mỗi bước xong thì chạy test rồi mới sang bước sau. Đừng
viết cả 8 file một lượt.

1. `app/utils.py` — exceptions, logging JSON, helper file. Nền móng cho mọi thứ khác.
2. `app/config.py` — Settings.
3. `app/models.py` — toàn bộ Pydantic schema.
4. `app/subtitles.py` — normalize_srt, hex_to_ass_color, build_force_style.
   Viết test ngay cho `hex_to_ass_color` (nhớ ASS dùng BGR ngược: `#FF8800` →
   `&H000088FF&`).
5. `app/ffmpeg_runner.py` — QUAN TRỌNG NHẤT. `build_ffmpeg_command()` phải là hàm
   thuần, chỉ nhận input và trả về `list[str]`, không I/O, không side effect. Viết
   test snapshot cho 6 tổ hợp liệt kê ở SPEC §9 trước khi implement phần chạy
   process.
6. `app/drive.py` — parse ID, download, upload. Bọc mọi call googleapiclient bằng
   `asyncio.to_thread`.
7. `app/jobs.py` — JobStore, run_job, janitor, cancel.
8. `app/main.py` — routes, auth, lifespan, exception handler.

## Yêu cầu kỹ thuật bắt buộc

- Python 3.12, type hints đầy đủ ở mọi hàm public, Pydantic v2.
- Chạy process bằng `asyncio.create_subprocess_exec`. TUYỆT ĐỐI không dùng
  `subprocess.run`, không `shell=True`, không nối chuỗi lệnh bằng f-string.
  Mọi đường dẫn từ input người dùng phải đi vào argv như một phần tử riêng.
- Không có thao tác blocking nào trong event loop: đọc/ghi file lớn, gọi Drive API,
  `shutil.rmtree` — tất cả bọc `asyncio.to_thread`.
- Xử lý `SIGTERM`: Cloud Run chỉ cho 10 giây. Terminate ffmpeg, ghi log, thoát sạch.
- Logging JSON một dòng ra stdout, mọi log liên quan job phải kèm field `job_id`.
- Không nuốt exception. Mọi lỗi map về `AppError` với `code` đúng theo bảng SPEC §3.6.
- File `/tmp` phải được dọn trong `finally`, kể cả khi job fail hay bị cancel.

## Những cái bẫy bạn phải xử lý đúng — tôi sẽ kiểm tra từng cái

1. `amix` mặc định `normalize=1` làm tiếng gốc nhỏ đi một nửa. Phải đặt `normalize=0`.
2. Video KHÔNG CÓ audio track: `amix` sẽ fail. Phải phát hiện bằng ffprobe và chèn
   `anullsrc=channel_layout=stereo:sample_rate=48000` làm input tiếng gốc.
3. `out_time_ms` của `-progress` thực chất là MICROsecond, không phải millisecond.
4. Đường dẫn trong filter `subtitles=` cần escape ký tự `:` `'` `\`. Cách an toàn
   nhất: `cwd` vào workspace của job và dùng tên file tương đối cố định (`subs.srt`).
5. Màu ASS là `&HAABBGGRR&` — đảo thứ tự RGB.
6. Thiếu `-pix_fmt yuv420p` thì video không phát được trên Safari/QuickTime.
7. Fast path: nếu không burn phụ đề và không đổi resolution/fps thì dùng `-c:v copy`,
   nhanh hơn hàng chục lần. Đừng bỏ qua tối ưu này.
8. `afade=t=out` cần `st = duration - fade_out`, lấy duration từ ffprobe.
9. File `.srt` thực tế hay có BOM, CRLF, hoặc encoding CP1258/latin-1. Phải tự dò.
10. `MediaIoBaseDownload` và `files().get()` phải truyền `supportsAllDrives=True`,
    nếu không sẽ fail với Shared Drive.

## Test

Dùng pytest + pytest-asyncio. Bắt buộc phủ những case ở SPEC §9. Test cho
`build_ffmpeg_command` không được gọi ffmpeg thật — so sánh argv sinh ra với giá
trị kỳ vọng. Tạo `tests/conftest.py` với fixture options mặc định.

Cuối cùng thêm một test tích hợp `tests/test_integration.py` được đánh dấu
`@pytest.mark.skipif(shutil.which("ffmpeg") is None)`: dùng
`ffmpeg -f lavfi -i testsrc=duration=5:size=320x240 -f lavfi -i sine=d=5` để sinh
video mẫu, sinh một `.srt` 2 dòng, chạy pipeline thật, assert output tồn tại và
ffprobe đọc được duration ≈ 5s.

## Quy tắc làm việc

- Sau mỗi bước, chạy `python -m pytest -q` và `python -c "import app.main"` để chắc
  chắn không lỗi import, rồi báo tôi biết bạn đã xong bước nào.
- Nếu SPEC thiếu thông tin, HỎI tôi thay vì tự bịa. Đừng thêm tính năng ngoài SPEC.
- Đừng sửa `docs/SPEC.md`. Nếu bạn thấy SPEC sai, nói cho tôi biết để tôi sửa.
- Giữ mỗi file dưới 400 dòng. Nếu vượt, tách module và nói tôi biết lý do.
- Viết comment bằng tiếng Việt cho phần logic phức tạp, tên biến/hàm bằng tiếng Anh.

Bắt đầu bằng Bước 0.
```

---

## Sau khi Claude Code làm xong

Prompt tiếp theo để kiểm tra chất lượng:

```
Bây giờ review lại toàn bộ code bạn vừa viết như một reviewer khó tính. Kiểm tra
cụ thể 10 cái bẫy trong prompt trước — với mỗi cái, chỉ ra chính xác dòng code nào
xử lý nó. Nếu cái nào chưa xử lý, sửa ngay. Sau đó chạy:

  docker build -t vrs:local .
  docker run --rm -p 8080:8080 -e API_KEY=devkey --memory=4g vrs:local

và dùng scripts/smoke_test.sh với file mẫu tự sinh bằng ffmpeg lavfi để xác nhận
pipeline chạy end-to-end trong container.
```
