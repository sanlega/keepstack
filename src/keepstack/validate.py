"""`keepstack validate`: apply a Terraform directory on keepstack and report what would break.

Steps: make sure keepstack runs, point Terraform at it with a temporary override file (local backend inside the
keepstack data dir, so the data dir is a complete environment), then plan, apply and plan again (idempotency).
Every failure is classified: a config error fails the validation, an emulator gap only warns unless --strict.
"""
import argparse
import contextlib
import json
import os
import re
import subprocess
import sys
import time

from . import check, daemon

OVERRIDE = "zz_keepstack_override.tf"
GAP_RE = re.compile(r"StatusCode: 501|NotImplemented|not implemented in this emulator", re.IGNORECASE)
# Terraform's JSON plan actions; "noop" and "read" are not changes.
CHANGE_ACTIONS = ("create", "update", "replace", "delete", "move", "import", "remove")
DESTRUCTIVE = ("replace", "delete")
TAIL = 4000  # characters of command output kept in the report


def relpath(path, start=None):
    """os.path.relpath, or the absolute path when there is no relative one (another Windows drive, or outside)."""
    try:
        rel = os.path.relpath(os.path.abspath(path), start)
    except ValueError:  # Windows: path and start on different drives
        return os.path.abspath(path)
    return os.path.abspath(path) if rel.startswith("..") else rel


def state_key(path):
    """Stable per-directory name, so several Terraform roots can share one data dir."""
    rel = relpath(path)
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", rel.replace(os.sep, "/")).strip("_.") or "root"


def override_text(state_file):
    p = state_file.replace("\\", "/")
    return f"""# Written by `keepstack validate` and removed when it finishes. Do not commit.
terraform {{
  backend "local" {{
    path = "{p}"
  }}
}}

provider "aws" {{
  region                      = "us-east-1"
  access_key                  = "test"
  secret_key                  = "test"
  s3_use_path_style           = true
  skip_credentials_validation = true
  skip_requesting_account_id  = true
  skip_metadata_api_check     = true
}}
"""


def parse_events(text):
    """Terraform `-json` output -> list of messages (non-JSON lines are ignored)."""
    out = []
    for line in text.splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def diagnostics(events):
    return [e["diagnostic"] for e in events if e.get("type") == "diagnostic"
            and e.get("diagnostic", {}).get("severity") == "error"]


def changes(events):
    """planned_change messages -> [{"address", "action", "type"}]."""
    out = []
    for e in events:
        if e.get("type") == "planned_change" and e["change"].get("action") in CHANGE_ACTIONS:
            r = e["change"]["resource"]
            out.append({"address": r["addr"], "action": e["change"]["action"], "type": r.get("resource_type", "")})
    return out


def classify(diag, compat):
    """-> finding dict for one Terraform error diagnostic."""
    text = (diag.get("summary", "") + " " + diag.get("detail", "")).strip()
    address = diag.get("address", "")
    rtype = address.split(".")[-2] if address.count(".") >= 1 and not address.startswith("module.") \
        else _type_of(address)
    known = compat.get(rtype, {})
    if GAP_RE.search(text):
        kind, hint = "emulator", "the emulator does not implement this API yet"
    elif known.get("status") == "broken":
        kind, hint = "emulator", known.get("workaround") or known.get("note", "")
    else:
        kind, hint = "config", ""
    rng = diag.get("range") or {}
    return {"kind": kind, "address": address, "message": text, "hint": hint,
            "file": rng.get("filename"), "line": (rng.get("start") or {}).get("line")}


def _type_of(address):
    """'module.net.aws_subnet.a["x"]' -> 'aws_subnet'."""
    parts = [p for p in re.sub(r"\[[^\]]*\]", "", address).split(".") if p]
    while parts[:1] == ["module"]:
        parts = parts[2:]
    if parts[:1] == ["data"]:
        parts = parts[1:]
    return parts[0] if parts else ""


def locate(tf_dir, address):
    """Root-module resource address -> (file relative to tf_dir, line) of its block, or (None, None).
    shortcut: resources inside modules are not located; follow `module` sources if users need it."""
    if address.startswith("module."):
        return None, None
    m = re.match(r"(data\.)?([a-z0-9_]+)\.([A-Za-z0-9_-]+)", address)
    if not m:
        return None, None
    kind = "data" if m.group(1) else "resource"
    pat = re.compile(rf'^[ \t]*{kind}\s+"{re.escape(m.group(2))}"\s+"{re.escape(m.group(3))}"', re.MULTILINE)
    for name in sorted(os.listdir(tf_dir)):
        if not name.endswith(".tf") or name == OVERRIDE:
            continue
        with open(os.path.join(tf_dir, name), encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        hit = pat.search(text)
        if hit:
            return name, text.count("\n", 0, hit.start()) + 1
    return None, None


# --- running Terraform ---------------------------------------------------------------------------

class Runner:
    def __init__(self, args, tf_dir, env):
        self.args, self.tf_dir, self.env = args, tf_dir, env

    def tf(self, *a):
        try:
            p = subprocess.run([self.args.terraform, *a], cwd=self.tf_dir, env=self.env, capture_output=True,
                               encoding="utf-8", errors="replace", check=False, timeout=self.args.timeout)
        except FileNotFoundError:
            raise OSError(f"Terraform not found ({self.args.terraform}); install it or use --terraform")
        except subprocess.TimeoutExpired:
            raise OSError(f"terraform {a[0]} did not finish within {self.args.timeout} s")
        return p.returncode, p.stdout, p.stderr


def output_env(text):
    """`terraform output -json` -> {"TF_OUTPUT_<name>": value}: strings as they are, anything else as JSON."""
    try:
        outputs = json.loads(text or "{}")
    except ValueError:
        return {}
    return {f"TF_OUTPUT_{k}": o.get("value") if isinstance(o.get("value"), str) else json.dumps(o.get("value"))
            for k, o in outputs.items()}


def run_smoke(commands, env, timeout):
    """Run each command through the shell -> [{"command", "exit_code", "output"}] (output tail only)."""
    results = []
    for cmd in commands:
        try:
            p = subprocess.run(cmd, shell=True, env=env, capture_output=True, encoding="utf-8", errors="replace",
                               timeout=timeout, check=False)
            code, out = p.returncode, (p.stdout or "") + (p.stderr or "")
        except subprocess.TimeoutExpired:
            code, out = None, f"timed out after {timeout} s"
        results.append({"command": cmd, "exit_code": code, "output": out.strip()[-TAIL:]})
    return results


def keepstack_env(port):
    from .cli import ENV, UNSET  # cli imports this module
    env = {k: v for k, v in os.environ.items() if k not in UNSET}
    env.update(ENV, AWS_ENDPOINT_URL=f"http://127.0.0.1:{port}", TF_IN_AUTOMATION="1", TF_INPUT="0")
    return env


@contextlib.contextmanager
def running_keepstack(args):
    """Use the keepstack serving this data dir, or start one and stop it afterwards. Yields the port."""
    data_dir = os.path.abspath(args.data_dir)
    run = daemon.read_run(data_dir)
    if daemon.is_running(run):
        yield run["port"]
        return
    up = argparse.Namespace(port=args.port, emulator_port=args.port + 1, image=args.image, data_dir=data_dir,
                            reset=False, verbose=False, shims=args.shims)
    with contextlib.redirect_stdout(sys.stderr):  # keep stdout clean for --json
        if daemon.detach(up) != 0:
            raise OSError("keepstack did not start (see the log lines above)")
    try:
        yield args.port
    finally:
        if not args.keep_running:
            with contextlib.redirect_stdout(sys.stderr):
                daemon.cmd_down(argparse.Namespace(data_dir=data_dir))


@contextlib.contextmanager
def terraform_override(tf_dir, state_file):
    path = os.path.join(tf_dir, OVERRIDE)
    lock = os.path.join(tf_dir, ".terraform.lock.hcl")
    had_lock = os.path.exists(lock)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(override_text(state_file))
    try:
        yield
    finally:
        with contextlib.suppress(OSError):
            os.remove(path)
        if not had_lock:  # do not leave files the user did not have
            with contextlib.suppress(OSError):
                os.remove(lock)


def step_status(rc, out, err, compat, findings, step):
    """Record the errors of a Terraform step -> "ok", "gap" (only emulator gaps) or "failed"."""
    if rc == 0 or (step == "idempotency" and rc == 2):
        return "ok"
    diags = diagnostics(parse_events(out))
    if not diags:  # init and crashes do not always produce JSON diagnostics
        findings.append({"kind": "config", "address": "", "message": f"terraform {step} failed: " +
                         (err or out).strip()[-TAIL:], "hint": "", "file": None, "line": None})
        return "failed"
    new = [classify(d, compat) for d in diags]
    findings.extend(new)
    return "gap" if all(f["kind"] == "emulator" for f in new) else "failed"


def validate(args):
    """-> report dict. Raises OSError when Terraform or keepstack cannot run."""
    tf_dir = os.path.abspath(args.path)
    if not os.path.isdir(tf_dir):
        raise OSError(f"not a directory: {args.path}")
    data_dir = os.path.abspath(args.data_dir)
    key = state_key(args.path)
    state_file = os.path.join(data_dir, "terraform", key + ".tfstate")
    os.makedirs(os.path.join(data_dir, "cache"), exist_ok=True)
    os.makedirs(os.path.dirname(state_file), exist_ok=True)
    compat = check.load_compat()["resources"]
    report = {"path": args.path, "state": state_file, "steps": {}, "changes": [], "drift": [], "smoke": [],
              "findings": [], "result": None}
    steps, findings = report["steps"], report["findings"]
    started = time.time()
    with running_keepstack(args) as port, terraform_override(tf_dir, state_file):
        env = keepstack_env(port)
        # Keep .terraform (providers, modules) out of the user's directory and out of the cached data dir.
        env["TF_DATA_DIR"] = os.path.join(data_dir, "cache", key)
        runner = Runner(args, tf_dir, env)
        plan_file = os.path.join(data_dir, "cache", key + ".tfplan")

        def step(name, *cmd):
            rc, out, err = runner.tf(*cmd)
            steps[name] = step_status(rc, out, err, compat, findings, name)
            return out

        step("init", "init", "-input=false", "-reconfigure", "-no-color")
        if steps["init"] == "ok":
            report["changes"] = changes(parse_events(step("plan", "plan", "-input=false", "-json",
                                                          "-out=" + plan_file)))
        if steps.get("plan") == "ok":
            step("apply", "apply", "-input=false", "-json", plan_file)
        if steps.get("apply") == "ok":  # after a partial apply the next plan only repeats the failures
            out = step("idempotency", "plan", "-input=false", "-json", "-detailed-exitcode")
            report["drift"] = changes(parse_events(out)) if steps["idempotency"] == "ok" else []
            for c in report["drift"]:
                f, line = locate(tf_dir, c["address"])
                findings.append({"kind": "drift", "address": c["address"],
                                 "message": f"plan is not empty after apply ({c['action']})",
                                 "hint": compat.get(c["type"], {}).get("workaround", ""), "file": f, "line": line})
            if report["drift"]:
                steps["idempotency"] = "failed"
        elif steps.get("apply"):
            steps["idempotency"] = "skipped"
        if args.run and steps.get("apply") in ("ok", "gap"):
            rc, out, _ = runner.tf("output", "-json")
            report["smoke"] = run_smoke(args.run, dict(env, **output_env(out if rc == 0 else "")), args.timeout)
            failed = [r for r in report["smoke"] if r["exit_code"] != 0]
            steps["smoke"] = "failed" if failed else "ok"
            findings.extend({"kind": "smoke", "address": "", "hint": "", "file": None, "line": None,
                             "message": f"`{r['command']}` exited with {r['exit_code']}: {r['output'][-1000:]}"}
                            for r in failed)
        if args.destroy and steps["init"] == "ok":
            step("destroy", "apply", "-destroy", "-auto-approve", "-input=false", "-json")
        with contextlib.suppress(OSError):
            os.remove(plan_file)
    report["duration_seconds"] = round(time.time() - started, 1)
    report["result"] = result_of(report, args)
    return report


def result_of(report, args):
    kinds = {f["kind"] for f in report["findings"]}
    destructive = [c for c in report["changes"] if c["action"] in DESTRUCTIVE]
    if kinds - {"emulator"} or (args.strict and "emulator" in kinds) or (args.fail_on_destroy and destructive):
        return "failed"
    return "passed"


# --- output --------------------------------------------------------------------------------------

LABELS = {"config": "config error", "emulator": "emulator gap", "drift": "not idempotent", "smoke": "smoke test"}


def counts(chs):
    c = {a: 0 for a in CHANGE_ACTIONS}
    for ch in chs:
        c[ch["action"]] += 1
    return c


def render_text(rep):
    ok = rep["result"] == "passed"
    out = [f"keepstack validate: {rep['path']}  ->  {'PASSED' if ok else 'FAILED'} ({rep['duration_seconds']} s)", ""]
    out.append("steps: " + ", ".join(f"{k} {v}" for k, v in rep["steps"].items()))
    c = counts(rep["changes"])
    out.append(f"changes: {c['create']} to add, {c['update']} to change, {c['replace']} to replace, "
               f"{c['delete']} to destroy")
    for ch in rep["changes"]:
        if ch["action"] in DESTRUCTIVE:
            out.append(f"    ! {ch['action']}: {ch['address']}")
    out += [f"smoke: {r['command']}  ->  exit {r['exit_code']}" for r in rep.get("smoke", [])]
    out.append("")
    for f in rep["findings"]:
        where = f" ({f['file']}:{f['line']})" if f.get("file") else ""
        out.append(f"{LABELS.get(f['kind'], f['kind'])}: {f['address'] or '-'}{where}")
        out.append(f"    {f['message']}")
        if f.get("hint"):
            out.append(f"    hint: {f['hint']}")
    if not rep["findings"]:
        out.append("No problems found.")
    return "\n".join(out)


def _md(s):
    return str(s).replace("|", "\\|").replace("\n", " ")


def render_markdown(rep):
    ok = rep["result"] == "passed"
    warn = any(f["kind"] == "emulator" for f in rep["findings"])
    head = "✅ passed" + (" with warnings" if warn else "") if ok else "❌ failed"
    c = counts(rep["changes"])
    out = [f"### keepstack validate: `{rep['path']}` {head}", "",
           "| Step | Result |", "|---|---|"]
    icons = {"ok": "✅", "gap": "⚠️", "skipped": "⏭️"}
    out += [f"| {k} | {icons.get(v, '❌')} {v} |" for k, v in rep["steps"].items()]
    out += ["", (f"**Plan:** {c['create']} to add, {c['update']} to change, {c['replace']} to replace, "
                f"{c['delete']} to destroy"), ""]
    destructive = [ch for ch in rep["changes"] if ch["action"] in DESTRUCTIVE]
    if destructive:
        out += ["> [!WARNING]", "> Destructive changes:"]
        out += [f"> - `{ch['address']}` will be **{ch['action']}d**" for ch in destructive]
        out.append("")
    if rep["findings"]:
        icon = {"config": "❌", "emulator": "⚠️", "drift": "🔁", "smoke": "🧪"}
        out += ["| | Kind | Resource | Message |", "|---|---|---|---|"]
        for f in rep["findings"]:
            where = f" `{f['file']}:{f['line']}`" if f.get("file") else ""
            hint = f"<br>*Hint:* {_md(f['hint'])}" if f.get("hint") else ""
            out.append(f"| {icon.get(f['kind'], '')} | {LABELS.get(f['kind'], f['kind'])} | "
                       f"`{_md(f['address'] or '-')}`{where} | {_md(f['message'][:500])}{hint} |")
        out.append("")
    if rep["changes"]:
        out += ["<details><summary>Planned changes</summary>", ""]
        out += [f"- `{ch['action']}` {ch['address']}" for ch in rep["changes"]]
        out += ["", "</details>", ""]
    out.append(f"<sub>keepstack · {rep['duration_seconds']} s · AWS Emulator, no AWS account used</sub>")
    return "\n".join(out)


def _escape(text, prop=False):
    """GitHub workflow command escaping (properties also escape ':' and ',')."""
    text = str(text).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    return text.replace(":", "%3A").replace(",", "%2C") if prop else text


def annotations(rep, tf_dir, root):
    """-> GitHub workflow commands (::error / ::warning) that show findings inline in the pull request diff."""
    level = {"config": "error", "drift": "error", "emulator": "warning"}
    items = [(level.get(f["kind"], "error"), f["address"], f.get("file"), f.get("line"),
              f"{LABELS.get(f['kind'], f['kind'])}: {f['message']}" + (f" (hint: {f['hint']})" if f.get("hint") else ""))
             for f in rep["findings"]]
    for c in rep["changes"]:
        if c["action"] in DESTRUCTIVE:
            file, line = locate(tf_dir, c["address"])
            items.append(("warning", c["address"], file, line, f"this change will {c['action']} {c['address']}"))
    out = []
    for lvl, address, file, line, msg in items:
        props = []
        if file:
            rel = relpath(os.path.join(tf_dir, file), root).replace(os.sep, "/")
            props.append("file=" + _escape(rel, True))
            if line:
                props.append(f"line={line}")
        props.append("title=" + _escape(f"keepstack: {address or 'terraform'}", True))
        out.append(f"::{lvl} {','.join(props)}::{_escape(msg)}")
    return out


def cmd_validate(args):
    try:
        rep = validate(args)
    except OSError as e:
        print(f"keepstack: {e}", file=sys.stderr)
        return 2
    if args.report:
        with open(args.report, "w", encoding="utf-8") as fh:
            json.dump(rep, fh, indent=2)
    if args.markdown:
        with open(args.markdown, "a", encoding="utf-8") as fh:  # append: $GITHUB_STEP_SUMMARY may hold more
            fh.write(render_markdown(rep) + "\n")
    if os.environ.get("GITHUB_ACTIONS") == "true":
        root = os.environ.get("GITHUB_WORKSPACE") or os.getcwd()
        for line in annotations(rep, os.path.abspath(args.path), root):
            print(line, file=sys.stderr if args.json else sys.stdout)  # keep --json output parseable
    print(json.dumps(rep, indent=2) if args.json else render_text(rep))
    return 0 if rep["result"] == "passed" else 1


def add_parser(sub, defaults):
    v = sub.add_parser("validate", help="apply a Terraform directory on keepstack and report what would break")
    v.add_argument("path", nargs="?", default=".", help="Terraform directory (default: current directory)")
    v.add_argument("--port", type=int, default=defaults["port"], help="port if keepstack has to be started")
    v.add_argument("--data-dir", default=defaults["data_dir"], help="state directory (default: .keepstack)")
    v.add_argument("--image", default=defaults["image"], help="emulator Docker image")
    v.add_argument("--shims", action="store_true", help="start keepstack with --shims if it is not running")
    v.add_argument("--keep-running", action="store_true", help="do not stop a keepstack that validate started")
    v.add_argument("--terraform", default=os.environ.get("TERRAFORM", "terraform"),
                   help="Terraform or OpenTofu binary (default: $TERRAFORM or terraform)")
    v.add_argument("--timeout", type=int, default=1800, help="seconds allowed per Terraform command")
    v.add_argument("--run", action="append", default=[], metavar="CMD",
                   help="command to run after the apply, e.g. smoke tests (repeatable); it gets the AWS variables "
                        "and TF_OUTPUT_<name> for every Terraform output")
    v.add_argument("--destroy", action="store_true", help="destroy everything at the end")
    v.add_argument("--strict", action="store_true", help="emulator gaps fail the validation too")
    v.add_argument("--fail-on-destroy", action="store_true", help="fail if the plan replaces or destroys resources")
    v.add_argument("--json", action="store_true", help="print the report as JSON")
    v.add_argument("--report", metavar="FILE", help="also write the JSON report to FILE")
    v.add_argument("--markdown", metavar="FILE", help="also append a Markdown report to FILE (e.g. $GITHUB_STEP_SUMMARY)")
    v.set_defaults(func=cmd_validate)

