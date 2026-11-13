"""Chaos test: kill things in the middle of `terraform apply` and check that keepstack recovers.

    python scripts/chaos.py --iterations 6            # TERRAFORM=/path/to/terraform if not on PATH

Every iteration applies examples/quickstart on a fresh keepstack and, after a random delay, does one of:

  emulator   `docker kill` the emulator (Docker restart, OOM): keepstack must restore it by itself
  keepstack  kill -9 keepstack and start it again a few seconds later, while Terraform keeps retrying
  power      kill Terraform, keepstack and the emulator at once, then `keepstack up` again (a power cut)

Then it re-applies until Terraform succeeds (at most 3 times) and requires a clean plan, also after another clean
restart. Exit code 1 if any iteration does not converge. Needs Docker, Terraform and a few minutes per iteration.
"""
import argparse
import json
import os
import random
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
MODES = ("emulator", "keepstack", "power")
ALREADY_EXISTS = re.compile(r"already exists|AlreadyExists|ResourceInUseException|AlreadyOwnedByYou", re.IGNORECASE)
PLANNED = re.compile(r"^  # (\S+) will be", re.MULTILINE)


def orphans(apply_output):
    """Resources a killed `terraform apply` created but never saved (as on AWS): the re-apply's errors when every
    one of them says "already exists", otherwise None (a real failure)."""
    errors = apply_output.split("\nError: ")[1:]
    if not errors or not all(ALREADY_EXISTS.search(e.split("\n", 1)[0]) for e in errors):
        return None
    return sorted(set(re.findall(r"^\s+with (\S+),", apply_output, re.MULTILINE)))


def run(cmd, env, cwd=None, timeout=900):
    p = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, encoding="utf-8", errors="replace",
                       timeout=timeout, check=False)
    return p.returncode, p.stdout + p.stderr


def kill_pid(pid):
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, check=False)
    else:
        os.kill(pid, signal.SIGKILL)


def iteration(i, mode, delay, args, tf, env, work):
    root = os.path.join(work, f"it{i}-{mode}")
    os.makedirs(root)
    shutil.copy(os.path.join(ROOT, "examples", "quickstart", "main.tf"), root)
    data = os.path.join(root, "data")

    def ks(*a):
        return run([sys.executable, "-m", "keepstack", *a, "--data-dir", data], env)

    up = ("up", "-d", "--shims", "--port", str(args.port), "--emulator-port", str(args.port + 1))
    rc, out = ks(*up)
    assert rc == 0, out
    rc, out = run([tf, "init", "-input=false", "-no-color"], env, cwd=root)
    assert rc == 0, out
    apply = subprocess.Popen([tf, "apply", "-auto-approve", "-input=false", "-no-color"], cwd=root, env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, encoding="utf-8", errors="replace")
    time.sleep(delay)
    with open(os.path.join(data, "run.json"), encoding="utf-8") as fh:
        state = json.load(fh)
    if mode == "emulator":
        run(["docker", "kill", state["container"]], env)
    elif mode == "keepstack":
        kill_pid(state["pid"])
        time.sleep(random.uniform(2, 8))
        rc, out = ks(*up)
        assert rc == 0, out
    else:
        apply.kill()
        kill_pid(state["pid"])
        run(["docker", "kill", state["container"]], env)
    first, _ = apply.communicate(timeout=900)
    if mode == "power":
        rc, out = ks(*up)
        assert rc == 0, out
    reapplies = []
    for _ in range(3):
        rc, out = run([tf, "apply", "-auto-approve", "-input=false", "-no-color"], env, cwd=root)
        reapplies.append(rc)
        if rc == 0:
            break
    plan_rc, plan = run([tf, "plan", "-detailed-exitcode", "-input=false", "-no-color"], env, cwd=root)
    ks("down")
    ks(*up)
    restart_rc, restart_plan = run([tf, "plan", "-detailed-exitcode", "-input=false", "-no-color"], env, cwd=root)
    _, log = ks("logs")
    ks("down")
    result = {"iteration": i, "mode": mode, "kill_after_s": round(delay, 1), "first_apply_rc": apply.returncode,
              "reapply_rcs": reapplies, "plan_rc": plan_rc, "plan_after_restart_rc": restart_rc,
              "unconfirmed_skipped": log.count("was never confirmed"), "abandoned": log.count("client gave up"),
              "replay_failed": log.count("REPLAY FAILED")}
    result["ok"] = reapplies[-1] == 0 and plan_rc == 0 and restart_rc == 0
    lost = orphans(out) if mode == "power" and reapplies[-1] != 0 else None
    if lost is not None:
        # Terraform died before saving them, as it would on AWS; keepstack's part is to keep exactly the same state
        # across another restart.
        result["orphaned_by_terraform"] = lost
        result["ok"] = PLANNED.findall(plan) == PLANNED.findall(restart_plan)
    if not result["ok"]:
        result["details"] = {"first_apply": first[-3000:], "last_reapply": out[-3000:],
                             "plan_after_restart": restart_plan[-3000:], "keepstack_log": log[-3000:]}
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--iterations", type=int, default=6)
    p.add_argument("--modes", default=",".join(MODES), help="comma-separated, used in turn")
    p.add_argument("--seed", type=int, default=None, help="for repeatable delays")
    p.add_argument("--min-delay", type=float, default=1.5, help="seconds after `terraform apply` starts")
    p.add_argument("--max-delay", type=float, default=14)
    p.add_argument("--port", type=int, default=9480)
    p.add_argument("--report", help="write the JSON results here")
    args = p.parse_args()
    random.seed(args.seed)
    tf = os.environ.get("TERRAFORM") or shutil.which("terraform")
    if not tf:
        sys.exit("terraform not found (PATH or TERRAFORM env var)")
    env = dict(os.environ, PYTHONPATH=os.path.join(ROOT, "src"), PYTHONIOENCODING="utf-8", TF_IN_AUTOMATION="1",
               AWS_ENDPOINT_URL=f"http://127.0.0.1:{args.port}", AWS_ACCESS_KEY_ID="test",
               AWS_SECRET_ACCESS_KEY="test", AWS_REGION="us-east-1")
    for k in ("AWS_SESSION_TOKEN", "AWS_PROFILE"):
        env.pop(k, None)
    modes = args.modes.split(",")
    work = tempfile.mkdtemp(prefix="keepstack-chaos-")
    results = []
    try:
        for i in range(args.iterations):
            mode = modes[i % len(modes)]
            res = iteration(i, mode, random.uniform(args.min_delay, args.max_delay), args, tf, env, work)
            results.append(res)
            print(json.dumps({k: v for k, v in res.items() if k != "details"}), flush=True)
            if not res["ok"]:
                print(json.dumps(res["details"], indent=2), flush=True)
    finally:
        shutil.rmtree(work, ignore_errors=True)
        if args.report:
            with open(args.report, "w", encoding="utf-8") as fh:
                json.dump(results, fh, indent=2)
    passed = sum(r["ok"] for r in results)
    print(f"chaos: {passed}/{len(results)} iterations converged")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
