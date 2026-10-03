#!/usr/bin/env bash
# Copyright (c) 2026 Stefan Koelle (https://stefankoelle.de)
# Licensed under the MIT License. See LICENSE file in project root for details.
# Start yt-playlist-sync locally in DRY_RUN mode with all data under a temp directory.
#
#   ./start-dev.sh                          # UI at http://<host>:8048 (0.0.0.0, like the container)
#   YOUTUBE_CHANNEL=@mychannel ./start-dev.sh
#   ./start-dev.sh --fresh                  # delete the temp data first, start clean
#   DRY_RUN=0 YOUTUBE_CHANNEL=@mychannel ./start-dev.sh   # real downloads to /tmp
#
# Overrides: PORT, HOST, DRY_RUN, DATA_DIR, CONFIG_DIR, RELOAD, INSTALL.
# Precedence: environment > .env > temp defaults.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

case "${1:-}" in
  "" | --fresh) ;;
  *) echo "usage: start-dev.sh [--fresh]" >&2; exit 2 ;;
esac

# Remember caller exports before .env is read.
ENV_CHANNEL="${YOUTUBE_CHANNEL-}"
ENV_DRY="${DRY_RUN-}"
ENV_DATA="${DATA_DIR-}"
ENV_CONFIG="${CONFIG_DIR-}"

if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

if [ -n "$ENV_CHANNEL" ]; then YOUTUBE_CHANNEL="$ENV_CHANNEL"; fi
if [ -n "$ENV_DRY" ]; then DRY_RUN="$ENV_DRY"; fi
if [ -n "$ENV_DATA" ]; then DATA_DIR="$ENV_DATA"; fi
if [ -n "$ENV_CONFIG" ]; then CONFIG_DIR="$ENV_CONFIG"; fi

TMP_BASE="${TMPDIR:-/tmp}/yt-playlist-sync"
YOUTUBE_CHANNEL="${YOUTUBE_CHANNEL:-@beispielkanal}"
DRY_RUN="${DRY_RUN:-1}"
DATA_DIR="${DATA_DIR:-$TMP_BASE/data}"
CONFIG_DIR="${CONFIG_DIR:-$TMP_BASE/config}"
PORT="${PORT:-8048}"
HOST="${HOST:-0.0.0.0}"
RELOAD="${RELOAD:-1}"

is_tmp_path() {
  case "$1" in
    /tmp/*) return 0 ;;
    "${TMPDIR:-/nonexistent}"/*) return 0 ;;
    *) return 1 ;;
  esac
}

if [ "${1:-}" = "--fresh" ]; then
  for d in "$DATA_DIR" "$CONFIG_DIR"; do
    if is_tmp_path "$d"; then
      rm -rf "$d"
    else
      echo "refusing to delete non-temp path: $d" >&2
      exit 1
    fi
  done
fi

if [ ! -x .venv/bin/uvicorn ] || [ "${INSTALL:-0}" = "1" ]; then
  python3 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt -r requirements-dev.txt
fi

export PATH="$ROOT/.venv/bin:$PATH"
export YOUTUBE_CHANNEL DRY_RUN DATA_DIR CONFIG_DIR
mkdir -p "$DATA_DIR" "$CONFIG_DIR"

ui_host="$HOST"
case "$HOST" in 0.0.0.0 | ::) ui_host=localhost ;; esac

echo "yt-playlist-sync dev"
echo "  url      http://$ui_host:$PORT"
echo "  dry-run  $DRY_RUN  (1 = yt-dlp runs with --simulate, nothing is downloaded)"
echo "  channel  $YOUTUBE_CHANNEL"
echo "  data     $DATA_DIR"
echo "  config   $CONFIG_DIR"

args=(--host "$HOST" --port "$PORT")
if [ "$RELOAD" = "1" ]; then args+=(--reload); fi
exec uvicorn app.main:app "${args[@]}"
