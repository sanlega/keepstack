"""Read-only web dashboard served by keepstack at http://127.0.0.1:<port>/__keepstack/.

Contract used by proxy.App.handle(): every request whose path starts with PREFIX is passed to
handle(app, method, path, headers) and must return (status, reason, [(header, value), ...], body_bytes).
Nothing here writes to the journal or the emulator. Modules are used as `compact.X` (not `from ... import`)
because proxy imports this module while compact imports proxy.
"""
import json
import os
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

from . import __version__, compact, daemon, proxy, snapshot

PREFIX = "/__keepstack"
API = PREFIX + "/api/"
DETAIL_LIMIT = 64 * 1024
ID_LIMIT = 1000
SECURITY_HEADERS = [
    ("Content-Security-Policy", ("default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
                                "connect-src 'self'; img-src data:; base-uri 'none'; form-action 'none'; "
                                "frame-ancestors 'none'")),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    ("Cache-Control", "no-store"),
]
# EC2-style ids, ARNs and URLs in a create response: what the client sees as the resource's identity.
ID_TOKEN_RE = re.compile(rb"\b(?:[a-z]{2,8}-[0-9a-f]{8,}|arn:aws[a-z-]*:[^\s\"'<>,]+|https?://(?![^\s\"'<>,]*/doc/)[^\s\"'<>,]+)")
_cache = {}  # db path -> (version key, derived data): the page polls every few seconds


def _reply(status, reason, ctype, body):
    if not isinstance(body, bytes):
        body = json.dumps(body).encode()
    return status, reason, [("Content-Type", ctype)] + SECURITY_HEADERS, body


def _error(status, reason, msg):
    return _reply(status, reason, "application/json; charset=utf-8", {"error": msg})


def _ro(db_path):
    return sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True, timeout=5)


def _version_key(db):
    return db.execute("select count(*), max(seq), sum(status) from journal").fetchone()


def _text(data, limit=None):
    raw = data.encode() if isinstance(data, str) else bytes(data or b"")
    cut = limit is not None and len(raw) > limit
    return raw[:limit].decode("utf-8", "replace") if cut else raw.decode("utf-8", "replace"), cut


# --- derived data --------------------------------------------------------------------------------

NAME_FIELD_RE = re.compile(rb'"(\w*Name)"\s*:\s*"([^"]+)"|(?:^|&)(\w*Name)=([^&]+)')
ARN_RE = re.compile(rb"arn:aws[\w-]*:[a-z0-9-]+:[\w-]*:\d*:[^\s\"<>&]+")


def _identifiers(row, kind):
    resp = compact._b(row.resp)
    found = [m.decode("utf-8", "replace") for m in ID_TOKEN_RE.findall(resp)]
    own = [t for t in found if t.startswith(kind.lower() + "-")]  # subnet-... for a Subnet, not its vpc-...
    # Prefer explicit *Name fields (TableName, QueueName...) over arbitrary values: a description or an
    # enum such as HASH / PAY_PER_REQUEST is not an identifier.
    names = []
    name_keys = {f"{kind}name".lower(), "name"}  # TableName for a Table, QueueName for a Queue, not AttributeName
    for m in NAME_FIELD_RE.finditer(compact.request_bytes(row)):
        key = (m.group(1) or m.group(3)).decode().lower()
        value = urllib.parse.unquote_plus((m.group(2) or m.group(4)).decode("utf-8", "replace"))
        if key in name_keys and compact.contains(resp, value):
            names.append(value)
    arn = ARN_RE.search(resp)
    arns = [arn.group(0).decode("utf-8", "replace")] if arn else []
    if not (own or names or arns):  # no explicit name: fall back to confirmed request values, minus enums
        names = [v for v in compact.request_values(row) if compact.contains(resp, v)
                 and not re.fullmatch(r"[\d./:]+|[a-z]{2,8}-[0-9a-f]{8,}|[A-Z][A-Z0-9_]*", v)]
    return list(dict.fromkeys(own[:1] + names + arns + ([] if own or names or arns else found)))[:3]


def _compute_resources(db):
    rows = [compact.Row(*r) for r in db.execute(compact.SQL)]
    ts = dict(db.execute("select seq, ts from journal"))
    kept, _, _ = compact.compact_rows(rows)  # whatever compact would drop was created and deleted
    kept = [r for r in kept if 200 <= (r.status or 0) < 300]
    blobs = {r.seq: compact.request_bytes(r) for r in kept}
    out = []
    for c in kept:
        if c.op and c.op.startswith("Create"):
            kind = c.op[6:]
        elif not c.op and c.method == "PUT" and re.fullmatch(r"/[^/?]+/?", c.path):
            kind = "Bucket"
        else:
            continue
        ids = [c.path.strip("/")] if kind == "Bucket" else _identifiers(c, kind)
        if not ids:
            continue  # a Create that does not name anything (CreateTags) is not a resource
        svc = daemon.service_of(c.headers)
        calls = 1 + sum(1 for r in kept if r.seq > c.seq and any(
            compact.contains(blobs[r.seq], k) and (proxy.id_like(k.encode()) or daemon.service_of(r.headers) == svc)
            for k in ids))
        out.append({"service": svc, "kind": kind, "ids": ids, "created": ts.get(c.seq), "calls": calls, "seq": c.seq})
    return out


def resources(app):
    with _ro(app.db_path) as db:
        key = _version_key(db)
        hit = _cache.get(app.db_path)
        if not hit or hit[0] != key:
            hit = _cache[app.db_path] = (key, _compute_resources(db))
    return hit[1]


def status(app):
    data_dir = os.path.abspath(app.args.data_dir)
    run = daemon.read_run(data_dir) or {}
    j = daemon.journal_stats(data_dir) or {"calls": 0, "services": {}, "size_bytes": 0, "last_restore": None}
    try:
        with _ro(app.db_path) as db:
            row = db.execute("select value from meta where key='shims'").fetchone()
        shims = bool(row and row[0] == "1")
    except sqlite3.Error:
        shims = False
    started = run.get("started")
    return {"state": "running" if app.ready.is_set() else "restoring", "port": app.args.port,
            "uptime_seconds": int(time.time() - started) if started else None, "version": __version__,
            "data_dir": data_dir, "last_restore": j["last_restore"], "shims": shims,
            "journal": {"calls": j["calls"], "size_bytes": j["size_bytes"]}, "services": j["services"]}


def journal(app, query):
    def num(name, default, hi):
        try:
            return max(0, min(int(query.get(name, [default])[0]), hi))
        except ValueError:
            return default
    limit, offset = max(1, num("limit", 100, 500)), num("offset", 0, 10**9)
    want = (query.get("service") or [""])[0]
    with _ro(app.db_path) as db:
        rows = db.execute("select seq, ts, op, method, path, headers, status from journal order by seq desc").fetchall()
    items = [{"seq": seq, "ts": ts, "service": daemon.service_of(h), "op": op, "method": method, "path": path,
              "label": op or f"{method} {path}", "status": status}
             for seq, ts, op, method, path, h, status in rows]
    if want:
        items = [i for i in items if i["service"] == want]
    return {"total": len(items), "limit": limit, "offset": offset, "rows": items[offset:offset + limit]}


def journal_detail(app, seq):
    with _ro(app.db_path) as db:
        row = db.execute("select seq, ts, op, method, path, headers, status, body, resp from journal where seq=?",
                         (seq,)).fetchone()
    if not row:
        return None
    seq, ts, op, method, path, headers, st, body, resp = row
    (req, req_cut), (res, res_cut) = _text(body, DETAIL_LIMIT), _text(resp, DETAIL_LIMIT)
    return {"seq": seq, "ts": ts, "service": daemon.service_of(headers), "op": op, "method": method, "path": path,
            "status": st, "request": req, "request_truncated": req_cut,
            "response": res, "response_truncated": res_cut}


def ids(app):
    for _ in range(3):  # the replay may add entries while we copy
        try:
            pairs = list(app.ids.to_new.items())
            break
        except RuntimeError:
            pairs = []
    rows = [{"original": a.decode("utf-8", "replace"), "current": b.decode("utf-8", "replace")}
            for a, b in pairs[:ID_LIMIT]]
    return {"total": len(pairs), "rows": rows}


def snapshots(app):
    out = []
    for name, m in snapshot.list_snapshots(os.path.abspath(app.args.data_dir)):
        out.append({"name": name, "created": m.get("created"), "calls": m.get("calls"),
                    "files": [f["path"] for f in m.get("files", [])]})
    return {"snapshots": out}


# --- HTTP ----------------------------------------------------------------------------------------

def handle(app, method, path, headers):
    host = next((v for k, v in headers.items() if k.lower() == "host"), "")
    if host.lower() not in (f"127.0.0.1:{app.args.port}", f"localhost:{app.args.port}"):
        return _error(403, "Forbidden", "unexpected Host header")  # DNS rebinding defence
    if method != "GET":
        return _error(405, "Method Not Allowed", "the dashboard is read-only")
    split = urllib.parse.urlsplit(path)
    route, query = split.path, urllib.parse.parse_qs(split.query)
    try:
        if route == PREFIX:
            return 301, "Moved Permanently", [("Location", PREFIX + "/")] + SECURITY_HEADERS, b""
        if route == PREFIX + "/":
            return _reply(200, "OK", "text/html; charset=utf-8", (Path(__file__).parent / "dashboard.html").read_bytes())
        if route.startswith(API):
            name = route[len(API):]
            simple = {"status": status, "resources": lambda a: {"resources": resources(a)}, "ids": ids,
                      "snapshots": snapshots}
            if name in simple:
                return _reply(200, "OK", "application/json; charset=utf-8", simple[name](app))
            if name == "journal":
                return _reply(200, "OK", "application/json; charset=utf-8", journal(app, query))
            if re.fullmatch(r"journal/\d+", name):
                d = journal_detail(app, int(name.split("/")[1]))
                if d:
                    return _reply(200, "OK", "application/json; charset=utf-8", d)
    except (sqlite3.Error, OSError) as e:
        return _error(500, "Internal Server Error", str(e))
    return _error(404, "Not Found", "no such route")


def cmd_ui(args):
    url = f"http://127.0.0.1:{args.port}{PREFIX}/"
    try:
        direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # never via an HTTP(S)_PROXY
        with direct.open(url + "api/status", timeout=3) as r:
            ok = r.status == 200
    except (urllib.error.URLError, OSError):
        ok = False
    if not ok:
        print(f"keepstack is not reachable on port {args.port}. Start it first with `keepstack up -d`.")
        return 1
    print(url)
    if not args.no_browser:
        webbrowser.open(url)
    return 0
