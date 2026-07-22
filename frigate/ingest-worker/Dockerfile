FROM python:3.14-slim
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt --break-system-packages
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
