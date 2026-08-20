#!/bin/sh
set -eu

python -m tv init-db >/dev/null
exec python -m tv "$@"

