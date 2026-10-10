#!/usr/bin/env bash
# Development convenience: the steps to get everything running, in order.
# Each is a plain command you can also run by hand from the project root.
# Settings are in config.toml (created from config/defaults.toml if missing);
# logs are in logs/.

set -e
cd "$(dirname "$0")"
PYTHON="${PYTHON:-python}"  # Python 3.11+

if ! command -v "$PYTHON" >/dev/null; then
  echo "error: '$PYTHON' not found. Install Python 3.11 or newer, or set PYTHON to it," >&2
  echo "e.g.: PYTHON=/c/msys64/ucrt64/bin/python ./launch.sh" >&2
  exit 1
fi

# 0. Create config.toml from the defaults on first run.
if [ ! -f config.toml ]; then
  cp config/defaults.toml config.toml
  echo "Created config.toml from config/defaults.toml"
fi

# 1. Download the whisper.cpp and llama.cpp servers, if needed.
"$PYTHON" scripts/install_api_servers.py

# 2. Download their models, if needed.
"$PYTHON" scripts/download_models.py

# 3. Start the servers, then the dashboard; Ctrl+C stops everything.
"$PYTHON" scripts/run_servers.py "$PYTHON" app.py
