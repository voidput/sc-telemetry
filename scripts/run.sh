#!/usr/bin/env sh
# Start the bridge + dashboard (http://127.0.0.1:8765). All arguments pass through, e.g.
#   scripts/run.sh --record
#   SC_DIR="$HOME/Games/star-citizen/drive_c/Program Files/Roberts Space Industries/StarCitizen" scripts/run.sh
set -eu
cd "$(dirname "$0")/.."
exec uv run sctelemetry "$@"
