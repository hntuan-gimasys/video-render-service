#!/usr/bin/env bash
# Render một video + một file phụ đề bằng service đang chạy.
#
#   bash scripts/render_one.sh <video> [srt] [nhac] [file-ket-qua]
#
# Ví dụ:
#   bash scripts/render_one.sh "D:/clip.mp4" "D:/phude.srt"
#   bash scripts/render_one.sh "D:/clip.mp4" "D:/phude.srt" "D:/nhac.mp3" ket_qua.mp4
#
# Dán phụ đề trực tiếp (đoạn AI sinh ra) thay vì truyền file — để "-" ở vị trí
# srt rồi đưa nội dung qua biến SRT_TEXT hoặc stdin:
#   SRT_TEXT=$(cat) bash scripts/render_one.sh clip.mp4 -      # gõ/dán rồi Ctrl+D
#   bash scripts/render_one.sh clip.mp4 - < phude_ai.txt
#
# Biến môi trường: BASE_URL (mặc định http://127.0.0.1:8080), API_KEY (devkey),
#                  OPTIONS (JSON options, xem docs/SPEC.md §4), SRT_TEXT
set -euo pipefail

BASE_URL="${BASE_URL:-http://127.0.0.1:8080}"
API_KEY="${API_KEY:-devkey}"
VIDEO="${1:-}"
SRT="${2:-}"
MUSIC="${3:-}"
OUT="${4:-ket_qua.mp4}"
OPTIONS="${OPTIONS:-{\"subtitle\":{\"font_size\":28,\"border_style\":4}}}"

if [ -z "$VIDEO" ]; then
  echo "Thiếu đường dẫn video. Dùng: bash scripts/render_one.sh <video> [srt] [nhac] [out]" >&2
  exit 2
fi

# srt = "-" nghĩa là dán nội dung phụ đề, không phải truyền file.
SRT_TEXT="${SRT_TEXT:-}"
if [ "$SRT" = "-" ]; then
  SRT=""
  if [ -z "$SRT_TEXT" ]; then
    echo "==> Dán nội dung phụ đề (SRT hoặc ASS), xong bấm Ctrl+D:" >&2
    SRT_TEXT=$(cat)
  fi
  [ -n "$SRT_TEXT" ] || { echo "Không nhận được nội dung phụ đề." >&2; exit 2; }
fi

for f in "$VIDEO" ${SRT:+"$SRT"} ${MUSIC:+"$MUSIC"}; do
  [ -f "$f" ] || { echo "Không thấy file: $f" >&2; exit 2; }
done
command -v jq >/dev/null || { echo "Cần jq (winget install jqlang.jq)" >&2; exit 2; }

echo "==> Kiểm tra service"
curl -sS --max-time 5 "${BASE_URL}/healthz" | jq -c . || {
  echo "Service chưa chạy. Mở terminal khác và chạy:" >&2
  echo '  API_KEY=devkey .venv/Scripts/python.exe -m uvicorn app.main:app --port 8080' >&2
  exit 1
}

echo "==> Gửi job"
ARGS=(-F "video_file=@${VIDEO}")
[ -n "$SRT" ]      && ARGS+=(-F "srt_file=@${SRT}")
# --form-string: nội dung dán có thể bắt đầu bằng @ hoặc < , -F sẽ hiểu nhầm
# thành "đọc từ file" và làm hỏng phụ đề.
[ -n "$SRT_TEXT" ] && ARGS+=(--form-string "srt_text=${SRT_TEXT}")
[ -n "$MUSIC" ]    && ARGS+=(-F "music_file=@${MUSIC}")

JOB=$(curl -sS -X POST "${BASE_URL}/api/jobs" \
  -H "Authorization: Bearer ${API_KEY}" \
  "${ARGS[@]}" -F "options=${OPTIONS}")
echo "$JOB" | jq .

JOB_ID=$(echo "$JOB" | jq -r '.job_id // empty')
[ -n "$JOB_ID" ] || { echo "Tạo job thất bại." >&2; exit 1; }

echo "==> Đang render (Ctrl+C để bỏ theo dõi, job vẫn chạy tiếp)"
while true; do
  STATE=$(curl -sS "${BASE_URL}/api/jobs/${JOB_ID}" -H "Authorization: Bearer ${API_KEY}")
  printf '\r  %-58s' "$(echo "$STATE" | jq -r '"\(.status)  \(.progress)%  \(.stage_message)"')"
  case "$(echo "$STATE" | jq -r .status)" in
    succeeded) echo; break ;;
    failed)    echo; echo "$STATE" | jq .error >&2; exit 1 ;;
    cancelled) echo; echo "Job đã bị huỷ" >&2; exit 1 ;;
  esac
  sleep 2
done

echo "==> Tải kết quả về ${OUT}"
curl -sS -o "$OUT" -H "Authorization: Bearer ${API_KEY}" \
  "${BASE_URL}/api/jobs/${JOB_ID}/download"
ls -lh "$OUT"
command -v ffprobe >/dev/null && ffprobe -hide_banner -v error \
  -show_entries format=duration,size -show_entries stream=codec_type,codec_name,width,height \
  -of default=nw=1 "$OUT"
echo "==> Xong. Mở file: $OUT"
