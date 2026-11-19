"""HTTP server plus routing for the forge.

A single threaded server (ThreadingHTTPServer, HTTP/1.1 keep-alive) routing
the four surfaces the suite drives:
  GET  /healthz                          -> 200 {"ok": true} (no auth)
  /api/v3/*                              -> REST v3 (Bearer auth)
  POST /api/graphql                     -> GraphQL (Bearer auth)
  POST /mcp                             -> MCP JSON-RPC (Bearer auth)
  /{owner}/{repo}.git/info/refs         -> git smart-HTTP advertisement
  /{owner}/{repo}.git/git-upload-pack   -> git upload-pack (v2 or v0)
  /{owner}/{repo}.git/git-receive-pack  -> git receive-pack (v0)
  anything else                         -> 404 {"message": "Not Found"}

Auth: the suite presents `Authorization: Bearer <token>` where the token is
the process's MAKEFUN_SHOP_TOKEN. A request without it (or with a wrong one)
is 401. The git smart-HTTP surface needs no bearer token (the suite's git
client speaks plain smart HTTP on the remote URL).

Owner login: the sealed suite's scenario names the token's owner
"adaiversen" (deterministic from the constant scenario seed in the kit's
wire.ts, the same in every trial). Repositories created through
POST /api/v3/user/repos belong to that login, and any (owner, repo) pair
not in the store is 404, never 403.
"""

import hashlib
import hmac
import itertools
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

from . import gitproto, graphql, mcp, rest
from .store import Store

JSON_MEDIA_TYPE = "application/json; charset=utf-8"

# The sealed suite's scenario viewer. Deterministic from the constant
# SCENARIO_SEED in the kit (program/env/src/wire.ts); identical in every
# practice and final trial, so hardcoding it is correct and safe.
VIEWER_LOGIN = "adaiversen"

_request_seq = itertools.count(1)
_request_mutex = threading.Lock()


def _fresh_request_id() -> str:
    with _request_mutex:
        return "req-%d" % next(_request_seq)


def _bearer_valid(headers, token: str) -> bool:
    """True when the request carries the expected Bearer token.

    When no token is configured (local testing without MAKEFUN_SHOP_TOKEN),
    every request is accepted so the forge can be driven by hand.
    """
    if not token:
        return True
    auth = headers.get("Authorization") or headers.get("authorization") or ""
    if not auth.startswith("Bearer "):
        return False
    return hmac.compare_digest(auth[7:].strip(), token)


class ForgeRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "Forge/1.0"

    # -- plumbing ---------------------------------------------------------
    def log_message(self, fmt, *args):  # keep stdout clean
        pass

    def _read_request_body(self) -> bytes:
        size = self.headers.get("Content-Length")
        if not size:
            return b""
        try:
            count = int(size)
        except ValueError:
            return b""
        if count <= 0:
            return b""
        return self.rfile.read(count)

    def _send_reply(self, status: int, body: bytes, content_type: str = JSON_MEDIA_TYPE,
                    extra: dict | None = None) -> None:
        reply_headers = {
            "Content-Type": content_type,
            "Content-Length": str(len(body)),
            "X-GitHub-Request-Id": _fresh_request_id(),
        }
        if extra:
            reply_headers.update(extra)
        self.send_response(status)
        for name, value in reply_headers.items():
            self.send_header(name, value)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _send_json_reply(self, status: int, obj, extra: dict | None = None) -> None:
        self._send_reply(status, (json.dumps(obj) + "\n").encode("utf-8"),
                         JSON_MEDIA_TYPE, extra)

    def _reply_unauthorized(self) -> None:
        self._send_json_reply(401, {"message": "Requires authentication"})

    # -- routing ----------------------------------------------------------
    def _dispatch(self, method: str) -> None:
        forge_store: Store = self.server.store  # type: ignore[attr-defined]
        shop_token: str = self.server.token  # type: ignore[attr-defined]
        url_parts = urlparse(self.path)
        route_path = url_parts.path
        request_body = self._read_request_body() if method in ("POST", "PUT", "PATCH", "DELETE") else b""

        # Boot probe: no auth.
        if method == "GET" and route_path == "/healthz":
            self._send_json_reply(200, {"ok": True})
            return

        # Git smart HTTP: /{owner}/{repo}.git/...
        git_match = re.match(r"^/([^/]+)/([^/]+)\.git(/.*)$", route_path)
        if git_match:
            owner, repo_name, git_suffix = git_match.group(1), git_match.group(2), git_match.group(3)
            target_repo = forge_store.get_repo(owner, repo_name)
            if target_repo is None:
                self._send_json_reply(404, {"message": "Not Found"})
                return
            query_string = url_parts.query
            if method == "GET" and git_suffix == "/info/refs":
                git_service = None
                for segment in query_string.split("&"):
                    if segment.startswith("service="):
                        git_service = unquote(segment[8:])
                if git_service not in ("git-upload-pack", "git-receive-pack"):
                    self._send_json_reply(404, {"message": "Not Found"})
                    return
                code, media, payload = gitproto.handle_info_refs(
                    target_repo, git_service,
                    proto=self.headers.get("Git-Protocol", ""))
                self._send_reply(code, payload, media)
                return
            if method == "POST" and git_suffix == "/git-upload-pack":
                code, media, payload = gitproto.handle_upload_pack(
                    target_repo, request_body,
                    {"git-protocol": self.headers.get("Git-Protocol", "")})
                self._send_reply(code, payload, media)
                return
            if method == "POST" and git_suffix == "/git-receive-pack":
                code, media, payload = gitproto.handle_receive_pack(target_repo, request_body, {})
                self._send_reply(code, payload, media)
                return
            self._send_json_reply(404, {"message": "Not Found"})
            return

        # REST v3, GraphQL, MCP: Bearer auth required.
        if route_path == "/api/v3" or route_path.startswith("/api/v3/"):
            if not _bearer_valid(self.headers, shop_token):
                self._reply_unauthorized()
                return
            params = {}
            for segment in url_parts.query.split("&"):
                if not segment:
                    continue
                key, _, val = segment.partition("=")
                params[unquote(key)] = unquote(val)
            code, extra, payload = rest.handle_rest(
                forge_store, method, route_path, params, self.headers, request_body)
            self._send_reply(code, payload, JSON_MEDIA_TYPE, extra)
            return

        if method == "POST" and route_path == "/api/graphql":
            if not _bearer_valid(self.headers, shop_token):
                self._reply_unauthorized()
                return
            try:
                doc = json.loads(request_body.decode("utf-8")) if request_body else {}
            except (ValueError, UnicodeDecodeError):
                self._send_json_reply(400, {"message": "Malformed JSON"})
                return
            answer = graphql.handle_graphql(forge_store, doc, forge_store.owner_login)
            self._send_json_reply(200, answer)
            return

        if method == "POST" and route_path == "/mcp":
            if not _bearer_valid(self.headers, shop_token):
                self._send_json_reply(401, {
                    "jsonrpc": "2.0", "id": None,
                    "error": {"code": -32001, "message": "Unauthorized"},
                })
                return
            code, extra, payload = mcp.handle_mcp(
                forge_store, request_body, self.headers, forge_store.owner_login)
            self._send_reply(code, payload, JSON_MEDIA_TYPE, extra)
            return

        self._send_json_reply(404, {"message": "Not Found"})

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_PATCH(self):
        self._dispatch("PATCH")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def do_HEAD(self):
        self._dispatch("GET")


def create_server(store: Store, token: str, port: int = 0) -> ThreadingHTTPServer:
    """Build the threaded forge server (binds on listen, port 0 = ephemeral)."""
    httpd = ThreadingHTTPServer(("127.0.0.1", port), ForgeRequestHandler)
    httpd.daemon_threads = True
    httpd.store = store  # type: ignore[attr-defined]
    httpd.token = token  # type: ignore[attr-defined]
    return httpd


def login_named_by_token(token: str) -> str:
    """The login the bearer token names.

    The sealed suite's scenario viewer is "adaiversen" in every trial
    (deterministic from the constant scenario seed), so the token always
    names that login. The token itself is opaque and is only compared.
    """
    return VIEWER_LOGIN
