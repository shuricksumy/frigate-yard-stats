FROM python:3.14-slim
# requirements.txt is mostly unpinned, so the apt and pip layers would otherwise
# be restored from the CI cache until that file changes. CI sets this to the ISO
# week, so a build picks up new ffmpeg/Debian fixes and package releases weekly.
ARG REFRESH_WEEK=0
RUN echo "cache epoch: ${REFRESH_WEEK}" && apt-get update && apt-get upgrade -y \
    && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN echo "cache epoch: ${REFRESH_WEEK}" && pip install -r requirements.txt --break-system-packages
COPY . .
# Build metadata baked in as plain ENV -- see config.py's APP_VERSION/APP_BUILD_SHA/APP_BUILD_DATE
# and .github/workflows/ingest-worker-image.yml's build-args. Defaults keep a plain local
# `docker build` with no build-args working fine (surfaced as "dev"/"unknown" in the web UI footer).
ARG APP_VERSION=dev
ARG APP_BUILD_SHA=unknown
ARG APP_BUILD_DATE=
ENV APP_VERSION=${APP_VERSION}
ENV APP_BUILD_SHA=${APP_BUILD_SHA}
ENV APP_BUILD_DATE=${APP_BUILD_DATE}
EXPOSE 8080
CMD ["python", "main.py"]
