"""Shared USAspending plumbing for fetch_usaspending.py (awards of $2M+,
large modifications and cuts) and triage_usaspending_small.py ($100K-$2M).

Two things every USAspending pull here has to get right (2026-10-11):

1. THE PENTAGON'S 90-DAY HOLD. Department of Defense contract data reaches
   USAspending ~88-91 days after signing, so an action-date window of the
   last few weeks never contains a single Defense award: on 2026-10-11 the
   25-day window held 1,764 new base awards of $2M+, none from Defense,
   while Defense awards signed July 1-15 (384 of $2M+) had just appeared.
   Every pull therefore runs two sweeps: all agencies over the recent
   window, plus Defense alone over DEFENSE_BACKLOG_DAYS..recent window.
   Days not yet published return nothing, so the wide backlog window costs
   only the pages that actually hold awards (~10 business days of them).

2. BASE AWARDS WITHOUT 10,000 TRANSACTIONS. spending_by_award with
   date_type "new_awards_only" returns only awards whose base action falls
   in the window, with the award description inline -- ~18 pages for 25
   days of $2M+ awards, where the old spending_by_transaction pull read
   ~102 pages of modifications to keep the 17% that were base awards, then
   fetched each award's detail page for its description.

Manifests map an award key -> {"action_date", "dod"?}; Defense entries are
kept until they leave the backlog window, everything else for keep_days.
"""
import json
import re
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from dc_common import days_ago_la, http_post_json

BASE = "https://api.usaspending.gov"
CONTRACT_TYPES = ["A", "B", "C", "D"]
DEFENSE = {"type": "awarding", "tier": "toptier", "name": "Department of Defense"}
RECENT_DAYS = 25
DEFENSE_BACKLOG_DAYS = 105
AWARD_FIELDS = [
    "Award ID", "Recipient Name", "Award Amount", "Awarding Agency", "Awarding Sub Agency",
    "Description", "Base Obligation Date", "Contract Award Type", "NAICS", "PSC",
    "generated_internal_id",
]
TRANSACTION_FIELDS = [
    "Award ID", "Recipient Name", "Transaction Amount", "Awarding Agency", "Awarding Sub Agency",
    "Transaction Description", "Action Date", "Mod", "Action Type", "PSC", "NAICS",
    "internal_id", "generated_internal_id",
]
# FPDS action type codes on a modification (USAspending returns the letter only)
ACTION_TYPES = {
    "A": "additional work (new agreement)", "B": "supplemental agreement for work within scope",
    "C": "funding-only action", "D": "change order", "E": "terminated for default",
    "F": "terminated for convenience", "G": "exercise of an option", "H": "definitized letter contract",
    "J": "novation agreement", "K": "closeout", "L": "definitized change order",
    "M": "other administrative action", "N": "legal contract cancellation",
    "P": "rerepresentation of non-novated merger/acquisition", "R": "rerepresentation",
    "S": "change in principal investigator", "T": "transfer action", "V": "vendor DUNS or name change",
    "W": "vendor address change", "X": "terminated for cause",
}
TERMINATIONS = {"E", "F", "N", "X"}


def sweeps(today, recent_days=RECENT_DAYS, defense_days=DEFENSE_BACKLOG_DAYS):
    """[(label, start, end, agencies or None)]: the recent window for every
    agency, then Defense's backlog (its 90-day hold) up to where the recent
    window begins."""
    return [
        ("recent", days_ago_la(recent_days), today, None),
        ("defense_backlog", days_ago_la(defense_days), days_ago_la(recent_days + 1), [DEFENSE]),
    ]


def search_awards(start, end, lower, upper=None, agencies=None, max_pages=60, errors=None, label=""):
    """New base contract awards (base action date in [start, end]) with an
    award amount in [lower, upper]. Returns rows in API field names."""
    amounts = {"lower_bound": lower}
    if upper is not None:
        amounts["upper_bound"] = upper
    filters = {
        "award_type_codes": CONTRACT_TYPES,
        "time_period": [{"start_date": start, "end_date": end, "date_type": "new_awards_only"}],
        "award_amounts": [amounts],
    }
    if agencies:
        filters["agencies"] = agencies
    rows, page = [], 1
    while page <= max_pages:
        payload = {"filters": filters, "fields": AWARD_FIELDS, "page": page, "limit": 100,
                   "sort": "Award Amount", "order": "desc"}
        try:
            status, data = http_post_json(f"{BASE}/api/v2/search/spending_by_award/", payload, timeout=60)
        except Exception as e:  # noqa: BLE001
            _err(errors, f"{label} page {page} fetch failed: {e}")
            break
        if status >= 400:
            _err(errors, f"{label} page {page} -> HTTP {status}")
            break
        rows += data.get("results", [])
        if not data.get("page_metadata", {}).get("hasNext"):
            break
        page += 1
    else:
        _err(errors, f"{label}: stopped at {max_pages} pages; smaller awards beyond that were not read")
    print(f"  USAspending {label} {start}..{end} ${lower:,}+: {len(rows)} awards, {page} page(s)", file=sys.stderr)
    return rows


def search_modifications(start, end, threshold, increases=True, agencies=None, max_pages=20, errors=None, label=""):
    """Contract modifications in [start, end] whose own amount is >= threshold
    (increases) or <= -threshold (cuts). Sorted by amount so paging stops at
    the first row past the threshold -- a few pages, not the ~100 it takes
    to read every transaction."""
    filters = {"award_type_codes": CONTRACT_TYPES, "time_period": [{"start_date": start, "end_date": end}]}
    if agencies:
        filters["agencies"] = agencies
    rows, page = [], 1
    while page <= max_pages:
        payload = {"filters": filters, "fields": TRANSACTION_FIELDS, "page": page, "limit": 100,
                   "sort": "Transaction Amount", "order": "desc" if increases else "asc"}
        try:
            status, data = http_post_json(f"{BASE}/api/v2/search/spending_by_transaction/", payload, timeout=60)
        except Exception as e:  # noqa: BLE001
            _err(errors, f"{label} page {page} fetch failed: {e}")
            break
        if status >= 400:
            _err(errors, f"{label} page {page} -> HTTP {status}")
            break
        results = data.get("results", [])
        past = False
        for r in results:
            amt = r.get("Transaction Amount") or 0
            if (amt < threshold) if increases else (amt > -threshold):
                past = True
                break
            if r.get("Mod") != "0":
                rows.append(r)
        if past or not data.get("page_metadata", {}).get("hasNext"):
            break
        page += 1
    print(f"  USAspending {label} {start}..{end}: {len(rows)} modification(s)", file=sys.stderr)
    return rows


def award_record(row, sweep):
    gen = row.get("generated_internal_id")
    return {
        "award_key": gen,
        "generated_internal_id": gen,
        "award_id": row.get("Award ID"),
        "recipient": row.get("Recipient Name"),
        # Kept under the old name: fetch_lobby_contracts.py and the prompts read it.
        # For a base award found by spending_by_award it's the obligation to date.
        "transaction_amount": row.get("Award Amount"),
        "awarding_agency": row.get("Awarding Agency"),
        "awarding_sub_agency": row.get("Awarding Sub Agency"),
        "action_date": row.get("Base Obligation Date"),
        "award_type": row.get("Contract Award Type"),
        "naics_description": (row.get("NAICS") or {}).get("description"),
        "product_or_service_description": (row.get("PSC") or {}).get("description"),
        "raw_description": row.get("Description"),
        "url": f"https://www.usaspending.gov/award/{gen}" if gen else None,
        "sweep": sweep,
    }


def modification_record(row, sweep):
    gen = row.get("generated_internal_id")
    code = (row.get("Action Type") or "").strip()[:1]
    return {
        "award_key": f"tx:{row.get('internal_id')}",
        "generated_internal_id": gen,
        "award_id": row.get("Award ID"),
        "modification": row.get("Mod"),
        "recipient": row.get("Recipient Name"),
        "transaction_amount": row.get("Transaction Amount"),
        "awarding_agency": row.get("Awarding Agency"),
        "awarding_sub_agency": row.get("Awarding Sub Agency"),
        "action_date": row.get("Action Date"),
        "action_type": ACTION_TYPES.get(code, code or None),
        "termination": code in TERMINATIONS,
        "naics_description": (row.get("NAICS") or {}).get("description"),
        "product_or_service_description": (row.get("PSC") or {}).get("description"),
        "raw_description": row.get("Transaction Description"),
        "url": f"https://www.usaspending.gov/award/{gen}" if gen else None,
        "sweep": sweep,
    }


def is_defense(rec):
    return (rec.get("awarding_agency") or "") == "Department of Defense"


def legacy_seen_ids(manifest):
    """Award IDs (PIIDs) recorded by the pre-2026-10-11 manifests, which were
    keyed by transaction internal_id -- so the switch to award keys doesn't
    re-report the last month's awards."""
    return {v.get("award_id") for k, v in manifest.items()
            if isinstance(v, dict) and not k.startswith(("CONT_", "tx:", "_")) and v.get("award_id")}


def defense_seed_cutoff(records):
    """For the first run after the Defense backlog sweep was added: the
    second-newest action date that holds a real day's worth of Defense
    backlog awards. Older ones were published before this run and are only
    recorded, not reported -- otherwise the first edition would list a
    month of already-published Pentagon awards as new."""
    days = Counter(r["action_date"] for r in records if r["sweep"] == "defense_backlog" and r.get("action_date"))
    populated = sorted(d for d, n in days.items() if n >= 3)
    if not populated:
        return None
    return populated[-2] if len(populated) >= 2 else populated[-1]


def take_new(records, manifest, legacy_ids, seed=None):
    """Records not yet in the manifest; adds them to it. Returns (new, seeded):
    a record for which seed(record) is true is recorded but not returned --
    used on a first run so a backlog that was published (or never covered)
    before isn't dumped into one edition (see defense_seed_cutoff)."""
    new, seeded = [], 0
    for r in records:
        key = r["award_key"]
        if not key or key in manifest:
            continue
        manifest[key] = {"action_date": r.get("action_date")}
        if is_defense(r):
            manifest[key]["dod"] = True
        if r.get("award_id") in legacy_ids and not key.startswith("tx:"):
            continue  # reported under the old transaction-keyed manifest
        if seed and seed(r):
            seeded += 1
            continue
        new.append(r)
    return new, seeded


def backlog_seed(seed_before):
    """seed predicate: Defense backlog records dated before seed_before."""
    if not seed_before:
        return None
    return lambda r: r["sweep"] == "defense_backlog" and (r.get("action_date") or "") < seed_before


def prune_manifest(manifest, keep_days, defense_days=DEFENSE_BACKLOG_DAYS):
    """Defense entries live until they leave the backlog window; others for keep_days.
    Pre-2026-10-11 entries (transaction-keyed) age out on the old schedule."""
    cut_recent = days_ago_la(keep_days)
    cut_defense = days_ago_la(defense_days + 3)
    out = {}
    for k, v in manifest.items():
        if k.startswith("_"):
            out[k] = v
            continue
        d = (v or {}).get("action_date") or "9"
        if d >= (cut_defense if (v or {}).get("dod") else cut_recent):
            out[k] = v
    return out


def _err(errors, msg):
    print(f"  {msg}", file=sys.stderr)
    if errors is not None:
        errors.append(msg)


# ---------------------------------------------------------------------------
# Cheap-model screen (Haiku), shared by triage_usaspending.py ($2M+ awards,
# modifications, cuts) and triage_usaspending_small.py ($100K-$2M).

SCREEN_MODEL = "claude-haiku-4-5-20251001"

# Same standing priority subjects as the SAM.gov net (triage_sam.py). A
# plain-English description of an award rarely says more than this.
SAFETY_NET_RE = re.compile(
    r"white house|executive office of the president|secret service|camp david|"
    r"air force one|marine one|district of columbia|washington,? d\.?c|\bd\.c\.|"
    r"national capital region|national mall|capitol|detention|deport|removal flight|"
    r"immigration|border wall|border patrol|port of entry|election security|"
    r"national guard|doge\b|department of government efficiency",
    re.IGNORECASE,
)

EO_RE = re.compile(r"\bE\.?\s?O\.?\s?(\d{5})\b|executive order (\d{5})", re.IGNORECASE)

SCREEN_SYSTEM = (
    "You screen federal contract awards for a Washington investigative "
    "reporter. Reply with a JSON array only, no prose, no code fences."
)

SCREEN_INSTRUCTIONS = """Below are {what}, one per line:
index | agency | recipient | amount | action | description | product/service

Tier each one. The one test is NEWSWORTHINESS: would a Washington investigative reporter plausibly want to write about it, or at least know about it? The categories below are examples, not a closed list -- flag anything newsworthy for any other reason too.
- "priority": tied to the White House / Executive Office of the President / presidential operations (Air Force One, Marine One, Camp David, White House Military Office, White House Communications Agency, Secret Service protective operations); Washington, D.C. government or federal property in the capital; the war or any active military operation; immigration detention, deportation, border enforcement; election security; federal law-enforcement or National Guard deployments; something driven by an executive order or the Department of Government Efficiency.
- "notable": a strange, alarming, or surprising purpose for that agency; an unusual company for the government to work with (a sports, entertainment, media, celebrity or consumer brand); consultants, PR, lobbying-adjacent or "strategic communications" work; surveillance, spyware, facial recognition, social-media monitoring; a policy shift visible in what the government is buying; a name that looks politically connected or scandal-adjacent; a first-of-its-kind buy; luxury, travel, art, or vanity spending; named-ship or major-weapons-program work; a contract terminated, or a large amount pulled back from a program; anything tied to a current news story.
- Everything else (spare parts, fuel, maintenance, janitorial, routine construction, IT licenses and support, medical supplies, standard research and services, routine option exercises and funding increments, de-obligating leftover funds at closeout) is routine: leave it OUT. Most lines are routine. Be strict: flag roughly {rate} of lines, no more than about {cap} per batch. An executive-order number stamped on a routine buy (ambulances, reagents, delivery, lodging, janitorial) is a boilerplate clause, not a story -- leave those OUT (they are counted separately). Ordinary repair or maintenance of an embassy, consulate or other building is routine. "Priority" is for the White House / EOP, D.C., war, immigration and election subjects only; anything else interesting is "notable".

Reply with a JSON array of objects, only for priority/notable lines:
[{{"i": <index>, "tier": "priority"|"notable", "why": "<one short plain-English sentence on why a reporter would care>"}}]
Return [] if nothing qualifies.

"""


def short_agency(a):
    return (a or "").replace("Department of ", "Dept. of ")[:60]


def screen_line(i, r):
    desc = (r.get("raw_description") or "").replace("\n", " ").replace("|", "/")[:300]
    psc = (r.get("product_or_service_description") or "")[:60]
    amt = r.get("transaction_amount") or 0
    if r.get("modification"):
        action = f"modification ({r.get('action_type') or 'unspecified'})"
    else:
        action = "new award" + (f", signed {r.get('action_date')}" if r.get("sweep") == "defense_backlog" else "")
    return (f"{i} | {short_agency(r.get('awarding_sub_agency') or r.get('awarding_agency'))} | "
            f"{(r.get('recipient') or '')[:60]} | ${amt:,.0f} | {action} | {desc} | {psc}")


def _screen_batch(batch, header):
    import extract_ja  # needs the claude CLI; imported only when screening
    prompt = header + "\n".join(screen_line(i, r) for i, r in batch)
    env = extract_ja.run_claude(prompt, SCREEN_SYSTEM, timeout=600, model=SCREEN_MODEL)
    text = env.get("result") or ""
    m = re.search(r"\[.*\]", text, re.DOTALL)
    if not m:
        raise RuntimeError(f"no JSON array in reply: {text[:200]!r}")
    valid = {i for i, _ in batch}
    flags = [f for f in json.loads(m.group(0))
             if isinstance(f, dict) and f.get("i") in valid and f.get("tier") in ("priority", "notable")]
    return flags, env.get("total_cost_usd") or 0


def screen(rows, what, rate="1%", cap=15, batch_size=120, workers=3):
    """Haiku tiers every row, then the regex safety net catches the standing
    priority subjects Haiku called routine. Returns (tiers {index: {"tier",
    "why"}}, unscreened index set, stats, errors)."""
    header = SCREEN_INSTRUCTIONS.format(what=what, rate=rate, cap=cap)
    indexed = list(enumerate(rows))
    batches = [indexed[k:k + batch_size] for k in range(0, len(indexed), batch_size)]

    def run(batch):
        try:
            return batch, *_screen_batch(batch, header), None
        except Exception as e:  # noqa: BLE001
            return batch, [], 0, str(e)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        results = list(ex.map(run, batches))

    tiers, unscreened, cost, errors = {}, set(), 0.0, []
    for batch, flags, c, err in results:
        cost += c
        if err:
            errors.append(f"screen batch {batch[0][0]}-{batch[-1][0]} failed: {err}")
            unscreened.update(i for i, _ in batch)
            continue
        for f in flags:
            r = rows[f["i"]]
            # Hundreds of VA buys carry "EO 14398" as a boilerplate clause;
            # Haiku flags them regardless of the instructions. They're
            # rolled up in executive_order_citations instead.
            if EO_RE.search(r.get("raw_description") or "") and "Veterans Affairs" in (r.get("awarding_agency") or ""):
                continue
            tiers[f["i"]] = {"tier": f["tier"], "why": f.get("why", "")}
    for i, r in indexed:
        if i in tiers or i in unscreened:
            continue
        m = SAFETY_NET_RE.search(f"{r.get('raw_description') or ''} {r.get('awarding_sub_agency') or ''}")
        if m:
            tiers[i] = {"tier": "keyword", "why": f"matched '{m.group(0)}' (Haiku called it routine)"}
    stats = {"batches": len(batches), "screened_ok": len(batches) - sum(1 for *_, e in results if e),
             "haiku_cost_usd": round(cost, 4)}
    return tiers, unscreened, stats, errors


def eo_citations(rows):
    eo, eo_usd = Counter(), Counter()
    for r in rows:
        m = EO_RE.search(r.get("raw_description") or "")
        if m:
            k = f"EO {m.group(1) or m.group(2)}"
            eo[k] += 1
            eo_usd[k] += r.get("transaction_amount") or 0
    return {k: {"awards": n, "total_usd": round(eo_usd[k])} for k, n in eo.most_common(5)}
