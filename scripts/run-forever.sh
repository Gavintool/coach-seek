#!/usr/bin/env bash
set -euo pipefail
umask 077

project=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
cd "$project"
python="$project/.venv/bin/python"
pid_file="$project/logs/coach-seek.pid"
run_lock="$project/logs/coach-seek.lock"
log_file="$project/logs/coach-seek.log"

supervise() {
    local child='' started result delay=2
    trap '
        if [[ -n $child ]]; then
            kill -TERM "$child" 2>/dev/null || true
            wait "$child" 2>/dev/null || true
        fi
        exit 0
    ' TERM INT
    while true; do
        started=$SECONDS
        "$python" -u -m coach_seek 8>&- &
        child=$!
        if wait "$child"; then
            exit 0
        else
            result=$?
        fi
        child=''
        if (( SECONDS - started >= 60 )); then
            delay=2
        fi
        printf '[%s] Coach Seek exited with status %d. Restarting in %ds.\n' \
            "$(date -Is)" "$result" "$delay"
        sleep "$delay" 8>&- &
        child=$!
        wait "$child"
        child=''
        delay=$((delay < 32 ? delay * 2 : 60))
    done
}

# Internal entry point. The start command passes the locked descriptor to nohup.
if [[ ${1:-} == _supervise ]]; then
    flock -n 8
    supervise
    exit
fi

case ${1:-status} in
    start|stop|status) ;;
    *) printf 'Usage: %s {start|stop|status}\n' "$0" >&2; exit 2 ;;
esac

mkdir -p logs
exec 9>"logs/coach-seek-control.lock"
flock 9
exec 8>"$run_lock"

case ${1:-status} in
    start)
        if ! flock -n 8; then
            echo "Coach Seek supervisor is already running."
            exit 0
        fi
        if [[ ! -x $python ]]; then
            echo "Create the project .venv and install requirements first." >&2
            exit 1
        fi
        # Config uses python-dotenv to load .env without executing it as shell code.
        "$python" -c 'from coach_seek.config import Config
try:
    Config.from_env()
except ValueError as exc:
    raise SystemExit(str(exc)) from None'
        nohup bash "$project/scripts/run-forever.sh" _supervise </dev/null >>"$log_file" 2>&1 9>&- &
        pid=$!
        # The lock is authoritative even if a crash leaves an old PID file.
        printf '%s\n' "$pid" >"$pid_file"
        echo "Coach Seek supervisor requested (PID $pid). Logs are in $log_file"
        ;;
    stop)
        if flock -n 8; then
            rm -f -- "$pid_file"
            echo "Coach Seek supervisor is stopped."
            exit 0
        fi
        read -r pid <"$pid_file"
        [[ $pid =~ ^[1-9][0-9]*$ ]]
        kill -TERM "$pid" 2>/dev/null || true
        if ! flock -w 15 8; then
            echo "Coach Seek supervisor is still stopping." >&2
            exit 1
        fi
        rm -f -- "$pid_file"
        echo "Coach Seek supervisor stopped."
        ;;
    status)
        if flock -n 8; then
            rm -f -- "$pid_file"
            echo "Coach Seek supervisor is stopped."
            exit 1
        fi
        read -r pid <"$pid_file"
        echo "Coach Seek supervisor is running (PID $pid). Logs are in $log_file"
        ;;
esac
