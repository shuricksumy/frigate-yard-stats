# Demo video generator

Produces `docs/images/web-ui-demo.mp4` -- a real screen recording (Playwright + headless Chromium)
of the actual web UI (`/ui` + `/ui/admin`) running against a real, throwaway `ingest-worker` +
Postgres, seeded with a small synthetic dataset. Not mocked HTML/CSS -- the exact same app code
users run, just pointed at fake data (see `seed.py`'s own docstring for what it seeds and why).

Every scene (car, truck, dog, delivery person) uses a real, freely-licensed stock photo
(`real_photos/`, see `real_photos/SOURCES.md` for exact sources/license), cropped/zoomed/labeled
to look like a camera crop (`gen_real_frames.py`). The delivery-person photo specifically was
chosen shot from directly behind -- no real, identifiable face visible -- since presenting a real
stranger as if they were caught on a home security camera felt like the wrong call regardless of
licensing; every other candidate photo of a delivery person showed a clear face and was rejected
for that reason. A real license plate visible in the red sedan photo is blurred out as a further
courtesy.

## One-time setup

```bash
cd frigate/ingest-worker/demo
python3 -m venv venv
./venv/bin/pip install -r requirements-demo.txt
./venv/bin/pip install -r ../requirements.txt   # psycopg2, fastapi, uvicorn, pyyaml, requests, etc.
./venv/bin/playwright install chromium
```

## Regenerating the demo video

All commands from `frigate/ingest-worker/demo/`, using the venv's Python (`./venv/bin/python`)
throughout. Needs Docker (for a throwaway Postgres) and a system `ffmpeg`.

```bash
# 1. Throwaway pgvector Postgres (never the real deployment's database)
docker run -d --rm --name yardstats-demo-pg -e POSTGRES_PASSWORD=test \
  -e POSTGRES_USER=n8n_projects -e POSTGRES_DB=home_automation \
  -p 55411:5432 pgvector/pgvector:pg16
# wait for it: docker exec yardstats-demo-pg pg_isready -U n8n_projects

# 2. Seed the dataset (schema + synthetic events/visits/sightings/clips)
rm -rf video_storage video_storage_alerts && mkdir -p video_storage video_storage_alerts
MQTT_HOST=localhost POSTGRES_HOST=localhost POSTGRES_PORT=55411 POSTGRES_PASSWORD=test \
  POSTGRES_USER=n8n_projects POSTGRES_DB=home_automation FRIGATE_API_BASE=http://frigate.test:5000 \
  API_KEY=demo-key SCHEMA_SQL_PATH=../schema.sql EMBEDDING_DIMENSIONS=64 \
  VIDEO_STORAGE_PATH="$PWD/video_storage" VIDEO_STORAGE_PATH_ALERTS="$PWD/video_storage_alerts" \
  ./venv/bin/python seed.py

# 3. Stub embedding backend (deterministic keyword-vocabulary vectors -- see stub_embed_server.py)
./venv/bin/python stub_embed_server.py &

# 4. The real app, with the per-object-type feature flags forced on (see run_demo_server.py --
#    these no longer have env vars, only profiles.yaml + main.py's apply_profile_defaults, which
#    this bare-API launch doesn't run) so the Admin dashboard's Health panel matches the seeded data
MQTT_HOST=localhost POSTGRES_HOST=localhost POSTGRES_PORT=55411 POSTGRES_PASSWORD=test \
  POSTGRES_USER=n8n_projects POSTGRES_DB=home_automation FRIGATE_API_BASE=http://frigate.test:5000 \
  API_KEY=demo-key SCHEMA_SQL_PATH=../schema.sql EMBEDDING_DIMENSIONS=64 \
  VIDEO_STORAGE_PATH="$PWD/video_storage" VIDEO_STORAGE_PATH_ALERTS="$PWD/video_storage_alerts" \
  LLAMA_PROXY_BASE_URL=http://127.0.0.1:8930 LLAMA_PROXY_EMBED_PATH=/v1/embeddings \
  OBJECT_TYPES=car,truck,person,dog ./venv/bin/python run_demo_server.py &

# 5. Record (tours Visits/Events/connected-events-back-navigation/Search/Admin -- see record.py)
./venv/bin/python record.py

# 6. Convert the recorded .webm to a compact H.264 mp4
ffmpeg -y -i recording/*.webm -vf "fps=20,format=yuv420p" -c:v libx264 -preset slow -crf 22 \
  -movflags +faststart web-ui-demo.mp4

# 7. Copy into place and clean up
cp web-ui-demo.mp4 ../../../docs/images/web-ui-demo.mp4
pkill -f run_demo_server.py; pkill -f stub_embed_server.py
docker rm -f yardstats-demo-pg
rm -rf recording web-ui-demo.mp4 video_storage video_storage_alerts
```

`dryrun.py` is the same tour but screenshots each step instead of recording video (into `shots/`)
-- useful for checking selectors/framing/timestamps still line up before spending a full recording
pass, e.g. after changing `seed.py`'s data or `record.py`'s steps.

## Notes for future edits

- All seeded timestamps are relative to `seed.py`'s own run time (`ts(minutes_ago)`, computed from
  `datetime.now()` once at import) -- keep every offset under ~50 minutes so everything still
  falls inside the web UI's default "Last 1 hour" filter even if there's a real-world delay between
  seeding and recording (iterating on `record.py` after seeding, etc).
- Never let a visit's own `crop_image_base64` (the flat composite grid) get set in `seed.py` --
  only `preview_gif_base64`. The whole point of showing this demo is the richer animated preview;
  the flat grid is a deliberately unused fallback here (see CLAUDE.md's "Visit preview" section for
  why it exists in production at all).
- If you add another real photo, check it for identifiable people/legible plates before using it
  (see `gen_real_frames.py`'s docstring) and record its source/license in `real_photos/SOURCES.md`.
