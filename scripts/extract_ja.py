#!/usr/bin/env python3
"""Cheap-model structured extraction from each SAM.gov J&A document (added
2026-10-03). Called by triage_sam.py; usable standalone for testing:

    python3 scripts/extract_ja.py data/sam_candidates.json data/sam_documents.json out.json

Haiku reads each justification's full document_text (see sam_docs.py) and
returns vendor / estimated value / FAR authority / action type / stated
reason / red flags / a 1-5 newsworthiness score. Every value and authority
must come with a verbatim quote, and verify() drops any that don't appear in
the document -- J&A templates are full of decoy numbers (approval
thresholds like "approve ($900K to $90 million)", unchecked "( )" authority
boxes), and on 2026-10-01's data naive regex picked a $500M "value" for a
screw and a $4.9B one for a $125M contract.
"""
import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

MODEL = "claude-haiku-4-5-20251001"
BATCH_CHARS = 60000
WORKERS = 4

AUTHORITIES = {
    "only_one_source": "only one responsible source",
    "urgency": "unusual and compelling urgency",
    "industrial_mobilization": "industrial mobilization / expert services",
    "international_agreement": "international agreement",
    "statute": "authorized or required by statute",
    "national_security": "national security",
    "public_interest": "public interest",
    "brand_name": "brand-name item",
    "fair_opportunity_exception": "exception to fair opportunity",
    "limited_sources": "limited sources (schedule order)",
    "simplified_sole_source": "sole source under simplified acquisition",
    "other": "other",
}
RED_FLAGS = [
    "urgency_from_poor_planning",   # urgency the agency created by letting a contract lapse/plan late
    "protest_or_challenge",         # a vendor protested or challenged the sole-source decision
    "incumbent_lock_in",            # only the incumbent can do it because of data rights/knowledge it built up
    "repeated_extension",           # another extension/bridge of an expiring contract
    "after_the_fact",               # posted after award, or a ratification of an unauthorized commitment
    "no_market_research",           # little or no market research / no sources sought
    "cites_executive_order",
    "politically_sensitive",        # immigration enforcement, elections, White House, DOGE, Jan. 6, etc.
    "unusually_large",
    "foreign_vendor",
]

SYSTEM = ("You extract facts from federal Justification & Approval (J&A) documents for an "
          "investigative reporter. Reply with a JSON array only, no prose, no code fences.")

INSTRUCTIONS = """Each document below is a federal agency's justification for buying something without full and open competition (sole-source / limited-source / exception to fair opportunity). For EACH document return one JSON object:

{"i": <index>,
 "vendor": "<company getting the work, as written; 'redacted' if blacked out; null if not named>",
 "value_usd": <number or null>,
 "value_quote": "<VERBATIM text, under 200 chars, copied exactly from the document, that states this action's estimated value; null if none>",
 "value_status": "stated" | "redacted" | "not_stated",
 "authority": one of %(auth)s,
 "authority_quote": "<VERBATIM text, under 200 chars, where the document invokes that authority; null if none>",
 "action": "new_award" | "modification" | "extension" | "bridge" | "ceiling_increase" | "follow_on" | "order" | "other",
 "what": "<under 25 words, plain English: what is being bought and for which office>",
 "why_no_competition": "<under 25 words: the agency's stated reason, plain English>",
 "red_flags": [any of %(flags)s],
 "flag_evidence": {"<each red flag you listed>": "<VERBATIM text, under 200 chars, from the document that shows it>"},
 "news": <1-5>,
 "news_why": "<under 15 words>"}

TRAPS -- read carefully:
- J&A templates list APPROVAL THRESHOLDS ("approve if over $90 million", "contracts between $900K and $90 million", "Simplified Acquisition Threshold of $250,000"). Those are NOT the value. The value is the dollar figure the document gives for THIS purchase (often "estimated value", "total estimated cost", "not to exceed", "ceiling"). For a modification, prefer the amount being added; if only the new total ceiling is given, use that.
- A "$" followed by blank space, "(b)(4)", or "[redacted]" means value_status "redacted", value_usd null.
- Forms list every FAR 6.302 authority with check boxes. Pick only the one actually checked (X, ☒, ✓) or the one the narrative relies on. Other authorities listed with empty boxes "( )" "[ ]" "☐" are NOT invoked.
- Only list a red flag you can back with a quote in flag_evidence (a protest, a lapse, an after-the-fact award must be stated in the document, not inferred).
- Copy quotes character for character from the document. Never paraphrase inside a quote. If you can't find exact supporting text, use null.
- Text may contain OCR errors; quote it as it appears.

"news" scale:
5 = a reporter should read this today (very large value, public-interest or politically sensitive purpose, protest/challenge, after-the-fact award, scandal-adjacent vendor, a policy shift visible in what's being bought)
4 = clearly interesting (large value with a weak rationale, urgency the agency caused itself, repeated bridges, White House/EOP/D.C./war/immigration ties)
3 = notable program or sizable services contract
2 = ordinary sole-source services/software/equipment
1 = routine spare part, NSN replacement part, routine maintenance, license renewal

DOCUMENTS:
"""


def _norm(s):
    return re.sub(r"[^a-z0-9$.]", "", (s or "").lower())


_MONEY = re.compile(r"\$\s?([\d,]+(?:\.\d+)?)\s*(million|billion|mil\b|m\b|b\b|k\b)?", re.IGNORECASE)


def _amounts(text):
    out = []
    for num, unit in _MONEY.findall(text or ""):
        try:
            v = float(num.replace(",", ""))
        except ValueError:
            continue
        u = (unit or "").lower()
        v *= 1e9 if u in ("billion", "b") else 1e6 if u in ("million", "mil", "m") else 1e3 if u == "k" else 1
        out.append(v)
    return out


def verify(rec, doc_text):
    """Drop any value/authority Haiku can't back with an exact quote."""
    norm_doc = _norm(doc_text)
    rec["value_verified"] = False
    q = rec.get("value_quote")
    if rec.get("value_usd") is not None:
        ok = bool(q) and _norm(q) in norm_doc and any(
            abs(a - rec["value_usd"]) <= max(1, 0.01 * rec["value_usd"]) for a in _amounts(q))
        if ok:
            rec["value_verified"] = True
        else:
            rec["value_usd"] = None
            rec["value_status"] = "unverified"
    # A quote with no value_usd (e.g. "ceiling ... will remain unchanged" on an
    # extension) is still useful context -- keep it only if it's real.
    if q and not (_norm(q) in norm_doc):
        rec["value_quote"] = None
    aq = rec.get("authority_quote")
    rec["authority_verified"] = bool(aq) and _norm(aq) in norm_doc
    if rec.get("authority") not in AUTHORITIES:
        rec["authority"] = "other"
    rec["red_flags"] = [f for f in rec.get("red_flags") or [] if f in RED_FLAGS]
    ev = rec.get("flag_evidence") if isinstance(rec.get("flag_evidence"), dict) else {}
    rec["flag_evidence"] = {f: q for f, q in ev.items()
                            if f in rec["red_flags"] and isinstance(q, str) and _norm(q) and _norm(q) in norm_doc}
    try:
        rec["news"] = max(1, min(5, int(rec.get("news") or 1)))
    except (TypeError, ValueError):
        rec["news"] = 1
    return rec


def doc_for(j, text):
    """What Haiku reads for one justification: the J&A itself when we have
    it, else the notice blurb."""
    head = f"TITLE: {j.get('title') or ''}\nAGENCY: {j.get('agency') or ''}\nNOTICE TEXT: {j.get('description_text') or ''}"
    return head + ("\nJ&A DOCUMENT:\n" + text if text else "\n(no J&A document attached)")


RETRY_WAITS = (20, 60)


def run_claude(prompt, system, timeout=600, model=MODEL):
    """One tool-less `claude -p` call, retried: parallel batches sometimes
    exit 1 with nothing on stderr (seen 2026-10-03, rate limiting), and a
    failed batch means its items go unread. Returns the JSON envelope."""
    err = None
    for wait in (0,) + RETRY_WAITS:
        time.sleep(wait)
        proc = subprocess.run(
            ["claude", "-p", "--model", model, "--tools", "", "--system-prompt", system,
             "--output-format", "json"],
            input=prompt, capture_output=True, text=True, timeout=timeout,
            # Thinking off: a simple extraction/sort, and much faster/cheaper.
            env={**os.environ, "MAX_THINKING_TOKENS": "0"},
        )
        if proc.returncode == 0:
            return json.loads(proc.stdout)
        err = f"claude exit {proc.returncode}: {(proc.stderr or proc.stdout)[:300]}"
    raise RuntimeError(err)


def _call(batch):
    prompt = INSTRUCTIONS % {"auth": json.dumps(list(AUTHORITIES)), "flags": json.dumps(RED_FLAGS)}
    prompt += "\n\n".join(f"===== DOCUMENT {i} =====\n{text}" for i, text in batch)
    env = run_claude(prompt, SYSTEM, timeout=900)
    m = re.search(r"\[.*\]", env.get("result") or "", re.DOTALL)
    if not m:
        raise RuntimeError(f"no JSON array in reply: {(env.get('result') or '')[:200]!r}")
    return json.loads(m.group(0)), env.get("total_cost_usd") or 0


def extract(justifications, texts):
    """texts: {noticeId: document_text} from sam_docs.py. Returns
    (records_by_index, cost, errors). Missing indexes = not extracted."""
    docs = [(i, doc_for(j, texts.get(j.get("noticeId")))) for i, j in enumerate(justifications)]
    batches, cur, size = [], [], 0
    for i, t in docs:
        if cur and size + len(t) > BATCH_CHARS:
            batches.append(cur)
            cur, size = [], 0
        cur.append((i, t))
        size += len(t)
    if cur:
        batches.append(cur)

    def run(batch):
        try:
            return batch, *_call(batch), None
        except Exception as e:  # noqa: BLE001
            return batch, [], 0, str(e)

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        results = list(ex.map(run, batches))
    recs, cost, errors = {}, 0.0, []
    for batch, out, c, err in results:
        cost += c
        if err:
            errors.append(f"J&A extraction batch {batch[0][0]}-{batch[-1][0]} failed: {err}")
            continue
        valid = {i for i, _ in batch}
        for r in out:
            if isinstance(r, dict) and r.get("i") in valid:
                recs[r["i"]] = verify(r, docs[r["i"]][1])
    print(f"J&A extraction: {len(recs)}/{len(justifications)} in {len(batches)} batches, ${cost:.3f}", file=sys.stderr)
    return recs, cost, errors


if __name__ == "__main__":
    with open(sys.argv[1]) as f:
        J = json.load(f)["justifications"]
    with open(sys.argv[2]) as f:
        T = json.load(f)
    recs, cost, errors = extract(J, T)
    with open(sys.argv[3], "w") as f:
        json.dump({"records": {str(k): v for k, v in sorted(recs.items())}, "cost": cost, "errors": errors}, f, indent=2)
