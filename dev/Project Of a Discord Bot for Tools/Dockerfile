FROM python:3.10-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DEBIAN_FRONTEND=noninteractive

WORKDIR /app

# Runtime dependencies used by media extraction and OCR.
# Store APT archives in /tmp instead of /var/cache/apt. Some Docker/BuildKit
# environments expose /var/cache/apt as read-only during image builds.
RUN mkdir -p /tmp/apt-archives \
    && apt-get -o Dir::Cache::Archives=/tmp/apt-archives update \
    && apt-get -o Dir::Cache::Archives=/tmp/apt-archives install -y --no-install-recommends \
        ffmpeg \
        tesseract-ocr \
        ca-certificates \
    && rm -rf /tmp/apt-archives /var/lib/apt/lists/*

COPY requirements.txt ./
RUN python -m pip install --upgrade pip \
    && pip install -r requirements.txt

# Copy the application. Secrets/config.py and persistent data are mounted separately.
COPY . /app

RUN mkdir -p /app/data /app/media

CMD ["python", "bot.py"]
