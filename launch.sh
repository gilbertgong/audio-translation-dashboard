#!/usr/bin/env bash
# Set up and start everything: checks Python, installs/updates the API server
# binaries and models, starts whisper-server and llama-server, then runs the
# dashboard (app.py). Ctrl+C stops it all.
#
# Usage: ./launch.sh [--skip-setup]
#   --skip-setup  don't check for new binaries or models (e.g. when offline)
#
# Environment: PYTHON (default: python), CONFIG (default: config.toml).
# Server output goes to tmp/logs/<name>.log.

set -euo pipefail
cd "$(dirname "$0")"

PYTHON="${PYTHON:-python}"
CONFIG="${CONFIG:-config.toml}"
LOG_DIR=tmp/logs
STARTUP_TIMEOUT=300  # seconds to wait for each server to become ready
SKIP_SETUP=0

usage() { sed -n '2,10s/^# \{0,1\}//p' "$0"; }
die() { echo "error: $*" >&2; exit 1; }

for arg in "$@"; do
  case "$arg" in
    --skip-setup) SKIP_SETUP=1 ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option: $arg (see --help)" ;;
  esac
done

# 1. Check prerequisites.
command -v "$PYTHON" >/dev/null || die "Python not found ('$PYTHON'); install Python 3.11+ or set PYTHON"
"$PYTHON" -c 'import sys; sys.exit(sys.version_info < (3, 11))' \
  || die "Python 3.11+ is required, found: $("$PYTHON" --version 2>&1)"
command -v curl >/dev/null || die "curl not found"

# 2-3. Install/update server binaries and download models.
if [[ $SKIP_SETUP == 0 ]]; then
  echo "== Checking API server binaries"
  "$PYTHON" scripts/install_api_servers.py "$CONFIG"
  echo "== Checking models"
  "$PYTHON" scripts/download_models.py "$CONFIG"
fi

# 4. Start the API servers; they are stopped again when this script exits.
mkdir -p "$LOG_DIR"
pids=()
cleanup() {
  trap - EXIT
  if (( ${#pids[@]} )); then
    echo "== Stopping API servers"
    kill "${pids[@]}" 2>/dev/null || true
    wait 2>/dev/null || true
  fi
}
trap cleanup EXIT
trap 'exit 130' INT TERM

http_status() { curl -s -o /dev/null --max-time 2 -w '%{http_code}' "$1" || true; }

start_server() {
  local name=$1 url cmd=() pid start
  url=$("$PYTHON" scripts/server_command.py "$name" --url --config "$CONFIG")
  "$PYTHON" scripts/server_command.py "$name" --config "$CONFIG" > "$LOG_DIR/$name.cmd"
  mapfile -d '' cmd < "$LOG_DIR/$name.cmd"

  [[ $(http_status "$url/health") == 000 ]] \
    || die "something is already listening at $url (is another launch.sh running?)"

  echo "== Starting $name at $url (log: $LOG_DIR/$name.log)"
  "${cmd[@]}" > "$LOG_DIR/$name.log" 2>&1 &
  pid=$!
  pids+=("$pid")

  start=$SECONDS
  until [[ $(http_status "$url/health") == 200 ]]; do
    if ! kill -0 "$pid" 2>/dev/null; then
      tail -n 20 "$LOG_DIR/$name.log" >&2
      die "$name exited during startup; see $LOG_DIR/$name.log"
    fi
    (( SECONDS - start < STARTUP_TIMEOUT )) \
      || die "$name not ready after ${STARTUP_TIMEOUT}s; see $LOG_DIR/$name.log"
    sleep 1
  done
  echo "   ready after $(( SECONDS - start ))s"
}

# whisper first: llama-server sizes itself to the GPU memory left over.
start_server whisper
start_server llama

# 5. Run the dashboard in the foreground.
echo "== Starting dashboard"
"$PYTHON" app.py "$CONFIG"
