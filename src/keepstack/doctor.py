"""`keepstack doctor`: environment diagnostics. Every external call is short-timed and never raises."""
import os
import re
import shutil
import socket
import subprocess
import sys
import urllib.request

from . import daemon
from .check import sym

IMAGE = "keepstack-emulator"
OK, WARN, FAIL = "ok", "warn", "fail"


def run_cmd(cmd, timeout=5):
    """-> (returncode, stdout+stderr) or (None, reason) when the tool is missing or hangs."""
    try:
        p = subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).strip()
    except FileNotFoundError:
        return None, "not found"
    except (subprocess.TimeoutExpired, OSError) as e:
        return None, type(e).__name__


def probe_port(port):
    """-> 'free' | 'keepstack' | 'other'."""
    try:
        socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
    except OSError:
        return "free"
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/__admin/health", timeout=2) as r:
            return "keepstack" if r.status == 200 and r.read(16).strip().lower().startswith(b"ok") else "other"
    except Exception:  # noqa: BLE001 - anything that is not a clean 200 "ok" is not keepstack
        return "other"


def mask(value):
    return value[:4] + "*" * 4 if value else ""


def check_python(version=sys.version_info):
    v = ".".join(map(str, version[:3]))
    if tuple(version[:2]) >= (3, 10):
        return OK, "python", v
    return FAIL, "python", f"{v} (3.10+ required)"


def check_docker(run, which):
    if not which("docker"):
        return [(FAIL, "docker", "CLI not found: install Docker Desktop or Docker Engine")]
    rc, out = run(["docker", "version", "--format", "{{.Server.Version}}"])
    if rc != 0:
        return [(FAIL, "docker", f"CLI found but the daemon is not reachable ({out.splitlines()[-1] if out else 'no output'}); start Docker")]
    res = [(OK, "docker", f"daemon {out}")]
    rc, out = run(["docker", "image", "inspect", IMAGE, "--format", "{{.Id}}"])
    res.append((OK, "image", f"{IMAGE} present") if rc == 0 else
               (WARN, "image", f"{IMAGE} missing: will be built on first `keepstack up` (~1 min)"))
    return res


def check_port(port, probe):
    state = probe(port)
    if state == "free":
        return OK, "port", f"{port} is free"
    if state == "keepstack":
        return OK, "port", f"{port} in use by keepstack (running)"
    return FAIL, "port", f"{port} is in use by another program: stop it or pass --port"


def check_aws_cli(run, which):
    if not which("aws"):
        return WARN, "aws cli", "not found (optional, but handy to talk to the emulator)"
    _rc, out = run(["aws", "--version"])
    m = re.search(r"aws-cli/(\d+)\.[\d.]+", out or "")
    if not m:
        return WARN, "aws cli", f"could not read version ({out})"
    if m.group(1) == "1":
        return FAIL, "aws cli", (f"{m.group(0)} is v1: it ignores AWS_ENDPOINT_URL for some services (e.g. SQS) and "
                                 "would send those calls to real AWS. Install v2")
    return OK, "aws cli", m.group(0)


def check_env(port, env):
    res = []
    url, want = env.get("AWS_ENDPOINT_URL"), f"http://127.0.0.1:{port}"
    if not url:
        res.append((WARN, "endpoint", 'AWS_ENDPOINT_URL is not set: run eval "$(keepstack env)"'))
    elif url.rstrip("/") == want:
        res.append((OK, "endpoint", f"AWS_ENDPOINT_URL={url}"))
    else:
        res.append((WARN, "endpoint", f"AWS_ENDPOINT_URL={url} points elsewhere (keepstack is on {want})"))
    risks = [f"{k} is set" for k in ("AWS_PROFILE", "AWS_SESSION_TOKEN") if env.get(k)]
    key = env.get("AWS_ACCESS_KEY_ID", "")
    if key.startswith(("AKIA", "ASIA")):
        risks.append(f"AWS_ACCESS_KEY_ID looks like a real key ({mask(key)})")
    if risks:
        res.append((FAIL, "credentials", "; ".join(risks) + ": calls may reach real AWS. Run keepstack env to override"))
    else:
        res.append((OK, "credentials", "no real-looking AWS credentials in the environment"))
    return res


def check_terraform(run, which):
    if not which("terraform"):
        return WARN, "terraform", "not found (optional)"
    rc, out = run(["terraform", "version"])
    return (OK, "terraform", out.splitlines()[0]) if rc == 0 and out else (WARN, "terraform", "found but `terraform version` failed")


def check_state(data_dir):
    if not os.path.isdir(data_dir):
        return OK, "state", f"{data_dir} does not exist yet (created by `keepstack up`)"
    j = daemon.journal_stats(data_dir)
    return OK, "state", f"{data_dir}: " + (f"{j['calls']} journaled calls" if j else "no journal yet")


def run_checks(args, run=run_cmd, env=None, which=shutil.which, probe=probe_port):
    env = os.environ if env is None else env
    res = [check_python()]
    res += check_docker(run, which)
    res += [check_port(args.port, probe), check_aws_cli(run, which)]
    res += check_env(args.port, env)
    res += [check_terraform(run, which), check_state(args.data_dir)]
    return res


def cmd_doctor(args):
    results = run_checks(args)
    width = max(len(name) for _, name, _ in results)
    for level, name, detail in results:
        mark = "!" if level == WARN else sym("ok" if level == OK else "broken")
        print(f"{mark}  {name + ':':<{width + 1}} {detail}")
    return 1 if any(level == FAIL for level, _, _ in results) else 0
