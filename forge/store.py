"""In-memory deterministic store for the forge.

Standard library only. No timestamps, no random ids, no wall clock anywhere
in stored state. All counters are plain integers starting at 1, handed out
in creation order. Every ``created_at``/``updated_at``/``merged_at``/
``closed_at`` the REST layer emits is the fixed string FIXED_TS
("2026-01-01T00:00:00Z").

ID scheme (deterministic, documented for the GraphQL/MCP workers):
  * Repo:   ``id`` = store-wide integer database id (creation order, from 1);
            ``node_id`` = base64("Repository:<id>").
  * Issue:  ``id`` = store-wide integer database id (creation order, from 1);
            ``node_id`` = base64("Issue:<id>").
  * Pull:   ``id`` = store-wide integer database id (creation order, from 1);
            ``node_id`` = base64("PullRequest:<id>").
  * Label:  ``id`` = per-repo integer (creation order, from 1);
            ``node_id`` = base64("Label:<repo_id>:<name>").
  * Comment: ``id`` = per-repo integer (creation order, from 1);
            ``node_id`` = base64("IssueComment:<repo_id>:<issue_number>:<id>").
  * Issue/Pull ``number`` is the per-repo counter (from 1), as on GitHub.

The git object layer lives in forge.gitcore (ObjectStore); a Repo owns one.
"""

import base64
import difflib

try:
    from .gitcore import ObjectStore, ancestors, parse_commit
except ImportError:  # running as a top-level package dir, not `forge.*`
    from gitcore import ObjectStore, ancestors, parse_commit

FIXED_TS = "2026-01-01T00:00:00Z"


class StoreError(Exception):
    """Base class; carries the HTTP status the REST layer maps it to."""

    status = 400


class RepoExistsError(StoreError):
    status = 422


class RepoNotFoundError(StoreError):
    status = 404


class StaleShaError(StoreError):
    status = 409


class LabelNotFoundError(StoreError):
    status = 422


class ConflictError(StoreError):
    status = 405


class RefExistsError(StoreError):
    status = 422


class RefNotFoundError(StoreError):
    status = 404


def _encode_b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


class Label:
    def __init__(self, repo, name, color="ededed", description=""):
        self.repo = repo
        self.id = repo.next_label_id()
        self.name = name
        self.color = color
        self.description = description

    @property
    def node_id(self):
        return _encode_b64("Label:%d:%s" % (self.repo.database_id, self.name))

    def to_dict(self):
        return {"id": self.id, "node_id": self.node_id, "name": self.name,
                "color": self.color, "description": self.description,
                "default": False}


class Comment:
    def __init__(self, repo, issue_number, body, user_login):
        self.repo = repo
        self.issue_number = issue_number
        self.id = repo.next_comment_id()
        self.body = body
        self.user_login = user_login

    @property
    def node_id(self):
        return _encode_b64("IssueComment:%d:%d:%d"
                           % (self.repo.database_id, self.issue_number, self.id))


class Issue:
    def __init__(self, repo, number, db_id, title, body="", labels=None):
        self.repo = repo
        self.number = number
        self.id = db_id
        self.title = title
        self.body = body
        self.state = "open"
        self.labels = list(labels or [])  # label names, in creation order
        self.comments = []  # list[Comment]

    @property
    def node_id(self):
        return _encode_b64("Issue:%d" % self.id)


class Pull:
    def __init__(self, repo, number, db_id, title, head, base, head_sha,
                 base_sha, body=""):
        self.repo = repo
        self.number = number
        self.id = db_id
        self.title = title
        self.body = body
        self.state = "open"
        self.head = head  # branch name
        self.base = base  # branch name
        self.head_sha = head_sha
        self.base_sha = base_sha
        self.merged = False
        self.merge_commit_sha = None
        self.mergeable = True

    @property
    def node_id(self):
        return _encode_b64("PullRequest:%d" % self.id)


class Repo:
    def __init__(self, store, owner, name, database_id, description="",
                 private=False, default_branch="main"):
        self.store = store
        self.owner = owner
        self.name = name
        self.database_id = database_id
        self.description = description
        self.private = private
        self.default_branch = default_branch
        self.objects = ObjectStore()
        self.refs = {}  # "refs/heads/main" -> commit sha
        self.issues = {}  # number -> Issue
        self.pulls = {}  # number -> Pull
        self.labels = {}  # name -> Label
        self.issue_counter = 0
        self.pull_counter = 0
        self._label_counter = 0
        self._comment_counter = 0

    # -- identity ------------------------------------------------------
    @property
    def id(self):
        return self.database_id

    @property
    def node_id(self):
        return _encode_b64("Repository:%d" % self.database_id)

    @property
    def full_name(self):
        return "%s/%s" % (self.owner, self.name)

    def next_label_id(self):
        self._label_counter += 1
        return self._label_counter

    def next_comment_id(self):
        self._comment_counter += 1
        return self._comment_counter

    # -- refs ----------------------------------------------------------
    def branch_ref(self, branch):
        return "refs/heads/%s" % branch

    def branch_tip(self, branch):
        return self.refs.get(self.branch_ref(branch))

    def resolve_ref(self, ref):
        """ref: branch name, full ref, or commit sha -> commit sha | None."""
        if ref is None:
            return self.branch_tip(self.default_branch)
        if ref in self.refs:
            return self.refs[ref]
        branch_ref_name = self.branch_ref(ref)
        if branch_ref_name in self.refs:
            return self.refs[branch_ref_name]
        tag_ref = "refs/tags/%s" % ref
        if tag_ref in self.refs:
            return self.refs[tag_ref]
        found_obj = self.objects.get(ref)
        if found_obj is not None and found_obj[0] == "commit":
            return ref
        return None

    def create_ref(self, ref, sha):
        if ref in self.refs:
            raise RefExistsError("Reference already exists: %s" % ref)
        target_obj = self.objects.get(sha)
        if target_obj is None or target_obj[0] != "commit":
            raise RefNotFoundError("Object does not exist or is not a commit: %s" % sha)
        self.refs[ref] = sha
        return sha

    def delete_ref(self, ref):
        if ref not in self.refs:
            raise RefNotFoundError("Reference does not exist: %s" % ref)
        del self.refs[ref]

    def list_branches(self):
        branches = []
        for ref_name in sorted(self.refs):
            if ref_name.startswith("refs/heads/"):
                branches.append((ref_name[len("refs/heads/"):], self.refs[ref_name]))
        return branches

    def list_commits(self, tip):
        """[(sha, message)] reachable from tip, tip first. [] when unknown."""
        if tip is None:
            return []
        history = []
        for commit_sha in ancestors(self.objects, tip):
            entry = self.objects.get(commit_sha)
            history.append((commit_sha, parse_commit(entry[1])["message"].rstrip("\n")))
        return history

    def _commit_tree_map(self, tip_sha):
        root_tree = self.objects.commit_tree(tip_sha)
        if root_tree is None:
            return {}
        return self.objects.flatten_tree(root_tree)

    # -- labels --------------------------------------------------------
    def create_label(self, name, color="ededed", description=""):
        if name in self.labels:
            raise RepoExistsError("Label already exists: %s" % name)
        fresh_label = Label(self, name, color=color, description=description)
        self.labels[name] = fresh_label
        return fresh_label

    def get_label(self, name):
        return self.labels.get(name)

    # -- issues --------------------------------------------------------
    def create_issue(self, title, body="", labels=None):
        label_names = []
        for label_name in (labels or []):
            if isinstance(label_name, dict):
                label_name = label_name.get("name")
            if label_name not in self.labels:
                raise LabelNotFoundError("Label does not exist: %s" % label_name)
            label_names.append(label_name)
        self.issue_counter += 1
        issue_id = self.store.next_issue_id()
        fresh_issue = Issue(self, self.issue_counter, issue_id, title, body, label_names)
        self.issues[fresh_issue.number] = fresh_issue
        return fresh_issue

    def get_issue(self, number):
        return self.issues.get(number)

    def list_issues(self, state=None):
        visible = [item for item in self.issues.values()
                   if state in (None, "all") or item.state == state]
        visible.sort(key=lambda item: item.number)
        return visible

    # -- pulls ---------------------------------------------------------
    def create_pull(self, title, head, base, body=""):
        head_tip = self.resolve_ref(head)
        if head_tip is None:
            raise RefNotFoundError("Head branch does not exist: %s" % head)
        base_tip = self.resolve_ref(base)
        if base_tip is None:
            raise RefNotFoundError("Base branch does not exist: %s" % base)
        self.pull_counter += 1
        pull_id = self.store.next_pull_id()
        fresh_pull = Pull(self, self.pull_counter, pull_id, title, head, base,
                          head_tip, base_tip, body)
        self.pulls[fresh_pull.number] = fresh_pull
        return fresh_pull

    def get_pull(self, number):
        return self.pulls.get(number)

    def list_pulls(self, state=None):
        visible = [item for item in self.pulls.values()
                   if state in (None, "all") or item.state == state]
        visible.sort(key=lambda item: item.number)
        return visible

    def pull_files(self, pull):
        """Diff base commit tree -> head commit tree.

        Returns [{filename, status, additions, deletions, changes, sha}].
        """
        base_files = self._commit_tree_map(pull.base_sha)
        head_files = self._commit_tree_map(pull.head_sha)
        changed = []
        for file_path in sorted(set(base_files) | set(head_files)):
            if file_path not in base_files:
                mode, blob = head_files[file_path]
                blob_bytes = self.objects.blob_bytes(blob) or b""
                changed.append({"filename": file_path, "status": "added",
                                "additions": len(blob_bytes.splitlines()),
                                "deletions": 0,
                                "changes": len(blob_bytes.splitlines()), "sha": blob})
            elif file_path not in head_files:
                mode, blob = base_files[file_path]
                blob_bytes = self.objects.blob_bytes(blob) or b""
                changed.append({"filename": file_path, "status": "removed",
                                "additions": 0,
                                "deletions": len(blob_bytes.splitlines()),
                                "changes": len(blob_bytes.splitlines()), "sha": blob})
            elif base_files[file_path][1] != head_files[file_path][1]:
                before = (self.objects.blob_bytes(base_files[file_path][1]) or b""
                          ).decode("utf-8", "replace").splitlines()
                after = (self.objects.blob_bytes(head_files[file_path][1]) or b""
                         ).decode("utf-8", "replace").splitlines()
                added = removed = 0
                for kind, a_start, a_end, b_start, b_end in difflib.SequenceMatcher(
                        a=before, b=after, autojunk=False).get_opcodes():
                    if kind == "insert":
                        added += b_end - b_start
                    elif kind == "delete":
                        removed += a_end - a_start
                    elif kind == "replace":
                        added += b_end - b_start
                        removed += a_end - a_start
                changed.append({"filename": file_path, "status": "modified",
                                "additions": added, "deletions": removed,
                                "changes": added + removed,
                                "sha": head_files[file_path][1]})
        return changed

    def _would_conflict(self, pull):
        """True when the squash would conflict: a path the head changed
        (vs the pull's base) was also changed on the default branch since
        the pull's base, to different bytes."""
        base_files = self._commit_tree_map(pull.base_sha)
        head_files = self._commit_tree_map(pull.head_sha)
        branch_tip_sha = self.branch_tip(self.default_branch)
        current_files = self._commit_tree_map(branch_tip_sha) if branch_tip_sha else {}
        for file_path in sorted(set(base_files) | set(head_files)):
            base_blob = base_files.get(file_path, (None, None))[1]
            head_blob = head_files.get(file_path, (None, None))[1]
            if base_blob == head_blob:
                continue
            current_blob = current_files.get(file_path, (None, None))[1]
            if current_blob != base_blob and current_blob != head_blob:
                return True
        return False

    def merge_pull(self, number):
        """Squash-merge: one commit on the default branch whose tree is the
        head branch's tree. Returns (squash_sha, message)."""
        target = self.pulls.get(number)
        if target is None:
            raise RepoNotFoundError("Pull request does not exist: %d" % number)
        if target.merged or target.state != "open":
            raise ConflictError("Pull request is already closed")
        if self._would_conflict(target):
            raise ConflictError("Merge conflict")
        head_root = self.objects.commit_tree(target.head_sha)
        branch_tip_sha = self.branch_tip(self.default_branch)
        parent_list = [branch_tip_sha] if branch_tip_sha else []
        merge_message = "Merge pull request #%d" % number
        squash_sha = self.objects.commit(head_root, parent_list, merge_message)
        self.refs[self.branch_ref(self.default_branch)] = squash_sha
        target.state = "closed"
        target.merged = True
        target.merge_commit_sha = squash_sha
        return squash_sha, merge_message

    # -- contents ------------------------------------------------------
    def put_content(self, path, content_bytes, message, branch="main",
                    expected_sha=None):
        """Write/update a file. Returns (commit_sha, blob_sha).

        expected_sha, when given, must equal the current blob sha or a
        StaleShaError is raised; a missing file with an expected sha raises
        RepoNotFoundError.
        """
        branch_tip_sha = self.branch_tip(branch)
        file_map = {}
        if branch_tip_sha is not None:
            for file_path, (_mode, blob) in self._commit_tree_map(branch_tip_sha).items():
                file_map[file_path] = self.objects.blob_bytes(blob) or b""
        if expected_sha is not None:
            if path not in file_map:
                raise RepoNotFoundError("File does not exist: %s" % path)
            current_blob = self.objects.blob(file_map[path])
            if current_blob != expected_sha:
                raise StaleShaError("sha does not match the current blob")
        file_map[path] = content_bytes
        fresh_blob = self.objects.blob(content_bytes)
        fresh_tree = self.objects.tree_from_files(file_map)
        parent_list = [branch_tip_sha] if branch_tip_sha else []
        fresh_commit = self.objects.commit(fresh_tree, parent_list, message)
        self.refs[self.branch_ref(branch)] = fresh_commit
        return fresh_commit, fresh_blob

    def get_content(self, path, ref="main"):
        """(bytes|None, blob_sha|None) for path at ref."""
        tip_sha = self.resolve_ref(ref)
        if tip_sha is None:
            return None, None
        found = self._commit_tree_map(tip_sha).get(path)
        if found is None:
            return None, None
        return self.objects.blob_bytes(found[1]), found[1]

    def list_dir(self, path, ref="main"):
        """Immediate children of directory `path` at ref.

        Returns [{name, path, type, sha}] or None when the directory does
        not exist. `path` "" is the repository root.
        """
        tip_sha = self.resolve_ref(ref)
        if tip_sha is None:
            return None
        flat_tree = self._commit_tree_map(tip_sha)
        dir_prefix = "" if path == "" else path.rstrip("/") + "/"
        entries = {}
        saw_any = False
        for full_path, (mode, blob) in flat_tree.items():
            if not full_path.startswith(dir_prefix):
                continue
            remainder = full_path[len(dir_prefix):]
            if remainder == "":
                continue
            saw_any = True
            top, _, _ = remainder.partition("/")
            if top not in entries:
                if "/" in remainder:
                    entries[top] = ("dir", None)
                else:
                    entries[top] = ("file", blob)
            elif entries[top][0] == "file" and "/" in remainder:
                entries[top] = ("dir", None)
        if not saw_any:
            return None
        listing = []
        for child_name in sorted(entries):
            kind, blob = entries[child_name]
            if kind == "dir":
                # sha of the subtree: rebuild from the flattened map
                subtree_files = {p[len(dir_prefix + child_name + "/"):]: self.objects.blob_bytes(s)
                                 for p, (_m, s) in flat_tree.items()
                                 if p.startswith(dir_prefix + child_name + "/")}
                blob = self.objects.tree_from_files(subtree_files)
            listing.append({"name": child_name, "path": dir_prefix + child_name,
                            "type": kind, "sha": blob})
        return listing

    def delete_content(self, path, message, branch="main", expected_sha=None):
        """Remove a file. Returns the new commit sha."""
        branch_tip_sha = self.branch_tip(branch)
        if branch_tip_sha is None:
            raise RepoNotFoundError("Branch does not exist: %s" % branch)
        file_map = {}
        for file_path, (_mode, blob) in self._commit_tree_map(branch_tip_sha).items():
            file_map[file_path] = self.objects.blob_bytes(blob) or b""
        if path not in file_map:
            raise RepoNotFoundError("File does not exist: %s" % path)
        if expected_sha is not None:
            if self.objects.blob(file_map[path]) != expected_sha:
                raise StaleShaError("sha does not match the current blob")
        del file_map[path]
        fresh_tree = self.objects.tree_from_files(file_map)
        fresh_commit = self.objects.commit(fresh_tree, [branch_tip_sha], message)
        self.refs[self.branch_ref(branch)] = fresh_commit
        return fresh_commit


class Store:
    def __init__(self, owner_login="owner"):
        self.owner_login = owner_login
        self.repos = {}  # (owner, name) -> Repo
        self._next_repo_id = 1
        self._next_issue_id = 1
        self._next_pull_id = 1

    def next_issue_id(self):
        issued = self._next_issue_id
        self._next_issue_id += 1
        return issued

    def next_pull_id(self):
        issued = self._next_pull_id
        self._next_pull_id += 1
        return issued

    def create_repo(self, owner, name, description="", private=False):
        if (owner, name) in self.repos:
            raise RepoExistsError("Repository already exists: %s/%s"
                                  % (owner, name))
        fresh_repo = Repo(self, owner, name, self._next_repo_id,
                          description=description, private=private)
        self._next_repo_id += 1
        self.repos[(owner, name)] = fresh_repo
        return fresh_repo

    def get_repo(self, owner, name):
        return self.repos.get((owner, name))

    def delete_repo(self, owner, name):
        self.repos.pop((owner, name), None)

    def list_repos(self, owner):
        return sorted((repo for (owner_key, _name), repo in self.repos.items() if owner_key == owner),
                      key=lambda repo: repo.name)
