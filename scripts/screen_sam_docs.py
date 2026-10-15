#!/usr/bin/env python3
"""Haiku reads the text of every SAM.gov attachment sam_docs.py extracted
this run and reports anything newsworthy, with a verbatim quote checked
against the document (added 2026-10-10). Called by triage_sam.py.

Input: data/sam_files.json (this run's files, from sam_docs.py).
Cache: data/sam-docs-read.json {"files": {resourceId: record}}, committed by
every run so no file is read twice. Record fields:
  d  date first read          s  status (below)        n  file name
  m  extraction method        c  characters screened   sol solicitation no.
  f  findings [{news, finding, quote, verified, context}]
  a  failed attempts          e  last error
  r  note (e.g. every line repeated an earlier file on the same notice)
Status: screened | wage_determination | boilerplate_only | no_text |
unsupported | failed (gave up after MAX_ATTEMPTS, or not read within
QUEUE_DAYS) | retry (read failed; queued). A file deferred for time or
screening budget gets no record; it waits in data/sam-docs-pending.json
(with its notice's details) and a later run reads it even after its notice
leaves the fetch window.

Cost (2026-10-09's 526 new files): 22.8M characters extracted -> 8.2M after
sam_boilerplate.clean -> ~70 Haiku calls. This text tokenizes poorly (tables,
part numbers: ~1.7 characters per token), so ~4.8M input tokens, ~$5/day at
list price; the cache means each file is paid for once, AM or PM.
"""
import base64
import hashlib
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sam_boilerplate  # noqa: E402
from dc_common import load_manifest, save_manifest  # noqa: E402
from extract_ja import parse_array, run_claude  # noqa: E402

FILES_PATH = "data/sam_files.json"
CACHE_PATH = "data/sam-docs-read.json"
PENDING_PATH = "data/sam-docs-pending.json"
# Per-run cap on cleaned characters sent to Haiku (~$6 at list price). A
# normal day is ~8M; the cap exists for the first runs (every older file
# re-attached to an amended notice is new to the cache: ~31M on 2026-10-10)
# and for days a major program drops a 500-page RFP package. What doesn't fit
# goes to the pending queue and is read by the next runs.
MAX_SCREEN_CHARS = int(os.environ.get("DC_SAM_SCREEN_MAX_CHARS") or 10_000_000)
# Wall-clock cap on starting new screening batches (added 2026-10-14). The
# 10M-char cap alone took 52 min on the 10/14 PM run (84 files' batches
# failing through retries and splits) and, after the 22-min fetch, pushed
# the Feds job past its 90-min timeout before the main pass. Batches not
# started in time leave their files in the pending queue, like over_budget.
SCREEN_BUDGET_SEC = int(os.environ.get("DC_SAM_SCREEN_BUDGET_SEC") or 1500)
QUEUE_DAYS = 14
QUEUE_META = ("name", "url", "size", "posted", "nids", "sols", "titles", "uiLinks", "agency", "kind", "type")
MODEL = "claude-haiku-4-5-20251001"
BATCH_CHARS = 120_000
CHUNK_CHARS = 100_000
WORKERS = 4
MAX_ATTEMPTS = 3
KEEP_DAYS = 60
CONTEXT_CHARS = 700
# Lines already screened for each solicitation on EARLIER runs (added
# 2026-10-12). Amendments posted days later re-upload the same SOW under a
# new file id; drop_repeated_lines() only knew about this run. Measured on
# the 10/10 attachment set split at 10/08: 3.2% of the later day's
# characters had already been screened. Small, but lossless, and it also
# stops an amendment from re-producing (and re-surfacing) an old finding.
# Only lines of files that finished screening are remembered, so a failed
# or deferred file is read in full later.
# {sol: {"d": last date, "h": base64 of 8-byte line hashes}}; not in git
# (random hashes don't compress) -- the workflow keeps it in actions/cache;
# a missing file just means no cross-run dedupe that run.
LINE_MEMORY_PATH = "data/sam-line-memory.json"
LINE_MEMORY_DAYS = 30

SYSTEM = ("You read federal contracting documents for a Washington investigative reporter and pull out "
          "anything newsworthy. Reply with a JSON array only, no prose, no code fences.")

INSTRUCTIONS = """Below are files attached to federal contract notices on SAM.gov: solicitations, statements of work, specifications, price schedules, amendments, questions and answers, sole-source justifications. Standard contract clauses and template text repeated across many notices have already been removed, so what remains is specific to each purchase. Lines may be out of order or broken by OCR.

Read every document in full and report each fact a Washington investigative reporter would want to know. The test is NEWSWORTHINESS. Examples, not a closed list:
- what the government is buying reveals a policy or a story: immigration detention capacity, deportation or removal flights, border wall, surveillance or facial-recognition technology, crowd control or riot gear, election systems, federal law-enforcement or National Guard deployments, weapons or munitions for an active conflict, emergency or wartime buys
- Washington, D.C. (District government, federal property in D.C., National Mall), the White House, Executive Office of the President, presidential operations (Air Force One, Marine One, Camp David), the Secret Service
- a startling dollar value, quantity or scale (tens of millions of dollars and up; 10,000 detention beds; a year of flights)
- an executive order, DOGE / Department of Government Efficiency, layoffs or reductions in force, office or facility closures, a program being ended or privatized, requirements being removed
- a named company, person or incumbent with political ties or a scandal; a vendor protest; a contract that lapsed or an award made before it was justified; urgency the agency caused itself; repeated bridges or extensions
- foreign governments or foreign military sales to sensitive countries; anything odd, alarming or first-of-its-kind for that agency

Most documents contain nothing newsworthy: routine parts, maintenance, janitorial or grounds work, wage tables, forms, delivery schedules. Returning nothing for them is correct. Don't report that something ordinary is being bought.

Output a JSON array, one object per finding:
[{"d": "<document id, e.g. D3>", "news": <3-5>, "finding": "<under 30 words: the specific fact, in plain English>", "quote": "<VERBATIM text under 250 characters copied exactly from that document that shows it>"}]
news: 5 = a reporter should read this today; 4 = clearly interesting; 3 = notable. Never output anything below 3. Output [] if nothing qualifies.
Copy quotes character for character (OCR errors included). Never paraphrase inside a quote.

DOCUMENTS:
"""


def _norm(s):
    return re.sub(r"[^a-z0-9$]", "", (s or "").lower())


def call_batch(batch):
    """batch: [(doc_id, header, text)]. Returns ({doc_id: [findings]}, cost)."""
    prompt = INSTRUCTIONS + "\n\n".join(f"===== DOCUMENT {d} =====\n{h}\n{t}" for d, h, t in batch)
    # Caching off: every prompt is unique, so the 1h cache write (2x input
    # price) is never read back -- measured 27% cheaper per call without it.
    env = run_claude(prompt, SYSTEM, timeout=900, model=MODEL, env={"DISABLE_PROMPT_CACHING": "1"})
    found = {}
    valid = {d for d, _, _ in batch}
    for r in parse_array(env.get("result")):
        if isinstance(r, dict) and r.get("d") in valid and isinstance(r.get("finding"), str):
            found.setdefault(r["d"], []).append(r)
    return found, env.get("total_cost_usd") or 0


def screen_with_split(batch, deadline, depth=0):
    """A failed batch is split in half and retried, twice over, before its
    documents are given up on for this run. Returns (found, cost, failed_ids, errors)."""
    try:
        found, cost = call_batch(batch)
        return found, cost, [], []
    except Exception as e:  # noqa: BLE001
        if len(batch) == 1 or depth >= 2:
            return {}, 0, [d for d, _, _ in batch], [f"doc screen batch of {len(batch)} failed: {e}"[:300]]
        if time.monotonic() > deadline:
            return {}, 0, [d for d, _, _ in batch], [f"doc screen batch of {len(batch)} failed: {e}"[:300]]
        mid = len(batch) // 2
        out = [screen_with_split(b, deadline, depth + 1) for b in (batch[:mid], batch[mid:])]
        found = {k: v for o in out for k, v in o[0].items()}
        return (found, sum(o[1] for o in out), [d for o in out for d in o[2]], [x for o in out for x in o[3]])


def verify(f, text):
    q = f.get("quote") if isinstance(f.get("quote"), str) else ""
    rec = {"news": max(3, min(5, int(f.get("news") or 3))) if str(f.get("news") or "").isdigit() else 3,
           "finding": f["finding"].strip()[:300], "quote": q.strip()[:300], "verified": False}
    nq = _norm(q)
    if len(nq) >= 12:
        # Locate the quote in the original text for a context window the main
        # pass can check the finding against.
        idx = _norm(text).find(nq)
        if idx >= 0:
            rec["verified"] = True
            first = re.escape(q.split()[0]) if q.split() else None
            pos = text.find(q.strip()[:40]) if first else -1
            if pos < 0 and first:
                m = re.search(first, text)
                pos = m.start() if m else -1
            if pos >= 0:
                rec["context"] = " ".join(text[max(0, pos - CONTEXT_CHARS // 2):pos + CONTEXT_CHARS].split())
    return rec


def line_key(line):
    """8-byte hash of a substantial line (26+ characters), else None."""
    key = " ".join(line.split()).lower()
    if len(key) <= 25:
        return None
    return int.from_bytes(hashlib.blake2b(key.encode(), digest_size=8).digest(), "big")


def drop_repeated_lines(cleaned, seen):
    """Drops lines already sent to Haiku in another file of the same
    solicitation -- this run or (via the line memory) an earlier one;
    amendments re-upload the same statement of work under a new file id.
    Only substantial lines (26+ characters) count; quotes are still verified
    against the file's full text. Returns (text, new line keys, lines dropped)."""
    keep, new, dropped = [], set(), 0
    for line in cleaned.split("\n"):
        k = line_key(line)
        if k is not None:
            if k in seen:
                dropped += 1
                continue
            new.add(k)
        keep.append(line)
    return "\n".join(keep), new, dropped


def load_line_memory(path=None):
    try:
        with open(path or LINE_MEMORY_PATH) as fh:
            raw = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    out = {}
    for sol, v in raw.items():
        b = base64.b64decode(v.get("h", ""))
        out[sol] = (v.get("d", ""), {int.from_bytes(b[i:i + 8], "big") for i in range(0, len(b), 8)})
    return out


def save_line_memory(mem, today, path=None):
    cutoff = (date.fromisoformat(today) - timedelta(days=LINE_MEMORY_DAYS)).isoformat()
    raw = {sol: {"d": d, "h": base64.b64encode(b"".join(h.to_bytes(8, "big") for h in sorted(hs))).decode()}
           for sol, (d, hs) in mem.items() if d >= cutoff and hs}
    with open(path or LINE_MEMORY_PATH, "w") as fh:
        json.dump(raw, fh, separators=(",", ":"))


def prune(cache, today):
    cutoff = (date.fromisoformat(today) - timedelta(days=KEEP_DAYS)).isoformat()
    return {k: v for k, v in cache.items() if v.get("d", today) >= cutoff}


def screen(today, files_path=FILES_PATH, cache_path=CACHE_PATH, pending_path=PENDING_PATH):
    """Screens every file in files_path that needs it (up to
    MAX_SCREEN_CHARS), updates and saves the cache and the pending queue.
    Returns (this run's records {rid: rec}, stats, errors)."""
    try:
        with open(files_path) as fh:
            files = json.load(fh)
    except (OSError, json.JSONDecodeError) as e:
        return {}, {"error": f"no {files_path}: {e}"}, [f"document screen skipped: no {files_path}"]
    manifest = load_manifest(cache_path)
    cache = prune(manifest.get("files", {}), today)
    # Skip anything already read to a final status (a re-run of this step,
    # or another run that got there first).
    todo = {rid: f for rid, f in files.items() if f.get("screen") and not f.get("deferred")
            and (cache.get(rid) or {}).get("s", "retry") == "retry"}

    recs, docs = {}, {}
    for rid, f in todo.items():
        base = {"d": cache.get(rid, {}).get("d", today), "n": f.get("name"), "sol": (f.get("sols") or [None])[0],
                "m": f.get("method")}
        text = f.get("text")
        if text is None or f.get("error"):
            attempts = cache.get(rid, {}).get("a", 0) + 1
            terminal = f.get("method") in ("unsupported",) or attempts >= MAX_ATTEMPTS
            recs[rid] = {**base, "s": ("unsupported" if f.get("method") == "unsupported" else "failed") if terminal
                         else "retry", "a": attempts, "e": f.get("error") or "no text"}
        elif not text.strip():
            recs[rid] = {**base, "s": "no_text", "c": 0}
        elif sam_boilerplate.is_wage_determination(text):
            recs[rid] = {**base, "s": "wage_determination", "c": 0}
        else:
            docs[rid] = (text, (f.get("sols") or [rid])[0])
            recs[rid] = base

    common = sam_boilerplate.common_shingles(docs)
    # Budget order: J&A files, then the queue (oldest first), then smallest
    # first -- a cutoff then leaves the fewest, largest files for later.
    # DC_SAM_SCREEN_FIRST_TYPES="Award Notice,Special Notice" puts those
    # notice types right after the J&As (a manual backfill run).
    first = {t.strip() for t in (os.environ.get("DC_SAM_SCREEN_FIRST_TYPES") or "").split(",") if t.strip()}
    order = sorted(docs, key=lambda r: (todo[r].get("kind") != "justification", todo[r].get("type") not in first,
                                        not todo[r].get("from_queue"),
                                        todo[r].get("queued_since") or "", len(docs[r][0])))
    chunks, raw_chars, kept_chars, over_budget = [], 0, 0, set()
    memory = load_line_memory()
    sol_lines, repeat_chars, memory_chars, file_lines = {}, 0, 0, {}
    for k, rid in enumerate(order):
        text = docs[rid][0]
        f = todo[rid]
        sol = docs[rid][1]
        cleaned = sam_boilerplate.clean(text, common, ocr="ocr" in (f.get("method") or ""))
        if sol not in sol_lines:
            sol_lines[sol] = set(memory.get(sol, ("", set()))[1])
        seen = sol_lines[sol]
        deduped, new_lines, repeats = drop_repeated_lines(cleaned, seen)
        if kept_chars and kept_chars + len(deduped) > MAX_SCREEN_CHARS:
            over_budget.add(rid)
            del recs[rid]
            continue
        if sol in memory:
            within_run_only = seen - memory[sol][1]
            memory_chars += len(drop_repeated_lines(cleaned, within_run_only)[0]) - len(deduped)
        seen.update(new_lines)
        file_lines[rid] = (sol, new_lines)
        raw_chars += len(text)
        kept_chars += len(deduped)
        repeat_chars += len(cleaned) - len(deduped)
        recs[rid]["c"] = len(deduped)
        if not cleaned.strip():
            recs[rid]["s"] = "boilerplate_only"
            continue
        if not deduped.strip():
            # every line was in a file already sent this run for the same
            # solicitation (an amendment re-uploading its SOW): read there
            recs[rid].update({"s": "screened", "r": "repeat of an earlier file on this notice"})
            continue
        note = f" ({repeats} lines identical to an earlier file of this solicitation omitted)" if repeats else ""
        pieces = [deduped[i:i + CHUNK_CHARS] for i in range(0, len(deduped), CHUNK_CHARS)]
        for p, piece in enumerate(pieces):
            header = (f"NOTICE: {(f.get('titles') or [''])[0]} ({(f.get('sols') or [''])[0]}; {f.get('agency') or ''}; "
                      f"{f.get('type') or f.get('kind')})\nFILE: {f.get('name')}" + (f" (part {p + 1} of {len(pieces)})" if len(pieces) > 1 else "")
                      + note)
            chunks.append((f"D{k}" + (f".{p + 1}" if len(pieces) > 1 else ""), rid, header, piece))

    batches, cur, size = [], [], 0
    for c in chunks:
        if cur and size + len(c[3]) > BATCH_CHARS:
            batches.append(cur)
            cur, size = [], 0
        cur.append(c)
        size += len(c[3])
    if cur:
        batches.append(cur)
    by_id = {c[0]: c[1] for c in chunks}

    deadline = time.monotonic() + SCREEN_BUDGET_SEC

    def run(b):
        if time.monotonic() > deadline:
            return None
        return screen_with_split([(d, h, t) for d, _, h, t in b], deadline)

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        results = list(ex.map(run, batches))
    cost, failed, errors, over_time = 0.0, set(), [], set()
    found = {}
    for b, res in zip(batches, results):
        if res is None:
            over_time.update(c[1] for c in b)
            continue
        fnd, c, bad, errs = res
        cost += c
        errors += errs
        failed.update(by_id[d] for d in bad)
        for d, fs in fnd.items():
            found.setdefault(by_id[d], []).extend(fs)
    # A file whose pieces straddle the cutoff waits whole for the next run.
    failed -= over_time
    for rid in over_time:
        del recs[rid]

    for rid in docs:
        if rid in over_budget or rid in over_time or recs[rid].get("s") == "boilerplate_only":
            continue
        if rid in failed:
            attempts = cache.get(rid, {}).get("a", 0) + 1
            recs[rid].update({"s": "failed" if attempts >= MAX_ATTEMPTS else "retry", "a": attempts,
                              "e": "screening call failed"})
            continue
        recs[rid]["s"] = "screened"
        fs = [verify(f, docs[rid][0]) for f in found.get(rid, [])]
        if fs:
            recs[rid]["f"] = sorted(fs, key=lambda x: (-x["verified"], -x["news"]))[:6]

    cache.update(recs)
    for rid, (sol, lines) in file_lines.items():
        if recs.get(rid, {}).get("s") == "screened" and lines:
            d, hs = memory.get(sol, ("", set()))
            memory[sol] = (today, hs | lines)
    try:
        save_line_memory(memory, today)
    except OSError as e:
        errors.append(f"line memory not saved: {e}")

    # Pending queue: everything not read to a final status this run.
    pending = load_manifest(pending_path).get("files", {})
    cutoff = (date.fromisoformat(today) - timedelta(days=QUEUE_DAYS)).isoformat()
    deferred = {rid for rid, f in files.items() if f.get("screen") and f.get("deferred")}
    queue = {}
    for rid in deferred | over_budget | over_time | {r for r, x in recs.items() if x["s"] == "retry"}:
        f = files[rid]
        meta = {k: f.get(k) for k in QUEUE_META}
        meta["queued_since"] = (pending.get(rid) or {}).get("queued_since") or f.get("queued_since") or today
        if meta["queued_since"] < cutoff:
            cache[rid] = {"d": today, "n": f.get("name"), "sol": (f.get("sols") or [None])[0], "s": "failed",
                          "e": f"not read within {QUEUE_DAYS} days"}
            continue
        queue[rid] = meta
    for rid, meta in pending.items():  # e.g. this run's fetch never got to the queue
        if rid not in files and (cache.get(rid) or {}).get("s", "retry") == "retry":
            queue.setdefault(rid, meta)
    save_manifest(cache_path, {"files": cache})
    save_manifest(pending_path, {"files": queue})
    stats = {
        "files_screened": sum(1 for r in recs.values() if r["s"] == "screened"),
        "files_boilerplate_only": sum(1 for r in recs.values() if r["s"] in ("boilerplate_only", "wage_determination")),
        "files_no_text": sum(1 for r in recs.values() if r["s"] == "no_text"),
        "files_unsupported": sum(1 for r in recs.values() if r["s"] == "unsupported"),
        "files_failed": sum(1 for r in recs.values() if r["s"] in ("failed", "retry")),
        "files_deferred_time_budget": len(deferred),
        "files_deferred_screen_budget": len(over_budget),
        "files_deferred_screen_time": len(over_time),
        "files_queued_for_next_run": len(queue),
        "chars_extracted": raw_chars, "chars_screened": kept_chars, "chars_skipped_as_repeats": repeat_chars,
        "chars_skipped_seen_on_earlier_runs": memory_chars,
        "batches": len(batches), "findings": sum(len(r.get("f") or []) for r in recs.values()),
        "cost_usd": round(cost, 3),
    }
    print(f"SAM document screen: {stats}", file=sys.stderr)
    return recs, stats, errors


if __name__ == "__main__":
    from dc_common import today_la
    screen(today_la())
