# Automatic Ripping Machine Config

Minimal configuration for running [Automatic Ripping Machine (ARM)](https://github.com/automatic-ripping-machine/automatic-ripping-machine)

## Prerequisites

* Physical DVD/Blu-ray drive
* Docker + Docker compose
* Free space
* .env file with correct settings

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

## Usage

Start:

```bash
docker compose up -d
```

You should eventually see ARM running. Open a browser and visit: http://localhost:8080

Check status:

```bash
docker compose ps
```

View logs:

```bash
docker compose logs -f
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
