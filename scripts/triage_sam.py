#!/usr/bin/env python3
"""Cheap-model screen of SAM.gov notices for the Feds edition (solicitations
originally; since 2026-10-10 every notice type -- award, special, sources
sought, solicitation, combined synopsis, presolicitation -- each output under
`notices_by_type` for its own section).

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

Every attachment (added 2026-10-10): screen_sam_docs.py has Haiku read the
text of every file on every notice (cached per file in
data/sam-docs-read.json). Each notice here gets `document_findings` (verbatim-
quoted newsworthy facts) and `document_coverage` (what was and wasn't read,
and why). A solicitation Haiku called routine by its title is promoted (tier
"document") when its files turned up a finding. A notice with a file still
unread for a retryable reason (time budget, failed call) is not marked seen,
so the next edition picks it up.

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
import screen_sam_docs
import vendor_history
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
# Each its own section of the edition, in this order, after Justifications.
TYPE_ORDER = [("a", "Award Notices"), ("o", "Solicitations"), ("k", "Combined Synopsis/Solicitations"),
              ("p", "Presolicitations"), ("s", "Special Notices"), ("r", "Sources Sought")]
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
    "You screen federal contract notices for a Washington investigative "
    "reporter. Reply with a JSON array only, no prose, no code fences."
)

INSTRUCTIONS = """Below are federal contract notices posted to SAM.gov -- solicitations, combined synopses/solicitations, presolicitations, award notices, special notices and sources-sought notices -- one per line:
index | notice type | agency | title | award (award notices: awardee, amount) | description (may be truncated)

Tier each one:
The one test is NEWSWORTHINESS: would a Washington investigative reporter plausibly want to write about it, or at least know about it? The categories below are examples of what tends to be newsworthy, not a complete list -- flag anything newsworthy for any other reason too (a scandal-adjacent vendor, a policy shift visible in what the government is buying, a strange or alarming purpose, an eye-popping size, a first-of-its-kind buy, something tied to a current news story).

- "priority": clearly newsworthy. Examples: tied to Washington, D.C. (District of Columbia government, federal property/facilities in D.C., National Capital Region, National Mall, D.C. National Guard); the White House, Executive Office of the President or presidential operations (Air Force One, Marine One, Camp David, White House Military Office, White House Communications Agency); the war or any active U.S. military operation (combat-theater support, munitions replenishment, deployments, emergency wartime buys); MAJOR work on a named Navy or Coast Guard vessel (new construction or a new ship class, a major overhaul, refueling or mid-life modernization, battle- or collision-damage repair, replacing a whole main system like engines or propulsion, dismantlement/disposal); the U.S. Secret Service; or anything unusual or urgent (emergency/expedited/urgent-and-compelling buys, odd subject matter for the agency, immigration detention, deportation flights, border wall, election security, federal law-enforcement deployments, politically sensitive buyers or vendors), or anything else a reporter would clearly want.
- "notable": not priority, but a reporter might plausibly care (a large or well-known program, major systems, IT modernization, a surprising buyer/purpose, anything that made you pause).
- "routine": run-of-the-mill parts, supplies, repairs, maintenance, janitorial, facilities upkeep, standard services. Most Defense Logistics Agency national-stock-number parts buys are routine, INCLUDING a spare part whose description merely lists the ships, submarines or aircraft it fits -- naming a USS in a parts list does not make it priority (at most notable, if the part itself is unusual). Small ship maintenance is routine too, even on a named vessel: scheduled drydocks and dockside availabilities, inspections, refrigeration/HVAC/piping repairs, minor alterations, single parts.

For award notices, also weigh who won and how much: a large award, a politically connected or scandal-adjacent awardee, a small or unknown company winning something big, an award with no competition. For special notices: intent-to-sole-source notices, industry days for major programs, policy announcements. For sources sought: what the agency is planning to buy next.

When unsure between notable and routine, choose notable.

Output ONLY the priority and notable items (omit routine ones) as a JSON array:
[{"i": <index>, "tier": "priority" | "notable", "why": "<under 15 words>"}]
Output [] if none qualify.

NOTICES:
"""


def notice_id(item):
    m = re.search(r"/opp/([^/]+)/view", item.get("uiLink") or "")
    return item.get("noticeId") or (m.group(1) if m else None)


def short_agency(path):
    parts = [p.strip() for p in (path or "").split(".") if p.strip()]
    return " / ".join(parts[:2] + parts[-1:]) if len(parts) > 3 else " / ".join(parts)


def award_text(s):
    a = s.get("award") or {}
    try:
        amt = f"${float(a['amount']):,.0f}" if a.get("amount") else ""
    except ValueError:
        amt = str(a.get("amount"))
    return ", ".join(x for x in (a.get("awardee"), amt) if x)


def line_for(i, s):
    desc = (s.get("description_text") or "")[:800].replace("\n", " ").replace("|", "/")
    return (f"{i} | {s.get('noticeTypeLabel') or ''} | {short_agency(s.get('agency'))} | {s.get('title') or ''} | "
            f"{award_text(s)} | {desc}")


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
              "urgency_from_poor_planning": 5, "outside_agency_mission": 5, "repeated_extension": 4,
              "cites_prior_agency_work": 3, "cites_executive_order": 2}
# A vendor whose main business isn't government work (2026-10-10, after the
# FBI's sole-source J&A to the UFC scored 3/5 in six of six test runs).
UNUSUAL_VENDOR_BONUS = 12
# No federal contract on record in USAspending (vendor_history.py) -- the size
# of the watchlist bonus -- and more when the J&A nonetheless leans on the
# vendor's earlier work for the agency (the UFC pattern).
FIRST_TIME_BONUS = 15
FIRST_TIME_PRIOR_WORK_BONUS = 10


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
    s += UNUSUAL_VENDOR_BONUS if (j.get("vendor_kind") or "").lower().startswith("unusual") else 0
    # A "government contractor" with no history under the J&A's name is far
    # more likely a name mismatch (a subsidiary, a trade name) than a debut.
    if j.get("first_time_vendor") and j.get("vendor_kind") != "government contractor":
        s += FIRST_TIME_BONUS
        s += FIRST_TIME_PRIOR_WORK_BONUS if j.get("first_time_but_cites_agency_experience") else 0
    s += 8 if j.get("keyword_hit") else 0
    s += j.get("_doc_bonus", 0)
    return round(s)


DOC_BONUS = {5: 12, 4: 7, 3: 3}
UNREAD_REASON = {"restricted": "access-restricted on SAM.gov", "unsupported": "no extractable text (video/CAD/etc.)",
                 "failed": "could not be downloaded or read", "queued": "queued, read in a later edition",
                 "retry": "read failed, retrying in a later edition"}


def doc_view(notice, cache, read_now, previously_listed, queue=()):
    """(findings, coverage) for one notice from the per-file cache.
    A finding is `fresh` when its file was first read this run or the
    solicitation was never listed in an edition -- an amendment re-attaching
    files already reported doesn't re-promote them."""
    findings, unread, n_read = [], [], 0
    for a in notice.get("attachments") or []:
        rec = cache.get(a["rid"]) or {}
        status = ("restricted" if a["status"] == "restricted" else "queued" if a["rid"] in queue
                  else rec.get("s") or "queued")
        if status in ("screened", "wage_determination", "boilerplate_only", "no_text"):
            n_read += 1
        elif status in UNREAD_REASON:
            unread.append({"file": a["name"], "reason": UNREAD_REASON[status], "url": a.get("url")})
        fresh = a["rid"] in read_now or not previously_listed
        for f in rec.get("f") or []:
            findings.append({**f, "file": a["name"], "url": a.get("url"), "file_posted": a.get("posted"),
                             "fresh": fresh})
    findings.sort(key=lambda f: (-f["fresh"], -f["verified"], -f["news"]))
    coverage = {"files": len(notice.get("attachments") or []), "read": n_read}
    if unread:
        coverage["unread"] = unread
    if notice.get("links"):
        coverage["external_links"] = sorted({l["host"] for l in notice["links"] if l.get("host")})
    return findings, coverage


def doc_bonus(findings):
    best = max(((DOC_BONUS[f["news"]] if f["verified"] else DOC_BONUS[f["news"]] // 2)
                for f in findings if f["fresh"] and f["news"] in DOC_BONUS), default=0)
    return best


def slim_findings(findings, n=4, context=True):
    out = []
    for f in findings[:n]:
        d = {k: f[k] for k in ("news", "finding", "quote", "verified", "file", "file_posted", "url") if f.get(k) is not None}
        if context and f.get("context"):
            d["context"] = f["context"]
        if not f["fresh"]:
            d["already_read_on_earlier_notice"] = True
        out.append(d)
    return out


def short(s, n):
    s = (s or "").strip()
    return s if len(s) <= n else s[:n].rsplit(" ", 1)[0] + "…"


def build_justifications(js, texts, seen, mode, today, doc_cache=None, read_now=frozenset(), queue=()):
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
    history = vendor_history.check([(x.get("vendor"), (fresh[i].get("award") or {}).get("uei"))
                                    for i, x in recs.items() if x.get("vendor")], today)
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
            **({"source_type": j["source_type"], "promoted_by": j.get("promoted_by")} if j.get("source_type") else {}),
            **({"award": j["award"]} if j.get("award") else {}),
            "document_links": [{"name": a["name"], "url": a["url"]} for a in j.get("attachments") or [] if a.get("url")][:3],
            "document_read": j.get("noticeId") in texts,
            "extracted": bool(x),
        }
        for k in ("vendor", "value_usd", "value_quote", "value_status", "value_verified", "authority",
                  "authority_quote", "authority_verified", "action", "what", "why_no_competition",
                  "red_flags", "flag_evidence", "news", "news_why", "vendor_kind"):
            if k in x:
                r[k] = x[k]
        h = history.get(r.get("vendor")) if r.get("vendor") else None
        if h is not None:
            r["first_time_vendor"] = not h["has_contracts"]
            if h["has_contracts"]:
                r["vendor_federal_history"] = {"first_contract": h["first"], "examples": h["examples"][:2]}
            elif "cites_prior_agency_work" in (r.get("flag_evidence") or {}):
                r["first_time_but_cites_agency_experience"] = True
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
        findings, coverage = doc_view(j, doc_cache or {}, read_now, False, queue)
        if findings:
            r["document_findings"] = slim_findings(findings)
        r["document_coverage"] = coverage
        r["_doc_bonus"] = doc_bonus(findings)
        r["score"] = score(r) if x else 25 + r["_doc_bonus"]  # unextracted: middle of the pack, main pass judges it
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
                      "news_why", "flag_evidence", "value_verified", "authority_verified", "postedDate",
                      "vendor_federal_history"):
                r.pop(k, None)
            if r.get("document_findings"):
                r["document_findings"] = [{k: v for k, v in f.items() if k != "context"}
                                          for f in r["document_findings"][:2]]
    # ledger + seen updates (only committed if the edition is actually sent)
    for r in out:
        if r["_vk"]:
            seen["vendors"].setdefault(r["_vk"], []).append({
                "d": today, "nid": r["_nid"], "vendor": r.get("vendor"), "agency": r["agency"],
                "sol": r["solicitationNumber"], "value_usd": r.get("value_usd"),
                "authority": r.get("authority"), "what": short(r.get("what"), 120), "uiLink": r["uiLink"]})
    for r in out:
        del r["_nid"], r["_vk"], r["_doc_bonus"]
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

    # Every attachment on every notice: Haiku reads what sam_docs.py
    # extracted this run; files read by earlier runs come from the cache.
    read_now, doc_stats, doc_errors = screen_sam_docs.screen(today)
    doc_cache = load_manifest(screen_sam_docs.CACHE_PATH).get("files", {})

    queue = load_manifest(screen_sam_docs.PENDING_PATH).get("files", {})
    justifications, ja_repeats, ja_cost, ja_errors = build_justifications(
        cands.get("justifications") or [], texts, seen, mode, today, doc_cache, read_now, queue)

    # Every non-J&A notice (award, special, sources sought, solicitation,
    # combined, presolicitation). One that fetch_sam.py also routed into
    # `justifications` is written up there, not twice.
    in_ja = {j.get("noticeId") for j in cands.get("justifications") or []}
    all_sols = [n for n in cands.get("notices") or cands.get("solicitations") or [] if notice_id(n) not in in_ja]
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

    docs = {}
    for i, s in indexed:
        prev = seen["flagged_sols"].get(s.get("solicitationNumber") or "")
        docs[i] = doc_view(s, doc_cache, read_now, bool(prev), queue)
        if i in flagged or i in unscreened:
            continue
        fresh = [f for f in docs[i][0] if f["fresh"]]
        if fresh:
            flagged[i] = {"i": i, "tier": "document",
                          "why": f"attachment: {short(fresh[0]['finding'], 160)} (title screen called it routine)"}
            continue
        m = SAFETY_NET_RE.search(f"{s.get('title') or ''} {s.get('description_text') or ''}")
        if m:
            flagged[i] = {"i": i, "tier": "keyword", "why": f"matched '{m.group(0)}' (Haiku called it routine)"}

    keep = []
    for i, s in indexed:
        if i not in flagged and i not in unscreened:
            continue
        rec = {k: v for k, v in s.items() if k not in ("attachments", "links", "noticeId", "pointOfContact")}
        rec["triage_tier"] = flagged[i]["tier"] if i in flagged else "unscreened"
        rec["triage_why"] = flagged[i].get("why", "") if i in flagged else "screening batch failed"
        findings, coverage = docs[i]
        if findings:
            rec["document_findings"] = slim_findings(findings)
        rec["document_coverage"] = coverage
        rec["document_links"] = [{"name": a["name"], "url": a["url"]}
                                 for a in s.get("attachments") or [] if a.get("url")][:3]
        keep.append(rec)
    keep.sort(key=lambda s: ({"priority": 0, "document": 1, "unscreened": 2, "notable": 3, "keyword": 4}[s["triage_tier"]],
                             -max((f["news"] for f in s.get("document_findings") or [] if not f.get("already_read_on_earlier_notice")), default=0)))
    for s in keep:
        prev = seen["flagged_sols"].get(s.get("solicitationNumber") or "")
        if prev:
            s["previously_listed"] = prev["d"]

    # Files on this edition's notices that weren't read, by reason, for the
    # footer -- the reader should know exactly what went unread.
    unread = Counter()
    unread_examples = []
    this_edition = [j for j in cands.get("justifications") or [] if j.get("noticeId") not in seen[mode]] + sols
    for n in this_edition:
        for u in doc_view(n, doc_cache, read_now, True, queue)[1].get("unread", []):
            unread[u["reason"]] += 1
            if len(unread_examples) < 25:
                unread_examples.append({"file": u["file"], "reason": u["reason"], "notice": short(n.get("title"), 90),
                                        "uiLink": n.get("uiLink")})
    link_hosts = Counter(l["host"] for n in this_edition for l in n.get("links") or [] if l.get("host"))

    # Late document findings: a file read this run (usually from the pending
    # queue, or newly attached to a notice an earlier edition already
    # covered) whose notice isn't in this edition. The notice isn't repeated;
    # only what the file turned up is.
    try:
        with open(screen_sam_docs.FILES_PATH) as fh:
            run_files = json.load(fh)
    except (OSError, json.JSONDecodeError):
        run_files = {}
    edition_nids = {n.get("noticeId") or notice_id(n) for n in this_edition}
    late = []
    for rid, rec in read_now.items():
        f = run_files.get(rid) or {}
        if not rec.get("f") or set(f.get("nids") or []) & edition_nids:
            continue
        late.append({
            "noticeTypeLabel": "Justification" if f.get("kind") == "justification" else f.get("type"),
            "title": (f.get("titles") or [None])[0],
            "agency": short_agency(f.get("agency")),
            "solicitationNumber": (f.get("sols") or [None])[0],
            "uiLink": next((u for u in f.get("uiLinks") or [] if u), None),
            "file": f.get("name"),
            "document_findings": slim_findings([{**x, "file": f.get("name"), "url": f.get("url"),
                                                 "file_posted": (f.get("posted") or "")[:10] or None, "fresh": True}
                                                for x in rec["f"]]),
        })
    late.sort(key=lambda x: -max(d["news"] + d["verified"] for d in x["document_findings"]))

    routine_counts = Counter(
        short_agency(s.get("agency")) for i, s in indexed if i not in flagged and i not in unscreened
    )
    # One section per notice type in the edition (2026-10-10).
    by_type = {}
    for t, label in TYPE_ORDER:
        tl = [s for s in all_sols if (s.get("noticeType") or "o") == t]
        new = [s for s in sols if (s.get("noticeType") or "o") == t]
        by_type[t] = {
            "label": label,
            "new": len(new),
            "already_reported": len(tl) - len(new),
            "flagged": [s for s in keep if (s.get("noticeType") or "o") == t],
            "routine_counts_by_agency": dict(Counter(
                short_agency(s.get("agency")) for i, s in indexed
                if (s.get("noticeType") or "o") == t and i not in flagged and i not in unscreened).most_common()),
        }

    # Remember everything this edition saw (routine items are counted in it,
    # so they shouldn't be counted again next time either). A notice is
    # reported once; a file of it read later comes back only as a late
    # document finding, below.
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
            "justifications_promoted_from_other_types": sum(1 for j in justifications if j.get("source_type")),
            "notices_total": len(all_sols),
            "notices_already_reported": sol_repeats,
            "notices_new": len(sols),
            "priority": sum(1 for s in keep if s["triage_tier"] == "priority"),
            "notable": sum(1 for s in keep if s["triage_tier"] == "notable"),
            "keyword": sum(1 for s in keep if s["triage_tier"] == "keyword"),
            "document": sum(1 for s in keep if s["triage_tier"] == "document"),
            "unscreened": len(unscreened),
            "routine": sum(routine_counts.values()),
            "amendments_of_listed": sum(1 for s in keep if s.get("previously_listed")),
            "files_queued_for_later_editions": len(queue),
        },
        # Every attachment on this edition's notices (see screen_sam_docs.py).
        "documents": {
            "notices_with_files": sum(1 for n in this_edition if n.get("attachments")),
            "files": sum(len(n.get("attachments") or []) for n in this_edition),
            "read_this_run": doc_stats,
            "unread_by_reason": dict(unread),
            "unread_examples": unread_examples,
            "external_links_not_followed": dict(link_hosts.most_common()),
        },
        "justifications": justifications,
        "notices_by_type": by_type,
        "late_document_findings": late[:20],
        "errors": ja_errors + errors + doc_errors,
    }
    with open(out_path, "w") as f:
        json.dump(out, f, indent=2)
        f.write("\n")
    print(f"wrote {out_path}: {out['counts']} cost=${cost + ja_cost:.3f}", file=sys.stderr)
    if len(errors) == len(batches) and batches:
        sys.exit(1)


if __name__ == "__main__":
    main()
