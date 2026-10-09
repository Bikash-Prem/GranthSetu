# Build context: repository root (see docker-compose.yml)
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 DATA_DIR=/app/data \
    STORAGE_DIR=/data/files OCR_LANGS=eng+kan+hin
# OCR for scanned books (English, Kannada, Hindi). Remove these packages to disable OCR.
RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-kan tesseract-ocr-hin \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY backend/requirements.txt .
RUN pip install -r requirements.txt
COPY backend/granthsetu ./granthsetu
COPY backend/scripts ./scripts
COPY data ./data
RUN useradd -r -u 10001 app && mkdir -p /data/files && chown -R app /app /data/files && chmod 700 /data/files
USER app
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --retries=10 CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/api/health')" || exit 1
CMD ["uvicorn", "granthsetu.api:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
