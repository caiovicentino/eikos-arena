#!/bin/bash
# Starts an arena run: checks the setup, copies the rules and the engine into a new run folder, hashes them into
# PREREG.sha256 before the first decision, and starts the engine from that frozen copy (dashboard and API on
# 127.0.0.1, port 8340 by default).
#
# usage: scripts/start_arena.sh [hours]        (default 72)
#   Jev key, one of:
#     TYPESAFE_API_KEY    Jev through TypeSafe's API
#     AI_GATEWAY_API_KEY  Jev through the Vercel AI Gateway
#     JEV_KEY_FILE        a file holding either key (chmod 600, outside the repo)
#   EIKOS_URL        your Eikos endpoint (default http://127.0.0.1:8000/v1/evaluate); EIKOS_API_KEY if it needs one
#   JEV_URL, JEV_MODEL  another Jev endpoint or model id
#   PORT             dashboard and API port on 127.0.0.1 (default 8340)
#   RUNS             where run folders go (default ./runs, ignored by git)
#   SKIP_CHECKS=1    start without scripts/check_setup.py
# If the engine stops: cd into the run folder and run `python3 arena.py --run-dir .` with the same environment
# (and --jev-key-file if you used JEV_KEY_FILE). It resumes from arena_state.json.
set -eu
ROOT=$(cd "$(dirname "$0")/.." && pwd)
A=$ROOT/arena
HOURS=${1:-72}
PORT=${PORT:-8340}
RUNS=${RUNS:-$ROOT/runs}
if [ -n "${JEV_KEY_FILE:-}" ]; then
  [ -r "$JEV_KEY_FILE" ] || { echo "cannot read JEV_KEY_FILE ($JEV_KEY_FILE)"; exit 1; }
  set -- --jev-key-file "$JEV_KEY_FILE"
else
  set --
fi
sha() { if command -v sha256sum > /dev/null; then sha256sum "$@"; else shasum -a 256 "$@"; fi; }

if [ "${SKIP_CHECKS:-0}" != 1 ]; then
  python3 "$ROOT/scripts/check_setup.py" "$@" \
    || { echo "Fix the problems above, or set SKIP_CHECKS=1 to start anyway."; exit 1; }
fi

RUN=$RUNS/run_$(date -u +%Y%m%d_%H%M)
mkdir -p "$RUN"
cp "$A/RULES.md" "$A/hl.py" "$A/arena_core.py" "$A/arena.py" "$A/arena.html" "$RUN/"
(cd "$RUN" && sha RULES.md hl.py arena_core.py arena.py > PREREG.sha256 \
  && date -u +"started %Y-%m-%dT%H:%M:%SZ" >> PREREG.sha256)

cd "$RUN"
nohup python3 arena.py --run-dir "$RUN" --hours "$HOURS" --port "$PORT" "$@" > "$RUN/engine.log" 2>&1 < /dev/null &
echo "$!" > "$RUN/engine.pid"
sleep 5
tail -n 3 "$RUN/engine.log"
cat "$RUN/PREREG.sha256"
echo "run folder: $RUN · dashboard: http://127.0.0.1:$PORT"
