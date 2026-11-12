"""`keepstack compact`: drop resources that were created and later deleted, without changing the final state.

Pairing is heuristic, so the result is always verified by replaying it into a throwaway emulator before the
journal is replaced (and the original is saved as snapshot `pre-compact`).
"""
import contextlib
import io
import json
import os
import re
import shutil
import socket
import sqlite3
import urllib.parse
from collections import namedtuple

from . import proxy, snapshot

Row = namedtuple("Row", "seq op method path headers body status resp")
PROTOCOL_PARAMS = {"action", "version"}
SCOPE_RE = re.compile(r"Credential=[^/,\s]+/[^/,\s]+/[^/,\s]+/([^/,\s]+)/aws4_request")
SQL = "select seq, op, method, path, headers, body, status, resp from journal order by seq"


def _b(x):
    return x.encode() if isinstance(x, str) else bytes(x or b"")


def _strings(node):
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for v in node.values():
            yield from _strings(v)
    elif isinstance(node, list):
        for v in node:
            yield from _strings(v)


def request_values(row):
    """Identifying values of a request: query/form parameters or JSON string values, minus protocol params."""
    split = urllib.parse.urlsplit(row.path)
    body = _b(row.body).decode("utf-8", "replace")
    vals = [v for k, vs in urllib.parse.parse_qs(split.query).items() if k.lower() not in PROTOCOL_PARAMS for v in vs]
    try:
        vals += list(_strings(json.loads(body)))
    except ValueError:
        vals += [v for k, vs in urllib.parse.parse_qs(body).items() if k.lower() not in PROTOCOL_PARAMS for v in vs]
    # id-like values or names; short words, numbers and booleans would match unrelated rows
    return [v for v in dict.fromkeys(vals) if len(v) >= 3 and not v.isdigit() and v.lower() not in ("true", "false")]


def request_bytes(row):
    raw = row.path.encode() + b"\n" + _b(row.body)
    return raw + b"\n" + urllib.parse.unquote_to_bytes(raw)  # form-encoded values (%3A, %2F...) match too


def foreign_ids(row, own):
    """Id-like tokens of a request that are not among `own` (tokens of the resource's identifiers)."""
    decoded = urllib.parse.unquote_to_bytes(row.path.encode() + b"\n" + _b(row.body))
    return {t for t in proxy.TOKEN_RE.findall(decoded) if proxy.id_like(t)} - own


def copy_source(row):
    try:
        h = json.loads(row.headers)
        return next((urllib.parse.unquote(v).strip("/") for k, v in h.items() if k.lower() == "x-amz-copy-source"), None)
    except (ValueError, AttributeError, TypeError):
        return None


def contains(hay, value):
    return re.search(rb"(?<![A-Za-z0-9_-])" + re.escape(value.encode()) + rb"(?![A-Za-z0-9_-])", hay) is not None


def service(row):
    try:
        h = json.loads(row.headers)
        m = SCOPE_RE.search(next((v for k, v in h.items() if k.lower() == "authorization"), ""))
    except (ValueError, AttributeError, TypeError):
        m = None
    return m.group(1) if m else None


def _short(v):
    return v if len(v) <= 24 else v[:21] + "..."


def compact_rows(rows):
    """Returns (kept rows, groups, shared). A group is {"label", "seqs"} for one removed resource (or
    "unconfirmed"); `shared` lists resources left alone because a row to drop also touches values that may survive."""
    groups, shared = [], []
    live = {r.seq: r for r in rows if 200 <= (r.status or 0) < 300}
    dropped = [r.seq for r in rows if r.seq not in live]
    if dropped:
        groups.append({"label": "unconfirmed calls (never got a 2xx response)", "seqs": dropped})

    # RPC style: DeleteX pairs with the latest earlier CreateX that mentions all of its identifying values.
    for d in [r for r in live.values() if (r.op or "").startswith("Delete")]:
        if d.seq not in live:
            continue
        noun, idents = d.op[6:], request_values(d)
        if not idents:
            continue
        for c in reversed([r for r in live.values() if r.op == "Create" + noun and r.seq < d.seq]):
            hay, resp = request_bytes(c), _b(c.resp)
            if all(contains(hay, v) or contains(resp, v) for v in idents):
                break
        else:
            continue
        # Values present in the create response (generated IDs, names it confirms) say which rows belong to
        # the resource; a Create that does not confirm any (CreateTags) is not a resource.
        keys = [v for v in idents if contains(resp, v)]
        if not keys:
            continue
        seqs = [c.seq, d.seq]
        for r in live.values():
            if c.seq < r.seq < d.seq:
                rb = request_bytes(r)
                # a plain name (not an ID) could also appear in another service: require the same one
                if any(contains(rb, k) and (proxy.id_like(k.encode()) or service(r) == service(c)) for k in keys):
                    seqs.append(r.seq)
        own = {t for v in idents for t in proxy.TOKEN_RE.findall(v.encode())}
        if any(foreign_ids(live[s], own) for s in seqs if s not in (c.seq, d.seq)):
            shared.append(f"{_short(keys[0])} ({c.op} ... {d.op})")
            continue
        for s in seqs:
            live.pop(s)
        groups.append({"label": f"{_short(keys[0])} ({c.op} ... {d.op}, {len(seqs)} calls)", "seqs": sorted(seqs)})

    # REST style: DELETE on path P cancels earlier PUT/POST on exactly P (a bucket also takes what is inside it).
    versioned = {r.path.split("?")[0].strip("/").split("/")[0] for r in live.values()
                 if r.method == "PUT" and r.path.split("?")[1:] == ["versioning"]}
    for d in [r for r in live.values() if r.method == "DELETE" and not r.op]:
        if d.seq not in live or d.path.strip("/").split("/")[0] in versioned:
            continue
        bucket_like = d.path.count("/") == 1 and "?" not in d.path

        inside = (d.path + "/", d.path + "?") if bucket_like else ()
        seqs = [r.seq for r in live.values() if r.seq < d.seq and not r.op and r.method in ("PUT", "POST", "DELETE")
                and (r.path == d.path or r.path.startswith(inside))]
        if not any(live[s].method != "DELETE" for s in seqs):
            continue
        # copy-source links objects across paths: dropping either end could change a surviving object
        res = d.path.strip("/")
        if any(copy_source(live[s]) for s in seqs) or any(
                (copy_source(r) or "").startswith(res) for r in live.values() if r.seq not in seqs):
            shared.append(f"{d.path} (PUT ... DELETE)")
            continue
        seqs.append(d.seq)
        for s in seqs:
            live.pop(s)
        groups.append({"label": f"{d.path} (PUT ... DELETE, {len(seqs)} calls)", "seqs": sorted(seqs)})
    return [r for r in rows if r.seq in live], groups, shared


def payload(rows):
    return sum(len(_b(r.path)) + len(_b(r.headers)) + len(_b(r.body)) + len(_b(r.resp)) for r in rows)


def free_ports(n):
    start = int(os.environ.get("KEEPSTACK_COMPACT_VERIFY_PORT", "0"))
    found, socks = [], []
    try:
        for p in range(start, start + 200) if start else [0] * n:
            s = socket.socket()
            try:
                s.bind(("127.0.0.1", p))
            except OSError:
                s.close()
                continue
            socks.append(s)
            found.append(s.getsockname()[1])
            if len(found) == n:
                return found
    finally:
        for s in socks:
            s.close()
    raise snapshot.SnapshotError("could not find free ports to verify the result")


def verify(data_dir, image):
    """Replays the journal of `data_dir` into a throwaway emulator. Returns the list of failed calls."""
    port, emu = free_ports(2)
    ns = type("Args", (), {"data_dir": data_dir, "port": port, "emulator_port": emu, "image": image,
                           "verbose": False, "shims": False, "reset": False, "detach": False})()
    proxy.ensure_image(image)
    app = proxy.App(ns)
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            app.boot()
    finally:
        app.stop()
    return [ln for ln in buf.getvalue().splitlines() if "REPLAY FAILED" in ln]


def report(rows, kept, groups, shared, dry):
    gone = {r.seq for r in rows} - {r.seq for r in kept}
    saved = payload([r for r in rows if r.seq in gone])
    print(f"Journal: {len(rows)} calls -> {len(kept)} calls ({len(gone)} {'would be ' if dry else ''}removed, "
          f"~{saved / 1024:.1f} KB of request/response data)")
    for g in groups:
        print(f"  {g['label']}")
    if shared:
        print("Kept (shared with other resources, not compacted):")
        for label in shared:
            print(f"  {label}")


def cmd_compact(args):
    data_dir = os.path.abspath(args.data_dir)
    try:
        if not args.dry_run:
            snapshot.require_stopped(data_dir)
        jpath = snapshot.journal_path(data_dir)
        snapshot.count_calls(jpath)
        db = sqlite3.connect(jpath, timeout=30)
        try:
            rows = [Row(*r) for r in db.execute(SQL)]
        finally:
            db.close()
        kept, groups, shared = compact_rows(rows)
        report(rows, kept, groups, shared, args.dry_run)
        kept_seqs = {r.seq for r in kept}
        if args.dry_run or len(kept) == len(rows):
            print("Nothing to do." if len(kept) == len(rows) else "Dry run: nothing was changed.")
            return 0
        snapshot.save_snapshot(data_dir, "pre-compact", force=True)
        print("Original journal saved as snapshot 'pre-compact'. Verifying the compacted journal in a throwaway "
              "emulator...")
        tmp = os.path.join(data_dir, "compact.tmp")
        shutil.rmtree(tmp, ignore_errors=True)
        os.makedirs(tmp)
        try:
            snapshot.backup(jpath, os.path.join(tmp, "journal.db"))
            tdb = sqlite3.connect(os.path.join(tmp, "journal.db"))
            try:
                with tdb:
                    tdb.executemany("delete from journal where seq=?", [(r.seq,) for r in rows if r.seq not in kept_seqs])
            finally:
                tdb.close()
            failed = verify(tmp, args.image)
            if failed:
                print("Verification failed: the compacted journal does not replay cleanly. "
                      "The original journal was kept.")
                print("\n".join(f"  {ln}" for ln in failed[:10]))
                return 1
            tdb = sqlite3.connect(os.path.join(tmp, "journal.db"))
            try:
                with tdb:
                    tdb.execute("delete from meta where key='last_restore'")  # it describes the throwaway run
                tdb.execute("vacuum")
            finally:
                tdb.close()
            before = os.path.getsize(jpath)
            snapshot.replace_journal(data_dir, os.path.join(tmp, "journal.db"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        print(f"Verified and applied: journal file {before / 1024:.1f} KB -> "
              f"{os.path.getsize(jpath) / 1024:.1f} KB. Undo with: keepstack snapshot restore pre-compact")
        return 0
    except (snapshot.SnapshotError, RuntimeError) as e:
        print(f"keepstack: {e}")
        return 1
