# LOCAL-RUN — grading the forge locally, without Docker

`run-local.sh` grades the forge in this directory against practice trial-0
(and trials 1–2) using only plain processes. Docker is not available in this
VM, and it is not needed: the kit's env server (`program/env/server.mjs`) is
a standalone Node program, and the kit ships a second local runner
(`sdk/run.ts`) that points the same sealed checks at a server you started
yourself.

## The exact commands

```bash
# Full trial — the sealed env grades the forge, the sealed scorer scores it
./run-local.sh                        # forge dir defaults to this directory
./run-local.sh /path/to/forge --trial 1 --port 8080 --wall 900

# Fast iteration — same 120 checks, no env server, ~seconds
./run-local.sh --fast                 # add --perf to also run the benchmarks
./run-local.sh --fast --json          # machine-readable output

# Exactly as the real grader launches the forge (see the -P warning below)
./run-local.sh --grader-flags

# Other options: --out DIR (default local-runs/trial-N-<timestamp>),
# --url URL (fast mode against an already-running forge), --help
```

`grade-local` also works here — see "grade-local --driver process" below.

## What run-local.sh does (full mode)

1. Reads the trial's `instance_sha256` and `seed` from the kit's
   `rules/instance.yaml` and derives the shop token exactly the way the
   platform does (`cli/makefun.mjs` `trialValues`):
   `shpat_` + `sha256("agent-fun/trial-shop-token/v1\n<instance_sha256>\n<index>").hex[:32]`.
   (The literal trial-0 value is deliberately not written here — it is a
   deterministic test-fixture token derived at runtime by `run-local.sh`,
   and a literal `shpat_` value would trip secret scanners on push.)
   The env and the
   forge only need to agree on the value; deriving the real one keeps the run
   faithful (auth checks present this token, and refuse a missing/wrong one).
2. Starts the env server on port 8080 (override with `--port`):
   `MAKEFUN_STATE_DIR=<out>/state MAKEFUN_ENV_PORT=8080
   MAKEFUN_SHOP_TOKEN=<token> MAKEFUN_TRIAL_WALL_BUDGET_SEC=900
   node <kit>/program/env/server.mjs`
   `MAKEFUN_STATE_DIR` being set is what makes the env bind `127.0.0.1`
   (the process-driver host) instead of `0.0.0.0`, and is where it writes
   `ready`, `calls.ndjson` and `export/{state,perf}.json`. It waits for
   `<state>/ready` (`{"schema":"github-core-clone-01/ready/v1","host":"127.0.0.1","port":8080}`).
   **The env server needs no harness** — it runs standalone; the harness only
   starts it, waits for `ready`, and SIGTERMs it afterwards.
3. Starts the forge with the trial's environment:
   `MAKEFUN_ENV_URL=http://127.0.0.1:8080 MAKEFUN_SHOP_TOKEN=<token>
   MAKEFUN_PROMPT_FILE=<kit>/practice/trial-N/input/prompt.txt
   MAKEFUN_TRIAL_SEED=<seed> python3 main.py`
   The forge binds an ephemeral port, POSTs `{"port": N}` to
   `$MAKEFUN_ENV_URL/register`, then polls GET `$MAKEFUN_ENV_URL/done`
   until `{"done": true}` and exits 0.
4. Waits for `/done`, SIGTERMs the env, then scores the export with the
   sealed scorer:
   `MAKEFUN_EXPORT_DIR=<out>/state/export MAKEFUN_SCORES_DIR=<out>/scores
   node <kit>/program/scorer/score.mjs`

Fast mode instead starts the forge with a dummy `MAKEFUN_ENV_URL`
(`http://127.0.0.1:8080`, nothing listening — `/register` retries forever in
the background while the forge still serves, because it binds first), finds
its port from the `[entry] listening on port N` line, and runs
`node --experimental-strip-types <kit>/sdk/run.ts --url http://127.0.0.1:<port>
--token <token> [--perf] [--json]`.

## What it prints on success

Full mode, verified 2026-11-18 against a starter-equivalent fixture forge
(a Python port of the kit starter: `/healthz`, the API root, JSON 404):

```
env: ready {"schema":"github-core-clone-01/ready/v1","host":"127.0.0.1","port":8080}
env: done after ~4s
forge exit: 0 | env exit: 0
[scorer] score 15/1000: feature 15 + agent 0 + perf 0; perf_p50_us 1000000000, working 1
checks passed: 3/120  working: 1
  git        0/30
  graphql    0/20
  issues     0/15
  mcp        0/15
  protocol   1/ 5
  pulls      0/20
  repos      2/15
failed checks (117): git.advertise-headers, …
run dir: <out>  (env.log, entry.log, state/export/{state,perf}.json, scores/scores.json)
```

Fast mode prints the same columns (`score 15 of 1000; working 1`) plus a
per-check table. The fixture's 3 passing checks are
`protocol.unknown-route-json-404`, `repos.not-found`, `repos.root` — the
kit starter scores exactly the same (verified directly too).

## How to read the score

Total is 0–1000 = **feature** + **agent** + **perf**, from
`program/scorer/score.ts`:
- feature = `round(600 * passed / 120)` — all seven check groups.
- agent = `round(200 * mcp_passed / 15)` — the MCP group again.
- perf = `perf.json` `points_total` (0–200) — the four benchmark workloads
  (push / clone / read / write), each 0–50 points against the house
  reference's calibrated times (`T_ref`: push 1640.82 ms, clone 266.941 ms,
  read 2759.715 ms, write 348.965 ms).
- `perf_p50_us` is the tie-break (geometric mean of the four p50s, in µs).
  `1000000000` is the sentinel for "no workload measured" (the forge could
  not even create the benchmark repos — e.g. every bench setup 404s).
- `working` is 1 when the env's boot check passed and at least one check
  passed; 0 on a boot failure (no register within the 20 s boot budget —
  the export then says `no entry registered before the boot deadline` and
  every check is 0).

Files that matter after a run: `<out>/scores/scores.json`
(`{"score":…,"feature":…,"agent":…,"perf":…,"perf_p50_us":…,"working":…}`),
`<out>/state/export/state.json` (per-check 0/1 bits + per-group counts),
`<out>/state/export/perf.json` (per-workload p50/p99/points), `<out>/env.log`
(the env's `[env] <check> pass|fail` lines), `<out>/entry.log` (the forge).

## grade-local --driver process — VERIFIED WORKING

`--driver process` exists and works in this kit (no Docker needed):

```bash
cd <kit>   # the kit dir (github-core-clone-01-kit/github-core-clone-01-kit)
node cli/makefun.mjs grade-local --kit . --bot /path/to/forge \
  --driver process --instances 1 --save /tmp/grade-local-out
```

Verified 2026-11-18 against a copy of the kit starter (`starter/`):
status `finished`, trial 0 `finished`, live `live_done` (entry exited 0
after 2.5 s, 0 model calls), replay `matched: state.json … again, byte for
byte`, scores `{"agent":0,"feature":15,"perf":0,"perf_p50_us":1000000000,
"score":15,"working":1}` — identical to run-local.sh. L1 regrade:
`reproduced byte for byte`. Notes from that run: upstream is "no model"
(no key nor `--gateway` given — fine here, the competition does not require
model calls, `harness.model_calls: optional`); "this checkout's grader is
not the kit's: node: the server's harness runs v26.13.1, this one v24.22.1".
It runs a live pass *and* a replay pass, so it is slower than
`run-local.sh --fast` and needs a git-snapshotable bot dir; prefer
run-local.sh for iteration and grade-local as a pre-submit cross-check.
For a Python forge it launches `python3 <forge>/main.py`
(`STARTER_SCRIPTS`: node22 → `main.ts`, python3.13 → `main.py`).

## Limitations vs the real grading

- **No isolation, no limits.** Locally the forge runs unisolated on this
  machine with no CPU/memory/pid caps and no internal network; the real
  trial caps it at 2 CPUs / 1024 MiB / 256 pids (env: 2 CPUs / 256 MiB /
  64 pids) and a 900 s wall budget. A forge that is fast locally can still
  be slow under caps — perf points measured here are optimistic.
- **No Docker, no gateway.** The entry makes no model calls in this
  competition, so nothing is lost there; but there is no forwarder and no
  egress control locally — a forge that tried to reach the internet would
  succeed here and fail there.
- **Python 3.12 locally vs 3.13 in the trial.** This VM has Python 3.12.3;
  the trial runs Python 3.13 (`python3.13` runtime; the grader launches
  `python3 -P -s /bot/main.py` with `PYTHONHASHSEED=0`). **Forge code must
  stay 3.12-compatible AND 3.13-compatible: avoid 3.13-only syntax and
  stdlib additions** (e.g. no `type` statement type aliases, no
  3.13-only `http`/`asyncio`/`typing` additions; test under 3.12 here and
  keep to the common subset). `dict | None` annotations etc. are fine on
  both. Likewise Node here is v24.22.1 vs Node 22 in the trial and the
  harness's Node v26.13.1 — the env/scorer/runner are the kit's own bytes,
  so this only matters if you edit kit-adjacent TS (don't).
- **One instance, one seed.** Local runs use the practice trial's fixed
  scenario seed (`6a09e667f3bcc909`); the final grades on fresh seeded data
  in the trial pod. Green locally is evidence, not a guarantee.
- **Perf numbers are noisy.** The house T_refs were measured on the
  production cluster beside live sandbox pods (26% run-to-run spread on the
  tie-break); this VM's timings will differ. Treat local perf points as
  relative (did it get faster/slower than the last run), not absolute.
- **The token is derived, not issued.** The local token is recomputed from
  `rules/instance.yaml`; if the kit's instance pin changed, re-derive (the
  script does this every run). Any shared value would work locally — the
  derivation just keeps the run faithful.

## Known findings about the forge in this directory (builder's job, not this script's)

Checked 2026-11-18 while validating the runner (read-only — no forge code
was written or modified here):

1. **The forge does not start yet.** `main.py` imports `forge.store` and
   `forge.server` imports `forge.gitproto / graphql / mcp / rest` — none of
   those modules exist yet (only `forge/__init__.py` and `forge/server.py`).
   `./run-local.sh` currently reports this cleanly: forge exits with
   `ImportError`, the env records a boot failure after its 20 s budget,
   score `0/1000`, working 0, exit status 1.
2. **`python3 -P -s` will break the import even once the modules exist.**
   Verified: `python3 -P -s -c "import forge.server"` → `ModuleNotFoundError:
   No module named 'forge'`, because `-P` drops the script's own directory
   from `sys.path` and `main.py` does no `sys.path` bootstrap. The real
   grader launches `python3 -P -s /bot/main.py`, so `main.py` needs to
   insert its own directory into `sys.path` (or the launch must set
   `PYTHONPATH`). `run-local.sh` defaults to plain `python3 main.py` and
   offers `--grader-flags` to catch exactly this class of bug.
3. **`main.py` never serves requests.** It calls `server_bind()` /
   `server_activate()` but never `serve_forever()` and never starts a
   serving thread — `create_server()` only builds the server. Once the
   imports exist, the env's boot check (GET `/healthz` twice) will time out
   unless a serving thread is started before `register()`.

## Harness tarball

`harness/harness-src.tar.gz` is the platform's server source (contracts,
grader, gateway) — extracted to `/tmp` for inspection only. It contains no
standalone local runner beyond what `cli/makefun.mjs` already bundles; the
env server + `sdk/run.ts` paths above are the usable ones, and both were
verified instead of building a third driver.
