#!/usr/bin/env bash

set -e
cd "$(dirname "$0")"

# Create config.toml from the defaults on first run.
if [ ! -f config.toml ]; then
  cp config/defaults.toml config.toml
  echo "Created config.toml from config/defaults.toml"
fi

python app.py --mock
