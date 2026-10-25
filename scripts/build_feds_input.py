#!/usr/bin/env python3
"""One compact text bundle for The Feds' one-turn main pass (added
2026-10-12), built from the triage files the multi-turn pass used to page
through with Read (data/sam_triage.json alone runs 0.3-2.4MB): 8-13 turns,
$0.38-0.54 a weekday run.

Every field the prompt's editorial rules name is kept under the same name
(vendor_kind, first_time_vendor, value_status, flag_evidence, keyword_hit,
document_findings with quote/verified/context, previously_listed, sweep,
triage_tier...). What's cut is bulk, not facts:
- a notice's description_text is capped at 800 characters (300 for
  document/keyword-tier notices, whose attachment finding is the point);
  the full text is one click away;
- a finding's `context` is kept only when the quote was verified and the
  finding scored 4+ (450 characters);
- justifications get the full record for the top FULL_J by score plus any
  with a keyword hit, news >= 4, or an unusual/first-time vendor; the rest
  one line each, with action and a short `what` so each gets a headline;
- every link is a ref (#N12 notice, #D40 document, #U7 USAspending award)
  that run_pass.py expands, so URLs are never retyped.
- White House and D.C. items (scripts/topics.py regex tags) are pulled out of
  their type-by-type sections into two buckets of their own, one per section
  the main pass writes (added 2026-10-24); every record keeps its full format.
If a triage file is missing or its screen failed, the candidate file it
came from is summarized instead and the bundle says so (the prompt's
fallback rules).

Output: data/feds_input.txt, data/feds_input.json ({"refs": {...}}).
"""
import json
import os
import re
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dc_common import today_la  # noqa: E402
from dc_extras import load_json, run_mode  # noqa: E402
from fetch_campaigns import extras_digest  # noqa: E402
from topics import award_topic, notice_topic  # noqa: E402

OUT_TXT = "data/feds_input.txt"
OUT_JSON = "data/feds_input.json"
DESC_CHARS = 800          # priority / notable notices
DESC_CHARS_MINOR = 300    # document / keyword tier: the attachment finding is the substance
CONTEXT_CHARS = 450
J_DESC_CHARS = 600
J_WHAT_CHARS = 160        # one-line justifications: enough to write a headline
EXCERPT_CHARS = 4000
FULL_J = 15
TYPES = [("a", "Award Notices"), ("o", "Solicitations"), ("k", "Combined Synopsis/Solicitations"),
         ("p", "Presolicitations"), ("s", "Special Notices"), ("r", "Sources Sought")]

refs = {}
_url_ref = {}


def ref(url, prefix):
    if not url:
        return ""
    if url in _url_ref:
        return _url_ref[url]
    k = f"{prefix}{len(refs) + 1}"
    refs[k] = url
    _url_ref[url] = f"#{k}"
    return f"#{k}"


def sq(s, n=None):
    s = " ".join(str(s or "").split())
    return s if n is None or len(s) <= n else s[:n] + "…"


def money(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return str(v) if v else ""
    return f"-${abs(v):,.0f}" if v < 0 else f"${v:,.0f}"


def findings(fs, indent="    "):
    out = []
    for f in fs or []:
        bits = [f"news {f.get('news')}", "VERIFIED" if f.get("verified") else "unverified (lead only)"]
        if f.get("kind") in ("metadata", "provenance"):
            bits.append("FILE METADATA/LINKS, not body text")
        if f.get("already_read_on_earlier_notice"):
            bits.append("already_read_on_earlier_notice")
        line = (f"{indent}- finding [{', '.join(bits)}]: {sq(f.get('finding'))} | quote: \"{sq(f.get('quote'))}\" | "
                f"file: {f.get('file')} (file_posted {f.get('file_posted') or '?'}) {ref(f.get('url'), 'D')}")
        out.append(line)
        if f.get("verified") and f.get("context") and (f.get("news") or 0) >= 4:
            out.append(f"{indent}  context: {sq(f['context'], CONTEXT_CHARS)}")
    return out


def coverage(c):
    if not c:
        return ""
    unread = c.get("unread") or []
    reasons = {}
    for u in unread:
        r = u.get("reason") if isinstance(u, dict) else str(u)
        reasons[r] = reasons.get(r, 0) + 1
    s = f"files {c.get('files', 0)}, read {c.get('read', 0)}"
    if reasons:
        s += ", unread: " + ", ".join(f"{v} {k}" for k, v in reasons.items())
    if c.get("external_links"):
        s += f", {len(c['external_links'])} outside-portal links"
    return s


def justification(j, n, full):
    head = (f"[J{n}] score {j.get('score')} | {sq(j.get('agency'))} | {sq(j.get('title'))} | sol {j.get('solicitationNumber')} | "
            f"posted {str(j.get('postedDate') or '')[:10]} | notice {ref(j.get('uiLink'), 'N')}")
    if j.get("source_type"):
        head += f" | POSTED AS {j['source_type']} (promoted_by {j.get('promoted_by')})"
    value = (money(j.get("value_usd")) + " (verified)") if j.get("value_usd") else f"value_status {j.get('value_status')}"
    if not full:
        return [head + f" | vendor {j.get('vendor') or '?'} | {value} | action {j.get('action')} | news {j.get('news')}"
                + f" | what: {sq(j.get('what'), J_WHAT_CHARS)}"]
    L = [head]
    fh = j.get("vendor_federal_history") or {}
    L.append(f"    vendor: {j.get('vendor') or '?'} | vendor_kind: {j.get('vendor_kind')} | first_time_vendor: "
             f"{j.get('first_time_vendor')}" + (f" (first contract {fh.get('first_contract')})" if fh.get("first_contract") else "")
             + (" | first_time_but_cites_agency_experience" if j.get("first_time_but_cites_agency_experience") else "")
             + (f" | watchlist: {j['watchlist']}" if j.get("watchlist") else ""))
    L.append(f"    value: {value}" + (f" | value_quote: \"{sq(j.get('value_quote'), 200)}\"" if j.get("value_quote") else "")
             + f" | authority: {j.get('authority')}" + (f" (\"{sq(j.get('authority_quote'), 200)}\")" if j.get("authority_quote") else "")
             + f" | action: {j.get('action')}")
    L.append(f"    what: {sq(j.get('what'))} | why_no_competition: {sq(j.get('why_no_competition'))}")
    if j.get("red_flags"):
        ev = j.get("flag_evidence") or {}
        L.append("    red_flags: " + "; ".join(f"{r}" + (f" (evidence: \"{sq(ev[r], 200)}\")" if ev.get(r) else " (no verified evidence)")
                                          for r in j["red_flags"]))
    if j.get("keyword_hit"):
        L.append(f"    keyword_hit: {sq(j['keyword_hit'], 400)}")
    L.append(f"    news {j.get('news')}: {sq(j.get('news_why'))}")
    if j.get("award"):
        a = j["award"]
        L.append(f"    award: {a.get('awardee')} {money(a.get('amount'))} {a.get('date') or ''}")
    if j.get("vendor_history"):
        L.append("    vendor_history: " + "; ".join(f"{h.get('date')} {sq(h.get('agency'), 60)} {money(h.get('value_usd') or h.get('value'))} "
                                               f"{sq(h.get('what'), 80)} {ref(h.get('link') or h.get('uiLink'), 'N')}"
                                               for h in j["vendor_history"][:6]))
    docs = j.get("document_links") or []
    if docs:
        L.append("    J&A document: " + ", ".join(f"{d.get('name')} {ref(d.get('url'), 'D')}" for d in docs[:3]))
    if not j.get("document_read", True):
        L.append("    document_read: false (only the notice blurb was available)")
    if j.get("extracted") is False:
        L.append(f"    extracted: false -- notice text: {sq(j.get('notice_text'), J_DESC_CHARS)}")
    L += findings(j.get("document_findings"))
    if j.get("document_excerpt"):
        L.append(f"    document_excerpt: {sq(j['document_excerpt'], EXCERPT_CHARS)}")
    return L


def notice(x, n):
    L = [f"[{n}] {x.get('triage_tier')} | {sq(x.get('agency'))} | {sq(x.get('title'))} | sol {x.get('solicitationNumber')} | "
         f"posted {str(x.get('postedDate') or '')[:10]} | notice {ref(x.get('uiLink'), 'N')}"
         + (f" | previously_listed {x['previously_listed']}" if x.get("previously_listed") else "")]
    if x.get("triage_why"):
        L.append(f"    triage_why: {sq(x['triage_why'])}")
    if x.get("award"):
        a = x["award"]
        L.append(f"    award: awardee {a.get('awardee')} | amount {money(a.get('amount')) or 'none posted'} | {a.get('date') or ''} | {a.get('number') or ''}")
    if x.get("description_text"):
        cap = DESC_CHARS if x.get("triage_tier") in ("priority", "notable", "termination", "size") else DESC_CHARS_MINOR
        L.append(f"    description: {sq(x['description_text'], cap)}")
    L += findings(x.get("document_findings"))
    cov = coverage(x.get("document_coverage"))
    if cov and "unread" in cov:
        L.append(f"    document_coverage: {cov}")
    return L


def split_sam(t):
    """(t without the White House / D.C. items, {"white_house": [...], "dc": [...]}),
    each bucket entry (kind, record, hit). Doesn't touch the triage file."""
    buckets = {"white_house": [], "dc": []}
    moved = {}

    def keep(x, kind):
        topic, hit = notice_topic(x)
        if topic:
            buckets[topic].append((kind, x, hit))
            moved[kind] = moved.get(kind, 0) + 1
        return not topic

    t2 = dict(t)
    t2["justifications"] = [j for j in t.get("justifications") or [] if keep(j, "J")]
    nb = {}
    for ty, v in (t.get("notices_by_type") or {}).items():
        v2 = dict(v)
        v2["flagged"] = [x for x in v.get("flagged") or [] if keep(x, ty)]
        nb[ty] = v2
    t2["notices_by_type"] = nb
    t2["_moved"] = moved
    return t2, buckets


def sam_section(t):
    L = []
    c = t.get("counts") or {}
    L.append(f"counts: {json.dumps(c, separators=(',', ':'))}")
    L.append(f"fetch_counts: {json.dumps(t.get('fetch_counts') or {}, separators=(',', ':'))}")
    errs = (t.get("errors") or []) + (t.get("fetch_errors") or [])
    if errs:
        L.append("errors: " + "; ".join(sq(e, 200) for e in errs))
    docs = t.get("documents") or {}
    L.append("documents: " + json.dumps({k: docs.get(k) for k in ("notices_with_files", "files", "unread_by_reason",
                                                                  "external_links_not_followed")}, separators=(",", ":"))
             + f" | read_this_run: {json.dumps((docs.get('read_this_run') or {}), separators=(',', ':'))}")
    js = t.get("justifications") or []
    full = set(range(min(FULL_J, len(js))))
    for i, j in enumerate(js):
        if j.get("keyword_hit") or (j.get("news") or 0) >= 4 or j.get("first_time_vendor") \
                or str(j.get("vendor_kind") or "").startswith("unusual"):
            full.add(i)
    mvj = (t.get("_moved") or {}).get("J")
    L += ["", f"=== SAM: SOLE-SOURCE JUSTIFICATIONS ({len(js)} new" + (f" here, {mvj} more are in the White House / D.C. parts above" if mvj else "")
          + f"; full record for {len(full)}, one line for the rest) ==="]
    for i, j in enumerate(js):
        L += justification(j, i + 1, i in full)
    nb = t.get("notices_by_type") or {}
    for ty, label in TYPES:
        v = nb.get(ty) or {}
        fl = v.get("flagged") or []
        mv = (t.get("_moved") or {}).get(ty)
        L += ["", f"=== SAM: {label.upper()} (new {v.get('new', 0)}, already_reported {v.get('already_reported', 0)}, "
                  f"flagged {len(fl)}" + (f", {mv} more flagged ones are in the White House / D.C. parts above" if mv else "") + ") ==="]
        for k, x in enumerate(fl):
            L += notice(x, f"{ty.upper()}{k + 1}")
        rc = v.get("routine_counts_by_agency") or {}
        if rc:
            L.append("routine_counts_by_agency: " + "; ".join(f"{sq(a, 70)}: {n}" for a, n in
                                                          sorted(rc.items(), key=lambda kv: -kv[1])))
    late = t.get("late_document_findings") or []
    L += ["", f"=== SAM: LATE DOCUMENT FINDINGS ({len(late)}) ==="]
    for k, x in enumerate(late):
        L.append(f"[LATE{k + 1}] {x.get('noticeTypeLabel')} | {sq(x.get('agency'))} | {sq(x.get('title'))} | "
                 f"sol {x.get('solicitationNumber')} | notice {ref(x.get('uiLink'), 'N')} | file {x.get('file')}")
        L += findings(x.get("document_findings"))
    return L


def sam_fallback(cands):
    L = ["!!! SAM SCREEN FAILED: below is the raw candidate list (data/sam_candidates.json) -- no document extraction; "
         "judge every notice yourself and say in known_gaps that the screening pass failed."]
    for j in cands.get("justifications") or []:
        L.append(f"[J] {sq(j.get('agency'), 80)} | {sq(j.get('title'))} | sol {j.get('solicitationNumber')} | "
                 f"{ref(j.get('uiLink'), 'N')} | {sq(j.get('description_text'), 400)}")
    for x in cands.get("notices") or []:
        L.append(f"[{x.get('noticeType')}] {sq(x.get('agency'), 80)} | {sq(x.get('title'))} | sol {x.get('solicitationNumber')} | "
                 f"{ref(x.get('uiLink'), 'N')} | {sq(x.get('description_text'), 250)}")
    return L


def award_line(a, label):
    bits = [f"[{label}] {a.get('triage_tier') or ''}", a.get("recipient"), money(a.get("transaction_amount")),
            f"{a.get('awarding_agency')} / {a.get('awarding_sub_agency')}", f"action_date {a.get('action_date')}"]
    if a.get("sweep"):
        bits.append(f"sweep {a['sweep']}")
    if a.get("modification"):
        bits.append(f"modification {a['modification']}")
    if a.get("action_type"):
        bits.append(f"action_type: {a['action_type']}")
    if a.get("termination"):
        bits.append("TERMINATION")
    bits += [f"raw_description: {sq(a.get('raw_description'), 400)}",
             f"psc: {sq(a.get('product_or_service_description'), 120)}", f"naics: {sq(a.get('naics_description'), 100)}"]
    if a.get("triage_why"):
        bits.append(f"triage_why: {sq(a['triage_why'], 200)}")
    bits.append(f"award {ref(a.get('url'), 'U')}")
    return " | ".join(str(b) for b in bits if b)


def usaspending(buckets):
    """Lines for the USAspending part; awards tagged White House / D.C. go to
    `buckets` (kind "A", record, hit) instead and are counted, not printed."""
    moved = {"white_house": 0, "dc": 0}

    def keep(a):
        topic, hit = award_topic(a)
        if topic:
            buckets[topic].append(("A", a, hit))
            moved[topic] += 1
        return not topic

    cands = load_json("data/usaspending_candidates.json", {}) or {}
    if cands.get("skipped") == "pm":
        return ["PM EDITION: USAspending is pulled in the AM edition only; write NO USAspending Contracts section "
                "(assemble.py carries the morning's in). Not a known gap."]
    L = []
    t = load_json("data/usaspending_triage.json", None)
    c = (t or {}).get("counts") or {}
    if not t or (c.get("batches", 0) > 0 and c.get("screened_ok", 0) == 0):
        L.append("!!! $2M+ SCREEN FAILED: unsorted candidates below; judge and list every one, and say in known_gaps "
                 "that the $2M+ screen failed.")
        n = 0
        for key in ("new_awards", "modifications", "cuts"):
            L.append(f"-- {key} --")
            for a in [a for a in cands.get(key) or [] if keep(a)]:
                n += 1
                L.append(award_line(a, f"A{n}"))
    else:
        L.append(f"counts: {json.dumps(c, separators=(',', ':'))}")
        if t.get("errors"):
            L.append("errors: " + "; ".join(sq(e, 200) for e in t["errors"]))
        n = 0
        for key in ("featured_awards", "featured_modifications", "featured_cuts"):
            rows = [a for a in t.get(key) or [] if keep(a)]
            L.append(f"-- {key} ({len(rows)}) --")
            for a in rows:
                n += 1
                L.append(award_line(a, f"A{n}"))
        rc = t.get("routine_counts_by_agency") or {}
        L.append("routine_counts_by_agency (listed by script in 'USAspending: Every Other New Award'): "
                 + "; ".join(f"{k}: {v}" for k, v in rc.items()))
        if t.get("executive_order_citations"):
            L.append(f"executive_order_citations: {json.dumps(t['executive_order_citations'])}")
    s = load_json("data/usaspending_small_triage.json", None)
    L.append("")
    L.append("-- UNDER $2 MILLION (data/usaspending_small_triage.json) --")
    sc = (s or {}).get("counts") or {}
    if not s or (sc.get("batches", 0) > 0 and sc.get("screened_ok", 0) == 0):
        L.append("!!! under-$2M screen failed or missing: say so in known_gaps.")
    else:
        L.append(f"counts: {json.dumps(sc, separators=(',', ':'))} | floor_usd {s.get('floor_usd')} | "
                 f"flagged_overflow {s.get('flagged_overflow')}")
        for k, a in enumerate([a for a in s.get("flagged") or [] if keep(a)]):
            L.append(award_line(a, f"SMALL{k + 1}"))
        if s.get("executive_order_citations"):
            L.append(f"executive_order_citations: {json.dumps(s['executive_order_citations'])}")
        rc = s.get("routine_counts_by_agency") or {}
        if rc:
            L.append("routine_counts_by_agency: " + "; ".join(f"{k}: {v}" for k, v in rc.items()))
    if moved["white_house"] or moved["dc"]:
        L.insert(1, f"NOTE: {moved['white_house']} awards/modifications/cuts tied to the White House and {moved['dc']} tied to "
                    "Washington, D.C. were moved to the White House and D.C. parts of this input; they are not listed here, "
                    "but they ARE counted in `counts` below.")
    return L


BUCKETS = [("white_house", "WHITE HOUSE & EXECUTIVE OFFICE OF THE PRESIDENT", "W"),
           ("dc", "WASHINGTON, D.C.", "D")]


def bucket_section(title, tag, rows):
    """One topic part of the input: every tagged item in its normal record
    format, grouped by what it came from, each labelled with its home section
    and the text that tagged it (`topic_hit`)."""
    L = [f"=== {title} ({len(rows)} items tagged by a keyword/agency match; judge each, see TASK) ==="]
    if not rows:
        return L + ["(none today)"]
    groups = [("J", "SOLE-SOURCE JUSTIFICATIONS", "Sole-Source Justifications / Extensions")] + \
             [(ty, label.upper(), label) for ty, label in TYPES] + [("A", "USASPENDING AWARDS, MODIFICATIONS AND CUTS", "USAspending Contracts")]
    for kind, head, home in groups:
        sub = [(x, hit) for k, x, hit in rows if k == kind]
        if not sub:
            continue
        L += ["", f"-- {head} (home section: {home}) --"]
        for i, (x, hit) in enumerate(sub, 1):
            label = f"{tag}{kind}{i}"
            if kind == "J":
                rec = justification(x, f"{tag}{i}", True)
            elif kind == "A":
                rec = [award_line(x, label)]
            else:
                rec = notice(x, label)
            rec[0] += f" | topic_hit: \"{sq(hit, 160)}\""
            L += rec
    return L


def main():
    today = today_la()
    d = date.fromisoformat(today)
    L = [f"=== TODAY'S INPUT (built by scripts/build_feds_input.py, {today}, {run_mode().upper()} edition) ===",
         f"TODAY = {today} ({d.strftime('%A, %B')} {d.day}, {d.year}).", ""]
    errs = []
    for f, host in (("omb_candidates.json", "OMB"), ("usaspending_candidates.json", "USAspending"), ("sam_candidates.json", "SAM.gov")):
        x = load_json(f"data/{f}", {}) or {}
        if x.get("reachable") is False:
            errs.append(f"{host} unreachable")
        errs += [f"{host}: {sq(e, 200)}" for e in (x.get("errors") or [])][:5]
    L.append("Source errors/gaps: " + ("; ".join(errs) or "none"))

    mech = load_json("content-mechanical.json", {}) or {}
    L += ["", "=== OMB APPORTIONMENTS (written by the cheap-model pass; a script inserts it -- don't write it; never "
              "Top Story) ==="]
    for sec in mech.get("sections") or []:
        L.append(f"{sq(re.sub('<[^>]+>', '', sec.get('headline') or ''))}. "
                 + " ".join(sq(re.sub('<[^>]+>', '', p)) for p in sec.get("paragraphs") or []))
    if not mech.get("sections"):
        L.append("(the OMB pass produced nothing usable today: say in known_gaps that OMB Apportionments is missing)")
    if mech.get("known_gaps"):
        L.append("OMB known gaps: " + mech["known_gaps"])

    buckets = {"white_house": [], "dc": []}
    t = load_json("data/sam_triage.json", None)
    if t and (t.get("batches", 0) == 0 or t.get("screened_ok", 0) > 0):
        t, sam_buckets = split_sam(t)
        for k in buckets:
            buckets[k] += sam_buckets[k]
        sam_lines = sam_section(t)
    else:
        sam_lines = sam_fallback(load_json("data/sam_candidates.json", {}) or {})
    us_lines = usaspending(buckets)

    for key, title, tag in BUCKETS:
        L += [""] + bucket_section(title, tag, buckets[key])
    L += [""] + sam_lines
    L += ["", "=== USASPENDING CONTRACTS ==="] + us_lines
    L += ["", "=== DETERMINISTIC EXTRAS (assemble.py appends these sections after you; do not copy them) ==="]
    L += extras_digest("feds")
    with open(OUT_TXT, "w") as f:
        f.write("\n".join(L) + "\n")
    with open(OUT_JSON, "w") as f:
        json.dump({"today": today, "refs": refs}, f, indent=1)
    print(f"build_feds_input: bundle {sum(map(len, L)):,} chars, {len(refs)} link refs", file=sys.stderr)


if __name__ == "__main__":
    main()
