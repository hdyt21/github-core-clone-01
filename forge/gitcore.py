"""Minimal deterministic git object store (SHA-1, loose objects in memory).

Standard library only. No timestamps, no randomness, no wall clock: every
object id is the SHA-1 git itself computes (``<type> <size>\\0`` + bytes),
so ids are identical across runs for identical content.

Object encodings match the kit's git-client.ts exactly:
  * tree entries are ``<mode> <name>\\0<20-byte sha>``, sorted by git's
    rule (a directory compares as if its name ended in ``/``);
  * commit bytes are ``tree <sha>\\n`` + ``parent <sha>\\n``* +
    ``author <who>\\ncommitter <who>\\n\\n<message>`` where
    ``<who>`` is ``<identity> <unix-seconds> <tz>``.
  * generated commits use the fixed identity ``Forge <forge@make.fun>``
    and the fixed timestamp 1700000000 (+0000), never the wall clock.

API (the surface the store and the git-protocol worker share):
  ObjectStore.put(type, data) -> sha
  ObjectStore.get(sha) -> (type, data) | None
  ObjectStore.__contains__(sha)
  ObjectStore.blob(data) -> sha
  ObjectStore.tree_from_files({path: bytes}) -> sha
  ObjectStore.flatten_tree(tree_sha) -> {path: (mode, blob_sha)}
  ObjectStore.commit_tree(commit_sha) -> tree sha
  hash_object(type, data) -> sha
  parse_tree(data) -> [(mode, name, oid)]
  build_tree(entries) -> bytes
  parse_commit(data) -> {tree, parents, author, committer, message}
  build_commit(tree, parents, message, ...) -> bytes
  ancestors(store, tip) -> [sha, ...] (tip first, depth-first, first-parent first)
"""

import hashlib

__all__ = [
    "ObjectStore",
    "hash_object",
    "parse_tree",
    "build_tree",
    "parse_commit",
    "build_commit",
    "ancestors",
    "FIXED_IDENTITY",
    "FIXED_TIMESTAMP",
    "EMPTY_TREE_SHA",
]

FIXED_IDENTITY = "Forge <forge@make.fun>"
FIXED_TIMESTAMP = 1700000000
FIXED_ZONE = "+0000"

# sha1("tree 0\\0") -- the git empty tree, a constant.
EMPTY_TREE_SHA = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


def hash_object(obj_type: str, data: bytes) -> str:
    digest = hashlib.sha1()
    digest.update(("%s %d\x00" % (obj_type, len(data))).encode("utf-8"))
    digest.update(data)
    return digest.hexdigest()


def _tree_entry_key(entry):
    mode, name, _oid = entry
    key = (name + "/") if mode == "40000" else name
    return key.encode("utf-8")


def build_tree(entries) -> bytes:
    """entries: iterable of (mode, name, oid). Returns tree object bytes."""
    encoded = bytearray()
    for entry_mode, entry_name, entry_oid in sorted(entries, key=_tree_entry_key):
        encoded += ("%s %s\x00" % (entry_mode, entry_name)).encode("utf-8")
        encoded += bytes.fromhex(entry_oid)
    return bytes(encoded)


def parse_tree(data: bytes):
    """Parse tree object bytes -> [(mode, name, oid)] in stored order."""
    found = []
    pos = 0
    while pos < len(data):
        space_at = data.index(b" ", pos)
        nul_at = data.index(b"\x00", space_at + 1)
        entry_mode = data[pos:space_at].decode("ascii")
        entry_name = data[space_at + 1:nul_at].decode("utf-8")
        entry_oid = data[nul_at + 1:nul_at + 21].hex()
        found.append((entry_mode, entry_name, entry_oid))
        pos = nul_at + 21
    return found


def build_commit(tree, parents=(), message="", author=None, committer=None,
                 timestamp=FIXED_TIMESTAMP, timezone=FIXED_ZONE) -> bytes:
    if not message.endswith("\n"):
        message += "\n"
    author_line = "%s %d %s" % (author or FIXED_IDENTITY, timestamp, timezone)
    committer_line = "%s %d %s" % (committer or FIXED_IDENTITY, timestamp, timezone)
    header_lines = ["tree %s" % tree]
    for parent_sha in parents:
        header_lines.append("parent %s" % parent_sha)
    header_lines.append("author %s" % author_line)
    header_lines.append("committer %s" % committer_line)
    header_lines.append("")
    return ("\n".join(header_lines) + "\n" + message).encode("utf-8")


def parse_commit(data: bytes):
    split_at = data.find(b"\n\n")
    header = data if split_at < 0 else data[:split_at]
    body_text = "" if split_at < 0 else data[split_at + 2:].decode("utf-8")
    tree_sha = ""
    parent_list = []
    author_line = ""
    committer_line = ""
    for raw_line in header.decode("utf-8").split("\n"):
        if not raw_line:
            continue
        field, _, value = raw_line.partition(" ")
        if field == "tree":
            tree_sha = value
        elif field == "parent":
            parent_list.append(value)
        elif field == "author":
            author_line = value
        elif field == "committer":
            committer_line = value
    return {"tree": tree_sha, "parents": parent_list, "author": author_line,
            "committer": committer_line, "message": body_text}


def ancestors(store, tip):
    """Commit shas reachable from tip, tip first, depth-first.

    Deterministic: parents are visited in stored order. Returns [] for an
    unknown tip.
    """
    reached = []
    visited = set()
    pending = [tip]
    while pending:
        current = pending.pop()
        if current in visited:
            continue
        visited.add(current)
        entry = store.get(current)
        if entry is None or entry[0] != "commit":
            continue
        reached.append(current)
        parent_list = parse_commit(entry[1])["parents"]
        for parent_sha in reversed(parent_list):
            pending.append(parent_sha)
    return reached


class ObjectStore:
    """In-memory dict of sha -> (type, data). Insertion order is kept."""

    def __init__(self):
        self._table = {}

    def put(self, obj_type: str, data: bytes) -> str:
        object_id = hash_object(obj_type, data)
        if object_id not in self._table:
            self._table[object_id] = (obj_type, data)
        return object_id

    def get(self, sha: str):
        return self._table.get(sha)

    def __contains__(self, sha: str) -> bool:
        return sha in self._table

    def __len__(self) -> int:
        return len(self._table)

    def blob(self, data: bytes) -> str:
        return self.put("blob", data)

    def tree(self, entries) -> str:
        return self.put("tree", build_tree(entries))

    def commit(self, tree, parents=(), message="", **kw) -> str:
        return self.put("commit", build_commit(tree, parents, message, **kw))

    def tree_from_files(self, files) -> str:
        """files: {path: bytes}. Builds nested trees bottom-up; returns root tree sha."""
        # node: {"files": {name: bytes}, "dirs": {name: node}}
        tree_root = {"files": {}, "dirs": {}}
        for file_path, blob_data in files.items():
            segments = [p for p in file_path.split("/") if p != ""]
            cursor = tree_root
            for segment in segments[:-1]:
                cursor = cursor["dirs"].setdefault(segment, {"files": {}, "dirs": {}})
            cursor["files"][segments[-1]] = blob_data
        return self._build_subtree(tree_root)

    def _build_subtree(self, node) -> str:
        tree_entries = []
        for file_name, blob_data in node["files"].items():
            tree_entries.append(("100644", file_name, self.blob(blob_data)))
        for dir_name, sub_node in node["dirs"].items():
            tree_entries.append(("40000", dir_name, self._build_subtree(sub_node)))
        return self.tree(tree_entries)

    def flatten_tree(self, tree_sha):
        """tree sha -> {path: (mode, blob_sha)} for every blob, recursively."""
        flat = {}

        def descend(oid, prefix):
            entry = self.get(oid)
            if entry is None or entry[0] != "tree":
                return
            for entry_mode, entry_name, entry_oid in parse_tree(entry[1]):
                full_path = prefix + entry_name
                if entry_mode == "40000":
                    descend(entry_oid, full_path + "/")
                else:
                    flat[full_path] = (entry_mode, entry_oid)

        descend(tree_sha, "")
        return flat

    def commit_tree(self, commit_sha):
        entry = self.get(commit_sha)
        if entry is None or entry[0] != "commit":
            return None
        return parse_commit(entry[1])["tree"]

    def blob_bytes(self, blob_sha):
        entry = self.get(blob_sha)
        if entry is None or entry[0] != "blob":
            return None
        return entry[1]

# ---------------------------------------------------------------------------
# pkt-line codec, packfiles and reachability helpers (appended 2026-11-18).
#
# Pure stdlib (struct, zlib, collections). No timestamps, no randomness:
# pack bytes are a deterministic function of the objects they carry.
# Wire encodings match the kit's git-client.ts exactly
# (program/env/src/git-client.ts):
#   * pkt-line: 4 hex length bytes (payload + 4) then the payload; 0000 is
#     flush, 0001 delim, 0002 response-end; max packet 65520 bytes.
#   * packfile: "PACK" + uint32be version (2) + uint32be count, then entries
#     (type/size varint + zlib data), then the 20-byte SHA-1 trailer over
#     everything before it. Type codes: commit 1, tree 2, blob 3, tag 4,
#     ofs-delta 6, ref-delta 7.
# ---------------------------------------------------------------------------

import struct
import zlib
from collections import deque

ZERO_OID = "0" * 40
PKT_LINE_MAX = 65520

_TYPE_TO_CODE = {"commit": 1, "tree": 2, "blob": 3, "tag": 4}
_CODE_TO_TYPE = {1: "commit", 2: "tree", 3: "blob", 4: "tag"}
_TYPE_OFS_DELTA = 6
_TYPE_REF_DELTA = 7


def _fetch_object(store, sha):
    """(otype, data) for sha, or None when missing.

    ObjectStore.get returns None for a missing object (the contract the
    store/rest workers rely on); this helper just names that lookup.
    """
    return store.get(sha)


def _stash_object(store, obj_type, data):
    """Store an object; works with ObjectStore and with a plain dict."""
    if hasattr(store, "put"):
        return store.put(obj_type, data)
    sha = hash_object(obj_type, data)
    store[sha] = (obj_type, data)
    return sha


def _require_object(store, sha):
    """(otype, data) for sha, raising KeyError when missing."""
    obj = _fetch_object(store, sha)
    if obj is None:
        raise KeyError(sha)
    return obj


def _looks_hex4(text):
    return len(text) == 4 and all(c in "0123456789abcdefABCDEF" for c in text)


# -- pkt-line ---------------------------------------------------------------

def pkt_line(payload: bytes) -> bytes:
    """One pkt-line: 4 hex length bytes (payload + 4) then the payload."""
    total = len(payload) + 4
    if total > PKT_LINE_MAX:
        raise ValueError("pkt-line payload is %d bytes, over the limit" % len(payload))
    return ("%04x" % total).encode("ascii") + payload


def flush_pkt() -> bytes:
    return b"0000"


def delim_pkt() -> bytes:
    return b"0001"


def response_end_pkt() -> bytes:
    return b"0002"


def _scan_pktlines(data, start=0):
    """Parse pkt-lines from data[start:].

    Returns (packets, end_offset) where packets is a list of
    (kind, payload) with kind in 'data' | 'flush' | 'delim' |
    'response-end'. Stops at the first bytes that are not a pkt-line header
    (trailing raw data, e.g. a packfile after the flush); end_offset is
    where it stopped. Raises ValueError on a truncated/malformed packet.
    """
    frames = []
    cursor = start
    total = len(data)
    while cursor < total:
        if cursor + 4 > total:
            raise ValueError("truncated pkt-line header")
        header = data[cursor:cursor + 4]
        try:
            header_text = header.decode("ascii")
        except UnicodeDecodeError:
            break
        if not _looks_hex4(header_text):
            break
        size = int(header_text, 16)
        if size == 0:
            frames.append(("flush", b""))
            cursor += 4
            continue
        if size == 1:
            frames.append(("delim", b""))
            cursor += 4
            continue
        if size == 2:
            frames.append(("response-end", b""))
            cursor += 4
            continue
        if size == 3 or size > PKT_LINE_MAX:
            raise ValueError("invalid pkt-line length %d" % size)
        if cursor + size > total:
            raise ValueError("truncated pkt-line")
        frames.append(("data", data[cursor + 4:cursor + size]))
        cursor += size
    return frames, cursor


def parse_pktlines(data):
    """Split a pkt-line stream into [payload bytes | None].

    None marks a control packet (flush; delim/response-end too). Raises
    ValueError on trailing bytes that are not pkt-lines.
    """
    frames, stop = _scan_pktlines(data)
    if stop != len(data):
        raise ValueError("trailing bytes after pkt-line stream")
    return [frame_payload if frame_kind == "data" else None for frame_kind, frame_payload in frames]


# -- deltas -----------------------------------------------------------------

def apply_delta(base_data, delta_data):
    """Apply a git delta to base_data (git-client.ts applyDelta).

    Delta format: source-size varint, target-size varint, then copy/insert
    instructions. Copy op: high bit set, offset/size bytes flagged by bits
    0-3/4-6 (size 0 means 0x10000). Insert op: low 7 bits are the length.
    """
    delta_bytes = bytes(delta_data)
    base_bytes = bytes(base_data)
    cursor = 0

    def read_number():
        nonlocal cursor
        number = 0
        bit_shift = 0
        while True:
            if cursor >= len(delta_bytes):
                raise ValueError("truncated delta varint")
            raw_byte = delta_bytes[cursor]
            cursor += 1
            number |= (raw_byte & 0x7F) << bit_shift
            bit_shift += 7
            if not (raw_byte & 0x80):
                return number
            if bit_shift > 63:
                raise ValueError("delta varint too long")

    expected_base = read_number()
    expected_out = read_number()
    if expected_base != len(base_bytes):
        raise ValueError("delta expects a %d-byte base, got %d" % (expected_base, len(base_bytes)))
    result = bytearray()
    while cursor < len(delta_bytes):
        opcode = delta_bytes[cursor]
        cursor += 1
        if opcode & 0x80:
            copy_start = 0
            copy_len = 0
            for flag_bit in range(4):
                if opcode & (1 << flag_bit):
                    if cursor >= len(delta_bytes):
                        raise ValueError("truncated delta copy offset")
                    copy_start |= delta_bytes[cursor] << (8 * flag_bit)
                    cursor += 1
            for flag_bit in range(4, 7):
                if opcode & (1 << flag_bit):
                    if cursor >= len(delta_bytes):
                        raise ValueError("truncated delta copy size")
                    copy_len |= delta_bytes[cursor] << (8 * (flag_bit - 4))
                    cursor += 1
            if copy_len == 0:
                copy_len = 0x10000
            if copy_start + copy_len > len(base_bytes):
                raise ValueError("delta copy out of base")
            result += base_bytes[copy_start:copy_start + copy_len]
        elif opcode:
            insert_len = opcode & 0x7F
            if cursor + insert_len > len(delta_bytes):
                raise ValueError("truncated delta insert")
            result += delta_bytes[cursor:cursor + insert_len]
            cursor += insert_len
        else:
            raise ValueError("delta opcode 0 is invalid")
    if len(result) != expected_out:
        raise ValueError("delta produced %d of %d bytes" % (len(result), expected_out))
    return bytes(result)


# -- packfile ---------------------------------------------------------------

def _write_entry_header(type_code, size):
    """A pack entry's type/size varint (git-client.ts writePackObjectHeader)."""
    head_byte = (type_code << 4) | (size & 0x0F)
    size >>= 4
    encoded = bytearray()
    while size:
        encoded.append(head_byte | 0x80)
        head_byte = size & 0x7F
        size >>= 7
    encoded.append(head_byte)
    return bytes(encoded)


def _read_pack_header(data, offset):
    """Read a pack entry header -> (size, type_code, next_offset)."""
    if offset >= len(data):
        raise ValueError("pack entry has no header")
    raw = data[offset]
    offset += 1
    kind_code = (raw >> 4) & 0x07
    total = raw & 0x0F
    bit_shift = 4
    while raw & 0x80:
        if offset >= len(data):
            raise ValueError("truncated pack entry header")
        raw = data[offset]
        offset += 1
        total |= (raw & 0x7F) << bit_shift
        bit_shift += 7
        if bit_shift > 63:
            raise ValueError("pack entry size varint too long")
    return total, kind_code, offset


def _read_ofs_offset(data, offset):
    """OFS_DELTA base-offset varint -> (relative_offset, next_offset)."""
    if offset >= len(data):
        raise ValueError("ofs-delta has no base offset")
    raw = data[offset]
    offset += 1
    relative = raw & 0x7F
    while raw & 0x80:
        if offset >= len(data):
            raise ValueError("truncated ofs-delta base offset")
        raw = data[offset]
        offset += 1
        relative = ((relative + 1) << 7) | (raw & 0x7F)
    return relative, offset


def _write_ofs_offset(value):
    """Inverse of _read_ofs_offset: encode a relative base offset."""
    encoded = [value & 0x7F]
    value >>= 7
    while value:
        value -= 1
        encoded.append(0x80 | (value & 0x7F))
        value >>= 7
    encoded.reverse()
    return bytes(encoded)


class _BaseMissing(Exception):
    """A REF_DELTA whose base has not resolved yet (retry later)."""


def parse_packfile(data, store):
    """Parse a v2/v3 packfile, resolve OFS_DELTA/REF_DELTA entries, store
    every resolved object (via put()). Returns the list of resolved object
    SHAs in pack order. Raises ValueError on a malformed pack, an
    unresolvable delta base, or a bad trailer checksum."""
    if len(data) < 32 or data[:4] != b"PACK":
        raise ValueError("not a packfile")
    pack_version = struct.unpack(">I", data[4:8])[0]
    if pack_version not in (2, 3):
        raise ValueError("unsupported pack version %d" % pack_version)
    entry_count = struct.unpack(">I", data[8:12])[0]
    trailer_at = len(data) - 20
    if trailer_at < 12:
        raise ValueError("packfile too short")
    seen_trailer = data[trailer_at:].hex()
    wanted_trailer = hashlib.sha1(data[:trailer_at]).hexdigest()
    if seen_trailer != wanted_trailer:
        raise ValueError("pack trailer checksum mismatch")

    entries = []
    offset_index = {}
    cursor = 12
    for _ in range(entry_count):
        start_at = cursor
        size, kind_code, cursor = _read_pack_header(data, cursor)
        base_at = None
        base_sha = None
        if kind_code == _TYPE_OFS_DELTA:
            relative, cursor = _read_ofs_offset(data, cursor)
            if relative <= 0 or relative > start_at:
                raise ValueError("ofs-delta base offset out of range")
            base_at = start_at - relative
        elif kind_code == _TYPE_REF_DELTA:
            if cursor + 20 > trailer_at:
                raise ValueError("truncated ref-delta base id")
            base_sha = data[cursor:cursor + 20].hex()
            cursor += 20
        elif kind_code not in _CODE_TO_TYPE:
            raise ValueError("unknown pack entry type %d" % kind_code)
        try:
            inflater = zlib.decompressobj()
            raw_data = inflater.decompress(data[cursor:trailer_at])
            used = len(data[cursor:trailer_at]) - len(inflater.unused_data)
        except zlib.error as err:
            raise ValueError("zlib stream did not inflate: %s" % err)
        if used <= 0:
            raise ValueError("empty zlib stream in pack entry")
        cursor += used
        if len(raw_data) != size:
            raise ValueError(
                "pack entry declares %d bytes, inflated to %d" % (size, len(raw_data)))
        record = {"offset": start_at, "type_code": kind_code, "size": size,
                  "data": raw_data, "base_offset": base_at, "base_oid": base_sha}
        entries.append(record)
        offset_index[start_at] = record
    if cursor != trailer_at:
        raise ValueError("pack has %d stray bytes before its trailer" % (trailer_at - cursor))

    done = {}  # offset -> (otype, data, sha)
    sha_index = {}    # sha -> entry
    in_progress = set()

    def resolve_entry(entry):
        if entry["offset"] in done:
            return done[entry["offset"]]
        if entry["offset"] in in_progress:
            raise ValueError("delta cycle at pack offset %d" % entry["offset"])
        in_progress.add(entry["offset"])
        try:
            code = entry["type_code"]
            if code == _TYPE_OFS_DELTA:
                base_record = offset_index.get(entry["base_offset"])
                if base_record is None:
                    raise ValueError("ofs-delta base not in pack")
                base_type, base_data, _ = resolve_entry(base_record)
                obj_type, obj_data = base_type, apply_delta(base_data, entry["data"])
            elif code == _TYPE_REF_DELTA:
                base_record = sha_index.get(entry["base_oid"])
                if base_record is not None:
                    base_type, base_data, _ = resolve_entry(base_record)
                    obj_type, obj_data = base_type, apply_delta(base_data, entry["data"])
                else:
                    stored_base = _fetch_object(store, entry["base_oid"])
                    if stored_base is None:
                        raise _BaseMissing(entry["base_oid"])
                    obj_type, obj_data = stored_base[0], apply_delta(stored_base[1], entry["data"])
            else:
                obj_type, obj_data = _CODE_TO_TYPE[code], entry["data"]
        finally:
            in_progress.discard(entry["offset"])
        sha = hash_object(obj_type, obj_data)
        done[entry["offset"]] = (obj_type, obj_data, sha)
        sha_index[sha] = entry
        return obj_type, obj_data, sha

    # Full objects first (so REF_DELTA bases resolve), then deltas; retry
    # until every delta's base is known, so entry order does not matter.
    for entry in entries:
        if entry["type_code"] not in (_TYPE_OFS_DELTA, _TYPE_REF_DELTA):
            resolve_entry(entry)
    waiting = [e for e in entries if e["type_code"] in (_TYPE_OFS_DELTA, _TYPE_REF_DELTA)]
    while waiting:
        moved = False
        still_waiting = []
        for entry in waiting:
            try:
                resolve_entry(entry)
                moved = True
            except _BaseMissing:
                still_waiting.append(entry)
        if not moved:
            raise ValueError("ref-delta base %s is not available" % (still_waiting[0]["base_oid"],))
        waiting = still_waiting

    object_ids = []
    for entry in entries:
        obj_type, obj_data, sha = done[entry["offset"]]
        _stash_object(store, obj_type, obj_data)
        object_ids.append(sha)
    return object_ids


def build_packfile(store, want, have):
    """Self-contained packfile (full entries, no deltas, no thin pack) with
    every object reachable from want minus those reachable from have.
    Deterministic: entries are sorted by sha."""
    to_send = collect_reachable(store, want) - collect_reachable(store, have)
    return build_packfile_from_objects(store, to_send)


def build_packfile_from_objects(store, oids):
    """Packfile holding exactly the given objects (sorted by sha)."""
    packed = []
    for oid in sorted(oids):
        entry = _fetch_object(store, oid)
        if entry is None:
            continue
        packed.append(entry)
    encoded = bytearray()
    encoded += b"PACK"
    encoded += struct.pack(">I", 2)
    encoded += struct.pack(">I", len(packed))
    for entry_type, entry_data in packed:
        encoded += _write_entry_header(_TYPE_TO_CODE[entry_type], len(entry_data))
        encoded += zlib.compress(entry_data)
    encoded += hashlib.sha1(bytes(encoded)).digest()
    return bytes(encoded)


# -- reachability -----------------------------------------------------------

def _tag_target_oid(data):
    for raw_line in data.split(b"\n"):
        if raw_line.startswith(b"object "):
            return raw_line[7:].decode("ascii").strip()
        if raw_line == b"":
            break
    return None


def collect_reachable(store, roots, max_commit_depth=None, stop_at=None):
    """Every object reachable from roots (commit -> tree + parents,
    tree -> entries with gitlinks skipped, tag -> target, blob -> nothing).
    max_commit_depth limits parent traversal (1 = the root commits only).
    stop_at is a set of commit shas whose parents are not followed (the
    client already holds them: a shallow boundary). Missing objects are
    skipped. Breadth-first, so a commit is first seen at minimum depth."""
    blocked = set(stop_at or ())
    visited = set()
    pending = deque()
    for start in roots:
        if isinstance(start, str) and len(start) == 40:
            pending.append((start, 0))
    while pending:
        current, depth = pending.popleft()
        if current in visited:
            continue
        if not isinstance(current, str) or len(current) != 40:
            continue
        entry = _fetch_object(store, current)
        if entry is None:
            continue
        visited.add(current)
        entry_type, entry_data = entry
        if entry_type == "commit":
            parsed = parse_commit(entry_data)
            if parsed["tree"]:
                pending.append((parsed["tree"], depth))
            if current not in blocked and (max_commit_depth is None or depth + 1 < max_commit_depth):
                for parent_sha in parsed["parents"]:
                    pending.append((parent_sha, depth + 1))
        elif entry_type == "tag":
            tag_target = _tag_target_oid(entry_data)
            if tag_target:
                pending.append((tag_target, depth))
        elif entry_type == "tree":
            for entry_mode, _name, child_oid in parse_tree(entry_data):
                if entry_mode == "160000":
                    continue
                pending.append((child_oid, depth))
    return visited


def shallow_boundaries(store, roots, depth):
    """Commits exactly depth-1 generations below roots: the new shallow
    boundary after a `deepen <depth>` fetch (depth 1 -> the roots)."""
    if not depth or depth < 1:
        return set()
    depths = {}
    pending = deque()
    for start in roots:
        if isinstance(start, str) and len(start) == 40:
            pending.append((start, 0))
    while pending:
        current, d = pending.popleft()
        if current in depths:
            continue
        entry = _fetch_object(store, current)
        if entry is None:
            continue
        entry_type, entry_data = entry
        if entry_type == "tag":
            tag_target = _tag_target_oid(entry_data)
            if tag_target:
                pending.append((tag_target, d))
            continue
        if entry_type != "commit":
            continue
        depths[current] = d
        if d + 1 < depth:
            for parent_sha in parse_commit(entry_data)["parents"]:
                pending.append((parent_sha, d + 1))
    return {oid for oid, d in depths.items() if d == depth - 1}


# -- convenience builders ---------------------------------------------------

def make_commit(store, tree_sha, parents, message, author="forge <forge@local>",
                committer="forge <forge@local>"):
    """Build a commit with a FIXED timestamp (0 +0000) so output is
    deterministic. Reuses build_commit (timestamp 0, given identities).
    Returns the commit sha."""
    raw = build_commit(tree_sha, parents, message, author=author,
                       committer=committer, timestamp=0, timezone="+0000")
    return store.put("commit", raw)


def make_tree(store, entries):
    """entries: [(mode, name, sha)]; sorted by git tree order. Returns the
    tree sha."""
    return store.put("tree", build_tree(entries))


def read_tree(store, tree_sha):
    """Parse a tree object into [(mode, name, sha)] in stored order."""
    entry = _require_object(store, tree_sha)
    if entry[0] != "tree":
        raise ValueError("%s is not a tree" % tree_sha)
    return parse_tree(entry[1])


def flatten_tree(store, tree_sha, prefix=""):
    """{full path: (mode, sha)} for every blob under tree_sha, recursively.
    (Module-level twin of ObjectStore.flatten_tree.)"""
    out = {}
    for mode, name, sha in read_tree(store, tree_sha):
        path = name if not prefix else prefix + "/" + name
        if mode == "40000":
            out.update(flatten_tree(store, sha, path))
        else:
            out[path] = (mode, sha)
    return out


def is_ancestor(store, anc, desc):
    """True when anc == desc or anc is reachable from desc by following
    commit parents (reuses ancestors())."""
    if anc == desc:
        return True
    return anc in ancestors(store, desc)
