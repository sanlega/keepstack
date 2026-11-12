"""keepstack command line: wires every subcommand to its module."""
import argparse
import os
import signal
import threading

from . import __version__, check, compact, daemon, dashboard, doctor, snapshot
from .proxy import DEFAULT_IMAGE, App, Server, ensure_image, log, make_handler

DEFAULT_PORT = 9297
DEFAULT_DATA_DIR = ".keepstack"
ENV = {"AWS_ENDPOINT_URL": None, "AWS_ACCESS_KEY_ID": "test", "AWS_SECRET_ACCESS_KEY": "test",
       "AWS_REGION": "us-east-1", "AWS_DEFAULT_REGION": "us-east-1"}
UNSET = ("AWS_SESSION_TOKEN", "AWS_PROFILE")  # if left set, the SDK might use real credentials


def cmd_up(args):
    if args.detach:
        return daemon.detach(args)
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
        daemon.write_run_state(args, app)
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
        daemon.clear_run_state(args)
        log("stopped")
    return 0


def cmd_env(args):
    env = dict(ENV, AWS_ENDPOINT_URL=f"http://127.0.0.1:{args.port}")
    if args.shell == "powershell":
        print("; ".join(f'$env:{k}="{v}"' for k, v in env.items()) + "; Remove-Item " +
              ",".join(f"Env:{k}" for k in UNSET) + " -ErrorAction SilentlyContinue")
    elif args.shell == "dotenv":  # KEY=VALUE lines: .env files, $GITHUB_ENV
        print("\n".join(f"{k}={v}" for k, v in env.items()))
    else:
        print("\n".join(f"export {k}={v}" for k, v in env.items()) + "\nunset " + " ".join(UNSET))
    return 0


def build_parser():
    p = argparse.ArgumentParser(prog="keepstack", description="Persistent state for AWS Emulator: your "
                                "local infrastructure survives restarts.")
    p.add_argument("--version", action="version", version=f"keepstack {__version__}")
    sub = p.add_subparsers(dest="cmd", metavar="<command>")

    def data_dir(sp):
        sp.add_argument("--data-dir", default=DEFAULT_DATA_DIR, help="state directory (default: .keepstack)")

    up = sub.add_parser("up", help="start the emulator, restore state and serve the API")
    up.add_argument("--port", type=int, default=DEFAULT_PORT, help="port your tools connect to (http)")
    up.add_argument("--emulator-port", type=int, default=9298, help="internal emulator port")
    up.add_argument("--image", default=DEFAULT_IMAGE, help="emulator Docker image")
    data_dir(up)
    up.add_argument("--reset", action="store_true", help="delete the saved state and start from scratch")
    up.add_argument("--verbose", action="store_true", help="log every request")
    up.add_argument("-d", "--detach", action="store_true", help="run in the background (stop with `keepstack down`)")
    up.add_argument("--shims", action="store_true",
                    help="work around known emulator bugs (remembered for this state directory)")
    up.set_defaults(func=cmd_up)

    down = sub.add_parser("down", help="stop a keepstack running in the background")
    data_dir(down)
    down.set_defaults(func=daemon.cmd_down)

    status = sub.add_parser("status", help="show whether keepstack is running and what the state contains")
    data_dir(status)
    status.add_argument("--json", action="store_true", help="machine-readable output")
    status.set_defaults(func=daemon.cmd_status)

    logs = sub.add_parser("logs", help="show the log of a keepstack running in the background")
    data_dir(logs)
    logs.add_argument("-f", "--follow", action="store_true", help="keep printing new lines")
    logs.set_defaults(func=daemon.cmd_logs)

    env = sub.add_parser("env", help="print the variables that point the AWS CLI/SDK/Terraform at keepstack")
    env.add_argument("--port", type=int, default=DEFAULT_PORT)
    env.add_argument("--shell", choices=("sh", "powershell", "dotenv"),
                     default="powershell" if os.name == "nt" else "sh",
                     help="output format (dotenv: KEY=VALUE lines for .env files or $GITHUB_ENV)")
    env.set_defaults(func=cmd_env)

    chk = sub.add_parser("check", help="report which Terraform resources will work on the emulator, before applying")
    chk.add_argument("path", nargs="?", default=".", help="Terraform directory (default: current directory)")
    chk.add_argument("--plan", help="use a `terraform show -json` plan file instead of scanning .tf files")
    chk.add_argument("--offline", action="store_true", help="do not fetch the emulator's known-issues list")
    chk.add_argument("--json", action="store_true", help="machine-readable output")
    chk.set_defaults(func=check.cmd_check)

    ui = sub.add_parser("ui", help="open the web dashboard of a running keepstack")
    ui.add_argument("--port", type=int, default=DEFAULT_PORT)
    ui.add_argument("--no-browser", action="store_true", help="only print the URL")
    ui.set_defaults(func=dashboard.cmd_ui)

    doc = sub.add_parser("doctor", help="check Docker, ports, AWS CLI and credentials setup")
    doc.add_argument("--port", type=int, default=DEFAULT_PORT)
    data_dir(doc)
    doc.set_defaults(func=doctor.cmd_doctor)

    snap = sub.add_parser("snapshot", help="save and restore named copies of the state")
    snap_sub = snap.add_subparsers(dest="snap_cmd", metavar="<action>", required=True)
    s = snap_sub.add_parser("save", help="save the current state under a name")
    s.add_argument("name")
    s.add_argument("--include", nargs="+", default=[], metavar="FILE",
                   help="also save these files (e.g. terraform.tfstate) and put them back on restore")
    s.add_argument("--force", action="store_true", help="overwrite an existing snapshot")
    data_dir(s)
    s.set_defaults(func=snapshot.cmd_save)
    s = snap_sub.add_parser("list", help="list snapshots")
    data_dir(s)
    s.set_defaults(func=snapshot.cmd_list)
    s = snap_sub.add_parser("restore", help="replace the state with a snapshot (keepstack must be stopped)")
    s.add_argument("name")
    data_dir(s)
    s.set_defaults(func=snapshot.cmd_restore)
    s = snap_sub.add_parser("delete", help="delete a snapshot")
    s.add_argument("name")
    data_dir(s)
    s.set_defaults(func=snapshot.cmd_delete)

    exp = sub.add_parser("export", help="write the state to a portable file")
    exp.add_argument("file")
    data_dir(exp)
    exp.set_defaults(func=snapshot.cmd_export)
    imp = sub.add_parser("import", help="replace the state with an exported file (keepstack must be stopped)")
    imp.add_argument("file")
    data_dir(imp)
    imp.set_defaults(func=snapshot.cmd_import)

    cmp_ = sub.add_parser("compact", help="shrink the journal by dropping resources that were created and deleted")
    cmp_.add_argument("--dry-run", action="store_true", help="only report what would be removed")
    cmp_.add_argument("--image", default=DEFAULT_IMAGE, help="emulator image used to verify the result")
    data_dir(cmp_)
    cmp_.set_defaults(func=compact.cmd_compact)
    return p


def main(argv=None):
    p = build_parser()
    args = p.parse_args(argv)
    if not args.cmd:
        p.print_help()
        return 2
    return args.func(args)
