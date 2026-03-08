#!/bin/zsh
set -euo pipefail
unsetopt BG_NICE 2>/dev/null || true

ROOT_DIR="${0:A:h:h}"
RUNTIME_DIR="$ROOT_DIR/runtime"
SESSION_ENV="$RUNTIME_DIR/session.env"
LEGACY_ENV="$RUNTIME_DIR/launchd.env"
MANAGER_PID_FILE="$RUNTIME_DIR/manager.pid"
CHILD_PID_FILE="$RUNTIME_DIR/polymarketbot.pid"
RUN_LOG="$RUNTIME_DIR/polymarketbot.log"

mkdir -p "$ROOT_DIR/reports" "$RUNTIME_DIR"

cd "$ROOT_DIR"

runtime_env="$SESSION_ENV"
if [[ ! -f "$runtime_env" && -f "$LEGACY_ENV" ]]; then
    runtime_env="$LEGACY_ENV"
fi

if [[ -f "$runtime_env" ]]; then
    set -a
    source "$runtime_env"
    set +a
fi

child_pid=""

forward_shutdown() {
    if [[ -n "$child_pid" ]] && kill -0 "$child_pid" 2>/dev/null; then
        kill -INT "$child_pid" 2>/dev/null || true
        wait "$child_pid" 2>/dev/null || true
    fi
}

cleanup() {
    rm -f "$CHILD_PID_FILE" "$MANAGER_PID_FILE"
}

trap 'cleanup' EXIT
trap 'forward_shutdown; exit 0' INT TERM

echo "$$" > "$MANAGER_PID_FILE"
if [[ -x /usr/bin/caffeinate ]]; then
    /usr/bin/caffeinate -dimsu "$ROOT_DIR/polymarketbot" >> "$RUN_LOG" 2>&1 &
else
    "$ROOT_DIR/polymarketbot" >> "$RUN_LOG" 2>&1 &
fi
child_pid=$!
echo "$child_pid" > "$CHILD_PID_FILE"

if wait "$child_pid"; then
    exit_code=0
else
    exit_code=$?
fi
cleanup
exit "$exit_code"
