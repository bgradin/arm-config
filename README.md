# Automatic Ripping Machine Config

Minimal configuration for running [Automatic Ripping Machine (ARM)](https://github.com/automatic-ripping-machine/automatic-ripping-machine)

## Prerequisites

* Physical DVD/Blu-ray drive
* Docker + Docker compose
* Free space
* .env file with correct settings
* Create a `data/filebot` folder and make sure the user passed to FileBot has ownership

## Environment Variables

All directory paths below are absolute paths on the host machine.

| Setting    | Description |
| ---------- | ----------- |
| ARM_UID    | User ID of ARM user, used for file ownership |
| ARM_GID    | Group ID of ARM user, used for file ownership |
| ARM_HOME   | ARM home directory |
| ARM_MEDIA  | Directory for ARM media rips |
| ARM_MUSIC  | Directory for ARM music rips |
| ARM_LOGS   | ARM log directory |
| ARM_CONFIG | ARM config directory |
| FILEBOT_UID | User ID of FileBot user |
| FILEBOT_GID | Group ID of FileBot user |
| FILEBOT_LICENSE_FILE | FileBot license file |
| SETTLE_DOWN_TIME | FileBot processing delay |
| SETTLE_DOWN_CHECK | FileBot processing delay |

## Usage

Start:

```bash
docker compose up -d --build
```

You should eventually see ARM running. Open a browser and visit: http://localhost:8080

Check status:

```bash
docker compose ps
```

View logs:

```bash
docker compose logs -f
# FileBot's detailed log is also written to $ARM_LOGS/tv-filebot.log
```

Stop:

```bash
docker compose down
```

Update ARM:

```bash
docker compose down
docker compose pull
docker compose up -d
```
