"""Round-trip tests for forge.gitcore (packfile/pkt-line) and forge.gitproto.

Run from the work dir:  python3 -m forge.test_gitcore

Builds a small commit history, round-trips packfiles (full and
incremental), checks delta application with hand-built REF_DELTA and
OFS_DELTA packs, and simulates a receive-pack push followed by v0 and
v2 upload-pack fetches plus shallow/deepen fetches and a non-fast-forward
refusal. Exits non-zero when any test fails.
"""

import hashlib
import struct
import sys
import zlib

from .gitcore import (
    ZERO_OID,
    ObjectStore,
    _write_entry_header,
    _write_ofs_offset,
    apply_delta,
    build_packfile,
    collect_reachable,
    delim_pkt,
    flush_pkt,
    hash_object,
    is_ancestor,
    make_commit,
    make_tree,
    parse_commit,
    parse_packfile,
    parse_pktlines,
    pkt_line,
    read_tree,
)
from .gitproto import handle_info_refs, handle_receive_pack, handle_upload_pack

PASSED = []
FAILED = []


def expect(name, cond, detail=""):
    if cond:
        PASSED.append(name)
        print("PASS %s" % name)
    else:
        FAILED.append(name)
        print("FAIL %s %s" % (name, detail))


class StubRepo:
    """Stand-in for forge.store.Repo: .objects, .refs, .default_branch."""

    def __init__(self):
        self.objects = ObjectStore()
        self.refs = {}
        self.default_branch = "main"


def _make_history(store):
    """Two commits, one tree with two files each. Returns (c1, c2, blobs)."""
    blob_a = store.put("blob", b"hello world\n")
    blob_b = store.put("blob", b"second file\n")
    tree1 = make_tree(store, [("100644", "a.txt", blob_a), ("100644", "b.txt", blob_b)])
    commit1 = make_commit(store, tree1, [], "first commit\n")
    blob_a2 = store.put("blob", b"hello world v2\n")
    tree2 = make_tree(store, [("100644", "a.txt", blob_a2), ("100644", "b.txt", blob_b)])
    commit2 = make_commit(store, tree2, [commit1], "second commit\n")
    return commit1, commit2, (blob_a, blob_b, blob_a2)


# -- delta construction helpers (hand-built, independent of gitcore) --------

def _encode_varint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _copy_instruction(off, size):
    op = 0x80
    off_bytes = []
    tmp = off
    while tmp:
        off_bytes.append(tmp & 0xFF)
        tmp >>= 8
    size_bytes = []
    tmp = size
    while tmp:
        size_bytes.append(tmp & 0xFF)
        tmp >>= 8
    for i in range(len(off_bytes)):
        op |= 1 << i
    for i in range(len(size_bytes)):
        op |= 1 << (4 + i)
    return bytes([op]) + bytes(off_bytes) + bytes(size_bytes)


def _insert_instruction(data):
    return bytes([len(data)]) + data


def _build_pack(entries):
    """entries: [(type_code, raw_bytes_after_header, zlib_payload)] ->
    complete pack bytes with a correct trailer."""
    out = bytearray(b"PACK")
    out += struct.pack(">II", 2, len(entries))
    for type_code, after_header, payload in entries:
        out += _write_entry_header(type_code, len(payload))
        out += after_header
        out += zlib.compress(payload)
    out += hashlib.sha1(bytes(out)).digest()
    return bytes(out)


def _extract_band_one(packets):
    """Reassemble band-1 bytes from a response's pkt-line packets."""
    out = bytearray()
    for kind, payload in packets:
        if kind == "data" and payload and payload[0] == 1:
            out += payload[1:]
    return bytes(out)


def _split_sections(packets):
    """Split v2 fetch response packets into {section: [payloads]}."""
    sections = {}
    current = None
    for kind, payload in packets:
        if kind == "data":
            text = payload.decode("utf-8", "replace")
            if text in ("acknowledgments\n", "shallow-info\n", "wanted-refs\n", "packfile\n"):
                current = text.strip()
                sections[current] = []
            elif current is not None:
                sections[current].append(payload)
    return sections


# -- tests ------------------------------------------------------------------

def test_hashing():
    expect("hash_object empty blob",
           hash_object("blob", b"") == "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391")


def test_history_roundtrip():
    store = ObjectStore()
    c1, c2, (b1, b2, b1b) = _make_history(store)
    parsed = parse_commit(store.get(c2)[1])
    expect("parse_commit parents/message",
           parsed["parents"] == [c1] and parsed["message"] == "second commit\n")
    expect("parse_commit author fixed ts",
           parsed["author"].endswith(" 0 +0000"), parsed["author"])
    tree = read_tree(store, parsed["tree"])
    expect("read_tree entries",
           tree == [("100644", "a.txt", b1b), ("100644", "b.txt", b2)], str(tree))
    from .gitcore import flatten_tree
    flat = flatten_tree(store, parsed["tree"])
    expect("flatten_tree", flat == {"a.txt": ("100644", b1b), "b.txt": ("100644", b2)},
           str(flat))
    expect("is_ancestor c1 of c2", is_ancestor(store, c1, c2))
    expect("is_ancestor reflexive", is_ancestor(store, c2, c2))
    expect("is_ancestor c2 not of c1", not is_ancestor(store, c2, c1))
    # Tree sort: a directory sorts as if its name ended in '/'.
    d = store.put("tree", b"")
    t = make_tree(store, [("100644", "a.txt", b1), ("40000", "a", d),
                          ("100644", "z.txt", b2)])
    names = [name for _mode, name, _sha in read_tree(store, t)]
    expect("tree sort dir-as-slash", names == ["a.txt", "a", "z.txt"], str(names))


def test_pack_roundtrip():
    store = ObjectStore()
    c1, c2, (b1, b2, b1b) = _make_history(store)
    pack = build_packfile(store, [c2], [])
    fresh = ObjectStore()
    shas = parse_packfile(pack, fresh)
    expected = collect_reachable(store, [c2])
    expect("pack round-trip count", set(shas) == expected,
           "%d vs %d" % (len(shas), len(expected)))
    ok = all(fresh.get(sha) == store.get(sha) for sha in expected)
    expect("pack round-trip byte-identical", ok)
    # Incremental: only objects c2 introduced (new commit, new tree, new blob).
    inc = build_packfile(store, [c2], [c1])
    fresh2 = ObjectStore()
    shas2 = parse_packfile(inc, fresh2)
    expect("incremental pack excludes old tip", c1 not in shas2)
    expect("incremental pack has new blob", b1b in shas2 and b1 not in shas2)
    expect("incremental pack has new commit", c2 in shas2)
    # Bad trailer is rejected.
    try:
        parse_packfile(pack[:-1] + b"\x00", ObjectStore())
        expect("pack bad trailer rejected", False)
    except ValueError:
        expect("pack bad trailer rejected", True)


def test_delta_application():
    base = (b"alpha bravo charlie delta echo foxtrot golf hotel india juliet\n" * 4)
    target = base[:6] + b"BRAVO-2 " + base[6:]
    delta = (_encode_varint(len(base)) + _encode_varint(len(target))
             + _copy_instruction(0, 6) + _insert_instruction(b"BRAVO-2 ") + _copy_instruction(6, len(base) - 6))
    expect("apply_delta direct", apply_delta(base, delta) == target)
    store = ObjectStore()
    base_sha = store.put("blob", base)
    target_sha = hash_object("blob", target)
    # REF_DELTA pack: full base, then a ref-delta against it.
    ref_pack = _build_pack([
        (3, b"", base),
        (7, bytes.fromhex(base_sha), delta),
    ])
    fresh = ObjectStore()
    shas = parse_packfile(ref_pack, fresh)
    expect("ref-delta pack resolves", shas == [base_sha, target_sha], str(shas))
    expect("ref-delta bytes", fresh.get(target_sha) == ("blob", target))
    # OFS_DELTA pack: full base, then an ofs-delta against it.
    entry1 = _write_entry_header(3, len(base)) + zlib.compress(base)
    entry2_off = 12 + len(entry1)
    rel = entry2_off - 12
    ofs_pack = _build_pack([
        (3, b"", base),
        (6, _write_ofs_offset(rel), delta),
    ])
    fresh2 = ObjectStore()
    shas2 = parse_packfile(ofs_pack, fresh2)
    expect("ofs-delta pack resolves", shas2 == [base_sha, target_sha], str(shas2))
    expect("ofs-delta bytes", fresh2.get(target_sha) == ("blob", target))
    # REF_DELTA against a base the store already holds (thin-pack style).
    thin = _build_pack([(7, bytes.fromhex(base_sha), delta)])
    fresh3 = ObjectStore()
    fresh3.put("blob", base)
    shas3 = parse_packfile(thin, fresh3)
    expect("ref-delta external base", shas3 == [target_sha])


def test_pktline_parsing():
    data = pkt_line(b"hello\n") + flush_pkt() + pkt_line(b"world\n")
    expect("parse_pktlines", parse_pktlines(data) == [b"hello\n", None, b"world\n"])
    expect("parse_pktlines delim",
           parse_pktlines(pkt_line(b"a") + delim_pkt() + pkt_line(b"b")) == [b"a", None, b"b"])


def _do_push(repo, old, new, ref, pack):
    cmd = ("%s %s %s\x00%s\n" % (
        old, new, ref, "report-status side-band-64k ofs-delta agent=test/1.0"))
    body = pkt_line(cmd.encode("utf-8")) + flush_pkt() + pack
    status, ctype, out = handle_receive_pack(repo, body, {})
    from .gitcore import _scan_pktlines
    packets, _ = _scan_pktlines(out)
    inner = _extract_band_one(packets)
    lines = parse_pktlines(inner)
    return status, ctype, lines


def test_receive_then_fetch():
    repo = StubRepo()
    store = ObjectStore()
    c1, c2, _blobs = _make_history(store)
    # Seed the repo with c1 only.
    for sha in collect_reachable(store, [c1]):
        repo.objects.put(*store.get(sha))
    repo.refs["refs/heads/main"] = c1
    # Push c2 (pack holds only the new objects).
    pack = build_packfile(store, [c2], [c1])
    status, ctype, lines = _do_push(repo, c1, c2, "refs/heads/main", pack)
    expect("receive-pack status/ctype",
           status == 200 and ctype == "application/x-git-receive-pack-result",
           "%s %s" % (status, ctype))
    expect("receive-pack report",
           lines == [b"unpack ok\n", b"ok refs/heads/main\n", None], str(lines))
    expect("receive-pack ref moved", repo.refs.get("refs/heads/main") == c2)
    expect("receive-pack objects stored",
           repo.objects.get(c2) == store.get(c2))
    # v0 upload-pack fetch of the new tip.
    req = (pkt_line(("want %s side-band-64k\n" % c2).encode("utf-8")) + flush_pkt()
           + pkt_line(b"done\n") + flush_pkt())
    status, ctype, out = handle_upload_pack(repo, req, {})
    from .gitcore import _scan_pktlines
    packets, _ = _scan_pktlines(out)
    expect("v0 fetch status/ctype",
           status == 200 and ctype == "application/x-git-upload-pack-result")
    expect("v0 fetch NAK first",
           packets[0] == ("data", b"NAK\n"), str(packets[0]))
    pack_bytes = _extract_band_one(packets)
    fresh = ObjectStore()
    shas = parse_packfile(pack_bytes, fresh)
    expect("v0 fetch pack has tip", c2 in shas)
    expect("v0 fetch commit byte-identical", fresh.get(c2) == store.get(c2))
    # v0 fetch with the tip already held transfers nothing new.
    req2 = (pkt_line(("want %s side-band-64k\n" % c2).encode("utf-8")) + flush_pkt()
            + pkt_line(("have %s\n" % c2).encode("utf-8"))
            + pkt_line(b"done\n") + flush_pkt())
    _s, _c, out2 = handle_upload_pack(repo, req2, {})
    packets2, _ = _scan_pktlines(out2)
    shas2 = parse_packfile(_extract_band_one(packets2), ObjectStore())
    expect("v0 fetch nothing-new", shas2 == [])


def test_v2_protocol():
    repo = StubRepo()
    store = ObjectStore()
    c1, c2, _blobs = _make_history(store)
    for sha in collect_reachable(store, [c2]):
        repo.objects.put(*store.get(sha))
    repo.refs["refs/heads/main"] = c2
    # v2 advertisement.
    status, ctype, body = handle_info_refs(repo, "git-upload-pack", proto="version=2")
    lines = parse_pktlines(body)
    expect("v2 advertisement opens version 2",
           status == 200 and lines[0] == b"version 2\n", str(lines[:1]))
    caps = b" ".join(lines[1:-1])
    expect("v2 advertisement caps",
           b"fetch" in caps and b"ls-refs" in caps and b"object-format=sha1" in caps,
           str(caps))
    # v0 advertisement still has the service preamble.
    _s, _c, body0 = handle_info_refs(repo, "git-upload-pack")
    expect("v0 advertisement preamble",
           parse_pktlines(body0)[0] == b"# service=git-upload-pack\n")
    # v2 ls-refs.
    req = (pkt_line(b"command=ls-refs\n") + delim_pkt()
           + pkt_line(b"peel\n") + pkt_line(b"symrefs\n") + pkt_line(b"unborn\n")
           + flush_pkt())
    _s, _c, out = handle_upload_pack(repo, req, {"git-protocol": "version=2"})
    ref_lines = [p for p in parse_pktlines(out) if p]
    main = [l for l in ref_lines if l.startswith(c2.encode() + b" refs/heads/main")]
    head = [l for l in ref_lines if b" HEAD " in l]
    expect("v2 ls-refs main", len(main) == 1, str(ref_lines))
    expect("v2 ls-refs HEAD symref",
           head and b"symref-target:refs/heads/main" in head[0], str(head))
    # v2 ls-refs with a ref-prefix returns only matching refs.
    req_p = (pkt_line(b"command=ls-refs\n") + delim_pkt()
             + pkt_line(b"ref-prefix refs/tags/\n") + flush_pkt())
    _s, _c, out_p = handle_upload_pack(repo, req_p, {"git-protocol": "version=2"})
    expect("v2 ls-refs prefix filters",
           [p for p in parse_pktlines(out_p) if p] == [])
    # v2 fetch.
    req_f = (pkt_line(b"command=fetch\n") + delim_pkt()
             + pkt_line(b"thin-pack\n") + pkt_line(b"ofs-delta\n")
             + pkt_line(("want %s\n" % c2).encode("utf-8"))
             + pkt_line(b"done\n") + flush_pkt())
    _s, _c, out_f = handle_upload_pack(repo, req_f, {"git-protocol": "version=2"})
    from .gitcore import _scan_pktlines
    packets, _ = _scan_pktlines(out_f)
    sections = _split_sections(packets)
    expect("v2 fetch acks NAK", sections.get("acknowledgments") == [b"NAK\n"],
           str(sections))
    shas = parse_packfile(_extract_band_one(packets), ObjectStore())
    expect("v2 fetch pack has tip", c2 in shas)


def test_shallow_fetch():
    repo = StubRepo()
    store = ObjectStore()
    c1, c2, _ = _make_history(store)
    b3 = store.put("blob", b"third file\n")
    t3 = make_tree(store, [("100644", "a.txt", b3), ("100644", "b.txt", store.put("blob", b"second file\n"))])
    c3 = make_commit(store, t3, [c2], "third commit\n")
    for sha in collect_reachable(store, [c3]):
        repo.objects.put(*store.get(sha))
    repo.refs["refs/heads/main"] = c3
    # depth-1 fetch: tip only, shallow-info names the tip.
    req = (pkt_line(b"command=fetch\n") + delim_pkt()
           + pkt_line(("want %s\n" % c3).encode("utf-8"))
           + pkt_line(b"deepen 1\n") + pkt_line(b"done\n") + flush_pkt())
    _s, _c, out = handle_upload_pack(repo, req, {"git-protocol": "version=2"})
    from .gitcore import _scan_pktlines
    packets, _ = _scan_pktlines(out)
    sections = _split_sections(packets)
    expect("shallow depth-1 info",
           sections.get("shallow-info") == [("shallow %s\n" % c3).encode("utf-8")],
           str(sections))
    shas = parse_packfile(_extract_band_one(packets), ObjectStore())
    expect("shallow depth-1 tip only", c3 in shas and c2 not in shas and c1 not in shas)
    # deepen 2 from the depth-1 boundary: parent only, boundary moves.
    req2 = (pkt_line(b"command=fetch\n") + delim_pkt()
            + pkt_line(("want %s\n" % c3).encode("utf-8"))
            + pkt_line(("have %s\n" % c3).encode("utf-8"))
            + pkt_line(("shallow %s\n" % c3).encode("utf-8"))
            + pkt_line(b"deepen 2\n") + pkt_line(b"done\n") + flush_pkt())
    _s, _c, out2 = handle_upload_pack(repo, req2, {"git-protocol": "version=2"})
    packets2, _ = _scan_pktlines(out2)
    sections2 = _split_sections(packets2)
    shallow2 = sections2.get("shallow-info") or []
    expect("deepen shallow-info moves boundary",
           ("shallow %s\n" % c2).encode("utf-8") in shallow2
           and ("unshallow %s\n" % c3).encode("utf-8") in shallow2,
           str(shallow2))
    shas2 = parse_packfile(_extract_band_one(packets2), ObjectStore())
    expect("deepen delivers parent only", c2 in shas2 and c1 not in shas2 and c3 not in shas2)


def test_non_fast_forward():
    repo = StubRepo()
    store = ObjectStore()
    c1, c2, _ = _make_history(store)
    for sha in collect_reachable(store, [c2]):
        repo.objects.put(*store.get(sha))
    repo.refs["refs/heads/main"] = c2
    # A sibling commit (child of c1, not of c2) is a non-fast-forward update.
    sib_tree = make_tree(store, [("100644", "a.txt", store.put("blob", b"sibling\n"))])
    sib = make_commit(store, sib_tree, [c1], "sibling commit\n")
    pack = build_packfile(store, [sib], [c2])
    _s, _c, lines = _do_push(repo, c2, sib, "refs/heads/main", pack)
    expect("non-ff refused",
           lines == [b"unpack ok\n", b"ng refs/heads/main non-fast-forward\n", None],
           str(lines))
    expect("non-ff ref unmoved", repo.refs.get("refs/heads/main") == c2)
    # Mixed push: one good ref, one refused; the good one applies.
    # (Mirror the suite: create refs/heads/side at c2 first.)
    _s, _c, setup_lines = _do_push(repo, ZERO_OID, c2, "refs/heads/side",
                                   build_packfile(store, [c2], [c2]))
    expect("mixed setup side created",
           setup_lines == [b"unpack ok\n", b"ok refs/heads/side\n", None],
           str(setup_lines))
    fwd = make_commit(store, sib_tree, [c2], "forward commit\n")
    pack2 = build_packfile(store, [fwd, sib], [c2])
    cmd1 = pkt_line(("%s %s %s\x00%s\n" % (
        c2, fwd, "refs/heads/main",
        "report-status side-band-64k agent=test/1.0")).encode("utf-8"))
    cmd2 = pkt_line(("%s %s %s\n" % (c2, sib, "refs/heads/side")).encode("utf-8"))
    body = cmd1 + cmd2 + flush_pkt() + pack2
    _s, _c, out = handle_receive_pack(repo, body, {})
    from .gitcore import _scan_pktlines
    packets, _ = _scan_pktlines(out)
    lines2 = parse_pktlines(_extract_band_one(packets))
    expect("mixed report",
           lines2 == [b"unpack ok\n", b"ok refs/heads/main\n",
                      b"ng refs/heads/side non-fast-forward\n", None],
           str(lines2))
    expect("mixed good ref applied", repo.refs.get("refs/heads/main") == fwd)
    expect("mixed refused ref unmoved", repo.refs.get("refs/heads/side") == c2)
    # Delete the branch.
    _s, _c, lines3 = _do_push(repo, fwd, ZERO_OID, "refs/heads/main", b"")
    expect("delete reported ok",
           lines3 == [b"unpack ok\n", b"ok refs/heads/main\n", None], str(lines3))
    expect("delete removed ref", "refs/heads/main" not in repo.refs)


def run_all():
    tests = [test_hashing, test_history_roundtrip, test_pack_roundtrip,
             test_delta_application, test_pktline_parsing,
             test_receive_then_fetch, test_v2_protocol, test_shallow_fetch,
             test_non_fast_forward]
    for test in tests:
        try:
            test()
        except Exception as err:  # keep going so one failure does not hide the rest
            FAILED.append(test.__name__)
            print("ERROR %s: %s: %s" % (test.__name__, type(err).__name__, err))
    print("\n%d passed, %d failed" % (len(PASSED), len(FAILED)))
    if FAILED:
        print("failed: %s" % ", ".join(FAILED))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(run_all())
