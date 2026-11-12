"""Opt-in workarounds for known AWS Emulator bugs (`keepstack up --shims`).

Contract used by proxy.App (both for live traffic and for replay), on the emulator side
of the ID translation:
- on_request(app, op, method, path, headers, body) -> (method, path, headers, body)
- on_response(app, op, status, data) -> data
Both are pass-through unless shims are enabled for this state directory. Once a journal is
written with shims on, they stay on for it (meta 'shims'): replay must rewrite the same calls.
"""
import json
import re
import urllib.parse

from . import proxy  # proxy imports this module: use proxy.log lazily

SG_ACTIONS = {"RevokeSecurityGroupEgress", "RevokeSecurityGroupIngress",
              "AuthorizeSecurityGroupEgress", "AuthorizeSecurityGroupIngress"}
PORT_PARAM = re.compile(r"IpPermissions\.(\d+)\.(From|To)Port$")
PROTOCOL_PARAM = re.compile(r"IpPermissions\.(\d+)\.IpProtocol$")
_logged = set()


def _applied(name):
    if name not in _logged:
        _logged.add(name)
        proxy.log(f"shim {name} applied")


def ec2_ipprotocol_all_ports(op, body):
    """Emulator 1.0.322 requires FromPort/ToPort to match the stored rule even for IpProtocol=-1;
    AWS ignores them there. Terraform sends 0/0, the default rule stores none: Revoke fails with
    InvalidPermission.NotFound. Verified: omitting both ports is accepted (sending -1/-1 is not)."""
    if op not in SG_ACTIONS:
        return body
    # Split the raw form body so untouched parameters keep their exact order and encoding.
    parts = body.decode("ascii", "replace").split("&")
    keys = [urllib.parse.unquote_plus(p.partition("=")[0]) for p in parts]
    all_ports = {m.group(1) for p, k in zip(parts, keys, strict=True) if (m := PROTOCOL_PARAM.match(k))
                 and urllib.parse.unquote_plus(p.partition("=")[2]) == "-1"}
    kept = [p for p, k in zip(parts, keys, strict=True)
            if not ((m := PORT_PARAM.match(k)) and m.group(1) in all_ports)]
    if len(kept) == len(parts):
        return body
    _applied("ec2-ipprotocol-all-ports")
    return "&".join(kept).encode("ascii")


def _drop_unset_throughput(node):
    """Recursively removes OnDemandThroughput objects whose limits are all -1 (= no cap)."""
    if isinstance(node, list):
        results = [_drop_unset_throughput(v) for v in node]  # a list: no short-circuit, visit all
        return any(results)
    if not isinstance(node, dict):
        return False
    odt = node.get("OnDemandThroughput")
    dropped = isinstance(odt, dict) and all(odt.get(k, -1) == -1 for k in ("MaxReadRequestUnits", "MaxWriteRequestUnits"))
    if dropped:
        del node["OnDemandThroughput"]
    results = [_drop_unset_throughput(v) for v in node.values()]
    return any(results) or dropped


def dynamodb_ondemand_throughput(op, data):
    """The emulator reports OnDemandThroughput {-1, -1} on on-demand tables, so Terraform plans
    `max_read_request_units = -1 -> null`. Real AWS omits the object when no cap is set: the
    provider (aws_dynamodb_table) flattens whatever it gets with no special case for -1 (it only
    suppresses a 0 -> -1 diff, where -1 is the *request* value that removes a cap), and its
    acceptance tests expect a clean plan for plain PAY_PER_REQUEST tables."""
    if op not in {"DescribeTable", "CreateTable", "UpdateTable"} or b"OnDemandThroughput" not in data:
        return data
    try:
        doc = json.loads(data)
    except ValueError:
        return data
    if not _drop_unset_throughput(doc):
        return data
    _applied("dynamodb-ondemand-throughput")
    return json.dumps(doc, separators=(",", ":")).encode()


# (name, description, hook) - docs enumerate this list.
SHIMS = [
    ("ec2-ipprotocol-all-ports", "drop FromPort/ToPort when IpProtocol=-1 in security group rule calls", "request"),
    ("dynamodb-ondemand-throughput", "hide OnDemandThroughput {-1,-1} on on-demand tables, as real AWS does", "response"),
]


def enabled(app):
    """Decides once per app: --shims on first use, or remembered in the journal's meta."""
    on = getattr(app, "shims_on", None)
    if on is None:
        remembered = app.get_meta("shims") == "1"
        flag = bool(getattr(app.args, "shims", False))
        on = remembered or flag
        if flag and not remembered:
            app.set_meta("shims", "1")
        if on:
            names = ", ".join(s[0] for s in SHIMS)
            note = "" if flag else " (remembered for this state directory)"
            proxy.log(f"compatibility shims enabled: {names}{note}")
        app.shims_on = on
    return on


def on_request(app, op, method, path, headers, body):
    if enabled(app):
        body = ec2_ipprotocol_all_ports(op, body)
    return method, path, headers, body


def on_response(app, op, status, data):
    if enabled(app) and 200 <= status < 300:
        data = dynamodb_ondemand_throughput(op, data)
    return data
