# ARM Configuration and Utilities

This repository runs [Automatic Ripping Machine (ARM)](https://github.com/automatic-ripping-machine/automatic-ripping-machine) and a TV organization sidekick service.

## Runtime layout

ARM remains the only privileged container. It owns mounting, ripping, and ejecting. Its small bridge writes captures and queue events to `${ARM_MEDIA}/.tv`. The `tv` container has no device access and sees only the completed TV tree and organizer data directory; episode files are renamed into their show/season folders in that same tree.

```text
ARM identify -> capture manifest -> ARM rip -> durable completion task
                                                   |
                                                   v
                                          analyze -> review UI
                                                   |
                                          show + episode info
                                                   |
                                                   v
                                      background atomic move
```

Episode organization stays in the completed-TV tree and keeps every season
under one year-qualified show folder:

```text
completed/tv/Show Name (2020) [tmdbid-123]/Season 01/S01E01.mkv
```

## Configuration

The existing ARM paths must be absolute host paths:

| Variable | Purpose |
| --- | --- |
| `ARM_UID`, `ARM_GID` | Numeric owner used by both services |
| `ARM_HOME` | ARM home directory |
| `ARM_MEDIA` | ARM media root; organizer data and TV staging live below it |
| `ARM_MUSIC` | ARM music output |
| `ARM_LOGS` | ARM log directory |
| `ARM_CONFIG` | ARM configuration directory |
| `TMDB_READ_TOKEN` | Optional TMDB v4 API read token; required for catalog search |
| `TV_PORT` | Host review-UI port, default `8081` |
| `TV_AUTH_TOKEN` | Optional bearer token for every HTTP request |
| `TV_HASH_ASSETS` | Full SHA-256 hashing, default `true` |
| `TV_CAPTURE_MENU_ASSETS` | Copy DVD menu VOBs, default `false` |
| `JELLYFIN_EPISODE_VERSIONS` | Enable reviewed multi-version episode export, default `false` |
| `TZ` | Container timezone |

When `TV_AUTH_TOKEN` is set, put the service behind a reverse proxy that supplies `Authorization: Bearer <token>`; the server-rendered UI does not put credentials in URLs or browser storage.

## Run

Before the first start, create writable host directories:

```bash
mkdir -p "${ARM_MEDIA}/completed/tv" "${ARM_MEDIA}/.tv"
chown -R "${ARM_UID}:${ARM_GID}" "${ARM_MEDIA}/completed/tv" "${ARM_MEDIA}/.tv"
```

Build and start both services:

```bash
docker compose up -d --build
```

Open ARM at <http://localhost:8080> and the TV review service at <http://localhost:8081>. Follow logs with:

```bash
docker compose logs -f arm tv
```

## Manual/local workflow

The collector has no third-party Python package dependencies; the review UI uses Jinja2 for auto-escaped templates. `ffprobe` improves asset validation, `makemkvcon` supplies primary source identity, and `lsdvd` supplies optional DVD structure. Install the project locally with `python3 -m pip install -e .` before using the CLI.

```bash
# Initialize storage using TV_DATA/INBOX/LIBRARY.
python3 -m tv init-db

# Capture a currently mounted disc and register it as ripping.
python3 -m tv collect \
  --disc-root /media/disc \
  --makemkv-source dev:/dev/sr0

# Attach ARM's completed output and analyze immediately.
python3 -m tv complete JOB_ID --rip-root /path/to/completed/rip

# Run the web service or inspect jobs as JSON.
python3 -m tv serve
python3 -m tv list

# Save a show, season, and episode assignment in the review UI. The worker
# records and performs the move into TV_LIBRARY automatically.
```

## Test

```bash
python3 -m unittest discover -v
python3 -m compileall -q tv arm tests
```
