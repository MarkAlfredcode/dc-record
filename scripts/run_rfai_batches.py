#!/usr/bin/env python3
"""Bulk-day RFAI one-liners (added 2026-11-05).

On 2026-11-05 fetch_campaigns.py found 1,456 new RFAIs; the single main pass
got an 825K-char bundle, wrote 135K output tokens and returned invalid JSON,
so no edition. Now, past DC_BULK_RFAI letters, fetch_campaigns.py leaves the
routine RFAIs (no sitting lawmaker, no keyword hit) out of the main bundle and
writes them to data/campaigns_rfai_chunks/chunk-NN.txt. This script runs one
small no-tools Claude pass per chunk (DC_RFAI_WORKERS at a time), checks that
every letter got exactly one item, and appends them to the edition's
"Requests for Additional Information" section, grouped under candidate and
PAC/party subheads. A letter a pass missed (or a chunk that failed twice)
gets a plain line built from its filing record, so no RFAI is ever dropped.

  run_rfai_batches.py content-TODAY.json data/campaigns_candidates.json
"""
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from run_pass import expand_refs, extract_json, run_claude, unlink_bad  # noqa: E402

PROMPT = "prompt-campaigns-rfai.txt"
SECTION = "Requests for Additional Information"
SYSTEM = ("You write one batch of FEC RFAI one-liners for DC Record, a journalist's daily research digest. "
          "Follow the instructions exactly. Reply with the JSON object only: no prose before or after it, no code fences.")
WORKERS = int(os.environ.get("DC_RFAI_WORKERS", "4"))


def esc(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


KEEP_CAPS = {"PAC", "USA", "US", "LLC", "INC", "II", "III", "IV", "GOP", "DNC", "RNC", "NRCC", "DCCC", "NRSC",
             "DSCC", "SEIU", "AFL-CIO", "UAW", "NEA"}
SMALL = {"of", "for", "the", "and", "to", "in", "a", "an", "on", "at", "by"}


def normal_case(s):
    out = []
    for i, w in enumerate((s or "A committee").split()):
        if w.upper() in KEEP_CAPS:
            out.append(w.upper())
        elif i and w.lower() in SMALL:
            out.append(w.lower())
        else:
            out.append(w[:1].upper() + w[1:].lower() if w.isupper() else w)
    return " ".join(out)


def plain_line(row):
    """Record-only line for a letter no pass wrote."""
    name = normal_case(row["committee"])
    rep = f" about its {esc(normal_case(row['report']))} report" if row.get("report") else ""
    return (f"<strong>FEC sends {esc(name)} a request for additional information{rep}</strong>. "
            f"Received {row['received']}. (<a href=\"#{row['n']}\">RFAI</a>)")


def run_chunk(i, chunk, prompt):
    with open(chunk["path"]) as f:
        text = f.read()
    want = set(chunk["refs"])
    for attempt in (1, 2):
        envl, why = run_claude(prompt + "\n" + text, result_path=f"claude_result_rfai_{i:02d}.json", system=SYSTEM)
        if envl is None:
            print(f"rfai batch {i}: claude failed ({why})", file=sys.stderr)
            continue
        try:
            obj = extract_json(envl.get("result"))
        except (ValueError, json.JSONDecodeError) as e:
            with open(f"rfai_batch_{i:02d}.raw.txt", "w") as f:
                f.write(envl.get("result") or "")
            print(f"rfai batch {i} attempt {attempt}: invalid JSON ({e})", file=sys.stderr)
            continue
        got = {}
        for it in obj.get("items") or []:
            try:
                n = int(it.get("n"))
            except (TypeError, ValueError):
                continue
            h = it.get("html") or ""
            if n in want and n not in got and f'href="#{n}"' in h:
                got[n] = h
        print(f"rfai batch {i}: {len(got)}/{len(want)} items (attempt {attempt})", file=sys.stderr)
        if len(got) >= 0.9 * len(want) or attempt == 2:
            return got
    return {}


def main():
    content_path, cand_path = sys.argv[1:3]
    with open(cand_path) as f:
        cand = json.load(f)
    chunks = cand.get("bulk_chunks") or []
    if not chunks:
        print("rfai batches: none (not a bulk day)", file=sys.stderr)
        return 0
    with open(PROMPT) as f:
        prompt = f.read()
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        results = list(ex.map(lambda a: run_chunk(a[0], a[1], prompt), enumerate(chunks, 1)))

    rows = [r for c in chunks for r in c["rows"]]
    written = {}
    for got in results:
        written.update(got)
    fallback = [r["n"] for r in rows if r["n"] not in written]
    for r in rows:
        written.setdefault(r["n"], plain_line(r))
    refs, bad = cand.get("refs", {}), set()
    lines = {n: unlink_bad(expand_refs(h, refs, bad), bad) for n, h in written.items()}

    with open(content_path) as f:
        content = json.load(f)
    secs = content.setdefault("sections", [])
    sec = next((s for s in secs if (s.get("eyebrow") or "").strip().lower() == SECTION.lower()), None)
    if sec is None:
        sec = {"eyebrow": SECTION, "headline": f"FEC sent {len(rows):,} more requests for additional information",
               "paragraphs": [], "blocks": []}
        at = next((i + 1 for i, s in enumerate(secs) if (s.get("eyebrow") or "").lower() == "sitting lawmakers"), 0)
        secs.insert(at, sec)
    sec["no_news"] = False
    blocks = sec.setdefault("blocks", [])
    for label, is_cand in (("Candidate committees", True), ("PACs and party committees", False)):
        items = [lines[r["n"]] for r in rows if r["candidate"] == is_cand]
        if items:
            blocks += [{"type": "subhead", "text": f"{label} ({len(items):,})"}, {"type": "list", "items": items}]
    if fallback:
        blocks.append({"type": "note", "text": f"{len(fallback)} of these {len(rows):,} lines were built from the FEC "
                                               "filing record because the writing pass missed them; open the RFAI for details."})
    with open(content_path, "w") as f:
        json.dump(content, f, indent=2)
        f.write("\n")
    print(f"rfai batches: appended {len(rows)} RFAIs in {len(chunks)} batches ({len(fallback)} record-only lines)",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
