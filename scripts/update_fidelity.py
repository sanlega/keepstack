"""Refresh src/keepstack/fidelity.json from the newest AWS Emulator release (run before cutting a keepstack release).

    python scripts/update_fidelity.py

`keepstack check` uses this bundled copy with --offline or when GitHub cannot be reached; online it fetches the
newest release itself.
"""
import json
import os
import sys
import tempfile

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "src"))

from keepstack import check

fid = check.fetch_fidelity(cache=tempfile.mkdtemp())
lines = [f'  "emulator_version": {json.dumps(fid["emulator_version"])},',
         f'  "scored_at": {json.dumps(fid["scored_at"])},',
         '  "services": {']
items = list(fid["services"].items())
lines += [f'    {json.dumps(ns)}: {json.dumps(score)}' + ("," if i < len(items) - 1 else "")
          for i, (ns, score) in enumerate(items)]
lines += ["  }"]
path = os.path.join(ROOT, "src", "keepstack", "fidelity.json")
with open(path, "w", encoding="utf-8", newline="\n") as fh:
    fh.write("{\n" + "\n".join(lines) + "\n}\n")
print(f"{path}: emulator {fid['emulator_version']}, {len(items)} services")
