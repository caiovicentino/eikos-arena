#!/bin/bash
# Starts an arena run. The rules and the engine are copied into a new run folder and hashed into PREREG.sha256
# before the first decision; the engine then runs from that frozen copy (dashboard and API on 127.0.0.1, port 8340
# by default).
#
# usage: JEV_KEY_FILE=/path/to/jev.key scripts/start_arena.sh [hours]
#   JEV_KEY_FILE  file holding a Vercel AI Gateway key that can call typesafe-ai/jev (keep it chmod 600, out of git)
#   EIKOS_URL     Eikos endpoint (default http://127.0.0.1:8232/v1/evaluate)
#   JEV_URL       Jev endpoint (default: the Vercel AI Gateway; any TypeSafe-compatible /v1/evaluate works)
#   PORT          dashboard and API port on 127.0.0.1 (default 8340)
#   SERVE_PY      optional: the serve.py that serves Eikos, hashed into PREREG.sha256 as serve_used.py
#   RUNS          where run folders go (default ./runs, ignored by git)
# If the engine stops, start `python3 arena.py` again with the same --run-dir: it resumes from arena_state.json.
set -eu
ROOT=$(cd "$(dirname "$0")/.." && pwd)
A=$ROOT/arena
HOURS=${1:-72}
EIKOS_URL=${EIKOS_URL:-http://127.0.0.1:8232/v1/evaluate}
JEV_URL=${JEV_URL:-https://ai-gateway.vercel.sh/v1/evaluate}
PORT=${PORT:-8340}
RUNS=${RUNS:-$ROOT/runs}
: "${JEV_KEY_FILE:?set JEV_KEY_FILE to a file holding your Vercel AI Gateway key}"
[ -r "$JEV_KEY_FILE" ] || { echo "cannot read $JEV_KEY_FILE"; exit 1; }
sha() { if command -v sha256sum > /dev/null; then sha256sum "$@"; else shasum -a 256 "$@"; fi; }

curl -s -m 5 "${EIKOS_URL%/v1/evaluate}/health" | grep -q '"ok": true' \
  || { echo "Eikos is not answering at ${EIKOS_URL%/v1/evaluate}/health (see the Eikos-27B model card)"; exit 1; }

RUN=$RUNS/run_$(date -u +%Y%m%d_%H%M)
mkdir -p "$RUN"
cp "$A/RULES.md" "$A/hl.py" "$A/arena_core.py" "$A/arena.py" "$RUN/"
FILES="RULES.md hl.py arena_core.py arena.py"
if [ -n "${SERVE_PY:-}" ]; then cp "$SERVE_PY" "$RUN/serve_used.py"; FILES="$FILES serve_used.py"; fi
(cd "$RUN" && sha $FILES > PREREG.sha256 && date -u +"started %Y-%m-%dT%H:%M:%SZ" >> PREREG.sha256)

cd "$RUN"
nohup python3 arena.py --run-dir "$RUN" --hours "$HOURS" --port "$PORT" --eikos-url "$EIKOS_URL" --jev-url "$JEV_URL" \
  --jev-key-file "$JEV_KEY_FILE" --html "$A/arena.html" > "$RUN/engine.log" 2>&1 < /dev/null &
echo "$!" > "$RUN/engine.pid"
sleep 5
tail -n 3 "$RUN/engine.log"
cat "$RUN/PREREG.sha256"
echo "run folder: $RUN · dashboard: http://127.0.0.1:$PORT"
