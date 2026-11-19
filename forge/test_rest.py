"""Tests for forge.store + forge.rest.

Run from the project root:  python3 -m forge.test_rest
"""

import base64
import json
import unittest

from forge.store import Store
from forge.rest import handle_rest

DEFAULT_HEADERS = {"Host": "127.0.0.1:18080"}


def rest_call(store, method, path, body=None, query=None, headers=None):
    raw = b"" if body is None else (
        body if isinstance(body, bytes) else json.dumps(body).encode("utf-8"))
    status, hdrs, out = handle_rest(store, method, path, query or {},
                                    headers or DEFAULT_HEADERS, raw)
    parsed = None
    if out:
        parsed = json.loads(out.decode("utf-8"))
    return status, hdrs, parsed


def encode_b64(text):
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


class RestTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(owner_login="adaiversen")
        self.owner = "adaiversen"

    # -- helpers ------------------------------------------------------
    def create_repo(self, name="demo", **kw):
        status, _h, body = rest_call(self.store, "POST", "/api/v3/user/repos",
                                     {"name": name, "private": True,
                                      "description": "d1", **kw})
        self.assertEqual(status, 201, body)
        return body

    def write_file(self, repo, path, content, branch=None, sha=None,
                   message="seeded commit"):
        payload = {"message": message, "content": encode_b64(content)}
        if branch:
            payload["branch"] = branch
        if sha:
            payload["sha"] = sha
        return rest_call(self.store, "PUT",
                         "/api/v3/repos/%s/%s/contents/%s" % (self.owner, repo, path),
                         payload)

    def seed_repo_file(self, name="demo"):
        self.create_repo(name)
        status, _h, body = self.write_file(name, "docs/readme.md", "hello world\n")
        self.assertEqual(status, 201, body)
        return body["commit"]["sha"]

    # -- root / meta --------------------------------------------------
    def test_root_and_meta_endpoints(self):
        status, hdrs, body = rest_call(self.store, "GET", "/api/v3/")
        self.assertEqual(status, 200)
        self.assertIn("current_user_url", body)
        self.assertIn("rate_limit_url", body)
        self.assertEqual(hdrs.get("X-OAuth-Scopes"), "repo, read:org")
        self.assertTrue(hdrs.get("X-GitHub-Request-Id"))
        status, _h, body = rest_call(self.store, "GET", "/api/v3/meta")
        self.assertEqual(status, 200)
        self.assertTrue(body["installed_version"])

    # -- repos --------------------------------------------------------
    def test_repo_lifecycle(self):
        repo = self.create_repo()
        self.assertEqual(repo["name"], "demo")
        self.assertTrue(repo["private"])
        self.assertEqual(repo["default_branch"], "main")
        self.assertEqual(repo["owner"]["login"], self.owner)
        self.assertEqual(repo["created_at"], "2026-01-01T00:00:00Z")
        self.assertTrue(repo["node_id"])
        status, _h, body = rest_call(self.store, "GET", "/api/v3/repos/%s/demo" % self.owner)
        self.assertEqual(status, 200)
        self.assertEqual(body["description"], "d1")
        status, _h, body = rest_call(self.store, "PATCH", "/api/v3/repos/%s/demo" % self.owner,
                                     {"description": "d2"})
        self.assertEqual(status, 200)
        self.assertEqual(body["description"], "d2")
        status, _h, body = rest_call(self.store, "GET", "/api/v3/repos/%s/demo" % self.owner)
        self.assertEqual(body["description"], "d2")
        # duplicate create -> 422
        status, _h, body = rest_call(self.store, "POST", "/api/v3/user/repos",
                                     {"name": "demo"})
        self.assertEqual(status, 422)
        self.assertIn("message", body)
        # delete -> 204, then 404
        status, _h, body = rest_call(self.store, "DELETE", "/api/v3/repos/%s/demo" % self.owner)
        self.assertEqual(status, 204)
        self.assertIsNone(body)
        status, _h, body = rest_call(self.store, "GET", "/api/v3/repos/%s/demo" % self.owner)
        self.assertEqual(status, 404)
        self.assertEqual(body, {"message": "Not Found"})

    def test_foreign_owner_404(self):
        self.create_repo()
        status, _h, body = rest_call(self.store, "GET", "/api/v3/repos/stranger/demo")
        self.assertEqual(status, 404)
        status, _h, body = rest_call(self.store, "GET", "/api/v3/repos/%s/nope" % self.owner)
        self.assertEqual(status, 404)

    def test_user_repos_pagination(self):
        for i in range(12):
            self.create_repo("repo-%02d" % i)
        status, hdrs, body = rest_call(self.store, "GET", "/api/v3/user/repos",
                                       query={"per_page": "10"})
        self.assertEqual(status, 200)
        self.assertEqual(len(body), 10)
        self.assertIn('rel="next"', hdrs.get("Link", ""))
        self.assertIn("page=2", hdrs["Link"])
        status, _h, body = rest_call(self.store, "GET", "/api/v3/user/repos",
                                     query={"per_page": "10", "page": "2"})
        self.assertEqual(len(body), 2)
        names = [r["name"] for r in body]
        self.assertEqual(names, sorted(names))

    # -- contents -----------------------------------------------------
    def test_contents_flow(self):
        self.seed_repo_file()
        path = "/api/v3/repos/%s/demo/contents/docs/readme.md" % self.owner
        status, _h, body = rest_call(self.store, "GET", path)
        self.assertEqual(status, 200)
        self.assertEqual(body["encoding"], "base64")
        self.assertEqual(base64.b64decode(body["content"]).decode(), "hello world\n")
        self.assertEqual(body["size"], len("hello world\n"))
        sha = body["sha"]
        # update with current sha -> 200
        status, _h, body = self.write_file("demo", "docs/readme.md", "hello v2\n",
                                           sha=sha)
        self.assertEqual(status, 200, body)
        new_sha = body["content"]["sha"]
        self.assertNotEqual(new_sha, sha)
        # stale sha -> 409
        status, _h, body = self.write_file("demo", "docs/readme.md", "hello v3\n",
                                           sha=sha)
        self.assertEqual(status, 409)
        # directory listing
        status, _h, body = rest_call(self.store, "GET",
                                     "/api/v3/repos/%s/demo/contents/docs" % self.owner)
        self.assertEqual(status, 200)
        self.assertIsInstance(body, list)
        self.assertEqual([e["name"] for e in body], ["readme.md"])
        # delete -> 200, then GET 404
        status, _h, body = rest_call(self.store, "DELETE", path,
                                     {"message": "rm", "sha": new_sha})
        self.assertEqual(status, 200)
        self.assertTrue(body["commit"]["sha"])
        status, _h, body = rest_call(self.store, "GET", path)
        self.assertEqual(status, 404)

    # -- branches / commits -------------------------------------------
    def test_branches_and_commits_list(self):
        tip = self.seed_repo_file()
        repo = self.store.get_repo(self.owner, "demo")
        status, _h, body = rest_call(self.store, "POST",
                                     "/api/v3/repos/%s/demo/git/refs" % self.owner,
                                     {"ref": "refs/heads/feature", "sha": tip})
        self.assertEqual(status, 201, body)
        self.assertEqual(body["ref"], "refs/heads/feature")
        self.assertEqual(body["object"]["sha"], tip)
        status, _h, body = rest_call(self.store, "GET",
                                     "/api/v3/repos/%s/demo/branches" % self.owner)
        self.assertEqual(status, 200)
        names = {b["name"]: b["commit"]["sha"] for b in body}
        self.assertEqual(names, {"main": tip, "feature": tip})
        status, _h, body = rest_call(self.store, "GET",
                                     "/api/v3/repos/%s/demo/commits" % self.owner,
                                     query={"sha": "feature"})
        self.assertEqual(status, 200)
        self.assertEqual([c["sha"] for c in body], [tip])
        self.assertEqual(body[0]["commit"]["message"], "seeded commit")

    # -- labels / issues ----------------------------------------------
    def test_labels_and_issues_flow(self):
        self.seed_repo_file()
        base = "/api/v3/repos/%s/demo" % self.owner
        status, _h, body = rest_call(self.store, "POST", base + "/labels",
                                     {"name": "bug", "color": "ededed"})
        self.assertEqual(status, 201)
        self.assertEqual(body["name"], "bug")
        status, _h, body = rest_call(self.store, "GET", base + "/labels")
        self.assertEqual([l["name"] for l in body], ["bug"])
        # unknown label -> 422
        status, _h, body = rest_call(self.store, "POST", base + "/issues",
                                     {"title": "t", "body": "b",
                                      "labels": ["nope"]})
        self.assertEqual(status, 422)
        # create two issues, numbers increment
        status, _h, first = rest_call(self.store, "POST", base + "/issues",
                                      {"title": "first", "body": "b1",
                                       "labels": ["bug"]})
        self.assertEqual(status, 201, first)
        self.assertEqual(first["state"], "open")
        self.assertEqual(first["labels"][0]["name"], "bug")
        status, _h, second = rest_call(self.store, "POST", base + "/issues",
                                       {"title": "second", "body": "b2"})
        self.assertEqual(second["number"], first["number"] + 1)
        # list + sort
        status, _h, body = rest_call(self.store, "GET", base + "/issues",
                                     query={"state": "open", "sort": "created",
                                            "direction": "desc", "per_page": "100"})
        self.assertEqual([i["number"] for i in body],
                         [second["number"], first["number"]])
        status, _h, body = rest_call(self.store, "GET", base + "/issues",
                                     query={"state": "open", "sort": "created",
                                            "direction": "asc"})
        self.assertEqual([i["number"] for i in body],
                         [first["number"], second["number"]])
        # get one
        status, _h, body = rest_call(self.store, "GET",
                                     "%s/issues/%d" % (base, first["number"]))
        self.assertEqual(status, 200)
        self.assertEqual(body["title"], "first")
        # patch close persists
        status, _h, body = rest_call(self.store, "PATCH",
                                     "%s/issues/%d" % (base, first["number"]),
                                     {"state": "closed"})
        self.assertEqual(status, 200)
        self.assertEqual(body["state"], "closed")
        status, _h, body = rest_call(self.store, "GET",
                                     "%s/issues/%d" % (base, first["number"]))
        self.assertEqual(body["state"], "closed")
        # open list omits the closed one
        status, _h, body = rest_call(self.store, "GET", base + "/issues",
                                     query={"state": "open"})
        self.assertEqual([i["number"] for i in body], [second["number"]])
        # comments
        status, _h, body = rest_call(self.store, "POST",
                                     "%s/issues/%d/comments" % (base, second["number"]),
                                     {"body": "hello comment"})
        self.assertEqual(status, 201)
        self.assertTrue(isinstance(body["id"], int) and body["id"] >= 1)
        cid = body["id"]
        status, _h, body = rest_call(self.store, "GET",
                                     "%s/issues/%d/comments" % (base, second["number"]))
        self.assertEqual(status, 200)
        self.assertTrue(any(c["id"] == cid and c["body"] == "hello comment"
                            for c in body))
        # unknown issue -> 404
        status, _h, body = rest_call(self.store, "GET", base + "/issues/999")
        self.assertEqual(status, 404)

    # -- pulls / merge -------------------------------------------------
    def _make_pull(self, name="demo"):
        tip = self.seed_repo_file(name)
        base = "/api/v3/repos/%s/%s" % (self.owner, name)
        rest_call(self.store, "POST", base + "/git/refs",
                  {"ref": "refs/heads/feature", "sha": tip})
        status, _h, body = self.write_file(name, "docs/extra.md", "new file\n",
                                           branch="feature")
        self.assertEqual(status, 201, body)
        head_sha = body["commit"]["sha"]
        status, _h, pull = rest_call(self.store, "POST", base + "/pulls",
                                     {"title": "add extra", "body": "pb",
                                      "head": "feature", "base": "main"})
        self.assertEqual(status, 201, pull)
        return base, pull, head_sha, tip

    def test_pull_files_and_merge(self):
        base, pull, head_sha, tip = self._make_pull()
        self.assertEqual(pull["state"], "open")
        self.assertEqual(pull["head"]["ref"], "feature")
        self.assertEqual(pull["head"]["sha"], head_sha)
        self.assertEqual(pull["base"]["ref"], "main")
        self.assertEqual(pull["base"]["sha"], tip)
        status, _h, body = rest_call(self.store, "GET",
                                     "%s/pulls/%d" % (base, pull["number"]))
        self.assertEqual(status, 200)
        self.assertEqual(body["title"], "add extra")
        # files
        status, _h, body = rest_call(self.store, "GET",
                                     "%s/pulls/%d/files" % (base, pull["number"]))
        self.assertEqual(status, 200)
        self.assertEqual(len(body), 1)
        self.assertEqual(body[0]["filename"], "docs/extra.md")
        self.assertEqual(body[0]["status"], "added")
        self.assertGreaterEqual(body[0]["additions"], 1)
        self.assertEqual(body[0]["changes"],
                         body[0]["additions"] + body[0]["deletions"])
        # merge (squash)
        status, _h, body = rest_call(self.store, "PUT",
                                     "%s/pulls/%d/merge" % (base, pull["number"]),
                                     {"merge_method": "squash"})
        self.assertEqual(status, 200, body)
        self.assertTrue(body["merged"])
        self.assertTrue(body["sha"])
        merge_sha = body["sha"]
        self.assertEqual(body["message"],
                         "Merge pull request #%d" % pull["number"])
        # pull is closed + merged, merge_commit_sha matches
        status, _h, body = rest_call(self.store, "GET",
                                     "%s/pulls/%d" % (base, pull["number"]))
        self.assertEqual(body["state"], "closed")
        self.assertTrue(body["merged"])
        self.assertTrue(body["merged_at"])
        self.assertEqual(body["merge_commit_sha"], merge_sha)
        # squash commit is on the default branch, with the head content
        repo = self.store.get_repo(self.owner, "demo")
        self.assertEqual(repo.branch_tip("main"), merge_sha)
        data, _sha = repo.get_content("docs/extra.md", "main")
        self.assertEqual(data, b"new file\n")
        # merge commit appears in default-branch commits, not before merge
        status, _h, body = rest_call(self.store, "GET", base + "/commits",
                                     query={"sha": "main"})
        self.assertTrue(any(c["sha"] == merge_sha for c in body))
        # list: default open omits merged; state=all includes it
        status, _h, body = rest_call(self.store, "GET", base + "/pulls")
        self.assertEqual(body, [])
        status, _h, body = rest_call(self.store, "GET", base + "/pulls",
                                     query={"state": "all"})
        self.assertEqual([p["number"] for p in body], [pull["number"]])
        # unknown pull -> 404 (get and merge)
        status, _h, body = rest_call(self.store, "GET", base + "/pulls/999")
        self.assertEqual(status, 404)
        status, _h, body = rest_call(self.store, "PUT", base + "/pulls/999/merge",
                                     {"merge_method": "squash"})
        self.assertEqual(status, 404)

    def test_merge_conflict_returns_405(self):
        tip = self.seed_repo_file()
        base = "/api/v3/repos/%s/demo" % self.owner
        for branch in ("a", "b"):
            rest_call(self.store, "POST", base + "/git/refs",
                      {"ref": "refs/heads/%s" % branch, "sha": tip})
            status, _h, body = rest_call(self.store, "GET",
                                         base + "/contents/docs/readme.md")
            self.write_file("demo", "docs/readme.md", "version %s\n" % branch,
                            branch=branch, sha=body["sha"])
        status, _h, pa = rest_call(self.store, "POST", base + "/pulls",
                                   {"title": "pa", "head": "a", "base": "main"})
        status, _h, pb = rest_call(self.store, "POST", base + "/pulls",
                                   {"title": "pb", "head": "b", "base": "main"})
        status, _h, body = rest_call(self.store, "PUT",
                                     "%s/pulls/%d/merge" % (base, pa["number"]),
                                     {"merge_method": "squash"})
        self.assertEqual(status, 200, body)
        status, _h, body = rest_call(self.store, "PUT",
                                     "%s/pulls/%d/merge" % (base, pb["number"]),
                                     {"merge_method": "squash"})
        self.assertEqual(status, 405, body)
        self.assertIn("message", body)

    # -- protocol ------------------------------------------------------
    def test_unknown_route_and_bad_json(self):
        status, hdrs, body = rest_call(self.store, "GET", "/api/v3/no-such-route")
        self.assertEqual(status, 404)
        self.assertEqual(body, {"message": "Not Found"})
        self.assertTrue(hdrs.get("X-GitHub-Request-Id"))
        status, _h, body = rest_call(self.store, "POST", "/api/v3/user/repos",
                                     body=b'{"name":')
        self.assertEqual(status, 400)
        self.assertIn("message", body)
        # every reply carries a request id, including errors
        status, hdrs, body = rest_call(self.store, "GET",
                                       "/api/v3/repos/%s/nope" % self.owner)
        self.assertTrue(hdrs.get("X-GitHub-Request-Id"))

    def test_ids_are_deterministic(self):
        # Two identical runs produce identical ids/shas (replay rule).
        def run():
            s = Store(owner_login="adaiversen")
            rest_call(s, "POST", "/api/v3/user/repos", {"name": "r"})
            _st, _h, b = rest_call(s, "PUT", "/api/v3/repos/adaiversen/r/contents/a.txt",
                                  {"message": "m", "content": encode_b64("x\n")})
            return b["commit"]["sha"], b["content"]["sha"]
        self.assertEqual(run(), run())


if __name__ == "__main__":
    unittest.main(verbosity=2)
