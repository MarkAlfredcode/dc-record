#!/usr/bin/env python3
"""Everything the Officials main pass used to fetch itself, done here with no
LLM (added 2026-10-12). The model used to run 7-16 tool turns a run:
FARA report pages, LDA API pages, the House Clerk index zip, PTR PDFs
(capped at 30 per edition), and the Senate eFD session flow. Now:

- FARA new and terminated registrants (efile.fara.gov report pages).
- LDA filings posted since YESTERDAY: every new registration (RR) and
  termination in full; activity reports listed when they carry a signal
  (a lobbyist in a covered government position, foreign entities, a
  conviction disclosure, $100K+ income/expenses), the rest counted. Q3
  reports (due Oct 20) run to 500+ a day.
- House and Senate financial disclosures filed since LOOKBACK (TODAY - 3),
  deduped against data/pfd-ptr-seen.json. Every PTR is parsed (no cap):
  House PDFs via trades.parse_house_ptr, Senate pages via parse_senate_ptr.
  A trade disclosed more than 45 days after it happened is marked LATE
  (STOCK Act limit; amended rows excluded). Senate annual/candidate/blind
  trust reports: the detail page text, for the model to summarize.
- The stock-trade tables themselves are built here and appended to the
  section by assemble.py (data/officials_append.json), so the model never
  retypes them; it writes the summary and one line per filing.

Output: data/officials_input.txt (bundle), data/officials_input.json
(refs, counts, items for mark_seen.py), data/officials_append.json.
"""
import html
import json
import os
import re
import sys
import tempfile
import time
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import top_officials  # noqa: E402
import trades  # noqa: E402
from dc_common import check_reachable, days_ago_la, http_get, load_manifest, pdftotext, save_manifest, today_la  # noqa: E402
from dc_extras import load_json, run_mode  # noqa: E402
from fetch_campaigns import extras_digest  # noqa: E402
from fetch_deadlines import senate_href, senate_label, senate_search, senate_session  # noqa: E402
from fetch_stopped_trading import curl_bytes, index_rows  # noqa: E402

OUT_TXT = "data/officials_input.txt"
OUT_JSON = "data/officials_input.json"
APPEND = "data/officials_append.json"
SEEN_PATH = "data/pfd-ptr-seen.json"
FARA = "https://efile.fara.gov/ords/fara/"
LDA = "https://lda.gov/api/v1/filings/"
LDA_MAX_PAGES = 30
LDA_SIGNAL_CAP = 400
LATE_DAYS = 45
DETAIL_CHARS = 6000
TAG = re.compile(r"<[^>]+>")


def plain(s):
    s = re.sub(r"<(style|script)[^>]*>.*?</\1>", " ", s or "", flags=re.S | re.I)
    return " ".join(html.unescape(TAG.sub(" ", s)).split())


# ---------------- FARA ----------------
# DOJ's daily bulk files (added 2026-10-12): the report pages give only a
# registrant's name and number, and the terminated-registrants page now loads
# its rows by AJAX. The bulk files add each registrant's foreign principals
# and countries, new clients of existing agents, and every filed document's
# PDF (scanned: OCR). Exhibit AB is the agreement with the foreign principal.
FARA_BULK = "https://efile.fara.gov/bulk/zip/{}.csv.zip"
FARA_READ_TYPES = ("Exhibit AB", "Registration Statement", "Amendment")
FARA_DOC_CHARS = 3500
FARA_SEEN = "data/fara-seen.json"
FARA_ITEMS = "data/fara_items.json"
# Top-official scan (added 2026-10-12): every supplemental statement (Item 12
# logs each text, call, email and meeting with a U.S. official), amendment,
# agreement, registration statement and informational-materials filing stamped
# in the last FARA_SCAN_DAYS is read once and searched for the President, the
# Vice President and the cabinet (scripts/top_officials.py). Results are kept
# in TOP_CACHE so no filing is OCR'd twice; filings with hits stay in the bundle
# until an edition links them or the model dismisses them (TOP_SEEN). The
# window is wide because DOJ's bulk files trail the stamp date by days.
FARA_SCAN_TYPES = ("Supplemental Statement", "Amendment", "Exhibit AB", "Registration Statement",
                   "Informational Materials")
FARA_SCAN_DAYS = 14
FARA_SCAN_SEC = int(os.environ.get("DC_FARA_SCAN_SEC") or 480)
TOP_CACHE = "data/fara-top-scanned.json"
TOP_SEEN = "data/fara-top-seen.json"
TOP_ITEMS = "data/fara_top_items.json"
LDA_TOP_ENTITIES = {"President of the U.S.": "the President", "Vice President of the U.S.": "the Vice President",
                    "Office of the Vice President of the United States": "the Office of the Vice President"}
LDA_TOP_CAP = 40


def fara_csv(name):
    import csv
    import io
    import zipfile

    z = zipfile.ZipFile(io.BytesIO(curl_bytes(FARA_BULK.format(name))))
    member = next(n for n in z.namelist() if n.endswith(".csv"))
    return list(csv.DictReader(io.StringIO(z.read(member).decode("utf-8", "replace"))))


def fara_date(s):
    d = trades.parse_date(s)
    return d.isoformat() if d else ""


FARA_MD = {}   # url -> doc_metadata findings (news >= 4: AI-tool fingerprints, tracked changes, comments)


def fara_doc_text(url, tmp, deadline=None, info=None):
    """Text of a filing (OCR for scanned pages). info, if given, gets
    sam_reader's page counts (unread_pages > 0: the deadline cut it short)."""
    import sam_reader
    p = os.path.join(tmp, re.sub(r"\W", "_", url[-60:]) + ".pdf")
    with open(p, "wb") as fh:
        fh.write(curl_bytes(url))
    text, got = sam_reader.read_pdf(p, deadline) if os.path.getsize(p) else ("", {})
    if info is not None:
        info.update(got)
    FARA_MD[url] = [{k: f[k] for k in ("news", "finding", "quote")} for f in got.get("md_flags") or [] if f["news"] >= 4]
    text = re.sub(r"[ \t]+", " ", text)
    i = text.find("1. Name of Registrant")
    return text[i:] if i > 0 else text


def fara_top(docs, texts, refs, errors, tmp):
    """Scans recent FARA filings for the President, VP and cabinet. Returns
    (bundle lines, number of filings with unreported hits)."""
    today = today_la()
    since = days_ago_la(FARA_SCAN_DAYS)
    cache = {u: v for u, v in load_manifest(TOP_CACHE).items() if v.get("stamped", "") >= days_ago_la(FARA_SCAN_DAYS + 7)}
    seen = load_manifest(TOP_SEEN)
    todo = [d for d in docs if d["Document Type"] in FARA_SCAN_TYPES and fara_date(d["Date Stamped"]) >= since
            and d["URL"] not in cache]
    # Contact logs first, informational materials (often long brochures) last.
    todo.sort(key=lambda d: (FARA_SCAN_TYPES.index(d["Document Type"]), fara_date(d["Date Stamped"])))
    deadline, unread, failed = time.time() + FARA_SCAN_SEC, 0, 0
    for d in todo:
        url = d["URL"]
        if time.time() > deadline:
            unread += 1
            continue
        info = {}
        try:
            text = texts[url] if url in texts else fara_doc_text(url, tmp, deadline, info)
        except Exception as e:  # noqa: BLE001
            failed += 1
            errors.append(f"FARA top-official scan, {url}: {e}")
            continue
        if info.get("unread_pages") and time.time() > deadline:
            unread += 1  # cut off mid-document: read it whole next run
            continue
        hits, more = top_officials.summarize(top_officials.scan(text))
        cache[url] = {"scanned": today, "stamped": fara_date(d["Date Stamped"]), "type": d["Document Type"],
                      "registrant": d["Registrant Name"].strip(), "num": d["Registration Number"],
                      "fp": f"{d['Foreign Principal Name']} ({d['Foreign Principal Country']})" if d["Foreign Principal Name"] else "",
                      "hits": [{k: h[k] for k in ("who", "role", "tier", "method", "snippet")} for h in hits],
                      "more": more, "md": FARA_MD.get(url) or []}
        if len(cache) % 10 == 0:
            save_manifest(TOP_CACHE, cache)  # keep finished OCR if a later step dies
    save_manifest(TOP_CACHE, cache)
    if unread:
        errors.append(f"FARA top-official scan: {unread} recent filings not read yet (time budget); the next run reads them")
    pending = sorted(((u, v) for u, v in cache.items() if (v["hits"] or v.get("md")) and u not in seen and v["stamped"] >= since),
                     key=lambda x: (x[1]["stamped"], x[1]["registrant"]))
    L, items = [], []
    for url, v in pending:
        ref = f"T{len(refs) + 1}"
        refs[ref] = url
        items.append({"key": url, "url": url, "ref": ref})
        fp = f" | foreign principal: {v['fp']}" if v["fp"] else ""
        L.append(f"[{ref}] FARA {v['type']}: {v['registrant']} (#{v['num']}) | stamped {v['stamped']}{fp}")
        for h in v["hits"]:
            L.append(f"    - {h['who']} [{h['tier']}; {h['role']}] method: {h['method'] or 'not stated'} :: {h['snippet']}")
        if v.get("more"):
            L.append(f"    (+{v['more']} more lines naming a top official in this filing, not shown)")
        for m in v.get("md") or []:
            L.append(f"    - FILE METADATA/LINKS (not body text; no official needs to be named) [news {m['news']}]: {m['finding']} :: \"{m['quote']}\"")
    json.dump({"items": items}, open(TOP_ITEMS, "w"), indent=1)
    scanned = sum(1 for v in cache.values() if v.get("scanned") == today)
    print(f"fara_top: {len(todo) - unread - failed} read this run ({scanned} cached today), {len(pending)} with "
          f"unreported hits, {unread} left for next run", file=sys.stderr)
    return L, len(pending)


def fara(since, refs, items, errors):
    """Returns (bundle lines, count of new items)."""
    regs = fara_csv("FARA_All_Registrants")
    fps = fara_csv("FARA_All_ForeignPrincipals")
    docs = fara_csv("FARA_All_RegistrantDocs")
    seen = load_manifest(FARA_SEEN)
    by_reg = {}
    for f in fps:
        by_reg.setdefault(f["Registration Number"], []).append(f)
    page = lambda num: f"{FARA}f?p=1381:200::::RP,200:P200_REG_NUMBER:{num}"  # noqa: E731

    def new(key, url):
        if key in seen:
            return None
        ref = f"F{len(refs) + 1}"
        refs[ref] = url
        items.append({"key": key, "url": url})
        return ref

    L, n = [], 0
    fp_line = lambda f: f"{f['Foreign Principal']} ({f['Country/Location Represented'].strip()})"  # noqa: E731
    for r in regs:
        num = r["Registration Number"]
        if fara_date(r["Registration Date"]) >= since and (ref := new(f"reg:{num}", page(num))):
            n += 1
            L.append(f"[{ref}] NEW REGISTRANT: {r['Name'].strip()} | #{num} | registered {r['Registration Date']} | "
                     f"foreign principal(s): " + ("; ".join(fp_line(f) for f in by_reg.get(num, [])) or "none listed yet"))
        if fara_date(r["Termination Date"]) >= since and (ref := new(f"term:{num}", page(num))):
            n += 1
            L.append(f"[{ref}] TERMINATED REGISTRANT: {r['Name'].strip()} | #{num} | terminated {r['Termination Date']}")
    for f in fps:
        num, who = f["Registration Number"], f["Foreign Principal"].strip()
        if fara_date(f["Foreign Principal Registration Date"]) >= since and fara_date(f["Registrant Date"]) < since \
                and (ref := new(f"fp:{num}:{who}", page(num))):
            n += 1
            L.append(f"[{ref}] NEW FOREIGN PRINCIPAL for an existing registrant: {f['Registrant Name'].strip()} (#{num}, "
                     f"registered {f['Registrant Date']}) now represents {fp_line(f)} from {f['Foreign Principal Registration Date']}")
        if fara_date(f["Foreign Principal Termination Date"]) >= since and (ref := new(f"fpterm:{num}:{who}", page(num))):
            n += 1
            L.append(f"[{ref}] FOREIGN PRINCIPAL DROPPED: {f['Registrant Name'].strip()} (#{num}) no longer represents "
                     f"{fp_line(f)}, as of {f['Foreign Principal Termination Date']}")
    recent = [d for d in docs if fara_date(d["Date Stamped"]) >= since and d["URL"] not in seen]
    to_read = [d for d in recent if d["Document Type"] in FARA_READ_TYPES]
    texts = {}
    with tempfile.TemporaryDirectory() as tmp:
        for d in to_read:
            try:
                texts[d["URL"]] = fara_doc_text(d["URL"], tmp)
            except Exception as e:  # noqa: BLE001
                errors.append(f"FARA document {d['URL']}: {e}")
        try:
            top = fara_top(docs, texts, refs, errors, tmp)
        except Exception as e:  # noqa: BLE001
            errors.append(f"FARA top-official scan: {e}")
            top = ([], 0)
    # Form questions repeat in every document: keep only lines unique to one.
    df = {}
    for t in texts.values():
        for line in {" ".join(x.split()).lower() for x in t.split("\n") if len(x.strip()) > 25}:
            df[line] = df.get(line, 0) + 1
    L.append(f"FILED DOCUMENTS stamped {since} onward ({len(recent)}):")
    for d in sorted(recent, key=lambda d: (d["Document Type"] not in FARA_READ_TYPES, d["Document Type"])):
        ref = new(d["URL"], d["URL"])
        if not ref:
            continue
        n += 1
        fp = f" | foreign principal: {d['Foreign Principal Name']} ({d['Foreign Principal Country']})" if d["Foreign Principal Name"] else ""
        sf = f" | short-form filer: {d['Short Form Name']}" if d["Short Form Name"] else ""
        L.append(f"[{ref}] {d['Document Type']}: {d['Registrant Name'].strip()} (#{d['Registration Number']}) | "
                 f"stamped {d['Date Stamped']}{fp}{sf}")
        t = texts.get(d["URL"])
        if t is not None:
            keep = [" ".join(x.split()) for x in t.split("\n") if x.strip()
                    and (len(x.strip()) <= 25 or df.get(" ".join(x.split()).lower(), 0) < 2 or len(texts) < 3)]
            body = " / ".join(keep)
            L.append("    text (OCR, form questions shared with other filings removed): "
                     + (body[:FARA_DOC_CHARS] + (" [...]" if len(body) > FARA_DOC_CHARS else "") if body else "(no text)"))
    json.dump({"items": items}, open(FARA_ITEMS, "w"), indent=1)
    return L, n, top


# ---------------- LDA ----------------
def lda_all(since, errors):
    out, url = [], f"{LDA}?filing_dt_posted_after={since}&page_size=100"
    for _ in range(LDA_MAX_PAGES):
        status, body = http_get(url, timeout=60)
        if status == 429:
            time.sleep(20)
            continue
        if status >= 400:
            errors.append(f"LDA HTTP {status}")
            break
        d = json.loads(body)
        out += d.get("results", [])
        url = d.get("next")
        if not url:
            break
        time.sleep(4)  # anonymous limit is ~15 requests/minute
    else:
        errors.append(f"LDA: stopped after {LDA_MAX_PAGES} pages ({len(out)} filings); the rest are not listed")
    return out


def lda_line(r):
    acts = r.get("lobbying_activities") or []
    issues = sorted({a.get("general_issue_code_display") or "" for a in acts} - {""})
    covered = []
    for a in acts:
        for lb in a.get("lobbyists") or []:
            cp = (lb.get("covered_position") or "").strip()
            if cp:
                p = lb.get("lobbyist") or {}
                covered.append(f"{p.get('first_name', '')} {p.get('last_name', '')} ({cp[:120]})".strip())
    desc = "; ".join(sorted({(a.get("description") or "").strip()[:200] for a in acts} - {""}))[:400]
    foreign = [f.get("name") for f in r.get("foreign_entities") or [] if f.get("name")]
    conv = r.get("conviction_disclosures") or []
    money = r.get("income") or r.get("expenses")
    bits = [f"{(r.get('registrant') or {}).get('name')} -> {(r.get('client') or {}).get('name')}"
            f" ({((r.get('client') or {}).get('general_description') or '')[:80]})",
            r.get("filing_type_display") or r.get("filing_type") or ""]
    if money:
        bits.append(f"${float(money):,.0f} {'income' if r.get('income') else 'expenses'}")
    if issues:
        bits.append("issues: " + ", ".join(issues))
    if desc:
        bits.append("lobbied on: " + desc)
    if covered:
        bits.append("COVERED POSITIONS: " + "; ".join(sorted(set(covered))[:6]))
    if foreign:
        bits.append("FOREIGN ENTITIES: " + ", ".join(foreign[:5]))
    if conv:
        bits.append(f"CONVICTION DISCLOSURES: {len(conv)}")
    if r.get("termination_date"):
        bits.append(f"terminated {r['termination_date']}")
    return " | ".join(bits), bool(covered or foreign or conv or (money and float(money) >= 100000))


# ---------------- disclosures ----------------
def mdy(d):
    return f"{d.month}/{d.day}/{d.year}"


def late_note(t, filed_iso):
    if not t.get("tx_date") or not filed_iso or t.get("status") == "Amended":
        return ""
    n = trades.days_between(t["tx_date"], filed_iso)
    return f"LATE {n}d" if n is not None and n > LATE_DAYS else ""


def company(asset):
    """'Home Depot, Inc. (HD) [ST]' -> 'Home Depot, Inc.'"""
    a = re.sub(r"\s*\[[A-Z]{2}\]?|\s*\[[A-Z]{2}$", "", asset or "")
    a = re.sub(r"\s*\([A-Z][A-Z.\-]{0,6}\)", "", a)
    a = re.sub(r"\s*-?\s*Common Stock$|\s+New Common Stock$", "", a.strip(), flags=re.I)
    return a.strip()[:80]


def is_stock(t):
    at = (t.get("asset_type") or "").upper()
    return at == "ST" or at.startswith("STOCK") or (not at and t.get("ticker"))


def main():
    today, mode = today_la(), run_mode()
    since = days_ago_la(1)
    lookback = days_ago_la(int(os.environ.get("DC_LOOKBACK_DAYS") or 3))
    errors, gaps, refs, items = [], [], {}, []
    n = {"fara": 0, "lda_rr": 0, "lda": 0, "lda_signal": 0, "house": 0, "senate": 0, "top": 0, "lda_top": 0}
    hosts = ["disclosures-clerk.house.gov", "efdsearch.senate.gov", "lda.gov", "efile.fara.gov"]
    down = [h for h in hosts if not check_reachable(h)]
    if len(down) >= 2:
        json.dump({"today": today, "errors": [f"unreachable: {', '.join(down)}"], "new_items": 0, "refs": {}},
                  open(OUT_JSON, "w"), indent=1)
        open(OUT_TXT, "w").write(f"SOURCES UNREACHABLE: {', '.join(down)}\n")
        return
    errors += [f"{h} unreachable" for h in down]
    L_fara, L_lda, L_pfd, tables, L_top, L_lda_top = [], [], [], [], [], []

    # FARA
    if "efile.fara.gov" not in down:
        try:
            fitems = []
            L_fara, n["fara"], (L_top, n["top"]) = fara(lookback, refs, fitems, errors)
        except Exception as e:  # noqa: BLE001
            errors.append(f"FARA bulk data: {e}")

    # LDA
    if "lda.gov" not in down:
        rows = lda_all(since, errors)
        rr = [r for r in rows if r.get("filing_type") == "RR"]
        term = [r for r in rows if r.get("termination_date") and r.get("filing_type") != "RR"]
        rest = [r for r in rows if r not in rr and r not in term]
        n["lda_rr"], n["lda"] = len(rr), len(rows)
        counts = {}
        for r in rows:
            counts[r.get("filing_type_display") or r.get("filing_type")] = counts.get(r.get("filing_type_display") or r.get("filing_type"), 0) + 1
        L_lda.append("All filings posted since " + since + ": " + ", ".join(f"{v} {k}" for k, v in sorted(counts.items(), key=lambda x: -x[1])))

        def add(r, label):
            ref = f"L{len(refs) + 1}"
            refs[ref] = r.get("filing_document_url") or r.get("url")
            line, _ = lda_line(r)
            L_lda.append(f"[{ref}] {label}{line}")

        # Every filing, not just the signal ones: the President or VP listed
        # as an entity lobbied, or a top official named in what was lobbied.
        for r in rows:
            acts = r.get("lobbying_activities") or []
            ents = sorted({LDA_TOP_ENTITIES[g.get("name")] for a in acts for g in a.get("government_entities") or []
                           if g.get("name") in LDA_TOP_ENTITIES})
            desc = " / ".join(sorted({(a.get("description") or "").strip() for a in acts} - {""}))
            named = sorted({h["who"] for h in top_officials.scan(desc, tiers=("name",))})
            if not (ents or named):
                continue
            n["lda_top"] += 1
            if n["lda_top"] > LDA_TOP_CAP:
                continue
            ref = f"L{len(refs) + 1}"
            refs[ref] = r.get("filing_document_url") or r.get("url")
            bits = [f"{(r.get('registrant') or {}).get('name')} -> {(r.get('client') or {}).get('name')}",
                    r.get("filing_type_display") or r.get("filing_type") or ""]
            if ents:
                bits.append("entities lobbied include " + ", ".join(ents))
            if named:
                bits.append("names " + ", ".join(named))
            L_lda_top.append(f"[{ref}] LDA " + " | ".join(bits) + " | lobbied on: " + desc[:500])
        if n["lda_top"] > LDA_TOP_CAP:
            L_lda_top.append(f"(+{n['lda_top'] - LDA_TOP_CAP} more LDA filings listing the President or VP, not listed)")
        L_lda.append(f"NEW REGISTRATIONS ({len(rr)}):")
        for r in rr:
            add(r, "")
        L_lda.append(f"TERMINATIONS ({len(term)}):")
        for r in term:
            add(r, "")
        flagged = [r for r in rest if lda_line(r)[1]]
        n["lda_signal"] = len(flagged) + len(term)
        L_lda.append(f"ACTIVITY REPORTS WITH A SIGNAL ({len(flagged)} of {len(rest)}; the rest only counted above):")
        for r in sorted(flagged, key=lambda r: -float(r.get("income") or r.get("expenses") or 0))[:LDA_SIGNAL_CAP]:
            add(r, "")
        if len(flagged) > LDA_SIGNAL_CAP:
            L_lda.append(f"(+{len(flagged) - LDA_SIGNAL_CAP} more with a signal, not listed)")

    seen = load_manifest(SEEN_PATH)

    def ptr_block(ref, who, filed_iso, txs, url):
        """Trade table for assemble.py to append (stocks only); the bundle
        gets every trade line."""
        stock = [t for t in txs if is_stock(t)]
        lines = []
        for t in txs:
            lines.append("    " + " | ".join(x for x in [t.get("ticker") or "--", t.get("asset", "")[:70], t.get("type"),
                                                     t.get("tx_date") or "?", t.get("amount"), t.get("owner") or "",
                                                     t.get("asset_type") or "", t.get("status") or "", late_note(t, filed_iso)] if x))
        if stock:
            tables.append({"ref": ref, "who": who, "url": url, "filed": filed_iso, "rows": [
                [t.get("ticker") or "--", company(t.get("asset", "")), t.get("type") or "", t.get("tx_date") or "",
                 t.get("amount") or "", late_note(t, filed_iso)] for t in stock]})
        return lines

    # House
    if "disclosures-clerk.house.gov" not in down:
        try:
            rows = index_rows(today[:4])
            lb = date.fromisoformat(lookback)
            with tempfile.TemporaryDirectory() as tmp:
                for r in rows:
                    fd = trades.parse_date(r.get("FilingDate"))
                    if not fd or fd < lb:
                        continue
                    key = f"house:{r['DocID']}"
                    if key in seen:
                        continue
                    ft = r.get("FilingType")
                    sub = "ptr-pdfs" if ft == "P" else "financial-pdfs"
                    url = f"https://disclosures-clerk.house.gov/public_disc/{sub}/{r['Year']}/{r['DocID']}.pdf"
                    who = f"{r.get('Prefix', '')} {r.get('First', '')} {r.get('Last', '')} {r.get('Suffix', '')}".strip()
                    who = " ".join(who.split()) + f" ({r.get('StateDst')})"
                    ref = f"H{len(refs) + 1}"
                    refs[ref] = url
                    items.append({"key": key, "url": url})
                    n["house"] += 1
                    kind = {"P": "PTR", "C": "candidate report", "X": "extension request", "O": "annual report",
                            "A": "amendment", "T": "termination report", "D": "new-filer report"}.get(ft, f"type {ft}")
                    head = f"[{ref}] HOUSE {kind}: {who} | filed {fd.isoformat()}"
                    if ft != "P":
                        L_pfd.append(head)
                        continue
                    p = os.path.join(tmp, f"{r['DocID']}.pdf")
                    open(p, "wb").write(curl_bytes(url))
                    txs = trades.parse_house_ptr(pdftotext(p)) if os.path.getsize(p) else []
                    if not txs:
                        L_pfd.append(head + " | no trade lines parsed (scanned image or unusual layout: link only, "
                                            "say nothing about its contents)")
                        continue
                    L_pfd.append(head + f" | {len(txs)} transactions:")
                    L_pfd += ptr_block(ref, who, fd.isoformat(), txs, url)
        except Exception as e:  # noqa: BLE001
            errors.append(f"House disclosures: {e}")

    # Senate
    if "efdsearch.senate.gov" not in down:
        try:
            s = senate_session()
            rows = senate_search(s, mdy(date.fromisoformat(lookback)), filer_types="[]")
            for r in rows:
                url = senate_href(r)
                if not url:
                    continue
                rid = url.rstrip("/").split("/")[-1]
                key = f"senate:{rid}"
                if key in seen:
                    continue
                label = senate_label(r)
                who = f"{r[0]} {r[1]} ({re.sub('<[^>]+>', '', r[2]).strip()})"
                ref = f"S{len(refs) + 1}"
                refs[ref] = url
                items.append({"key": key, "url": url})
                n["senate"] += 1
                head = f"[{ref}] SENATE {label}: {who} | filed {r[4]}"
                if "/paper/" in url:
                    L_pfd.append(head + " | paper filing (scanned images): link only")
                    continue
                low = label.lower()
                if "periodic transaction" in low:
                    page = s.get(url, timeout=60).text
                    txs = trades.parse_senate_ptr(page)
                    filed = trades.parse_date(r[4])
                    if not txs:
                        L_pfd.append(head + " | no transactions parsed: link only")
                        continue
                    L_pfd.append(head + f" | {len(txs)} transactions:")
                    L_pfd += ptr_block(ref, who, filed.isoformat() if filed else None, txs, url)
                elif any(k in low for k in ("annual", "candidate", "blind trust", "new filer", "termination")):
                    page = s.get(url, timeout=60).text
                    body = page.split("<main", 1)[-1].split(">", 1)[-1] if "<main" in page else page
                    L_pfd += [head + " | detail page text:", "    " + plain(body)[:DETAIL_CHARS]]
                else:
                    L_pfd.append(head)
        except Exception as e:  # noqa: BLE001
            errors.append(f"Senate eFD: {e}")

    oge = load_json("data/oge_candidates.json", {}) or {}
    mech = load_json("content-mechanical.json", {}) or {}
    new_items = (n["fara"] + n["top"] + n["lda_top"] + n["lda_rr"] + n["lda_signal"] + n["house"] + n["senate"]
                 + len(oge.get("flagged") or []))

    d = date.fromisoformat(today)
    L = [f"=== TODAY'S INPUT (built by scripts/fetch_officials.py, {today}, {mode.upper()} edition) ===",
         f"TODAY = {today} ({d.strftime('%A, %B')} {d.day}, {d.year}). FARA/LDA window: posted {since} onward. "
         f"Disclosures: filed {lookback} onward, minus filings reported in earlier editions.",
         "Errors/gaps: " + ("; ".join(errors + gaps) or "none"), ""]
    L.append("--- OGE ETHICS FILINGS (section already written by the cheap-model pass; a script inserts it -- "
             "do not write it, only weigh it for Top Story) ---")
    for sec in mech.get("sections") or []:
        L.append(f"{plain(sec.get('headline'))}. " + " ".join(plain(p) for p in sec.get("paragraphs") or [])
                 + (" [no_news]" if sec.get("no_news") else ""))
    if not mech.get("sections"):
        # Haiku pass failed: the main pass writes the section itself.
        L.append("(the cheap-model OGE pass produced nothing usable today: WRITE an 'OGE Ethics Filings' section "
                 "yourself, first in `sections`, from the filings below -- one list item per flagged filing, "
                 "describing what the document shows; routine filings as a per-agency count note -- and say in "
                 "known_gaps that the OGE pass failed)")
        for f in oge.get("flagged") or []:
            ref = f"O{len(refs) + 1}"
            refs[ref] = f.get("link")
            L.append(f"[{ref}] {f.get('name')} | {f.get('title')} | {f.get('agency')} | {plain(f.get('type'))} | "
                     f"{f.get('docDate', '')[:10]} | flag: {f.get('flag_reason')}"
                     + (" | Pending Final OGE Disposition" if f.get("pending_final_disposition") else ""))
            if f.get("pdf_text"):
                L.append("    " + " ".join(f["pdf_text"].split())[:3000])
        by = oge.get("non_flagged_by_agency") or {}
        if by:
            L.append("Routine filings by agency: " + "; ".join(f"{k}: {len(v) if isinstance(v, list) else v}"
                                                              for k, v in by.items()))
    if mech.get("known_gaps"):
        L.append("OGE known gaps: " + mech["known_gaps"])
    L += ["", f"--- TOP OFFICIALS: the President, VP or cabinet in filings ({n['top']} FARA filings stamped "
              f"{days_ago_la(FARA_SCAN_DAYS)} onward not yet reported, {n['lda_top']} LDA filings) ---",
          "Lines are OCR'd: contact-log columns (date | official | title | method | purpose) are often interleaved "
          "across the ' / ' line breaks. tier name = the person is named; office = their immediate office's staff; "
          "title = only the post (POTUS etc.). Each person's post and tenure is in brackets."]
    L += L_top + L_lda_top
    if not (L_top or L_lda_top):
        L.append("(none)")
    L += ["", f"--- FARA ({n['fara']} new items: registrants, foreign principals, filed documents; "
              f"since {lookback}, minus items reported earlier) ---"] + L_fara
    L += ["", f"--- PERSONAL FINANCIAL DISCLOSURES ({n['house']} House, {n['senate']} Senate) ---",
          "Trade lines: ticker | asset | type | date | amount | owner | asset type | status | LATE Nd = disclosed N days "
          "after the trade (STOCK Act allows 45)."] + L_pfd
    L += ["", f"--- LDA ({n['lda']} filings) ---"] + L_lda
    L += ["", "--- DETERMINISTIC EXTRAS (assemble.py appends these sections after you; do not copy them) ---"]
    L += extras_digest("officials")
    open(OUT_TXT, "w").write("\n".join(L) + "\n")
    json.dump({"today": today, "errors": errors, "counts": n, "new_items": new_items, "refs": refs, "items": items,
               "window": f"FARA/LDA posted {since} onward; disclosures filed {lookback} onward"},
              open(OUT_JSON, "w"), indent=1)

    # Stock-trade tables for assemble.py to append under the disclosures section.
    blocks = []
    for t in tables:
        blocks.append({"type": "subhead", "text": f"{html.escape(t['who'])} — trades (filed {t['filed']})"})
        blocks.append({"type": "table", "headers": ["Ticker", "Company", "Type", "Date", "Amount", "Late?"],
                       "rows": [[html.escape(c) for c in row] for row in t["rows"]]})
        blocks.append({"type": "note", "text": f'<a href="{t["url"]}">Read the filing</a>.'})
    json.dump({"date": today, "append": {"Personal Financial Disclosures & Stock Trades": blocks}},
              open(APPEND, "w"), indent=1)
    print(f"fetch_officials: FARA {n['fara']}, LDA {n['lda']} ({n['lda_rr']} RR), House {n['house']}, Senate "
          f"{n['senate']}, {len(tables)} trade tables; bundle {sum(map(len, L)):,} chars; errors {len(errors)}",
          file=sys.stderr)


if __name__ == "__main__":
    main()
