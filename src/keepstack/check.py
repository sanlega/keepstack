"""`keepstack check`: pre-flight report of which Terraform resources will work on the emulator."""
import gzip
import json
import os
import re
import sys
import urllib.request
from collections import Counter

RELEASE_URL = "https://api.github.com/repos/aws/aws-emulator/releases/latest"
ISSUES_URL = "https://api.github.com/repos/aws/aws-emulator/contents/issues/things-that-do-not-work"
RESOURCE_RE = re.compile(r'resource\s+"(aws_[a-z0-9_]+)"\s+"([^"]+)"')
MODULE_RE = re.compile(r'^\s*module\s+"([^"]+)"\s*\{(.*?)^\s*\}', re.DOTALL | re.MULTILINE)
SOURCE_RE = re.compile(r'^\s*source\s*=\s*"([^"]+)"', re.MULTILINE)
ISSUE_RE = re.compile(r"^\d{8}T\d{6}Z-([a-z0-9]+)-(.+)\.md$")

# Everything not listed here maps to the first word after `aws_` (aws_iam_role -> iam).
EC2 = ("vpc", "subnet", "route_table", "internet_gateway", "nat_gateway", "eip", "network_acl", "security_group",
       "instance", "ebs_", "key_pair", "launch_template", "ami", "network_interface", "ec2_")
SERVICE_PREFIXES = (("cloudwatch_log_", "logs"), ("cloudwatch_event_", "events"), ("cloudwatch_", "cloudwatch"),
                    ("api_gateway_", "apigateway"))
STATUS_ORDER = ("broken", "partial", "untested", "ok")
UNICODE = {"broken": "✗", "partial": "⚠", "untested": "?", "ok": "✓"}
ASCII = {"broken": "x", "partial": "!", "untested": "?", "ok": "+"}


def sym(name):
    """Status symbol, ASCII when the console cannot print it (e.g. Windows cp1252)."""
    try:
        "".join(UNICODE.values()).encode(sys.stdout.encoding or "ascii")
        return UNICODE[name]
    except (UnicodeEncodeError, LookupError):
        return ASCII[name]


def service_of(rtype):
    name = rtype.removeprefix("aws_")
    if name == "route" or name.startswith(EC2):
        return "ec2"
    for prefix, svc in SERVICE_PREFIXES:
        if name.startswith(prefix):
            return svc
    return name.split("_")[0]


def scan_dir(path):
    """-> (Counter of resource types, [(module name, source)] for remote modules)."""
    types, remote = Counter(), []
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d != ".terraform"]
        for f in files:
            if not f.endswith(".tf"):
                continue
            with open(os.path.join(root, f), encoding="utf-8", errors="replace") as fh:
                text = fh.read()
            types.update(m.group(1) for m in RESOURCE_RE.finditer(text))
            for m in MODULE_RE.finditer(text):
                src = SOURCE_RE.search(m.group(2))
                if src and not src.group(1).startswith((".", "/")):
                    remote.append((m.group(1), src.group(1)))
    return types, remote


def _walk_module(mod):
    yield from mod.get("resources", [])
    for child in mod.get("child_modules", []):
        yield from _walk_module(child)


def scan_plan(path):
    with open(path, encoding="utf-8") as fh:
        plan = json.load(fh)
    if plan.get("resource_changes"):
        # skip pure deletes: they will not exist after apply
        rs = [r for r in plan["resource_changes"] if r.get("change", {}).get("actions") != ["delete"]]
    else:
        rs = list(_walk_module(plan.get("planned_values", {}).get("root_module", {})))
    return Counter(r["type"] for r in rs if r.get("type", "").startswith("aws_")), []


# keepstack service name (see service_of) -> namespace of the service in the emulator's fidelity data.
FIDELITY_NAMESPACES = {"logs": "cloudwatchlogs", "events": "eventbridge", "cognito": "cognitoidentityprovider",
                       "lb": "elasticloadbalancingv2", "alb": "elasticloadbalancingv2", "elb": "elasticloadbalancing",
                       "db": "rds", "msk": "kafka", "appautoscaling": "applicationautoscaling",
                       "elasticsearch": "elasticsearchservice", "service": "servicediscovery", "default": "ec2"}


def summarize_scores(data):
    """The emulator's scores.json (one entry per fidelity test) -> {namespace: [passed, total]}.
    Only single-service tests count, like the per-service scores AWS publishes; skipped tests are left out."""
    services = {}
    for unit in data.get("units", []):
        if len(unit.get("services", [])) != 1 or unit.get("outcome") not in ("pass", "fail"):
            continue
        ns = unit["services"][0].split("#")[0].removeprefix("com.amazonaws.")
        score = services.setdefault(ns, [0, 0])
        score[0] += unit["outcome"] == "pass"
        score[1] += 1
    return dict(sorted(services.items()))


def load_fidelity():
    with open(os.path.join(os.path.dirname(__file__), "fidelity.json"), encoding="utf-8") as fh:
        return json.load(fh)


def cache_dir():
    base = os.environ.get("XDG_CACHE_HOME") or (os.environ.get("LOCALAPPDATA") if os.name == "nt" else None)
    return os.path.join(base or os.path.join(os.path.expanduser("~"), ".cache"), "keepstack")


def _get(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "keepstack", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def fetch_fidelity(cache=None):
    """Fidelity data of the newest emulator release, downloaded once per release and cached."""
    release = json.loads(_get(RELEASE_URL, timeout=10))
    version = release["tag_name"].lstrip("v")
    path = os.path.join(cache or cache_dir(), f"fidelity-{version}.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    asset = next(a for a in release["assets"] if a["name"] == "scores.json.gz")
    data = json.loads(gzip.decompress(_get(asset["browser_download_url"])))
    fid = {"emulator_version": version, "scored_at": data.get("scoredAt"), "services": summarize_scores(data)}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(fid, fh)
    return fid


def fidelity_for(services, fid):
    """-> {service: {"namespace", "passed", "total", "score"}} for the services that have fidelity data."""
    out = {}
    for svc in sorted(services):
        ns = FIDELITY_NAMESPACES.get(svc, svc)
        if ns in fid["services"]:
            passed, total = fid["services"][ns]
            out[svc] = {"namespace": ns, "passed": passed, "total": total,
                        "score": round(100 * passed / total) if total else None}
    return out


def load_compat():
    with open(os.path.join(os.path.dirname(__file__), "compat.json"), encoding="utf-8") as fh:
        return json.load(fh)


def parse_issue(filename):
    """'20261022T193451Z-ec2-createsubnet-stub.md' -> ('ec2', 'createsubnet stub'), or None."""
    m = ISSUE_RE.match(filename)
    return (m.group(1), m.group(2).replace("-", " ")) if m else None


def fetch_issue_names():
    req = urllib.request.Request(ISSUES_URL, headers={"User-Agent": "keepstack",
                                                      "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return [e["name"] for e in json.load(r)]


def known_gaps(services, fetch):
    """-> (gaps per used service, note). Never raises: network trouble only produces a note."""
    try:
        names = fetch()
    except Exception as e:  # noqa: BLE001 - rate limits, offline, proxies... all just degrade
        return None, f"could not fetch the emulator's known issues ({type(e).__name__}: {e}); use --offline to skip"
    gaps = {}
    for n in names:
        parsed = parse_issue(n)
        if parsed and parsed[0] in services:
            g = gaps.setdefault(parsed[0], {"count": 0, "examples": []})
            g["count"] += 1
            if len(g["examples"]) < 5:
                g["examples"].append(parsed[1])
    return gaps, None


def build_report(args, fetch=None, fetch_scores=None):
    if not args.plan and not os.path.isdir(args.path):
        raise OSError(f"not a directory: {args.path}")
    counts, remote = scan_plan(args.plan) if args.plan else scan_dir(args.path)
    compat = load_compat()
    resources = []
    for rtype, n in sorted(counts.items()):
        k = compat["resources"].get(rtype, {})
        resources.append({"type": rtype, "count": n, "service": service_of(rtype), "status": k.get("status", "untested"),
                          "note": k.get("note", ""), "workaround": k.get("workaround", "")})
    report = {"source": "plan" if args.plan else "scan", "path": args.plan or args.path,
              "emulator_version": compat["emulator_version"], "resources": resources,
              "modules_not_analysed": [{"name": n, "source": s} for n, s in remote],
              "gaps": None, "gaps_note": None}
    services = {r["service"] for r in resources}
    if not args.offline:
        report["gaps"], report["gaps_note"] = known_gaps(services, fetch or fetch_issue_names)
    fid, source = load_fidelity(), "bundled"
    if not args.offline:
        try:
            fid, source = (fetch_scores or fetch_fidelity)(), "latest release"
        except Exception as e:  # noqa: BLE001 - offline, rate limits: the bundled data is still useful
            report["gaps_note"] = report["gaps_note"] or f"could not fetch the latest fidelity data ({type(e).__name__})"
    report["fidelity"] = {"emulator_version": fid["emulator_version"], "scored_at": fid.get("scored_at"),
                          "source": source, "services": fidelity_for(services, fid)}
    summary = dict.fromkeys(STATUS_ORDER, 0)
    for r in resources:
        summary[r["status"]] += r["count"]
    report["summary"] = summary
    return report


def render(rep):
    how = "plan" if rep["source"] == "plan" else "scanned .tf files"
    out = [f"keepstack check: {rep['path']} ({how}), emulator {rep['emulator_version']}", ""]
    labels = {"broken": "will fail", "partial": "works with a caveat", "untested": "not tested yet", "ok": "ok"}
    width = max([len(r["type"]) for r in rep["resources"]] + [0])
    for status in STATUS_ORDER:
        rows = [r for r in rep["resources"] if r["status"] == status]
        if not rows:
            continue
        out.append(f"{sym(status)} {labels[status]}")
        for r in rows:
            out.append(f"    {r['type']:<{width}}  x{r['count']}" + (f"  {r['note']}" if r["note"] else ""))
            if r["workaround"]:
                out.append(f"    {'':<{width}}  workaround: {r['workaround']}")
        out.append("")
    fid = rep.get("fidelity") or {}
    if fid.get("services"):
        out.append(f"Emulator fidelity for the services you use (official AWS tests, emulator "
                   f"{fid['emulator_version']}, {fid['source']}):")
        rows = sorted(fid["services"].items(), key=lambda kv: (kv[1]["score"] is None, kv[1]["score"] or 0))
        width = max(len(svc) for svc, _ in rows)
        out += [f"    {svc:<{width}}  {f['score']:>3}%  ({f['passed']}/{f['total']} tests)" for svc, f in rows]
        out.append("")
    if rep["modules_not_analysed"]:
        out.append("Modules not analysed (remote source; use --plan to include them):")
        out += [f"    {m['name']}  ({m['source']})" for m in rep["modules_not_analysed"]]
        out.append("")
    if rep["gaps"]:
        out.append("Known emulator gaps for the services you use (aws/aws-emulator issues):")
        for svc, g in sorted(rep["gaps"].items()):
            out.append(f"    {svc}: {g['count']} reported")
            out += [f"        - {e}" for e in g["examples"]]
        out.append("")
    elif rep["gaps"] == {}:
        out += ["No known emulator gaps for the services you use.", ""]
    if rep["gaps_note"]:
        out += [f"note: {rep['gaps_note']}", ""]
    s = rep["summary"]
    out.append(f"{sum(s.values())} resources: {s['ok']} ok, {s['partial']} with caveats, {s['broken']} broken, "
               f"{s['untested']} untested.")
    if not rep["resources"]:
        out.append("No aws_* resources found. Is PATH right? Remote modules need --plan.")
    elif s["broken"]:
        out.append("Next: fix or work around the broken resources before `terraform apply`.")
    else:
        out.append("Next: keepstack up -d && eval \"$(keepstack env)\" && terraform apply")
    return "\n".join(out)


def cmd_check(args):
    try:
        rep = build_report(args)
    except (OSError, ValueError) as e:  # missing path/plan, invalid JSON
        print(f"keepstack: {e}", file=sys.stderr)
        return 2
    print(json.dumps(rep, indent=2) if args.json else render(rep))
    return 1 if rep["summary"]["broken"] else 0
