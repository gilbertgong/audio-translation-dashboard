#!/usr/bin/env bash

set -e
cd "$(dirname "$0")/.."

python src/app.py --mock
