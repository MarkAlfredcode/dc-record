"""Mechanical transaction parsers + trade-timing checks, shared by
fetch_ethics_agreements.py (OGE 278-Ts vs. ethics-agreement deadlines) and
fetch_presidential_trades.py (the House/Senate PTR parsers were also used by
the Conflicts edition's fetch_pfd_analysis.py, removed 2026-10-10).

Parsers are deliberately tolerant and report what they could NOT parse, so
callers can say "parsed X of Y" instead of implying completeness:
- House PTR PDFs (pdftotext -layout): one transaction per line, asset name
  and the top of the amount range can wrap onto the following line.
- Senate PTRs: an HTML table on efdsearch.senate.gov.
- OGE 278-T PDFs: clean digital ones (most officials) AND the President's,
  which are scanned and OCR'd with heavy noise ("lourchaso" = Purchase,
  "717/2026" = 7/7/2026) -- confirmed 2026-10-03 against Trump's 9/8/2026
  278-T: 1,049 of its 1,156 numbered transactions parse.
"""
import html as htmllib
import re
from datetime import date, datetime

AMOUNT_FLOORS = [1001, 15001, 50001, 100001, 250001, 500001, 1000001, 5000001, 25000001, 50000001]
AMOUNT_LABELS = {
    1001: "$1,001 - $15,000", 15001: "$15,001 - $50,000", 50001: "$50,001 - $100,000",
    100001: "$100,001 - $250,000", 250001: "$250,001 - $500,000", 500001: "$500,001 - $1,000,000",
    1000001: "$1,000,001 - $5,000,000", 5000001: "$5,000,001 - $25,000,000",
    25000001: "$25,000,001 - $50,000,000", 50000001: "Over $50,000,000",
}
AMOUNT_CEILINGS = {
    1001: 15000, 15001: 50000, 50001: 100000, 100001: 250000, 250001: 500000,
    500001: 1000000, 1000001: 5000000, 5000001: 25000000, 25000001: 50000000, 50000001: 50000000,
}


def amount_floor(text):
    """Snap a (possibly OCR-mangled) lower bound like '1.000 001' to the
    nearest standard disclosure bracket floor."""
    digits = re.sub(r"\D", "", text or "")
    if not digits:
        return None
    v = int(digits)
    same_len = [b for b in AMOUNT_FLOORS if len(str(b)) == len(digits)]
    pool = same_len or AMOUNT_FLOORS
    return min(pool, key=lambda b: abs(b - v))


def parse_date(s):
    s = re.sub(r"\s+", "", s or "")
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None


def classify(type_text):
    t = (type_text or "").lower()
    if re.search(r"partial", t):
        return "Sale (partial)"
    if re.search(r"urch|urc|chas|^p$", t):
        return "Purchase"
    if re.search(r"sal|ale|^s$", t):
        return "Sale"
    if re.search(r"xch|^e$", t):
        return "Exchange"
    return None


# ---------- House PTR ----------
_HOUSE_LINE = re.compile(
    r"^\s*(?:\d{6,}\s+)?(?:(?P<owner>SP|JT|DC)\s+)?(?P<asset>\S.*?)\s+"
    r"(?P<type>P|S|S \(partial\)|E)\s+"
    r"(?P<tx>\d{2}/\d{2}/\d{4})\s+(?P<notif>\d{2}/\d{2}/\d{4})\s+"
    r"(?P<amt>\$[\d,]+(?:\s*-\s*(?:\$[\d,]+)?)?|Over \$[\d,]+)"
)


def parse_house_ptr(text):
    lines = text.splitlines()
    out = []
    for i, line in enumerate(lines):
        m = _HOUSE_LINE.match(line)
        if not m:
            continue
        asset, amt = m.group("asset").strip(), m.group("amt").strip()
        # "Filing Status: New/Amended" follows each trade (pdftotext garbles
        # the label to "F   S   : Amended"). Amended rows are corrections to
        # trades already disclosed, so they can't be judged late by date.
        status = "New"
        for nxt in lines[i + 1:i + 6]:
            if _HOUSE_LINE.match(nxt):
                break
            sm = re.search(r":\s*(New|Amended|Deleted)\b", nxt)
            if sm:
                status = sm.group(1)
                break
        # Continuation lines: more asset name (e.g. "[GS]") and/or the top of
        # the amount range, until a "Filing Status:"-style label line.
        for nxt in lines[i + 1:i + 3]:
            if not nxt.strip() or ":" in nxt or _HOUSE_LINE.match(nxt):
                break
            tail = re.search(r"(\$[\d,]+)\s*$", nxt)
            body = nxt[: tail.start()] if tail else nxt
            if body.strip():
                asset += " " + body.strip()
            if tail and amt.endswith("-"):
                amt += " " + tail.group(1)
        tx = parse_date(m.group("tx"))
        out.append({
            "owner": m.group("owner") or "",
            "asset": re.sub(r"\s+", " ", asset),
            "ticker": _ticker(asset),
            "asset_type": (re.search(r"\[([A-Z]{2})\]", asset) or [None, None])[1],
            "type": {"P": "Purchase", "S": "Sale", "E": "Exchange"}.get(m.group("type"), "Sale (partial)"),
            "tx_date": tx.isoformat() if tx else None,
            "amount": amt,
            "amount_floor": amount_floor(amt.split("-")[0]),
            "status": status,
        })
    return out


def _ticker(asset):
    m = re.search(r"\(([A-Z][A-Z.\-]{0,6})\)", asset or "")
    return m.group(1) if m else None


# ---------- Senate PTR ----------
def parse_senate_ptr(page_html):
    body = page_html.split("<tbody", 1)[-1].split("</tbody>", 1)[0]
    out = []
    for row in re.findall(r"<tr>(.*?)</tr>", body, re.S):
        cells = [re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", c))).strip()
                 for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
        if len(cells) < 8:
            continue
        _, tx, owner, ticker, asset, asset_type, ttype, amt = cells[:8]
        d = parse_date(tx)
        out.append({
            "owner": owner,
            "asset": asset,
            "ticker": None if ticker in ("--", "") else ticker,
            "asset_type": asset_type,
            "type": classify(ttype) or ttype,
            "tx_date": d.isoformat() if d else None,
            "amount": amt,
            "amount_floor": amount_floor(amt.split("-")[0]),
        })
    return out


# ---------- OGE 278-T ----------
def parse_oge_278t(text):
    """Returns (transactions, declared_count). declared_count is the highest
    transaction number seen on a numbered row -- the honest denominator."""
    out, declared = [], 0
    for line in text.splitlines():
        m = re.match(r"^\s*(\d{1,4})\s{2,}(.*)$", line)
        if not m or "$" not in line:
            continue
        n = int(m.group(1))
        left, amt = m.group(2).split("$", 1)
        toks = [t for t in re.split(r"\s{2,}", left.strip()) if t]
        if toks and len(toks[-1]) <= 4 and re.fullmatch(r"[A-Za-z]{1,4}", toks[-1]):
            toks = toks[:-1]  # "Yes"/"No"/OCR'd "Vos" notification column
        if len(toks) < 3:
            continue
        date_tok, type_tok = toks[-1], toks[-2]
        desc = " ".join(toks[:-2])
        ttype = classify(type_tok)
        if not ttype:
            # OCR sometimes glues the type onto the description column
            ttype = classify(desc.split()[-1]) if desc.split() else None
        d = parse_date(date_tok)
        declared = max(declared, n)
        out.append({
            "n": n,
            "asset": re.sub(r"\s+", " ", desc).strip(" •.,"),
            "ticker": _ticker(desc),
            "type": ttype or "Unclear (OCR)",
            "tx_date": d.isoformat() if d else None,
            "amount": AMOUNT_LABELS.get(_low(amt), "$" + amt.strip()[:30]),
            "amount_floor": _low(amt),
        })
    # One transaction per number; keep the first parse of each.
    uniq = {}
    for t in out:
        uniq.setdefault(t["n"], t)
    return list(uniq.values()), declared


def _low(amt):
    """Lower bound of a range whose separator OCR may render as '-', '·',
    ':' or '•' ("$50 001·$100000"): the first run of digits/spaces/commas."""
    m = re.match(r"\s*(\d[\d ,.]*)", amt or "")
    return amount_floor(m.group(1)) if m else None


# ---------- timing checks ----------
def days_between(a_iso, b_iso):
    try:
        return (date.fromisoformat(b_iso[:10]) - date.fromisoformat(a_iso[:10])).days
    except (TypeError, ValueError):
        return None


def load_events(path="data/events.json"):
    import json
    try:
        with open(path) as f:
            cfg = json.load(f)
    except FileNotFoundError:
        return []
    sectors = cfg.get("sectors", {})
    events = []
    for ev in cfg.get("events", []):
        if not ev.get("date"):
            continue  # undated placeholder -- skip until someone fills it in
        pats = list(ev.get("keywords", []))
        for s in ev.get("sectors", []):
            pats.extend(sectors.get(s, {}).get("keywords", []))
        tickers = set(ev.get("tickers", []))
        for s in ev.get("sectors", []):
            tickers.update(sectors.get(s, {}).get("tickers", []))
        any_asset = bool(ev.get("any_asset"))
        if not pats and not tickers and not any_asset:
            continue
        events.append({
            "date": ev["date"], "label": ev["label"], "source": ev.get("source"),
            "before": int(ev.get("window_before_days", 14)), "after": int(ev.get("window_after_days", 3)),
            "regex": re.compile(r"\b(?:" + "|".join(pats) + r")", re.I) if pats else None,
            "tickers": tickers,
            "any_asset": any_asset,
            # market-wide events only count trades of real size (default: the
            # $15,001+ bracket) -- small same-day buy/sell rebalancing is noise
            "min_amount": int(ev.get("min_amount", 15001 if any_asset else 0)),
        })
    return events


_NON_EQUITY = re.compile(r"\[(?:GS|CS|BA|OT|RE|DO)\]|\b(?:bond|note|bill|muni|treasury|money market|cash|cd|certificate of deposit|"
                         r"cnty|county|sch(?:ool)? dist|rev(?:enue)?\b|rfdg|auth\b)|\d+\.\d+\s*%", re.I)


def event_hits(trade, events):
    """Trades on a related asset within an event's window. Negative
    days_before_event = the trade came AFTER the event."""
    hits = []
    if not trade.get("tx_date"):
        return hits
    for ev in events:
        delta = days_between(trade["tx_date"], ev["date"])
        if ev["min_amount"] and (trade.get("amount_floor") or 0) < ev["min_amount"]:
            continue
        if delta is None or not (-ev["after"] <= delta <= ev["before"]):
            continue
        # any_asset: a market-wide shock (Liberation Day, the tariff pause) --
        # every equity trade counts, but bonds/munis/cash-like holdings don't.
        related = (ev["any_asset"] and not _NON_EQUITY.search(trade.get("asset") or "")) or (
            trade.get("ticker") and trade["ticker"] in ev["tickers"]) or (
            ev["regex"] and ev["regex"].search(trade.get("asset") or ""))
        if related:
            hits.append({"event": ev["label"], "event_date": ev["date"], "days_before_event": delta,
                         "source": ev.get("source")})
    return hits


def timing_phrase(hit):
    d = hit["days_before_event"]
    if d > 0:
        return f"{d} day{'s' if d != 1 else ''} before {hit['event']} ({hit['event_date']})"
    if d == 0:
        return f"the same day as {hit['event']}"
    return f"{-d} day{'s' if d != -1 else ''} after {hit['event']} ({hit['event_date']})"


def text_diff(old_text, new_text, cap=15):
    """Line-level diff of two disclosure texts, whitespace-normalized, page
    furniture dropped. Returns (added, removed) lists of lines."""
    def clean(t):
        keep = []
        for ln in (t or "").splitlines():
            ln = re.sub(r"\s+", " ", ln).strip()
            if len(ln) < 4 or re.search(r"Page \d+ of \d+|Filing ID|Filing Type:|Filing Date:|Digitally Signed|electronically signed|^Page \d+", ln, re.I):
                continue
            keep.append(ln)
        return keep
    a, b = clean(old_text), clean(new_text)
    sa, sb = set(a), set(b)
    added = [ln for ln in b if ln not in sa]
    removed = [ln for ln in a if ln not in sb]
    return added[:cap], removed[:cap], len(added), len(removed)
