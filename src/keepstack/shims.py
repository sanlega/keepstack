"""Opt-in workarounds for known AWS Emulator bugs (`keepstack up --shims`).

Contract used by proxy.App (both for live traffic and for replay), on the emulator side
of the ID translation:
- on_request(app, op, method, path, headers, body) -> (method, path, headers, body)
- on_response(app, op, status, data) -> data
Both must be pass-through unless shims are enabled for this state directory.
"""


def on_request(app, op, method, path, headers, body):
    return method, path, headers, body


def on_response(app, op, status, data):
    return data
