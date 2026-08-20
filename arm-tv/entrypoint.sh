#!/bin/sh
set -eu

# The upstream watcher only reacts to files added after it starts. Run one
# initial AMC pass so rips that completed while the service was stopped are
# handled too; the persistent exclude list makes this safe across restarts.
if find /input -type f \( \
    -iname '*.m2ts' -o -iname '*.mkv' -o -iname '*.mp4' -o -iname '*.avi' \
    -o -iname '*.ts' -o -iname '*.vob' -o -iname '*.webm' \
  \) -print -quit | grep -q .; then
  filebot -script fn:amc "$@"
fi

exec /opt/bin/filebot-watcher "$@"
