# syntax=docker/dockerfile:1
FROM ubuntu:24.04

ARG RK_VARIANT=false

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip python3-venv \
    software-properties-common gnupg ca-certificates curl tzdata \
 && if [ "${RK_VARIANT}" = "true" ]; then \
    add-apt-repository -y ppa:jjriek/rockchip-multimedia; \
 fi \
 && apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
 && if [ "${RK_VARIANT}" = "true" ]; then \
    apt-get install -y --no-install-recommends librga2 libdrm2; \
 fi \
 && apt-get remove -y software-properties-common gnupg \
 && apt-get autoremove -y \
 && apt-get clean && rm -rf /var/lib/apt/lists/*

RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

WORKDIR /app
COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY app ./app
COPY README.md ./README.md

ENV MEDIA_ENGINE_DATA_ROOT=/data \
    MEDIA_ENGINE_INPUT_DIR=/data/input \
    MEDIA_ENGINE_OUTPUT_DIR=/data/output \
    MEDIA_ENGINE_WORK_DIR=/data/work \
    MEDIA_ENGINE_TEMP_DIR=/tmp/media-engine \
    MEDIA_ENGINE_SELF_TEST_ON_STARTUP=true

RUN mkdir -p /data/input /data/output /data/work /tmp/media-engine
EXPOSE 8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
