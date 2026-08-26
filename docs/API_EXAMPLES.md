# Ví dụ gọi API

## Tạo job từ upload trực tiếp

```bash
curl -X POST https://<service-url>/api/jobs \
  -H "Authorization: Bearer $API_KEY" \
  -F "video_file=@video.mp4" \
  -F "srt_file=@subs.srt" \
  -F "music_file=@bgm.mp3" \
  -F 'options={
        "subtitle": {"font_name":"Be Vietnam Pro","font_size":28,"border_style":4},
        "music": {"volume":0.15,"fade_out":4},
        "output": {"crf":21,"preset":"medium"}
      }'
```

## Tạo job từ link Google Drive

```bash
curl -X POST https://<service-url>/api/jobs \
  -H "Authorization: Bearer $API_KEY" \
  -F "video_url=https://drive.google.com/file/d/1AbC.../view" \
  -F "srt_file=@subs.srt" \
  -F 'options={"delivery":{"upload_to_drive":true,"drive_folder_id":"1XyZ..."}}'
```

## Theo dõi tiến độ

```bash
watch -n2 "curl -s https://<service-url>/api/jobs/$JOB_ID \
  -H 'Authorization: Bearer $API_KEY' | jq '{status,progress}'"
```

## Tải kết quả

```bash
curl -o final.mp4 -H "Authorization: Bearer $API_KEY" \
  https://<service-url>/api/jobs/$JOB_ID/download
```

## Chỉ ghép nhạc, không phụ đề (fast path, dùng -c:v copy)

```bash
curl -X POST https://<service-url>/api/jobs \
  -H "Authorization: Bearer $API_KEY" \
  -F "video_file=@video.mp4" \
  -F "music_file=@bgm.mp3" \
  -F 'options={"subtitle":{"enabled":false}}'
```

---

## Ghép các đoạn từ một thư mục Drive

```bash
curl -X POST "$BASE/api/jobs" \
  -H "Authorization: Bearer $API_KEY" \
  -F "video_folder_url=https://drive.google.com/drive/folders/<folder-id>" \
  -F "clips=$(cat buoc-truoc.json)" \
  -F "intro_text=2tr9/người|3N2Đ tại GARRYA MÙ CANG CHẢI" \
  -F "music_url=https://drive.google.com/file/d/<music-id>/view"
```

Trong đó `buoc-truoc.json` là output của pipeline sinh nội dung:

```json
{
  "video_srt": "1\n00:00:00,000 --> 00:00:05,000\nRời xa ồn ào đô thị\n",
  "video_edit_script": [
    { "source_video": "canh-rung-som.mp4", "start": "00:00", "end": "00:04" },
    { "source_video": "ho-boi-vo-cuc.mp4", "start": "00:02", "end": "00:06" }
  ]
}
```

`source_video` khớp với **tên file trong thư mục Drive**. Sai tên thì job dừng
ngay kèm danh sách video có thật, chứ không ghép nhầm cảnh — và dừng TRƯỚC khi
tải byte nào.

Hoặc cú pháp gọn theo số hiệu video (thứ tự tên file trong thư mục):

```bash
curl -X POST "$BASE/api/jobs" \
  -H "Authorization: Bearer $API_KEY" \
  -F "video_folder_url=https://drive.google.com/drive/folders/<folder-id>" \
  -F $'clips=1 00:00-00:05\n2 0:10-0:18\n3'
```

## Text bìa cho TikTok + hiệu ứng chữ

TikTok lấy frame đầu tiên làm ảnh bìa nên text bìa hiện ngay từ giây 0. Ô
`intro_text` nhận `|` làm dấu xuống dòng; dòng đầu tự động to hơn hẳn.

```bash
curl -X POST "$BASE/api/jobs" \
  -H "Authorization: Bearer $API_KEY" \
  -F "video_folder_url=https://drive.google.com/drive/folders/<folder-id>" \
  -F "srt_text=$(cat phude.srt)" \
  -F "intro_text=2tr9/người|3N2Đ tại GARRYA MÙ CANG CHẢI|(Free nâng hạng Villa Hồ Bơi riêng)" \
  -F 'options={"subtitle":{"effect":"pop"}}'
```

Hiệu ứng chọn được: `none`, `fade` (mặc định), `pop`, `slide_up`, `typewriter`,
`glow`. Đổi màu/cỡ text bìa qua `options.intro`:

```json
{
  "intro": {
    "text": "2tr9/người\n3N2Đ tại GARRYA MÙ CANG CHẢI",
    "duration": 2.5,
    "primary_color": "#FFF200",
    "position_ratio": 0.44,
    "headline_scale": 1.55
  }
}
```
