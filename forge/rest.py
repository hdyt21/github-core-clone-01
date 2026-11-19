"""GitHub REST v3 handlers for the forge.

One entry point, as the server calls it:

    handle_rest(store, method, path, query, headers, body)
        -> (status, response_headers, body_bytes)

``path`` is the request path under ``/api/v3`` (``/api/v3`` itself or
``/api/v3/...``); ``query`` maps names to (single) string values; ``headers``
is a case-insensitive mapping (the server's header object or a plain dict);
``body`` is the raw request bytes.

Auth is enforced by the SERVER (it checks the Bearer token before calling
here), so this module never returns 401. It DOES return 404 (never 403)
when the owner in the path is not the token login (``store.owner_login``).

Every reply is ``application/json`` (the server sets the Content-Type) and
carries a non-empty ``X-GitHub-Request-Id`` (deterministic ``req-<n>``
counter) plus, on the root route, ``X-OAuth-Scopes: repo, read:org``.
List replies honour ``?page=`` / ``?per_page=`` (default 30/page) and carry
an absolute ``Link`` header with ``rel="next"`` (and ``rel="last"``) when
more pages exist; the absolute URL is built from the request's Host header.

Endpoints (the full list the kit's checks-rest.ts drives, plus the small
git/refs surface the same checks use to create branches):
  GET    /api/v3/                                   root + scopes header
  GET    /api/v3/meta                               {"installed_version": ...}
  GET    /api/v3/user/repos                         paginated repo list
  POST   /api/v3/user/repos                         201 repo (422 duplicate)
  GET    /api/v3/repos/{o}/{r}                      repo / 404
  PATCH  /api/v3/repos/{o}/{r}                      update description/...
  DELETE /api/v3/repos/{o}/{r}                      204
  POST   /api/v3/repos/{o}/{r}/git/refs             201 {ref, object:{sha}}
  GET    /api/v3/repos/{o}/{r}/git/refs[/...]       ref(s)
  DELETE /api/v3/repos/{o}/{r}/git/refs/{ref}       204
  GET    /api/v3/repos/{o}/{r}/branches             [{name, commit:{sha}}]
  GET    /api/v3/repos/{o}/{r}/commits[?sha=]       [{sha, commit:{message}}]
  PUT    /api/v3/repos/{o}/{r}/contents/{path}      201 create / 200 update / 409 stale sha
  GET    /api/v3/repos/{o}/{r}/contents/{path}      file object or dir listing
  DELETE /api/v3/repos/{o}/{r}/contents/{path}      200 {commit:{sha}}
  POST   /api/v3/repos/{o}/{r}/labels               201 label
  GET    /api/v3/repos/{o}/{r}/labels               list
  POST   /api/v3/repos/{o}/{r}/issues               201 open issue / 422 unknown label
  GET    /api/v3/repos/{o}/{r}/issues               list (?state=, ?sort=created&direction=)
  GET    /api/v3/repos/{o}/{r}/issues/{n}           issue / 404
  PATCH  /api/v3/repos/{o}/{r}/issues/{n}           update state/title/body/labels
  POST   /api/v3/repos/{o}/{r}/issues/{n}/comments  201 comment
  GET    /api/v3/repos/{o}/{r}/issues/{n}/comments  list
  POST   /api/v3/repos/{o}/{r}/pulls                201 open pull
  GET    /api/v3/repos/{o}/{r}/pulls                list (?state=, default open)
  GET    /api/v3/repos/{o}/{r}/pulls/{n}            pull / 404
  GET    /api/v3/repos/{o}/{r}/pulls/{n}/files      [{filename, status, additions, ...}]
  PUT    /api/v3/repos/{o}/{r}/pulls/{n}/merge      200 {sha, merged:true, message}
                                                    404 unknown / 405 conflicted
  anything else -> 404 {"message": "Not Found"}; malformed JSON body -> 400.

Standard library only. Handlers are O(1) per issue/pull read (dict lookup);
list endpoints sort only the repo's own small collections.
"""

import base64
import binascii
import itertools
import json
import threading
from urllib.parse import unquote

try:
    from .gitcore import parse_commit
    from .store import (Comment, ConflictError, FIXED_TS, LabelNotFoundError,
                        RefExistsError, RefNotFoundError, RepoExistsError,
                        RepoNotFoundError, StaleShaError, StoreError)
except ImportError:  # running as a top-level package dir, not `forge.*`
    from gitcore import parse_commit
    from store import (Comment, ConflictError, FIXED_TS, LabelNotFoundError,
                       RefExistsError, RefNotFoundError, RepoExistsError,
                       RepoNotFoundError, StaleShaError, StoreError)

FORGE_VERSION = "1.0.0"

_request_counter = itertools.count(1)
_request_lock = threading.Lock()


def _fresh_request_id():
    with _request_lock:
        return "req-%d" % next(_request_counter)


def _header_value(headers, name):
    if headers is None:
        return None
    try:
        return headers.get(name)
    except Exception:
        return None


def _request_base(headers):
    host = _header_value(headers, "Host") or _header_value(headers, "host") or "localhost"
    host = str(host).strip()
    if "://" in host:
        host = host.split("://", 1)[1].split("/", 1)[0]
    return "http://%s" % host


# -- response helpers ---------------------------------------------------

def _reply(status, obj, extra=None):
    headers = {"X-GitHub-Request-Id": _fresh_request_id()}
    if extra:
        headers.update(extra)
    if obj is None:
        return status, headers, b""
    return status, headers, json.dumps(obj, separators=(",", ":"),
                                       ensure_ascii=False).encode("utf-8")


def _reply_error(status, message):
    return _reply(status, {"message": message})


def _reply_not_found(msg="Not Found"):
    return _reply_error(404, msg)


def _read_json_body(body):
    """-> (obj_dict, error_response|None). Empty body is {}."""
    if body is None or body == b"" or body == "":
        return {}, None
    if isinstance(body, dict):
        return body, None
    try:
        text = body.decode("utf-8") if isinstance(body, (bytes, bytearray)) else str(body)
        obj = json.loads(text)
    except (ValueError, UnicodeDecodeError):
        return None, _reply_error(400, "Problems parsing JSON")
    if not isinstance(obj, dict):
        return None, _reply_error(400, "Problems parsing JSON")
    return obj, None


def _query_int(query, name, default):
    try:
        return int(query.get(name, default))
    except (TypeError, ValueError):
        return default


def _page_items(items, query, path, headers):
    """(page_items, link_extra_headers). Absolute Link URLs from Host."""
    page = max(1, _query_int(query, "page", 1))
    per_page = _query_int(query, "per_page", 30)
    per_page = max(1, min(100, per_page))
    total = len(items)
    start = (page - 1) * per_page
    page_items = items[start:start + per_page]
    extra = {}
    last_page = max(1, (total + per_page - 1) // per_page)
    if page < last_page or (total > per_page and page == 1 and last_page > 1):
        base = _request_base(headers)
        nxt = "%s%s?page=%d&per_page=%d" % (base, path, page + 1, per_page)
        last = "%s%s?page=%d&per_page=%d" % (base, path, last_page, per_page)
        links = ['<%s>; rel="next"' % nxt, '<%s>; rel="last"' % last]
        if page > 1:
            first = "%s%s?page=1&per_page=%d" % (base, path, per_page)
            prev = "%s%s?page=%d&per_page=%d" % (base, path, page - 1, per_page)
            links += ['<%s>; rel="first"' % first, '<%s>; rel="prev"' % prev]
        extra["Link"] = ", ".join(links)
    return page_items, extra


# -- JSON shapes ----------------------------------------------------------
# Every field the suite reads is present; timestamps are all FIXED_TS.
# See forge/store.py for the deterministic id / node_id scheme.

def _user_shape(login):
    return {"login": login, "id": 1, "node_id": _encode_b64("User:1"),
            "avatar_url": "", "gravatar_id": "", "url": "",
            "html_url": "", "type": "User", "site_admin": False}


def _encode_b64(text):
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def _repo_shape(repo, base):
    o, r = repo.owner, repo.name
    prefix = "%s/api/v3/repos/%s/%s" % (base, o, r)
    open_issues = sum(1 for i in repo.issues.values() if i.state == "open")
    return {
        "id": repo.database_id, "node_id": repo.node_id, "name": r,
        "full_name": repo.full_name,
        "owner": _user_shape(o),
        "private": repo.private,
        "html_url": "%s/%s/%s" % (base, o, r),
        "description": repo.description, "fork": False,
        "url": prefix,
        "archive_url": prefix + "/{archive_format}{/ref}",
        "assignees_url": prefix + "/assignees{/user}",
        "blobs_url": prefix + "/git/blobs{/sha}",
        "branches_url": prefix + "/branches{/branch}",
        "clone_url": "%s/%s/%s.git" % (base, o, r),
        "collaborators_url": prefix + "/collaborators{/collaborator}",
        "comments_url": prefix + "/comments{/number}",
        "commits_url": prefix + "/commits{/sha}",
        "compare_url": prefix + "/compare/{base}...{head}",
        "contents_url": prefix + "/contents/{+path}",
        "contributors_url": prefix + "/contributors",
        "deployments_url": prefix + "/deployments",
        "downloads_url": prefix + "/downloads",
        "events_url": prefix + "/events",
        "forks_url": prefix + "/forks",
        "git_commits_url": prefix + "/git/commits{/sha}",
        "git_refs_url": prefix + "/git/refs{/sha}",
        "git_tags_url": prefix + "/git/tags{/sha}",
        "git_url": "git://%s/%s/%s.git" % (base.split("://", 1)[-1], o, r),
        "issue_comment_url": prefix + "/issues/comments{/number}",
        "issue_events_url": prefix + "/issues/events{/number}",
        "issues_url": prefix + "/issues{/number}",
        "keys_url": prefix + "/keys{/key_id}",
        "labels_url": prefix + "/labels{/name}",
        "languages_url": prefix + "/languages",
        "merges_url": prefix + "/merges",
        "milestones_url": prefix + "/milestones{/number}",
        "notifications_url": prefix + "/notifications{?since,all,participating}",
        "pulls_url": prefix + "/pulls{/number}",
        "releases_url": prefix + "/releases{/id}",
        "ssh_url": "git@%s:%s/%s.git" % (base.split("://", 1)[-1], o, r),
        "stargazers_url": prefix + "/stargazers",
        "statuses_url": prefix + "/statuses/{sha}",
        "subscribers_url": prefix + "/subscribers",
        "subscription_url": prefix + "/subscription",
        "tags_url": prefix + "/tags",
        "teams_url": prefix + "/teams",
        "trees_url": prefix + "/git/trees{/sha}",
        "created_at": FIXED_TS, "updated_at": FIXED_TS, "pushed_at": FIXED_TS,
        "git_url_short": None,
        "homepage": None, "size": 0, "stargazers_count": 0,
        "watchers_count": 0, "language": None, "has_issues": True,
        "has_projects": True, "has_downloads": True, "has_wiki": True,
        "has_pages": False, "has_discussions": False, "forks_count": 0,
        "mirror_url": None, "archived": False, "disabled": False,
        "open_issues_count": open_issues, "license": None, "allow_forking": True,
        "is_template": False, "web_commit_signoff_required": False,
        "topics": [], "visibility": "private" if repo.private else "public",
        "forks": 0, "open_issues": open_issues, "watchers": 0,
        "default_branch": repo.default_branch,
        "permissions": {"admin": True, "maintain": True, "push": True,
                        "triage": True, "pull": True},
    }


def _label_shape(repo, label, base):
    prefix = "%s/api/v3/repos/%s/%s" % (base, repo.owner, repo.name)
    d = label.to_dict()
    d["url"] = "%s/labels/%s" % (prefix, label.name)
    return d


def _issue_shape(repo, issue, base):
    prefix = "%s/api/v3/repos/%s/%s" % (base, repo.owner, repo.name)
    url = "%s/issues/%d" % (prefix, issue.number)
    return {
        "id": issue.id, "node_id": issue.node_id, "number": issue.number,
        "title": issue.title, "body": issue.body, "state": issue.state,
        "locked": False,
        "labels": [_label_shape(repo, repo.labels[n], base)
                   for n in issue.labels if n in repo.labels],
        "user": _user_shape(repo.owner),
        "assignee": None, "assignees": [], "milestone": None,
        "comments": len(issue.comments),
        "created_at": FIXED_TS, "updated_at": FIXED_TS,
        "closed_at": FIXED_TS if issue.state == "closed" else None,
        "author_association": "OWNER",
        "html_url": "%s/%s/%s/issues/%d" % (base, repo.owner, repo.name,
                                            issue.number),
        "url": url, "repository_url": prefix,
        "labels_url": url + "/labels{/name}",
        "comments_url": url + "/comments",
        "events_url": url + "/events",
    }


def _comment_shape(repo, comment, base):
    prefix = "%s/api/v3/repos/%s/%s" % (base, repo.owner, repo.name)
    url = "%s/issues/comments/%d" % (prefix, comment.id)
    return {
        "id": comment.id, "node_id": comment.node_id, "body": comment.body,
        "user": _user_shape(repo.owner),
        "created_at": FIXED_TS, "updated_at": FIXED_TS,
        "html_url": "%s/%s/%s/issues/%d#issuecomment-%d"
                    % (base, repo.owner, repo.name, comment.issue_number,
                       comment.id),
        "url": url,
        "issue_url": "%s/issues/%d" % (prefix, comment.issue_number),
    }


def _pull_shape(repo, pull, base):
    prefix = "%s/api/v3/repos/%s/%s" % (base, repo.owner, repo.name)
    url = "%s/pulls/%d" % (prefix, pull.number)
    files = repo.pull_files(pull)
    additions = sum(f["additions"] for f in files)
    deletions = sum(f["deletions"] for f in files)
    head_repo = {"id": repo.database_id, "node_id": repo.node_id,
                 "name": repo.name, "full_name": repo.full_name}
    return {
        "id": pull.id, "node_id": pull.node_id, "number": pull.number,
        "title": pull.title, "body": pull.body, "state": pull.state,
        "locked": False,
        "head": {"label": "%s:%s" % (repo.owner, pull.head),
                 "ref": pull.head, "sha": pull.head_sha,
                 "user": _user_shape(repo.owner), "repo": head_repo},
        "base": {"label": "%s:%s" % (repo.owner, pull.base),
                 "ref": pull.base, "sha": pull.base_sha,
                 "user": _user_shape(repo.owner), "repo": head_repo},
        "merged": pull.merged, "mergeable": pull.mergeable,
        "merge_commit_sha": pull.merge_commit_sha,
        "merged_at": FIXED_TS if pull.merged else None,
        "created_at": FIXED_TS, "updated_at": FIXED_TS,
        "closed_at": FIXED_TS if pull.state == "closed" else None,
        "author_association": "OWNER",
        "user": _user_shape(repo.owner),
        "assignee": None, "assignees": [], "requested_reviewers": [],
        "requested_teams": [], "labels": [], "milestone": None,
        "comments": 0, "review_comments": 0,
        "commits": 1, "additions": additions, "deletions": deletions,
        "changed_files": len(files),
        "html_url": "%s/%s/%s/pull/%d" % (base, repo.owner, repo.name,
                                          pull.number),
        "url": url, "diff_url": url + ".diff", "patch_url": url + ".patch",
        "issue_url": "%s/issues/%d" % (prefix, pull.number),
        "commits_url": url + "/commits",
        "review_comments_url": url + "/comments",
        "review_comment_url": prefix + "/pulls/comments{/number}",
        "comments_url": url + "/comments",
        "statuses_url": prefix + "/statuses/{sha}",
    }


def _file_shape(repo, path, data, blob_sha, base):
    prefix = "%s/api/v3/repos/%s/%s" % (base, repo.owner, repo.name)
    name = path.rsplit("/", 1)[-1]
    return {
        "type": "file", "encoding": "base64", "size": len(data),
        "name": name, "path": path,
        "content": base64.b64encode(data).decode("ascii"),
        "sha": blob_sha,
        "url": "%s/contents/%s" % (prefix, path),
        "html_url": "%s/%s/%s/blob/main/%s"
                    % (base, repo.owner, repo.name, path),
        "git_url": "%s/git/blobs/%s" % (prefix, blob_sha),
        "download_url": "%s/contents/%s" % (prefix, path),
        "_links": {"self": "%s/contents/%s" % (prefix, path),
                   "git": "%s/git/blobs/%s" % (prefix, blob_sha),
                   "html": "%s/%s/%s/blob/main/%s"
                           % (base, repo.owner, repo.name, path)},
    }


def _dir_shape(repo, entry, base):
    prefix = "%s/api/v3/repos/%s/%s" % (base, repo.owner, repo.name)
    return {
        "type": entry["type"], "name": entry["name"], "path": entry["path"],
        "sha": entry["sha"],
        "size": 0 if entry["type"] == "dir" else None,
        "url": "%s/contents/%s" % (prefix, entry["path"]),
        "html_url": "%s/%s/%s/tree/main/%s"
                    % (base, repo.owner, repo.name, entry["path"]),
        "git_url": "%s/git/trees/%s" % (prefix, entry["sha"]),
        "download_url": None,
        "_links": {"self": "%s/contents/%s" % (prefix, entry["path"]),
                   "git": "%s/git/trees/%s" % (prefix, entry["sha"]),
                   "html": "%s/%s/%s/tree/main/%s"
                           % (base, repo.owner, repo.name, entry["path"])},
    }


# -- entry point ----------------------------------------------------------

def handle_rest(store, method, path, query, headers, body):
    """-> (status, response_headers, body_bytes). See module docstring."""
    query = query or {}
    base = _request_base(headers)
    raw = (path or "/").split("?", 1)[0]
    if not raw.startswith("/api/v3"):
        return _reply_not_found()
    rest = raw[len("/api/v3"):]
    segments = [unquote(s) for s in rest.strip("/").split("/") if s != ""]
    try:
        return _route_request(store, method, segments, query, headers, body, base)
    except StoreError as e:
        return _reply_error(e.status, str(e))
    except Exception as e:  # never leak a stack trace / HTML to the suite
        return _reply_error(500, "Internal error: %s" % e)


def _route_request(store, method, seg, query, headers, body, base):
    # GET /api/v3/ (root)
    if not seg:
        if method != "GET":
            return _reply_not_found()
        root = {
            "current_user_url": base + "/api/v3/user",
            "current_user_authorizations_html_url":
                base + "/api/v3/authorizations",
            "authorizations_url": base + "/api/v3/authorizations",
            "code_search_url": base + "/api/v3/search/code{?q}",
            "commit_search_url": base + "/api/v3/search/commits{?q}",
            "emails_url": base + "/api/v3/user/emails",
            "emojis_url": base + "/api/v3/emojis",
            "events_url": base + "/api/v3/events",
            "feeds_url": base + "/api/v3/feeds",
            "followers_url": base + "/api/v3/user/followers",
            "following_url": base + "/api/v3/user/following{/target}",
            "gists_url": base + "/api/v3/gists{/gist_id}",
            "hub_url": base + "/api/v3/hub",
            "issue_search_url": base + "/api/v3/search/issues{?q}",
            "issues_url": base + "/api/v3/issues",
            "keys_url": base + "/api/v3/user/keys",
            "labels_url": base + "/api/v3/repos/{owner}/{repo}/labels{/name}",
            "notifications_url": base + "/api/v3/notifications",
            "organizations_url": base + "/api/v3/user/orgs",
            "public_gists_url": base + "/api/v3/gists/public",
            "rate_limit_url": base + "/api/v3/rate_limit",
            "repository_url": base + "/api/v3/repos/{owner}/{repo}",
            "repository_search_url": base + "/api/v3/search/repositories{?q}",
            "current_user_repositories_url":
                base + "/api/v3/user/repos{?type,page,per_page,sort}",
            "starred_url": base + "/api/v3/user/starred{/owner}{/repo}",
            "starred_gists_url": base + "/api/v3/user/starred",
            "topic_search_url": base + "/api/v3/search/topics{?q}",
            "user_url": base + "/api/v3/users/{user}",
            "user_organizations_url": base + "/api/v3/users/{user}/orgs",
            "user_repositories_url": base + "/api/v3/users/{user}/repos{?type,page,per_page,sort}",
            "user_search_url": base + "/api/v3/search/users{?q}",
        }
        return _reply(200, root, {"X-OAuth-Scopes": "repo, read:org"})

    # GET /api/v3/meta
    if seg == ["meta"]:
        if method != "GET":
            return _reply_not_found()
        return _reply(200, {"installed_version": FORGE_VERSION,
                            "verifiable_password_authentication": True})

    # /api/v3/user/repos
    if seg[:2] == ["user", "repos"] and len(seg) == 2:
        if method == "GET":
            repos = store.list_repos(store.owner_login)
            items = [_repo_shape(r, base) for r in repos]
            page_items, extra = _page_items(items, query, "/api/v3/user/repos",
                                            headers)
            return _reply(200, page_items, extra)
        if method == "POST":
            obj, err = _read_json_body(body)
            if err is not None:
                return err
            name = obj.get("name")
            if not isinstance(name, str) or not name:
                return _reply_error(422, "name is required")
            try:
                repo = store.create_repo(
                    store.owner_login, name,
                    description=obj.get("description") or "",
                    private=bool(obj.get("private", False)))
            except RepoExistsError as e:
                return _reply_error(422, str(e))
            return _reply(201, _repo_shape(repo, base))
        return _reply_not_found()

    # /api/v3/repos/{owner}/{repo}[...]
    if seg[0] == "repos" and len(seg) >= 3:
        owner, name = seg[1], seg[2]
        if owner != store.owner_login:
            return _reply_not_found()
        repo = store.get_repo(owner, name)
        tail = seg[3:]
        if repo is None:
            # Any sub-resource of a missing repo is also 404.
            return _reply_not_found()
        return _repo_endpoints(store, repo, tail, method, query, headers, body,
                               base, "/api/v3/repos/%s/%s" % (owner, name))

    return _reply_not_found()


def _repo_endpoints(store, repo, tail, method, query, headers, body, base,
                    repo_path):
    # /repos/{o}/{r} itself
    if not tail:
        if method == "GET":
            return _reply(200, _repo_shape(repo, base))
        if method == "PATCH":
            obj, err = _read_json_body(body)
            if err is not None:
                return err
            if "description" in obj:
                repo.description = obj.get("description") or ""
            if "private" in obj:
                repo.private = bool(obj.get("private"))
            if isinstance(obj.get("name"), str) and obj["name"] \
                    and obj["name"] != repo.name:
                new_name = obj["name"]
                if store.get_repo(repo.owner, new_name) is not None:
                    return _reply_error(422, "Repository already exists: %s/%s"
                                        % (repo.owner, new_name))
                del store.repos[(repo.owner, repo.name)]
                repo.name = new_name
                store.repos[(repo.owner, new_name)] = repo
            return _reply(200, _repo_shape(repo, base))
        if method == "DELETE":
            store.delete_repo(repo.owner, repo.name)
            return _reply(204, None)
        return _reply_not_found()

    kind = tail[0]

    # -- git refs -------------------------------------------------------
    if kind == "git" and len(tail) >= 2 and tail[1] == "refs":
        ref_parts = tail[2:]
        if method == "POST" and not ref_parts:
            obj, err = _read_json_body(body)
            if err is not None:
                return err
            ref = obj.get("ref")
            sha = obj.get("sha")
            if not isinstance(ref, str) or not ref.startswith("refs/"):
                return _reply_error(422, "ref must start with refs/")
            if not isinstance(sha, str) or not sha:
                return _reply_error(422, "sha is required")
            try:
                repo.create_ref(ref, sha)
            except (RefExistsError, RefNotFoundError) as e:
                return _reply_error(e.status, str(e))
            return _reply(201, {"ref": ref,
                                "object": {"sha": sha, "type": "commit"},
                                "url": "%s/api/v3/repos/%s/%s/git/refs/%s"
                                       % (base, repo.owner, repo.name, ref)})
        if method == "GET":
            if not ref_parts:
                refs = [{"ref": r, "object": {"sha": s, "type": "commit"}}
                        for r, s in sorted(repo.refs.items())]
                return _reply(200, refs)
            ref = "/".join(ref_parts)
            if ref in repo.refs:
                return _reply(200, {"ref": ref, "object": {
                    "sha": repo.refs[ref], "type": "commit"}})
            return _reply_not_found()
        if method == "DELETE" and ref_parts:
            ref = "/".join(ref_parts)
            if ref == repo.branch_ref(repo.default_branch):
                return _reply_error(422, "Cannot delete the default branch")
            try:
                repo.delete_ref(ref)
            except RefNotFoundError as e:
                return _reply_error(404, str(e))
            return _reply(204, None)
        return _reply_not_found()

    # -- branches -------------------------------------------------------
    if kind == "branches" and len(tail) == 1:
        if method != "GET":
            return _reply_not_found()
        out = [{"name": n,
                "commit": {"sha": s,
                           "url": "%s/api/v3/repos/%s/%s/commits/%s"
                                  % (base, repo.owner, repo.name, s)},
                "protected": False}
               for n, s in repo.list_branches()]
        return _reply(200, out)

    # -- commits --------------------------------------------------------
    if kind == "commits" and len(tail) == 1:
        if method != "GET":
            return _reply_not_found()
        ref = query.get("sha") or query.get("ref")
        tip = repo.resolve_ref(ref) if ref else repo.branch_tip(repo.default_branch)
        if tip is None:
            # Unborn branch: no commits yet. Unknown ref: 404.
            if ref and repo.resolve_ref(ref) is None:
                return _reply_not_found()
            return _reply(200, [])
        items = []
        for sha, message in repo.list_commits(tip):
            obj = repo.objects.get(sha)
            parents = parse_commit(obj[1])["parents"] if obj else []
            items.append({
                "sha": sha,
                "commit": {
                    "message": message,
                    "author": {"name": "Forge", "email": "forge@make.fun",
                               "date": FIXED_TS},
                    "committer": {"name": "Forge", "email": "forge@make.fun",
                                  "date": FIXED_TS},
                    "tree": {"sha": repo.objects.commit_tree(sha)},
                    "comment_count": 0,
                },
                "url": "%s/api/v3/repos/%s/%s/commits/%s"
                       % (base, repo.owner, repo.name, sha),
                "html_url": "%s/%s/%s/commit/%s"
                            % (base, repo.owner, repo.name, sha),
                "parents": [{"sha": p} for p in parents],
            })
        page_items, extra = _page_items(
            items, query, repo_path + "/commits", headers)
        return _reply(200, page_items, extra)

    # -- contents -------------------------------------------------------
    if kind == "contents":
        content_path = "/".join(tail[1:])
        if method == "GET":
            ref = query.get("ref")
            data, blob_sha = repo.get_content(content_path, ref or "main") \
                if content_path else (None, None)
            if data is not None:
                return _reply(200, _file_shape(repo, content_path,
                                               data, blob_sha, base))
            entries = repo.list_dir(content_path, ref or "main")
            if entries is None:
                return _reply_not_found()
            return _reply(200, [_dir_shape(repo, e, base)
                                for e in entries])
        if method == "PUT":
            obj, err = _read_json_body(body)
            if err is not None:
                return err
            message = obj.get("message")
            if not isinstance(message, str):
                return _reply_error(422, "message is required")
            content_b64 = obj.get("content")
            if not isinstance(content_b64, str):
                return _reply_error(422, "content is required")
            try:
                raw = base64.b64decode(content_b64, validate=True)
            except (binascii.Error, ValueError):
                return _reply_error(422, "content is not valid base64")
            branch = obj.get("branch") or "main"
            sha = obj.get("sha")
            existing, _ = repo.get_content(content_path, branch)
            if sha is not None:
                if existing is None:
                    return _reply_error(422, "sha does not match any existing file")
                if repo.get_content(content_path, branch)[1] != sha:
                    return _reply_error(409, "sha does not match the current blob")
            elif existing is not None:
                return _reply_error(422, "sha is required to update a file")
            try:
                commit_sha, blob_sha = repo.put_content(
                    content_path, raw, message, branch=branch,
                    expected_sha=sha)
            except StaleShaError as e:
                return _reply_error(409, str(e))
            except RepoNotFoundError as e:
                return _reply_error(422, str(e))
            status = 200 if existing is not None else 201
            return _reply(status, {
                "content": _file_shape(repo, content_path, raw,
                                       blob_sha, base),
                "commit": {"sha": commit_sha, "message": message,
                           "author": {"name": "Forge",
                                      "email": "forge@make.fun",
                                      "date": FIXED_TS},
                           "committer": {"name": "Forge",
                                         "email": "forge@make.fun",
                                         "date": FIXED_TS},
                           "tree": {"sha": repo.objects.commit_tree(commit_sha)}},
            })
        if method == "DELETE":
            obj, err = _read_json_body(body)
            if err is not None:
                return err
            message = obj.get("message") or ""
            sha = obj.get("sha")
            branch = obj.get("branch") or "main"
            try:
                commit_sha = repo.delete_content(content_path, message,
                                                 branch=branch,
                                                 expected_sha=sha)
            except RepoNotFoundError as e:
                return _reply_error(404, str(e))
            except StaleShaError as e:
                return _reply_error(409, str(e))
            return _reply(200, {"commit": {"sha": commit_sha,
                                           "message": message}})
        return _reply_not_found()

    # -- labels ---------------------------------------------------------
    if kind == "labels":
        if len(tail) == 1:
            if method == "GET":
                return _reply(200, [_label_shape(repo, l, base)
                                    for l in sorted(repo.labels.values(),
                                                    key=lambda x: x.name)])
            if method == "POST":
                obj, err = _read_json_body(body)
                if err is not None:
                    return err
                name = obj.get("name")
                if not isinstance(name, str) or not name:
                    return _reply_error(422, "name is required")
                try:
                    label = repo.create_label(
                        name, color=obj.get("color") or "ededed",
                        description=obj.get("description") or "")
                except RepoExistsError as e:
                    return _reply_error(422, str(e))
                return _reply(201, _label_shape(repo, label, base))
            return _reply_not_found()
        if len(tail) == 2 and method == "GET":
            label = repo.get_label(tail[1])
            if label is None:
                return _reply_not_found()
            return _reply(200, _label_shape(repo, label, base))
        return _reply_not_found()

    # -- issues ---------------------------------------------------------
    if kind == "issues":
        if len(tail) == 1:
            if method == "GET":
                state = query.get("state") or "open"
                if state not in ("open", "closed", "all"):
                    state = "open"
                issues = repo.list_issues(
                    None if state == "all" else state)
                if query.get("sort") == "created":
                    reverse = (query.get("direction") or "desc") == "desc"
                    issues = sorted(issues, key=lambda i: i.number,
                                    reverse=reverse)
                items = [_issue_shape(repo, i, base) for i in issues]
                page_items, extra = _page_items(
                    items, query, repo_path + "/issues", headers)
                return _reply(200, page_items, extra)
            if method == "POST":
                obj, err = _read_json_body(body)
                if err is not None:
                    return err
                title = obj.get("title")
                if not isinstance(title, str) or not title:
                    return _reply_error(422, "title is required")
                try:
                    issue = repo.create_issue(
                        title, body=obj.get("body") or "",
                        labels=obj.get("labels") or [])
                except LabelNotFoundError as e:
                    return _reply_error(422, str(e))
                return _reply(201, _issue_shape(repo, issue, base))
            return _reply_not_found()
        if len(tail) >= 2:
            try:
                number = int(tail[1])
            except ValueError:
                return _reply_not_found()
            issue = repo.get_issue(number)
            if len(tail) == 2:
                if issue is None:
                    return _reply_not_found()
                if method == "GET":
                    return _reply(200, _issue_shape(repo, issue, base))
                if method == "PATCH":
                    obj, err = _read_json_body(body)
                    if err is not None:
                        return err
                    if "title" in obj and isinstance(obj["title"], str):
                        issue.title = obj["title"]
                    if "body" in obj:
                        issue.body = obj.get("body") or ""
                    if "state" in obj:
                        if obj["state"] not in ("open", "closed"):
                            return _reply_error(422, "state must be open or closed")
                        issue.state = obj["state"]
                    if "labels" in obj:
                        names = []
                        for n in obj.get("labels") or []:
                            if isinstance(n, dict):
                                n = n.get("name")
                            if n not in repo.labels:
                                return _reply_error(422, "Label does not exist: %s" % n)
                            names.append(n)
                        issue.labels = names
                    return _reply(200, _issue_shape(repo, issue, base))
                return _reply_not_found()
            if len(tail) == 3 and tail[2] == "comments":
                if issue is None:
                    return _reply_not_found()
                if method == "GET":
                    return _reply(200, [_comment_shape(repo, c, base)
                                        for c in issue.comments])
                if method == "POST":
                    obj, err = _read_json_body(body)
                    if err is not None:
                        return err
                    cbody = obj.get("body")
                    if not isinstance(cbody, str) or not cbody:
                        return _reply_error(422, "body is required")
                    comment = Comment(repo, number, cbody, repo.owner)
                    issue.comments.append(comment)
                    return _reply(201, _comment_shape(repo, comment, base))
                return _reply_not_found()
        return _reply_not_found()

    # -- pulls ----------------------------------------------------------
    if kind == "pulls":
        if len(tail) == 1:
            if method == "GET":
                state = query.get("state") or "open"
                if state not in ("open", "closed", "all"):
                    state = "open"
                pulls = repo.list_pulls(None if state == "all" else state)
                items = [_pull_shape(repo, p, base) for p in pulls]
                page_items, extra = _page_items(
                    items, query, repo_path + "/pulls", headers)
                return _reply(200, page_items, extra)
            if method == "POST":
                obj, err = _read_json_body(body)
                if err is not None:
                    return err
                title = obj.get("title")
                head = obj.get("head")
                base_branch = obj.get("base")
                if not isinstance(title, str) or not title:
                    return _reply_error(422, "title is required")
                if not isinstance(head, str) or not head:
                    return _reply_error(422, "head is required")
                if not isinstance(base_branch, str) or not base_branch:
                    return _reply_error(422, "base is required")
                try:
                    pull = repo.create_pull(title, head, base_branch,
                                            body=obj.get("body") or "")
                except RefNotFoundError as e:
                    return _reply_error(422, str(e))
                return _reply(201, _pull_shape(repo, pull, base))
            return _reply_not_found()
        if len(tail) >= 2:
            try:
                number = int(tail[1])
            except ValueError:
                return _reply_not_found()
            pull = repo.get_pull(number)
            if len(tail) == 2:
                if pull is None:
                    return _reply_not_found()
                if method == "GET":
                    return _reply(200, _pull_shape(repo, pull, base))
                return _reply_not_found()
            if len(tail) == 3 and tail[2] == "files":
                if pull is None:
                    return _reply_not_found()
                if method != "GET":
                    return _reply_not_found()
                files = repo.pull_files(pull)
                out = []
                for f in files:
                    out.append({
                        "sha": f["sha"], "filename": f["filename"],
                        "status": f["status"],
                        "additions": f["additions"],
                        "deletions": f["deletions"],
                        "changes": f["changes"],
                        "blob_url": "%s/api/v3/repos/%s/%s/git/blobs/%s"
                                    % (base, repo.owner, repo.name, f["sha"]),
                        "raw_url": "%s/api/v3/repos/%s/%s/contents/%s"
                                   % (base, repo.owner, repo.name, f["filename"]),
                        "contents_url": "%s/api/v3/repos/%s/%s/contents/%s"
                                        % (base, repo.owner, repo.name, f["filename"]),
                    })
                return _reply(200, out)
            if len(tail) == 3 and tail[2] == "merge":
                if method != "PUT":
                    return _reply_not_found()
                if pull is None:
                    return _reply_not_found()
                _obj, err = _read_json_body(body)
                if err is not None:
                    return err
                try:
                    sha, message = repo.merge_pull(number)
                except RepoNotFoundError as e:
                    return _reply_error(404, str(e))
                except ConflictError as e:
                    return _reply_error(405, str(e))
                return _reply(200, {"sha": sha, "merged": True,
                                    "message": message})
        return _reply_not_found()

    return _reply_not_found()
