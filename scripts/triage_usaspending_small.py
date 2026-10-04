#!/usr/bin/env python3
"""Cheap-model screen of USAspending contract awards UNDER $2 million.

fetch_usaspending.py only pulls $2M+ awards, which leaves ~1,200 new
awards a day in the $100K-$2M band unread -- and small awards are where
a lot of the stories are (a $900K no-bid deal for a White House vendor, a
$1.5M detention-services contract, a tiny shell company with a first
federal award). This script pulls them, hands the search-row text
(recipient, agency, amount, description, product/service) to Haiku in
batches with no tools, and keeps only what Haiku calls priority/notable
plus a regex safety net for the standing priority subjects. The routine
remainder is reduced to per-agency counts.

The search rows already carry the description, so unlike the $2M+ pass
there's no per-award detail fetch.

Floor: SMALL_FLOOR env var (default $100,000). Below that it's ~10K+
awards a day, mostly micro-purchases; raise the volume at your own cost.

Output: data/usaspending_small_triage.json
Side effect: rewrites data/usaspending-small-manifest.json (novelty dedupe;
the workflow commits it only when the email actually went out).
"""
import json
import os
import re
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import extract_ja
from dc_common import (
    check_reachable, days_ago_la, http_post_json, load_manifest, save_manifest,
    today_la, write_candidates,
)

MANIFEST_PATH = "data/usaspending-small-manifest.json"
OUT_PATH = "data/usaspending_small_triage.json"
BASE = "https://api.usaspending.gov"
MODEL = "claude-haiku-4-5-20251001"
CEILING = 1_999_999  # the API rejects fractional bounds
FLOOR = int(os.environ.get("SMALL_FLOOR", "100000"))
WINDOW_DAYS = 3        # action-date lookback; the manifest dedupes the overlap
MANIFEST_DAYS = 30
MAX_PAGES = 80         # 100 rows/page -> 8,000 rows, a hard stop on runaway volume
BATCH_SIZE = 120
WORKERS = 3
MAX_FLAGGED = 45       # priority first, then by dollar amount; the rest are counted, not listed

# Same standing priority subjects as the SAM.gov net (triage_sam.py). A
# plain-English description of a small award rarely says more than this.
SAFETY_NET_RE = re.compile(
    r"white house|executive office of the president|secret service|camp david|"
    r"air force one|marine one|district of columbia|washington,? d\.?c|\bd\.c\.|"
    r"national capital region|national mall|capitol|detention|deport|removal flight|"
    r"immigration|border wall|border patrol|port of entry|election security|"
    r"national guard|doge\b|department of government efficiency",
    re.IGNORECASE,
)

EO_RE = re.compile(r"\bE\.?\s?O\.?\s?(\d{5})\b|executive order (\d{5})", re.IGNORECASE)

SYSTEM = (
    "You screen federal contract awards for a Washington investigative "
    "reporter. Reply with a JSON array only, no prose, no code fences."
)

INSTRUCTIONS = """Below are newly reported federal contract awards, each under $2 million, one per line:
index | agency | recipient | amount | description | product/service

Tier each one. The one test is NEWSWORTHINESS: would a Washington investigative reporter plausibly want to write about it, or at least know about it? The categories below are examples, not a closed list -- flag anything newsworthy for any other reason too.
- "priority": tied to the White House / Executive Office of the President / presidential operations (Air Force One, Marine One, Camp David, White House Military Office, White House Communications Agency, Secret Service protective operations); Washington, D.C. government or federal property in the capital; the war or any active military operation; immigration detention, deportation, border enforcement; election security; federal law-enforcement or National Guard deployments; something driven by an executive order or the Department of Government Efficiency.
- "notable": a strange, alarming, or surprising purpose for that agency; consultants, PR, lobbying-adjacent or "strategic communications" work; surveillance, spyware, facial recognition, social-media monitoring; a policy shift visible in what the government is buying; a name that looks politically connected or scandal-adjacent; a first-of-its-kind buy; luxury, travel, art, or vanity spending; named-ship or major-weapons-program work; anything tied to a current news story.
- Everything else (spare parts, maintenance, janitorial, routine construction, IT licenses and support, medical supplies, standard research and services) is routine: leave it OUT. Most lines are routine. Be strict: flag roughly 1% of lines, no more than about 15 per batch. An executive-order number stamped on a routine buy (ambulances, reagents, delivery, lodging, janitorial) is a boilerplate clause, not a story -- leave those OUT (they are counted separately). Ordinary repair or maintenance of an embassy, consulate or other building is routine. "Priority" is for the White House / EOP, D.C., war, immigration and election subjects only; anything else interesting is "notable".

Reply with a JSON array of objects, only for priority/notable lines:
[{"i": <index>, "tier": "priority"|"notable", "why": "<one short plain-English sentence on why a reporter would care>"}]
Return [] if nothing qualifies.

"""


def short_agency(a):
    return (a or "").replace("Department of ", "Dept. of ")[:60]


def line_for(i, r):
    desc = (r.get("raw_description") or "").replace("\n", " ").replace("|", "/")[:300]
    psc = (r.get("product_or_service_description") or "")[:60]
    amt = r.get("transaction_amount") or 0
    return f"{i} | {short_agency(r.get('awarding_agency'))} | {(r.get('recipient') or '')[:60]} | ${amt:,.0f} | {desc} | {psc}"


def screen_batch(batch):
    prompt = INSTRUCTIONS + "\n".join(line_for(i, r) for i, r in batch)
    env = extract_ja.run_claude(prompt, SYSTEM, timeout=600, model=MODEL)
    text = env.get("result") or ""
    m = re.search(r"\[.*\]", text, re.DOTALL)
    if not m:
        raise RuntimeError(f"no JSON array in reply: {text[:200]!r}")
    valid = {i for i, _ in batch}
    flags = [f for f in json.loads(m.group(0))
             if isinstance(f, dict) and f.get("i") in valid and f.get("tier") in ("priority", "notable")]
    return flags, env.get("total_cost_usd") or 0


def fetch_rows(today, start, out):
    manifest = load_manifest(MANIFEST_PATH)
    rows, page = [], 1
    while page <= MAX_PAGES:
        payload = {
            "filters": {
                "award_type_codes": ["A", "B", "C", "D"],
                "time_period": [{"start_date": start, "end_date": today}],
                "award_amounts": [{"lower_bound": FLOOR, "upper_bound": CEILING}],
            },
            "fields": [
                "Award ID", "Recipient Name", "Transaction Amount", "Awarding Agency",
                "Awarding Sub Agency", "Transaction Description", "Action Date", "Mod",
                "PSC", "NAICS",
            ],
            "page": page, "limit": 100, "sort": "Transaction Amount", "order": "desc",
        }
        try:
            status, data = http_post_json(f"{BASE}/api/v2/search/spending_by_transaction/", payload, timeout=60)
        except Exception as e:  # noqa: BLE001
            out["errors"].append(f"page {page} fetch failed: {e}")
            break
        if status >= 400:
            out["errors"].append(f"page {page} -> HTTP {status}")
            break
        for row in data.get("results", []):
            if row.get("Mod") != "0":
                continue
            iid = str(row.get("internal_id"))
            if iid in manifest:
                continue
            gen = row.get("generated_internal_id")
            rows.append({
                "internal_id": row.get("internal_id"),
                "award_id": row.get("Award ID"),
                "recipient": row.get("Recipient Name"),
                "transaction_amount": row.get("Transaction Amount"),
                "awarding_agency": row.get("Awarding Agency"),
                "awarding_sub_agency": row.get("Awarding Sub Agency"),
                "action_date": row.get("Action Date"),
                "naics_description": (row.get("NAICS") or {}).get("description"),
                "product_or_service_description": (row.get("PSC") or {}).get("description"),
                "raw_description": row.get("Transaction Description"),
                "url": f"https://www.usaspending.gov/award/{gen}" if gen else None,
            })
            manifest[iid] = {"action_date": row.get("Action Date"), "award_id": row.get("Award ID")}
        if not data.get("page_metadata", {}).get("hasNext"):
            break
        page += 1
    else:
        out["errors"].append(f"stopped at {MAX_PAGES} pages; lower-dollar awards beyond that were not read")
    return rows, manifest


def main():
    today = today_la()
    start = days_ago_la(WINDOW_DAYS)
    out = {
        "window": {"start": start, "end": today},
        "floor_usd": FLOOR, "ceiling_usd": 2_000_000, "model": MODEL,
        "reachable": check_reachable("api.usaspending.gov"),
        "flagged": [], "routine_counts_by_agency": {}, "counts": {}, "errors": [],
    }
    if not out["reachable"]:
        out["errors"].append("api.usaspending.gov unreachable")
        write_candidates(OUT_PATH, out)
        return

    rows, manifest = fetch_rows(today, start, out)
    print(f"USAspending <$2M: {len(rows)} new awards (floor ${FLOOR:,})", file=sys.stderr)

    indexed = list(enumerate(rows))
    batches = [indexed[k:k + BATCH_SIZE] for k in range(0, len(indexed), BATCH_SIZE)]

    def run(batch):
        try:
            return batch, *screen_batch(batch), None
        except Exception as e:  # noqa: BLE001
            return batch, [], 0, str(e)

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        results = list(ex.map(run, batches))

    flagged, unscreened, cost = {}, set(), 0.0
    for batch, flags, c, err in results:
        cost += c
        if err:
            out["errors"].append(f"screen batch {batch[0][0]}-{batch[-1][0]} failed: {err}")
            unscreened.update(i for i, _ in batch)
            continue
        for f in flags:
            r = rows[f["i"]]
            # Hundreds of VA buys carry "EO 14398" as a boilerplate clause;
            # Haiku flags them regardless of the instructions. They're
            # rolled up in executive_order_citations instead.
            if EO_RE.search(r.get("raw_description") or "") and "Veterans Affairs" in (r.get("awarding_agency") or ""):
                continue
            flagged[f["i"]] = f

    for i, r in indexed:
        if i in flagged or i in unscreened:
            continue
        m = SAFETY_NET_RE.search(f"{r.get('raw_description') or ''} {r.get('awarding_sub_agency') or ''}")
        if m:
            flagged[i] = {"i": i, "tier": "keyword", "why": f"matched '{m.group(0)}' (Haiku called it routine)"}

    order = {"priority": 0, "unscreened": 1, "notable": 2, "keyword": 3}
    keep = []
    for i, r in indexed:
        if i in flagged:
            keep.append({**r, "triage_tier": flagged[i]["tier"], "triage_why": flagged[i].get("why", "")})
        elif i in unscreened:
            keep.append({**r, "triage_tier": "unscreened", "triage_why": "screening batch failed"})
    keep.sort(key=lambda r: (order[r["triage_tier"]], -(r.get("transaction_amount") or 0)))

    out["flagged_overflow"] = max(0, len(keep) - MAX_FLAGGED)
    out["flagged"] = keep[:MAX_FLAGGED]
    out["routine_counts_by_agency"] = dict(Counter(
        short_agency(r.get("awarding_agency")) for i, r in indexed
        if i not in flagged and i not in unscreened).most_common())
    eo, eo_usd = Counter(), Counter()
    for r in rows:
        m = EO_RE.search(r.get("raw_description") or "")
        if m:
            k = f"EO {m.group(1) or m.group(2)}"
            eo[k] += 1
            eo_usd[k] += r.get("transaction_amount") or 0
    out["executive_order_citations"] = {k: {"awards": n, "total_usd": round(eo_usd[k])} for k, n in eo.most_common(5)}
    out["counts"] = {
        "new_awards": len(rows), "batches": len(batches), "screened_ok": len(batches) - sum(
            1 for _, _, _, e in results if e),
        "priority": sum(1 for r in keep if r["triage_tier"] == "priority"),
        "notable": sum(1 for r in keep if r["triage_tier"] == "notable"),
        "keyword": sum(1 for r in keep if r["triage_tier"] == "keyword"),
        "unscreened": sum(1 for r in keep if r["triage_tier"] == "unscreened"),
        "haiku_cost_usd": round(cost, 4),
    }
    print(f"USAspending <$2M triage: {out['counts']}", file=sys.stderr)

    cutoff = days_ago_la(MANIFEST_DAYS)
    save_manifest(MANIFEST_PATH, {k: v for k, v in manifest.items() if (v.get("action_date") or "9") >= cutoff})
    write_candidates(OUT_PATH, out)


if __name__ == "__main__":
    main()
