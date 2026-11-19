"""Model Context Protocol endpoint for the forge (POST /mcp).

JSON-RPC 2.0 over Streamable HTTP, answered as ``application/json``
(the transport also allows SSE; plain JSON is simpler and allowed).
Standard library only, stateless per request: every handler reads only
the request and the shared store, so two concurrent tools/call requests
keep their own JSON-RPC ids and their own arguments with no shared
mutable call state.

Wiring (see forge/server.py): the server enforces Bearer auth (401)
before calling here when a token is configured, then calls
``handle_mcp(store, body, headers, login)`` and answers with the
returned ``(status, extra_headers, body_bytes)``. This module enforces
auth itself too, so a direct caller gets the same 401.

Protocol eras (mcp-client.ts / SPEC.md section 6):
  * Modern (preferred), revision 2026-07-28: no handshake, no
    session. ``server/discover`` answers a result whose
    ``supportedVersions`` is a non-empty list of YYYY-MM-DD revisions;
    every request declares its revision in ``params._meta`` and in the
    ``MCP-Protocol-Version`` header (both are accepted and ignored
    beyond that -- no version is refused, so the client's
    UnsupportedProtocolVersionError retry path never triggers).
  * Legacy (2025-11-25 and earlier): ``initialize`` echoes a revision
    the client offered, then ``notifications/initialized`` (a
    notification: no id, answered 202 with an empty body).
    ``server/discover`` is implemented, so the client negotiates modern
    first and never falls back -- but initialize is implemented anyway
    for a client that does.

Tool contract (checks-mcp.ts / SPEC.md section 6.4): the six tools are
listed under their canonical github-mcp-server names (a server prefix
is allowed by the suite, matched as a suffix case-insensitively, so
plain names are the simplest correct choice). ``tools/list`` is built
once at import time and serialized deterministically (``sort_keys``),
so two calls answer byte for byte identically. ``tools/call``
dispatches by suffix, case-insensitively, and normalizes arguments:
the suite maps its documented names onto the schema's nearest
property and fills a ``full_name`` property with ``owner/repo``
(checks-mcp.ts ``mapArguments``), so handlers accept both the
documented names AND ``full_name`` (split on ``/``) AND property-name
variants (``pull_number`` vs ``pullNumber`` vs ``number``, ``head``
vs ``head_ref`` vs ``head_branch``, ...). ``required`` is kept to
exactly the documented arguments (or fewer) -- a required property
the suite cannot fill fails its check before the call is even made.

All six tools act on the SAME store object REST and GraphQL use
(forge/store.py): a repo/issue created over MCP reads back over REST
as the same object, ids unchanged. Tool results are returned both as
a JSON text block and as ``structuredContent`` (the suite reads
structured content first, else parses the text).

Errors: unknown tool -> JSON-RPC -32602 (never HTTP 500); tools/call
missing a required argument -> a tool result with ``isError: true``;
a body that is not JSON -> HTTP 400 with JSON-RPC -32700; no
Authorization header -> HTTP 401 (decided before any tool lookup).
"""

import json

try:
    from .graphql import issue_shape, pull_shape, repo_shape
except ImportError:  # loaded as a top-level module, not `forge.*`
    from graphql import issue_shape, pull_shape, repo_shape

MCP_SERVER_NAME = "github-core-clone-forge"
MCP_SERVER_VERSION = "1.0.0"

SUPPORTED_PROTOCOL_VERSIONS = ["2026-07-28", "2025-11-25", "2025-06-18",
                               "2025-03-26", "2024-11-05"]
LEGACY_PROTOCOL_VERSIONS = ["2025-11-25", "2025-06-18", "2025-03-26",
                            "2024-11-05"]

# ---------------------------------------------------------------------------
# Tool definitions. Built once; serialized deterministically (sort_keys)
# so two tools/list calls answer byte for byte identically.
# ---------------------------------------------------------------------------

TOOL_DEFINITIONS = [
    {
        "name": "create_repo",
        "description": "Create a new repository owned by the authenticated user.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string",
                         "description": "Name of the repository to create."},
                "description": {"type": "string",
                                "description": "Short description of the repository."},
                "private": {"type": "boolean",
                            "description": "Whether the repository is private."},
                "owner": {"type": "string",
                          "description": "Owner login; defaults to the authenticated user."},
            },
            "required": ["name"],
        },
    },
    {
        "name": "get_repo",
        "description": "Get a repository by owner and name.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "owner": {"type": "string",
                          "description": "Login of the repository owner."},
                "repo": {"type": "string",
                         "description": "Name of the repository."},
                "full_name": {"type": "string",
                              "description": "Full name of the repository, owner/repo."},
            },
            "required": ["owner", "repo"],
        },
    },
    {
        "name": "create_issue",
        "description": "Create an issue in a repository.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "owner": {"type": "string",
                          "description": "Login of the repository owner."},
                "repo": {"type": "string",
                         "description": "Name of the repository."},
                "full_name": {"type": "string",
                              "description": "Full name of the repository, owner/repo."},
                "title": {"type": "string",
                          "description": "Title of the issue."},
                "body": {"type": "string",
                         "description": "Body text of the issue."},
                "labels": {"type": "array",
                           "items": {"type": "string"},
                           "description": "Label names to attach (must already exist)."},
            },
            "required": ["owner", "repo", "title"],
        },
    },
    {
        "name": "list_issues",
        "description": "List issues in a repository, optionally filtered by state.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "owner": {"type": "string",
                          "description": "Login of the repository owner."},
                "repo": {"type": "string",
                         "description": "Name of the repository."},
                "full_name": {"type": "string",
                              "description": "Full name of the repository, owner/repo."},
                "state": {"type": "string",
                          "enum": ["open", "closed", "all"],
                          "description": "Issue state to list: open, closed or all."},
            },
            "required": ["owner", "repo"],
        },
    },
    {
        "name": "create_pull_request",
        "description": "Open a pull request from a head branch into a base branch.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "owner": {"type": "string",
                          "description": "Login of the repository owner."},
                "repo": {"type": "string",
                         "description": "Name of the repository."},
                "full_name": {"type": "string",
                              "description": "Full name of the repository, owner/repo."},
                "title": {"type": "string",
                          "description": "Title of the pull request."},
                "body": {"type": "string",
                         "description": "Body text of the pull request."},
                "head": {"type": "string",
                         "description": "Name of the branch with the changes."},
                "base": {"type": "string",
                         "description": "Name of the branch to merge into."},
            },
            "required": ["owner", "repo", "title", "head", "base"],
        },
    },
    {
        "name": "merge_pull_request",
        "description": "Squash-merge a pull request.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "owner": {"type": "string",
                          "description": "Login of the repository owner."},
                "repo": {"type": "string",
                         "description": "Name of the repository."},
                "full_name": {"type": "string",
                              "description": "Full name of the repository, owner/repo."},
                "pull_number": {"type": "integer",
                                "description": "Number of the pull request to merge."},
                "merge_method": {"type": "string",
                                 "description": "Merge method; squash is used."},
            },
            "required": ["owner", "repo", "pull_number"],
        },
    },
]

_TOOL_INDEX = {tool["name"]: tool for tool in TOOL_DEFINITIONS}

# Canonical argument keys per tool, with the synonym spellings the suite
# may send (checks-mcp.ts ARGUMENT_SYNONYMS, plus camelCase variants).
_ARGUMENT_SYNONYMS = {
    "owner": ["owner", "owner_login", "org", "organization", "organisation",
              "user", "user_login", "login", "account", "namespace",
              "owner_name"],
    "repo": ["repo", "repository", "repository_name", "repo_name",
             "repo_slug", "project", "project_name"],
    "full_name": ["full_name", "repo_full_name", "repository_full_name",
                  "fullName", "fullname"],
    "name": ["name", "repo", "repository", "repository_name", "repo_name"],
    "title": ["title", "issue_title", "pr_title", "pull_request_title",
              "subject"],
    "body": ["body", "issue_body", "description", "content", "text",
             "comment"],
    "description": ["description", "about", "summary", "body"],
    "private": ["private", "is_private"],
    "state": ["state", "status"],
    "head": ["head", "head_ref", "head_branch", "pull_request_head",
             "pr_head", "source", "source_ref", "source_branch", "from",
             "from_branch", "branch"],
    "base": ["base", "base_ref", "base_branch", "pull_request_base",
             "pr_base", "target", "target_ref", "target_branch", "into",
             "into_branch"],
    "pull_number": ["pull_number", "pull_request_number", "pr_number",
                    "number", "issue_number", "index", "pullNumber",
                    "pullRequestNumber", "prNumber"],
    "merge_method": ["merge_method", "mergeMethod", "method"],
    "labels": ["labels", "label"],
}

_REQUIRED_ARGUMENTS = {
    "create_repo": ["name"],
    "get_repo": ["owner", "repo"],
    "create_issue": ["owner", "repo", "title"],
    "list_issues": ["owner", "repo"],
    "create_pull_request": ["owner", "repo", "title", "head", "base"],
    "merge_pull_request": ["owner", "repo", "pull_number"],
}


def _normalize_tool_name(name):
    """Lowercase, non-alphanumerics removed (checks-mcp.ts normalizeName)."""
    return "".join(ch for ch in str(name).lower() if ch.isalnum())


def _normalize_arguments(arguments):
    """Map any accepted spelling onto canonical keys.

    ``full_name`` (``"owner/repo"``) fills owner/repo when they are absent.
    ``pull_number`` accepts an integer or a digit string.
    """
    if not isinstance(arguments, dict):
        return {}
    by_norm = {}
    for key, value in arguments.items():
        by_norm[_normalize_tool_name(key)] = value
    out = {}
    for canonical, synonyms in _ARGUMENT_SYNONYMS.items():
        for synonym in synonyms:
            norm = _normalize_tool_name(synonym)
            if norm in by_norm and by_norm[norm] is not None:
                out[canonical] = by_norm[norm]
                break
    if ("owner" not in out or "repo" not in out) and "full_name" in out:
        full = str(out["full_name"])
        if "/" in full:
            owner, repo = full.split("/", 1)
            out.setdefault("owner", owner)
            out.setdefault("repo", repo)
    if "pull_number" in out:
        value = out["pull_number"]
        if isinstance(value, str) and value.isdigit():
            out["pull_number"] = int(value)
    return out


class _ToolFailure(Exception):
    """A tool execution failure: answered as an isError tool result."""


def _lookup_repo(store, args, login):
    owner = args.get("owner") or login
    repo_name = args.get("repo")
    if not owner or not repo_name:
        raise _ToolFailure("a repository owner and name are required")
    repo = store.get_repo(owner, repo_name)
    if repo is None:
        raise _ToolFailure("Repository not found: %s/%s" % (owner, repo_name))
    return repo


def _create_repo_tool(store, args, login):
    owner = args.get("owner") or login
    name = args.get("name")
    try:
        repo = store.create_repo(
            owner, name,
            description=args.get("description") or "",
            private=bool(args.get("private", False)))
    except Exception as exc:
        raise _ToolFailure(str(exc))
    return repo_shape(repo)


def _get_repo_tool(store, args, login):
    return repo_shape(_lookup_repo(store, args, login))


def _create_issue_tool(store, args, login):
    repo = _lookup_repo(store, args, login)
    labels = args.get("labels")
    if isinstance(labels, str):
        labels = [labels]
    try:
        issue = repo.create_issue(args.get("title"),
                                  body=args.get("body") or "",
                                  labels=labels)
    except Exception as exc:
        raise _ToolFailure(str(exc))
    return issue_shape(repo, issue)


def _list_issues_tool(store, args, login):
    repo = _lookup_repo(store, args, login)
    state = str(args.get("state") or "open").lower()
    if state == "all":
        state = None
    elif state not in ("open", "closed"):
        raise _ToolFailure("unknown issue state: %s" % state)
    return [issue_shape(repo, issue)
            for issue in repo.list_issues(state=state)]


def _create_pull_tool(store, args, login):
    repo = _lookup_repo(store, args, login)
    try:
        pull = repo.create_pull(args.get("title"), args.get("head"),
                                args.get("base"),
                                body=args.get("body") or "")
    except Exception as exc:
        raise _ToolFailure(str(exc))
    return pull_shape(repo, pull)


def _merge_pull_tool(store, args, login):
    repo = _lookup_repo(store, args, login)
    number = args.get("pull_number")
    if not isinstance(number, int):
        raise _ToolFailure("pull_number must be an integer")
    try:
        repo.merge_pull(number)
    except Exception as exc:
        raise _ToolFailure(str(exc))
    pull = repo.get_pull(number)
    if pull is None:
        raise _ToolFailure("Pull request not found: #%d" % number)
    return pull_shape(repo, pull)


_TOOL_DISPATCH = {
    "create_repo": _create_repo_tool,
    "get_repo": _get_repo_tool,
    "create_issue": _create_issue_tool,
    "list_issues": _list_issues_tool,
    "create_pull_request": _create_pull_tool,
    "merge_pull_request": _merge_pull_tool,
}


def _locate_tool(name):
    """The tool for a requested name: exact match first, then a name whose
    normalized form ends with the canonical name (checks-mcp.ts findTool:
    a server prefix such as ``github__create_issue`` is allowed)."""
    if not isinstance(name, str) or not name:
        return None
    if name in _TOOL_INDEX:
        return _TOOL_INDEX[name]
    wanted = _normalize_tool_name(name)
    for canonical, tool in _TOOL_INDEX.items():
        norm = _normalize_tool_name(canonical)
        if wanted == norm:
            return tool
    for canonical, tool in _TOOL_INDEX.items():
        norm = _normalize_tool_name(canonical)
        if len(wanted) > len(norm) and wanted.endswith(norm):
            return tool
    return None


# ---------------------------------------------------------------------------
# JSON-RPC envelope helpers.
# ---------------------------------------------------------------------------

def _dump_json(obj):
    return json.dumps(obj, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _success_envelope(req_id, result):
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _failure_envelope(req_id, code, message):
    return {"jsonrpc": "2.0", "id": req_id,
            "error": {"code": code, "message": message}}


def _tool_response(payload, is_error=False):
    text = json.dumps(payload, sort_keys=True)
    return {"content": [{"type": "text", "text": text}],
            "structuredContent": payload,
            "isError": bool(is_error)}


def _invoke_tool(store, params, login):
    """tools/call params -> (result-dict | error-dict, is_jsonrpc_error).

    Returns (envelope-payload, True) for a JSON-RPC error, else the tool
    result object to wrap in a success envelope.
    """
    if not isinstance(params, dict):
        return _failure_envelope(None, -32602, "tools/call needs params"), True
    name = params.get("name")
    tool = _locate_tool(name)
    if tool is None:
        return ({"code": -32602,
                 "message": "Unknown tool: %s" % (name,)}, True)
    canonical = tool["name"]
    args = _normalize_arguments(params.get("arguments") or {})
    missing = [key for key in _REQUIRED_ARGUMENTS.get(canonical, [])
               if key not in args or args[key] is None
               or (isinstance(args[key], str) and args[key] == "")]
    if missing:
        return _tool_response(
            {"error": "missing required argument(s): %s" % ", ".join(missing)},
            is_error=True), False
    try:
        payload = _TOOL_DISPATCH[canonical](store, args, login)
    except _ToolFailure as exc:
        return _tool_response({"error": str(exc)}, is_error=True), False
    except Exception as exc:  # never HTTP 500 for a tool failure
        return _tool_response(
            {"error": "%s: %s" % (type(exc).__name__, exc)}, is_error=True), False
    return _tool_response(payload, is_error=False), False


def _handle_request(store, request, login):
    """One JSON-RPC request object -> response object, or None for a
    notification (no id: nothing is answered)."""
    if not isinstance(request, dict):
        return _failure_envelope(None, -32600,
                                 "Invalid Request: expected a JSON-RPC object")
    has_id = "id" in request
    req_id = request.get("id")
    method = request.get("method")
    if not has_id:
        # Notifications (notifications/initialized, ...) need no answer.
        return None
    if not isinstance(method, str) or not method:
        return _failure_envelope(req_id, -32600,
                                 "Invalid Request: a method name is required")
    params = request.get("params")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        return _failure_envelope(req_id, -32602,
                                 "Invalid params: params must be an object")

    if method == "initialize":
        offered = params.get("protocolVersion")
        if offered in SUPPORTED_PROTOCOL_VERSIONS or offered in LEGACY_PROTOCOL_VERSIONS:
            chosen = offered
        else:
            chosen = "2025-11-25"
        return _success_envelope(req_id, {
            "protocolVersion": chosen,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": MCP_SERVER_NAME, "version": MCP_SERVER_VERSION},
        })
    if method == "server/discover":
        info = {"name": MCP_SERVER_NAME, "version": MCP_SERVER_VERSION}
        return _success_envelope(req_id, {
            "supportedVersions": list(SUPPORTED_PROTOCOL_VERSIONS),
            "capabilities": {"tools": {}},
            "serverInfo": dict(info),
            "instructions": "GitHub-compatible forge: repositories, "
                            "issues, pull requests and git over MCP tools.",
            "_meta": {"io.modelcontextprotocol/serverInfo": dict(info)},
        })
    if method == "ping":
        return _success_envelope(req_id, {})
    if method == "tools/list":
        return _success_envelope(req_id, {"tools": TOOL_DEFINITIONS})
    if method == "tools/call":
        payload, is_rpc_error = _invoke_tool(store, params, login)
        if is_rpc_error:
            return _failure_envelope(req_id, payload["code"],
                                     payload["message"])
        return _success_envelope(req_id, payload)
    return _failure_envelope(req_id, -32601,
                             "Method not found: %s" % method)


def _header_value(headers, name):
    """Case-insensitive header read for a dict or an HTTPMessage."""
    if headers is None:
        return ""
    try:
        value = headers.get(name)
    except Exception:
        value = None
    if value:
        return value
    if isinstance(headers, dict):
        wanted = name.lower()
        for key, val in headers.items():
            if str(key).lower() == wanted:
                return val or ""
    return ""


def handle_mcp(store, body, headers, login):
    """Answer one POST /mcp request.

    ``body`` is the raw request body (bytes/str) or an already-parsed
    object; ``headers`` the request headers; ``login`` the token owner's
    login. Returns ``(status, extra_headers, body_bytes)``. Auth is
    decided before any tool lookup: no Authorization header -> 401.
    A body that is not JSON -> HTTP 400 with JSON-RPC -32700. Every other
    request answers 200 (202 with an empty body for notifications).
    """
    auth = _header_value(headers, "authorization") or ""
    if not (isinstance(auth, str) and auth.startswith("Bearer ")
            and auth[len("Bearer "):].strip()):
        return 401, {}, _dump_json(_failure_envelope(
            None, -32001, "Unauthorized"))
    if isinstance(body, (bytes, bytearray)):
        raw = bytes(body)
    elif isinstance(body, str):
        raw = body.encode("utf-8")
    elif isinstance(body, (dict, list)):
        payload = body
        raw = None
    else:
        raw = b""
    if raw is not None:
        if not raw.strip():
            return 400, {}, _dump_json(_failure_envelope(
                None, -32700, "Parse error: empty body"))
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return 400, {}, _dump_json(_failure_envelope(
                None, -32700, "Parse error: body is not valid JSON"))
    if isinstance(payload, list):
        responses = [_handle_request(store, item, login) for item in payload]
        responses = [r for r in responses if r is not None]
        return 200, {}, _dump_json(responses)
    if not isinstance(payload, dict):
        return 400, {}, _dump_json(_failure_envelope(
            None, -32600, "Invalid Request: expected a JSON-RPC object"))
    response = _handle_request(store, payload, login)
    if response is None:
        return 202, {}, b""
    return 200, {}, _dump_json(response)
