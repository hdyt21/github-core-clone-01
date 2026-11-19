# SPEC — github-core-clone-01: a GitHub-compatible git forge

Source of truth: kit `github-core-clone-01-kit/` (sealed suite `program/env/src/`,
identical copy in `sdk/conformance/` — verified byte-identical by `diff -rq`),
`program/scorer/`, `starter/`, `competition.yaml`, `rules/instance.yaml`,
`practice/trial-{0,1,2}/`. Runtime: **Node 22 only, standard library only, no
dependencies, no install step, no internet, no git binary.** Entry runs as
`node /bot/main.ts`; `/bot` is read-only, write only under `/tmp`.
Limits per trial: **2 CPUs, 1024 MiB, 256 pids, 900 s wall budget**
(`competition.yaml` / `rules/instance.yaml` limits).

Check groups (120 total): git 30, repos 15, issues 15, pulls 20, graphql 20,
mcp 15, protocol 5. Checks run one at a time, in registration order, each with
its own timeout (5–15 s, clamped 5,000–15,000 ms; default 15,000 ms). A check
that times out is recorded 0 with detail `"deadline"`. The suite is the only
client; it never talks to github.com.

## 0. Rules that hold on every surface

- **One shared store.** REST, GraphQL, MCP and git smart HTTP all read/write the
  SAME store (same repos, issues, PRs, branches, commits, objects, ids).
  Cross-surface reads are checked everywhere (e.g. a GraphQL `createIssue` must
  be visible over REST with the same number/title/state; an MCP `create_repo`
  must be the same object over REST with a stable `id` across repeated reads).
- **Deterministic seeded data.** Every name/path/blob/title the suite sends
  derives from one fixed seed `SCENARIO_SEED = 0x6a09e667f3bcc909`
  (`SCENARIO_SEED_HEX = "6a09e667f3bcc909"`, epoch `2025-01-01T00:00:00Z` =
  `1735689600`). The forge does NOT need to reproduce the seed — it only needs
  to store and echo back exactly what the suite sends. Never invent names.
- **SHA-1 everywhere.** Object format is `sha1` only. Object id =
  SHA-1 over `"<type> <size>\0" + bytes`. Type codes: commit 1, tree 2, blob 3,
  tag 4, OFS_DELTA 6, REF_DELTA 7.
- **JSON discipline (protocol group, 5 checks).** Every REST reply is
  `application/json` (a charset parameter is allowed; `application/vnd.github+json`
  and `+json` also count as JSON), **never HTML** (not `text/html`, and body must
  not start with `<`). Every reply carries a **non-empty
  `X-GitHub-Request-Id` header** (any non-empty value; uniqueness not checked).
  No `X-RateLimit-*` header is required. Error bodies are JSON
  `{"message": <non-empty string>, ...}`. Unknown route = **404 JSON**
  `{"message": "Not Found"}`. Malformed JSON request body = **400 JSON**
  `{"message": ...}`.
- **Auth (see §2).** Missing/wrong token → 401 JSON `{"message": ...}`.
- **`/healthz` takes no auth.**

---

## 1. Lifecycle

Env vars the forge sees:

| Var | Meaning |
|---|---|
| `MAKEFUN_ENV_URL` | Env origin, e.g. `http://store:8080` (in-container env listens on `0.0.0.0:8080`; under the process driver `127.0.0.1:8080`). Trailing slashes must be stripped before use. |
| `MAKEFUN_SHOP_TOKEN` | The single bearer token every API call must carry. The forge must read this env var and compare `Authorization` against it. |

Sequence (starter `main.ts` — keep as-is, grow only `app.ts`):

1. **Bind ephemeral port.** `server.listen(0)`; read the port from
   `server.address().port`. **Never bind 8080** — the env owns it (on the
   one-pod layout it is on the same loopback).
2. **Register.** `POST $MAKEFUN_ENV_URL/register` with JSON body
   `{"port": N}` (N = the bound port, integer 1–65535; extra keys ignored;
   body cap 65,536 bytes). Responses: first register **200 `{"ok": true}`**;
   a later register **409 `{"ok": false, "error": "already_registered"}`**
   (first entry stays dialled; the process keeps waiting). Unparsable body
   **400**; wrong method **405**. The env records
   `http://<peer-address-from-the-request>:N` as the base URL (IPv4-mapped
   IPv6 `::ffff:a.b.c.d` is normalised to IPv4). Retry register/done with
   exponential backoff (starter: 250 ms initial, ×2, cap 2,000 ms; 30 s per
   request timeout). If `MAKEFUN_ENV_URL` is unset, exit 1 at once.
3. **Boot check.** Env calls `GET /healthz` **twice** within 10,000 ms
   (boot budget 20,000 ms). No register before the boot budget = boot failure:
   every check 0, `working: 0`, note `"no entry registered before the boot deadline"`.
4. **Poll done.** `GET $MAKEFUN_ENV_URL/done` — answers **at once** (never held
   open; env answers within 1,000 ms): `{"done": false}` while the suite runs,
   `{"done": true}` once the export is written. Poll with short backoff
   (250 ms → 2,000 ms cap); never hold a request open; never exit early.
5. **Exit 0** once `{"done": true}`. Close the server first, then exit 0.
   The harness then SIGTERMs the env, which writes/keeps the export.

Phases inside the env (for timing context only): boot → conformance (240 s
budget) → benchmark (240 s) → export (30 s). Internal deadline = wall budget
(900 s) − 90 s margin. At the deadline, pending checks are 0 and the export is
still written. A trial still running at 900 s scores 0 on every column.

---

## 2. Auth, headers, error shape

- Every API call (REST, GraphQL, MCP, git) arrives with
  `Authorization: Bearer <MAKEFUN_SHOP_TOKEN>`. `/healthz` takes none.
- **Missing token → 401.** **Wrong token → 401** (see Ambiguities §10: the
  suite only ever sends the correct token or no token; treating any
  non-matching token as unauthenticated is the safe reading of "a request
  without it is 401" + "the entry's only credential").
- 401 body: JSON `{"message": <non-empty string>}` with JSON content type and
  `X-GitHub-Request-Id`. (Checked by `repos.unauthorized`,
  `protocol.error-message`, `graphql.unauthenticated`, `mcp.auth-required`.)
- **Ownership, not 403:** a repository belongs to the login the token names.
  The same repository name under another owner is **404** `{"message": ...}`,
  never 403 (`repos.foreign-token`). The token owner's login is the viewer;
  repos are addressed as `{viewer-login}/{name}`.
- `X-OAuth-Scopes`: `GET /api/v3/` must return header `X-OAuth-Scopes`
  (starter value: `repo, read:org`; the check only requires the header to be
  present — see §3). No other endpoint is checked for this header.
- Request headers the suite sends (accept them; do not require them):
  REST `Accept: application/vnd.github+json`, `Content-Type: application/json`
  on bodies; GraphQL `Accept: application/json`, `User-Agent:
  makefun-github-core-clone-env/1`; git `User-Agent: makefun-git-client/1.0`;
  MCP `Accept: application/json, text/event-stream`,
  `Content-Type: application/json`, `MCP-Protocol-Version`,
  `Mcp-Method` / `Mcp-Name` (see §6).
- Error body shape everywhere: JSON object with a **string `message`**
  (non-empty). Exact message text is NOT asserted except where quoted below
  (`"Not Found"`, `"non-fast-forward"`, GraphQL field name, MCP codes).

---

## 3. REST v3 — `/api/v3`

Base: `<entry>/api/v3`. All responses JSON + `X-GitHub-Request-Id`.
`per_page` / `page` query params are honoured where the suite passes them;
`Link: <url>; rel="next"` (and `rel="last"` etc.) must stay on the entry's own
origin. Default page size when `per_page` is absent is NOT asserted by any
check — any sane default (GitHub's 30) is fine.

### 3.1 Root / meta / user

| Method & path | Request | Response |
|---|---|---|
| `GET /api/v3/` and `GET /api/v3` | — | **200** JSON object (starter: `{"current_user_url": "/api/v3/user", "rate_limit_url": "/api/v3/rate_limit"}` — only "is a JSON object" is asserted), header `X-OAuth-Scopes: repo, read:org` |
| `GET /api/v3/meta` | — | **200** `{"installed_version": <non-empty string>}` (any string) |
| `GET /api/v3/user` | — | **200** `{"login": <token owner's login>, ...}`. Not in the REST check list, but the MCP setup calls it to learn the owner and falls back to the scenario viewer if it refuses — implement it (login is the only field read). |

### 3.2 Repositories (`repos` group, 15 checks)

- `POST /api/v3/user/repos` — body `{"name": string, "private": bool,
  "description": string}` (also `auto_init: false` from the git client).
  → **201** `{"name": <exact name>, "private": bool, "default_branch":
  <non-empty string>, ...}`. `private` must echo the request value (check
  sends `true` and asserts `body.private === true`). `default_branch` must be
  stable across repeated GETs. **Duplicate name:** the pagination check
  accepts **422** for an already-created seeded repo (and then GETs it);
  the git client also accepts 422 whose body says "already exists". Return
  422 with a `{"message": ...}` mentioning already exists for duplicates.
  A repo is created **empty (no refs)** — `clone-empty` requires a fresh repo
  to advertise 0 refs and clone with 0 objects.
- `GET /api/v3/repos/{owner}/{repo}` — → **200**
  `{"name", "private": bool, "default_branch": string, "description": string,
  ...}`; missing repo or wrong owner → **404** `{"message": ...}`.
  `description` must round-trip the POST value and any PATCH value.
- `PATCH /api/v3/repos/{owner}/{repo}` — body `{"description": "..."}` →
  **200** `{"description": <new value>}`; a later GET returns the new
  description.
- `DELETE /api/v3/repos/{owner}/{repo}` — → **204** (empty body is fine;
  status only is asserted); a later GET is **404**.
- `GET /api/v3/user/repos?per_page=10` — → **200** JSON array of
  `{"name": <non-empty string>, ...}`. **Pagination (repos.pagination):**
  the suite creates 30 seeded repos, then follows `Link: <url>; rel="next"`
  from `per_page=10` until no `next`. Requirements: each page ≤ 10 items;
  no repo repeated across pages; all 30 reachable; **at least 3 pages**.
  `Link` URLs must stay on the entry's origin. (Ordering across pages is not
  asserted; only completeness + no duplicates + page count.)
- `GET /api/v3/repos/{owner}/{repo}/branches` — → **200** array of
  `{"name": <non-empty string>, "commit": {"sha": <40-hex>}}`. Must list the
  default branch AND any branch created via `POST /git/refs`, with the
  branch's sha equal to the sha it was created at.
- `GET /api/v3/repos/{owner}/{repo}/commits[?sha=<branch>]` — → **200** array
  of `{"sha": <40-hex>, "commit": {"message": string}}`. With `?sha=<branch>`
  it must list a commit made on that branch (exact message asserted) and
  WITHOUT `?sha=` (default branch) it must NOT list that branch-only commit.
  (Commits are created via contents PUTs and git pushes; the forge must keep
  one commit graph per branch.)
- `POST /api/v3/repos/{owner}/{repo}/git/refs` — body
  `{"ref": "refs/heads/<branch>", "sha": <40-hex>}` → **201**
  `{"ref": "refs/heads/<branch>", "object": {"sha": <40-hex>}}`. Used by the
  REST checks and by the MCP PR setup (fallback: contents PUT with `branch=`).
- `GET /api/v3/repos/{owner}/{repo}/git/refs/heads/{branch}` — → **200**
  `{"ref": ..., "object": {"sha": ...}}` (shape read by MCP setup: it also
  accepts `{"sha": ...}` or `{"commit": {"sha": ...}}`). Not directly scored
  in REST, but implement it — MCP `preparePullRequest` uses it to learn the
  base sha and to test branch existence.
- `GET /api/v3/repos/{owner}/{repo}/branches/{branch}` — → **200**
  `{"name": ..., "commit": {"sha": ...}}`. Same note as above (MCP setup
  fallback for ref lookup / branch existence).

### 3.3 Contents

- `PUT /api/v3/repos/{owner}/{repo}/contents/{path}` — body
  `{"message": string, "content": <standard base64 of file bytes>,
  "branch"?: string, "sha"?: string}`. Path may contain `/` (encode each
  segment; do NOT split on `/` as separate route segments).
  - Create (no `sha`, file absent): **201**
    `{"content": {"sha": <40-hex blob sha>, "encoding": "base64"},
    "commit": {"sha": <40-hex commit sha>}}`.
  - Update (correct current blob `sha`): **200 or 201** (both accepted) with
    the same shape; the new content sha must DIFFER from the old one.
  - Stale `sha` (not the current blob sha): **409** `{"message": ...}` and
    nothing changes.
  - Must work on an **unborn repository** (no refs yet): the first PUT creates
    the default branch and its first commit.
  - `branch` (when given) commits on that branch, creating the branch if
    needed (MCP setup relies on this as the branch-creation fallback).
  - Each PUT is one commit; the commit sha must be a real git commit object
    reachable by git clone/fetch and by `GET /commits`.
- `GET /api/v3/repos/{owner}/{repo}/contents/{path}[?ref=<branch>]` —
  file: **200** `{"type": "file", "encoding": "base64", "content": <base64>,
  "sha": <40-hex blob sha>, "size": <byte length>}`. `content` must base64-decode
  to the exact committed bytes (newlines in the base64 are tolerated by the
  client, which strips whitespace before decoding); `sha` must equal the
  PUT's `content.sha`; `size` must equal the byte length of the content.
  `?ref=` reads the file as of that branch.
  Directory: **200** JSON **array** of
  `{"type": "file"|"dir", "name": <non-empty>, "path": <non-empty>,
  "sha": <40-hex>}` listing every entry in that directory (check creates
  `seed.txt`, `one.txt`, `two.txt` in one directory and requires all three
  listed by `name`).
  Missing file: **404** `{"message": ...}` (also after a delete).
- `DELETE /api/v3/repos/{owner}/{repo}/contents/{path}` — body
  `{"message": string, "sha": <current blob sha>}` → **200**
  `{"commit": {"sha": <40-hex>}}`; a later GET of the path is **404**.

### 3.4 Labels & issues (`issues` group, 15 checks)

- `POST /api/v3/repos/{owner}/{repo}/labels` — body
  `{"name": string, "color": string}` (check sends color `"ededed"`) →
  **201** `{"name": <non-empty string>, ...}` (return `color` too).
  **A label must be created here before an issue may carry it.**
- `POST /api/v3/repos/{owner}/{repo}/issues` — body
  `{"title": string, "body": string, "labels"?: [string]}` → **201**
  `{"number": <int ≥ 1>, "title": <echo>, "body": <echo>, "state": "open",
  "labels": [{"name": ...}, ...]}`. Numbers **increment by 1 per issue within
  a repo**, starting at 1 (the first issue in a fresh repo is #1; PRs share
  the numbering sequence with issues — see §3.5 note below). Labels in the
  response are objects with `name`; the created label must be included when
  requested. **Unknown label → 422** `{"message": ...}` (label name =
  `<existing-label>-not-a-label`, i.e. a name never created via POST /labels).
- `GET /api/v3/repos/{owner}/{repo}/issues?state=&sort=&direction=&per_page=`
  — → **200** array of the issue shape above (every item must satisfy the
  shape: number int ≥1, title string, body string, state `"open"|"closed"`,
  labels array of `{name}`).
  - `state=open` lists only open issues; a closed issue must be absent
    (both the "lists the open one" and "omits the closed one" checks).
  - `sort=created&direction=asc` orders by creation ascending (by number);
    `direction=desc` reverses it (newest first). The check asserts the
    relative order of 3 issues it created, not absolute timestamps.
  - Default (no `state`) behaviour for issues is NOT asserted by a dedicated
    check; GitHub default is `open` — use that.
- `GET /api/v3/repos/{owner}/{repo}/issues/{n}` — → **200** the issue shape;
  number/title/body must equal the created values.
- `PATCH /api/v3/repos/{owner}/{repo}/issues/{n}` — body
  `{"state": "open"|"closed"}` → **200**
  `{"number": <int ≥ 1>, "state": <the requested state>}`; the close must
  persist on a later GET.
- `POST /api/v3/repos/{owner}/{repo}/issues/{n}/comments` — body
  `{"body": string}` → **201**
  `{"id": <int ≥ 1>, "body": <exact echo>}`.
- `GET /api/v3/repos/{owner}/{repo}/issues/{n}/comments` — → **200** array of
  `{"id": <int ≥ 1>, "body": string}`; must contain the posted comment by
  `id` + body.

### 3.5 Pull requests (`pulls` group, 20 checks)

Numbering: issues and PRs share one per-repo counter in real GitHub. The
suite never asserts a PR's absolute number against an issue's number in the
same repo (each fixture uses a fresh repo and only checks `second == first+1`
for two issues, and PR numbers only for equality with the created PR's own
number). Implement a **single shared counter per repo** (issues + PRs) to be
safe; it satisfies every check either way.

- `POST /api/v3/repos/{owner}/{repo}/pulls` — body
  `{"head": <branch>, "base": <branch>, "title": string, "body": string}` →
  **201** `{"number": <int ≥ 1>, "state": "open", "title": <echo>,
  "body": <echo>, "head": {"ref": <head branch>, "sha": <head tip 40-hex>},
  "base": {"ref": <base branch>, "sha": <base tip 40-hex>}}`.
  `head.sha` MUST equal the head branch tip at creation;
  `base.sha` MUST equal the base branch tip at creation (initial commit sha
  in the fixtures). Creating a PR for the same head twice: the GraphQL
  fixture tolerates a **422** on re-creation and then looks up the open PR
  from that branch via `GET /pulls?state=all` — return 422 (not 500) for a
  duplicate open PR from the same head.
- `GET /api/v3/repos/{owner}/{repo}/pulls/{n}` — → **200** the PR shape
  above; unknown number (e.g. `999999999`) → **404** `{"message": ...}`.
  After a merge: `state: "closed"`, `merged: true`,
  `merged_at: <non-empty string>`, `merge_commit_sha: <the merge sha>`.
- `GET /api/v3/repos/{owner}/{repo}/pulls/{n}/files` — → **200** array of
  `{"filename": <non-empty string>, "status": <non-empty string>,
  "additions": <int ≥ 0>, "deletions": <int ≥ 0>, "changes": <int ≥ 0>}`.
  Must list every path changed between base and head. For a new file on the
  head, `status` must be one of `"added"`, `"modified"`, `"changed"` and
  `additions >= 1`. Multi-file PRs must list all changed paths.
- `GET /api/v3/repos/{owner}/{repo}/pulls?state=all|open&per_page=` — →
  **200** array of the PR shape. `state=all` lists open AND merged PRs;
  **default (no `state`) lists open only** (a merged PR must NOT appear).
- `PUT /api/v3/repos/{owner}/{repo}/pulls/{n}/merge` — body
  `{"merge_method": "squash"}` (the suite always sends squash) → **200**
  `{"merged": true, "sha": <40-hex merge commit sha>, "message": <string>}`.
  Effects (all asserted):
  - the PR becomes `state: "closed"`, `merged: true`, with `merged_at`
    non-empty and `merge_commit_sha` == the returned `sha`;
  - the merge commit appears in `GET /commits?sha=<base>` (i.e. on the
    default/base branch);
  - the squash content is on the default branch: `GET
    /contents/<changed-path>?ref=<base>` decodes to the head's file bytes
    (a squash = ONE commit whose tree is base tree + head's changed files).
  - Unknown PR → **404** `{"message": ...}`.
  - Conflicted PR → **405** `{"message": ...}`. Conflict case (exact
    fixture): two branches both modify the SAME file differently from the
    same base; merging the first succeeds (200); merging the second must be
    405 (its file content differs from both base and the now-merged default).
    Rule: a PR is conflicted iff any file it changes was also changed on the
    base since the PR branched (different content than the merge-base
    version), i.e. a three-way merge would conflict. A clean fast-forward /
    non-overlapping change must merge.

### 3.6 Protocol checks (rest of `protocol`, 5)

- Every REST reply (200, 401, 404, 422, unknown route) is JSON, never HTML.
- Unknown `/api/v3/...` route → **404** `{"message": ...}` JSON.
- Malformed JSON body (e.g. raw body `{"name":`) on `POST /user/repos` →
  **400** `{"message": ...}` JSON.
- Every reply carries non-empty `X-GitHub-Request-Id`.
- 401, 404, 422 bodies carry a string `message`.

---

## 4. Git smart HTTP

Remote shape: `<baseUrl>/<owner>/<repo>.git`. No auth is checked on git
(the git client sends `Authorization: Bearer <token>` when configured, but no
check asserts a git 401 — still accept the header and ignore it; do NOT 401
git requests, the suite would fail). Repos are created via REST only; a push
cannot invent a repo. **Missing repo → 404 on `GET /info/refs` for BOTH
services**, and the server must stay healthy afterwards (a known repo still
200s). A malformed POST body (e.g. `zzzznot-a-pkt-line`) must not crash the
server: afterwards the repo must still advertise, list refs and clone intact.

### 4.1 pkt-line framing

- Packet: 4 lowercase-hex length bytes (payload length + 4), then payload.
  `0000` = flush, `0001` = delim, `0002` = response-end. Max packet 65,520
  bytes total (payload ≤ 65,516). Length `0003` is invalid.
- All protocol messages are pkt-line framed; pack data inside sideband is
  also pkt-line framed (see §4.4).

### 4.2 info/refs (advertisement)

`GET /{owner}/{repo}.git/info/refs?service=git-upload-pack` and
`?service=git-receive-pack`.

- **upload-pack, v2** (client sends `Git-Protocol: version=2`): first data
  packet is `version 2\n`, then one capability line per packet, then flush.
  Required capabilities (check `git.advertise-v2` fails if missing):
  `fetch`, `ls-refs`. Also advertise:
  `fetch=shallow` (required: a depth fetch throws client-side if the
  `fetch` value lacks `shallow`), `ls-refs=unborn` (so the client sends
  `unborn` in ls-refs), `agent=...` (optional), `object-format=sha1`.
  Content-Type MUST be `application/x-git-upload-pack-advertisement`.
  The first packet must be `version 2` (or, tolerantly, the
  `# service=git-upload-pack` preamble — the headers check accepts either
  first line for the v2 request, but `advertise-v2` requires protocol 2, so
  answer v2 when `Git-Protocol: version=2` is present).
- **upload-pack, v0** (no `Git-Protocol` header): first data packet is the
  preamble `# service=git-upload-pack\n`, then flush, then ref lines
  `<oid> <ref>\n` (first line carries capabilities after a NUL:
  `<oid> HEAD\0<space-separated caps>\n`), peeled tags as
  `<oid> <ref>^{}\n`, `symref=HEAD:refs/heads/main` in caps.
  Content-Type `application/x-git-upload-pack-advertisement`.
  Empty repo: advertise the pseudo-ref `capabilities^{}` with the zero oid
  (`0000000000000000000000000000000000000000 capabilities^{}\0<caps>`)
  and no real refs; HEAD symref still `refs/heads/main`.
  v0 caps the client needs: `side-band-64k` (a v0 server without it "cannot
  be framed" — fetchV0 throws), `thin-pack`, `ofs-delta`, `shallow`,
  `multi_ack` / `multi_ack_detailed` (optional), `agent`.
- **receive-pack (v0 ONLY)**: `GET .../info/refs?service=git-receive-pack`
  (client sends NO `Git-Protocol` header here). First data packet
  `# service=git-receive-pack\n`, then flush, then v0 ref lines as above.
  Content-Type `application/x-git-receive-pack-advertisement`. Required
  caps: **`report-status`, `side-band-64k`, `delete-refs`** (missing any
  fails `git.advertise-receive-pack`). Also advertise `report-status-v2`
  (client prefers it when offered; both parse the same way),
  `ofs-delta`, `agent`. `refs/heads/main` must be listed at the current tip.
  If the server answered v2 for receive-pack it fails (receive-pack must
  advertise protocol v0).

### 4.3 upload-pack v2 — ls-refs and fetch

`POST /{owner}/{repo}.git/git-upload-pack`, `Content-Type:
application/x-git-upload-pack-request`, `Accept:
application/x-git-upload-pack-result`, `Git-Protocol: version=2`.
Response Content-Type: `application/x-git-upload-pack-result`.

**ls-refs** — request pkt-lines:
`command=ls-refs\n`, optional `agent=...\n`, `object-format=sha1\n`, delim
(`0001`), then `peel\n`, `symrefs\n`, `unborn\n` (only if advertised),
`ref-prefix <prefix>\n` (zero or more), flush.
Response pkt-lines, one ref per line:
`<oid> <ref> [symref-target:<target>] [peeled:<oid>]\n`, plus for unborn
refs `unborn <ref> [symref-target:<target>]\n`, then flush.
Required behaviour (checked):
- `HEAD` line has `symref-target:refs/heads/main`.
- Annotated tag refs point at the TAG object oid and carry
  `peeled:<commit oid>`.
- `ref-prefix` filtering is done SERVER-side: a `refs/heads/` request must
  return ONLY `refs/heads/*` (raw list is checked, not just client-filtered);
  `refs/tags/` likewise; a non-matching prefix returns ZERO refs.
- `peel` / `symrefs` are honoured (client requests them by default).

**fetch** — request pkt-lines: `command=fetch\n`, optional agent /
object-format lines, delim, then `thin-pack\n`, `no-progress\n`,
`ofs-delta\n`, optional `filter ...\n`, `want <oid>\n` (one per want),
`have <oid>\n`, `shallow <oid>\n` (current shallow boundary, when deepening),
`deepen <n>\n`, `deepen-since <ts>\n`, `deepen-not <ref>\n`, `done\n`, flush.
Response sections, each introduced by a bare line packet then delim/flush
separated:
`acknowledgments\n` (lines `NAK`, `ACK <oid>`, `ready`), `shallow-info\n`
(lines `shallow <oid>`, `unshallow <oid>`), `wanted-refs\n` (lines
`<oid> <ref>`), `packfile\n` (sideband packets: first byte = band,
**1 = pack data, 2 = progress text, 3 = error**; band 1 bytes concatenated
= the packfile). End with flush.
Rules:
- **Only objects the client does not have are sent.** Incremental fetch
  (`want=new-tip, have=old-tip`): send ONLY the new tip's reachable closure
  minus the old tip's closure; re-sending the old tip fails the check.
  Fetch with tip already held: send **0 objects** (an empty pack — `PACK`
  v2, count 0, 32 bytes — in band 1, or omit the packfile section entirely;
  do NOT emit a packfile section with zero sideband packets, the client
  throws on that).
- **Shallow:** `deepen 1` (depth-1 clone) delivers ONLY the tip commit + its
  tree/blobs — NO parent commits — and `shallow-info` must name the tip
  (`shallow <tip>`). `deepen 2` from a depth-1 clone delivers exactly one
  more generation (the parent + its tree/blobs, NOT the grandparent) and
  moves the boundary (`shallow <parent>` and/or `unshallow <old-tip>`).
  A shallow fetch of a new tip (`depth 1`, `have=<old tip>`,
  `shallow=<old boundary>`) must NOT pull old history or re-send the parent.
- Pack checksum/trailer must verify (see §4.5). Deltas are OPTIONAL:
  the server may send plain (non-delta) objects; `pack-deltas` passes with
  0 deltas as long as every object resolves and hashes correctly. If the
  server DOES send deltas, OFS_DELTA (6) and REF_DELTA (7) must resolve
  (thin-pack bases are objects the client already has).
- **v0 fallback:** if the server answered v0, the client instead sends
  `want <oid> <caps-on-first-want>\n` lines, `shallow`/`deepen` lines, flush,
  then `have` lines + `done\n`, flush (two sections — a single section makes
  the client fail). Response: `NAK`/`ACK`/`shallow`/`unshallow` line packets
  then sideband packets (band 1 = pack). The rules say a v0 answer loses
  the three v2 checks (`advertise-v2`, `ls-refs`, `ls-refs-prefix`) while
  the rest fall back to v0 — so v2 is required for full marks, v0 must still
  work.

### 4.4 receive-pack v0 — push

`POST /{owner}/{repo}.git/git-receive-pack`, `Content-Type:
application/x-git-receive-pack-request`, `Accept:
application/x-git-receive-pack-result`. Response Content-Type:
`application/x-git-receive-pack-result`.
Request body: pkt-line command list, then the pack (raw bytes, NOT
pkt-framed):
- Each command: `<old-oid> <new-oid> <ref>\n`; the FIRST command line carries
  capabilities after a NUL: `<old> <new> <ref>\0<space-separated caps>\n`
  (client sends `report-status` or `report-status-v2`, `side-band-64k`,
  `ofs-delta`, `agent=...` when advertised). Then flush (`0000`).
- Then the packfile bytes (always present for create/update commands, even
  if it carries 0 objects — "a bare command list" is an unpack error; a
  delete-only command list carries NO pack).
- `old = 0000...0` = create; `new = 0000...0` = delete; otherwise update.
Response: the report-status stream, pkt-line framed, inside sideband
**band 1** when `side-band-64k` was negotiated (band 2 = progress, band 3 =
error), otherwise as raw pkt-lines. Report lines (each `\n`-terminated):
```
unpack ok
ok <ref>
ng <ref> <reason>
option <key> <value>      (report-status-v2 only, after its ref's line)
```
- `unpack ok` MUST be present (a refused update must still unpack ok).
- **Fast-forward rule (checked exactly):** an update is accepted only if
  old == current tip AND new is a descendant of old (or old is zero =
  create, which always succeeds if the ref does not exist / new exists as an
  object). A non-fast-forward update is refused PER REF with reason
  exactly **`non-fast-forward`** — i.e. line
  `ng <ref> non-fast-forward` — and the ref MUST NOT move. HTTP status stays
  200 (a refused ref is a successful exchange, never an HTTP error).
- **Mixed push:** one command list with one fast-forward and one refused
  ref applies ONLY the good ref, and the report has exactly one line per
  ref (`ok` for the first, `ng ... non-fast-forward` for the second), in
  command order.
- Deletes: `old=<tip> new=0000...0` removes the ref (branch or tag); report
  `ok <ref>`; the ref disappears from ls-refs/advertisement afterwards and
  other refs are untouched.
- After a push, the objects must be stored durably and visible to clone /
  `GET /commits` / contents reads. A push of a 100-file single commit must
  round-trip byte-exactly (all trees/blobs).
- The server must verify/accept the pack the client sends: the client sends
  plain (non-delta) packs built by `writePack` (PACK v2). Store every object
  whose SHA-1 recomputes; a corrupt pack must fail `unpack` (not crash).

### 4.5 Packfile & object formats

- Pack: `PACK` + BE32 version (**2**; the client also reads 3) + BE32 object
  count + entries + 20-byte trailer = **SHA-1 of all preceding pack bytes**.
- Entry: varint type/size header (type in bits 4–6 of first byte, size low
  4 bits then 7-bit little-endian continuation groups), then zlib-deflated
  object bytes. OFS_DELTA: negative-offset varint to the base entry, then
  delta instructions. REF_DELTA: 20-byte base oid, then delta instructions.
  Delta instruction stream: varint source size, varint target size, then
  ops — copy (`0x80` bit set: offset/size bytes per flag bits) or insert
  (low 7 bits = literal length; opcode 0 invalid); copy size 0 means 0x10000.
- Object bytes:
  - blob: raw file bytes.
  - tree: entries sorted by name: `<mode> <name>\0<20-byte raw oid>`;
    file mode `100644`, directory mode `40000`.
  - commit: `tree <oid>\n` + `parent <oid>\n`* + `author <Name> <<email>>
    <unix-sec> +0000\n` + `committer <same>\n` + `\n` + message. The client
    builds commits with `buildCommit` (message as given, no forced trailing
    newline added by the server — store commit bytes EXACTLY as pushed; the
    clone check compares bytes and parses `author`/`committer` timestamps and
    the viewer email).
  - tag: `object <oid>\ntype commit\ntag <name>\ntagger <Name> <<email>>
    <unix-sec> +0000\n\n<message>`.
- Clone verification: every cloned object's SHA-1 must recompute to its id;
  the reachable closure from the tip must be complete; commit bytes,
  parents, message and author/committer lines must round-trip byte-exactly;
  trees must round-trip paths, modes and nested directories; tag objects
  must round-trip and peel (`refs/tags/v1` → tag oid, `^{}` → commit oid).

---

## 5. GraphQL — `POST /api/graphql`

- Transport: `POST /api/graphql`, `Content-Type: application/json`,
  `Accept: application/json`, `Authorization: Bearer <token>`,
  `User-Agent: makefun-github-core-clone-env/1`. Body JSON
  `{"query": <document string>, "variables": {...}}` (variables may be `{}`).
  Answer JSON `{"data": {...}, "errors": [...]}`.
- **Unauthenticated → HTTP 401** (exactly; no GraphQL body needed).
- **GraphQL-level failure (unknown field, bad argument) → HTTP 200 with an
  `errors` array** — never an HTTP error. Each error needs at least
  `{"message": <string>}`; for the invalid-field check the message MUST
  contain the field name (`noSuchFieldOnViewer`).
- **Batching:** a JSON **array** of operation documents in one body is
  answered by a JSON **array** of per-operation `{"data","errors"}` bodies
  **in the same order**.
- **Global ids are opaque and stable:** `Repository.id`, `Issue.id`,
  `PullRequest.id`, comment `id` must be non-empty strings, equal on repeated
  reads, and MUST NOT be the plain numeric id (`databaseId` for repos,
  `number` for issues — e.g. base64 / prefixed encoding is fine).
- **Same store as REST** (see §0). Mutations must be visible over REST
  immediately (same number/title/state/head/base).

### 5.1 The exact documents (copy verbatim — the suite sends these strings)

```graphql
query ViewerLogin { viewer { login } }

query RepositoryFields($owner: String!, $name: String!) {
  repository(owner: $owner, name: $name) {
    id
    databaseId
    name
    owner { login }
    defaultBranchRef { name }
    viewerPermission
    hasIssuesEnabled
  }
}

query Issue_fields {
  Issue: __type(name: "Issue") {
    fields(includeDeprecated: true) { name }
  }
}

query PullRequestAndRepositoryFields {
  PullRequest: __type(name: "PullRequest") {
    fields(includeDeprecated: true) { name }
  }
  Repository: __type(name: "Repository") {
    fields(includeDeprecated: true) { name }
  }
}

query SearchType_enumValues {
  SearchType: __type(name: "SearchType") {
    enumValues(includeDeprecated: true) { name }
  }
}

query OpenIssues($owner: String!, $name: String!, $first: Int!) {
  repository(owner: $owner, name: $name) {
    issues(first: $first, states: [OPEN], orderBy: {field: CREATED_AT, direction: DESC}) {
      nodes { number title state id }
    }
  }
}

query IssueByNumber($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) { number title state id body }
  }
}

mutation IssueCreate($input: CreateIssueInput!) {
  createIssue(input: $input) { issue { number id } }
}

mutation IssueClose($input: CloseIssueInput!) {
  closeIssue(input: $input) { issue { number id state } }
}

query PullRequests($owner: String!, $name: String!, $first: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequests(first: $first) {
      nodes { number headRefName baseRefName mergeable state }
    }
  }
}

query PullRequestFiles($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      number
      files(first: 10) { nodes { path } }
    }
  }
}

query PullRequestId($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) { id number }
  }
}

mutation PullRequestCreate($input: CreatePullRequestInput!) {
  createPullRequest(input: $input) {
    pullRequest { number id state headRefName baseRefName }
  }
}

mutation MergePullRequest($input: MergePullRequestInput!) {
  mergePullRequest(input: $input) {
    pullRequest { number merged state }
  }
}

mutation AddComment($input: AddCommentInput!) {
  addComment(input: $input) {
    commentEdge { node { id body } }
  }
}

query ViewerInvalidField {
  viewer { login noSuchFieldOnViewer }
}

query NodeRepository($id: ID!) {
  node(id: $id) {
    id
    ... on Repository { name nameWithOwner }
  }
}

query IssuesPage($owner: String!, $name: String!, $first: Int!, $after: String) {
  repository(owner: $owner, name: $name) {
    issues(first: $first, after: $after) {
      nodes { number }
      pageInfo { hasNextPage endCursor }
    }
  }
}
```

### 5.2 Field / type requirements (from the documents + checks)

- `Query.viewer { login }` = token owner's login.
- `Query.repository(owner:, name:)` → `Repository` or **null** (unknown repo:
  `data.repository = null` with **no errors** — the `data` object must still
  be present, `errors` absent/empty). Fields: `id` (opaque global id),
  `databaseId` (int, the REST numeric repo id), `name`, `owner { login }`,
  `defaultBranchRef { name }` (== default branch, asserted when branches
  exist), `viewerPermission` ∈ `ADMIN | MAINTAIN | WRITE | TRIAGE | READ`
  (any one is accepted), `hasIssuesEnabled: true`.
- `Query.node(id:)` → the node with that global id; `... on Repository`
  yields `name` and `nameWithOwner` (`"<owner>/<repo>"`).
- `Repository.issue(number:)` → Issue `{number, title, state, id, body}`
  (`state` is GraphQL enum `"OPEN"`/`"CLOSED"` here, NOT lowercase).
- `Repository.issues(first:, states:, orderBy:, after:)` →
  `{nodes: [...], pageInfo: {hasNextPage, endCursor}}` (pageInfo required at
  least for the `IssuesPage` document; `OpenIssues` only reads `nodes`).
  Semantics: `states: [OPEN]` omits closed issues; `orderBy: {field:
  CREATED_AT, direction: DESC}` returns issue numbers in **descending**
  order (ties by number DESC). Cursor pagination: `first: 5, after: null`
  returns 5 nodes + `hasNextPage: true` + non-empty `endCursor`; the next
  page with `after: <endCursor>` returns the remaining nodes (fixture: 7
  issues → pages of 5 + 2) with `hasNextPage: false`; no issue appears on
  both pages; all 7 are returned across the two pages. (Cursor value format
  is free — opaque string — but it must be non-empty and must resume
  correctly.)
- `Repository.pullRequests(first:)` → `{nodes: [{number, headRefName,
  baseRefName, mergeable, state}]}`; `mergeable` ∈ `MERGEABLE | CONFLICTING |
  UNKNOWN`; `state` ∈ `OPEN | CLOSED | MERGED`; at most `first` nodes.
- `Repository.pullRequest(number:)` → PullRequest `{number, id, ...}` and
  `files(first: 10) { nodes: [{path}] }` listing the changed paths (must
  include the fixture's changed file path).
- **Introspection** `__type(name:)` with aliases (`Issue: __type(...)`,
  `PullRequest:`, `Repository:`, `SearchType:`) and
  `fields(includeDeprecated: true) { name }` /
  `enumValues(includeDeprecated: true) { name }`:
  - `Issue` fields must include: `id, number, title, state, body`.
  - `PullRequest` fields must include: `id, number, headRefName,
    baseRefName, mergeable, state, merged, files`.
  - `Repository` fields must include: `id, databaseId, name, nameWithOwner,
    owner, defaultBranchRef, viewerPermission, hasIssuesEnabled, issue,
    issues, pullRequests, pullRequest`.
  - `SearchType` enumValues must include `ISSUE`.
  (Other fields/enum values may be added freely. Enums the schema should
  know: `IssueState` OPEN/CLOSED, `MergeableState`
  MERGEABLE/CONFLICTING/UNKNOWN, `PullRequestState` OPEN/CLOSED/MERGED,
  `PullRequestMergeMethod` MERGE/SQUASH/REBASE, `RepositoryPermission`
  ADMIN/MAINTAIN/WRITE/TRIAGE/READ, `IssueOrderField`
  CREATED_AT/UPDATED_AT/COMMENTS, `OrderDirection` ASC/DESC.)
- **Mutations** (input objects, one `input:` argument each):
  - `createIssue(input: {repositoryId, title, body})` →
    `{createIssue: {issue: {number, id}}}`; the issue must appear over REST
    as open with the same title.
  - `closeIssue(input: {issueId})` →
    `{closeIssue: {issue: {number, id, state: "CLOSED"}}}`; REST must then
    show the issue `closed`.
  - `createPullRequest(input: {repositoryId, baseRefName, headRefName,
    title})` → `{createPullRequest: {pullRequest: {number, id, state:
    "OPEN", headRefName, baseRefName}}}`; the head branch must already have
    been pushed over git; REST must read the PR back with the same head/base.
    (`body` is not sent by this document — do not require it.)
  - `mergePullRequest(input: {pullRequestId, mergeMethod: "SQUASH"})` →
    `{mergePullRequest: {pullRequest: {number, merged: true, state:
    "CLOSED"}}}` (GraphQL `state` here is the enum string; REST must show
    `merged: true`, `state: "closed"`). The merge is a squash onto the
    default branch, same semantics as REST §3.5.
  - `addComment(input: {subjectId, body})` →
    `{addComment: {commentEdge: {node: {id, body}}}}` (body echoed exactly);
    the comment must appear in REST `GET /issues/{n}/comments`.
- Variables: string/int variables, inline enum lists (`states: [OPEN]`),
  inline input objects (`orderBy: {field: CREATED_AT, direction: DESC}`),
  null variables (`after: null`), operation names, and batched arrays must
  all be handled. Only the documents above are sent — a general GraphQL
  executor is NOT required, but the parser must handle exactly these
  documents (including the `__type` aliases and the `... on Repository`
  inline fragment).

---

## 6. MCP — `POST /mcp`

JSON-RPC 2.0 over Streamable HTTP at `<baseUrl>/mcp`. Answer either
`application/json` (one JSON-RPC object) or `text/event-stream` (SSE, final
data frame = the JSON-RPC response). **Every request must answer within
3 seconds** (client timeout `MCP_DEFAULT_TIMEOUT_MS = 3000`).

### 6.1 Two eras — implement the modern one, tolerate the legacy one

**Modern (preferred, revision `2026-07-28`):**
- No handshake, no session. Every request declares its revision in
  `params._meta["io.modelcontextprotocol/protocolVersion"]` (plus a plain
  `params._meta.protocolVersion` key, plus `params._meta` carrying
  `io.modelcontextprotocol/clientInfo` `{name:
  "makefun-github-core-clone-env", version: "1.0.0"}` and
  `io.modelcontextprotocol/clientCapabilities: {}`), and mirrors it in the
  `MCP-Protocol-Version` request header. Every POST also carries
  `Accept: application/json, text/event-stream`, `Content-Type:
  application/json`, `Mcp-Method: <method>` and, for `tools/call`,
  `Mcp-Name: <tool name>` headers, and `Authorization: Bearer <token>`.
- `server/discover` (method, params `{}` + `_meta`) is **mandatory** in this
  revision: answer a result with a non-empty `supportedVersions` list of
  `YYYY-MM-DD` revision strings (must include `2026-07-28` or a version the
  client accepts; the client picks `2026-07-28` if listed, else the first
  listed). The result should also carry `serverInfo: {name, version}`,
  `capabilities: {tools: {...}}` (and optionally `instructions`,
  `resultType`). If `server/discover` answers JSON-RPC `-32022`
  (UnsupportedProtocolVersionError), its `error.data.supported` must name
  usable versions — the client retries with one of them.
- If the server does NOT implement `server/discover`, the client falls back
  to legacy (see below) — the discover check then passes as "pre-discover
  revision". **Implementing modern `server/discover` is the simpler path.**

**Legacy (2025-11-25 and earlier — accepted, not required):**
- `initialize` with params `{protocolVersion: <one of 2025-11-25,
  2025-06-18, 2025-03-26, 2024-11-05> (client offers newest first),
  capabilities: {}, clientInfo: {...}}` → result
  `{protocolVersion: <the revision the server chose — echo one the client
  offered>, capabilities: {tools: {...}}, serverInfo: {name, version}}`.
  Then `notifications/initialized` (a notification: no `id`, no response
  body required). An optional `Mcp-Session-Id` response header may be sent;
  the client carries it onward if present, and `DELETE /mcp` with that
  header is a best-effort session close (ignore it safely).
- A pre-discover server answers `server/discover` with **404** or JSON-RPC
  **-32601** (Method not found) — either is accepted as "pre-discover".

### 6.2 initialize / serverInfo (mcp.initialize)

Whichever era negotiates, the settled connection must yield:
`serverInfo.name` and `serverInfo.version` non-empty strings, and
`capabilities.tools` present (an object; may be `{}`).

### 6.3 tools/list

- Method `tools/list`, params `{}` (+ `_meta`; paginated via `cursor` /
  `nextCursor` — the client follows `nextCursor` for up to 10 pages; a
  single page with no `nextCursor` is fine).
- Result: `{tools: [...]}`. Requirements:
  - **At least 6 tools.**
  - All six required tools present, matched by the suite as: exact canonical
    name, or an obvious alias, or a name whose normalised form (lowercase,
    non-alphanumerics removed) **ends with** the canonical/alias normalised
    form — so a prefix such as `github__create_issue` is allowed, matched as
    a suffix, case-insensitively. Canonical names + aliases:
    - `create_repo` — aliases: `create_repository`, `repo_create`,
      `repos_create`, `repository_create`
    - `get_repo` — `get_repository`, `repo_get`, `repos_get`, `repository_get`
    - `create_issue` — `issue_create`, `issues_create`
    - `list_issues` — `issues_list`, `issue_list`
    - `create_pull_request` — `pull_request_create`, `pulls_create`,
      `create_pr`, `pr_create`
    - `merge_pull_request` — `pull_request_merge`, `pulls_merge`,
      `merge_pr`, `pr_merge`
  - **Every tool** needs a **non-empty `description`** and an
    **`inputSchema` of `{"type": "object", ...}`** (with `properties` and
    `required` as usual JSON Schema).
  - **Byte-identical across calls:** two `tools/list` calls must return the
    same tools array byte for byte (same order, same JSON). No timestamps,
    no random ids, no per-call variation.

### 6.4 tools/call — argument shapes

Method `tools/call`, params `{"name": <tool name>, "arguments": {...}}`.
Result shape: `{content: [{type: "text", text: <JSON string of the result
object>}], structuredContent: <result object>?, isError: false}` — the suite
reads `structuredContent` first, else parses JSON out of the text blocks
(it also finds JSON wrapped in prose, outermost `{...}`/`[...]`). So:
**always include the result object as JSON in a text block AND/OR as
`structuredContent`**, with the fields named below (the suite looks for
`number`/`pull_number`/`full_name` etc. in that JSON).
Argument mapping: the suite maps its documented argument names onto the
tool's own `inputSchema.properties` by synonym (see below) and fills a
property named `full_name` (or `repo_full_name` / `repository_full_name` /
`fullName`) with `"owner/repo"`. **A tool whose `inputSchema.required`
contains a property the suite cannot fill fails its check** — so keep
`required` to exactly the documented arguments (or fewer). Synonyms the
suite recognises (normalised matching):
- owner: `owner, owner_login, org, organization, organisation, user,
  user_login, login, account, namespace, owner_name`
- repo: `repo, repository, repository_name, repo_name, repo_slug, project,
  project_name`
- title: `title, issue_title, pr_title, pull_request_title, subject`
- body: `body, issue_body, description, content, text, comment`
- head: `head, head_ref, head_branch, pull_request_head, pr_head, source,
  source_ref, source_branch, from, from_branch, branch`
- base: `base, base_ref, base_branch, pull_request_base, pr_base, target,
  target_ref, target_branch, into, into_branch`
- pull_number: `pull_number, pull_request_number, pr_number, number,
  issue_number, index`
- name: `name, repo, repository, repository_name, repo_name`
- description: `description, about, summary, body`
- private: `private, is_private`
- state: `state, status`

Documented calls (github-mcp-server's names):
| Tool | Arguments the suite sends | Effect / result the suite checks |
|---|---|---|
| `create_repo` | `{name, description?, private?}` (suite sends `description` + `private: true` in rest-parity; `owner` is NOT sent — the repo belongs to the token owner) | Creates the repo; result JSON should carry `full_name: "owner/repo"` (suite splits it to learn owner/repo). REST `GET /repos/{owner}/{repo}` must then 200 with the same repo, `id` stable across two reads. |
| `get_repo` | `{owner, repo}` | Result text/JSON must name the repo (the concurrent-calls check requires the result text to contain the repo name, and the two concurrent calls' answers to carry their own JSON-RPC ids and their own repos). |
| `create_issue` | `{owner, repo, title, body?}` | Creates the issue; REST `GET /repos/{owner}/{repo}/issues?state=all` must list it with the same title. Result JSON should carry `number`. |
| `list_issues` | `{owner, repo, state?}` (suite sends `state: "open"`) | Result text must contain the title of an issue that exists in the repo, or a `"number": <n>` matching it (checked by substring / `"number":\s*<n>` regex). |
| `create_pull_request` | `{owner, repo, title, head, base}` (suite also sends `body` in one path) | Opens the PR; REST must read it back with the same `head.ref` and `base.ref`. Result JSON should carry `number` (else the suite falls back to finding the PR by title/head in the list). |
| `merge_pull_request` | `{owner, repo, pull_number}` | Merges the PR (squash semantics as §3.5); REST `GET /pulls/{n}` must then show `merged: true`. |

All six act on the SAME store as REST (§0): ids unchanged across reads.

### 6.5 Errors & edge cases

- **Unknown tool** (`tools/call` with a name like `mcp_no_such_tool_<tag>`):
  JSON-RPC error code **-32602 or -32601**, **never HTTP 500** (any other
  error code fails; a success result fails).
- **Missing required arguments** (`tools/call create_issue` with `{}`):
  answer either `isError: true` in the tool result, or JSON-RPC error
  **-32602**. Accepting the call (success, `isError` absent/false) fails.
- **Non-JSON body**: answer **HTTP 400 with JSON-RPC -32700** (parse error).
  Exception: a pre-2026-07-28 (legacy) server may answer HTTP 200 with
  -32700 — that also passes. The check sends the truncated body
  `{"jsonrpc": "2.0", "id": 424242, "method": "tools/list"` (unterminated).
- **No Authorization header** → **HTTP 401** (checked on `tools/call`;
  auth is decided before any tool lookup, so 401 even for an unknown tool
  name).
- **Concurrent calls:** two `tools/call` in flight must keep their own
  JSON-RPC `id`s — each response's `id` must equal the request's `id`
  (string ids are used: `concurrent-left-<tag>`, `concurrent-right-<tag>`),
  and each answer must be for its own arguments (own repo).
- **3-second limit:** every request must answer within 3 s.
- Notifications (`notifications/initialized`, etc.) carry no `id`; no
  JSON-RPC response is required for them. A `DELETE /mcp` (legacy session
  close) may be ignored/answered 200 or 405 — the client ignores the result.
- JSON-RPC envelope: request `{"jsonrpc": "2.0", "id": <string|number>,
  "method": ..., "params": {...}}`; success
  `{"jsonrpc": "2.0", "id": <same>, "result": {...}}`; error
  `{"jsonrpc": "2.0", "id": <same>, "error": {"code": <int>, "message":
  <string>, "data"?: ...}}`. The `id` in the response MUST match the
  request's `id` (the concurrent check asserts this for string ids).
- SSE form (if used): `Content-Type: text/event-stream`, frames
  `data: <JSON-RPC response JSON>\n\n`; the response is the last data frame
  (the client reads until the frame whose `id` matches the request).

### 6.6 MCP setup routes it relies on (REST)

The MCP checks build fixtures over REST, so these must work (all covered in
§3): `GET /api/v3/user`, `POST /api/v3/user/repos`, `GET
/api/v3/repos/{owner}/{repo}`, `PUT /contents/{path}` (with and without
`branch`), `POST /git/refs`, `GET /git/refs/heads/{branch}` or
`GET /branches/{branch}`, `GET/POST /issues`, `GET/POST /pulls`,
`GET /pulls/{number}`.

---

## 7. State export / determinism (what the FORGE must guarantee)

The export itself is written by the env, not the forge — but the forge's
behaviour determines whether two runs produce byte-identical exports:

- The env writes `<state>/export/state.json` (version 1):
  `{version: 1, working: 0|1, checks: {<group>.<slug>: 0|1, ...120 ids},
  counts: {total: 120, passed: N, byGroup: {git: {total, passed}, repos: …,
  issues: …, pulls: …, graphql: …, mcp: …, protocol: …}},
  perfTiers: {push: 0, clone: 0, read: 0, write: 0}, notes?: [...]}` —
  canonical JSON (keys sorted at every depth, one trailing newline).
  `working` = 1 only when boot succeeded AND ≥1 check passed.
  **No time, port, date or random id may influence any check bit.**
- `perf.json` (version 1) holds the timings only (see §8).
- **Replay rule (competition.yaml):** every contender trial runs twice
  (live, then replayed). The replay must leave the SAME `state.json` byte
  for byte; an entry whose two exports differ is listed and unranked.
  Consequences for the forge:
  - No `Date.now()` / wall-clock / random values in any response that a
    check bit depends on (timestamps in `merged_at`, commit author lines,
    etc. must be deterministic OR not asserted for exact value — `merged_at`
    is only asserted non-empty, so a real timestamp there is safe; but
    anything the suite compares for equality across runs — ids, shas,
    orderings — must be deterministic functions of the request data).
  - No port numbers, no process ids, no random ids in any response.
  - `tools/list` byte-identical across calls (§6.3); global ids stable
    across reads (§5, §6.4); `default_branch` stable across reads (§3.2).
  - The forge must be stateless across trials in the sense that a fresh
    process starts with an EMPTY store (the suite creates everything it
    needs; leftover state from a previous trial would break the replay
    comparison and the pagination counts).
- Boot failure export: every check 0, `working: 0`, note
  `"no entry registered before the boot deadline"` — happens only if the
  forge never registers / never answers `/healthz` twice.

---

## 8. Performance workloads (bench.ts / manifest.ts)

Each workload: **3 timed rounds, median round = the workload's time**,
after **5 untimed warm-up operations**, payloads built before the clock
starts. Each workload has a **60,000 ms budget** (phase budget 240 s total);
a workload that cannot finish in budget scores **0** and stops early.
Failed/cut workload: `p50_ms = p99_ms = 1,000,000,000` (sentinel, never 0),
`points = 0`. Setup (repo creation, history, the read issue) happens inside
the workload's budget but outside every timed round. A workload's setup
failure = 0 points. All four repos are recreated per workload (DELETE then
create; 404 on the delete is normal on a fresh entry). Owner is always the
scenario viewer login. Bench repos: `bench-push`, `bench-clone`,
`bench-read`, `bench-write`.

| Workload | Fixed work per round | Ops/round |
|---|---|---|
| `push` | 200 sequential pushes of a 1 KiB change to one repo: each op = one receive-pack exchange (advertisement + command list + pack) creating one new commit on `refs/heads/main`. File `bench-data.txt`, each commit replaces it with a fresh 1 KiB blob; pack carries only the 3 new objects (blob, tree, commit); first op creates the branch (`oldOid` zero), later ops update from the previous commit. A refused update fails the workload. | 200 |
| `clone` | 50 concurrent shallow clones (depth 1) of a 200-commit repo. History created once in setup: one push carrying all 200 commits, each replacing `history.txt` with a 512-byte blob. Each clone verifies its pack's checksum but does not parse objects. | 50 |
| `read` | 2,000 sequential `GET /api/v3/repos/{owner}/{repo}/issues/{number}` on ONE warm keep-alive connection (same RestClient / undici pool). Setup creates the issue first. | 2,000 |
| `write` | 200 sequential `POST /api/v3/repos/{owner}/{repo}/issues` (each a new issue; title/body from the seeded scenario). | 200 |

Scoring formula (per workload, `t` = median round in ms, `T_ref` = §8.1):

```
points = round(clamp(25 - 12.5 * log2(t / T_ref), 0, 50))
```

At `t = T_ref` → 25; at `t = T_ref/2` → 38 (37.5 rounds to 38); at
`t = 2*T_ref` → 13 (12.5 rounds to 13); at `t ≤ T_ref/4` → 50; at
`t ≥ 4*T_ref` → 0. `T_ref = 0` (uncalibrated) → 0 points. Perf column =
sum of the four workloads' points (0–200).
Tie-break `perf_p50_us` = `round(1000 * geometric_mean(p50_ms of the
workloads that measured))` in microseconds (failed workloads excluded from
the mean; if none measured, `1,000,000,000`). Lower is faster.

### 8.1 T_ref medians — EXACT values (calibration.json)

Source: `program/env/src/calibration.json` (schema
`agent-fun/calibration/v1`, measured 2026-11-14 on the production cluster
dsh-makefun, median of 3 runs of the house reference forge; identical file
in `sdk/conformance/calibration.json`). **These are the T_refs; do not use
the superseded 2026-11-13 rig values** (`push 514.838, clone 121.436, read
899.962, write 120.093` — explicitly "history only: none of these figures
is a T_ref any more").

| Workload | T_ref (p50_ms) | Runs' p50_ms |
|---|---|---|
| `push` | **1640.82** | [1914.548, 1640.82, 1415.785] |
| `clone` | **266.941** | [266.941, 313.634, 259.436] |
| `read` | **2759.715** | [2874.092, 2759.715, 2428.118] |
| `write` | **348.965** | [448.01, 348.965, 289.433] |

At its own T_ref each workload scores `round(25 - 12.5*log2(1)) = 25`, so
the reference forge earns 100/200 on perf at median times. Reference
conformance in calibration: 120/120 passed (git 30/30, repos 15/15, issues
15/15, pulls 20/20, graphql 20/20, mcp 15/15, protocol 5/5), `working: 1`.

Performance notes for the builder: the hot paths are push (receive-pack
unpack + ref update), shallow clone (pack generation for depth 1),
single-issue GET (read), and issue creation (write). Keep per-request work
O(1)-ish: avoid re-hashing the whole object store per request, avoid
re-serialising whole repos per list call, keep the git object store in
memory with an index, and generate packs without deltas (plain objects are
accepted and are the fastest correct choice).

---

## 9. Scoring columns

`scores.json` (written by the sealed scorer, exactly these keys, one integer
each — no extra keys):

| Column | Range | Formula |
|---|---|---|
| `score` | 0–1000 | `feature + agent + perf` |
| `feature` | 0–600 | `round(600 * passed / total)`, total = 120 checks |
| `agent` | 0–200 | `round(200 * passedMcp / totalMcp)`, totalMcp = 15 (the mcp group, counted BOTH in feature's 120 and alone as agent) |
| `perf` | 0–200 | `perf.json points_total` (sum of the four workload points, §8) |
| `perf_p50_us` | µs | `perf.json perf_p50_us` — tie-break, ascending (lower ranks first), then earlier receipt |
| `working` | 0\|1 | 1 when the export says the store works AND ≥1 check passed, else 0 |

A missing/malformed export scores 0 on every column (trial stands as 0, not
void). `MAX_SCORE = 1000`.

**House reference forge rule (competition.yaml):** the reference forge is
entered as a house entry 4 hours before the deadline (2026-11-21T02:30:00Z),
flagged baseline. It always goes to the final whatever it scores, takes
none of the 60 contender seats, holds the place it finishes in and never
takes that place's prize (a paid place it holds is not awarded; no entry
moves up). **A prize needs a score STRICTLY above the reference forge's:**
an entry beats it only when its score on the final's 3 instances is strictly
higher AND the 90% paired t-interval of its per-instance lead over the
reference's is above 0; an interval reaching 0 is a statistical tie and does
not beat it. An entry at or below the reference keeps its rank and takes no
place. Prizes: $500 total, 20 places — 1st $100, 2nd $70, 3rd $50, 4th–10th
$20 each, 11th–20th $14 each, paid in USDC on Solana, one place per entrant.
Deadline 2026-11-21T06:30:00Z; opens 2026-11-14T06:30:00Z; max 5 submissions/day,
10 total; last submission before the deadline is the final entry.

---

## 10. Ambiguities / things NOT in the kit (do not guess — decided as follows)

1. **Wrong token:** the suite never sends a wrong token, only the correct
   token or none. This spec says 401 for any non-matching token (§2).
2. **Default `per_page`:** never asserted without an explicit `per_page`
   (pagination uses 10; issue/PR lists use 100). Any default is fine.
3. **List ordering** (repos list, commits list without `?sha=`, branches):
   not asserted except where stated (issues sort, GraphQL order). Any stable
   order is fine.
4. **`GET /api/v3/user` fields:** only `login` is read. Return at least that.
5. **Repo `id` / `databaseId` values:** only stability/equality is asserted
   (REST re-reads, MCP rest-parity, GraphQL `databaseId` equality with its
   own earlier read). Any stable per-repo integer works; repo `id` must be a
   JSON value that stringifies stably. GraphQL `id` must NOT equal the plain
   `databaseId` string.
6. **Issue/PR `id` (REST):** only asserted present for comments (`id` int
   ≥1) and stable across two REST reads in MCP rest-parity (repo `id` and
   issue `id` must not change between reads). Any stable integer works.
7. **`merged_at` format:** only non-empty string asserted. Any ISO string is fine.
8. **`installed_version` value:** any non-empty string.
9. **`X-GitHub-Request-Id` value:** any non-empty string (uniqueness not checked).
10. **Timestamps in commit objects:** the suite BUILDS the commit bytes it
    pushes (fixed seeded timestamps) and the forge must store them verbatim.
    For commits the forge creates itself (contents PUTs, squash merges), the
    timestamp is never asserted for an exact value — but use a FIXED
    deterministic timestamp (e.g. the scenario epoch) rather than the wall
    clock, to keep replays byte-identical.
11. **`viewerPermission`:** any of ADMIN/MAINTAIN/WRITE/TRIAGE/READ passes.
12. **GraphQL `SearchType` enumValues beyond ISSUE:** only ISSUE is asserted.
13. **MCP `serverInfo` / discover extra fields:** free-form beyond
    name/version/capabilities.tools/supportedVersions.
14. **Push of a ref that does not exist with a non-zero old oid, or an
    update whose old oid ≠ current tip:** the suite only tests the
    descendant (non-fast-forward) case and the create/delete cases. Treat
    any old-oid mismatch as a refusal (`ng <ref> non-fast-forward` is the
    only reason string the suite asserts; use it for old-mismatch too).
15. **Duplicate repo create via REST:** 422 accepted by the pagination check
    and the git client (when the body says "already exists"). Other callers
    never send a duplicate create and assert on it.
16. **Duplicate open PR from the same head:** GraphQL fixture tolerates 422
    and re-reads the list. Return 422.
17. **Rate limiting:** none required; no `X-RateLimit-*` headers required,
    no 429 behaviour is checked.
18. **Practice trials:** `practice/trial-{0,1,2}/input/prompt.txt` are
    identical task text; `reference/scenario.json` only carries
    `{schema: "github-core-clone-01/scenario/v1", competition,
    scenario_seed: "6a09e667f3bcc909", purpose: "practice", k: 0, t, seed}`
    (per-trial drand seeds `371c9c0e…`, `00ac170e…`, `3abbf036…`). The
    scenario seed is constant across trials — the per-trial seed does not
    change the wire contract in this kit.
19. **What the kit does NOT contain:** no reference forge source (only its
    calibration medians), no `GET /api/v3/user/repos` default ordering, no
    webhook/events/notifications surface, no search endpoint (GraphQL
    `SearchType` is introspection-only).

## 11. Check inventory (for the builder's checklist)

- git (30): advertise-v2, advertise-receive-pack, advertise-headers,
  unknown-repo-404, ls-refs, ls-refs-prefix, clone-empty, push-first-commit,
  clone-object-hashes, clone-blob-bytes, clone-tree-entries,
  clone-commit-bytes, clone-annotated-tag, fetch-incremental,
  fetch-nothing-new, fetch-shallow-depth1, fetch-shallow-deepen,
  fetch-shallow-new-tip, push-update-ref, push-create-branch,
  push-delete-branch, push-non-fast-forward, push-mixed-report,
  push-delete-tag, push-100-files, pack-integrity, pack-deltas,
  sideband-framing, push-sideband-report, repo-survives-errors.
- repos (15): pagination, root, meta, create, not-found, patch, delete,
  branches, commits, unauthorized, foreign-token, contents-create,
  contents-directory, contents-stale-sha, contents-delete.
- issues (15): create, create-title-body, create-labels, numbers-increment,
  list-open, list-open-excludes-closed, list-sort-asc, list-sort-desc,
  list-shape, get-one, patch-close, patch-close-persists, comment-create,
  comments-list, bad-label.
- pulls (20): create, create-body, create-head-sha, create-base-sha,
  get-one, files-list, files-shape, files-multiple, merge-status,
  merge-body, merge-closes, merge-commit-on-default,
  merge-content-on-default, merge-commit-sha, merge-unknown,
  merge-conflict, list-state-all, list-default-open, list-shape,
  get-unknown.
- graphql (20): viewer-login, repository-fields, introspection-issue,
  introspection-types, issues-open-order, issue-by-number, create-issue,
  close-issue, pull-requests-list, pull-request-files, create-pull-request,
  merge-pull-request, add-comment, invalid-field, unauthenticated,
  node-repository, unknown-repository, batched-operations,
  global-id-stability, issues-pagination.
- mcp (15): initialize, server-discover, tools-list, tool-schemas,
  create-issue, list-issues, create-pull-request, merge-pull-request,
  unknown-tool, parse-error, missing-arguments, concurrent-calls,
  tools-list-idempotent, rest-parity, auth-required.
- protocol (5): content-type-json, unknown-route-json-404,
  malformed-body-400, request-id, error-message.
