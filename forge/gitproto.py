"""Smart-HTTP handlers: git-upload-pack (protocol v2 and v0) and
git-receive-pack (v0 only).

They work on a Repo (forge.store.Repo): .objects is an ObjectStore,
.refs maps "refs/heads/main" -> sha, .default_branch names the default
branch. Each handler returns (status, content_type, body_bytes); the HTTP
server (forge.server) answers 404 for a missing repo before calling us,
and passes the Git-Protocol header through as proto /
headers["git-protocol"].

Wire facts, each from the kit (program/env/src/git-client.ts):
  * info/refs v2 (Git-Protocol: version=2): first packet "version 2",
    then one capability line per packet, then flush. The client requires
    the `fetch` and `ls-refs` capabilities and object-format=sha1
    (parseAdvertisement; the advertise-v2 check).
  * info/refs v0: "# service=<service>" preamble packet, flush, then
    "<oid> <ref>" lines, capabilities after a NUL on the first line; the
    empty repo advertises "<zero> capabilities^{}" (parseAdvertisement).
  * v2 ls-refs request: "command=ls-refs", agent/object-format lines,
    delim, then peel/symrefs/unborn/ref-prefix lines, flush. Reply: one
    "<oid> <ref>[ symref-target:X][ peeled:Y]" packet per ref, flush.
  * v2 fetch request: "command=fetch", delim, then thin-pack/no-progress/
    ofs-delta, want/have/shallow/deepen lines, done, flush. Reply sections:
    "acknowledgments" (NAK), "shallow-info" (shallow/unshallow),
    "packfile" (sideband-1 pack chunks), sections ended by delim, the
    reply by flush (parseV2Fetch; the sideband-framing check).
  * v0 fetch request: want lines (capabilities space-separated on the
    first), flush, have lines + done, flush. Reply: a "NAK" packet,
    shallow/unshallow packets, then sideband-1 pack chunks, flush
    (fetchV0).
  * receive-pack request: "<old> <new> <ref>" lines, capabilities after a
    NUL on the first, flush, then the raw packfile. Reply: report-status
    ("unpack ok" then "ok <ref>" / "ng <ref> <reason>" per ref), pkt-lined
    inside sideband band 1 when side-band-64k was negotiated (push(),
    parsePushReport). A non-fast-forward update is refused per ref with
    reason "non-fast-forward" and the ref does not move (RULES.txt §Git);
    a push with one good and one refused ref applies the good one.
"""

from .gitcore import (
    ZERO_OID,
    _scan_pktlines,
    build_packfile_from_objects,
    collect_reachable,
    delim_pkt,
    flush_pkt,
    is_ancestor,
    parse_packfile,
    pkt_line,
    shallow_boundaries,
)

__all__ = ["handle_info_refs", "handle_upload_pack", "handle_receive_pack"]

AGENT_IDENT = "forge-git/1.0"

# v2 advertisement capabilities (git-client.ts parseAdvertisement splits
# each line at the first '='; fetch's value is split on spaces for the
# shallow/filter features, ls-refs' value for unborn).
_V2_CAPS = [
    "agent=%s" % AGENT_IDENT,
    "ls-refs=unborn",
    "fetch=shallow filter",
    "object-format=sha1",
]

_SIDEBAND_SLICE = 65515  # pkt-line max 65520 minus 4 header minus 1 band byte


# -- small helpers ----------------------------------------------------------

def _lookup_header(hdrs, header_name):
    """Case-insensitive header lookup; "" when absent."""
    if not hdrs:
        return ""
    for key, value in hdrs.items():
        if key.lower() == header_name.lower():
            return value or ""
    return ""


def _wants_v2(version):
    return bool(version) and version.strip().lower() == "version=2"


def _head_ref_target(target_repo):
    """The ref HEAD points at: the default branch when it exists, else the
    first branch, else the (unborn) default branch."""
    default = getattr(target_repo, "default_branch", None) or "main"
    target = "refs/heads/%s" % default
    if target in target_repo.refs:
        return target
    heads = sorted(ref for ref in target_repo.refs if ref.startswith("refs/heads/"))
    if heads:
        return heads[0]
    return target


def _object_is_tag(obj_store, oid):
    entry = obj_store.get(oid)
    return entry is not None and entry[0] == "tag"


def _peel_to_commit(obj_store, tag_oid):
    """Follow tag -> target until a non-tag object; None when unresolvable."""
    visited = set()
    current_oid = tag_oid
    while True:
        entry = obj_store.get(current_oid)
        if entry is None:
            return None
        if entry[0] != "tag":
            return current_oid
        if current_oid in visited:
            return None
        visited.add(current_oid)
        peeled_target = None
        for raw_line in entry[1].split(b"\n"):
            if raw_line.startswith(b"object "):
                peeled_target = raw_line[7:].decode("ascii").strip()
                break
            if raw_line == b"":
                break
        if not peeled_target:
            return None
        current_oid = peeled_target


def _write_ref(target_repo, ref, sha):
    update = getattr(target_repo, "update_ref", None)
    if callable(update):
        update(ref, sha)
    else:
        target_repo.refs[ref] = sha


def _remove_ref(target_repo, ref):
    delete = getattr(target_repo, "delete_ref", None)
    if callable(delete):
        try:
            delete(ref)
            return
        except Exception:
            pass
    target_repo.refs.pop(ref, None)


def _advances_fast_forward(obj_store, old, new):
    """Both ends must be commits and old must be an ancestor of new."""
    old_entry = obj_store.get(old)
    new_entry = obj_store.get(new)
    if old_entry is None or new_entry is None:
        return False
    if old_entry[0] != "commit" or new_entry[0] != "commit":
        return False
    return is_ancestor(obj_store, old, new)


def _wrap_sideband(pack):
    """Pack bytes as sideband-1 pkt-lines (band byte 1 + chunk)."""
    out = bytearray()
    for start in range(0, len(pack), _SIDEBAND_SLICE):
        out += pkt_line(b"\x01" + pack[start:start + _SIDEBAND_SLICE])
    return bytes(out)


# -- info/refs --------------------------------------------------------------

def _v0_capability_line(target_repo, service, head_ref):
    if service == "git-receive-pack":
        caps = ["report-status", "side-band-64k", "delete-refs", "ofs-delta"]
    else:
        caps = ["side-band-64k", "thin-pack", "ofs-delta", "multi_ack_detailed", "shallow"]
    caps.append("symref=HEAD:%s" % head_ref)
    caps.append("agent=%s" % AGENT_IDENT)
    caps.append("object-format=sha1")
    return " ".join(caps)


def _v0_advertised_refs(target_repo):
    """[(oid, name)] in advertisement order: each ref, then its ^{} peeled
    line right after it (the client attaches a peel only to a preceding
    base line)."""
    out = []
    for ref in sorted(target_repo.refs):
        sha = target_repo.refs[ref]
        out.append((sha, ref))
        if _object_is_tag(target_repo.objects, sha):
            peeled = _peel_to_commit(target_repo.objects, sha)
            if peeled:
                out.append((peeled, ref + "^{}"))
    return out


def _build_v0_advertisement(target_repo, service):
    head_ref = _head_ref_target(target_repo)
    caps = _v0_capability_line(target_repo, service, head_ref)
    body = pkt_line(("# service=%s\n" % service).encode("utf-8")) + flush_pkt()
    head_sha = target_repo.refs.get(head_ref)
    ref_lines = _v0_advertised_refs(target_repo)
    if head_sha:
        body += pkt_line(("%s HEAD\x00%s\n" % (head_sha, caps)).encode("utf-8"))
        for oid, name in ref_lines:
            body += pkt_line(("%s %s\n" % (oid, name)).encode("utf-8"))
    elif ref_lines:
        first_oid, first_name = ref_lines[0]
        body += pkt_line(("%s %s\x00%s\n" % (first_oid, first_name, caps)).encode("utf-8"))
        for oid, name in ref_lines[1:]:
            body += pkt_line(("%s %s\n" % (oid, name)).encode("utf-8"))
    else:
        body += pkt_line(("%s capabilities^{}\x00%s\n" % (ZERO_OID, caps)).encode("utf-8"))
    body += flush_pkt()
    return body


def _build_v2_advertisement():
    body = pkt_line(b"version 2\n")
    for cap_line in _V2_CAPS:
        body += pkt_line((cap_line + "\n").encode("utf-8"))
    body += flush_pkt()
    return body


def handle_info_refs(repo, service, proto=None, headers=None):
    """GET /info/refs?service=<service>. proto is the Git-Protocol header
    value ("" when absent); headers is accepted as a fallback source."""
    if proto is None:
        proto = _lookup_header(headers, "git-protocol")
    if service == "git-upload-pack" and _wants_v2(proto):
        return 200, "application/x-git-upload-pack-advertisement", _build_v2_advertisement()
    if service == "git-receive-pack":
        return 200, "application/x-git-receive-pack-advertisement", _build_v0_advertisement(repo, service)
    if service == "git-upload-pack":
        return 200, "application/x-git-upload-pack-advertisement", _build_v0_advertisement(repo, service)
    return 404, "text/plain", b"unknown service"


# -- upload-pack ------------------------------------------------------------

def _split_v2_frames(body):
    """Split a v2 request body into (command_lines, arg_lines).

    Raises ValueError on a malformed pkt-line stream."""
    packets, _end = _scan_pktlines(body)
    command = []
    args = []
    seen_delim = False
    for kind, payload in packets:
        if kind == "delim":
            seen_delim = True
            continue
        if kind == "flush":
            break
        if kind != "data":
            continue
        line = payload.decode("utf-8", "replace").rstrip("\n")
        (args if seen_delim else command).append(line)
    return command, args


def _answer_ls_refs_v2(target_repo, args):
    peel = "peel" in args
    unborn = "unborn" in args
    prefixes = [a[len("ref-prefix "):] for a in args if a.startswith("ref-prefix ")]
    keep = lambda name: (not prefixes) or any(name.startswith(p) for p in prefixes)
    head_ref = _head_ref_target(target_repo)
    head_sha = target_repo.refs.get(head_ref)
    lines = []
    if not prefixes:
        if head_sha:
            lines.append("%s HEAD symref-target:%s\n" % (head_sha, head_ref))
        elif unborn:
            lines.append("unborn HEAD symref-target:%s\n" % head_ref)
    for ref in sorted(target_repo.refs):
        if not keep(ref):
            continue
        sha = target_repo.refs[ref]
        line = "%s %s" % (sha, ref)
        if peel and _object_is_tag(target_repo.objects, sha):
            peeled = _peel_to_commit(target_repo.objects, sha)
            if peeled:
                line += " peeled:%s" % peeled
        lines.append(line + "\n")
    body = b"".join(pkt_line(line.encode("utf-8")) for line in lines) + flush_pkt()
    return 200, "application/x-git-upload-pack-result", body


def _plan_fetch(target_repo, want_list, have_list, shallow_list, depth):
    """(send_set, new_shallow, new_unshallow) for a fetch.

    Closure: commits within `depth` generations of wants (or the full
    closure when depth is None), plus their trees/blobs. Held: what the
    client already has -- reachable from haves, stopping at the old
    shallow boundaries (a shallow client holds the boundary commit but
    not its parents). New shallow boundaries are the commits at exactly
    depth; old boundaries inside the closure that are no longer
    boundaries become unshallow."""
    obj_store = target_repo.objects
    if depth is not None:
        closure = collect_reachable(obj_store, want_list, max_commit_depth=depth)
        new_shallow = shallow_boundaries(obj_store, want_list, depth)
    else:
        closure = collect_reachable(obj_store, want_list)
        new_shallow = set()
    held = collect_reachable(obj_store, have_list, stop_at=set(shallow_list))
    send = closure - held
    unshallow = [s for s in shallow_list if s in closure and s not in new_shallow]
    return send, new_shallow, unshallow


def _serve_fetch_v2(target_repo, body):
    try:
        command, args = _split_v2_frames(body)
    except ValueError:
        return 400, "text/plain", b"malformed pkt-line stream"
    command_name = None
    for line in command:
        if line.startswith("command="):
            command_name = line[len("command="):]
    if command_name == "ls-refs":
        return _answer_ls_refs_v2(target_repo, args)
    if command_name != "fetch":
        return 400, "text/plain", b"unknown command"

    want_list, have_list, shallow_list, depth = [], [], [], None
    for line in args:
        if line.startswith("want "):
            oid = line[5:].strip()
            if len(oid) == 40:
                want_list.append(oid)
        elif line.startswith("have "):
            oid = line[5:].strip()
            if len(oid) == 40:
                have_list.append(oid)
        elif line.startswith("shallow "):
            oid = line[8:].strip()
            if len(oid) == 40:
                shallow_list.append(oid)
        elif line.startswith("deepen "):
            try:
                depth = int(line[7:].strip())
            except ValueError:
                pass
        # thin-pack / ofs-delta / no-progress / filter / done: accepted,
        # we always answer with a self-contained pack of full objects.
    send, new_shallow, unshallow = _plan_fetch(target_repo, want_list, have_list, shallow_list, depth)
    pack = build_packfile_from_objects(target_repo.objects, send)

    out = pkt_line(b"acknowledgments\n") + pkt_line(b"NAK\n") + delim_pkt()
    if depth is not None or shallow_list:
        out += pkt_line(b"shallow-info\n")
        for sha in sorted(new_shallow):
            out += pkt_line(("shallow %s\n" % sha).encode("utf-8"))
        for sha in sorted(unshallow):
            out += pkt_line(("unshallow %s\n" % sha).encode("utf-8"))
        out += delim_pkt()
    out += pkt_line(b"packfile\n")
    out += _wrap_sideband(pack)
    out += flush_pkt()
    return 200, "application/x-git-upload-pack-result", out


def _serve_fetch_v0(target_repo, body):
    try:
        packets, _end = _scan_pktlines(body)
    except ValueError:
        return 400, "text/plain", b"malformed pkt-line stream"
    # Two flush-separated sections: wants (with shallow/deepen), then
    # haves + done. The first want line carries capabilities
    # space-separated after the oid ("want <oid> side-band-64k ...").
    want_list, have_list, shallow_list, depth = [], [], [], None
    section = 0
    for kind, payload in packets:
        if kind == "flush":
            section += 1
            continue
        if kind != "data":
            continue
        line = payload.decode("utf-8", "replace").rstrip("\n")
        if line.startswith("want "):
            oid = line.split(" ")[1] if len(line.split(" ")) > 1 else ""
            if len(oid) == 40:
                want_list.append(oid)
        elif line.startswith("have "):
            oid = line[5:].strip()
            if len(oid) == 40:
                have_list.append(oid)
        elif line.startswith("shallow "):
            oid = line[8:].strip()
            if len(oid) == 40:
                shallow_list.append(oid)
        elif line.startswith("deepen "):
            try:
                depth = int(line[7:].strip())
            except ValueError:
                pass
        # done / deepen-since / deepen-not: accepted, no action needed.
    send, new_shallow, unshallow = _plan_fetch(target_repo, want_list, have_list, shallow_list, depth)
    pack = build_packfile_from_objects(target_repo.objects, send)

    out = pkt_line(b"NAK\n")
    for sha in sorted(new_shallow):
        out += pkt_line(("shallow %s\n" % sha).encode("utf-8"))
    for sha in sorted(unshallow):
        out += pkt_line(("unshallow %s\n" % sha).encode("utf-8"))
    out += _wrap_sideband(pack)
    out += flush_pkt()
    return 200, "application/x-git-upload-pack-result", out


def handle_upload_pack(repo, body, headers=None):
    """POST git-upload-pack. Protocol v2 when the Git-Protocol header says
    version=2, else v0."""
    if _wants_v2(_lookup_header(headers, "git-protocol")):
        return _serve_fetch_v2(repo, body)
    return _serve_fetch_v0(repo, body)


# -- receive-pack -----------------------------------------------------------

def handle_receive_pack(repo, body, headers=None):
    """POST git-receive-pack (v0 only). Parses the ref-update command list,
    applies the packfile first, then each fast-forward ref update; reports
    report-status (pkt-lined inside sideband band 1 when side-band-64k was
    negotiated)."""
    try:
        packets, end = _scan_pktlines(body)
    except ValueError:
        return 400, "text/plain", b"malformed pkt-line stream"
    commands = []  # (old, new, ref)
    capabilities = []
    for kind, payload in packets:
        if kind == "flush":
            break
        if kind != "data":
            continue
        line = payload.decode("utf-8", "replace").rstrip("\n")
        if "\x00" in line:
            line, caps = line.split("\x00", 1)
            capabilities = caps.split(" ")
        parts = line.split(" ")
        if len(parts) < 3:
            continue
        old_oid, new_oid, ref_name = parts[0], parts[1], parts[2]
        if len(old_oid) == 40 and len(new_oid) == 40 and ref_name.startswith("refs/"):
            commands.append((old_oid, new_oid, ref_name))
    if not commands:
        return 400, "text/plain", b"no ref commands"
    sideband = "side-band-64k" in capabilities

    pack_bytes = body[end:]
    unpack_ok = True
    unpack_detail = "ok"
    if pack_bytes:
        if len(pack_bytes) < 20 or pack_bytes[:4] != b"PACK":
            unpack_ok = False
            unpack_detail = "unpack error: not a packfile"
        else:
            try:
                parse_packfile(pack_bytes, repo.objects)
            except ValueError as err:
                unpack_ok = False
                unpack_detail = "unpack error: %s" % err

    results = []  # (ref, ok, reason)
    for old_oid, new_oid, ref_name in commands:
        if not unpack_ok:
            results.append((ref_name, False, "unpack failed"))
            continue
        current = repo.refs.get(ref_name)
        if new_oid == ZERO_OID:
            # Delete: allowed (delete-refs is always offered); the old
            # value must match the ref when one is given.
            if current is None:
                results.append((ref_name, False, "ref does not exist"))
            elif old_oid != ZERO_OID and old_oid != current:
                results.append((ref_name, False, "old value does not match"))
            else:
                _remove_ref(repo, ref_name)
                results.append((ref_name, True, ""))
            continue
        if current is None:
            # Create: old must be all zeros and the new object must exist.
            if old_oid != ZERO_OID:
                results.append((ref_name, False, "ref does not exist"))
            elif repo.objects.get(new_oid) is None:
                results.append((ref_name, False, "object not found"))
            else:
                _write_ref(repo, ref_name, new_oid)
                results.append((ref_name, True, ""))
            continue
        if old_oid != current:
            results.append((ref_name, False, "old value does not match"))
        elif new_oid == current:
            results.append((ref_name, True, ""))
        elif repo.objects.get(new_oid) is None:
            results.append((ref_name, False, "object not found"))
        elif _advances_fast_forward(repo.objects, current, new_oid):
            _write_ref(repo, ref_name, new_oid)
            results.append((ref_name, True, ""))
        else:
            results.append((ref_name, False, "non-fast-forward"))

    inner = pkt_line(("unpack %s\n" % unpack_detail).encode("utf-8"))
    for ref_name, ok, reason in results:
        if ok:
            inner += pkt_line(("ok %s\n" % ref_name).encode("utf-8"))
        else:
            inner += pkt_line(("ng %s %s\n" % (ref_name, reason)).encode("utf-8"))
    inner += flush_pkt()
    if sideband:
        out = _wrap_sideband(inner) + flush_pkt()
    else:
        out = inner
    return 200, "application/x-git-receive-pack-result", out
