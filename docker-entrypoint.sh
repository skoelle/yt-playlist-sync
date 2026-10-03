#!/bin/sh
set -e

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"
umask "${UMASK:-0022}"

getent group app >/dev/null 2>&1 || groupadd -o -g "$PGID" app
id app >/dev/null 2>&1 || useradd -o -u "$PUID" -g "$PGID" -M -d /config -s /usr/sbin/nologin app

mkdir -p /config /data
chown -R "$PUID:$PGID" /config

if ! gosu app sh -c 'touch /data/.write-test && rm -f /data/.write-test' 2>/dev/null; then
  echo "WARNING: /data is not writable for uid=$PUID gid=$PGID, downloads will be paused" >&2
fi

exec gosu app "$@"
