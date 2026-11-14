#!/bin/zsh
# Runs before every mini-run task for almanac-grid.
set -euo pipefail

# Start Postgres if it isn't up. The mini is headless, so start the server directly
# rather than launching the Postgres.app GUI. Paths are Postgres.app's defaults for v16;
# verify them on the mini.
PG_BIN="/Applications/Postgres.app/Contents/Versions/16/bin"
PG_DATA="$HOME/Library/Application Support/Postgres/var-16"
if ! "$PG_BIN/pg_isready" -q; then
  "$PG_BIN/pg_ctl" -D "$PG_DATA" -l "$PG_DATA/postgres.log" start -w
fi

[[ -d .venv ]] || python3 -m venv .venv
.venv/bin/pip install -q -r requirements-dev.txt
