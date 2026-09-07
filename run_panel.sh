#!/bin/bash
# run_panel.sh — clean start/stop/status for the aichat panel (:8080).
# The panel is a blocking NiceGUI app; running it bare in a terminal occupies
# that terminal and orphans if the tab closes. This wrapper backgrounds it with
# a pidfile so you never have to hunt it in htop.
#
#   ./run_panel.sh start     # background, logs to History/panel.log
#   ./run_panel.sh stop      # graceful kill
#   ./run_panel.sh restart
#   ./run_panel.sh status

cd "$(dirname "$0")"
PIDFILE="./.panel.pid"
LOG="./History/panel.log"
# Prefer the repo venv when present (manual runs get venv too, not system python)
if [ -z "${PY:-}" ] && [ -x "./venv_ui/bin/python" ]; then
    PY="./venv_ui/bin/python"
fi
PY="${PY:-python3}"

start() {
    if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
        echo "already running (pid $(cat "$PIDFILE")) — http://localhost:8080"
        return 0
    fi
    # Viking memory needs its embedding model; without it every recall
    # silently returns nothing (the .06 outage). Pull once if missing.
    if command -v ollama >/dev/null 2>&1; then
        if ! ollama list 2>/dev/null | grep -q 'qwen3-embedding'; then
            echo "pulling qwen3-embedding:0.6b (Viking memory needs it) ..."
            ollama pull qwen3-embedding:0.6b 2>&1 | tail -1 \
                || echo "WARN: embedding pull failed — panel starts, recall stays empty"
        fi
    else
        echo "WARN: no ollama binary — panel starts, models + recall unavailable"
    fi
    mkdir -p History
    nohup "$PY" aichat_xterm.py > "$LOG" 2>&1 &
    echo $! > "$PIDFILE"
    sleep 1
    echo "started (pid $(cat "$PIDFILE")) — http://localhost:8080 (log: $LOG)"
}

stop() {
    if [ -f "$PIDFILE" ] && kill "$(cat "$PIDFILE")" 2>/dev/null; then
        rm -f "$PIDFILE"
        echo "stopped"
        return 0
    fi
    # fallback: no pidfile — match the script by name
    if pkill -f "aichat_xterm.py"; then
        rm -f "$PIDFILE"
        echo "stopped (by name)"
        return 0
    fi
    echo "not running"
}

case "${1:-start}" in
    start)   start ;;
    stop)    stop ;;
    restart) stop; sleep 1; start ;;
    status)
        if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
            echo "running (pid $(cat "$PIDFILE")) — http://localhost:8080"
        else
            echo "not running"
        fi
        ;;
    *) echo "usage: $0 {start|stop|restart|status}" ;;
esac
