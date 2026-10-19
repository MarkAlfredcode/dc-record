#!/usr/bin/env python3
"""Everything the Campaigns main pass used to fetch itself, done here with no
LLM (added 2026-10-12). The model used to spend 21-37 tool turns a weekday
run ($0.70-1.24) calling the FEC filings API, downloading and pdftotext-ing
RFAI/Form 99 PDFs, looking up incumbency and rewriting the dedupe manifest;
every turn re-sent the whole conversation. Now it gets one text bundle on
stdin and writes the edition.

1. FRQ (RFAI letters) and F99 filings received since LOOKBACK (TODAY - 3).
2. Dedupe: drops a filing whose `img:<beginning_image_number>` key (or a
   pre-2026-10-10 `file_number` / `sub:<sub_id>` key) is in
   data/fec-filings-seen.json, or whose PDF link was shown in an earlier
   edition of this AM/PM mode (data/surfaced-links.json).
3. Downloads + pdftotext every surviving FRQ and non-termination F99 (no
   cap), and greps the full text for the red-flag keyword list (replaces
   fetch_keyword_scan.py + the Haiku keyword handoff).
4. Sitting lawmakers: a committee in data/member-committees.json (every
   current member's principal committees, any cycle -- also catches a House
   member running for Senate), or a candidate the FEC marks "Incumbent"
   (cached in data/incumbent-candidates-manifest.json; committee ->
   candidate ids cached in data/committee-candidates.json).
5. Voided & negative transactions (data/negative_amounts_candidates.json,
   written by fetch_negative_amounts.py) and the extras summaries.
6. NASA APOD -> data/apod.json (assemble.py adds it; no model tokens).

RFAI letters are mostly fixed frame text (preliminary-review intro, "you
will not receive an additional notice", amendment/contact paragraphs).
Only the header (date, treasurer, report referenced, due date) and the
numbered items are kept; any other line that appears in 40%+ of today's
letters is dropped as template.

Output: data/campaigns_input.txt (appended to prompt-campaigns.txt on
stdin) and data/campaigns_candidates.json (the filing list, for
mark_campaigns_seen.py).
"""
import difflib
import html
import json
import os
import re
import sys
import tempfile
from collections import Counter
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import surfaced  # noqa: E402
from dc_common import (  # noqa: E402
    check_reachable, download, http_get, load_manifest, pdftotext, save_manifest, today_la,
)
from dc_extras import fec_get, fec_key_cycle, load_extras, load_json, lookback, run_mode  # noqa: E402

OUT_TXT = "data/campaigns_input.txt"
OUT_JSON = "data/campaigns_candidates.json"
APOD_PATH = "data/apod.json"
SEEN_PATH = "data/fec-filings-seen.json"
INCUMBENT_PATH = "data/incumbent-candidates-manifest.json"
COMMITTEE_CAND_PATH = "data/committee-candidates.json"
MEMBER_PATH = "data/member-committees.json"
NEGATIVE_PATH = "data/negative_amounts_candidates.json"
MAX_DOC_CHARS = 60_000

KEYWORD_RE = re.compile(
    r"fraud|fraudulent|embezzl|misappropriat|theft|steal|stolen"
    r"|forg(?:e|ed|ery)|unauthorized use|restitution|indictment|subpoena"
    r"|grand jury|kickback|bribe|bribery|money launder|criminal referral"
    r"|department of justice|whistleblower|ponzi|scheme", re.I)
RFAI_ITEMS_START = re.compile(r"(?:needed|requested) for the following|following \d+ items?\b", re.I)
RFAI_ITEMS_END = re.compile(r"please note, you will not receive|adequate responses must be received|"
                            r"electronic filers must file amendments|^\s*sincerely", re.I)
RFAI_HEADER_END = re.compile(r"^\s*dear (?:treasurer|sir|madam|candidate)", re.I)
HEADER_JUNK = re.compile(r"^\s*(federal election commission|washington, d\.?c\.?|rq-\d+)\s*$", re.I)
PAGE_JUNK = re.compile(r"^\s*(image#\s*\d+|page \d+ of \d+)\s*$", re.I)
F99_HEAD, F99_TAIL = 16_000, 4_000


def keys_for(f):
    out = [f"img:{f.get('beginning_image_number')}"]
    if f.get("file_number"):
        out.append(str(f["file_number"]))
    if f.get("sub_id"):
        out.append(f"sub:{f['sub_id']}")
    return out


def fetch_filings(form_type, since, keys):
    out, page = [], 1
    while True:
        data = fec_get("filings/", {"form_type": form_type, "sort": "-receipt_date", "min_receipt_date": since,
                                    "per_page": 100, "page": page}, keys)
        out.extend(data.get("results", []))
        if page >= (data.get("pagination") or {}).get("pages", 1) or not data.get("results"):
            return out
        page += 1


def is_termination(f):
    return "termination" in (f.get("document_description") or "").lower()


def squash(text):
    lines = [" ".join(l.split()) for l in (text or "").split("\n")]
    lines = [l for l in lines if not PAGE_JUNK.match(l)]
    out = []
    for l in lines:  # collapse runs of blank lines
        if l or (out and out[-1]):
            out.append(l)
    return "\n".join(out).strip()


def rfai_core(text):
    """Header + numbered items of an RFAI letter, or None if the anchors
    aren't there (then the whole letter is kept)."""
    lines = text.split("\n")
    start = next((i for i, l in enumerate(lines) if RFAI_ITEMS_START.search(l)), None)
    if start is None:
        return None
    end = next((i for i in range(start + 1, len(lines)) if RFAI_ITEMS_END.search(lines[i])), len(lines))
    hdr_end = next((i for i, l in enumerate(lines[:start]) if RFAI_HEADER_END.search(l)), start)
    header = [l for l in lines[:hdr_end] if l and not HEADER_JUNK.match(l)]
    # The sentence the count sits in: "...is needed for the following 3 item(s):"
    m = re.search(r"following (\d+) item", lines[start], re.I)
    count = f"[The letter asks about {m.group(1)} item(s):]" if m else lines[start]
    return "\n".join(header + [count] + lines[start + 1:end]).strip()


SENT_SPLIT = re.compile(r"(?<=[.;:)])\s+(?=[A-Z(\"])")


def reflow(text):
    """Joins each paragraph's wrapped lines (pdftotext -layout wraps the
    same FEC sentence differently in every letter) and puts one sentence per
    line, so repeated template sentences can be matched."""
    out = []
    for para in re.split(r"\n\s*\n", text):
        lines = [l for l in para.split("\n") if l.strip()]
        if not lines:
            continue
        # Header blocks (addresses, dates) are short lines: keep them as is.
        if sum(len(l) for l in lines) / len(lines) < 45:
            out.append("\n".join(lines))
            continue
        out.append("\n".join(SENT_SPLIT.split(" ".join(lines))))
    return "\n\n".join(out)


def point_back(entries, min_letters=2):
    """entries: [(n, body)] in the order they are printed, bodies reflowed to
    one sentence per line. A long sentence found in 2+ letters is
    FEC item template (e.g. how to redesignate general-election money): it
    is printed in full the first time, and later runs of such sentences
    become a pointer to that letter, so each letter keeps its own specifics
    (amounts, names, dates) without repeating the legal explanation."""
    df = Counter()
    for _, b in entries:
        df.update({l.lower() for l in b.split("\n") if len(l) > 50})
    first, out = {}, []
    for n, b in entries:
        keep, run = [], []
        for l in b.split("\n") + [None]:
            k = l.lower() if l is not None else None
            if k is not None and df.get(k, 0) >= min_letters and first.get(k, n) != n:
                run.append(first[k])
                continue
            if run:
                refs = ", ".join(f"[{x}]" for x in sorted(set(run)))
                keep.append(f"[... {len(run)} sentence(s) of standard FEC item text, printed in full in letter {refs} ...]")
                run = []
            if l is not None:
                if df.get(k, 0) >= min_letters:
                    first.setdefault(k, n)
                keep.append(l)
        out.append("\n".join(keep))
    return out


def drop_template_lines(texts):
    """Lines (26+ chars) found in 40%+ of the letters (and at least 4) are
    letter template; returns (texts without them, sorted template lines)."""
    if len(texts) < 4:
        return texts, []
    df = Counter()
    for t in texts:
        df.update({l.lower() for l in t.split("\n") if len(l) > 25})
    floor = max(4, int(0.4 * len(texts) + 0.999))
    common = {l for l, n in df.items() if n >= floor}
    if not common:
        return texts, []
    out = ["\n".join(l for l in t.split("\n") if l.lower() not in common) for t in texts]
    return out, sorted(common)


def committee_candidates(cids, keys, errors):
    """{committee_id: [candidate_id]} with a persistent cache."""
    cache = load_manifest(COMMITTEE_CAND_PATH)
    for cid in cids:
        if cid in cache:
            continue
        try:
            data = fec_get(f"committee/{cid}/candidates/", {"per_page": 20}, keys)
            cache[cid] = sorted({r["candidate_id"] for r in data.get("results", []) if r.get("candidate_id")})
        except Exception as e:  # noqa: BLE001
            errors.append(f"candidate lookup for {cid} failed: {e}")
    save_manifest(COMMITTEE_CAND_PATH, cache)
    return cache


def incumbents(cand_ids, keys, today, errors):
    man = load_manifest(INCUMBENT_PATH)
    todo = sorted(c for c in cand_ids if c not in man)
    for i in range(0, len(todo), 50):
        try:
            data = fec_get("candidates/", {"candidate_id": todo[i:i + 50], "per_page": 100}, keys)
            for r in data.get("results", []):
                man[r["candidate_id"]] = {"name": r.get("name"), "office": r.get("office"), "state": r.get("state"),
                                          "party": r.get("party"),
                                          "incumbent_challenge_full": r.get("incumbent_challenge_full"),
                                          "checked": today}
        except Exception as e:  # noqa: BLE001
            errors.append(f"candidate batch lookup failed: {e}")
    save_manifest(INCUMBENT_PATH, man)
    return man


def _apod_page(errors):
    """NASA moved APOD to science.nasa.gov/apod in 2026 and api.nasa.gov's
    planetary/apod now returns the NASA logo as the image (and often a 500),
    so read the page itself: title, hero image, explanation, credit."""
    status, body = http_get("https://science.nasa.gov/apod/", timeout=25)
    if status >= 400:
        errors.append(f"APOD page HTTP {status}")
        return None
    page = body.decode("utf-8", "replace") if isinstance(body, bytes) else body
    img = re.search(r"https://assets\.science\.nasa\.gov/dynamicimage/assets/science/cds/apod/apod/[^\"'?\s]+\.(?:jpe?g|png)", page)
    title = re.search(r'<h2 class="display-48[^>]*>([^<]+)</h2>', page)
    expl = re.search(r"<strong>Explanation:</strong>(.*?)(?:<br\s*/?>\s*<br|</p>)", page, re.S)
    credit = re.search(r"Credit:</th>\s*<td[^>]*>(.*?)</td>", page, re.S)
    if not (img and title and expl):
        errors.append("APOD page layout changed (image/title/explanation not found)")
        return None
    plain = lambda h: " ".join(html.unescape(re.sub(r"<[^>]+>", "", h)).split())
    text = re.sub(r"^Explanation\s*:\s*", "", plain(expl.group(1)))
    sentences = re.split(r"(?<=[.!?])\s+", text)
    return {"title": html.unescape(title.group(1)).strip(), "image_url": img.group(0) + "?w=1024",
            "caption": html.escape(" ".join(sentences[:2]), quote=False),
            "credit": (plain(credit.group(1)) + " / " if credit else "") + "science.nasa.gov/apod"}


def apod(errors):
    pic = _apod_page(errors)
    if pic:
        return pic
    status, body = http_get("https://api.nasa.gov/planetary/apod?api_key=DEMO_KEY", timeout=20)
    if status >= 400:
        errors.append(f"APOD HTTP {status} (omitted)")
        return None
    d = json.loads(body)
    url = d.get("url") or d.get("hdurl") or ""
    if d.get("media_type") != "image" or "nasa-logo" in url:  # the API's broken-scraper placeholder
        return None
    sentences = re.split(r"(?<=[.!?])\s+", " ".join((d.get("explanation") or "").split()))
    caption = html.escape(" ".join(sentences[:2]), quote=False)
    credit = " ".join((d.get("copyright") or "").split())
    return {"title": d.get("title"), "image_url": url, "caption": caption,
            "credit": (credit + " / " if credit else "") + "apod.nasa.gov"}


def extras_digest(edition, max_chars=1500):
    """Each extras file's summary plus the start of its sections as plain
    text (links shown as "text <url>"), so the one-turn pass can weigh them
    for Top Story without a Read tool."""
    tag = re.compile(r"<[^>]+>")
    href = re.compile(r"""<a [^>]*href=["']([^"']+)["'][^>]*>(.*?)</a>""", re.S)

    def plain(x):
        return html.unescape(tag.sub("", href.sub(lambda m: f"{m.group(2)} <{m.group(1)}>", x or "")))

    out = []
    for ex in load_extras(edition):
        out.append(f"* {ex.get('name')}: {ex.get('summary')}")
        bits = []
        for sec in ex.get("sections") or []:
            bits.append(f"[{sec.get('eyebrow')}] {plain(sec.get('headline'))}. " + " ".join(plain(p) for p in sec.get("paragraphs") or []))
            for b in sec.get("blocks") or []:
                for it in (b.get("items") or [])[:8]:
                    bits.append("- " + plain(it))
                for row in (b.get("rows") or [])[:8]:
                    bits.append("- " + " | ".join(plain(c) for c in row))
                if b.get("type") in ("note", "paragraph", "card"):
                    bits.append(plain(b.get("title", "") + " " + b.get("text", "")))
        txt = "\n".join(" ".join(x.split()) for x in bits if x.strip())
        if txt:
            out.append(txt[:max_chars] + (" [...]" if len(txt) > max_chars else ""))
    return out


def money(v):
    try:
        return f"-${abs(float(v)):,.2f}" if float(v) < 0 else f"${float(v):,.2f}"
    except (TypeError, ValueError):
        return str(v)


def main():
    today, mode = today_la(), run_mode()
    since = lookback(3)
    errors = []
    out = {"today": today, "mode": mode, "lookback": since, "reachable": True, "filings": [], "errors": errors}
    if not (check_reachable("api.open.fec.gov") or check_reachable("docquery.fec.gov")):
        out["reachable"] = False
        errors.append("api.open.fec.gov and docquery.fec.gov both unreachable")
        with open(OUT_JSON, "w") as f:
            json.dump(out, f, indent=1)
        with open(OUT_TXT, "w") as f:
            f.write("FEC UNREACHABLE: api.open.fec.gov and docquery.fec.gov both failed to connect.\n")
        print("fetch_campaigns: FEC unreachable", file=sys.stderr)
        return
    keys = fec_key_cycle()

    raw = []
    for ft in ("FRQ", "F99"):
        try:
            raw.extend(fetch_filings(ft, since, keys))
        except Exception as e:  # noqa: BLE001
            errors.append(f"{ft} filings fetch failed: {e}")
    seen = load_manifest(SEEN_PATH)
    prior = surfaced.prior("campaigns")
    filings, n_seen, n_prior, uniq = [], 0, 0, set()
    for f in raw:
        k = keys_for(f)[0]
        if k in uniq:
            continue
        uniq.add(k)
        if any(x in seen for x in keys_for(f)):
            n_seen += 1
        elif surfaced.norm(f.get("pdf_url")) in prior:
            n_prior += 1
        else:
            filings.append(f)

    # Full text of every RFAI and non-termination Form 99.
    texts = {}
    with tempfile.TemporaryDirectory() as tmp:
        for f in filings:
            if is_termination(f) or not f.get("pdf_url"):
                continue
            k = keys_for(f)[0]
            try:
                p = os.path.join(tmp, k.replace(":", "_") + ".pdf")
                download(f["pdf_url"], p, timeout=45)
                texts[k] = squash(pdftotext(p))
            except Exception as e:  # noqa: BLE001
                errors.append(f"{f.get('committee_name')} {f['pdf_url']}: download/extract failed: {e}")
                texts[k] = ""

    # Sitting lawmakers / candidate identities.
    members = (load_json(MEMBER_PATH, {}) or {}).get("committees", {})
    cand_cids = sorted({f["committee_id"] for f in filings if f.get("office") in ("H", "S", "P")
                        and not f.get("candidate_id")})
    c2c = committee_candidates(cand_cids, keys, errors) if cand_cids else {}
    cand_ids = {f["candidate_id"] for f in filings if f.get("candidate_id")}
    for cid in cand_cids:
        cand_ids.update(c2c.get(cid) or [])
    man = incumbents(cand_ids, keys, today, errors) if cand_ids else load_manifest(INCUMBENT_PATH)

    recs = []
    for f in filings:
        k = keys_for(f)[0]
        cands = [f["candidate_id"]] if f.get("candidate_id") else (c2c.get(f.get("committee_id")) or [])
        cinfo = [man[c] | {"id": c} for c in cands if c in man]
        sitting = members.get(f.get("committee_id"))
        if not sitting and f.get("office") in ("H", "S"):
            inc = next((c for c in cinfo if c.get("incumbent_challenge_full") == "Incumbent"), None)
            if inc:
                sitting = f"{inc['name']} ({(inc.get('party') or '?')[:1]}-{inc.get('state')}, {inc.get('office')})"
        text = texts.get(k, "")
        kw = sorted({m.group(0).lower() for m in KEYWORD_RE.finditer(text)})
        recs.append({
            "key": k, "form": f.get("form_type"), "committee": f.get("committee_name"),
            "committee_id": f.get("committee_id"), "office": f.get("office"),
            "committee_type": f.get("committee_type"), "description": f.get("document_description"),
            "received": (f.get("receipt_date") or "")[:10], "pages": f.get("pages"), "pdf_url": f.get("pdf_url"),
            "report": " ".join(x for x in [f.get("report_type_full"), str(f.get("report_year") or "")] if x).strip(),
            "sitting": sitting, "candidates": cinfo, "termination": is_termination(f),
            "keywords": kw, "text": text,
        })
    out["filings"] = [{k: v for k, v in r.items() if k != "text"} for r in recs]

    # Trim RFAI letters to header + items, then drop cross-letter template.
    letters = [r for r in recs if r["form"] == "FRQ" and r["text"]]
    for r in letters:
        r["body"] = reflow(rfai_core(r["text"]) or r["text"])
    bodies, template = drop_template_lines([r["body"] for r in letters])
    for r, b in zip(letters, bodies):
        r["body"] = b
    for r in recs:
        r.setdefault("body", r["text"])

    neg = load_json(NEGATIVE_PATH, {}) or {}
    pic = apod(errors)
    with open(APOD_PATH, "w") as f:
        json.dump({"date": today, "apod": pic}, f, indent=1)

    # ---- the bundle ----
    d = date.fromisoformat(today)
    L = [f"=== TODAY'S INPUT (built by scripts/fetch_campaigns.py at {today}, {mode.upper()} edition) ===",
         f"TODAY = {today} ({d.strftime('%A, %B')} {d.day}, {d.year}). LOOKBACK = {since}.",
         f"Window: FEC FRQ (RFAI) and F99 filings received {since} onward. In window: {len(uniq)}; "
         f"already reported in a prior edition: {n_seen}; shown in an earlier {mode.upper()} edition: {n_prior}; "
         f"new below: {len(recs)}.",
         "Errors/gaps: " + ("; ".join(errors + list(neg.get("errors") or [])) or "none"), ""]

    flagged = [r for r in recs if r["keywords"]]
    L.append(f"--- RED-FLAG KEYWORD HITS ({len(flagged)}) ---")
    for r in flagged:
        snip = []
        for kw in r["keywords"][:4]:
            m = re.search(re.escape(kw), r["text"], re.I)
            if m:
                snip.append(f"[{kw}] …{' '.join(r['text'][max(0, m.start() - 120):m.end() + 120].split())}…")
        L.append(f"{r['committee']} ({r['form']}; see item below): " + " | ".join(snip))
    L.append("")

    rows = neg.get("rows") or []
    L.append(f"--- VOIDED & NEGATIVE TRANSACTIONS ({len(rows)} new rows, $10,000+ negative) ---")
    refs = {}
    for i, x in enumerate(rows, 1):
        refs[f"V{i}"] = x.get("pdf_url")
        L.append(f"[V{i}] Sched {x.get('schedule')} | {x.get('committee_name')} ({x.get('committee_id')}) | {money(x.get('amount'))}"
                 f" | counterparty: {x.get('counterparty')} | {x.get('date', '')[:10]} | \"{x.get('description') or ''}\""
                 + (f" | KEYWORDS: {', '.join(x['matched_keywords'])}" if x.get("matched_keywords") else ""))
    L.append("")

    def head(r):
        bits = [f"{r['committee']} ({r['committee_id']})"]
        bits.append({"H": "House candidate", "S": "Senate candidate", "P": "presidential candidate"}.get(
            r["office"], f"PAC/party (type {r['committee_type']})"))
        if r["sitting"]:
            bits.append(f"SITTING LAWMAKER: {r['sitting']}")
        for c in r["candidates"][:2]:
            bits.append(f"candidate {c.get('name')} ({c.get('party')}, {c.get('office')}-{c.get('state')}, "
                        f"{c.get('incumbent_challenge_full')})")
        bits += [r["description"] or "", f"received {r['received']}", f"{r['pages']} pp"]
        if r["keywords"]:
            bits.append("KEYWORDS: " + ", ".join(r["keywords"]))
        return " | ".join(b for b in bits if b)

    def body(r):
        b = r["body"]
        if not b:
            return "(no text layer -- pdftotext returned nothing; describe from the filing record only and say so)"
        if r["form"] == "F99" and len(b) > F99_HEAD + F99_TAIL:
            # Long Form 99 attachments are bank loan agreements (C-1) and
            # similar legal documents; the keyword scan above read all of it.
            return (b[:F99_HEAD] + f"\n[... {len(b) - F99_HEAD - F99_TAIL:,} characters omitted from the middle; "
                    f"red-flag keyword scan covered the full text ...]\n" + b[-F99_TAIL:])
        return b if len(b) <= MAX_DOC_CHARS else b[:MAX_DOC_CHARS] + f"\n[... truncated at {MAX_DOC_CHARS:,} of {len(b):,} characters]"

    order = lambda r: (not r["sitting"], not r["keywords"], r["committee"] or "")  # noqa: E731
    frq = sorted([r for r in recs if r["form"] == "FRQ"], key=order)
    f99 = sorted([r for r in recs if r["form"] == "F99" and not r["termination"]], key=order)
    terms = sorted([r for r in recs if r["termination"]], key=lambda r: r["committee"] or "")
    num = {id(r): i + 1 for i, r in enumerate(frq + f99 + terms)}
    for r in frq + f99 + terms:
        refs[str(num[id(r)])] = r["pdf_url"]
        r["n"] = num[id(r)]
    for r, b in zip(frq, point_back([(num[id(r)], r["body"]) for r in frq])):
        r["body"] = b
    L.append(f"--- RFAI LETTERS (form FRQ) -- header and numbered items; frame text removed ({len(frq)}) ---")
    for r in frq:
        L += [f"[{num[id(r)]}] {head(r)}", body(r), ""]
    L.append(f"--- FORM 99 (non-termination) -- full text ({len(f99)}) ---")
    printed = []  # (signature, number) of each Form 99 printed in full
    for r in f99:
        sig = re.sub(r"[\d\s]+", " ", r["body"]).lower()[:6000]
        twin = next((m for s2, m in printed if sig and difflib.SequenceMatcher(None, sig, s2).quick_ratio() > 0.97
                     and difflib.SequenceMatcher(None, sig, s2).ratio() > 0.95), None)
        if twin:
            L += [f"[{num[id(r)]}] {head(r)}", f"(same document as [{twin}] apart from numbers/image ids; "
                  f"compare dollar figures in the PDF if they matter)", ""]
            continue
        printed.append((sig, num[id(r)]))
        L += [f"[{num[id(r)]}] {head(r)}", body(r), ""]
    L.append(f"--- TERMINATIONS ({len(terms)}; not read) ---")
    for r in terms:
        L.append(f"[{r['n']}] {r['committee']} ({r['committee_id']}) | {r['description']} | received {r['received']}"
                 + (f" | SITTING LAWMAKER: {r['sitting']}" if r["sitting"] else ""))
    L.append("")
    if template:
        L.append(f"--- TEMPLATE LINES removed from the RFAI letters above (each appeared in 40%+ of today's {len(letters)} letters) ---")
        L += template
        L.append("")

    L.append("--- DETERMINISTIC EXTRAS (assemble.py appends these sections after you; do not copy them) ---")
    L += extras_digest("campaigns")
    with open(OUT_TXT, "w") as f:
        f.write("\n".join(L) + "\n")
    out["refs"] = {k: v for k, v in refs.items() if v}
    with open(OUT_JSON, "w") as f:
        json.dump(out, f, indent=1)
    raw_chars = sum(len(r["text"]) for r in recs)
    print(f"fetch_campaigns: {len(recs)} new filings ({len(letters)} RFAIs, {len(terms)} terminations, "
          f"{len(flagged)} keyword hits), {len(rows)} negative rows; letter text {raw_chars:,} -> "
          f"{sum(len(r['body']) for r in recs):,} chars; bundle {sum(map(len, L)):,} chars; errors: {len(errors)}",
          file=sys.stderr)


if __name__ == "__main__":
    main()
