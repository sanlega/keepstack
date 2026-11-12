"""Snapshots (`snapshot save/list/restore/delete`) and `export`/`import`.

A snapshot is `<data-dir>/snapshots/<name>/` with a consistent copy of journal.db, a manifest.json and copies of
any `--include`d files. Copies use the sqlite3 backup API, so saving works while keepstack is running; replacing
the journal (restore/import) does not, because the running proxy holds the old one.
"""
import json
import os
import re
import shutil
import sqlite3
import time

from . import __version__

NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
JOURNAL_COLUMNS = {"seq", "ts", "op", "method", "path", "headers", "body", "status", "resp"}
PRE_RESTORE = "pre-restore"


class SnapshotError(Exception):
    pass


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


def require_stopped(data_dir):
    try:
        with open(os.path.join(data_dir, "run.json"), encoding="utf-8") as f:
            pid = json.load(f).get("pid")
    except (OSError, ValueError, AttributeError):
        return
    if pid_alive(pid):
        raise SnapshotError(f"keepstack is running for this data directory (pid {pid}). "
                            "Run `keepstack down` (or press Ctrl+C in its terminal) and try again.")


def check_name(name):
    if not NAME_RE.fullmatch(name):
        raise SnapshotError(f"invalid snapshot name {name!r}: use letters, digits, '.', '_' or '-' "
                            "(max 64, starting with a letter or digit)")


def snap_dir(data_dir, name):
    return os.path.join(data_dir, "snapshots", name)


def backup(src, dst):
    """Consistent copy of a SQLite file (safe while another process writes to it)."""
    s = sqlite3.connect(src, timeout=30)
    d = sqlite3.connect(dst)
    try:
        s.backup(d)
    finally:
        d.close()
        s.close()


def count_calls(path):
    """Number of journal rows; raises SnapshotError unless `path` is a keepstack journal."""
    if not os.path.isfile(path):
        raise SnapshotError(f"{path} does not exist")
    try:
        db = sqlite3.connect(f"file:{path.replace(os.sep, '/')}?mode=ro", uri=True, timeout=30)
        try:
            cols = {r[1] for r in db.execute("pragma table_info(journal)")}
            if not JOURNAL_COLUMNS <= cols:
                raise SnapshotError(f"{path} is not a keepstack journal (no 'journal' table with the expected columns)")
            return db.execute("select count(*) from journal").fetchone()[0]
        finally:
            db.close()
    except sqlite3.Error as e:
        raise SnapshotError(f"{path} is not a keepstack journal ({e})") from e


def journal_path(data_dir):
    path = os.path.join(data_dir, "journal.db")
    if not os.path.exists(path):
        raise SnapshotError(f"no state to save yet: {path} does not exist")
    return path


def save_snapshot(data_dir, name, includes=(), force=False):
    """Writes the snapshot atomically (temp dir, then rename). Returns its manifest."""
    check_name(name)
    src = journal_path(data_dir)
    includes = [os.path.abspath(f) for f in includes]
    for f in includes:
        if not os.path.isfile(f):
            raise SnapshotError(f"--include file not found: {f}")
    dest = snap_dir(data_dir, name)
    if os.path.exists(dest) and not force:
        raise SnapshotError(f"snapshot {name!r} already exists (use --force to overwrite it)")
    tmp = dest + ".tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(os.path.join(tmp, "files"))
    try:
        backup(src, os.path.join(tmp, "journal.db"))
        files = []
        for i, f in enumerate(includes):
            stored = f"files/{i}_{os.path.basename(f)}"
            shutil.copy2(f, os.path.join(tmp, stored))
            files.append({"path": f, "stored": stored})
        manifest = {"name": name, "created": time.time(), "keepstack_version": __version__,
                    "calls": count_calls(os.path.join(tmp, "journal.db")), "files": files}
        with open(os.path.join(tmp, "manifest.json"), "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=2)
        shutil.rmtree(dest, ignore_errors=True)
        os.rename(tmp, dest)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return manifest


def replace_journal(data_dir, src):
    """Swaps data_dir/journal.db for a copy of `src` (already validated)."""
    os.makedirs(data_dir, exist_ok=True)
    tmp = os.path.join(data_dir, "journal.db.new")
    try:
        backup(src, tmp)
        for ext in ("-wal", "-shm", "-journal"):  # leftovers of the old journal must not be applied to the new one
            if os.path.exists(os.path.join(data_dir, "journal.db" + ext)):
                os.remove(os.path.join(data_dir, "journal.db" + ext))
        os.replace(tmp, os.path.join(data_dir, "journal.db"))
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def safety_snapshot(data_dir, overwritten_files=()):
    """Saves the current state as `pre-restore` so a mistaken restore/import can be undone."""
    if not os.path.exists(os.path.join(data_dir, "journal.db")):
        return
    save_snapshot(data_dir, PRE_RESTORE, [f for f in overwritten_files if os.path.isfile(f)], force=True)
    print(f"Current state saved as snapshot '{PRE_RESTORE}' (undo with: keepstack snapshot restore {PRE_RESTORE}).")


def load_manifest(data_dir, name):
    check_name(name)
    try:
        with open(os.path.join(snap_dir(data_dir, name), "manifest.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        raise SnapshotError(f"snapshot {name!r} does not exist (see `keepstack snapshot list`)") from e


def run(fn, args):
    try:
        return fn(os.path.abspath(args.data_dir), args) or 0
    except SnapshotError as e:
        print(f"keepstack: {e}")
        return 1


def cmd_save(args):
    def go(data_dir, args):
        m = save_snapshot(data_dir, args.name, args.include, args.force)
        extra = f" and {len(m['files'])} file(s)" if m["files"] else ""
        print(f"Snapshot '{args.name}' saved: {m['calls']} calls{extra}.")
    return run(go, args)


def list_snapshots(data_dir):
    """[(name, manifest)] sorted by name."""
    root = os.path.join(data_dir, "snapshots")
    out = []
    for name in sorted(os.listdir(root)) if os.path.isdir(root) else []:
        try:
            out.append((name, load_manifest(data_dir, name)))
        except SnapshotError:
            continue  # not a snapshot (e.g. a leftover .tmp dir)
    return out


def cmd_list(args):
    def go(data_dir, args):
        rows = [(name, time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(m.get("created", 0))),
                 str(m.get("calls", "?")), ", ".join(f["path"] for f in m.get("files", [])) or "-")
                for name, m in list_snapshots(data_dir)]
        if not rows:
            print("No snapshots yet. Create one with: keepstack snapshot save NAME")
            return
        rows.insert(0, ("NAME", "CREATED", "CALLS", "FILES"))
        w = [max(len(r[i]) for r in rows) for i in range(3)]
        for r in rows:
            print(f"{r[0]:<{w[0]}}  {r[1]:<{w[1]}}  {r[2]:>{w[2]}}  {r[3]}")
    return run(go, args)


def cmd_delete(args):
    def go(data_dir, args):
        load_manifest(data_dir, args.name)
        shutil.rmtree(snap_dir(data_dir, args.name))
        print(f"Snapshot '{args.name}' deleted.")
    return run(go, args)


def cmd_restore(args):
    def go(data_dir, args):
        m = load_manifest(data_dir, args.name)
        require_stopped(data_dir)
        base = snap_dir(data_dir, args.name)
        count_calls(os.path.join(base, "journal.db"))
        files = m.get("files", [])
        for f in files:
            if not os.path.isfile(os.path.join(base, f["stored"])):
                raise SnapshotError(f"snapshot {args.name!r} is damaged: missing {f['stored']}")
        tmp = None
        if args.name == PRE_RESTORE:  # the safety snapshot below would overwrite the one being restored
            tmp = base = shutil.copytree(base, os.path.join(data_dir, "restore.tmp"))
        try:
            safety_snapshot(data_dir, [f["path"] for f in files])
            replace_journal(data_dir, os.path.join(base, "journal.db"))
            for f in files:
                os.makedirs(os.path.dirname(f["path"]), exist_ok=True)
                shutil.copy2(os.path.join(base, f["stored"]), f["path"])
                print(f"Restored file {f['path']}")
        finally:
            if tmp:
                shutil.rmtree(tmp, ignore_errors=True)
        print(f"State restored from snapshot '{args.name}' ({m.get('calls', '?')} calls). Start with `keepstack up`.")
    return run(go, args)


def cmd_export(args):
    def go(data_dir, args):
        src = journal_path(data_dir)
        out = os.path.abspath(args.file)
        tmp = out + ".tmp"
        try:
            backup(src, tmp)
            db = sqlite3.connect(tmp)
            try:
                with db:
                    db.execute("create table if not exists meta(key text primary key, value text)")
                    db.execute("insert or replace into meta(key, value) values('export_info', ?)",
                               (json.dumps({"keepstack_version": __version__,
                                            "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}),))
            finally:
                db.close()
            os.replace(tmp, out)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
        print(f"Exported {count_calls(out)} calls to {out}")
    return run(go, args)


def cmd_import(args):
    def go(data_dir, args):
        src = os.path.abspath(args.file)
        n = count_calls(src)
        require_stopped(data_dir)
        safety_snapshot(data_dir)
        replace_journal(data_dir, src)
        print(f"Imported {n} calls from {src}. Start with `keepstack up`.")
    return run(go, args)
