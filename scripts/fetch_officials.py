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
import trades  # noqa: E402
from dc_common import check_reachable, days_ago_la, http_get, load_manifest, pdftotext, today_la  # noqa: E402
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
def fara_report(page, since_mdy, today_mdy):
    url = f"{FARA}f?p=1381:{page}:0::NO:{page}:P{page}_FROMDATE,P{page}_TODATE:{since_mdy},{today_mdy}"
    # efile.fara.gov 403s Python's TLS fingerprint; curl gets through.
    body = curl_bytes(url).decode("utf-8", "replace")
    if "a-IRR-table" not in body and "a-IRR-noDataMsg" in body:
        return []  # APEX drops the table entirely when the window is empty
    if "a-IRR-table" not in body and "is-loading" in body:
        # Seen 2026-10-12 on the terminated-registrants report (page 7): the
        # rows load by AJAX, even for a six-week window.
        raise LookupError(f"FARA page {page} now loads its rows client-side; not fetchable as plain HTML")
    if "a-IRR-table" not in body:
        raise RuntimeError(f"FARA page {page}: no report table in the response ({len(body)} bytes)")
    t = body[body.find("a-IRR-table"):]
    rows = []
    for tr in re.findall(r"<tr\s*>(.*?)</tr>", t, re.S):
        cells = dict(re.findall(r'<td[^>]*headers="([A-Z_]+)"[^>]*>(.*?)</td>', tr, re.S))
        if cells.get("NAME"):
            num = plain(cells.get("REG_NUMBER"))
            rows.append({"name": plain(cells["NAME"]), "number": num,
                         "registered": plain(cells.get("REG_DATE")), "terminated": plain(cells.get("TERMINATION_DATE")),
                         "url": f"{FARA}f?p=1381:200::::RP,200:P200_REG_NUMBER:{num}"})
    return rows


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
    n = {"fara": 0, "lda_rr": 0, "lda": 0, "lda_signal": 0, "house": 0, "senate": 0}
    hosts = ["disclosures-clerk.house.gov", "efdsearch.senate.gov", "lda.gov", "efile.fara.gov"]
    down = [h for h in hosts if not check_reachable(h)]
    if len(down) >= 2:
        json.dump({"today": today, "errors": [f"unreachable: {', '.join(down)}"], "new_items": 0, "refs": {}},
                  open(OUT_JSON, "w"), indent=1)
        open(OUT_TXT, "w").write(f"SOURCES UNREACHABLE: {', '.join(down)}\n")
        return
    errors += [f"{h} unreachable" for h in down]
    L_fara, L_lda, L_pfd, tables = [], [], [], []

    # FARA
    if "efile.fara.gov" not in down:
        sd, td = date.fromisoformat(since), date.fromisoformat(today)
        for page, kind in ((6, "NEW"), (7, "TERMINATED")):
            try:
                for i, r in enumerate(fara_report(page, sd.strftime("%m/%d/%Y"), td.strftime("%m/%d/%Y"))):
                    ref = f"F{len(refs) + 1}"
                    refs[ref] = r["url"]
                    n["fara"] += 1
                    L_fara.append(f"[{ref}] {kind}: {r['name']} | registration #{r['number']} | registered {r['registered']}"
                                  + (f" | terminated {r['terminated']}" if r["terminated"] not in ("", "-") else ""))
            except LookupError as e:
                gaps.append(f"FARA {kind.lower()} registrants not checked: {e}")
            except Exception as e:  # noqa: BLE001
                errors.append(f"FARA {kind.lower()} registrants: {e}")

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
    new_items = (n["fara"] + n["lda_rr"] + n["lda_signal"] + n["house"] + n["senate"]
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
    L += ["", f"--- FARA REGISTRANTS ({n['fara']}) ---"] + L_fara
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
