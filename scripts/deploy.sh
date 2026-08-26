#!/usr/bin/env bash
# Deploy Video Render Service lên Cloud Run.
# Sửa các biến bên dưới rồi chạy:  bash scripts/deploy.sh
#
# CẢNH BÁO nếu chạy trên Windows Git Bash (MSYS): Git Bash tự "dịch" mọi
# argument trông giống đường dẫn POSIX tuyệt đối (bắt đầu bằng "/") sang dạng
# Windows trước khi đưa cho gcloud.exe — kể cả khi nó nằm giữa một chuỗi lớn
# như "--set-env-vars=WORK_DIR=/tmp/jobs,...". Hậu quả: gcloud "chạy thành
# công", KHÔNG báo lỗi gì, nhưng WORK_DIR trên container lại thành một đường
# dẫn Windows vô nghĩa (đã xảy ra thật khi soạn script này). export
# MSYS_NO_PATHCONV=1 hay MSYS2_ARG_CONV_EXCL đều không sửa triệt để (có thể
# làm hỏng luôn cách gcloud.cmd tự định vị script Python của nó). Cách chắc
# ăn nhất: chạy script này trong WSL, hoặc trong PowerShell/cmd.exe thật (không
# qua Git Bash), hoặc trên Linux/macOS. Sau khi chạy xong, LUÔN kiểm tra lại:
#   gcloud run services describe "$SERVICE" --region="$REGION" \
#     --format="yaml(spec.template.spec.containers[0].env)"
# và tự mắt xác nhận WORK_DIR đúng là "/tmp/jobs".
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-your-gcp-project}"
REGION="${REGION:-asia-southeast1}"          # Singapore, gần VN nhất
SERVICE="${SERVICE:-video-render-service}"
REPO="${REPO:-containers}"                    # Artifact Registry repo
SA_NAME="${SA_NAME:-video-render-sa}"
# Đặt SA_EMAIL để dùng một service account đã có sẵn (ví dụ SA compute mặc
# định) thay vì tạo SA riêng mới — khi đó bỏ qua luôn bước tạo SA bên dưới.
# Mặc định (không đặt SA_EMAIL) là tạo SA riêng chỉ có đúng quyền đọc secret,
# theo nguyên tắc least-privilege — nên giữ mặc định này trừ khi có lý do cụ thể.
SA_EMAIL_OVERRIDE="${SA_EMAIL:-}"
if [ -n "${SA_EMAIL_OVERRIDE}" ]; then
  SA_EMAIL="${SA_EMAIL_OVERRIDE}"
else
  SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
fi
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/${SERVICE}:$(date +%Y%m%d-%H%M%S)"

# ---- API_KEY: đọc từ Secret Manager (khuyến nghị) ------------------------
SECRET_NAME="${SECRET_NAME:-video-render-api-key}"

echo "==> Project: ${PROJECT_ID} | Region: ${REGION}"

gcloud config set project "${PROJECT_ID}" >/dev/null

echo "==> Bật API cần thiết"
gcloud services enable \
  run.googleapis.com \
  artifactregistry.googleapis.com \
  cloudbuild.googleapis.com \
  drive.googleapis.com \
  secretmanager.googleapis.com

echo "==> Tạo Artifact Registry (bỏ qua nếu đã có)"
gcloud artifacts repositories create "${REPO}" \
  --repository-format=docker --location="${REGION}" \
  --description="Container images" 2>/dev/null || true

if [ -z "${SA_EMAIL_OVERRIDE}" ]; then
  echo "==> Tạo service account (bỏ qua nếu đã có)"
  gcloud iam service-accounts create "${SA_NAME}" \
    --display-name="Video Render Service" 2>/dev/null || true
else
  echo "==> Dùng service account có sẵn: ${SA_EMAIL}"
fi

echo "==> Tạo secret API_KEY nếu chưa có"
if ! gcloud secrets describe "${SECRET_NAME}" >/dev/null 2>&1; then
  GENERATED_KEY=$(openssl rand -hex 32)
  printf '%s' "${GENERATED_KEY}" | gcloud secrets create "${SECRET_NAME}" --data-file=-
  echo "    Đã tạo API_KEY mới, LƯU LẠI NGAY (chỉ hiện một lần ở đây):"
  echo "    ${GENERATED_KEY}"
else
  echo "    Secret đã có sẵn, giữ nguyên giá trị."
fi

echo "==> Cấp quyền đọc secret cho SA"
SECRET_ACCESS_GRANTED=1
if ! gcloud secrets add-iam-policy-binding "${SECRET_NAME}" \
       --member="serviceAccount:${SA_EMAIL}" \
       --role="roles/secretmanager.secretAccessor" 2>/tmp/vrs_secret_iam_err.log; then
  SECRET_ACCESS_GRANTED=0
  echo "    KHÔNG gán được quyền (thường do tài khoản của bạn thiếu quyền"
  echo "    secretmanager.secrets.setIamPolicy — chỉ Owner/Admin cấp được):"
  tail -1 /tmp/vrs_secret_iam_err.log
fi

BASE_ENV_VARS="WORK_DIR=/tmp/jobs,MAX_DOWNLOAD_MB=4096,MAX_FOLDER_VIDEOS=30,MAX_CONCURRENT_JOBS=1,JOB_TTL_SECONDS=3600,LOG_LEVEL=INFO"
if [ "${SECRET_ACCESS_GRANTED}" = "1" ]; then
  DEPLOY_ENV_FLAGS=(--set-env-vars="${BASE_ENV_VARS}" --set-secrets="API_KEY=${SECRET_NAME}:latest")
else
  # Không đọc lại được giá trị secret cũ (cùng thiếu quyền) -> sinh key MỚI,
  # truyền trực tiếp qua env var. Kém an toàn hơn Secret Manager: giá trị hiện
  # nguyên văn trong `gcloud run services describe` / Cloud Console, ai có
  # quyền xem cấu hình service (ví dụ role run.viewer/run.admin) cũng đọc được.
  # Vẫn là cách duy nhất chạy được khi thiếu quyền IAM ở bước trên — sửa lại
  # bằng --set-secrets khi có ai đó (Owner) cấp secretAccessor cho SA.
  FALLBACK_KEY=$(openssl rand -hex 32)
  echo "    -> Dùng key mới qua biến môi trường (kém an toàn hơn), LƯU LẠI NGAY:"
  echo "    ${FALLBACK_KEY}"
  DEPLOY_ENV_FLAGS=(--set-env-vars="${BASE_ENV_VARS},API_KEY=${FALLBACK_KEY}")
fi

echo "==> Build image"
gcloud builds submit --tag "${IMAGE}" .

echo "==> Deploy"
gcloud run deploy "${SERVICE}" \
  --image="${IMAGE}" \
  --region="${REGION}" \
  --platform=managed \
  --service-account="${SA_EMAIL}" \
  --execution-environment=gen2 \
  --cpu=4 \
  --memory=16Gi \
  --cpu-boost \
  --no-cpu-throttling \
  --timeout=3600 \
  --concurrency=20 \
  --min-instances=1 \
  --max-instances=1 \
  --allow-unauthenticated \
  "${DEPLOY_ENV_FLAGS[@]}"

echo
echo "==> Xong. URL:"
gcloud run services describe "${SERVICE}" --region="${REGION}" --format='value(status.url)'
cat <<EOF

LƯU Ý QUAN TRỌNG:
  1. Share thư mục/file Google Drive cho email: ${SA_EMAIL}
$([ -n "${SA_EMAIL_OVERRIDE}" ] && echo "     CẢNH BÁO: đây là SA có sẵn (không phải SA riêng least-privilege) —
     nếu nó có role Editor trên project, share Drive folder cho nó nghĩa là
     bất kỳ ai/service nào khác đang dùng chung SA này cũng đọc được folder đó.")
  2. --max-instances=1 là BẮT BUỘC vì job state nằm trong RAM (không có DB).
  3. --no-cpu-throttling là BẮT BUỘC để ffmpeg chạy nền sau khi request đã trả về.
  4. /tmp là RAM disk: 16Gi memory ≈ xử lý được video input + output tổng ~10GB.
  5. --allow-unauthenticated: bắt buộc phải vậy vì app tự kiểm API_KEY qua header
     Authorization (SPEC §3.2). Nếu bật --no-allow-unauthenticated, Cloud Run sẽ
     chiếm luôn header Authorization để đòi Google ID token, đụng với API_KEY và
     MỌI request sẽ bị 401 kể cả khi API_KEY đúng. An ninh chỉ còn dựa vào
     API_KEY (đủ mạnh vì sinh bằng openssl rand -hex 32) + HTTPS của Cloud Run.
  6. KHÔNG dùng --use-http2: uvicorn (xem Dockerfile CMD) chỉ nói HTTP/1.1,
     không có h2. Bật --use-http2 làm Cloud Run proxy xuống container bằng
     HTTP/2 và toàn bộ request bị 502 Bad Gateway ngay từ Google Frontend,
     kể cả /docs hay /healthz — đã tự kiểm chứng lúc soạn script này.
$([ "${SECRET_ACCESS_GRANTED}" = "0" ] && echo "  7. API_KEY đang truyền qua --set-env-vars (KHÔNG qua Secret Manager) vì
     tài khoản chạy script thiếu quyền secretmanager.secrets.setIamPolicy.
     Giá trị hiện nguyên văn trong cấu hình service — ai xem được service này
     (role run.viewer/run.admin) cũng đọc được key. Nhờ Owner của project chạy:
       gcloud secrets add-iam-policy-binding ${SECRET_NAME} \\
         --member=serviceAccount:${SA_EMAIL} --role=roles/secretmanager.secretAccessor
     rồi deploy lại (script sẽ tự chuyển sang --set-secrets khi bước gán quyền
     ở trên thành công).")
EOF
