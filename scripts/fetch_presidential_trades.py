#!/usr/bin/env python3
"""The President's (and Vice President's) trades, counted and summarized
(added 2026-10-03). Trump's 278-Ts run to ~1,000+ transactions a month in
scanned, OCR'd PDFs -- a routine one-line OGE listing buries the story
("Weapons, Oil, AI: Trump Made 1,156 Personal Financial Trades During July").

For each new Trump/Vance 278-T PDF in the window: parse every transaction
(trades.parse_oge_278t), then report the declared total, how many parsed,
purchases vs. sales, the dollar range, the date span, a sector breakdown
using data/events.json's sector keyword lists, the largest transactions, and
any trades inside an event window. Pure data, no LLM.

Persisted dedupe: data/presidential-trades-seen.json.
Output: extras/officials/presidential_trades.json
"""
import os
import re
import sys
import tempfile
from collections import Counter

import oge_index
import trades
from dc_common import download, pdftotext
from dc_extras import SeenManifest, esc, link, load_json, lookback, mdY, money, section, write_extra

SEEN_PATH = "data/presidential-trades-seen.json"
FILERS = {"Trump, Donald": "President Donald J. Trump", "Vance, J": "Vice President JD Vance"}
MUNI_RE = re.compile(r"\b(?:CNTY|COUNTY|SCH(?:OOL)? DIST|REV(?:ENUE)?|RFDG|AUTH|MUNI|G\.?O\.?|BDS?|BOND|ISD|CITY OF|ST |STATE OF|\d+\.\d+\s*%|Due [A-Z][a-z]{2})", re.I)


def sector_regexes():
    cfg = load_json("data/events.json", {}) or {}
    out = {}
    for name, s in (cfg.get("sectors") or {}).items():
        pats = s.get("keywords", [])
        out[name] = (re.compile(r"\b(?:" + "|".join(pats) + r")", re.I) if pats else None, set(s.get("tickers", [])))
    return out


def classify_sector(t, sectors):
    for name, (rx, tickers) in sectors.items():
        if (t.get("ticker") and t["ticker"] in tickers) or (rx and rx.search(t["asset"])):
            return name
    return None


def summarize(filer, row, txs, declared, events, sectors):
    buys = [t for t in txs if t["type"] == "Purchase"]
    sells = [t for t in txs if t["type"] in ("Sale", "Sale (partial)")]
    lo = sum(t["amount_floor"] or 0 for t in txs)
    hi = sum(trades.AMOUNT_CEILINGS.get(t["amount_floor"], 0) for t in txs)
    dates = sorted(t["tx_date"] for t in txs if t.get("tx_date"))
    munis = sum(1 for t in txs if MUNI_RE.search(t["asset"]))
    by_sector = Counter(classify_sector(t, sectors) for t in txs)
    by_sector.pop(None, None)
    timed = [(t, h) for t in txs for h in trades.event_hits(t, events)]
    big = sorted(txs, key=lambda t: -(t["amount_floor"] or 0))[:15]

    url = row["_link"]
    paras = [
        f"<strong>{esc(filer)}</strong> filed a {link('periodic transaction report', url)} (posted {mdY(row['docDate'])}) "
        f"listing <strong>{declared:,} transactions</strong>"
        + (f" made between {mdY(dates[0])} and {mdY(dates[-1])}" if dates else "") + ".",
        f"Parsed {len(txs):,} of {declared:,} line items from the scanned PDF: {len(buys):,} purchases, {len(sells):,} sales"
        + (f", {len(txs) - len(buys) - len(sells)} unclear from the scan" if len(txs) - len(buys) - len(sells) else "")
        + f". Combined value of the parsed trades: {money(lo)} to {money(hi)} (disclosure ranges). "
        f"{munis:,} look like municipal or other bonds; the rest are stocks, funds and other assets.",
    ]
    if by_sector:
        nice = {"oil_gas": "oil & gas", "defense": "defense", "nuclear_uranium": "nuclear/uranium",
                "airlines": "airlines", "crypto": "crypto", "ai_chips": "AI & chips"}
        paras.append("Sector-tagged trades: " + ", ".join(f"{nice.get(k, k)} {v}" for k, v in by_sector.most_common()) + ".")
    blocks = [
        {"type": "subhead", "text": "Largest transactions"},
        {"type": "table", "headers": ["#", "Asset", "Type", "Date", "Amount"],
         "rows": [[str(t["n"]), esc(t["asset"][:80]), esc(t["type"]), mdY(t["tx_date"]) if t.get("tx_date") else "?", esc(t["amount"])] for t in big]},
    ]
    if timed:
        blocks.append({"type": "subhead", "text": "Trades near market-moving events"})
        blocks.append({"type": "list", "items": [
            f"{esc(t['type'].lower())} of {esc(t['asset'][:80])} ({esc(t['amount'])}) on {mdY(t['tx_date'])}, {esc(trades.timing_phrase(h))}"
            for t, h in timed[:20]]})
    blocks.append({"type": "note", "text": "Counted mechanically from OGE's OCR of a scanned filing: a few line items in every report are too garbled to parse, and dollar figures are the disclosure form's ranges, not exact amounts. Check any single trade against the PDF before quoting it."})
    return paras, blocks, f"{filer}: {declared:,} transactions in a new 278-T ({len(buys)} buys / {len(sells)} sales parsed)."


def main():
    start = lookback()
    try:
        rows = oge_index.fetch_all()
    except Exception as e:  # noqa: BLE001
        write_extra("officials", "presidential_trades", [], f"Presidential trades check unavailable: {e}")
        return
    seen = SeenManifest(SEEN_PATH, keep_days=120)
    events, sectors = trades.load_events(), sector_regexes()
    tmp = tempfile.mkdtemp()
    sections, summaries = [], []
    for r in sorted(rows, key=lambda r: r.get("docDate", "")):
        filer = next((v for k, v in FILERS.items() if (r.get("name") or "").startswith(k)), None)
        if not filer or r.get("docDate", "")[:10] < start:
            continue
        r["_link"] = oge_index.doc_link(r.get("type"))
        if not oge_index.base_label(r.get("type")).startswith("278 Transaction") or not oge_index.is_pdf(r["_link"]):
            continue
        if r["_link"] in seen:
            continue
        path = os.path.join(tmp, f"p{len(summaries)}.pdf")
        try:
            download(r["_link"], path, timeout=120)
        except Exception as e:  # noqa: BLE001
            print(f"download failed {r['_link']}: {e}", file=sys.stderr)
            continue
        txs, declared = trades.parse_oge_278t(pdftotext(path))
        if not txs:
            summaries.append(f"{filer} posted a 278-T that couldn't be parsed ({r['_link']}).")
            continue
        paras, blocks, summary = summarize(filer, r, txs, max(declared, len(txs)), events, sectors)
        verb = "Trades" if filer.startswith("President") else "Vice President's Trades"
        sections.append(section("The President's Trades" if filer.startswith("President") else verb,
                                f"{filer.split(' ', 1)[1] if filer.startswith('President') else filer} reported {max(declared, len(txs)):,} personal trades",
                                paras, blocks))
        summaries.append(summary)
        seen.add(r["_link"])
    seen.save()
    write_extra("officials", "presidential_trades", sections, " ".join(summaries) or "No new presidential or vice-presidential 278-Ts.")


if __name__ == "__main__":
    main()
