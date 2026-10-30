# Paradise Bot — production image.
#
#   docker build -t paradisebot .
#   docker run -d --name paradise --env-file .env -v "$PWD/data:/app/data" paradisebot
#
# The runtime image is non-root, ships ffmpeg (clip export) and keeps its
# mutable state on the /app/data volume. It carries no speech-recognition
# weights: the bot does not transcribe anything.

FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# ffmpeg: voice playback + clip export. libopus: Opus encode/decode.
# libsodium: Discord voice encryption (PyNaCl). fonts: card rendering fallback.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg libopus0 libsodium23 fonts-dejavu-core ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN python -m pip install --upgrade pip && python -m pip install -r requirements.txt

COPY . .

RUN useradd --create-home --uid 10001 paradise \
    && mkdir -p /app/data \
    && chown -R paradise:paradise /app
USER paradise

VOLUME ["/app/data"]
CMD ["python", "bot.py"]
