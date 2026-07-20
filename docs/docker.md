# Docker & Docker Compose, explained for this project

This page assumes you've never really used Docker before. If you already know your way around
`docker compose`, you probably only need the "Profiles in this project" and "Everyday commands"
sections below.

## The five-minute version

- **Docker** runs an application inside a lightweight, isolated "container" — like a tiny virtual
  machine that only has exactly what that one application needs (Python, ffmpeg, etc. for
  `ingest-worker`; Frigate's own dependencies for `frigate`). You don't install Python or ffmpeg on
  your actual machine — Docker does it inside the container, and it never touches anything else on
  your system.
- **A Docker image** is the packaged, ready-to-run version of an application (like a shipped
  product). **A container** is one running instance of an image.
- **Docker Compose** is a tool for describing *several* containers, and how they talk to each
  other, in one YAML file (`docker-compose.yml`) — instead of typing a long `docker run ...`
  command by hand for each one. `docker compose up -d` reads that file and starts everything it
  describes.
- **`.env`** is a plain text file of `KEY=value` lines that `docker-compose.yml` reads values from
  (e.g. `${MQTT_HOST}` in the compose file comes from `MQTT_HOST=...` in `.env`). This project ships
  `.env.example` as a template — you copy it to `.env` and fill in real values; `.env` itself is
  gitignored so your real passwords/IPs never get committed.

## Installing Docker

If you don't have it yet: [Docker's own install docs](https://docs.docker.com/get-docker/) cover
every OS. On Linux (most homelab setups), Docker Compose is the `docker compose` subcommand (no
separate install) on any reasonably recent Docker install. Confirm it works:

```bash
docker --version
docker compose version
```

## Profiles in this project

One thing that trips people up: this repo has **one `docker-compose.yml` file describing two
different physical machines** — your Frigate/camera box, and whatever machine runs n8n/Postgres
(often the same box, sometimes not). Compose "profiles" are how one file safely covers both
without ever starting the wrong thing on the wrong host.

| Profile | What it starts | Where you run it |
|---|---|---|
| `pipeline` | `postgres-projects` (the database) + `ingest-worker` (this project's own service) | Wherever your n8n/Postgres already runs |
| `nvr` | `frigate` itself | Your camera/NVR machine |
| `mqtt` | An optional local Mosquitto MQTT broker | Add this only if you don't already have an MQTT broker running somewhere |

A plain `docker compose up -d` with **no** `--profile` flag starts **nothing at all** — that's
deliberate, so you can't accidentally bring up the wrong stack on the wrong machine. You always say
which one you mean:

```bash
# On the n8n/Postgres host:
cd frigate
docker compose --profile pipeline up -d

# On the Frigate/camera host:
cd frigate
docker compose --profile nvr up -d

# If you want a local MQTT broker too (dev/testing, or you don't have one already):
docker compose --profile pipeline --profile mqtt up -d
```

Both hosts read the *same* `.env` file layout (copy `frigate/.env.example` to `frigate/.env` on
each host) — each service only reads the environment variables it actually uses, so it's safe to
leave the "other host's" section as the placeholder `changeme` values.

## Everyday commands

```bash
# Start (or restart with new settings after editing .env)
docker compose --profile pipeline up -d

# Watch what a service is doing right now (Ctrl+C to stop watching, doesn't stop the container)
docker compose --profile pipeline logs -f ingest-worker

# Is it actually running?
docker compose --profile pipeline ps

# Stop everything in this profile (containers, not their data)
docker compose --profile pipeline down

# Pick up a newly-published ingest-worker image (see below) and restart with it
docker compose --profile pipeline pull ingest-worker
docker compose --profile pipeline up -d ingest-worker
```

## How `ingest-worker` gets updated

`ingest-worker`'s image is built automatically on GitHub every time this project's code changes
(see `.github/workflows/ingest-worker-image.yml`) and published to GitHub Container Registry
(GHCR). Your `docker-compose.yml` points at that published image by default — so updating means
"pull the new image, restart the container," not "rebuild from source":

```bash
cd frigate
docker compose --profile pipeline pull ingest-worker
docker compose --profile pipeline up -d ingest-worker
```

If you're actively developing changes to `ingest-worker` yourself, swap the compose file's
`image:` line for `build: ./ingest-worker` and use `docker compose --profile pipeline build
ingest-worker` instead — see [`configuration.md`](configuration.md) for more on that.

## Basic troubleshooting

- **A container keeps restarting / exits immediately** — `docker compose --profile pipeline logs
  ingest-worker` (drop `-f` to just see what already happened, not follow live) almost always
  tells you why in the last few lines — usually a missing/wrong value in `.env` (e.g.
  `POSTGRES_PROJECTS_PASSWORD` not set, or `FRIGATE_API_BASE` unreachable from this host).
- **"port is already allocated"** — something else on that host is already using the port
  (`8080` for `ingest-worker`'s API, `5432` for Postgres, `1883` for MQTT). Either stop the other
  thing, or change the port on this project's side via the relevant `.env` variable (e.g.
  `INGEST_WORKER_API_PORT`).
- **`ingest-worker` can't reach Frigate** — `FRIGATE_API_BASE` in `.env` must be Frigate's real
  LAN IP and port (e.g. `http://192.168.1.10:5000`), not a Docker service name — these two
  services usually run on two different physical hosts, so Docker's internal container-to-
  container networking doesn't apply here.
- **Changed `.env` but nothing changed** — most settings only take effect on container start, not
  live. Run `docker compose --profile pipeline up -d` again after editing `.env` (Compose only
  recreates containers whose config actually changed, so this is safe to re-run any time).
- **Changed `profiles.yaml` but nothing changed** — this is a *different* gotcha from the one
  above: `profiles.yaml` is bind-mounted, not a Compose-level env var, so Compose has no way to
  tell its content changed and `docker compose up -d` is a no-op here. Use `docker compose
  --profile pipeline restart ingest-worker` instead (or `up -d --force-recreate ingest-worker`) to
  actually pick up the edit.
- **Started fresh and want to wipe the database** — see
  [`troubleshooting.md`](troubleshooting.md#starting-over) for how to safely reset Postgres.
