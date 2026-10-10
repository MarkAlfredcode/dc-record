#!/usr/bin/env python3
"""For a run that published to the website but sent no email (the PM
editions have no email subscribers): keep the parts of a dedupe manifest that
belong to this AM/PM mode, revert the rest to the committed version.

    python3 scripts/keep_mode_keys.py data/sam-seen.json pm extracts

keeps the working copy's ["pm"] and ["extracts"] (a cache, safe to keep) and
restores every other key -- e.g. sam-seen.json's "flagged_sols" and
"vendors", which the AM edition reads and which shouldn't record what only a
web edition showed. Before 2026-10-10 the whole file was reverted, so every
PM edition deduped against nothing and repeated 25-41 SAM.gov items from the
previous PM.
"""
import json
import subprocess
import sys


def main():
    path, keep = sys.argv[1], sys.argv[2:]
    with open(path) as f:
        work = json.load(f)
    r = subprocess.run(["git", "show", f"HEAD:{path}"], capture_output=True, text=True)
    base = json.loads(r.stdout) if r.returncode == 0 and r.stdout.strip() else {}
    for k in keep:
        if k in work:
            base[k] = work[k]
    with open(path, "w") as f:
        json.dump(base, f, indent=2)
        f.write("\n")
    print(f"{path}: kept {', '.join(keep)} from this run, reverted the rest", file=sys.stderr)


if __name__ == "__main__":
    main()
