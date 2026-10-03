#!/usr/bin/env python3
"""Cheap-model screen of SAM.gov solicitations for the Feds edition.

Solicitations run ~700/day, nearly all routine Defense Logistics Agency
parts buys. Reading every title + description on the main (pricier) model
is the expensive part of the Federal Contracting Actions task, so this
script hands them to Haiku in batches with no tools, asks it to name only
the priority/notable ones, and writes a slimmed file the main pass reads
instead:

- every solicitation Haiku flagged, in full, with its tier + one-line reason
- every solicitation in a batch that failed to screen, in full ("unscreened"),
  so a Haiku failure can never silently drop a story
- the routine remainder reduced to per-agency counts

Justifications are NOT screened -- the main pass reports every one anyway --
but are copied through, along with the fetch counts/errors, so the main pass
reads only this file.

Output: data/sam_triage.json. On total failure, writes nothing usable and
the main prompt falls back to data/sam_candidates.json.
"""
import json
import os
import re
import subprocess
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

IN_PATH = "data/sam_candidates.json"
OUT_PATH = "data/sam_triage.json"
MODEL = "claude-haiku-4-5-20251001"
BATCH_SIZE = 80
WORKERS = 3

SYSTEM = (
    "You screen federal contract solicitations for a Washington investigative "
    "reporter. Reply with a JSON array only, no prose, no code fences."
)

INSTRUCTIONS = """Below are federal contract solicitations posted to SAM.gov, one per line:
index | agency | title | description (may be truncated)

Tier each one:
- "priority": tied to Washington, D.C. (District of Columbia government, federal property/facilities in D.C., National Capital Region, National Mall, D.C. National Guard); the White House, Executive Office of the President or presidential operations (Air Force One, Marine One, Camp David, White House Military Office, White House Communications Agency); the war or any active U.S. military operation (combat-theater support, munitions replenishment, deployments, emergency wartime buys); work on a named Navy or Coast Guard vessel as a whole (USS ___ drydock, overhaul, refit, repair availability, alterations, dismantlement, a new ship class); the U.S. Secret Service; or anything unusual or urgent (emergency/expedited/urgent-and-compelling buys, odd subject matter for the agency, immigration detention, deportation flights, border wall, election security, federal law-enforcement deployments, politically sensitive buyers or vendors).
- "notable": not priority, but a reporter might plausibly care (a large or well-known program, major systems, IT modernization, a surprising buyer/purpose, anything that made you pause).
- "routine": run-of-the-mill parts, supplies, repairs, maintenance, janitorial, facilities upkeep, standard services. Most Defense Logistics Agency national-stock-number parts buys are routine, INCLUDING a spare part whose description merely lists the ships, submarines or aircraft it fits -- naming a USS in a parts list does not make it priority (at most notable, if the part itself is unusual).

When unsure between notable and routine, choose notable.

Output ONLY the priority and notable items (omit routine ones) as a JSON array:
[{"i": <index>, "tier": "priority" | "notable", "why": "<under 15 words>"}]
Output [] if none qualify.

SOLICITATIONS:
"""


def short_agency(path):
    parts = [p.strip() for p in (path or "").split(".") if p.strip()]
    return " / ".join(parts[:2] + parts[-1:]) if len(parts) > 3 else " / ".join(parts)


def line_for(i, s):
    desc = (s.get("description_text") or "").replace("\n", " ").replace("|", "/")
    return f"{i} | {short_agency(s.get('agency'))} | {s.get('title') or ''} | {desc}"


def screen_batch(batch):
    """batch: list of (global_index, solicitation). Returns list of flags, or
    raises on any failure so the caller can mark the whole batch unscreened."""
    prompt = INSTRUCTIONS + "\n".join(line_for(i, s) for i, s in batch)
    proc = subprocess.run(
        ["claude", "-p", "--model", MODEL, "--tools", "", "--system-prompt", SYSTEM,
         "--output-format", "json"],
        input=prompt, capture_output=True, text=True, timeout=600,
        # Thinking off: measured on a real 80-item batch, 101s -> 6s and
        # roughly half the cost, and this is a simple sort, not reasoning.
        env={**os.environ, "MAX_THINKING_TOKENS": "0"},
    )
    if proc.returncode != 0:
        raise RuntimeError(f"claude exit {proc.returncode}: {proc.stderr[:300]}")
    envelope = json.loads(proc.stdout)
    text = envelope.get("result") or ""
    m = re.search(r"\[.*\]", text, re.DOTALL)
    if not m:
        raise RuntimeError(f"no JSON array in reply: {text[:200]!r}")
    flags = json.loads(m.group(0))
    valid = {i for i, _ in batch}
    out = []
    for f in flags:
        if isinstance(f, dict) and f.get("i") in valid and f.get("tier") in ("priority", "notable"):
            out.append(f)
    return out, envelope.get("total_cost_usd") or 0


def main():
    in_path = sys.argv[1] if len(sys.argv) > 1 else IN_PATH
    out_path = sys.argv[2] if len(sys.argv) > 2 else OUT_PATH
    # Never let a stale file from an earlier run stand in for today's screen.
    if os.path.exists(out_path):
        os.remove(out_path)
    with open(in_path) as f:
        cands = json.load(f)
    sols = cands.get("solicitations") or []
    indexed = list(enumerate(sols))
    batches = [indexed[k:k + BATCH_SIZE] for k in range(0, len(indexed), BATCH_SIZE)]

    def run(batch):
        try:
            return batch, *screen_batch(batch), None
        except Exception as e:  # noqa: BLE001
            return batch, [], 0, str(e)

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        results = list(ex.map(run, batches))

    flagged, unscreened, errors = {}, [], []
    cost = 0.0
    for batch, flags, c, err in results:
        cost += c
        if err:
            errors.append(f"triage batch {batch[0][0]}-{batch[-1][0]} failed: {err}")
            unscreened.extend(i for i, _ in batch)
            continue
        for f in flags:
            flagged[f["i"]] = f

    keep = []
    for i, s in indexed:
        if i in flagged:
            keep.append({**s, "triage_tier": flagged[i]["tier"], "triage_why": flagged[i].get("why", "")})
        elif i in unscreened:
            keep.append({**s, "triage_tier": "unscreened", "triage_why": "screening batch failed"})
    keep.sort(key=lambda s: {"priority": 0, "unscreened": 1, "notable": 2}[s["triage_tier"]])

    routine_counts = Counter(
        short_agency(s.get("agency")) for i, s in indexed if i not in flagged and i not in unscreened
    )

    out = {
        # Everything else the main pass needs from sam_candidates.json, so it
        # never has to open that file (and its ~700 descriptions) at all.
        "window": cands.get("window"),
        "reachable": cands.get("reachable"),
        "fetch_errors": cands.get("errors") or [],
        "fetch_counts": cands.get("counts") or {},
        "justifications": cands.get("justifications") or [],
        "model": MODEL,
        "screened_ok": len(batches) - len(errors),
        "batches": len(batches),
        "counts": {
            "solicitations_total": len(sols),
            "priority": sum(1 for s in keep if s["triage_tier"] == "priority"),
            "notable": sum(1 for s in keep if s["triage_tier"] == "notable"),
            "unscreened": len(unscreened),
            "routine": sum(routine_counts.values()),
        },
        "flagged_solicitations": keep,
        "routine_counts_by_agency": dict(routine_counts.most_common()),
        "errors": errors,
    }
    with open(out_path, "w") as f:
        json.dump(out, f, indent=2)
        f.write("\n")
    print(f"wrote {out_path}: {out['counts']} cost=${cost:.3f}", file=sys.stderr)
    if len(errors) == len(batches) and batches:
        sys.exit(1)


if __name__ == "__main__":
    main()
