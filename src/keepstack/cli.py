"""keepstack: persistent state for AWS Emulator.

A proxy in front of the emulator. It stores every successful write call in SQLite
and, when the emulator starts (or restarts), replays them in order.
Values the server generates at random (IDs, ARNs, UUIDs, receipt handles...)
differ on every replay, so the proxy compares the original response with the new
one, learns what changed and translates it in both directions: clients always see
the original values.

Standard library only. Requires Docker; the emulator image is built automatically on first run.
"""
import argparse
import base64
import difflib
import hashlib
import http.client
import json
import os
import random
import re
import signal
import sqlite3
import ssl
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__

READ_PREFIXES = ("Describe", "Get", "List", "Head", "Query", "Scan", "BatchGet", "Search",
                 "Select", "Lookup", "Check", "Validate", "Estimate", "Preview", "Simulate")
# Reads that change state (visibility, receipt handles): journaled only if they return something,
# and without the write lock so a 20 s long-poll does not block other writes.
CONSUMING_OPS = {"ReceiveMessage"}
HOP = {"connection", "keep-alive", "transfer-encoding", "te", "trailer", "upgrade",
       "proxy-authorization", "proxy-connection", "content-length", "expect", "accept-encoding"}
RESP_SKIP = HOP | {"server", "date"}  # send_response already sets them

TOKEN_RE = re.compile(rb"[A-Za-z0-9_-]+")
B64_RE = re.compile(rb"[A-Za-z0-9+/]{16,}={0,2}")  # base64 blobs: learned whole, not piecewise
TIMELIKE_RE = re.compile(rb"[0-9TZEe.+-]+")     # dates, times, epochs, numbers (including 1.7E9)
REQUEST_ID_KEYS = {b"requestid", b"request_id", b"x-amz-request-id", b"x-amzn-requestid"}
READY_TIMEOUT = 300
REPLAY_RETRY_SECONDS = 30  # replay is faster than AWS: e.g. PutItem before the table is ACTIVE
MAX_ALIGN_TOKENS = 4000    # difflib is quadratic: above this, match by position only


def log(msg):
    print(f"[keepstack] {msg}", flush=True)


def hget(headers, name):
    name = name.lower()
    return next((v for k, v in headers.items() if k.lower() == name), None)


def operation(path, headers, body):
    """AWS operation name, or None for REST services (S3, Lambda...)."""
    target = hget(headers, "X-Amz-Target")
    if target:
        return target.rsplit(".", 1)[-1]
    split = urllib.parse.urlsplit(path)
    m = re.search(r"/operation/([A-Za-z0-9]+)/?$", split.path)  # Smithy RPC v2
    if m:
        return m.group(1)
    action = urllib.parse.parse_qs(split.query).get("Action")
    if not action and "x-www-form-urlencoded" in (hget(headers, "Content-Type") or ""):
        action = urllib.parse.parse_qs(body.decode("utf-8", "replace")).get("Action")
    return action[0] if action else None


def is_write(method, op):
    if op:
        return op in CONSUMING_OPS or not op.startswith(READ_PREFIXES)
    return method not in ("GET", "HEAD", "OPTIONS")


def id_like(tok):
    """Does this look like a generated value (ID, random suffix) rather than a word, a status or a date?"""
    if len(tok) < 6 or TIMELIKE_RE.fullmatch(tok):
        return False
    if re.search(rb"[0-9]", tok) and re.search(rb"[A-Za-z]", tok):
        return True  # vpc-0abc..., UUIDs, Z0123ABC, mp67t4dbd4
    tail = tok.rsplit(b"-", 1)[-1]  # random Secrets Manager ARN suffix: db-pass-AbCdEf
    return b"-" in tok and len(tail) >= 6 and tail.lower() != tail and tail.upper() != tail


def b64_blobs(data):
    return [m for m in B64_RE.findall(data) if b"+" in m or m.endswith(b"=")]


class IdMap:
    """Translates original values (the ones clients know) <-> values of the current emulator."""

    def __init__(self):
        self.to_new, self.to_orig = {}, {}
        self._re = None

    def learn(self, orig_resp, new_resp):
        """Pairs tokens of the stored response with those of the replayed one. Returns how many it learned."""
        orig, new = orig_resp or b"", new_resp or b""
        learned = 0
        ba, bb = b64_blobs(orig), b64_blobs(new)
        if len(ba) == len(bb):
            for x, y in zip(ba, bb):
                learned += self._learn_pair(x, y)
            for blob in ba:
                orig = orig.replace(blob, b" ")
            for blob in bb:
                new = new.replace(blob, b" ")
        a, b = TOKEN_RE.findall(orig), TOKEN_RE.findall(new)
        if len(a) == len(b):
            pairs = [(i, i) for i in range(len(a))]
        elif len(a) + len(b) <= MAX_ALIGN_TOKENS:
            # Slightly different structure (e.g. one more optional field). In the differing spans only
            # values preceded by the same key are paired: never loose words blindly.
            pairs = []
            for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
                if tag != "replace":
                    continue
                free = list(range(max(j1, 1), j2))
                for i in range(max(i1, 1), i2):
                    j = next((j for j in free if b[j - 1] == a[i - 1]), None)
                    if j is not None:
                        pairs.append((i, j))
                        free.remove(j)
        else:
            pairs = []
        for i, j in pairs:
            if not (i and a[i - 1].lower() in REQUEST_ID_KEYS) and id_like(a[i]) and id_like(b[j]):
                learned += self._learn_pair(a[i], b[j])
        return learned

    def _learn_pair(self, x, y):
        if x == y or x in self.to_new or y in self.to_orig:
            return 0  # identical, or already known (e.g. another resource's VpcId appearing in this response)
        self._add(x, y)
        return 1

    def _add(self, orig, new):
        self.to_new[orig], self.to_orig[new] = new, orig
        self._re = None

    def _alias(self, tok):
        """New emulator value that collides with an original of ANOTHER resource: give it a unique alias."""
        rnd = random.Random(tok)
        cut = tok.rfind(b"-") + 1  # keep the prefix (igw-, vpc-...) and change only the variable part
        shape = tok
        while True:
            for _ in range(100):
                alias = shape[:cut] + bytes(
                    ord(rnd.choice("0123456789abcdef" if chr(c) in "0123456789abcdef" else
                                   "ABCDEFGHIJKLMNOPQRSTUVWXYZ" if chr(c).isupper() else
                                   "abcdefghijklmnopqrstuvwxyz")) if chr(c).isalnum() else c
                    for c in shape[cut:])
                if alias not in self.to_new and alias not in self.to_orig:
                    self._add(alias, tok)
                    return alias
            shape += b"0"  # variable part too short to find a free slot: lengthen it

    def _pattern(self):
        if self._re is None:
            toks = sorted(set(self.to_new) | set(self.to_orig), key=len, reverse=True)
            # boundary: no alphanumeric or _/- before or after; also after %2F, %3A... (URL-encoded)
            self._re = re.compile(rb"(?:(?<![A-Za-z0-9_-])|(?<=%[0-9A-Fa-f]{2}))(?:" +
                                  b"|".join(re.escape(t) for t in toks) + rb")(?![A-Za-z0-9_-])") if toks else False
        return self._re

    def to_emulator(self, data):
        pat = self._pattern()
        if not pat or not data:
            return data
        return pat.sub(lambda m: self.to_new.get(m.group(0), m.group(0)), data)

    def to_client(self, data):
        pat = self._pattern()
        if not pat or not data:
            return data

        def repl(m):
            tok = m.group(0)
            if tok in self.to_orig:
                return self.to_orig[tok]
            if tok in self.to_new:  # collision: the emulator reused a value the client associates with another resource
                return self._alias(tok)
            return tok
        return pat.sub(repl, data)


def fix_body_headers(headers, body):
    """If the proxy changed the body, recompute the headers that summarize it."""
    h = dict(headers)
    for k in list(h):
        lk = k.lower()
        if lk == "x-amz-content-sha256" and re.fullmatch(r"[0-9a-f]{64}", h[k]):
            h[k] = hashlib.sha256(body).hexdigest()
        elif lk == "content-md5":
            h[k] = base64.b64encode(hashlib.md5(body).digest()).decode()
    return h


class App:
    def __init__(self, args):
        self.args = args
        self.ids = IdMap()
        self.lock = threading.RLock()       # serializes writes and the replay
        self.ready = threading.Event()      # cleared while restarting/replaying
        self.stopping = False
        self.container = f"keepstack-{args.port}"  # one per instance: allows several in parallel
        os.makedirs(args.data_dir, exist_ok=True)
        self.db_path = os.path.join(args.data_dir, "journal.db")
        with self.db() as db:
            db.execute("""create table if not exists journal(
                seq integer primary key autoincrement, ts real, op text, method text,
                path text, headers text, body blob, status int, resp blob)""")
        self.ctx = ssl.create_default_context()
        self.ctx.check_hostname = False
        self.ctx.verify_mode = ssl.CERT_NONE  # the emulator's self-signed certificate, only on 127.0.0.1

    def db(self):
        return sqlite3.connect(self.db_path, timeout=30)

    def reset(self):
        with self.db() as db:
            db.execute("delete from journal")
        log("journal cleared (--reset)")

    # --- emulator -----------------------------------------------------------
    def docker(self, *a, check=True):
        return subprocess.run(["docker", *a], capture_output=True, text=True, check=check)

    def start_emulator(self):
        self.docker("rm", "-f", self.container, check=False)
        self.docker("run", "-d", "--rm", "--name", self.container, "--memory=2g", "--memory-swap=2g",
                    "-p", f"127.0.0.1:{self.args.emulator_port}:9297", self.args.image, "--auth", "off")
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                if self.forward("GET", "/__admin/health", {}, b"")[0] == 200:
                    return
            except OSError:
                pass
            time.sleep(0.5)
        raise RuntimeError("the emulator did not answer /__admin/health within 60 s")

    def boot(self):
        """Starts a clean emulator and replays the journal. Blocks traffic meanwhile."""
        self.ready.clear()
        with self.lock:
            self.start_emulator()
            self.ids = IdMap()
            ok = fail = 0
            with self.db() as db:
                rows = db.execute("select seq, op, method, path, headers, body, status, resp from journal "
                                  "order by seq").fetchall()
            for seq, op, method, path, headers, body, status, resp in rows:
                name = op or method
                if not 200 <= status < 300:
                    log(f"warning: #{seq} {name} was never confirmed (keepstack was interrupted mid-call); skipping")
                    continue
                deadline, delay = time.time() + REPLAY_RETRY_SECONDS, 0.25
                while True:
                    try:
                        st, data = self.send(method, path, json.loads(headers), body)
                    except Exception as e:  # noqa: BLE001 - one broken row must not prevent restoring the rest
                        st, data = 0, repr(e).encode()
                    if 200 <= st < 300 or time.time() > deadline:
                        break
                    time.sleep(delay)
                    delay = min(delay * 2, 2)
                if 200 <= st < 300:
                    ok += 1
                    self.ids.learn(resp, data)
                else:
                    fail += 1
                    log(f"REPLAY FAILED #{seq} {name} {path}: HTTP {st} {data[:200]!r}")
            log(f"state restored: {ok} calls replayed, {fail} failed, {len(self.ids.to_new)} values translated")
        self.ready.set()

    def watch(self):
        """If the container dies (Docker restarted, OOM...), bring it back up with the state restored."""
        while not self.stopping:
            self.docker("wait", self.container, check=False)
            if self.stopping:
                return
            log("the emulator stopped; restarting it and restoring state")
            try:
                self.boot()
            except Exception as e:  # noqa: BLE001 - keep watching even if one boot fails
                log(f"error restarting: {e}; retrying in 5 s")
                self.docker("rm", "-f", self.container, check=False)  # otherwise `docker wait` would block forever
                time.sleep(5)

    def stop(self):
        self.stopping = True
        self.docker("rm", "-f", self.container, check=False)

    # --- traffic ------------------------------------------------------------
    def forward(self, method, path, headers, body):
        conn = http.client.HTTPSConnection("127.0.0.1", self.args.emulator_port, context=self.ctx, timeout=300)
        try:
            conn.request(method, path, body=body or None, headers=headers)
            r = conn.getresponse()
            return r.status, r.reason, r.getheaders(), r.read()
        finally:
            conn.close()

    def send(self, method, path, headers, body):
        """Translates the request to the current emulator's values and sends it. Returns (status, untranslated body)."""
        new_body = self.ids.to_emulator(body)
        if new_body != body:
            headers = fix_body_headers(headers, new_body)
        status, _, _, data = self.forward(method, self.ids.to_emulator(path.encode()).decode(), headers, new_body)
        return status, data

    def handle(self, method, path, headers, body):
        op = operation(path, headers, body)
        write = is_write(method, op)
        if not self.ready.wait(READY_TIMEOUT):
            return 503, "Service Unavailable", [], b"keepstack: the emulator is not finishing startup"
        locked = write and op not in CONSUMING_OPS
        if locked:
            self.lock.acquire()
        try:
            seq = None
            if locked:
                # Journal first, emulator second: if SQLite fails, the client gets an error
                # and the emulator is untouched. A write is never applied but unsaved.
                with self.db() as db:
                    seq = db.execute("insert into journal(ts, op, method, path, headers, body, status) "
                                     "values(?,?,?,?,?,?,0)",
                                     (time.time(), op, method, path, json.dumps(headers), body)).lastrowid
            new_body = self.ids.to_emulator(body)
            fwd_headers = fix_body_headers(headers, new_body) if new_body != body else headers
            try:
                status, reason, resp_headers, data = self.forward(
                    method, self.ids.to_emulator(path.encode()).decode(), fwd_headers, new_body)
            except Exception:
                if seq:
                    with self.db() as db:
                        db.execute("delete from journal where seq=?", (seq,))
                raise
            data = self.ids.to_client(data)
            ok = 200 <= status < 300
            if write:
                with self.db() as db:
                    if seq and ok:
                        db.execute("update journal set status=?, resp=? where seq=?", (status, data, seq))
                    elif seq:
                        db.execute("delete from journal where seq=?", (seq,))
                    elif ok and b"ReceiptHandle" in data:  # ReceiveMessage that returned messages
                        db.execute("insert into journal(ts, op, method, path, headers, body, status, resp) "
                                   "values(?,?,?,?,?,?,?,?)",
                                   (time.time(), op, method, path, json.dumps(headers), body, status, data))
            return status, reason, resp_headers, data
        finally:
            if locked:
                self.lock.release()


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *a):
            if app.args.verbose:
                log(fmt % a)

        def read_body(self):
            if (self.headers.get("Transfer-Encoding") or "").lower() == "chunked":
                out = bytearray()
                while True:
                    size = int(self.rfile.readline().split(b";")[0], 16)
                    if size == 0:
                        while self.rfile.readline() not in (b"\r\n", b"\n", b""):
                            pass
                        return bytes(out)
                    out += self.rfile.read(size)
                    self.rfile.readline()
            n = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(n) if n else b""

        def reply(self, status, reason, resp_headers, data):
            self.send_response(status, reason)
            upstream_len = None
            for k, v in resp_headers:
                if k.lower() == "content-length":
                    upstream_len = v
                elif k.lower() not in RESP_SKIP:
                    self.send_header(k, v)
            # HEAD has no body, but its Content-Length (e.g. an S3 object's size) matters
            self.send_header("Content-Length", upstream_len if self.command == "HEAD" and upstream_len else str(len(data)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def any(self):
            try:
                body = self.read_body()
            except ValueError:
                self.close_connection = True
                return self.reply(400, "Bad Request", [], b"keepstack: malformed request body")
            headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP}
            try:
                result = app.handle(self.command, self.path, headers, body)
            except Exception as e:  # noqa: BLE001 - return 502 instead of hanging the client
                log(f"error forwarding {self.command} {self.path}: {e}")
                result = 502, "Bad Gateway", [], f"keepstack: {e}".encode()
            self.reply(*result)

        do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = do_PATCH = do_OPTIONS = any

    return Handler


class Server(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR lets two processes listen on the same port; two proxies
    # would replay the journal twice onto the same emulator.
    allow_reuse_address = os.name != "nt"

    def handle_error(self, request, client_address):
        # Clients (Terraform, SDKs) close keep-alive connections whenever they like: not an error.
        if not isinstance(sys.exc_info()[1], (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
            super().handle_error(request, client_address)


DEFAULT_IMAGE = "keepstack-emulator"
ENV = {"AWS_ENDPOINT_URL": None, "AWS_ACCESS_KEY_ID": "test", "AWS_SECRET_ACCESS_KEY": "test",
       "AWS_REGION": "us-east-1", "AWS_DEFAULT_REGION": "us-east-1"}
UNSET = ("AWS_SESSION_TOKEN", "AWS_PROFILE")  # if left set, the SDK might use real credentials


def ensure_image(image):
    if subprocess.run(["docker", "image", "inspect", image], capture_output=True, check=False).returncode == 0:
        return
    if image != DEFAULT_IMAGE:
        raise RuntimeError(f"Docker image {image!r} does not exist")
    log(f"building the {image} image (first run only, ~1 min)...")
    here = os.path.dirname(os.path.abspath(__file__))
    r = subprocess.run(["docker", "build", "-t", image, "-f", os.path.join(here, "Dockerfile"), here],
                       capture_output=True, text=True, check=False)
    if r.returncode != 0:
        raise RuntimeError(f"docker build failed:\n{r.stderr[-2000:]}")


def cmd_up(args):
    app = App(args)
    # Grab the port before touching the journal or Docker: if another proxy is running, exit without disturbing it.
    try:
        server = Server(("127.0.0.1", args.port), make_handler(app))
    except OSError:
        log(f"port {args.port} is already in use: is another keepstack running?")
        return 1

    def on_sigterm(*_):
        raise KeyboardInterrupt  # same clean shutdown as Ctrl+C: stops the container too
    signal.signal(signal.SIGTERM, on_sigterm)
    try:
        ensure_image(args.image)
        if args.reset:
            app.reset()
        app.boot()
        threading.Thread(target=app.watch, daemon=True).start()
        log(f"ready on http://127.0.0.1:{args.port}  (Ctrl+C to stop; state is kept in {app.db_path})")
        log("in another terminal: eval \"$(keepstack env)\"   |   PowerShell: keepstack env --shell powershell | iex")
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    except Exception as e:  # noqa: BLE001 - do not leave the container orphaned
        log(f"error: {e}")
        return 1
    finally:
        app.stop()
        server.server_close()
        log("stopped")
    return 0


def cmd_env(args):
    env = dict(ENV, AWS_ENDPOINT_URL=f"http://127.0.0.1:{args.port}")
    if args.shell == "powershell":
        print("; ".join(f'$env:{k}="{v}"' for k, v in env.items()) + "; Remove-Item " +
              ",".join(f"Env:{k}" for k in UNSET) + " -ErrorAction SilentlyContinue")
    else:
        print("\n".join(f"export {k}={v}" for k, v in env.items()) + "\nunset " + " ".join(UNSET))
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="keepstack", description="Persistent state for AWS Emulator: your "
                                "local infrastructure survives restarts.")
    p.add_argument("--version", action="version", version=f"keepstack {__version__}")
    sub = p.add_subparsers(dest="cmd", metavar="{up,env}")

    up = sub.add_parser("up", help="start the emulator, restore state and serve the API")
    up.add_argument("--port", type=int, default=9297, help="port your tools connect to (http)")
    up.add_argument("--emulator-port", type=int, default=9298, help="internal emulator port")
    up.add_argument("--image", default=DEFAULT_IMAGE, help="emulator Docker image")
    up.add_argument("--data-dir", default=".keepstack", help="journal directory (journal.db)")
    up.add_argument("--reset", action="store_true", help="delete the saved state and start from scratch")
    up.add_argument("--verbose", action="store_true", help="log every request")
    up.set_defaults(func=cmd_up)

    env = sub.add_parser("env", help="print the variables that point the AWS CLI/SDK/Terraform at keepstack")
    env.add_argument("--port", type=int, default=9297)
    env.add_argument("--shell", choices=("sh", "powershell"), default="powershell" if os.name == "nt" else "sh")
    env.set_defaults(func=cmd_env)

    args = p.parse_args(argv)
    if not args.cmd:
        p.print_help()
        return 2
    return args.func(args)
