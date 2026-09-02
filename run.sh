#!/usr/bin/env bash
#
# One command to run the project. POSIX twin of run.ps1 -- same subcommands,
# same behaviour -- for the Linux GPU box and for Git Bash on Windows.
#
#   ./run.sh              start the app at http://127.0.0.1:8080
#   ./run.sh dev          Vite dev server with HMR + auto-reloading backend
#   ./run.sh setup        create .venv, install Python and npm deps, make .env
#   ./run.sh build        rebuild the frontend bundle only
#   ./run.sh check        database and pipeline pre-flight, then exit
#
# PORT, BIND and VITE_PORT override the defaults:  PORT=9000 ./run.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FRONTEND="$ROOT/web/frontend"
DIST="$FRONTEND/dist"
PORT="${PORT:-8080}"
BIND="${BIND:-127.0.0.1}"
VITE_PORT="${VITE_PORT:-5173}"

say()  { printf '\033[36m==> %s\033[0m\n' "$1"; }
warn() { printf '\033[33m    %s\033[0m\n' "$1"; }
die()  { printf '\033[31mrun.sh: %s\033[0m\n' "$1" >&2; exit 1; }

# Both venv layouts, because the same checkout is used from Git Bash on
# Windows (Scripts/python.exe) and from a Linux shell (bin/python).
venv_python() {
    for p in "$ROOT/.venv/bin/python" "$ROOT/.venv/Scripts/python.exe"; do
        [ -x "$p" ] && { printf '%s' "$p"; return; }
    done
}

python_bin() {
    local p
    p="$(venv_python)"
    if [ -n "$p" ]; then printf '%s' "$p"; return; fi
    for c in python3 python; do
        if command -v "$c" >/dev/null 2>&1; then
            warn "no .venv -- using $c. Run './run.sh setup' for an isolated one."
            printf '%s' "$c"; return
        fi
    done
    die "Python not found on PATH. Install Python 3.11+, then run './run.sh setup'."
}

install_node() {
    [ -d "$FRONTEND/node_modules" ] && return
    command -v npm >/dev/null 2>&1 || die "npm not found on PATH. Install Node 20+."
    say 'installing frontend dependencies (first run only)'
    (cd "$FRONTEND" && npm install)
}

build_frontend() {
    install_node
    say 'building frontend'
    (cd "$FRONTEND" && npm run build)
}

# True when any frontend source is newer than the bundle, or there is no
# bundle. node_modules and dist are excluded: one is too large to walk, the
# other would be compared against itself.
frontend_stale() {
    [ -f "$DIST/index.html" ] || return 0
    local newer
    newer="$(find "$FRONTEND/src" "$FRONTEND/public" "$FRONTEND/index.html" \
                  "$FRONTEND/package.json" "$FRONTEND/vite.config.ts" \
                  -newer "$DIST/index.html" -print -quit 2>/dev/null || true)"
    [ -n "$newer" ]
}

case "${1:-start}" in

setup)
    if [ -z "$(venv_python)" ]; then
        command -v python3 >/dev/null 2>&1 || die "Python not found on PATH."
        say 'creating .venv'
        python3 -m venv "$ROOT/.venv"
    fi
    py="$(venv_python)"
    say 'installing Python dependencies'
    "$py" -m pip install --upgrade pip --quiet
    "$py" -m pip install -r "$ROOT/requirements.txt"
    install_node
    if [ ! -f "$ROOT/.env" ]; then
        cp "$ROOT/.env.example" "$ROOT/.env"
        say '.env created from .env.example'
        warn 'it points at mongodb://localhost:27017 -- edit it for Atlas.'
    fi
    say "setup complete. Run './run.sh' to start."
    # The GPU half is deliberately not installed here: torch, the YOLO weights
    # and the vision model are a multi-gigabyte download, and the app runs
    # without them. See README "Local backend".
    warn 'extraction also needs torch, the YOLO weights and Ollama -- see README.'
    ;;

build)
    build_frontend
    ;;

check)
    py="$(python_bin)"
    "$py" "$ROOT/scripts/db_check.py"
    "$py" "$ROOT/scripts/health.py"
    ;;

dev)
    py="$(python_bin)"
    install_node
    say "frontend  http://localhost:$VITE_PORT   <- open this one"
    say "backend   http://$BIND:$PORT"
    (cd "$FRONTEND" && npm run dev -- --port "$VITE_PORT") &
    vite=$!
    trap 'kill "$vite" 2>/dev/null || true' EXIT INT TERM
    "$py" "$ROOT/web/app.py" --port "$PORT" --host "$BIND" --reload
    ;;

start)
    py="$(python_bin)"
    if [ "${NO_BUILD:-}" != "1" ] && frontend_stale; then build_frontend; fi
    "$py" "$ROOT/web/app.py" --port "$PORT" --host "$BIND"
    ;;

*)
    die "unknown command '$1'. One of: start dev build setup check"
    ;;
esac
