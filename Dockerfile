# Paradise Bot — production image.
#
#   docker build -t paradisebot .
#   docker run -d --name paradise --env-file .env -v "$PWD/data:/app/data" paradisebot
#
# Bake the wake-word model in (no download, no network at run time):
#   docker build --build-arg WHISPER_MODEL=small -t paradisebot .
#   docker run … -e VOICE_WAKE_STT_MODEL_DIR=/opt/whisper \
#                -e VOICE_WAKE_STT_OFFLINE=true paradisebot
#
# The runtime image is non-root, ships ffmpeg (voice playback, clip export) and
# keeps its mutable state on the /app/data volume.

FROM python:3.12-slim AS base
# HF_HOME lives on the volume, so the wake-word model is downloaded once and
# survives a container rebuild instead of being fetched again on every start.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/app/data/huggingface

# ffmpeg: voice playback + clip export. libopus: Opus encode/decode.
# libsodium: Discord voice encryption (PyNaCl). fonts: card rendering fallback.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg libopus0 libsodium23 fonts-dejavu-core ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt requirements-local-stt.txt ./
RUN python -m pip install --upgrade pip && python -m pip install -r requirements.txt

# On-device speech-to-text for the spoken wake word (faster-whisper: runs on
# this container's CPU, no API key, no audio ever leaves the machine).
#   docker build --build-arg INSTALL_LOCAL_STT=0 …   to skip it (~250 MB)
ARG INSTALL_LOCAL_STT=1
RUN if [ "$INSTALL_LOCAL_STT" = "1" ]; then python -m pip install -r requirements-local-stt.txt; fi

COPY . .

# Optionally ship the model inside the image. Empty (the default) means the bot
# downloads it on first start-up into HF_HOME on the volume instead.
ARG WHISPER_MODEL=
RUN if [ -n "$WHISPER_MODEL" ]; then \
        python tools/fetch_whisper_model.py "$WHISPER_MODEL" --dir /opt/whisper; \
    fi

RUN useradd --create-home --uid 10001 paradise \
    && mkdir -p /app/data /opt/whisper \
    && chown -R paradise:paradise /app /opt/whisper
USER paradise

VOLUME ["/app/data"]
CMD ["python", "bot.py"]
