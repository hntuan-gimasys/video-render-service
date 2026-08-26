#!/usr/bin/env bash
# Sinh file mẫu cho scripts/smoke_test.sh bằng ffmpeg lavfi (không cần tải gì).
# Dùng: bash scripts/make_samples.sh [thư mục đích, mặc định ./samples]
set -euo pipefail

OUT_DIR="${1:-samples}"
DURATION="${DURATION:-6}"
mkdir -p "$OUT_DIR"

echo "==> Sinh video mẫu ${DURATION}s (320x240, có tiếng sine 440Hz)"
ffmpeg -y -hide_banner -loglevel error \
  -f lavfi -i "testsrc=duration=${DURATION}:size=320x240:rate=25" \
  -f lavfi -i "sine=frequency=440:duration=${DURATION}" \
  -c:v libx264 -pix_fmt yuv420p -c:a aac -shortest \
  "${OUT_DIR}/input.mp4"

echo "==> Sinh video mẫu KHÔNG có audio track (để thử nhánh anullsrc)"
ffmpeg -y -hide_banner -loglevel error \
  -f lavfi -i "testsrc=duration=${DURATION}:size=320x240:rate=25" \
  -c:v libx264 -pix_fmt yuv420p \
  "${OUT_DIR}/input_no_audio.mp4"

echo "==> Sinh nhạc nền mẫu 4s"
ffmpeg -y -hide_banner -loglevel error \
  -f lavfi -i "sine=frequency=220:duration=4" \
  "${OUT_DIR}/music.mp3"

echo "==> Sinh phụ đề mẫu (UTF-8 BOM + CRLF + tiếng Việt có dấu, giống file thực tế)"
printf '\xef\xbb\xbf1\r\n00:00:01,000 --> 00:00:03,000\r\nXin ch\xc3\xa0o th\xe1\xba\xbf gi\xe1\xbb\x9bi\r\n\r\n2\r\n00:00:03,500 --> 00:00:05,500\r\nD\xc3\xb2ng th\xe1\xbb\xa9 hai c\xc3\xb3 d\xe1\xba\xa5u\r\n' \
  > "${OUT_DIR}/subs.srt"

echo
echo "==> Xong:"
ls -lh "${OUT_DIR}"
