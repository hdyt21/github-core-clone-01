"""GraphQL v4 executor for the forge (POST /api/graphql).

Standard library only. A compact, purpose-built executor for precisely
the documents the suite sends (checks-graphql.ts / SPEC.md section 5):
gh's viewer / repository / issue / pull-request reads, the __type
feature probes, the create/close/merge/addComment mutations, node(id:),
search, cursor pagination and operation batching. No general GraphQL
library is used and none is needed: the parser below handles operation
names, variables, inline enum lists, inline input objects, null
variables, aliases and inline fragments -- everything those documents
use.

Wiring (see forge/server.py): the server enforces Bearer auth (401)
and malformed-JSON (400) before calling here, then calls
``handle_graphql(store, payload, login)`` and answers HTTP 200 with
the returned JSON. A GraphQL-level failure (unknown field, bad
argument) is therefore a 200 whose body carries ``errors`` -- never
an HTTP error.

Store contract (forge/store.py, read 2026-11-18 -- do NOT re-derive
from the brief): ``Store.repos`` maps ``(owner, name) -> Repo``;
``Store.get_repo(owner, name)`` / ``Store.create_repo(owner, name,
description="", private=False)``; ``Repo`` carries ``.owner``,
``.name``, ``.database_id`` (int, the REST numeric id), ``.node_id``
(opaque base64, see below), ``.full_name``, ``.description``,
``.private``, ``.default_branch``, ``.refs``, ``.issues``,
``.pulls``; ``Repo.create_issue(title, body="", labels=None)``,
``Repo.get_issue(number)``, ``Repo.list_issues(state=None)`` (sorted
by number); ``Repo.create_pull(title, head, base, body="")``,
``Repo.get_pull(number)``, ``Repo.list_pulls(state=None)``,
``Repo.pull_files(pull)`` (``[{filename, status, ...}]``),
``Repo.merge_pull(number)`` (squash, returns ``(sha, message)``);
``Issue`` has ``.number``, ``.id`` (store-wide int), ``.node_id``,
``.title``, ``.body``, ``.state`` (``"open"``/``"closed"``),
``.labels`` (names), ``.comments`` (list of ``Comment``);
``Pull`` has ``.number``, ``.id``, ``.node_id``, ``.title``,
``.body``, ``.state``, ``.head``/``.base`` (branch names),
``.head_sha``/``.base_sha``, ``.merged``, ``.merge_commit_sha``,
``.mergeable`` (bool); ``Comment(repo, issue_number, body, user_login)``
has ``.id``, ``.body``, ``.node_id``. There is no store method to close
an issue or to add a comment: closing is ``issue.state = "closed"``
and commenting is appending a ``Comment`` to ``issue.comments`` -- the
same attributes the REST layer reads, so both are visible over REST
at once.

Global ids: the store's own ``.node_id`` properties are used verbatim
(``base64("Repository:<database_id>")`` etc.). They are opaque (never
the plain numeric id), stable across reads, and decodable back to the
object by splitting the decoded ``"<Type>:<id>"`` payload and scanning
the store -- that is how mutation inputs (``repositoryId``,
``issueId``, ``pullRequestId``, ``subjectId``) and ``node(id:)``
resolve.

IDs / choices that are guesses, with the kit text behind each:
  * ``viewerPermission`` is always ``"ADMIN"`` -- checks-graphql.ts
    accepts any of ADMIN/MAINTAIN/WRITE/TRIAGE/READ for the token
    owner.
  * ``defaultBranchRef`` always names ``repo.default_branch``, even
    on an empty repo -- the check asserts it only when branches exist.
  * ``issues`` with no ``orderBy`` defaults to CREATED_AT DESC
    (number DESC); ``pullRequests`` with no ``states`` defaults to
    OPEN only. Both match the only documents that exercise them.
  * Cursors are opaque base64 ``"cursor:<index>"`` over the ordered
    connection list; the format is free per SPEC.md section 5.2.
  * ``mergeable`` is ``"MERGEABLE"`` when ``pull.mergeable`` is
    truthy, else ``"CONFLICTING"`` -- the store tracks one boolean
    per PR.
  * ``search`` matches case-insensitively on title+body, ignoring
    ``key:value`` qualifiers (``repo:``, ``type:``, ...); only type
    ISSUE returns nodes. No suite document sends ``search`` today
    (only the SearchType introspection probe), so this is defensive.
"""

import base64
from types import SimpleNamespace

try:
    from .store import Comment as _StoreComment
except ImportError:  # loaded as a top-level module, not `forge.*`
    try:
        from store import Comment as _StoreComment
    except ImportError:
        _StoreComment = None

FIXED_TS = "2026-01-01T00:00:00Z"


# --------------------------------------------------------------------------
# Small shared serializers (also used by forge/mcp.py for tool results).
# --------------------------------------------------------------------------

def repo_shape(repo):
    """The REST-shaped repository object MCP tool results carry."""
    return {
        "id": repo.database_id,
        "node_id": repo.node_id,
        "name": repo.name,
        "full_name": repo.full_name,
        "owner": {"login": repo.owner},
        "private": bool(repo.private),
        "description": repo.description or "",
        "default_branch": repo.default_branch,
    }


def issue_shape(repo, issue):
    """The REST-shaped issue object MCP tool results carry."""
    return {
        "id": issue.id,
        "node_id": issue.node_id,
        "number": issue.number,
        "title": issue.title,
        "body": issue.body,
        "state": issue.state,
        "labels": [{"name": name} for name in issue.labels],
        "repository": {"full_name": repo.full_name},
    }


def pull_shape(repo, pull):
    """The REST-shaped pull-request object MCP tool results carry."""
    return {
        "id": pull.id,
        "node_id": pull.node_id,
        "number": pull.number,
        "title": pull.title,
        "body": pull.body,
        "state": pull.state,
        "merged": bool(pull.merged),
        "merge_commit_sha": pull.merge_commit_sha,
        "head": {"ref": pull.head, "sha": pull.head_sha},
        "base": {"ref": pull.base, "sha": pull.base_sha},
        "head_ref": pull.head,
        "base_ref": pull.base,
        "repository": {"full_name": repo.full_name},
    }


# --------------------------------------------------------------------------
# Schema: field names per type (validation + introspection share one table,
# so a field the introspection lists is always executable and vice versa).
# --------------------------------------------------------------------------

def _type_ref(kind, name=None, of=None):
    """An introspection type reference: {kind, name, ofType}."""
    return {"kind": kind, "name": name, "ofType": of}


def _non_null_ref(kind, name=None, of=None):
    return _type_ref("NON_NULL", None, _type_ref(kind, name, of))


_SCALAR_REF = lambda name: _type_ref("SCALAR", name)          # noqa: E731
_OBJECT_REF = lambda name: _type_ref("OBJECT", name)          # noqa: E731
_ENUM_REF = lambda name: _type_ref("ENUM", name)              # noqa: E731
_LIST_REF = lambda inner: _type_ref("LIST", None, inner)      # noqa: E731

# field name -> introspection type reference, per GraphQL type.
_SCHEMA_FIELDS = {
    "Query": {
        "viewer": _OBJECT_REF("User"),
        "repository": _OBJECT_REF("Repository"),
        "node": _type_ref("INTERFACE", "Node"),
        "search": _OBJECT_REF("SearchResult"),
        "__type": _OBJECT_REF("__Type"),
        "__schema": _OBJECT_REF("__Schema"),
    },
    "Mutation": {
        "createIssue": _OBJECT_REF("CreateIssuePayload"),
        "closeIssue": _OBJECT_REF("CloseIssuePayload"),
        "createPullRequest": _OBJECT_REF("CreatePullRequestPayload"),
        "mergePullRequest": _OBJECT_REF("MergePullRequestPayload"),
        "addComment": _OBJECT_REF("AddCommentPayload"),
    },
    "Repository": {
        "id": _non_null_ref("SCALAR", "ID"),
        "databaseId": _non_null_ref("SCALAR", "Int"),
        "name": _non_null_ref("SCALAR", "String"),
        "nameWithOwner": _non_null_ref("SCALAR", "String"),
        "owner": _OBJECT_REF("User"),
        "defaultBranchRef": _OBJECT_REF("Ref"),
        "viewerPermission": _ENUM_REF("RepositoryPermission"),
        "hasIssuesEnabled": _non_null_ref("SCALAR", "Boolean"),
        "issue": _OBJECT_REF("Issue"),
        "issues": _OBJECT_REF("IssueConnection"),
        "pullRequests": _OBJECT_REF("PullRequestConnection"),
        "pullRequest": _OBJECT_REF("PullRequest"),
        "description": _type_ref("SCALAR", "String"),
        "isPrivate": _non_null_ref("SCALAR", "Boolean"),
        "private": _non_null_ref("SCALAR", "Boolean"),
        "url": _type_ref("SCALAR", "URI"),
        "createdAt": _type_ref("SCALAR", "DateTime"),
        "updatedAt": _type_ref("SCALAR", "DateTime"),
        "fork": _type_ref("SCALAR", "Boolean"),
        "stargazerCount": _type_ref("SCALAR", "Int"),
    },
    "Issue": {
        "id": _non_null_ref("SCALAR", "ID"),
        "number": _non_null_ref("SCALAR", "Int"),
        "title": _non_null_ref("SCALAR", "String"),
        "state": _non_null_ref("ENUM", "IssueState"),
        "body": _type_ref("SCALAR", "String"),
        "labels": _OBJECT_REF("LabelConnection"),
        "author": _OBJECT_REF("User"),
        "createdAt": _type_ref("SCALAR", "DateTime"),
        "updatedAt": _type_ref("SCALAR", "DateTime"),
        "closedAt": _type_ref("SCALAR", "DateTime"),
        "url": _type_ref("SCALAR", "URI"),
        "comments": _OBJECT_REF("IssueCommentConnection"),
    },
    "PullRequest": {
        "id": _non_null_ref("SCALAR", "ID"),
        "number": _non_null_ref("SCALAR", "Int"),
        "title": _type_ref("SCALAR", "String"),
        "body": _type_ref("SCALAR", "String"),
        "state": _non_null_ref("ENUM", "PullRequestState"),
        "merged": _non_null_ref("SCALAR", "Boolean"),
        "headRefName": _non_null_ref("SCALAR", "String"),
        "baseRefName": _non_null_ref("SCALAR", "String"),
        "mergeable": _ENUM_REF("MergeableState"),
        "files": _OBJECT_REF("PullRequestChangedFileConnection"),
        "author": _OBJECT_REF("User"),
        "createdAt": _type_ref("SCALAR", "DateTime"),
        "updatedAt": _type_ref("SCALAR", "DateTime"),
        "mergedAt": _type_ref("SCALAR", "DateTime"),
        "url": _type_ref("SCALAR", "URI"),
    },
    "User": {
        "id": _non_null_ref("SCALAR", "ID"),
        "login": _non_null_ref("SCALAR", "String"),
        "name": _type_ref("SCALAR", "String"),
    },
    "Ref": {
        "name": _non_null_ref("SCALAR", "String"),
    },
    "IssueConnection": {
        "nodes": _LIST_REF(_OBJECT_REF("Issue")),
        "edges": _LIST_REF(_OBJECT_REF("IssueEdge")),
        "pageInfo": _non_null_ref("OBJECT", "PageInfo"),
        "totalCount": _type_ref("SCALAR", "Int"),
    },
    "PullRequestConnection": {
        "nodes": _LIST_REF(_OBJECT_REF("PullRequest")),
        "edges": _LIST_REF(_OBJECT_REF("PullRequestEdge")),
        "pageInfo": _non_null_ref("OBJECT", "PageInfo"),
        "totalCount": _type_ref("SCALAR", "Int"),
    },
    "IssueEdge": {
        "node": _OBJECT_REF("Issue"),
        "cursor": _non_null_ref("SCALAR", "String"),
    },
    "PullRequestEdge": {
        "node": _OBJECT_REF("PullRequest"),
        "cursor": _non_null_ref("SCALAR", "String"),
    },
    "PageInfo": {
        "hasNextPage": _non_null_ref("SCALAR", "Boolean"),
        "hasPreviousPage": _non_null_ref("SCALAR", "Boolean"),
        "startCursor": _type_ref("SCALAR", "String"),
        "endCursor": _type_ref("SCALAR", "String"),
    },
    "SearchResult": {
        "nodes": _LIST_REF(_type_ref("INTERFACE", "Node")),
        "edges": _LIST_REF(_OBJECT_REF("SearchEdge")),
        "pageInfo": _non_null_ref("OBJECT", "PageInfo"),
        "issueCount": _type_ref("SCALAR", "Int"),
    },
    "SearchEdge": {
        "node": _type_ref("INTERFACE", "Node"),
        "cursor": _non_null_ref("SCALAR", "String"),
    },
    "LabelConnection": {
        "nodes": _LIST_REF(_OBJECT_REF("Label")),
        "totalCount": _type_ref("SCALAR", "Int"),
        "pageInfo": _non_null_ref("OBJECT", "PageInfo"),
    },
    "IssueCommentConnection": {
        "nodes": _LIST_REF(_OBJECT_REF("Comment")),
        "totalCount": _type_ref("SCALAR", "Int"),
        "pageInfo": _non_null_ref("OBJECT", "PageInfo"),
    },
    "PullRequestChangedFileConnection": {
        "nodes": _LIST_REF(_OBJECT_REF("PullRequestChangedFile")),
        "totalCount": _type_ref("SCALAR", "Int"),
        "pageInfo": _non_null_ref("OBJECT", "PageInfo"),
    },
    "PullRequestChangedFile": {
        "path": _non_null_ref("SCALAR", "String"),
    },
    "Label": {
        "id": _non_null_ref("SCALAR", "ID"),
        "name": _non_null_ref("SCALAR", "String"),
        "color": _type_ref("SCALAR", "String"),
        "description": _type_ref("SCALAR", "String"),
    },
    "Comment": {
        "id": _non_null_ref("SCALAR", "ID"),
        "body": _non_null_ref("SCALAR", "String"),
        "author": _OBJECT_REF("User"),
    },
    "CommentEdge": {
        "node": _OBJECT_REF("Comment"),
        "cursor": _non_null_ref("SCALAR", "String"),
    },
    "CreateIssuePayload": {
        "issue": _OBJECT_REF("Issue"),
        "clientMutationId": _type_ref("SCALAR", "String"),
    },
    "CloseIssuePayload": {
        "issue": _OBJECT_REF("Issue"),
        "clientMutationId": _type_ref("SCALAR", "String"),
    },
    "CreatePullRequestPayload": {
        "pullRequest": _OBJECT_REF("PullRequest"),
        "clientMutationId": _type_ref("SCALAR", "String"),
    },
    "MergePullRequestPayload": {
        "pullRequest": _OBJECT_REF("PullRequest"),
        "clientMutationId": _type_ref("SCALAR", "String"),
    },
    "AddCommentPayload": {
        "commentEdge": _OBJECT_REF("CommentEdge"),
        "subject": _type_ref("INTERFACE", "Node"),
        "clientMutationId": _type_ref("SCALAR", "String"),
    },
    "__Type": {
        "name": _type_ref("SCALAR", "String"),
        "kind": _non_null_ref("ENUM", "__TypeKind"),
        "description": _type_ref("SCALAR", "String"),
        "fields": _LIST_REF(_OBJECT_REF("__Field")),
        "enumValues": _LIST_REF(_OBJECT_REF("__EnumValue")),
        "inputFields": _LIST_REF(_OBJECT_REF("__InputValue")),
        "interfaces": _LIST_REF(_OBJECT_REF("__Type")),
        "possibleTypes": _LIST_REF(_OBJECT_REF("__Type")),
        "ofType": _OBJECT_REF("__Type"),
    },
    "__Field": {
        "name": _non_null_ref("SCALAR", "String"),
        "description": _type_ref("SCALAR", "String"),
        "type": _non_null_ref("OBJECT", "__Type"),
    },
    "__EnumValue": {
        "name": _non_null_ref("SCALAR", "String"),
        "description": _type_ref("SCALAR", "String"),
    },
    "__InputValue": {
        "name": _non_null_ref("SCALAR", "String"),
        "description": _type_ref("SCALAR", "String"),
        "type": _non_null_ref("OBJECT", "__Type"),
    },
    "__Schema": {
        "queryType": _OBJECT_REF("__Type"),
        "mutationType": _OBJECT_REF("__Type"),
        "types": _LIST_REF(_OBJECT_REF("__Type")),
        "description": _type_ref("SCALAR", "String"),
    },
}

# type name -> kind, for __type(name:). Enum values where the kind is ENUM.
_ENUM_MEMBERS = {
    "IssueState": ["OPEN", "CLOSED"],
    "PullRequestState": ["OPEN", "CLOSED", "MERGED"],
    "MergeableState": ["MERGEABLE", "CONFLICTING", "UNKNOWN"],
    "RepositoryPermission": ["ADMIN", "MAINTAIN", "WRITE", "TRIAGE", "READ"],
    "SearchType": ["COMMIT", "DISCUSSION", "ISSUE", "REPOSITORY", "USER"],
    "IssueOrderField": ["CREATED_AT", "UPDATED_AT", "COMMENTS"],
    "OrderDirection": ["ASC", "DESC"],
    "PullRequestMergeMethod": ["MERGE", "SQUASH", "REBASE"],
    "__TypeKind": ["SCALAR", "OBJECT", "INTERFACE", "UNION", "ENUM",
                   "INPUT_OBJECT", "LIST", "NON_NULL"],
}

_TYPE_KIND_MAP = {}
for _tname in _SCHEMA_FIELDS:
    _TYPE_KIND_MAP[_tname] = "OBJECT"
for _tname in _ENUM_MEMBERS:
    _TYPE_KIND_MAP[_tname] = "ENUM"
_TYPE_KIND_MAP.update({
    "Node": "INTERFACE",
    "String": "SCALAR", "Int": "SCALAR", "Boolean": "SCALAR",
    "ID": "SCALAR", "DateTime": "SCALAR", "URI": "SCALAR",
})


def _introspect_type(name):
    """Introspection record for a type name, or None when unknown."""
    if name not in _TYPE_KIND_MAP:
        return None
    kind = _TYPE_KIND_MAP[name]
    fields = None
    if kind in ("OBJECT", "INTERFACE"):
        fields = [{"name": fname, "type": ftype}
                  for fname, ftype in _SCHEMA_FIELDS.get(name, {}).items()]
    enum_values = None
    if kind == "ENUM":
        enum_values = [{"name": value} for value in _ENUM_MEMBERS.get(name, [])]
    return {"name": name, "kind": kind, "fields": fields,
            "enumValues": enum_values}


# --------------------------------------------------------------------------
# Global ids: the store's node_id, decoded back to the object.
# --------------------------------------------------------------------------

def _encode_b64(text):
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def _decode_gid(gid):
    """("Repository"|"Issue"|"PullRequest"|"IssueComment"|"Label", payload)
    for a node_id the store issued, else None."""
    if not isinstance(gid, str) or not gid:
        return None
    try:
        raw = base64.b64decode(gid.encode("ascii")).decode("utf-8")
    except Exception:
        return None
    parts = raw.split(":")
    if len(parts) < 2 or parts[0] not in (
            "Repository", "Issue", "PullRequest", "IssueComment", "Label"):
        return None
    return parts[0], parts[1:]


def _repo_by_db_id(store, db_id):
    for repo in store.repos.values():
        if repo.database_id == db_id:
            return repo
    return None


def _repo_from_gid(store, gid):
    decoded = _decode_gid(gid)
    if decoded is None or decoded[0] != "Repository":
        return None
    try:
        return _repo_by_db_id(store, int(decoded[1][0]))
    except (ValueError, IndexError):
        return None


def _issue_from_gid(store, gid):
    """(repo, issue) for an Issue node id, else None."""
    decoded = _decode_gid(gid)
    if decoded is None or decoded[0] != "Issue":
        return None
    try:
        want = int(decoded[1][0])
    except (ValueError, IndexError):
        return None
    for repo in store.repos.values():
        for issue in repo.issues.values():
            if issue.id == want:
                return repo, issue
    return None


def _pull_from_gid(store, gid):
    """(repo, pull) for a PullRequest node id, else None."""
    decoded = _decode_gid(gid)
    if decoded is None or decoded[0] != "PullRequest":
        return None
    try:
        want = int(decoded[1][0])
    except (ValueError, IndexError):
        return None
    for repo in store.repos.values():
        for pull in repo.pulls.values():
            if pull.id == want:
                return repo, pull
    return None


def _node_from_gid(store, gid):
    """(typename, payload) for any node id: payload is a Repo, a
    (repo, issue) / (repo, pull) pair, or a Comment. None when unknown."""
    decoded = _decode_gid(gid)
    if decoded is None:
        return None
    kind, parts = decoded
    if kind == "Repository":
        repo = _repo_from_gid(store, gid)
        return ("Repository", repo) if repo is not None else None
    if kind == "Issue":
        found = _issue_from_gid(store, gid)
        return ("Issue", found) if found is not None else None
    if kind == "PullRequest":
        found = _pull_from_gid(store, gid)
        return ("PullRequest", found) if found is not None else None
    if kind == "IssueComment":
        try:
            repo_id, issue_number, comment_id = (int(parts[0]),
                                                 int(parts[1]),
                                                 int(parts[2]))
        except (ValueError, IndexError):
            return None
        repo = _repo_by_db_id(store, repo_id)
        if repo is None:
            return None
        issue = repo.get_issue(issue_number)
        if issue is None:
            return None
        for comment in issue.comments:
            if comment.id == comment_id:
                return ("Comment", comment)
        return None
    return None


def _comment_gid(repo, issue_number, comment_id):
    return _encode_b64("IssueComment:%d:%d:%d"
                       % (repo.database_id, issue_number, comment_id))


def _append_comment(repo, issue, body, login):
    """Append a comment the REST layer reads (issue.comments)."""
    if _StoreComment is not None:
        comment = _StoreComment(repo, issue.number, body, login)
    else:  # pragma: no cover - only when store.py cannot be imported
        comment = SimpleNamespace(
            id=repo.next_comment_id(), body=body, user_login=login,
            issue_number=issue.number, repo=repo)
        comment.node_id = _comment_gid(repo, issue.number, comment.id)
    issue.comments.append(comment)
    return comment


# --------------------------------------------------------------------------
# Cursors (opaque; format is free per SPEC.md 5.2).
# --------------------------------------------------------------------------

def _encode_cursor(index):
    raw = ("cursor:%d" % index).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(cursor):
    if not isinstance(cursor, str) or not cursor:
        return None
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
    except Exception:
        return None
    if not raw.startswith("cursor:"):
        return None
    try:
        return int(raw[len("cursor:"):])
    except ValueError:
        return None


# --------------------------------------------------------------------------
# Lexer + recursive-descent parser for the documents the suite sends.
# Handles: operation names, variable definitions, variables, inline enum
# lists, inline input objects, null/bool/int/string literals, aliases,
# directives (skipped), named + inline fragments.
# --------------------------------------------------------------------------

class _ParseFailure(Exception):
    pass


class _GraphQLFailure(Exception):
    """An error that becomes one entry of the response `errors` array."""


def _tokenize(text):
    """[(kind, value)] kinds: NAME STRING INT FLOAT VAR PUNCT ELLIPSIS EOF."""
    tokens = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in " \t\r\n,":
            i += 1
            continue
        if c == "#":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if text.startswith("...", i):
            tokens.append(("ELLIPSIS", "..."))
            i += 3
            continue
        if c in "{}()[]!:=@":
            tokens.append(("PUNCT", c))
            i += 1
            continue
        if c == "$":
            j = i + 1
            while j < n and (text[j].isalnum() or text[j] == "_"):
                j += 1
            if j == i + 1:
                raise _ParseFailure("stray $ at offset %d" % i)
            tokens.append(("VAR", text[i + 1:j]))
            i = j
            continue
        if c == '"':
            j = i + 1
            out = []
            while j < n and text[j] != '"':
                ch = text[j]
                if ch == "\\":
                    j += 1
                    if j >= n:
                        break
                    esc = text[j]
                    mapping = {"n": "\n", "t": "\t", "r": "\r",
                               "b": "\b", "f": "\f", '"': '"',
                               "\\": "\\", "/": "/"}
                    if esc == "u":
                        out.append(chr(int(text[j + 1:j + 5], 16)))
                        j += 4
                    else:
                        out.append(mapping.get(esc, esc))
                    j += 1
                    continue
                out.append(ch)
                j += 1
            if j >= n:
                raise _ParseFailure("unterminated string")
            tokens.append(("STRING", "".join(out)))
            i = j + 1
            continue
        if c.isdigit() or (c == "-" and i + 1 < n and text[i + 1].isdigit()):
            j = i
            if text[j] == "-":
                j += 1
            while j < n and text[j].isdigit():
                j += 1
            is_float = False
            if j < n and text[j] == ".":
                is_float = True
                j += 1
                while j < n and text[j].isdigit():
                    j += 1
            if j < n and text[j] in "eE":
                is_float = True
                j += 1
                if j < n and text[j] in "+-":
                    j += 1
                while j < n and text[j].isdigit():
                    j += 1
            raw = text[i:j]
            tokens.append(("FLOAT" if is_float else "INT",
                           float(raw) if is_float else int(raw)))
            i = j
            continue
        if c.isalpha() or c == "_":
            j = i
            while j < n and (text[j].isalnum() or text[j] == "_"):
                j += 1
            tokens.append(("NAME", text[i:j]))
            i = j
            continue
        raise _ParseFailure("unexpected character %r at offset %d" % (c, i))
    tokens.append(("EOF", None))
    return tokens


class _DocumentParser:
    def __init__(self, tokens):
        self.tokens = tokens
        self.pos = 0

    def peek(self):
        return self.tokens[self.pos]

    def next(self):
        tok = self.tokens[self.pos]
        self.pos += 1
        return tok

    def expect_punct(self, punct):
        kind, value = self.next()
        if kind != "PUNCT" or value != punct:
            raise _ParseFailure("expected %r, got %r" % (punct, value))
        return value

    def expect_name(self):
        kind, value = self.next()
        if kind != "NAME":
            raise _ParseFailure("expected a name, got %r" % (value,))
        return value

    def _skip_balanced(self, open_p, close_p):
        """Consume a balanced (...) / [...] / {...} span, opener already read."""
        depth = 1
        while depth > 0:
            kind, value = self.next()
            if kind == "EOF":
                raise _ParseFailure("unterminated %s" % open_p)
            if kind == "PUNCT" and value == open_p:
                depth += 1
            elif kind == "PUNCT" and value == close_p:
                depth -= 1

    def _skip_directives(self):
        while self.peek() == ("PUNCT", "@"):
            self.next()
            self.expect_name()
            if self.peek() == ("PUNCT", "("):
                self.next()
                self._skip_balanced("(", ")")

    def parse_document(self):
        operations, fragments = [], {}
        while self.peek()[0] != "EOF":
            kind, value = self.peek()
            if kind == "NAME" and value == "fragment":
                self.next()
                name = self.expect_name()
                on = self.expect_name()
                if on != "on":
                    raise _ParseFailure("expected 'on' in fragment definition")
                type_condition = self.expect_name()
                self._skip_directives()
                fragments[name] = {
                    "type_condition": type_condition,
                    "selections": self.parse_selection_set(),
                }
            else:
                operations.append(self.parse_operation())
        return operations, fragments

    def parse_operation(self):
        kind, value = self.peek()
        if kind == "PUNCT" and value == "{":
            return {"kind": "query", "name": None,
                    "selections": self.parse_selection_set()}
        op_kind = self.expect_name()
        if op_kind not in ("query", "mutation", "subscription"):
            raise _ParseFailure("expected query/mutation, got %r" % op_kind)
        name = None
        if self.peek()[0] == "NAME":
            name = self.next()[1]
        if self.peek() == ("PUNCT", "("):
            self.next()
            self._skip_balanced("(", ")")
        self._skip_directives()
        return {"kind": op_kind, "name": name,
                "selections": self.parse_selection_set()}

    def parse_selection_set(self):
        self.expect_punct("{")
        selections = []
        while self.peek() != ("PUNCT", "}"):
            if self.peek()[0] == "EOF":
                raise _ParseFailure("unterminated selection set")
            selections.append(self.parse_selection())
        self.expect_punct("}")
        return selections

    def parse_selection(self):
        if self.peek() == ("ELLIPSIS", "..."):
            self.next()
            kind, value = self.peek()
            if kind == "NAME" and value == "on":
                self.next()
                type_condition = self.expect_name()
                self._skip_directives()
                return {"kind": "inline_fragment",
                        "type_condition": type_condition,
                        "selections": self.parse_selection_set()}
            if kind == "PUNCT" and value == "{":
                return {"kind": "inline_fragment", "type_condition": None,
                        "selections": self.parse_selection_set()}
            name = self.expect_name()
            self._skip_directives()
            return {"kind": "fragment", "name": name}
        name = self.expect_name()
        alias = None
        if self.peek() == ("PUNCT", ":"):
            self.next()
            alias = name
            name = self.expect_name()
        args = {}
        if self.peek() == ("PUNCT", "("):
            args = self.parse_arguments()
        self._skip_directives()
        selections = None
        if self.peek() == ("PUNCT", "{"):
            selections = self.parse_selection_set()
        return {"kind": "field", "name": name, "alias": alias,
                "args": args, "selections": selections}

    def parse_arguments(self):
        self.expect_punct("(")
        args = {}
        while self.peek() != ("PUNCT", ")"):
            if self.peek()[0] == "EOF":
                raise _ParseFailure("unterminated argument list")
            name = self.expect_name()
            self.expect_punct(":")
            args[name] = self.parse_value()
        self.expect_punct(")")
        return args

    def parse_value(self):
        kind, value = self.next()
        if kind == "VAR":
            return {"$var": value}
        if kind in ("STRING", "INT", "FLOAT"):
            return value
        if kind == "NAME":
            if value == "true":
                return True
            if value == "false":
                return False
            if value == "null":
                return None
            return {"$enum": value}
        if kind == "PUNCT" and value == "[":
            items = []
            while self.peek() != ("PUNCT", "]"):
                items.append(self.parse_value())
            self.expect_punct("]")
            return items
        if kind == "PUNCT" and value == "{":
            obj = {}
            while self.peek() != ("PUNCT", "}"):
                key = self.expect_name()
                self.expect_punct(":")
                obj[key] = self.parse_value()
            self.expect_punct("}")
            return obj
        raise _ParseFailure("unexpected value token %r" % (value,))


def _materialize_value(value, variables):
    """Replace variables / enum markers with runtime values."""
    if isinstance(value, dict):
        if set(value.keys()) == {"$var"}:
            return variables.get(value["$var"])
        if set(value.keys()) == {"$enum"}:
            return value["$enum"]
        return {k: _materialize_value(v, variables) for k, v in value.items()}
    if isinstance(value, list):
        return [_materialize_value(v, variables) for v in value]
    return value


# --------------------------------------------------------------------------
# Execution.
# --------------------------------------------------------------------------

class _TypedValue:
    """A resolved value with its GraphQL type name."""
    __slots__ = ("typename", "value")

    def __init__(self, typename, value):
        self.typename = typename
        self.value = value


class _ExecContext:
    def __init__(self, store, login, variables, fragments):
        self.store = store
        self.login = login
        self.variables = variables
        self.fragments = fragments


def _issue_state_name(state):
    return "CLOSED" if str(state).lower() == "closed" else "OPEN"


def _pull_state_name(pull):
    if pull.merged:
        return "MERGED"
    return "CLOSED" if str(pull.state).lower() == "closed" else "OPEN"


def _page_info_shape(total, start, end):
    """PageInfo for the slice items[start:end] of a list of length total."""
    if end > start:
        start_cursor = _encode_cursor(start)
        end_cursor = _encode_cursor(end - 1)
    else:
        start_cursor = None
        end_cursor = None
    return {
        "hasNextPage": end < total,
        "hasPreviousPage": start > 0,
        "startCursor": start_cursor,
        "endCursor": end_cursor,
    }


def _paginate_nodes(items, args, edge_typename="IssueEdge"):
    """Paginate a list of _TypedValue by first/after. Returns the
    connection payload the connection resolvers read: {"nodes", "edges",
    "pageInfo", "total"}."""
    first = args.get("first")
    after = _decode_cursor(args.get("after"))
    start = (after + 1) if after is not None else 0
    if first is None:
        end = len(items)
    else:
        try:
            end = min(len(items), start + max(0, int(first)))
        except (TypeError, ValueError):
            end = len(items)
    page = items[start:end]
    return {
        "nodes": page,
        "edges": [{"node": node, "cursor": _encode_cursor(start + i)}
                  for i, node in enumerate(page)],
        "edgeTypename": edge_typename,
        "pageInfo": _page_info_shape(len(items), start, end),
        "total": len(items),
    }


def _issues_connection(ctx, repo, args):
    issues = list(repo.list_issues())
    states = args.get("states")
    if states:
        wanted = {str(s).upper() for s in states}
        issues = [i for i in issues
                  if _issue_state_name(i.state) in wanted]
    order_by = args.get("orderBy") or {}
    direction = str(order_by.get("direction") or "DESC").upper()
    issues.sort(key=lambda i: i.number, reverse=(direction != "ASC"))
    nodes = [_TypedValue("Issue", (repo, i)) for i in issues]
    return _paginate_nodes(nodes, args)


def _pulls_connection(ctx, repo, args):
    pulls = list(repo.list_pulls())
    states = args.get("states")
    if states:
        wanted = {str(s).upper() for s in states}
        pulls = [p for p in pulls if _pull_state_name(p) in wanted]
    else:
        # GitHub's default for pullRequests is OPEN only.
        pulls = [p for p in pulls if _pull_state_name(p) == "OPEN"]
    pulls.sort(key=lambda p: p.number, reverse=True)
    nodes = [_TypedValue("PullRequest", (repo, p)) for p in pulls]
    return _paginate_nodes(nodes, args, "PullRequestEdge")


def _search_issue_nodes(ctx, args):
    """type ISSUE only: issues whose title/body contain every bare term of
    the query (key:value qualifiers ignored), across all repositories."""
    if str(args.get("type") or "ISSUE").upper() != "ISSUE":
        return []
    query = str(args.get("query") or "")
    terms = [w.lower() for w in query.split()
             if ":" not in w and w.strip()]
    found = []
    for repo in ctx.store.repos.values():
        for issue in repo.list_issues():
            hay = ("%s %s" % (issue.title, issue.body)).lower()
            if all(term in hay for term in terms):
                found.append(_TypedValue("Issue", (repo, issue)))
    found.sort(key=lambda n: n.value[1].number, reverse=True)
    return found


# -- field resolvers: (ctx, value, field, args) -> scalar | _TypedValue | [_TypedValue] --

def _query_fields(ctx, _value, field, args):
    if field == "viewer":
        return _TypedValue("User", {"login": ctx.login})
    if field == "repository":
        repo = ctx.store.get_repo(args.get("owner"), args.get("name"))
        return _TypedValue("Repository", repo) if repo is not None else None
    if field == "node":
        found = _node_from_gid(ctx.store, args.get("id"))
        return _TypedValue(found[0], found[1]) if found is not None else None
    if field == "search":
        nodes = _search_issue_nodes(ctx, args)
        conn = _paginate_nodes(nodes, args, "SearchEdge")
        conn["issueCount"] = len(nodes)
        return _TypedValue("SearchResult", conn)
    if field == "__type":
        info = _introspect_type(args.get("name"))
        return _TypedValue("__Type", info) if info is not None else None
    if field == "__schema":
        return _TypedValue("__Schema", {})
    raise _GraphQLFailure('Cannot query field "%s" on type "Query".' % field)


def _mutation_fields(ctx, _value, field, args):
    store = ctx.store
    inp = args.get("input") or {}
    if not isinstance(inp, dict):
        raise _GraphQLFailure("mutation %s needs an input object" % field)
    if field == "createIssue":
        repo = _repo_from_gid(store, inp.get("repositoryId"))
        if repo is None:
            raise _GraphQLFailure("repository not found: %s"
                                  % (inp.get("repositoryId"),))
        title = inp.get("title")
        if not isinstance(title, str) or not title:
            raise _GraphQLFailure("createIssue needs a non-empty title")
        try:
            issue = repo.create_issue(title, body=inp.get("body") or "")
        except Exception as exc:  # LabelNotFoundError etc.
            raise _GraphQLFailure(str(exc))
        return _TypedValue("CreateIssuePayload", {
            "issue": _TypedValue("Issue", (repo, issue)),
            "clientMutationId": inp.get("clientMutationId")})
    if field == "closeIssue":
        found = _issue_from_gid(store, inp.get("issueId"))
        if found is None:
            raise _GraphQLFailure("issue not found: %s" % (inp.get("issueId"),))
        repo, issue = found
        issue.state = "closed"
        return _TypedValue("CloseIssuePayload", {
            "issue": _TypedValue("Issue", (repo, issue)),
            "clientMutationId": inp.get("clientMutationId")})
    if field == "createPullRequest":
        repo = _repo_from_gid(store, inp.get("repositoryId"))
        if repo is None:
            raise _GraphQLFailure("repository not found: %s"
                                  % (inp.get("repositoryId"),))
        head = inp.get("headRefName")
        base = inp.get("baseRefName")
        title = inp.get("title")
        if not head or not base or not title:
            raise _GraphQLFailure(
                "createPullRequest needs repositoryId, headRefName, "
                "baseRefName and title")
        try:
            pull = repo.create_pull(title, head, base,
                                    body=inp.get("body") or "")
        except Exception as exc:  # RefNotFoundError etc.
            raise _GraphQLFailure(str(exc))
        return _TypedValue("CreatePullRequestPayload", {
            "pullRequest": _TypedValue("PullRequest", (repo, pull)),
            "clientMutationId": inp.get("clientMutationId")})
    if field == "mergePullRequest":
        found = _pull_from_gid(store, inp.get("pullRequestId"))
        if found is None:
            raise _GraphQLFailure("pull request not found: %s"
                                  % (inp.get("pullRequestId"),))
        repo, pull = found
        try:
            repo.merge_pull(pull.number)
        except Exception as exc:  # RepoNotFoundError / ConflictError
            raise _GraphQLFailure(str(exc))
        return _TypedValue("MergePullRequestPayload", {
            "pullRequest": _TypedValue("PullRequest", (repo, pull)),
            "clientMutationId": inp.get("clientMutationId")})
    if field == "addComment":
        found = _issue_from_gid(store, inp.get("subjectId"))
        if found is None:
            raise _GraphQLFailure("subject not found: %s"
                                  % (inp.get("subjectId"),))
        repo, issue = found
        body = inp.get("body")
        if not isinstance(body, str):
            raise _GraphQLFailure("addComment needs a body string")
        comment = _append_comment(repo, issue, body, ctx.login)
        return _TypedValue("AddCommentPayload", {
            "commentEdge": _TypedValue("CommentEdge", {
                "node": _TypedValue("Comment", comment), "cursor": ""}),
            "subject": _TypedValue("Issue", (repo, issue)),
            "clientMutationId": inp.get("clientMutationId")})
    raise _GraphQLFailure('Cannot query field "%s" on type "Mutation".' % field)


def _repository_fields(ctx, repo, field, args):
    if field == "id":
        return repo.node_id
    if field == "databaseId":
        return repo.database_id
    if field == "name":
        return repo.name
    if field == "nameWithOwner":
        return repo.full_name
    if field == "owner":
        return _TypedValue("User", {"login": repo.owner})
    if field == "defaultBranchRef":
        return _TypedValue("Ref", {"name": repo.default_branch})
    if field == "viewerPermission":
        return "ADMIN"
    if field == "hasIssuesEnabled":
        return True
    if field == "description":
        return repo.description or ""
    if field in ("isPrivate", "private"):
        return bool(repo.private)
    if field == "url":
        return ""
    if field in ("createdAt", "updatedAt"):
        return FIXED_TS
    if field == "fork":
        return False
    if field == "stargazerCount":
        return 0
    if field == "issue":
        issue = repo.get_issue(args.get("number"))
        return _TypedValue("Issue", (repo, issue)) if issue is not None else None
    if field == "issues":
        return _TypedValue("IssueConnection", _issues_connection(ctx, repo, args))
    if field == "pullRequest":
        pull = repo.get_pull(args.get("number"))
        return _TypedValue("PullRequest", (repo, pull)) if pull is not None else None
    if field == "pullRequests":
        return _TypedValue("PullRequestConnection", _pulls_connection(ctx, repo, args))
    raise _GraphQLFailure(
        'Cannot query field "%s" on type "Repository".' % field)


def _issue_fields(ctx, pair, field, args):
    repo, issue = pair
    if field == "id":
        return issue.node_id
    if field == "number":
        return issue.number
    if field == "title":
        return issue.title
    if field == "state":
        return _issue_state_name(issue.state)
    if field == "body":
        return issue.body
    if field == "labels":
        nodes = []
        for name in issue.labels:
            label = repo.get_label(name)
            value = label if label is not None else {"name": name}
            nodes.append(_TypedValue("Label", value))
        conn = _paginate_nodes(nodes, args)
        conn["total"] = len(nodes)
        return _TypedValue("LabelConnection", conn)
    if field == "author":
        return _TypedValue("User", {"login": ctx.login})
    if field in ("createdAt", "updatedAt"):
        return FIXED_TS
    if field == "closedAt":
        return FIXED_TS if str(issue.state).lower() == "closed" else None
    if field == "url":
        return ""
    if field == "comments":
        nodes = [_TypedValue("Comment", c) for c in issue.comments]
        conn = _paginate_nodes(nodes, args)
        conn["total"] = len(nodes)
        return _TypedValue("IssueCommentConnection", conn)
    raise _GraphQLFailure('Cannot query field "%s" on type "Issue".' % field)


def _pull_fields(ctx, pair, field, args):
    repo, pull = pair
    if field == "id":
        return pull.node_id
    if field == "number":
        return pull.number
    if field == "title":
        return pull.title
    if field == "body":
        return pull.body
    if field == "state":
        return _pull_state_name(pull)
    if field == "merged":
        return bool(pull.merged)
    if field == "headRefName":
        return pull.head
    if field == "baseRefName":
        return pull.base
    if field == "mergeable":
        return "MERGEABLE" if pull.mergeable else "CONFLICTING"
    if field == "files":
        try:
            changed = repo.pull_files(pull)
        except Exception:
            changed = []
        nodes = [_TypedValue("PullRequestChangedFile", {"path": f["filename"]})
                 for f in changed if isinstance(f, dict) and f.get("filename")]
        conn = _paginate_nodes(nodes, args)
        conn["total"] = len(nodes)
        return _TypedValue("PullRequestChangedFileConnection", conn)
    if field == "author":
        return _TypedValue("User", {"login": ctx.login})
    if field in ("createdAt", "updatedAt"):
        return FIXED_TS
    if field == "mergedAt":
        return FIXED_TS if pull.merged else None
    if field == "url":
        return ""
    raise _GraphQLFailure(
        'Cannot query field "%s" on type "PullRequest".' % field)


def _user_fields(ctx, value, field, args):
    login = value.get("login") if isinstance(value, dict) else ctx.login
    if field == "login":
        return login
    if field == "id":
        return _encode_b64("User:%s" % login)
    if field == "name":
        return login
    raise _GraphQLFailure('Cannot query field "%s" on type "User".' % field)


def _connection_fields(ctx, conn, field, args):
    if field == "nodes":
        return list(conn["nodes"])
    if field == "edges":
        return [_TypedValue(conn.get("edgeTypename", "IssueEdge"), e)
                for e in conn["edges"]]
    if field == "pageInfo":
        return _TypedValue("PageInfo", conn["pageInfo"])
    if field == "totalCount":
        return conn["total"]
    if field == "issueCount":
        return conn.get("issueCount", conn["total"])
    raise _GraphQLFailure(
        'Cannot query field "%s" on a connection type.' % field)


def _page_info_fields(ctx, value, field, args):
    if field in value:
        return value[field]
    raise _GraphQLFailure(
        'Cannot query field "%s" on type "PageInfo".' % field)


def _edge_fields(ctx, edge, field, args):
    if field == "node":
        return edge["node"]
    if field == "cursor":
        return edge["cursor"]
    raise _GraphQLFailure('Cannot query field "%s" on an edge type.' % field)


def _type_fields(ctx, info, field, args):
    if field == "name":
        return info["name"]
    if field == "kind":
        return info["kind"]
    if field == "description":
        return None
    if field == "fields":
        if info["fields"] is None:
            return None
        return [_TypedValue("__Field", f) for f in info["fields"]]
    if field == "enumValues":
        if info["enumValues"] is None:
            return None
        return [_TypedValue("__EnumValue", v) for v in info["enumValues"]]
    if field == "inputFields":
        return None
    if field == "interfaces":
        return []
    if field == "possibleTypes":
        return []
    if field == "ofType":
        of = info.get("ofType")
        if of is None:
            return None
        sub = {"name": of.get("name"), "kind": of.get("kind"),
               "fields": None, "enumValues": None, "ofType": of.get("ofType")}
        return _TypedValue("__Type", sub)
    raise _GraphQLFailure('Cannot query field "%s" on type "__Type".' % field)


def _field_fields(ctx, info, field, args):
    if field == "name":
        return info["name"]
    if field == "description":
        return None
    if field == "type":
        ref = info["type"]
        sub = {"name": ref.get("name"), "kind": ref.get("kind"),
               "fields": None, "enumValues": None,
               "ofType": ref.get("ofType")}
        return _TypedValue("__Type", sub)
    raise _GraphQLFailure(
        'Cannot query field "%s" on type "__Field".' % field)


def _enum_value_fields(ctx, info, field, args):
    if field == "name":
        return info["name"]
    if field == "description":
        return None
    raise _GraphQLFailure(
        'Cannot query field "%s" on type "__EnumValue".' % field)


def _schema_fields(ctx, _value, field, args):
    if field == "queryType":
        return _TypedValue("__Type", _introspect_type("Query"))
    if field == "mutationType":
        return _TypedValue("__Type", _introspect_type("Mutation"))
    if field == "types":
        return [_TypedValue("__Type", _introspect_type(name))
                for name in sorted(_TYPE_KIND_MAP)]
    if field == "description":
        return None
    raise _GraphQLFailure(
        'Cannot query field "%s" on type "__Schema".' % field)


def _payload_fields(ctx, payload, field, args):
    if field in payload:
        return payload[field]
    raise _GraphQLFailure('Cannot query field "%s" on a payload type.' % field)


def _label_fields(ctx, value, field, args):
    if isinstance(value, dict):
        name = value.get("name")
        color = value.get("color", "")
        description = value.get("description", "")
        node_id = value.get("node_id")
    else:
        name, color, description = value.name, value.color, value.description
        node_id = value.node_id
    if field == "id":
        return node_id
    if field == "name":
        return name
    if field == "color":
        return color
    if field == "description":
        return description
    raise _GraphQLFailure('Cannot query field "%s" on type "Label".' % field)


def _comment_fields(ctx, comment, field, args):
    if field == "id":
        node_id = getattr(comment, "node_id", None)
        if node_id is not None:
            return node_id
        repo = getattr(comment, "repo", None)
        if repo is not None:
            return _comment_gid(repo, comment.issue_number, comment.id)
        return "IC_%s" % comment.id
    if field == "body":
        return comment.body
    if field == "author":
        return _TypedValue("User", {"login": getattr(
            comment, "user_login", None) or ctx.login})
    raise _GraphQLFailure(
        'Cannot query field "%s" on type "Comment".' % field)


def _changed_file_fields(ctx, value, field, args):
    if field == "path":
        return value["path"]
    raise _GraphQLFailure(
        'Cannot query field "%s" on type "PullRequestChangedFile".' % field)


_FIELD_RESOLVERS = {
    "Query": _query_fields,
    "Mutation": _mutation_fields,
    "Repository": _repository_fields,
    "Issue": _issue_fields,
    "PullRequest": _pull_fields,
    "User": _user_fields,
    "Ref": lambda ctx, v, f, a: v[f] if f in v else (_ for _ in ()).throw(
        _GraphQLFailure('Cannot query field "%s" on type "Ref".' % f)),
    "IssueConnection": _connection_fields,
    "PullRequestConnection": _connection_fields,
    "LabelConnection": _connection_fields,
    "IssueCommentConnection": _connection_fields,
    "PullRequestChangedFileConnection": _connection_fields,
    "SearchResult": _connection_fields,
    "PageInfo": _page_info_fields,
    "IssueEdge": _edge_fields,
    "PullRequestEdge": _edge_fields,
    "SearchEdge": _edge_fields,
    "CommentEdge": _edge_fields,
    "__Type": _type_fields,
    "__Field": _field_fields,
    "__EnumValue": _enum_value_fields,
    "__InputValue": _enum_value_fields,
    "__Schema": _schema_fields,
    "CreateIssuePayload": _payload_fields,
    "CloseIssuePayload": _payload_fields,
    "CreatePullRequestPayload": _payload_fields,
    "MergePullRequestPayload": _payload_fields,
    "AddCommentPayload": _payload_fields,
    "Label": _label_fields,
    "Comment": _comment_fields,
    "PullRequestChangedFile": _changed_file_fields,
}


def _run_selections(ctx, node, selections):
    out = {}
    for sel in selections:
        kind = sel["kind"]
        if kind == "fragment":
            frag = ctx.fragments.get(sel["name"])
            if frag is None:
                raise _GraphQLFailure('Unknown fragment "%s".' % sel["name"])
            if frag["type_condition"] and frag["type_condition"] != node.typename:
                continue
            out.update(_run_selections(ctx, node, frag["selections"]))
            continue
        if kind == "inline_fragment":
            if sel["type_condition"] and sel["type_condition"] != node.typename:
                continue
            out.update(_run_selections(ctx, node, sel["selections"]))
            continue
        out[sel["alias"] or sel["name"]] = _run_field(ctx, node, sel)
    return out


def _run_field(ctx, node, sel):
    name = sel["name"]
    if name == "__typename":
        return node.typename
    known = _SCHEMA_FIELDS.get(node.typename, {})
    if name not in known:
        raise _GraphQLFailure(
            'Cannot query field "%s" on type "%s".' % (name, node.typename))
    args = {k: _materialize_value(v, ctx.variables)
            for k, v in sel["args"].items()}
    resolver = _FIELD_RESOLVERS.get(node.typename)
    if resolver is None:
        raise _GraphQLFailure('No resolver for type "%s".' % node.typename)
    result = resolver(ctx, node.value, name, args)
    sub = sel["selections"]
    if sub is None:
        if isinstance(result, _TypedValue):
            raise _GraphQLFailure(
                'Field "%s" of type "%s" must have a selection of '
                "subfields." % (name, node.typename))
        if isinstance(result, list):
            if result and isinstance(result[0], _TypedValue):
                raise _GraphQLFailure(
                    'Field "%s" of type "%s" must have a selection of '
                    "subfields." % (name, node.typename))
        return result
    if result is None:
        return None
    if isinstance(result, _TypedValue):
        return _run_selections(ctx, result, sub)
    if isinstance(result, list):
        return [_run_selections(ctx, item, sub) if isinstance(item, _TypedValue) else item
                for item in result]
    raise _GraphQLFailure(
        'Field "%s" of type "%s" must not have a selection of subfields.'
        % (name, node.typename))


def _run_document(store, body, login):
    """One operation document -> {"data": ...} or {"data": None, "errors": [...]}."""
    if not isinstance(body, dict):
        return {"data": None,
                "errors": [{"message": "operation body must be a JSON object"}]}
    query = body.get("query")
    if not isinstance(query, str) or not query.strip():
        return {"data": None,
                "errors": [{"message": "request needs a query string"}]}
    variables = body.get("variables") or {}
    if not isinstance(variables, dict):
        return {"data": None,
                "errors": [{"message": "variables must be a JSON object"}]}
    try:
        operations, fragments = _DocumentParser(_tokenize(query)).parse_document()
    except _ParseFailure as exc:
        return {"data": None, "errors": [{"message": "Syntax Error: %s" % exc}]}
    if not operations:
        return {"data": None,
                "errors": [{"message": "no operation in document"}]}
    operation_name = body.get("operationName")
    operation = None
    if operation_name:
        for op in operations:
            if op["name"] == operation_name:
                operation = op
                break
        if operation is None:
            return {"data": None, "errors": [
                {"message": 'Unknown operation named "%s".' % operation_name}]}
    elif len(operations) == 1:
        operation = operations[0]
    else:
        return {"data": None, "errors": [
            {"message": "Must provide operation name for multiple operations"}]}
    root_type = "Mutation" if operation["kind"] == "mutation" else "Query"
    ctx = _ExecContext(store, login, variables, fragments)
    try:
        data = _run_selections(ctx, _TypedValue(root_type, None), operation["selections"])
    except _GraphQLFailure as exc:
        return {"data": None, "errors": [{"message": str(exc)}]}
    except Exception as exc:  # never turn a GraphQL failure into a 500
        return {"data": None,
                "errors": [{"message": "internal error: %s: %s"
                            % (type(exc).__name__, exc)}]}
    return {"data": data}


def handle_graphql(store, body, login):
    """Answer one GraphQL request body.

    ``body`` is the parsed JSON request: one operation object
    ``{query, variables?, operationName?}`` or a JSON array of them
    (GitHub's operation batching, graphql-client.ts ``batch()``). Returns
    the response JSON object for one operation, or a list of per-operation
    response objects in request order for a batch. The server wraps the
    return value with HTTP 200 and ``application/json``; auth (401) and
    malformed JSON (400) are enforced by the server before this is called.
    """
    if isinstance(body, list):
        return [_run_document(store, op, login) for op in body]
    return _run_document(store, body, login)
