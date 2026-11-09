#!/usr/bin/env bash
# Start, check or stop the local Ollama used for Qwen candidate recovery.
# Loopback only, one instance, no system service. Requests are never logged here.
# Usage: qwen_runtime.sh start|check|stop   (OLLAMA_HOME defaults to ~/ai-v3-dev/ollama)
set -u
HOME_DIR="${OLLAMA_HOME:-$HOME/ai-v3-dev/ollama}"
BIN="$HOME_DIR/dist/bin/ollama"; PID_FILE="$HOME_DIR/ollama.pid"; LOG="$HOME_DIR/serve.log"
ADDR=127.0.0.1:11434; URL="http://$ADDR"; MODEL="${OLLAMA_MODEL:-qwen2.5vl:7b}"
fail() { printf 'FAIL  %s\n' "$*"; exit 1; }
ok() { printf 'PASS  %s\n' "$*"; }
running_pid() { [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null && cat "$PID_FILE"; }
listeners() { ss -ltnH 2>/dev/null | awk '{print $4}' | grep -E ':11434$'; }
case "${1:-}" in
  start)
    [ -x "$BIN" ] || fail "Ollama binary not found at $BIN"
    if running_pid >/dev/null; then ok "already running (pid $(running_pid))"; exit 0; fi
    listeners >/dev/null && fail "port 11434 is in use by another process; refusing a second instance"
    # Allowlisted environment: no database, mail or auth secrets reach this process.
    env -i HOME="$HOME_DIR/home" PATH="$HOME_DIR/dist/bin:/usr/bin:/bin" OLLAMA_HOST="$ADDR" \
      OLLAMA_MODELS="$HOME_DIR/models" OLLAMA_KEEP_ALIVE="${OLLAMA_KEEP_ALIVE:-10m}" \
      nohup setsid "$BIN" serve >> "$LOG" 2>&1 < /dev/null &
    echo $! > "$PID_FILE"; chmod 600 "$PID_FILE"
    for _ in $(seq 1 30); do curl -fs -m 2 "$URL/api/version" >/dev/null && { ok "started on $ADDR (pid $(cat "$PID_FILE"))"; exit 0; }; sleep 1; done
    fail "did not answer within 30 s; see $LOG"
    ;;
  check)
    bad=0
    version=$(curl -fs -m 5 "$URL/api/version") && ok "server answers: $version" || { printf 'FAIL  server not answering\n'; exit 1; }
    bound=$(listeners | sort -u | tr '\n' ' ')
    [ "$bound" = "$ADDR " ] && ok "bound to loopback only ($bound)" || { printf 'FAIL  unexpected listener: %s\n' "$bound"; bad=1; }
    curl -fs -m 5 "$URL/api/tags" | grep -q "\"$MODEL\"" && ok "model present: $MODEL" || { printf 'FAIL  model missing: %s\n' "$MODEL"; bad=1; }
    nvidia-smi --query-gpu=name,memory.used,memory.total --format=csv,noheader >/dev/null 2>&1 && ok "GPU: $(nvidia-smi --query-gpu=name,memory.used,memory.total --format=csv,noheader)" || { printf 'FAIL  nvidia-smi unavailable\n'; bad=1; }
    grep -q 'library=CUDA' "$LOG" 2>/dev/null && ok "Ollama detected CUDA" || { printf 'FAIL  no CUDA detection in log\n'; bad=1; }
    printf 'INFO  loaded models: %s\n' "$(curl -fs -m 5 "$URL/api/ps")"
    exit $bad
    ;;
  stop)
    pid=$(running_pid) || { ok "not running"; rm -f "$PID_FILE"; exit 0; }
    kill "$pid"; for _ in $(seq 1 10); do kill -0 "$pid" 2>/dev/null || break; sleep 1; done
    kill -0 "$pid" 2>/dev/null && kill -9 "$pid"
    rm -f "$PID_FILE"; ok "stopped"
    ;;
  *) printf 'Usage: %s start|check|stop\n' "$0"; exit 2 ;;
esac
