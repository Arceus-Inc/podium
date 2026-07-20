#!/usr/bin/env bash
# Boot the Arceus cockpit for local testing: Postgres + migrations + LLM-keyed api/conductor + browser.
#
#   ./scripts/cockpit-dev.sh            # boot everything, open the dashboard
#   PG_PORT=5555 ./scripts/cockpit-dev.sh
#
# Idempotent: reuses a running Postgres, kills stale api processes, re-runs migrations.
# Stop the api afterwards with the printed kill command; Postgres keeps running.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
PG_PORT="${PG_PORT:-55444}"
API_PORT="${API_PORT:-8901}"
PG_DATA="${PG_DATA:-$REPO/.podium/pg}"
WORKDIR="${WORKDIR:-$REPO/.podium/workdir}"
KEYS_ENV="${KEYS_ENV:-$HOME/chorus/.env}"   # AZURE_OPENAI_* live here — never committed, never echoed
PG_BIN="${PG_BIN:-$(ls -d /opt/homebrew/opt/postgresql@*/bin 2>/dev/null | sort -V | tail -1)}"

[ -x "$PG_BIN/postgres" ] || { echo "postgres not found (set PG_BIN)"; exit 1; }

# --- LLM keys (required: the conductor refuses beats without a model) -----------------------
# Parse KEY=VALUE lines verbatim instead of `source`: a value like `Name <mail@x>` makes the
# shell see a redirect, error, and silently abandon the REST of the file — found live when
# TAVILY_API_KEY (after such a line) never reached the server and every web_search failed.
if [ -f "$KEYS_ENV" ]; then
    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in \#*|'') continue ;; esac
        case "$line" in
            [A-Za-z_]*=*) export "${line%%=*}=${line#*=}" 2>/dev/null || true ;;
        esac
    done < "$KEYS_ENV"
fi
[ -n "${AZURE_OPENAI_API_KEY:-}" ] || { echo "AZURE_OPENAI_API_KEY missing (set KEYS_ENV or export it)"; exit 1; }

# --- Postgres: reuse if listening, else init-if-needed and start ----------------------------
if ! "$PG_BIN/pg_isready" -q -h 127.0.0.1 -p "$PG_PORT"; then
    if [ ! -d "$PG_DATA" ]; then
        mkdir -p "$PG_DATA"
        "$PG_BIN/initdb" -D "$PG_DATA" -U postgres -A trust >/dev/null
    fi
    # LC_ALL=C: macOS PG otherwise dies with "postmaster became multithreaded during startup"
    LC_ALL=C "$PG_BIN/pg_ctl" -D "$PG_DATA" -l "$PG_DATA/pg.log" \
        -o "-p $PG_PORT -c listen_addresses=127.0.0.1 -c fsync=off" start >/dev/null
    "$PG_BIN/pg_isready" -q -h 127.0.0.1 -p "$PG_PORT" -t 15
fi
"$PG_BIN/psql" -h 127.0.0.1 -p "$PG_PORT" -U postgres -tAc \
    "SELECT 1 FROM pg_database WHERE datname='podium'" | grep -q 1 \
    || "$PG_BIN/createdb" -h 127.0.0.1 -p "$PG_PORT" -U postgres podium

# --- Stale servers: kill EVERY podium uvicorn — a zombie that lost the port keeps its -------
# embedded conductor polling the shared DB and claims runs with stale code.
pgrep -f "uvicorn podium.main" | xargs kill -9 2>/dev/null || true

# --- Env: app runs as the RLS-bound runtime role; conductor discovery + DDL as postgres -----
export PODIUM_DATABASE_URL="postgresql+asyncpg://podium_app@127.0.0.1:$PG_PORT/podium"
export PODIUM_CONDUCTOR_CONTROL_DATABASE_URL="postgresql+asyncpg://postgres@127.0.0.1:$PG_PORT/podium"
export PODIUM_ENGINE_LEDGER_DSN="postgresql://podium_app@127.0.0.1:$PG_PORT/podium"
export PODIUM_CONDUCTOR_EMBEDDED=1
export PODIUM_CONDUCTOR_MAX_TICKS=0      # infinite pulses — the company-OS mode
export PODIUM_DEV_BOOTSTRAP=1            # "✦ New playground" mints workspace+company+token
export PODIUM_MODEL_API_KEY="$AZURE_OPENAI_API_KEY"
export PODIUM_MODEL_BASE_URL="$AZURE_OPENAI_BASE_URL"
export PODIUM_MODEL_DEPLOYMENT="$AZURE_OPENAI_DEPLOYMENT"
export PODIUM_WORKDIR="$WORKDIR"
export PODIUM_LOG_DIR="$WORKDIR/logs"
# A long autonomous operator run makes many governed calls; the default 100/min bucket 429s it dead.
# Raise it for local operation (the single-bucket limiter is a known audit finding, P-H6).
export PODIUM_RATE_LIMIT_MAX="${PODIUM_RATE_LIMIT_MAX:-100000}"
# Warm-start the lattice learning loop for a young company: the default per-employee gate needs
# >=2 similar beats, which a single build never reaches, so learning stays dark. MIN_CLUSTER=1 lets
# a single strong episode consolidate; raise it once the company has run enough repeated work.
export CHORUS_LATTICE_MIN_CLUSTER="${CHORUS_LATTICE_MIN_CLUSTER:-1}"
mkdir -p "$WORKDIR/logs"

# Role + podium migrations + engine deltas (idempotent; needs the admin DSN)
PODIUM_DATABASE_URL="postgresql+asyncpg://postgres@127.0.0.1:$PG_PORT/podium" \
    "$REPO/.venv/bin/python" -m podium.bootstrap_db >/dev/null

# --- API + embedded conductor, backgrounded; log at .podium/api.log -------------------------
nohup "$REPO/.venv/bin/uvicorn" podium.main:create_app --factory \
    --host 127.0.0.1 --port "$API_PORT" > "$REPO/.podium/api.log" 2>&1 &
API_PID=$!

for _ in $(seq 1 30); do
    curl -sf "http://127.0.0.1:$API_PORT/readyz" >/dev/null && break
    sleep 1
done
curl -sf "http://127.0.0.1:$API_PORT/readyz" >/dev/null \
    || { echo "api failed to start — tail .podium/api.log"; exit 1; }

echo "cockpit: http://127.0.0.1:$API_PORT/dashboard   (api pid $API_PID, log .podium/api.log)"
echo "stop:    kill $API_PID"
open "http://127.0.0.1:$API_PORT/dashboard" 2>/dev/null || true
