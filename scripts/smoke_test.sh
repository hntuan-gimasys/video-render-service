#!/usr/bin/env bash
# Test nhanh service đang chạy ở BASE_URL.
set -euo pipefail
BASE_URL="${BASE_URL:-http://localhost:8080}"
API_KEY="${API_KEY:-devkey}"

echo "== healthz =="
curl -sS "${BASE_URL}/healthz" | jq .

echo "== tạo job =="
JOB=$(curl -sS -X POST "${BASE_URL}/api/jobs" \
  -H "Authorization: Bearer ${API_KEY}" \
  -F "video_file=@samples/input.mp4" \
  -F "srt_file=@samples/subs.srt" \
  -F "music_file=@samples/music.mp3" \
  -F 'options={"subtitle":{"font_size":28},"music":{"volume":0.15}}')
echo "$JOB" | jq .
JOB_ID=$(echo "$JOB" | jq -r .job_id)

echo "== poll =="
while true; do
  S=$(curl -sS "${BASE_URL}/api/jobs/${JOB_ID}" -H "Authorization: Bearer ${API_KEY}")
  echo "$S" | jq -c '{status,progress,stage_message}'
  ST=$(echo "$S" | jq -r .status)
  [ "$ST" = "succeeded" ] && break
  [ "$ST" = "failed" ] && { echo "$S" | jq .error; exit 1; }
  sleep 3
done

echo "== tải về =="
curl -sS -o out.mp4 "${BASE_URL}/api/jobs/${JOB_ID}/download" -H "Authorization: Bearer ${API_KEY}"
ls -lh out.mp4 && ffprobe -hide_banner out.mp4
