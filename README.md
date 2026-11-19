# Pure-Python Git Forge

[![make.fun · github-core-clone-01](https://make.fun/badge/github-core-clone-01.svg)](https://make.fun/bounty/github-core-clone-01?utm_source=github&utm_medium=readme&utm_campaign=github-core-clone-01)
[![rank](https://make.fun/badge/github-core-clone-01/hdyt21/github-core-clone-01/rank.svg)](https://make.fun/bounty/github-core-clone-01/leaderboard?utm_source=github&utm_medium=readme&utm_campaign=github-core-clone-01)
[![reward](https://make.fun/badge/github-core-clone-01/hdyt21/github-core-clone-01/reward.svg)](https://make.fun/bounty/github-core-clone-01?utm_source=github&utm_medium=readme&utm_campaign=github-core-clone-01)

A standalone GitHub-compatible git forge written in pure Python 3.13, using
only the standard library — no git binary, no package manager, no outside
service, a single process. Built for the make.fun competition
`github-core-clone-01` ("Build a GitHub-compatible git forge").

## What it serves (one process, one store)

1. **Git clone + push over smart HTTP** — `git-upload-pack` (v2 and v0)
   and `git-receive-pack` (v0) with pkt-line framing, packfile read/write
   (including OFS/REF deltas), refs, shallow clone and deepen.
2. **REST v3 API** under `/api/v3` — repositories, contents (read/write
   files through the git store), issues, pull requests (with squash merge),
   branches, commits, labels, comments.
3. **GraphQL** at `/api/graphql` — the query/mutation shapes `gh` and
   `github-mcp-server` send (repository, issues, pullRequests, createIssue,
   closeIssue, createPullRequest, mergePullRequest, addComment, node, ...).
4. **MCP tools** (JSON-RPC 2.0) at `/mcp` — `create_repo`, `get_repo`,
   `create_issue`, `list_issues`, `create_pull_request`,
   `merge_pull_request`.

## Run

```sh
python3 main.py
```

The forge reads `MAKEFUN_ENV_URL` and `MAKEFUN_SHOP_TOKEN` from the
environment, binds an ephemeral port (never 8080), registers that port with
the suite, serves until the suite says done, then exits 0.

## Layout

- `main.py` — entrypoint: lifecycle (bind, register, poll `/done`, exit).
- `forge/server.py` — threaded HTTP server, routing, Bearer auth.
- `forge/gitcore.py` — deterministic git object store (SHA-1) plus packfile
  and pkt-line codecs.
- `forge/gitproto.py` — smart-HTTP protocol handlers (info/refs,
  upload-pack, receive-pack).
- `forge/store.py` — the forge's own store: repos, issues, pulls, labels,
  comments; squash merge with conflict detection.
- `forge/rest.py` — REST v3 handlers.
- `forge/graphql.py` — minimal GraphQL executor for the suite's documents.
- `forge/mcp.py` — MCP JSON-RPC tools.

## Determinism

No timestamps, no random IDs, no wall clock anywhere in the state: every
generated commit uses a fixed identity and timestamp, every reply id comes
from a deterministic counter, and the state export is byte-identical across
runs on the same instance.

## Tests

```sh
python3 -m forge.test_rest        # 11 tests
python3 -m forge.test_gitcore     # 52 tests
python3 -m forge.test_graphql_mcp # 34 tests
```

Local suite (kit practice trial-0): **120/120 checks pass**
(feature 600/600, agent 200/200).

## License

MIT — see [LICENSE](LICENSE).
