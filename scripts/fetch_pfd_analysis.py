#!/usr/bin/env python3
"""Disclosure Red Flags for congressional financial disclosures (added
2026-10-03) -- the analysis layer on top of the Officials edition's existing
PTR coverage, which lists trades but never checks them against anything.

For every House/Senate filing new in the LOOKBACK window:
- PTRs: parse every trade mechanically (trades.py) and flag
    * LATE DISCLOSURES -- trade disclosed more than 45 days after it happened
      (the STOCK Act outer limit), and
    * EVENT TIMING -- trades in a sector tied to a data/events.json event,
      within that event's window.
- Amended annual reports: fetch the amendment AND the original and print
  what actually changed (lines added/removed), instead of just "amended".

Pure data, no LLM. Persisted dedupe: data/pfd-analysis-seen.json (reverted
on site-refresh-only runs like the other seen-manifests).

Output: extras/conflicts/pfd_analysis.json (moved from the Officials edition to the
web-only Conflicts edition 2026-10-04). Also records each flag in
data/conflict-signals.json and hands every parsed stock trade to
trade_awards.py (Trades Near Contract Awards).
"""
import csv
import io
import os
import re
import sys
import tempfile
import zipfile

import trades
import trade_awards
from conflict_signals import Signals
from stock_act_ledger import Ledger, history_phrase
from jurisdiction import Jurisdiction
from dc_common import download, http_get, pdftotext, today_la
from dc_extras import SeenManifest, esc, link, lookback, mdY, section, write_extra
from fetch_deadlines import iso_from_mdy, senate_href, senate_label, senate_search, senate_session

HOUSE_ZIP = "https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}FD.zip"
HOUSE_PTR = "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{year}/{doc}.pdf"
HOUSE_FD = "https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}/{doc}.pdf"
SEEN_PATH = "data/pfd-analysis-seen.json"
LATE_DAYS = 45
TIMED_MIN_AMOUNT = 15001  # $1,001-$15,000 trades matched to broad events are noise, not red flags
MAX_PDFS = 150  # safety valve on a heavy day; every skipped filing is counted in the note
# Outside income worth a look ("Members of Congress Score Major Paydays From Books")
OUTSIDE_RE = re.compile(r"royalt|\bbook|publish|\badvance\b|author|speak|honorari|lectur|\bboard\b|director|consult|advis|"
                        r"\bfees?\b|commission|contract|partner|licens", re.I)
NOT_OUTSIDE_RE = re.compile(r"pension|retire|\bira\b|annuit|distribution|social security|401|thrift|deferred comp", re.I)
COMMERCIAL_RE = re.compile(r"\b(?:inc|llc|l\.l\.c|corp|corporation|company|co\.|holdings|capital|partners|bank|group|ltd|lp|fund|ventures)\b", re.I)


def house_rows(year):
    status, body = http_get(HOUSE_ZIP.format(year=year), timeout=120)
    if status >= 400:
        return []
    z = zipfile.ZipFile(io.BytesIO(body))
    name = next(n for n in z.namelist() if n.endswith(".txt"))
    return list(csv.DictReader(io.StringIO(z.read(name).decode("utf-8-sig", errors="replace")), delimiter="\t"))


def pdf_text(url, tmp):
    path = os.path.join(tmp, re.sub(r"\W", "_", url)[-80:] + ".pdf")
    try:
        download(url, path, timeout=60)
    except Exception as e:  # noqa: BLE001
        print(f"download failed {url}: {e}", file=sys.stderr)
        return None  # distinct from "" (downloaded but scanned/unreadable)
    return pdftotext(path)


def who_house(r):
    where = f"{r['StateDst'][:2]}-{r['StateDst'][2:].lstrip('0') or 'AL'}"
    if (r.get("Prefix") or "").strip().startswith("Hon"):
        return f"Rep. {r['First']} {r['Last']} ({where})"
    return f"{r['First']} {r['Last']} (House candidate, {where})"


def main():
    start = lookback()
    today = today_la()
    seen = SeenManifest(SEEN_PATH, keep_days=30)
    events = trades.load_events()
    late, timed, diffs, notes, errors, oversight, outside = [], [], [], [], [], [], []
    budget = MAX_PDFS
    tmp = tempfile.mkdtemp()

    # ---------------- House ----------------
    year = int(today[:4])
    rows = house_rows(year) + house_rows(year - 1)
    if not rows:
        errors.append("House disclosure index unavailable")
    fresh = [r for r in rows if (iso_from_mdy(r.get("FilingDate", "")) or "") >= start
             and r.get("FilingType") in ("P", "A", "O") and f"house:{r['DocID']}" not in seen]
    jur = Jurisdiction()
    for r in fresh:
        key = f"house:{r['DocID']}"
        filed = iso_from_mdy(r["FilingDate"])
        if budget <= 0:
            notes.append(f"{who_house(r)} filing {r['DocID']} not analyzed (volume cap).")
            continue
        budget -= 1
        if r["FilingType"] == "P":
            url = HOUSE_PTR.format(year=r["Year"], doc=r["DocID"])
            text = pdf_text(url, tmp)
            if text is None:
                notes.append(f"{link(who_house(r) + ' PTR', url)}: download failed, will retry next run.")
                continue  # not marked seen
            parsed = trades.parse_house_ptr(text)
            if not parsed:
                notes.append(f"{link(who_house(r) + ' PTR', url)}: scanned or unparseable, not checked.")
            comms = jur.member_committees(r["Last"], r["First"], r["StateDst"][:2]) if "Hon" in (r.get("Prefix") or "") else []
            for t in parsed:
                _check_trade(t, who_house(r), filed, url, events, late, timed, jur, comms, oversight)
        elif r["FilingType"] == "O":
            url = HOUSE_FD.format(year=r["Year"], doc=r["DocID"])
            fd_text = pdf_text(url, tmp)
            if fd_text is None:
                notes.append(f"{link(who_house(r) + ' annual report', url)}: download failed, will retry next run.")
                continue
            _house_outside(who_house(r), url, fd_text, outside)
        else:  # amended annual report -> diff against the original
            # The original is an annual (O) or, for non-incumbents, a candidate report (C).
            orig = sorted([o for o in rows if o["FilingType"] in ("O", "C") and o["Last"] == r["Last"]
                           and o["First"] == r["First"] and o["StateDst"] == r["StateDst"]
                           and (iso_from_mdy(o["FilingDate"]) or "") < filed],
                          key=lambda o: iso_from_mdy(o["FilingDate"]) or "", reverse=True)
            new_url = HOUSE_FD.format(year=r["Year"], doc=r["DocID"])
            if not orig:
                notes.append(f"{link(who_house(r) + ' amended report', new_url)}: original not found in the index, no diff.")
            else:
                old_url = HOUSE_FD.format(year=orig[0]["Year"], doc=orig[0]["DocID"])
                budget -= 1
                new_text, old_text = pdf_text(new_url, tmp), pdf_text(old_url, tmp)
                if new_text is None or old_text is None:
                    notes.append(f"{link(who_house(r) + ' amended report', new_url)}: download failed, will retry next run.")
                    continue
                diffs.append(_diff(who_house(r), "candidate report" if orig[0]["FilingType"] == "C" else f"CY{r['Year']} annual report", old_url, new_url,
                                   old_text, new_text))
                _house_outside(who_house(r), new_url, new_text, outside)
        seen.add(key)

    # ---------------- Senate ----------------
    try:
        s = senate_session()
        y, m, d = start.split("-")
        srows = senate_search(s, f"{m}/{d}/{y}")
        for r in srows:
            href = senate_href(r)
            lab = senate_label(r)
            if not href:
                continue
            key = "senate:" + href.rstrip("/").split("/")[-1]
            if key in seen:
                continue
            who = f"Sen. {r[0]} {r[1]}"
            filed = iso_from_mdy(r[4])
            if "/paper/" in href:
                if "Periodic Transaction" in lab:
                    notes.append(f"{link(who + ' paper PTR', href)}: scanned paper filing, not checked.")
                seen.add(key)
                continue
            if budget <= 0:
                notes.append(f"{link(who + ' ' + lab, href)} not analyzed (volume cap).")
                continue
            if lab.startswith("Periodic Transaction"):
                budget -= 1
                page = s.get(href, timeout=60).text
                comms = jur.member_committees(r[1].split(",")[0], r[0])
                amended = "Amendment" in lab  # an amendment restates old trades; the original filing governs lateness
                for t in trades.parse_senate_ptr(page):
                    if amended:
                        t["status"] = "Amended"
                    _check_trade(t, who, filed, href, events, late, timed, jur, comms, oversight)
            elif lab.startswith("Annual Report") and "Extension" not in lab and "/annual/" in href:
                budget -= 1
                _senate_outside(who, href, s.get(href, timeout=60).text, outside)
            if lab.startswith("Annual Report") and "Amendment" in lab and "/annual/" in href:
                base = re.sub(r"\s*\(Amendment.*\)", "", lab)
                hist = senate_search(s, "01/01/2012", report_types="[7]", last_name=r[1].split(",")[0])
                orig = sorted([h for h in hist if senate_label(h) == base and "/paper/" not in (senate_href(h) or "")],
                              key=lambda h: iso_from_mdy(h[4]) or "")
                if not orig:
                    notes.append(f"{link(who + ' ' + lab, href)}: original not found, no diff.")
                else:
                    budget -= 2
                    old_href = senate_href(orig[0])
                    diffs.append(_diff(who, base, old_href, href, _html_text(s.get(old_href, timeout=60).text),
                                       _html_text(s.get(href, timeout=60).text)))
            seen.add(key)
    except Exception as e:  # noqa: BLE001
        errors.append(f"Senate eFD analysis failed: {e}")

    seen.save()
    # Repeat-offender context, one ledger entry per filing with late trades.
    ledger = Ledger()
    hist = {}
    for x in late:
        hist.setdefault(x["url"], (x["who"], x["filed"], []))[2].append(x)
    history = {}
    for url, (who_, filed, xs) in hist.items():
        n, first = ledger.record(who_, url, filed, len(xs))
        history[url] = history_phrase(n, first)
    ledger.save()
    sections, summary = _build(late, timed, diffs, notes, errors, history, oversight, outside, jur)
    write_extra("conflicts", "pfd_analysis", sections, summary)
    signals = Signals()
    for x in late:
        signals.add(f"late|{x['url']}|{x['t']['asset']}|{x['t']['tx_date']}|{x['t']['type']}", "late_trade", x["who"], x["filed"],
                    f"{x['t']['type']} of {x['t']['asset'][:60]} ({x['t']['amount']}) disclosed {x['lag']} days after the trade",
                    x["url"], company=x["t"]["asset"])
    for x in timed:
        signals.add(f"timed|{x['url']}|{x['t']['asset']}|{x['t']['tx_date']}|{x['t']['type']}", "event_timed", x["who"], x["t"]["tx_date"],
                    f"{x['t']['type']} of {x['t']['asset'][:60]} ({x['t']['amount']}): {trades.timing_phrase(x['hit'])}",
                    x["url"], company=x["t"]["asset"])
    for x in oversight:
        signals.add(f"oversight|{x['url']}|{x['t']['asset']}|{x['t']['tx_date']}|{x['t']['type']}", "oversight_trade", x["who"], x["t"]["tx_date"],
                    f"{x['t']['type']} of {x['t']['asset'][:60]} ({x['t']['amount']}) in a sector overseen by {x['committee']}",
                    x["url"], company=x["t"]["asset"])
    try:
        trade_awards.run(ALL_TRADES, signals)
    except Exception as e:  # noqa: BLE001
        print(f"trade_awards failed: {e}", file=sys.stderr)
    signals.save()


def _html_text(page):
    body = page.split("<main", 1)[-1] if "<main" in page else page
    body = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", body, flags=re.S)
    body = re.sub(r"</(tr|p|div|h\d|li)>", "\n", body)
    import html as h
    return h.unescape(re.sub(r"<[^>]+>", " ", body))


def _house_outside(who, url, text, outside):
    """Schedule C (earned income) and Schedule E (positions) of a House
    annual report. pdftotext garbles the headers to e.g. 'S   C: E   I'."""
    def section(start, end):
        m = re.search(start + r"[^\n]*\n(.*?)\n[^\n]*" + end, text, re.S)
        return [] if not m else [ln for ln in m.group(1).split("\n") if ln.strip()]
    for ln in section(r" C: E", r" D: L"):
        cols = [c for c in re.split(r"\s{2,}", ln.strip()) if c]
        if len(cols) < 2 or cols[0].lower().startswith("source"):
            continue
        blob = " ".join(cols)
        if OUTSIDE_RE.search(blob) and not NOT_OUTSIDE_RE.search(blob):
            outside.append({"who": who, "url": url, "kind": "income", "text": " · ".join(cols)})
    for ln in section(r" E: P", r" F: A"):
        cols = [c for c in re.split(r"\s{2,}", ln.strip()) if c]
        if len(cols) >= 2 and not cols[0].lower().startswith("position") and COMMERCIAL_RE.search(cols[-1]):
            outside.append({"who": who, "url": url, "kind": "position", "text": " · ".join(cols)})


def _senate_rows(page, part):
    import html as h
    m = re.search(r"<h3[^>]*>\s*" + re.escape(part) + r"(.*?)(?=<h3|$)", page, re.S)
    if not m:
        return []
    rows = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", m.group(1), re.S):
        cells = [re.sub(r"\s+", " ", h.unescape(re.sub(r"<[^>]+>", " ", c))).strip() for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
        cells = [c for c in cells if c]
        if cells:
            rows.append(cells)
    return rows


def _senate_outside(who, url, page, outside):
    for cells in _senate_rows(page, "Part 1."):
        outside.append({"who": who, "url": url, "kind": "honoraria", "text": " · ".join(cells[1:])})
    for cells in _senate_rows(page, "Part 2."):
        blob = " ".join(cells)
        if OUTSIDE_RE.search(blob) and not NOT_OUTSIDE_RE.search(blob):
            outside.append({"who": who, "url": url, "kind": "income", "text": " · ".join(cells[1:])})
    for cells in _senate_rows(page, "Part 8."):
        if COMMERCIAL_RE.search(" ".join(cells)):
            outside.append({"who": who, "url": url, "kind": "position", "text": " · ".join(cells[1:])})
    for cells in _senate_rows(page, "Part 9."):
        outside.append({"who": who, "url": url, "kind": "agreement", "text": " · ".join(cells[1:])})


ALL_TRADES = []  # every non-amended trade parsed this run, for trade_awards.run


def _check_trade(t, who, filed, url, events, late, timed, jur=None, comms=(), oversight=None):
    if t.get("status", "New") != "Amended":
        ALL_TRADES.append({"who": who, "t": t, "filed": filed, "url": url, "comms": comms, "jur": jur})
    if jur is not None and comms and oversight is not None:
        secs = jur.sectors_for_committees(comms)
        hit = jur.trade_sector(t, list(secs))
        if hit:
            oversight.append({"who": who, "t": t, "url": url, "sector": hit, "committee": jur.committee_name(secs[hit])})
    lag = trades.days_between(t.get("tx_date"), filed)
    if lag is not None and lag > LATE_DAYS and t.get("status", "New") == "New":
        late.append({"who": who, "t": t, "filed": filed, "lag": lag, "url": url})
    if (t.get("amount_floor") or 0) < TIMED_MIN_AMOUNT:
        return
    for h in trades.event_hits(t, events):
        if h["days_before_event"] < 0:  # trade came after the event; it can't have been a tip-off
            continue
        timed.append({"who": who, "t": t, "hit": h, "url": url})


def _diff(who, what, old_url, new_url, old_text, new_text):
    added, removed, n_add, n_rem = trades.text_diff(old_text, new_text)
    return {"who": who, "what": what, "old_url": old_url, "new_url": new_url,
            "added": added, "removed": removed, "n_add": n_add, "n_rem": n_rem,
            "unreadable": not old_text.strip() or not new_text.strip()}


def _build(late, timed, diffs, notes, errors, history=None, oversight=(), outside=(), jur=None):
    blocks, paras = [], []
    if late:
        late.sort(key=lambda x: -x["lag"])
        blocks.append({"type": "subhead", "text": f"Late disclosures: trades reported more than {LATE_DAYS} days after they happened"})
        blocks.append({"type": "table", "headers": ["Filer", "Asset", "Trade", "Traded", "Disclosed", "Days", "Amount"],
                       "rows": [[f"<strong>{esc(x['who'])}</strong>", link(x["t"]["asset"][:70], x["url"]), esc(x["t"]["type"]),
                                 mdY(x["t"]["tx_date"]), mdY(x["filed"]), str(x["lag"]), esc(x["t"]["amount"])]
                                for x in late]})
        per_filing = {}
        for x in late:
            per_filing.setdefault(x["url"], x)
        blocks.append({"type": "list", "items": [
            f"<strong>{esc(x['who'])}</strong>: {sum(1 for y in late if y['url'] == u)} late trade(s) in {link('this filing', u)}.{(history or {}).get(u, '')}"
            for u, x in per_filing.items()]})
        filers = sorted({x["who"] for x in late})
        paras.append(f"{len(late)} trade{'s' if len(late) != 1 else ''} disclosed past the STOCK Act's {LATE_DAYS}-day limit, by {', '.join(filers[:6])}{' and others' if len(filers) > 6 else ''}.")
    if timed:
        # One line per trade (not per trade x event); list every event it preceded, nearest first.
        grouped = {}
        for x in timed:
            t = x["t"]
            grouped.setdefault((x["who"], x["url"], t["asset"], t["type"], t["tx_date"], t["amount"]), []).append(x)
        timed = [g[0] | {"hits": sorted({(y["hit"]["event"], y["hit"]["event_date"]): y["hit"] for y in g}.values(), key=lambda h: h["days_before_event"])} for g in grouped.values()]
        blocks.append({"type": "subhead", "text": "Trades just before market-moving events"})

        def _events(hits):
            # "3 days after A (date); 3 days before B (date); ..." -> one line, every event named once
            return "; ".join(trades.timing_phrase(h) for h in hits)
        blocks.append({"type": "list", "items": [
            f"<strong>{esc(x['who'])}</strong>: {esc(x['t']['type'].lower())} of {link(x['t']['asset'][:80], x['url'])} "
            f"({esc(x['t']['amount'])}) on {mdY(x['t']['tx_date'])}, {len(x['hits'])} event{'s' if len(x['hits']) != 1 else ''} nearby: {_events(x['hits'])}."
            if len(x["hits"]) > 1 else
            f"<strong>{esc(x['who'])}</strong>: {esc(x['t']['type'].lower())} of {link(x['t']['asset'][:80], x['url'])} "
            f"({esc(x['t']['amount'])}) on {mdY(x['t']['tx_date'])}, {esc(trades.timing_phrase(x['hits'][0]))}."
            for x in timed]})
        paras.append(f"{len(timed)} disclosed trade{'s' if len(timed) != 1 else ''} of $15,001 or more landed shortly before an event on the watch calendar.")
    SECTOR_NAMES = {"ai_chips": "AI/chip", "oil_gas": "oil and gas", "nuclear_uranium": "nuclear/uranium", "steel_aluminum": "steel/aluminum",
                    "health_insurers": "health-insurer", "insurers_utilities": "utility/insurer", "critical_minerals": "critical-minerals",
                    "prediction_markets": "prediction-market", "clean_energy": "clean-energy"}
    oversight = sorted(oversight, key=lambda x: -(x["t"].get("amount_floor") or 0))
    outside = list({(x["who"], x["kind"], x["text"]): x for x in outside}.values())
    if oversight:
        blocks.append({"type": "subhead", "text": "Trades in their own committee's jurisdiction"})
        blocks.append({"type": "list", "items": [
            f"<strong>{esc(x['who'])}</strong> ({esc(x['committee'])}): {esc(x['t']['type'].lower())} of "
            f"{link(x['t']['asset'][:80], x['url'])} ({esc(x['t']['amount'])}) on {mdY(x['t']['tx_date'])} ({esc(SECTOR_NAMES.get(x['sector'], x['sector']))} sector)."
            for x in oversight]})
        paras.append(f"{len(oversight)} trade{'s' if len(oversight) != 1 else ''} in sectors overseen by the filer's own committee.")
    if outside:
        labels = {"income": "Earned income", "honoraria": "Honoraria", "position": "Outside position", "agreement": "Agreement/arrangement"}
        blocks.append({"type": "subhead", "text": "Outside income, positions and arrangements in new annual reports"})
        blocks.append({"type": "list", "items": [
            f"<strong>{esc(x['who'])}</strong>, {labels[x['kind']].lower()}: {esc(x['text'][:220])} ({link('report', x['url'])})"
            for x in outside]})
        paras.append(f"{len(outside)} outside-income, position or future-employment entr{'ies' if len(outside) != 1 else 'y'} in newly filed annual reports.")
    for dfx in diffs:
        blocks.append({"type": "subhead", "text": f"What changed: {dfx['who']}, amended {dfx['what']}"})
        if dfx["unreadable"]:
            blocks.append({"type": "note", "text": f"One version is scanned/unreadable, so no line diff. Compare by hand: {link('original', dfx['old_url'])} vs. {link('amendment', dfx['new_url'])}."})
            continue
        items = [f"<strong>Added:</strong> {esc(a)}" for a in dfx["added"]] + [f"<strong>Removed:</strong> {esc(r)}" for r in dfx["removed"]]
        if items:
            blocks.append({"type": "list", "items": items})
        blocks.append({"type": "note", "text": f"{dfx['n_add']} line(s) added, {dfx['n_rem']} removed{' (first 15 of each shown)' if max(dfx['n_add'], dfx['n_rem']) > 15 else ''}. "
                                               f"{link('Original', dfx['old_url'])} · {link('Amendment', dfx['new_url'])}. Line-level text diff: a moved or reformatted line can show up as both removed and added."})
    if diffs:
        paras.append(f"{len(diffs)} amended annual report{'s' if len(diffs) != 1 else ''}, with what changed spelled out below.")
    if notes:
        blocks.append({"type": "subhead", "text": "Not checked"})
        blocks.append({"type": "list", "items": notes})
    if errors:
        blocks.append({"type": "note", "text": "Partial run: " + esc(" | ".join(errors))})
    has_news = bool(late or timed or diffs or oversight or outside)
    if not has_news:
        return [], "No late trades, event-timed trades or amended reports in this window." + (" Errors: " + "; ".join(errors) if errors else "")
    return [section("Disclosure Red Flags", "Late trades, trades in their own jurisdiction, outside income and amended reports", paras, blocks)], " ".join(paras)


if __name__ == "__main__":
    main()
