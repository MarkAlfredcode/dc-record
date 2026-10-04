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

Justifications (added 2026-10-03): Haiku reads each one's actual J&A
document (extract_ja.py) for vendor / value / authority / red flags, and
this script scores and sorts them, attaching each vendor's earlier
sole-source justifications from the ledger in data/sam-seen.json.

Dedupe (added 2026-10-03): the fetch window is two days and the edition runs
AM and PM, so the same notice used to appear in up to four editions (25-29
of ~100 items repeated edition to edition, measured 2026-09-30..10-03).
data/sam-seen.json remembers every notice already handed to an edition of
the same MODE (subscribers pick AM and/or PM, so each mode dedupes against
itself); repeats are counted, not re-listed, and never re-sent to Haiku.
The workflow commits that file only when the email actually went out.

Output: data/sam_triage.json. On total failure, writes nothing usable and
the main prompt falls back to data/sam_candidates.json.
"""
import json
import os
import re
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

import extract_ja
from dc_common import load_manifest, save_manifest, today_la
from dc_extras import run_mode

IN_PATH = "data/sam_candidates.json"
DOCS_PATH = "data/sam_documents.json"
OUT_PATH = "data/sam_triage.json"
SEEN_PATH = "data/sam-seen.json"
WATCHLIST_PATH = "data/watchlist.json"
SEEN_DAYS = 3          # notices: the fetch window is 2 days
FLAGGED_SOL_DAYS = 60  # solicitation numbers we listed, to spot later amendments
VENDOR_DAYS = 365
EXTRACT_DAYS = 4       # J&A extraction cache shared by AM and PM
# Main pass gets the J&A text itself (to check its write-up against) only
# for the top of the list; everything else it sees as extracted fields.
DOC_EXCERPT_TOP = 8
DOC_EXCERPT_CHARS = 6000
ROUTINE_SCORE = 30  # news 1-2 with no bonus signals
MODEL = "claude-haiku-4-5-20251001"
BATCH_SIZE = 80
WORKERS = 3

# Deterministic safety net: Haiku's routine calls vary run to run (in testing
# it sent the National Zoo and a border-security post to "routine" on one run
# and not another), and anything it calls routine never reaches the main
# pass. Any routine-tiered solicitation matching these is passed through
# anyway as tier "keyword" -- ~10 extra items/day on 2026-10-01's data.
SAFETY_NET_RE = re.compile(
    r"washington,? d\.?c|\bd\.c\.|district of columbia|national capital region|"
    r"national mall|white house|executive office of the president|secret service|"
    r"camp david|air force one|marine one|capitol|smithsonian|national zoo|"
    r"arlington national cemetery|detention|deport|removal flight|immigration|"
    r"border|port of entry|unusual and compelling|urgent|emergency",
    re.IGNORECASE,
)

# The J&A version of the net. Narrower than SAFETY_NET_RE because it runs
# over whole documents: "Washington, DC" is in half the letterheads,
# "cross-border" in shipping terms, and every J&A form prints an unchecked
# "( ) Unusual and Compelling Urgency" box. Urgency is caught by the
# extracted `authority` field instead.
JA_NET_RE = re.compile(
    r"white house|executive office of the president|secret service|camp david|"
    r"air force one|marine one|national mall|district of columbia government|"
    r"d\.?c\.? national guard|capitol police|u\.s\. capitol|detention|deport|removal flight|"
    r"immigration|border wall|border patrol|port of entry|election security|doge\b|"
    r"department of government efficiency",
    re.IGNORECASE,
)

SYSTEM = (
    "You screen federal contract solicitations for a Washington investigative "
    "reporter. Reply with a JSON array only, no prose, no code fences."
)

INSTRUCTIONS = """Below are federal contract solicitations posted to SAM.gov, one per line:
index | agency | title | description (may be truncated)

Tier each one:
The one test is NEWSWORTHINESS: would a Washington investigative reporter plausibly want to write about it, or at least know about it? The categories below are examples of what tends to be newsworthy, not a complete list -- flag anything newsworthy for any other reason too (a scandal-adjacent vendor, a policy shift visible in what the government is buying, a strange or alarming purpose, an eye-popping size, a first-of-its-kind buy, something tied to a current news story).

- "priority": clearly newsworthy. Examples: tied to Washington, D.C. (District of Columbia government, federal property/facilities in D.C., National Capital Region, National Mall, D.C. National Guard); the White House, Executive Office of the President or presidential operations (Air Force One, Marine One, Camp David, White House Military Office, White House Communications Agency); the war or any active U.S. military operation (combat-theater support, munitions replenishment, deployments, emergency wartime buys); MAJOR work on a named Navy or Coast Guard vessel (new construction or a new ship class, a major overhaul, refueling or mid-life modernization, battle- or collision-damage repair, replacing a whole main system like engines or propulsion, dismantlement/disposal); the U.S. Secret Service; or anything unusual or urgent (emergency/expedited/urgent-and-compelling buys, odd subject matter for the agency, immigration detention, deportation flights, border wall, election security, federal law-enforcement deployments, politically sensitive buyers or vendors), or anything else a reporter would clearly want.
- "notable": not priority, but a reporter might plausibly care (a large or well-known program, major systems, IT modernization, a surprising buyer/purpose, anything that made you pause).
- "routine": run-of-the-mill parts, supplies, repairs, maintenance, janitorial, facilities upkeep, standard services. Most Defense Logistics Agency national-stock-number parts buys are routine, INCLUDING a spare part whose description merely lists the ships, submarines or aircraft it fits -- naming a USS in a parts list does not make it priority (at most notable, if the part itself is unusual). Small ship maintenance is routine too, even on a named vessel: scheduled drydocks and dockside availabilities, inspections, refrigeration/HVAC/piping repairs, minor alterations, single parts.

When unsure between notable and routine, choose notable.

Output ONLY the priority and notable items (omit routine ones) as a JSON array:
[{"i": <index>, "tier": "priority" | "notable", "why": "<under 15 words>"}]
Output [] if none qualify.

SOLICITATIONS:
"""


def notice_id(item):
    m = re.search(r"contract/opp/([^/]+)/view", item.get("uiLink") or "")
    return m.group(1) if m else None


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
    # Thinking off inside run_claude: measured on a real 80-item batch,
    # 101s -> 6s and roughly half the cost; this is a sort, not reasoning.
    envelope = extract_ja.run_claude(prompt, SYSTEM, timeout=600, model=MODEL)
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


_SUFFIX = re.compile(r"\b(incorporated|inc|llc|l\.l\.c|corp|corporation|co|company|ltd|limited|lp|llp|plc|gmbh|the|dba)\b")


def vendor_key(name):
    if not name or name.strip().lower() in ("redacted", "null", "none", "unknown"):
        return None
    k = _SUFFIX.sub(" ", re.sub(r"[^a-z0-9& ]", " ", name.lower().split("(")[0]))
    k = " ".join(k.split())
    return k or None


def watchlist_matcher():
    try:
        with open(WATCHLIST_PATH) as f:
            entries = json.load(f).get("entries") or []
    except Exception:  # noqa: BLE001
        return lambda text: None
    pats = [(e["name"], re.compile(r"\b(?:" + "|".join(e.get("aliases") or [re.escape(e["name"])]) + r")\b", re.I))
            for e in entries]
    return lambda text: next((n for n, p in pats if p.search(text or "")), None)


AUTH_BONUS = {"public_interest": 15, "urgency": 6, "national_security": 4}
FLAG_BONUS = {"protest_or_challenge": 8, "politically_sensitive": 6, "after_the_fact": 6,
              "urgency_from_poor_planning": 5, "repeated_extension": 4, "cites_executive_order": 2}


def score(j):
    s = j.get("news", 1) * 10 + AUTH_BONUS.get(j.get("authority"), 0)
    v = j.get("value_usd") or 0
    s += 12 if v >= 1e8 else 7 if v >= 1e7 else 2 if v >= 1e6 else 0
    # A red flag Haiku could quote from the document counts in full; one it
    # only inferred counts half (it inferred a protest that wasn't in one J&A).
    ev = j.get("flag_evidence") or {}
    s += sum(FLAG_BONUS.get(f, 0) * (1 if f in ev else 0.5) for f in j.get("red_flags") or [])
    s += 4 if len(j.get("vendor_history") or []) >= 2 else 0
    s += 15 if j.get("watchlist") else 0
    s += 8 if j.get("keyword_hit") else 0
    return round(s)


def short(s, n):
    s = (s or "").strip()
    return s if len(s) <= n else s[:n].rsplit(" ", 1)[0] + "…"


def build_justifications(js, texts, seen, mode, today):
    """Returns (records for the main pass, repeat count, cost, errors)."""
    fresh = [j for j in js if j.get("noticeId") not in seen[mode]]
    repeats = len(js) - len(fresh)
    # The other mode's edition usually already paid to extract these.
    cache = seen["extracts"]
    todo = [j for j in fresh if j.get("noticeId") not in cache]
    got, cost, errors = ({}, 0.0, []) if not todo else extract_ja.extract(todo, texts)
    for i, rec in got.items():
        if todo[i].get("noticeId"):
            cache[todo[i]["noticeId"]] = {**rec, "d": today}
    recs = {i: cache[j["noticeId"]] for i, j in enumerate(fresh) if j.get("noticeId") in cache}
    match = watchlist_matcher()
    out = []
    for i, j in enumerate(fresh):
        x = recs.get(i, {})
        r = {
            "title": j.get("title"),
            "agency": short_agency(j.get("agency")),
            "solicitationNumber": j.get("solicitationNumber"),
            "postedDate": j.get("postedDate"),
            "uiLink": j.get("uiLink"),
            "notice_text": short(j.get("description_text"), 900),
            "document_links": [a for a in j.get("attachments") or [] if a.get("url")][:2],
            "document_read": j.get("noticeId") in texts,
            "extracted": bool(x),
        }
        for k in ("vendor", "value_usd", "value_quote", "value_status", "value_verified", "authority",
                  "authority_quote", "authority_verified", "action", "what", "why_no_competition",
                  "red_flags", "flag_evidence", "news", "news_why"):
            if k in x:
                r[k] = x[k]
        vk = vendor_key(r.get("vendor"))
        r["vendor_history"] = [h for h in seen["vendors"].get(vk, []) if h["nid"] != j.get("noticeId")] if vk else []
        r["watchlist"] = match(" ".join(str(r.get(k) or "") for k in ("vendor", "title", "what")))
        # Same D.C./White House/Secret Service/border net as solicitations,
        # run over the full notice + J&A text: on 2026-10-01 a "White House
        # threat detection warning" sat past where the notice text was cut.
        hay = f"{j.get('title') or ''} {j.get('description_text') or ''} {texts.get(j.get('noticeId')) or ''}"
        m = next(JA_NET_RE.finditer(hay), None)
        if m:
            r["keyword_hit"] = " ".join(hay[max(0, m.start() - 120):m.end() + 120].split())
        r["score"] = score(r) if x else 25  # unextracted: middle of the pack, main pass judges it
        r["_nid"], r["_vk"] = j.get("noticeId"), vk
        out.append(r)
    out.sort(key=lambda r: -r["score"])
    for rank, r in enumerate(out):
        if (rank < DOC_EXCERPT_TOP or r.get("keyword_hit")) and r["_nid"] in texts:
            r["document_excerpt"] = texts[r["_nid"]][:DOC_EXCERPT_CHARS]
        elif r["extracted"] and r["score"] < ROUTINE_SCORE:
            # Routine (spare parts, renewals): the main pass only one-lines
            # these, so it gets just enough to do that.
            for k in ("notice_text", "value_quote", "authority_quote", "why_no_competition",
                      "news_why", "flag_evidence", "value_verified", "authority_verified", "postedDate"):
                r.pop(k, None)
    # ledger + seen updates (only committed if the edition is actually sent)
    for r in out:
        if r["_vk"]:
            seen["vendors"].setdefault(r["_vk"], []).append({
                "d": today, "nid": r["_nid"], "vendor": r.get("vendor"), "agency": r["agency"],
                "sol": r["solicitationNumber"], "value_usd": r.get("value_usd"),
                "authority": r.get("authority"), "what": short(r.get("what"), 120), "uiLink": r["uiLink"]})
        del r["_nid"], r["_vk"]
    return out, repeats, cost, errors


def prune(seen, today):
    def keep(d, days):
        return d >= (date.fromisoformat(today) - timedelta(days=days)).isoformat()
    for mode in ("am", "pm"):
        seen[mode] = {k: d for k, d in seen.get(mode, {}).items() if keep(d, SEEN_DAYS)}
    seen["extracts"] = {k: v for k, v in seen.get("extracts", {}).items() if keep(v["d"], EXTRACT_DAYS)}
    seen["flagged_sols"] = {k: v for k, v in seen.get("flagged_sols", {}).items() if keep(v["d"], FLAGGED_SOL_DAYS)}
    vendors = {}
    for k, hist in seen.get("vendors", {}).items():
        dedup = {h["nid"]: h for h in hist if keep(h["d"], VENDOR_DAYS)}
        if dedup:
            vendors[k] = sorted(dedup.values(), key=lambda h: h["d"])
    seen["vendors"] = vendors
    return seen


def main():
    in_path = sys.argv[1] if len(sys.argv) > 1 else IN_PATH
    out_path = sys.argv[2] if len(sys.argv) > 2 else OUT_PATH
    # Never let a stale file from an earlier run stand in for today's screen.
    if os.path.exists(out_path):
        os.remove(out_path)
    with open(in_path) as f:
        cands = json.load(f)
    try:
        with open(DOCS_PATH) as f:
            texts = json.load(f)
    except Exception:  # noqa: BLE001
        texts = {}
    mode, today = run_mode(), today_la()
    seen = prune(load_manifest(SEEN_PATH), today)
    for j in cands.get("justifications") or []:
        j.setdefault("noticeId", notice_id(j))

    justifications, ja_repeats, ja_cost, ja_errors = build_justifications(
        cands.get("justifications") or [], texts, seen, mode, today)

    all_sols = cands.get("solicitations") or []
    sols = [s for s in all_sols if notice_id(s) not in seen[mode]]
    sol_repeats = len(all_sols) - len(sols)
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

    for i, s in indexed:
        if i in flagged or i in unscreened:
            continue
        m = SAFETY_NET_RE.search(f"{s.get('title') or ''} {s.get('description_text') or ''}")
        if m:
            flagged[i] = {"i": i, "tier": "keyword", "why": f"matched '{m.group(0)}' (Haiku called it routine)"}

    keep = []
    for i, s in indexed:
        if i in flagged:
            keep.append({**s, "triage_tier": flagged[i]["tier"], "triage_why": flagged[i].get("why", "")})
        elif i in unscreened:
            keep.append({**s, "triage_tier": "unscreened", "triage_why": "screening batch failed"})
    keep.sort(key=lambda s: {"priority": 0, "unscreened": 1, "notable": 2, "keyword": 3}[s["triage_tier"]])
    for s in keep:
        prev = seen["flagged_sols"].get(s.get("solicitationNumber") or "")
        if prev:
            s["previously_listed"] = prev["d"]

    routine_counts = Counter(
        short_agency(s.get("agency")) for i, s in indexed if i not in flagged and i not in unscreened
    )

    # Remember everything this edition saw (routine items are counted in it,
    # so they shouldn't be counted again next time either).
    for j in cands.get("justifications") or []:
        if j.get("noticeId"):
            seen[mode][j["noticeId"]] = today
    for s in sols:
        if notice_id(s):
            seen[mode][notice_id(s)] = today
    for s in keep:
        if s.get("solicitationNumber") and not s.get("previously_listed"):
            seen["flagged_sols"][s["solicitationNumber"]] = {"d": today, "title": short(s.get("title"), 100)}
    save_manifest(SEEN_PATH, seen)

    out = {
        # Everything else the main pass needs from sam_candidates.json, so it
        # never has to open that file (and its ~700 descriptions) at all.
        "window": cands.get("window"),
        "reachable": cands.get("reachable"),
        "fetch_errors": cands.get("errors") or [],
        "fetch_counts": cands.get("counts") or {},
        "model": MODEL,
        "screened_ok": len(batches) - len(errors),
        "batches": len(batches),
        "counts": {
            "justifications_total": len(cands.get("justifications") or []),
            "justifications_new": len(justifications),
            "justifications_already_reported": ja_repeats,
            "justification_documents_read": sum(1 for j in justifications if j["document_read"]),
            "justifications_extracted": sum(1 for j in justifications if j["extracted"]),
            "solicitations_total": len(all_sols),
            "solicitations_already_reported": sol_repeats,
            "solicitations_new": len(sols),
            "priority": sum(1 for s in keep if s["triage_tier"] == "priority"),
            "notable": sum(1 for s in keep if s["triage_tier"] == "notable"),
            "keyword": sum(1 for s in keep if s["triage_tier"] == "keyword"),
            "unscreened": len(unscreened),
            "routine": sum(routine_counts.values()),
            "amendments_of_listed": sum(1 for s in keep if s.get("previously_listed")),
        },
        "justifications": justifications,
        "flagged_solicitations": keep,
        "routine_counts_by_agency": dict(routine_counts.most_common()),
        "errors": ja_errors + errors,
    }
    with open(out_path, "w") as f:
        json.dump(out, f, indent=2)
        f.write("\n")
    print(f"wrote {out_path}: {out['counts']} cost=${cost + ja_cost:.3f}", file=sys.stderr)
    if len(errors) == len(batches) and batches:
        sys.exit(1)


if __name__ == "__main__":
    main()
