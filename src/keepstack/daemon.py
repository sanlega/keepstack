"""Background mode: `up -d`, `down`, `status`, `logs`. (stub, being implemented)"""


def write_run_state(args, app):
    """Called by `up` once ready (foreground or background)."""


def clear_run_state(args):
    """Called by `up` on shutdown."""


def detach(args):
    print("keepstack: --detach is not implemented yet")
    return 1


def cmd_down(args):
    print("keepstack: down is not implemented yet")
    return 1


def cmd_status(args):
    print("keepstack: status is not implemented yet")
    return 1


def cmd_logs(args):
    print("keepstack: logs is not implemented yet")
    return 1
