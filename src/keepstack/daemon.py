"""Background mode: `up -d`, `down`, `status`, `logs`."""
import json
import os
import re
import signal
import socket
import sqlite3
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

from . import __version__

START_TIMEOUT = 300  # the first run builds the emulator image
STOP_TIMEOUT = 30
SCOPE_RE = re.compile(r"Credential=[^/,\s]+/[^/,\s]+/[^/,\s]+/([^/,\s]+)/aws4_request")


def run_path(data_dir):
    return os.path.join(data_dir, "run.json")


def log_path(data_dir):
    return os.path.join(data_dir, "keepstack.log")


def read_run(data_dir):
    try:
        with open(run_path(data_dir), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def pid_alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        k32 = ctypes.windll.kernel32
        k32.OpenProcess.restype = ctypes.c_void_p
        h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = k32.GetExitCodeProcess(ctypes.c_void_p(h), ctypes.byref(code))
        k32.CloseHandle(ctypes.c_void_p(h))
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def port_open(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def is_running(run):
    """The pid in run.json is alive AND its port accepts connections. A run file copied to another machine or boot
    (restored cache, copied data dir) can name a live but unrelated process; keepstack binds its port first."""
    return bool(run) and pid_alive(run.get("pid")) and port_open(run.get("port") or 0)


def write_run_state(args, app):
    data_dir = os.path.abspath(args.data_dir)
    state = {"pid": os.getpid(), "port": args.port, "emulator_port": args.emulator_port,
             "container": app.container, "data_dir": data_dir,
             "log": log_path(data_dir) if os.environ.get("KEEPSTACK_DETACHED") else None,
             "started": time.time(), "version": __version__}
    with open(run_path(data_dir), "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def clear_run_state(args):
    try:
        os.remove(run_path(args.data_dir))
    except OSError:
        pass


def tail_lines(path, n=200):
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read().splitlines()[-n:]


def detach(args):
    data_dir = os.path.abspath(args.data_dir)
    run = read_run(data_dir)
    if is_running(run):
        print(f"keepstack is already running (pid {run['pid']}, port {run['port']}). Stop it with `keepstack down`.")
        return 0
    os.makedirs(data_dir, exist_ok=True)
    cmd = [sys.executable, "-m", "keepstack", "up", "--port", str(args.port), "--emulator-port",
           str(args.emulator_port), "--image", args.image, "--data-dir", data_dir]
    cmd += [f for f, on in (("--reset", args.reset), ("--verbose", args.verbose), ("--shims", args.shims)) if on]
    flags = (0x00000008 | 0x00000200 | 0x08000000) if os.name == "nt" else 0  # DETACHED | NEW_GROUP | NO_WINDOW
    logfile = log_path(data_dir)
    start = os.path.getsize(logfile) if os.path.exists(logfile) else 0
    with open(logfile, "ab") as out:
        child = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                 env=dict(os.environ, PYTHONIOENCODING="utf-8", KEEPSTACK_DETACHED="1"),
                                 creationflags=flags, start_new_session=os.name != "nt")
    print(f"starting keepstack in the background (log: {logfile})...", flush=True)
    deadline = time.time() + START_TIMEOUT
    while time.time() < deadline:
        with open(logfile, "rb") as f:
            f.seek(start)
            text = f.read().decode("utf-8", errors="replace")
        ready = next((line for line in text.splitlines() if "ready on" in line), None)
        if ready:
            print(ready)
            print("  keepstack status | keepstack logs -f | keepstack down | eval \"$(keepstack env)\"")
            return 0
        if child.poll() is not None:
            break
        time.sleep(0.3)
    else:
        child.kill()
    print("keepstack did not start; last log lines:")
    print("\n".join(tail_lines(logfile, 20)))
    return 1


def cmd_down(args):
    data_dir = os.path.abspath(args.data_dir)
    run = read_run(data_dir)
    if not run:
        print("keepstack is not running.")
        return 0
    pid = run.get("pid")
    was_alive = is_running(run)  # never signal a pid that is not serving keepstack's port
    if was_alive:
        try:
            os.kill(pid, signal.SIGTERM)  # on Windows this terminates the process outright
        except OSError:
            pass
        deadline = time.time() + STOP_TIMEOUT
        while pid_alive(pid) and time.time() < deadline:
            time.sleep(0.2)
        if pid_alive(pid):
            print(f"keepstack (pid {pid}) did not stop within {STOP_TIMEOUT} s.")
            return 1
    # Always: Windows termination and a crash both skip the proxy's own cleanup.
    if run.get("container"):
        subprocess.run(["docker", "rm", "-f", run["container"]], capture_output=True, check=False)
    try:
        os.remove(run_path(data_dir))
    except OSError:
        pass
    print("keepstack stopped." if was_alive else f"cleaned up a stale run file (pid {pid} was not running).")
    return 0


def service_of(headers):
    """AWS service of a journal row, from the SigV4 credential scope of its Authorization header."""
    try:
        h = json.loads(headers) if headers else {}
        auth = next((v for k, v in h.items() if k.lower() == "authorization"), "")
        m = SCOPE_RE.search(auth if isinstance(auth, str) else "")
        return m.group(1) if m else "unknown"
    except (ValueError, AttributeError):
        return "unknown"


def service_breakdown(rows):
    """Counts journal rows by AWS service."""
    return dict(Counter(service_of(headers) for (headers,) in rows).most_common())


def journal_stats(data_dir):
    db_path = os.path.join(data_dir, "journal.db")
    if not os.path.exists(db_path):
        return None
    try:
        db = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
        try:
            calls = db.execute("select count(*) from journal").fetchone()[0]
            services = service_breakdown(db.execute("select headers from journal"))
            row = db.execute("select value from meta where key='last_restore'").fetchone()
        finally:
            db.close()
    except sqlite3.Error:
        return None
    try:
        restore = json.loads(row[0]) if row else None
    except ValueError:
        restore = None
    return {"calls": calls, "services": services, "size_bytes": os.path.getsize(db_path), "last_restore": restore}


def collect_status(data_dir):
    data_dir = os.path.abspath(data_dir)
    run = read_run(data_dir)
    running = alive = is_running(run)
    st = {"running": running, "data_dir": data_dir, "journal": journal_stats(data_dir)}
    if run:
        st.update(pid=run.get("pid"), port=run.get("port"), emulator_port=run.get("emulator_port"),
                  container=run.get("container"), log=run.get("log"), version=run.get("version"),
                  started=run.get("started"), stale=not alive)
        if running and run.get("started"):
            st["uptime_seconds"] = int(time.time() - run["started"])
    return st


def fmt_duration(s):
    h, rem = divmod(int(s), 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m}m {s}s" if h else f"{m}m {s}s" if m else f"{s}s"


def fmt_size(n):
    for unit in ("B", "KB", "MB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def cmd_status(args):
    st = collect_status(args.data_dir)
    if args.json:
        print(json.dumps(st, indent=2))
        return 0
    rows = [("status", "running" if st["running"] else "stopped" + (" (stale run file)" if st.get("stale") else ""))]
    if st["running"]:
        rows += [("pid", st["pid"]), ("endpoint", f"http://127.0.0.1:{st['port']}"),
                 ("uptime", fmt_duration(st.get("uptime_seconds", 0))), ("container", st.get("container"))]
        if st.get("log"):
            rows.append(("log", st["log"]))
    rows.append(("data dir", st["data_dir"]))
    j = st["journal"]
    if j is None:
        rows.append(("journal", "no state yet"))
    else:
        rows.append(("journal", f"{j['calls']} calls, {fmt_size(j['size_bytes'])}"))
        rows += [(f"  {svc}", n) for svc, n in j["services"].items()]
        r = j["last_restore"]
        if r:
            when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["ts"])) if r.get("ts") else "?"
            summary = f"{r.get('replayed', 0)} replayed, {r.get('failed', 0)} failed, {r.get('translated', 0)} translated"
            rows.append(("last restore", f"{when}: {summary}"))
    width = max(len(k) for k, _ in rows)
    print("\n".join(f"{k:<{width}}  {v}" for k, v in rows))
    return 0


def cmd_logs(args):
    path = log_path(os.path.abspath(args.data_dir))
    if not os.path.exists(path):
        print(f"no log yet ({path}): start keepstack in the background with `keepstack up -d`.")
        return 1
    lines = tail_lines(path)
    if lines:
        print("\n".join(lines))
    if args.follow:
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                f.seek(0, os.SEEK_END)
                while True:
                    line = f.readline()
                    if line:
                        print(line, end="", flush=True)
                    else:
                        time.sleep(0.3)
        except KeyboardInterrupt:
            pass
    return 0
