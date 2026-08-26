FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    FONTS_DIR=/app/fonts

# ffmpeg + libass (burn-in phụ đề) + bộ font Unicode có dấu tiếng Việt
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        fonts-dejavu-core \
        fonts-liberation2 \
        fontconfig \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
# Bỏ phần dev dependencies khi build image production
RUN sed '/^# dev/,$d' requirements.txt > requirements.prod.txt \
    && pip install --no-cache-dir -r requirements.prod.txt

COPY app/ ./app/
COPY fonts/ ./fonts/

# Đăng ký font tuỳ chỉnh với fontconfig để libass tìm thấy theo FontName
RUN fc-cache -f /app/fonts || true

RUN useradd -m -u 1000 appuser && mkdir -p /tmp/jobs && chown -R appuser /tmp/jobs /app
USER appuser

ENV PORT=8080
EXPOSE 8080

# 1 worker duy nhất: job store nằm trong RAM của process
CMD exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --workers 1 --timeout-keep-alive 75
