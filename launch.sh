#!/usr/bin/env bash
# Development convenience: the steps to get everything running, in order.
# Each is a plain command you can also run by hand from the project root.
# Settings: config/defaults.toml, overridden by config_local.toml if present.
# Logs are in logs/.

set -e
cd "$(dirname "$0")"
PYTHON="${PYTHON:-python}"  # Python 3.11+

if ! command -v "$PYTHON" >/dev/null; then
  echo "error: '$PYTHON' not found. Install Python 3.11 or newer, or set PYTHON to it," >&2
  echo "e.g.: PYTHON=/c/msys64/ucrt64/bin/python ./launch.sh" >&2
  exit 1
fi

# 1. Download the servers (whisper.cpp, llama.cpp, the TTS engine), if needed.
"$PYTHON" src/install_api_servers.py

# 2. Download their models, if needed.
"$PYTHON" src/download_models.py

# 3. Start the servers, then the dashboard; Ctrl+C stops everything.
"$PYTHON" src/run_servers.py "$PYTHON" src/app.py
