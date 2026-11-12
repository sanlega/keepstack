"""Read-only web dashboard served by keepstack at http://127.0.0.1:<port>/__keepstack/ (stub, being implemented).

Contract used by proxy.App.handle(): every request whose path starts with PREFIX is passed to
handle(app, method, path, headers) and must return (status, reason, [(header, value), ...], body_bytes).
"""
PREFIX = "/__keepstack"


def handle(app, method, path, headers):
    return 404, "Not Found", [("Content-Type", "text/plain")], b"keepstack dashboard: not implemented yet"


def cmd_ui(args):
    print("keepstack: ui is not implemented yet")
    return 1
