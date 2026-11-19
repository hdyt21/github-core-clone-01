"""Tests for forge/graphql.py and forge/mcp.py.

Run from the project root:  python3 -m forge.test_graphql_mcp

Uses the REAL store (forge/store.py) -- no stub: the store exists, so
these tests also prove GraphQL/MCP agree with the store the REST layer
uses. Every check mirrors a check the suite runs (checks-graphql.ts /
checks-mcp.ts): the exact documents, the exact tool argument shapes, the
exact error codes.
"""

import json
import sys
import threading

try:
    from forge.graphql import handle_graphql
    from forge.mcp import handle_mcp
    from forge.store import Store
except ImportError:  # run from inside forge/ or as a top-level module
    from graphql import handle_graphql
    from mcp import handle_mcp
    from store import Store

TEST_OWNER = "adaiversen"
AUTH_HEADERS = {"Authorization": "Bearer <redacted>"}

# -- the exact documents the suite sends (checks-graphql.ts) --------------

VIEWER_DOC = """query ViewerLogin {
  viewer {
    login
  }
}"""

REPO_FIELDS_DOC = """query RepositoryFields($owner: String!, $name: String!) {
  repository(owner: $owner, name: $name) {
    id
    databaseId
    name
    owner {
      login
    }
    defaultBranchRef {
      name
    }
    viewerPermission
    hasIssuesEnabled
  }
}"""

ISSUE_FIELDS_DOC = """query Issue_fields {
  Issue: __type(name: "Issue") {
    fields(includeDeprecated: true) {
      name
    }
  }
}"""

TYPE_FIELDS_DOC = """query PullRequestAndRepositoryFields {
  PullRequest: __type(name: "PullRequest") {
    fields(includeDeprecated: true) {
      name
    }
  }
  Repository: __type(name: "Repository") {
    fields(includeDeprecated: true) {
      name
    }
  }
}"""

SEARCH_TYPE_ENUM_DOC = """query SearchType_enumValues {
  SearchType: __type(name: "SearchType") {
    enumValues(includeDeprecated: true) {
      name
    }
  }
}"""

OPEN_ISSUES_DOC = """query OpenIssues($owner: String!, $name: String!, $first: Int!) {
  repository(owner: $owner, name: $name) {
    issues(first: $first, states: [OPEN], orderBy: {field: CREATED_AT, direction: DESC}) {
      nodes {
        number
        title
        state
        id
      }
    }
  }
}"""

ISSUE_BY_NUMBER_DOC = """query IssueByNumber($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      number
      title
      state
      id
      body
    }
  }
}"""

CREATE_ISSUE_DOC = """mutation IssueCreate($input: CreateIssueInput!) {
  createIssue(input: $input) {
    issue {
      number
      id
    }
  }
}"""

CLOSE_ISSUE_DOC = """mutation IssueClose($input: CloseIssueInput!) {
  closeIssue(input: $input) {
    issue {
      number
      id
      state
    }
  }
}"""

PULL_REQUESTS_DOC = """query PullRequests($owner: String!, $name: String!, $first: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequests(first: $first) {
      nodes {
        number
        headRefName
        baseRefName
        mergeable
        state
      }
    }
  }
}"""

PULL_REQUEST_FILES_DOC = """query PullRequestFiles($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      number
      files(first: 10) {
        nodes {
          path
        }
      }
    }
  }
}"""

PULL_REQUEST_ID_DOC = """query PullRequestId($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      id
      number
    }
  }
}"""

CREATE_PULL_REQUEST_DOC = """mutation PullRequestCreate($input: CreatePullRequestInput!) {
  createPullRequest(input: $input) {
    pullRequest {
      number
      id
      state
      headRefName
      baseRefName
    }
  }
}"""

MERGE_PULL_REQUEST_DOC = """mutation MergePullRequest($input: MergePullRequestInput!) {
  mergePullRequest(input: $input) {
    pullRequest {
      number
      merged
      state
    }
  }
}"""

ADD_COMMENT_DOC = """mutation AddComment($input: AddCommentInput!) {
  addComment(input: $input) {
    commentEdge {
      node {
        id
        body
      }
    }
  }
}"""

INVALID_FIELD_DOC = """query ViewerInvalidField {
  viewer {
    login
    noSuchFieldOnViewer
  }
}"""

NODE_REPOSITORY_DOC = """query NodeRepository($id: ID!) {
  node(id: $id) {
    id
    ... on Repository {
      name
      nameWithOwner
    }
  }
}"""

ISSUES_PAGE_DOC = """query IssuesPage($owner: String!, $name: String!, $first: Int!, $after: String) {
  repository(owner: $owner, name: $name) {
    issues(first: $first, after: $after) {
      nodes {
        number
      }
      pageInfo {
        hasNextPage
        endCursor
      }
    }
  }
}"""

SEARCH_DOC = """query SearchIssues {
  search(query: "graphql probe", type: ISSUE, first: 10) {
    nodes {
      ... on Issue {
        number
        title
      }
    }
    issueCount
  }
}"""


# -- helpers ----------------------------------------------------------------

def run_gql(store, query, variables=None):
    body = {"query": query, "variables": variables or {}}
    out = handle_graphql(store, body, TEST_OWNER)
    assert isinstance(out, dict), "single op must answer one object"
    assert "errors" not in out, "unexpected GraphQL errors: %s" % (out,)
    return out["data"]


def run_gql_raw(store, query, variables=None):
    return handle_graphql(store, {"query": query,
                                  "variables": variables or {}}, TEST_OWNER)


def run_mcp(store, method, params=None, req_id=1, headers=AUTH_HEADERS):
    body = json.dumps({"jsonrpc": "2.0", "id": req_id, "method": method,
                       "params": params or {}}).encode("utf-8")
    status, _extra, out = handle_mcp(store, body, headers, TEST_OWNER)
    payload = json.loads(out.decode("utf-8")) if out else None
    return status, payload


def call_tool(store, name, arguments, req_id=1, headers=AUTH_HEADERS):
    status, payload = run_mcp(store, "tools/call",
                              {"name": name, "arguments": arguments},
                              req_id=req_id, headers=headers)
    assert status == 200, "tools/call HTTP %d: %s" % (status, payload)
    assert "error" not in payload, "tools/call RPC error: %s" % (payload,)
    assert payload["id"] == req_id, "response id %r != request id %r" % (
        payload.get("id"), req_id)
    result = payload["result"]
    assert result.get("isError") is not True, "tool error: %s" % (result,)
    text = result["content"][0]["text"]
    return json.loads(text), result


def fixture_repo_with_pr(store, name="gql-repo"):
    """A repo with a main commit (a.txt) and a feature branch that modifies
    a.txt and adds b.txt, plus one open PR feature -> main."""
    repo = store.create_repo(TEST_OWNER, name, description="graphql fixture")
    tip, _ = repo.put_content("a.txt", b"base a\n", "first commit",
                              branch="main")
    repo.create_ref("refs/heads/feature", tip)
    repo.put_content("a.txt", b"head a changed\n", "change a",
                     branch="feature")
    repo.put_content("b.txt", b"new file b\n", "add b", branch="feature")
    pull = repo.create_pull("fixture pull request", "feature", "main",
                            body="fixture")
    return repo, pull


# -- GraphQL tests ----------------------------------------------------------

def test_gql_viewer_login():
    store = Store(owner_login=TEST_OWNER)
    data = run_gql(store, VIEWER_DOC)
    assert data["viewer"]["login"] == TEST_OWNER, data


def test_gql_issue_introspection():
    store = Store(owner_login=TEST_OWNER)
    data = run_gql(store, ISSUE_FIELDS_DOC)
    names = [f["name"] for f in data["Issue"]["fields"]]
    for want in ("id", "number", "title", "state", "body"):
        assert want in names, "Issue missing %s: %s" % (want, names)


def test_gql_type_introspection():
    store = Store(owner_login=TEST_OWNER)
    data = run_gql(store, TYPE_FIELDS_DOC)
    pr = [f["name"] for f in data["PullRequest"]["fields"]]
    for want in ("id", "number", "headRefName", "baseRefName", "mergeable",
                 "state", "merged", "files"):
        assert want in pr, "PullRequest missing %s: %s" % (want, pr)
    repo = [f["name"] for f in data["Repository"]["fields"]]
    for want in ("id", "databaseId", "name", "nameWithOwner", "owner",
                 "defaultBranchRef", "viewerPermission", "hasIssuesEnabled",
                 "issue", "issues", "pullRequests", "pullRequest"):
        assert want in repo, "Repository missing %s: %s" % (want, repo)
    data = run_gql(store, SEARCH_TYPE_ENUM_DOC)
    enums = [e["name"] for e in data["SearchType"]["enumValues"]]
    assert "ISSUE" in enums, enums


def test_gql_repository_fields():
    store = Store(owner_login=TEST_OWNER)
    repo = store.create_repo(TEST_OWNER, "fields-repo", description="d")
    data = run_gql(store, REPO_FIELDS_DOC,
                   {"owner": TEST_OWNER, "name": "fields-repo"})
    node = data["repository"]
    assert node["id"] == repo.node_id, node
    assert node["databaseId"] == repo.database_id, node
    assert node["name"] == "fields-repo", node
    assert node["owner"]["login"] == TEST_OWNER, node
    assert node["defaultBranchRef"]["name"] == "main", node
    assert node["viewerPermission"] in (
        "ADMIN", "MAINTAIN", "WRITE", "TRIAGE", "READ"), node
    assert node["hasIssuesEnabled"] is True, node


def test_gql_open_issues_order():
    store = Store(owner_login=TEST_OWNER)
    repo = store.create_repo(TEST_OWNER, "issues-repo")
    nums = [repo.create_issue("open %d" % i, body="b").number
            for i in (1, 2, 3)]
    closed = repo.create_issue("closed one", body="b")
    closed.state = "closed"
    data = run_gql(store, OPEN_ISSUES_DOC,
                   {"owner": TEST_OWNER, "name": "issues-repo", "first": 10})
    got = [n["number"] for n in data["repository"]["issues"]["nodes"]]
    assert got == sorted(nums, reverse=True), got
    assert closed.number not in got, got
    for node in data["repository"]["issues"]["nodes"]:
        assert node["state"] == "OPEN", node
        assert node["id"], node


def test_gql_issue_by_number():
    store = Store(owner_login=TEST_OWNER)
    repo = store.create_repo(TEST_OWNER, "one-issue")
    issue = repo.create_issue("the title", body="the body")
    data = run_gql(store, ISSUE_BY_NUMBER_DOC,
                   {"owner": TEST_OWNER, "name": "one-issue",
                    "number": issue.number})
    node = data["repository"]["issue"]
    assert node["number"] == issue.number, node
    assert node["title"] == "the title", node
    assert node["body"] == "the body", node
    assert node["state"] == "OPEN", node
    assert node["id"] == issue.node_id, node


def test_gql_create_issue_readback():
    store = Store(owner_login=TEST_OWNER)
    repo = store.create_repo(TEST_OWNER, "create-issue-repo")
    data = run_gql(store, CREATE_ISSUE_DOC, {"input": {
        "repositoryId": repo.node_id, "title": "created over graphql",
        "body": "hello"}})
    number = data["createIssue"]["issue"]["number"]
    issue = repo.get_issue(number)
    assert issue is not None, "issue #%d missing from store" % number
    assert issue.title == "created over graphql", issue.title
    assert issue.state == "open", issue.state
    assert data["createIssue"]["issue"]["id"] == issue.node_id


def test_gql_close_issue():
    store = Store(owner_login=TEST_OWNER)
    repo = store.create_repo(TEST_OWNER, "close-repo")
    issue = repo.create_issue("to close", body="b")
    data = run_gql(store, CLOSE_ISSUE_DOC, {"input": {"issueId": issue.node_id}})
    assert data["closeIssue"]["issue"]["number"] == issue.number, data
    assert data["closeIssue"]["issue"]["state"] == "CLOSED", data
    assert repo.get_issue(issue.number).state == "closed"


def test_gql_pull_requests_list():
    store = Store(owner_login=TEST_OWNER)
    repo, pull = fixture_repo_with_pr(store)
    data = run_gql(store, PULL_REQUESTS_DOC,
                   {"owner": TEST_OWNER, "name": "gql-repo", "first": 5})
    nodes = data["repository"]["pullRequests"]["nodes"]
    node = [n for n in nodes if n["number"] == pull.number]
    assert node, "PR #%d not listed: %s" % (pull.number, nodes)
    node = node[0]
    assert node["headRefName"] == "feature", node
    assert node["baseRefName"] == "main", node
    assert node["state"] == "OPEN", node
    assert node["mergeable"] in ("MERGEABLE", "CONFLICTING", "UNKNOWN"), node


def test_gql_pull_request_files():
    store = Store(owner_login=TEST_OWNER)
    repo, pull = fixture_repo_with_pr(store)
    data = run_gql(store, PULL_REQUEST_FILES_DOC,
                   {"owner": TEST_OWNER, "name": "gql-repo", "number": pull.number})
    paths = [n["path"] for n in
             data["repository"]["pullRequest"]["files"]["nodes"]]
    assert "a.txt" in paths, paths
    assert "b.txt" in paths, paths


def test_gql_create_pull_request_readback():
    store = Store(owner_login=TEST_OWNER)
    repo, _ = fixture_repo_with_pr(store, name="create-pr-repo")
    tip = repo.branch_tip("main")
    repo.create_ref("refs/heads/create-branch", tip)
    repo.put_content("c.txt", b"c\n", "add c", branch="create-branch")
    data = run_gql(store, CREATE_PULL_REQUEST_DOC, {"input": {
        "repositoryId": repo.node_id, "baseRefName": "main",
        "headRefName": "create-branch", "title": "created over graphql"}})
    pr = data["createPullRequest"]["pullRequest"]
    assert pr["state"] == "OPEN", pr
    assert pr["headRefName"] == "create-branch", pr
    assert pr["baseRefName"] == "main", pr
    stored = repo.get_pull(pr["number"])
    assert stored is not None, "PR #%d missing from store" % pr["number"]
    assert stored.head == "create-branch" and stored.base == "main", stored
    assert pr["id"] == stored.node_id


def test_gql_merge_pull_request():
    store = Store(owner_login=TEST_OWNER)
    repo, pull = fixture_repo_with_pr(store, name="merge-repo")
    data = run_gql(store, PULL_REQUEST_ID_DOC,
                   {"owner": TEST_OWNER, "name": "merge-repo", "number": pull.number})
    pr_id = data["repository"]["pullRequest"]["id"]
    data = run_gql(store, MERGE_PULL_REQUEST_DOC,
                   {"input": {"pullRequestId": pr_id, "mergeMethod": "SQUASH"}})
    merged = data["mergePullRequest"]["pullRequest"]
    assert merged["merged"] is True, merged
    assert merged["number"] == pull.number, merged
    stored = repo.get_pull(pull.number)
    assert stored.merged is True and stored.state == "closed", stored
    assert stored.merge_commit_sha, stored


def test_gql_add_comment():
    store = Store(owner_login=TEST_OWNER)
    repo = store.create_repo(TEST_OWNER, "comment-repo")
    issue = repo.create_issue("commented issue", body="b")
    data = run_gql(store, ADD_COMMENT_DOC, {"input": {
        "subjectId": issue.node_id, "body": "a graphql comment"}})
    node = data["addComment"]["commentEdge"]["node"]
    assert node["body"] == "a graphql comment", node
    assert node["id"], node
    comments = repo.get_issue(issue.number).comments
    assert any(c.body == "a graphql comment" for c in comments), comments


def test_gql_invalid_field_errors():
    store = Store(owner_login=TEST_OWNER)
    out = run_gql_raw(store, INVALID_FIELD_DOC)
    assert "errors" in out and out["errors"], out
    assert any("noSuchFieldOnViewer" in e.get("message", "")
               for e in out["errors"]), out


def test_gql_unknown_repository_null():
    store = Store(owner_login=TEST_OWNER)
    out = run_gql_raw(store, REPO_FIELDS_DOC,
                      {"owner": TEST_OWNER, "name": "does-not-exist"})
    assert "errors" not in out or not out["errors"], out
    assert out["data"]["repository"] is None, out


def test_gql_batch_two_ops():
    store = Store(owner_login=TEST_OWNER)
    out = handle_graphql(store, [{"query": VIEWER_DOC},
                                 {"query": ISSUE_FIELDS_DOC}], TEST_OWNER)
    assert isinstance(out, list) and len(out) == 2, out
    assert out[0]["data"]["viewer"]["login"] == TEST_OWNER, out
    assert out[1]["data"]["Issue"]["fields"], out


def test_gql_node_repository():
    store = Store(owner_login=TEST_OWNER)
    repo = store.create_repo(TEST_OWNER, "node-repo")
    data = run_gql(store, NODE_REPOSITORY_DOC, {"id": repo.node_id})
    node = data["node"]
    assert node["id"] == repo.node_id, node
    assert node["name"] == "node-repo", node
    assert node["nameWithOwner"] == "adaiversen/node-repo", node


def test_gql_global_id_stability():
    store = Store(owner_login=TEST_OWNER)
    repo = store.create_repo(TEST_OWNER, "stable-repo")
    issue = repo.create_issue("stable", body="b")
    first = run_gql(store, REPO_FIELDS_DOC,
                    {"owner": TEST_OWNER, "name": "stable-repo"})
    second = run_gql(store, REPO_FIELDS_DOC,
                     {"owner": TEST_OWNER, "name": "stable-repo"})
    assert first["repository"]["id"] == second["repository"]["id"]
    assert first["repository"]["id"] != str(repo.database_id)
    first_i = run_gql(store, ISSUE_BY_NUMBER_DOC,
                      {"owner": TEST_OWNER, "name": "stable-repo",
                       "number": issue.number})
    second_i = run_gql(store, ISSUE_BY_NUMBER_DOC,
                       {"owner": TEST_OWNER, "name": "stable-repo",
                        "number": issue.number})
    assert first_i["repository"]["issue"]["id"] == \
        second_i["repository"]["issue"]["id"]
    assert first_i["repository"]["issue"]["id"] != str(issue.number)


def test_gql_issues_pagination():
    store = Store(owner_login=TEST_OWNER)
    repo = store.create_repo(TEST_OWNER, "pages-repo")
    numbers = [repo.create_issue("page %d" % i, body="b").number
               for i in range(1, 8)]
    first = run_gql(store, ISSUES_PAGE_DOC,
                    {"owner": TEST_OWNER, "name": "pages-repo", "first": 5,
                     "after": None})
    conn = first["repository"]["issues"]
    assert len(conn["nodes"]) == 5, conn
    assert conn["pageInfo"]["hasNextPage"] is True, conn
    assert conn["pageInfo"]["endCursor"], conn
    second = run_gql(store, ISSUES_PAGE_DOC,
                     {"owner": TEST_OWNER, "name": "pages-repo", "first": 5,
                      "after": conn["pageInfo"]["endCursor"]})
    conn2 = second["repository"]["issues"]
    assert len(conn2["nodes"]) == 2, conn2
    assert conn2["pageInfo"]["hasNextPage"] is False, conn2
    seen = [n["number"] for n in conn["nodes"] + conn2["nodes"]]
    assert sorted(seen) == sorted(numbers), seen
    assert len(set(seen)) == 7, seen


def test_gql_search_issue():
    store = Store(owner_login=TEST_OWNER)
    repo = store.create_repo(TEST_OWNER, "search-repo")
    repo.create_issue("graphql probe one", body="b")
    repo.create_issue("unrelated", body="b")
    data = run_gql(store, SEARCH_DOC, {})
    nodes = data["search"]["nodes"]
    assert any(n["title"] == "graphql probe one" for n in nodes), nodes
    assert data["search"]["issueCount"] == 1, data


# -- MCP tests ----------------------------------------------------------------

def test_mcp_initialize_handshake():
    store = Store(owner_login=TEST_OWNER)
    status, payload = run_mcp(store, "initialize", {
        "protocolVersion": "2025-11-25", "capabilities": {},
        "clientInfo": {"name": "env", "version": "1.0.0"}}, req_id="init-1")
    assert status == 200, payload
    result = payload["result"]
    assert result["protocolVersion"] == "2025-11-25", result
    assert result["serverInfo"]["name"], result
    assert result["serverInfo"]["version"], result
    assert isinstance(result["capabilities"].get("tools"), dict), result
    assert payload["id"] == "init-1", payload


def test_mcp_server_discover():
    store = Store(owner_login=TEST_OWNER)
    status, payload = run_mcp(store, "server/discover", {}, req_id="disc-1")
    assert status == 200, payload
    versions = payload["result"]["supportedVersions"]
    assert versions, versions
    assert "2026-07-28" in versions, versions
    for version in versions:
        assert len(version) == 10 and version[4] == "-" and version[7] == "-", version
    assert payload["result"]["serverInfo"]["name"], payload
    assert isinstance(payload["result"]["capabilities"].get("tools"), dict)


def test_mcp_notification_initialized():
    store = Store(owner_login=TEST_OWNER)
    body = json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized",
                       "params": {}}).encode("utf-8")
    status, _extra, out = handle_mcp(store, body, AUTH_HEADERS, TEST_OWNER)
    assert status == 202 and out == b"", (status, out)


def test_mcp_tools_list_idempotent():
    store = Store(owner_login=TEST_OWNER)
    status1, payload1 = run_mcp(store, "tools/list", {}, req_id=1)
    status2, payload2 = run_mcp(store, "tools/list", {}, req_id=2)
    assert status1 == 200 and status2 == 200
    tools1, tools2 = payload1["result"]["tools"], payload2["result"]["tools"]
    assert json.dumps(tools1, sort_keys=True) == json.dumps(tools2, sort_keys=True)
    assert len(tools1) >= 6, tools1
    names = [t["name"] for t in tools1]
    for want in ("create_repo", "get_repo", "create_issue", "list_issues",
                 "create_pull_request", "merge_pull_request"):
        assert want in names, names
    for tool in tools1:
        assert isinstance(tool.get("description"), str) and tool["description"], tool
        schema = tool.get("inputSchema") or {}
        assert schema.get("type") == "object", tool
        assert isinstance(schema.get("properties"), dict), tool
        assert isinstance(schema.get("required"), list), tool


def test_mcp_create_repo_parity():
    store = Store(owner_login=TEST_OWNER)
    payload, _result = call_tool(store, "create_repo",
                                 {"name": "mcp-parity", "description": "d",
                                  "private": True})
    assert payload["full_name"] == "adaiversen/mcp-parity", payload
    repo = store.get_repo(TEST_OWNER, "mcp-parity")
    assert repo is not None and repo.private is True, repo
    assert repo.description == "d", repo
    first_id, second_id = repo.id, store.get_repo(TEST_OWNER, "mcp-parity").id
    assert first_id == second_id


def test_mcp_get_repo_prefixed():
    store = Store(owner_login=TEST_OWNER)
    store.create_repo(TEST_OWNER, "mcp-get")
    payload, _ = call_tool(store, "github__get_repo",
                           {"full_name": "adaiversen/mcp-get"})
    assert "mcp-get" in json.dumps(payload), payload
    assert payload["full_name"] == "adaiversen/mcp-get", payload


def test_mcp_create_issue_and_list():
    store = Store(owner_login=TEST_OWNER)
    store.create_repo(TEST_OWNER, "mcp-issues")
    payload, _ = call_tool(store, "create_issue",
                           {"owner": TEST_OWNER, "repo": "mcp-issues",
                            "title": "mcp issue one", "body": "b"})
    number = payload["number"]
    listed = store.get_repo(TEST_OWNER, "mcp-issues").list_issues()
    assert any(i.number == number and i.title == "mcp issue one"
               for i in listed), listed
    payload2, _ = call_tool(store, "list_issues",
                            {"owner": TEST_OWNER, "repo": "mcp-issues",
                             "state": "open"})
    text = json.dumps(payload2)
    assert "mcp issue one" in text, text
    assert '"number": %d' % number in text, text


def test_mcp_create_and_merge_pr():
    store = Store(owner_login=TEST_OWNER)
    repo = store.create_repo(TEST_OWNER, "mcp-prs")
    tip, _ = repo.put_content("a.txt", b"base\n", "first", branch="main")
    repo.create_ref("refs/heads/mcp-head", tip)
    repo.put_content("a.txt", b"changed\n", "change", branch="mcp-head")
    payload, _ = call_tool(store, "create_pull_request",
                           {"owner": TEST_OWNER, "repo": "mcp-prs",
                            "title": "mcp pr", "head": "mcp-head",
                            "base": "main"})
    number = payload["number"]
    stored = repo.get_pull(number)
    assert stored is not None, payload
    assert stored.head == "mcp-head" and stored.base == "main", stored
    payload2, _ = call_tool(store, "merge_pull_request",
                            {"owner": TEST_OWNER, "repo": "mcp-prs",
                             "pull_number": number})
    assert payload2["merged"] is True, payload2
    stored = repo.get_pull(number)
    assert stored.merged is True and stored.state == "closed", stored


def test_mcp_merge_number_variant():
    store = Store(owner_login=TEST_OWNER)
    repo = store.create_repo(TEST_OWNER, "mcp-pr-variant")
    tip, _ = repo.put_content("a.txt", b"base\n", "first", branch="main")
    repo.create_ref("refs/heads/h2", tip)
    repo.put_content("a.txt", b"v2\n", "change", branch="h2")
    pull = repo.create_pull("variant pr", "h2", "main")
    payload, _ = call_tool(store, "merge_pull_request",
                           {"owner": TEST_OWNER, "repo": "mcp-pr-variant",
                            "pullNumber": pull.number})
    assert payload["merged"] is True, payload


def test_mcp_unknown_tool():
    store = Store(owner_login=TEST_OWNER)
    status, payload = run_mcp(store, "tools/call",
                              {"name": "mcp_no_such_tool_abcdef",
                               "arguments": {}}, req_id=9)
    assert status != 500, payload
    assert payload["error"]["code"] in (-32602, -32601), payload


def test_mcp_missing_arguments():
    store = Store(owner_login=TEST_OWNER)
    _status, payload = run_mcp(store, "tools/call",
                               {"name": "create_issue", "arguments": {}},
                               req_id=10)
    assert "error" not in payload, payload
    assert payload["result"]["isError"] is True, payload


def test_mcp_parse_error():
    store = Store(owner_login=TEST_OWNER)
    body = b'{"jsonrpc": "2.0", "id": 424242, "method": "tools/list"'
    status, _extra, out = handle_mcp(store, body, AUTH_HEADERS, TEST_OWNER)
    payload = json.loads(out.decode("utf-8"))
    assert status == 400, (status, payload)
    assert payload["error"]["code"] == -32700, payload


def test_mcp_auth_required():
    store = Store(owner_login=TEST_OWNER)
    status, payload = run_mcp(store, "tools/call",
                              {"name": "create_issue",
                               "arguments": {"owner": "no-auth",
                                             "repo": "no-auth-x",
                                             "title": "t"}},
                              req_id=11, headers={})
    assert status == 401, (status, payload)


def test_mcp_concurrent_ids():
    store = Store(owner_login=TEST_OWNER)
    store.create_repo(TEST_OWNER, "concurrent-left")
    store.create_repo(TEST_OWNER, "concurrent-right")
    results = {}

    def call(tag, repo_name):
        status, payload = run_mcp(
            store, "tools/call",
            {"name": "get_repo",
             "arguments": {"owner": TEST_OWNER, "repo": repo_name}},
            req_id="concurrent-%s" % tag)
        results[tag] = (status, payload)

    left = threading.Thread(target=call, args=("left", "concurrent-left"))
    right = threading.Thread(target=call, args=("right", "concurrent-right"))
    left.start()
    right.start()
    left.join()
    right.join()
    for tag, repo_name in (("left", "concurrent-left"),
                           ("right", "concurrent-right")):
        status, payload = results[tag]
        assert status == 200, (tag, payload)
        assert payload["id"] == "concurrent-%s" % tag, payload
        assert repo_name in payload["result"]["content"][0]["text"], payload


# -- runner -----------------------------------------------------------------

ALL_TESTS = [
    test_gql_viewer_login,
    test_gql_issue_introspection,
    test_gql_type_introspection,
    test_gql_repository_fields,
    test_gql_open_issues_order,
    test_gql_issue_by_number,
    test_gql_create_issue_readback,
    test_gql_close_issue,
    test_gql_pull_requests_list,
    test_gql_pull_request_files,
    test_gql_create_pull_request_readback,
    test_gql_merge_pull_request,
    test_gql_add_comment,
    test_gql_invalid_field_errors,
    test_gql_unknown_repository_null,
    test_gql_batch_two_ops,
    test_gql_node_repository,
    test_gql_global_id_stability,
    test_gql_issues_pagination,
    test_gql_search_issue,
    test_mcp_initialize_handshake,
    test_mcp_server_discover,
    test_mcp_notification_initialized,
    test_mcp_tools_list_idempotent,
    test_mcp_create_repo_parity,
    test_mcp_get_repo_prefixed,
    test_mcp_create_issue_and_list,
    test_mcp_create_and_merge_pr,
    test_mcp_merge_number_variant,
    test_mcp_unknown_tool,
    test_mcp_missing_arguments,
    test_mcp_parse_error,
    test_mcp_auth_required,
    test_mcp_concurrent_ids,
]


def run_all():
    passed, failed = 0, 0
    for test in ALL_TESTS:
        try:
            test()
        except Exception as exc:
            failed += 1
            print("FAIL %s: %s: %s" % (test.__name__, type(exc).__name__, exc))
        else:
            passed += 1
            print("PASS %s" % test.__name__)
    print("%d passed, %d failed, %d total" % (passed, failed, len(ALL_TESTS)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(run_all())
