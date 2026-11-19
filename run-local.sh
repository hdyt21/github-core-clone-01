#!/bin/bash
# run-local.sh — grade the github-core-clone-01 forge locally, WITHOUT Docker.
#
# Full mode (the default): the sealed env server grades the forge exactly
# the way the real trial does — it is the same program/env/server.mjs the
# platform runs, the same 120 conformance checks, the same 4 benchmark
# workloads, the same export (state.json + perf.json) and the same sealed
# scorer.
#
#   ./run-local.sh [FORGE_DIR] [--trial N] [--port N] [--wall SECS]
#                  [--out DIR] [--grader-flags]
#
# Fast mode (--fast): no env server. The forge is started with a dummy
# MAKEFUN_ENV_URL (it keeps retrying /register in the background while it
# serves), and the kit's sdk/run.ts runs the same 120 checks (+ --perf for
# the benchmarks) directly against the forge's port. Same checks, seconds
# instead of a full trial; use it while iterating, full mode before submit.
#
#   ./run-local.sh [FORGE_DIR] --fast [--perf] [--json] [--url URL]
#
# FORGE_DIR defaults to this script's directory and must contain main.py.
# The kit is expected at ~/workspace/makefun/github-core-clone-01-kit/
# github-core-clone-01-kit (override with KIT_DIR). Nothing in the kit is
# modified; every run writes under --out (default local-runs/ in the forge
# dir). See LOCAL-RUN.md for the full notes.
set -u

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# First argument is the forge dir, unless it is an option (then the default
# — this script's own directory — is used).
if [ $# -gt 0 ] && [ "${1#-}" = "$1" ]; then
  FORGE_PATH="$1"; shift
else
  FORGE_PATH="$BASE_DIR"
fi

KIT_PATH="${KIT_DIR:-$HOME/workspace/makefun/github-core-clone-01-kit/github-core-clone-01-kit}"
TRIAL_NUM=0
LISTEN_PORT=8080
WALL_SECS=900
FAST_MODE=0
PERF_MODE=0
JSON_FLAG=0
GRADER_MODE=0
OUT_DIR=""
FORGE_ADDR=""

while [ $# -gt 0 ]; do
  case "$1" in
    --trial) TRIAL_NUM="$2"; shift 2;;
    --port) LISTEN_PORT="$2"; shift 2;;
    --wall) WALL_SECS="$2"; shift 2;;
    --fast) FAST_MODE=1; shift;;
    --perf) PERF_MODE=1; shift;;
    --json) JSON_FLAG=1; shift;;
    --grader-flags) GRADER_MODE=1; shift;;
    --out) OUT_DIR="$2"; shift 2;;
    --url) FORGE_ADDR="$2"; shift 2;;
    -h|--help) sed -n '2,24p' "${BASH_SOURCE[0]}"; exit 0;;
    *) echo "run-local.sh: unknown argument: $1" >&2; exit 2;;
  esac
done

case "$TRIAL_NUM" in 0|1|2) ;; *) echo "run-local.sh: --trial must be 0, 1 or 2" >&2; exit 2;; esac
[ -f "$FORGE_PATH/main.py" ] || { echo "run-local.sh: no main.py in $FORGE_PATH" >&2; exit 2; }
[ -f "$KIT_PATH/program/env/server.mjs" ] || { echo "run-local.sh: kit not found at $KIT_PATH" >&2; exit 2; }
command -v node >/dev/null || { echo "run-local.sh: node is required" >&2; exit 2; }
command -v python3 >/dev/null || { echo "run-local.sh: python3 is required" >&2; exit 2; }

# --- trial values (rules/instance.yaml) -------------------------------------
# instance_sha256 and seed for trial $TRIAL_NUM, e.g. trial 0:
#   seed 371c9c0e0aace432ba4ad68194bada8740441c8bc3b848df49ffee68e7e9a38a
#   instance 2dbde1d1494af3cbb4bfd5e6cd44b3dbab56c5cc7d158ebb9313d9d93b7737fc
TRIAL_INSTANCE_SHA="$(awk -v t="$TRIAL_NUM" '$1=="-" && $2=="t:" && $3==t {f=1} f && $1=="instance_sha256:" {gsub(/"/,"",$2); print $2; exit}' "$KIT_PATH/rules/instance.yaml")"
TRIAL_SEED_HEX="$(awk -v t="$TRIAL_NUM" '$1=="-" && $2=="t:" && $3==t {f=1} f && $1=="seed:" {gsub(/"/,"",$2); print $2; exit}' "$KIT_PATH/rules/instance.yaml")"
if [ -z "${TRIAL_INSTANCE_SHA:-}" ] || [ -z "${TRIAL_SEED_HEX:-}" ]; then
  echo "run-local.sh: could not read trial $TRIAL_NUM from $KIT_PATH/rules/instance.yaml" >&2; exit 2
fi
# The shop token the platform derives for this trial (cli/makefun.mjs
# trialValues): "shpat_" + sha256("agent-fun/trial-shop-token/v1\n
# <instance_sha256>\n<index>").hex[:32]. The env and the forge only need to
# agree on it; deriving the real one keeps the run faithful.
SHOP_TOKEN="$(node -e 'const c=require("node:crypto");process.stdout.write("shpat_"+c.createHash("sha256").update(`agent-fun/trial-shop-token/v1\n${process.argv[1]}\n${process.argv[2]}`,"utf8").digest("hex").slice(0,32))' "$TRIAL_INSTANCE_SHA" "$TRIAL_NUM")"
[ -n "$SHOP_TOKEN" ] || { echo "run-local.sh: token derivation failed" >&2; exit 2; }

RUN_STAMP="$(date +%Y%m%d-%H%M%S)"
if [ -z "$OUT_DIR" ]; then OUT_DIR="$FORGE_PATH/local-runs/trial-$TRIAL_NUM-$RUN_STAMP"; fi
# Absolute: the forge is launched from its own directory (cd), so a relative
# OUT_DIR would make its log land under the forge dir instead of the run dir.
case "$OUT_DIR" in /*) ;; *) OUT_DIR="$PWD/$OUT_DIR";; esac
FORGE_PATH="$(cd "$FORGE_PATH" && pwd)"
mkdir -p "$OUT_DIR"
PROMPT_PATH="$KIT_PATH/practice/trial-$TRIAL_NUM/input/prompt.txt"

echo "== github-core-clone-01 local run =="
echo "forge:   $FORGE_PATH"
echo "kit:     $KIT_PATH"
echo "trial:   $TRIAL_NUM (instance ${TRIAL_INSTANCE_SHA:0:12}…, seed ${TRIAL_SEED_HEX:0:12}…)"
echo "token:   ${SHOP_TOKEN:0:12}… (derived, same for env and forge)"
echo "out:     $OUT_DIR"
echo "python:  $(python3 --version 2>&1) | node: $(node --version)"

# --- forge launcher ----------------------------------------------------------
# Default: plain `python3 main.py` (works with a main.py that imports its own
# package). --grader-flags runs it exactly as the real grader does:
# `PYTHONHASHSEED=0 python3 -P -s main.py` — note -P drops the script's own
# directory from sys.path, so main.py must bootstrap sys.path itself (see
# LOCAL-RUN.md). The env URL/token/prompt/seed below are the trial's.
start_forge() { # $1 = env URL, $2 = log file; sets FORGE_PROC
  # The (cd … && exec python …) & backgrounds a subshell of THIS shell that
  # execs into python, so $! is python's own PID: kill/wait hit the forge.
  if [ "$GRADER_MODE" -eq 1 ]; then
    ( cd "$FORGE_PATH" && exec env PYTHONHASHSEED=0 MAKEFUN_ENV_URL="$1" MAKEFUN_SHOP_TOKEN="$SHOP_TOKEN" \
      MAKEFUN_PROMPT_FILE="$PROMPT_PATH" MAKEFUN_TRIAL_SEED="$TRIAL_SEED_HEX" \
      python3 -P -s main.py >"$2" 2>&1 ) &
  else
    ( cd "$FORGE_PATH" && exec env MAKEFUN_ENV_URL="$1" MAKEFUN_SHOP_TOKEN="$SHOP_TOKEN" \
      MAKEFUN_PROMPT_FILE="$PROMPT_PATH" MAKEFUN_TRIAL_SEED="$TRIAL_SEED_HEX" \
      python3 main.py >"$2" 2>&1 ) &
  fi
  FORGE_PROC=$!
}

stop_children() { # kill any children we started, quietly
  for p in ${CHILD_PIDS:-}; do kill "$p" 2>/dev/null; done
  wait 2>/dev/null
}
trap stop_children EXIT

# --- fast mode: sdk/run.ts straight at the forge -----------------------------
if [ "$FAST_MODE" -eq 1 ]; then
  if [ -n "$FORGE_ADDR" ]; then
    TARGET_URL="$FORGE_ADDR"
    echo "fast: using already-running forge at $TARGET_URL"
  else
    # Dummy env URL: no env server runs, so /register retries forever in the
    # background — but the forge binds its port first, so it still serves.
    start_forge "http://127.0.0.1:$LISTEN_PORT" "$OUT_DIR/entry.log"
    ENTRY_PROC="$FORGE_PROC"
    CHILD_PIDS="$ENTRY_PROC"
    echo "fast: forge pid $ENTRY_PROC (log $OUT_DIR/entry.log)"
    TARGET_URL=""
    for _ in $(seq 1 100); do
      if grep -q "listening on port" "$OUT_DIR/entry.log" 2>/dev/null; then
        P="$(grep -o "listening on port [0-9]*" "$OUT_DIR/entry.log" | head -1 | awk '{print $4}')"
        TARGET_URL="http://127.0.0.1:$P"; break
      fi
      kill -0 "$ENTRY_PROC" 2>/dev/null || { echo "fast: forge exited early — see $OUT_DIR/entry.log"; tail -20 "$OUT_DIR/entry.log"; exit 1; }
      sleep 0.2
    done
    [ -n "$TARGET_URL" ] || { echo "fast: forge printed no listening port — see $OUT_DIR/entry.log"; tail -20 "$OUT_DIR/entry.log"; exit 1; }
    echo "fast: forge at $TARGET_URL"
  fi
  CHECK_ARGS=(--url "$TARGET_URL" --token "$SHOP_TOKEN")
  [ "$PERF_MODE" -eq 1 ] && CHECK_ARGS+=(--perf)
  [ "$JSON_FLAG" -eq 1 ] && CHECK_ARGS+=(--json)
  echo "fast: node --experimental-strip-types sdk/run.ts ${CHECK_ARGS[*]}"
  node --experimental-strip-types "$KIT_PATH/sdk/run.ts" "${CHECK_ARGS[@]}" 2> "$OUT_DIR/fast.err" | tee "$OUT_DIR/fast.log"
  RUN_RC=${PIPESTATUS[0]}
  echo "---"
  echo "fast log: $OUT_DIR/fast.log (stderr: $OUT_DIR/fast.err)"
  [ "$RUN_RC" -eq 0 ] || echo "note: sdk/run.ts exits 1 only when /healthz did not answer; a failing check is reported in the table, never by the exit status."
  exit "$RUN_RC"
fi

# --- full mode: sealed env server + sealed scorer ---------------------------
STATE_DIR="$OUT_DIR/state"
mkdir -p "$STATE_DIR"
ENV_LOG_PATH="$OUT_DIR/env.log"
ENTRY_LOG_PATH="$OUT_DIR/entry.log"

export MAKEFUN_STATE_DIR="$STATE_DIR" MAKEFUN_ENV_PORT="$LISTEN_PORT" \
       MAKEFUN_SHOP_TOKEN="$SHOP_TOKEN" MAKEFUN_TRIAL_WALL_BUDGET_SEC="$WALL_SECS"
node "$KIT_PATH/program/env/server.mjs" >"$ENV_LOG_PATH" 2>&1 &
ENV_PROC=$!
CHILD_PIDS="$ENV_PROC"
echo "env: pid $ENV_PROC (log $ENV_LOG_PATH)"

READY_JSON=""
for _ in $(seq 1 150); do
  if [ -f "$STATE_DIR/ready" ]; then READY_JSON="$(cat "$STATE_DIR/ready")"; break; fi
  kill -0 "$ENV_PROC" 2>/dev/null || { echo "env: server exited early — see $ENV_LOG_PATH"; tail -20 "$ENV_LOG_PATH"; exit 1; }
  sleep 0.2
done
[ -n "$READY_JSON" ] || { echo "env: no <state>/ready in 30s — see $ENV_LOG_PATH"; tail -20 "$ENV_LOG_PATH"; exit 1; }
echo "env: ready $READY_JSON"
ENV_PORT_NUM="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["port"])' "$STATE_DIR/ready")"
ENV_ORIGIN="http://127.0.0.1:$ENV_PORT_NUM"

start_forge "$ENV_ORIGIN" "$ENTRY_LOG_PATH"
ENTRY_PROC="$FORGE_PROC"
CHILD_PIDS="$ENV_PROC $ENTRY_PROC"
echo "forge: pid $ENTRY_PROC (log $ENTRY_LOG_PATH)"

# The env answers {"done":false} while the suite runs and {"done":true} once
# the export is written. Wait at most wall budget + 120 s. If the forge dies
# early, the env still finishes on its own: after the 20 s boot budget it
# writes the boot-failure export (every check 0) and answers done — so keep
# polling up to 60 s past the forge's death instead of giving up at once.
DONE_FLAG=0
POLL_DEADLINE=$((WALL_SECS + 120))
ELAPSED_SECS=0
FORGE_GIVEUP=-1
FORGE_DIED_EARLY=0
while [ "$ELAPSED_SECS" -lt "$POLL_DEADLINE" ]; do
  if curl -s --max-time 2 "$ENV_ORIGIN/done" 2>/dev/null | grep -q '"done":true'; then DONE_FLAG=1; break; fi
  if ! kill -0 "$ENTRY_PROC" 2>/dev/null; then
    if [ "$FORGE_GIVEUP" -eq -1 ]; then
      echo "forge: exited early (before /done) — see $ENTRY_LOG_PATH"; tail -30 "$ENTRY_LOG_PATH"
      echo "forge: waiting up to 60s for the env to record the boot failure…"
      FORGE_GIVEUP=$((ELAPSED_SECS + 60))
      FORGE_DIED_EARLY=1
    elif [ "$ELAPSED_SECS" -ge "$FORGE_GIVEUP" ]; then
      echo "forge: env did not finish the boot failure either"
      break
    fi
  fi
  sleep 2; ELAPSED_SECS=$((ELAPSED_SECS + 2))
done
if [ "$DONE_FLAG" -eq 0 ]; then
  echo "TIMEOUT: env did not say done after ${ELAPSED_SECS}s (cap ${POLL_DEADLINE}s)"
  echo "env log tail:"; tail -20 "$ENV_LOG_PATH"
  echo "forge log tail:"; tail -20 "$ENTRY_LOG_PATH"
  kill -TERM "$ENV_PROC" 2>/dev/null
  exit 1
fi
echo "env: done after ~${ELAPSED_SECS}s"

# The forge exits 0 once it sees done; give it a moment, then stop the env
# (SIGTERM makes it rewrite the export only if it is not worse, and exit 0).
for _ in $(seq 1 15); do kill -0 "$ENTRY_PROC" 2>/dev/null || break; sleep 1; done
if kill -0 "$ENTRY_PROC" 2>/dev/null; then echo "forge: still running after done — killing"; kill "$ENTRY_PROC" 2>/dev/null; fi
wait "$ENTRY_PROC" 2>/dev/null; FORGE_RC=$?
kill -TERM "$ENV_PROC" 2>/dev/null
wait "$ENV_PROC" 2>/dev/null; ENV_RC=$?
echo "forge exit: ${FORGE_RC:-?} | env exit: ${ENV_RC:-?}"

# --- score with the sealed scorer -------------------------------------------
echo "--- export ---"
ls -la "$STATE_DIR/export/" 2>/dev/null || { echo "no export written — see $ENV_LOG_PATH"; tail -20 "$ENV_LOG_PATH"; exit 1; }
MAKEFUN_EXPORT_DIR="$STATE_DIR/export" MAKEFUN_SCORES_DIR="$OUT_DIR/scores" \
  node "$KIT_PATH/program/scorer/score.mjs" | tee "$OUT_DIR/scorer.log"
echo "scores: $OUT_DIR/scores/scores.json"
python3 - "$STATE_DIR/export/state.json" <<'PYEOF'
import json, sys
state = json.load(open(sys.argv[1]))
print("checks passed: %d/%d  working: %d" % (state["counts"]["passed"], state["counts"]["total"], state["working"]))
for group, cnt in sorted(state["counts"]["byGroup"].items()):
    print("  %-9s %2d/%2d" % (group, cnt["passed"], cnt["total"]))
failed = [k for k, v in state["checks"].items() if v == 0]
print("failed checks (%d): %s" % (len(failed), ", ".join(failed[:40]) + (" …" if len(failed) > 40 else "")))
PYEOF
echo "---"
echo "run dir: $OUT_DIR  (env.log, entry.log, state/export/{state,perf}.json, scores/scores.json)"
trap - EXIT
# A forge that died before /done is a failure of the forge, not of the run:
# the score above (a boot failure: 0) is real, but the exit status says so.
[ "$FORGE_DIED_EARLY" -eq 1 ] && exit 1
exit 0
