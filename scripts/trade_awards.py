"""Trades Near Contract Awards (added 2026-10-04): a member traded a company's
stock within 30 days of that company winning a $10M+ federal contract.

Input: every individual-stock trade parsed from the PTRs fetch_pfd_analysis.py
read this run (not just the flagged ones). For each issuer, one USAspending
search for base contract awards (Mod 0) to a recipient of the same name
(corporate suffixes stripped), $10M+, in the window around the filer's trades.
The recipient name must equal the issuer's name (suffixes stripped, and an
ampersand on one side only is a mismatch -- Crane & Co. is not Crane Co.),
so subsidiary contracts (Boeing Defense vs Boeing) are missed, not guessed at. Timing is circumstantial: it shows the overlap, not that anyone knew.

Cache: data/trade-award-cache.json (7-day TTL).
"""
import concurrent.futures as cf
import json
import os
import time
from datetime import date, timedelta

import trades as trades_mod
from conflict_signals import company_key
from dc_common import days_ago_la, http_post_json, today_la
from dc_extras import esc, link, mdY, money, section, write_extra

CACHE_PATH = "data/trade-award-cache.json"
CACHE_TTL_DAYS = 7
MIN_AWARD = 10_000_000  # new (base) contract awards
MIN_MOD = 50_000_000  # modifications/option exercises: higher bar, big contractors get $10M+ mods every month
WINDOW_DAYS = 30
MAX_ISSUERS = 200
BUDGET_SEC = 420
STOCK_TYPES = {"ST", "Stock", "Stock (Publicly Traded)"}


def _load_cache():
    try:
        with open(CACHE_PATH) as f:
            c = json.load(f)
    except (FileNotFoundError, ValueError):
        return {}
    cutoff = days_ago_la(CACHE_TTL_DAYS)
    return {k: v for k, v in c.items() if v.get("fetched", "") >= cutoff}


def _search(core, start, end):
    awards, page = [], 1
    while page <= 3:
        payload = {
            "filters": {"award_type_codes": ["A", "B", "C", "D"], "recipient_search_text": [core],
                        "time_period": [{"start_date": start, "end_date": end}],
                        "award_amounts": [{"lower_bound": MIN_AWARD}]},
            "fields": ["Award ID", "Recipient Name", "Transaction Amount", "Awarding Agency", "Awarding Sub Agency",
                       "Transaction Description", "Action Date", "Mod", "generated_internal_id"],
            "page": page, "limit": 100, "sort": "Transaction Amount", "order": "desc",
        }
        status, data = http_post_json("https://api.usaspending.gov/api/v2/search/spending_by_transaction/", payload, timeout=40)
        if status >= 400 or not isinstance(data, dict):
            raise RuntimeError(f"HTTP {status}")
        for r in data.get("results", []):
            is_mod = str(r.get("Mod") or "0") not in ("0", "")
            if (r.get("Transaction Amount") or 0) < (MIN_MOD if is_mod else MIN_AWARD):
                continue  # award_amounts filters on the whole award; the action itself must clear the bar
            gid = r.get("generated_internal_id") or r.get("Award ID")
            awards.append({"recipient": r.get("Recipient Name") or "", "amount": r.get("Transaction Amount") or 0,
                           "agency": r.get("Awarding Agency") or "", "sub_agency": r.get("Awarding Sub Agency") or "",
                           "date": r.get("Action Date"), "mod": is_mod, "description": (r.get("Transaction Description") or "")[:200],
                           "url": f"https://www.usaspending.gov/award/{gid}" if gid else ""})
        if not (data.get("page_metadata") or {}).get("hasNext"):
            break
        page += 1
    return awards


def _name_matches(core, recipient, asset):
    rk = company_key(recipient)
    if rk != core:
        return False
    # "Crane & Co., Inc." (currency paper) is not "Crane Company" (CR): an ampersand the
    # stock's own name lacks means a different firm that company_key() flattened to the same core.
    return ("&" in recipient) == ("&" in asset)


def run(items, signals):
    """items: [{who, t, filed, url, comms, jur}] -> writes extras/conflicts/trade_awards.json."""
    by_issuer = {}
    for it in items:
        t = it["t"]
        if t.get("asset_type") not in STOCK_TYPES or not t.get("tx_date"):
            continue
        core = company_key(t["asset"])
        if len(core) < 4:
            continue
        by_issuer.setdefault(core, []).append(it)
    if not by_issuer:
        write_extra("conflicts", "trade_awards", [], "Trades near awards: no individual-stock trades to check.")
        return
    issuers = sorted(by_issuer, key=lambda c: -len(by_issuer[c]))[:MAX_ISSUERS]
    cache = _load_cache()
    today = today_la()
    deadline = time.time() + BUDGET_SEC
    errors = []

    def fetch(core):
        txs = sorted(i["t"]["tx_date"] for i in by_issuer[core])
        start = date.fromisoformat(txs[0]) - timedelta(days=WINDOW_DAYS)
        end = min(date.fromisoformat(txs[-1]) + timedelta(days=WINDOW_DAYS), date.fromisoformat(today))
        ck = f"{core}|{start}|{end}"
        if ck in cache:
            return core, cache[ck]["awards"]
        if time.time() > deadline:
            return core, None
        time.sleep(0.2)
        try:
            awards = _search(core, str(start), str(end))
        except Exception as e:  # noqa: BLE001
            errors.append(f"{core}: {e}")
            return core, None
        cache[ck] = {"fetched": today, "awards": awards}
        return core, awards

    awards_by_issuer, skipped = {}, 0
    with cf.ThreadPoolExecutor(max_workers=4) as ex:
        for core, awards in ex.map(fetch, issuers):
            if awards is None:
                skipped += 1
            else:
                awards_by_issuer[core] = [a for a in awards if _name_matches(core, a["recipient"], by_issuer[core][0]["t"]["asset"])]
    if not os.environ.get("DC_NO_SAVE"):
        with open(CACHE_PATH, "w") as f:
            json.dump(cache, f)

    hits = {}
    for core, awards in awards_by_issuer.items():
        for it in by_issuer[core]:
            t = it["t"]
            for a in awards:
                gap = trades_mod.days_between(t["tx_date"], a["date"])  # + = award came after the trade
                if gap is None or abs(gap) > WINDOW_DAYS:
                    continue
                key = (it["who"], core, a["url"])
                h = hits.setdefault(key, {"who": it["who"], "core": core, "award": a, "trades": [], "overlap": None})
                if not any(x["t"] is t for x in h["trades"]):
                    h["trades"].append({"t": t, "gap": gap, "url": it["url"]})
                jur = it.get("jur")
                if jur and it.get("comms") and h["overlap"] is None:
                    agency_secs = set(jur.sectors_for_agency(a["agency"] + " " + a["sub_agency"]))
                    own = jur.sectors_for_committees(it["comms"])
                    shared = agency_secs & set(own)
                    if shared:
                        h["overlap"] = jur.committee_name(own[sorted(shared)[0]])
    # One row per filer x company: its three largest nearby actions, the rest counted.
    groups = {}
    for h in hits.values():
        g = groups.setdefault((h["who"], h["core"]), {"who": h["who"], "core": h["core"], "actions": [], "trades": {}, "overlap": None})
        g["actions"].append(h["award"])
        for x in h["trades"]:
            g["trades"][(x["t"]["tx_date"], x["t"]["type"], x["t"]["amount"])] = x
        g["overlap"] = g["overlap"] or h["overlap"]
    found = []
    for g in groups.values():
        g["actions"].sort(key=lambda a: -a["amount"])
        g["trades"] = sorted(g["trades"].values(), key=lambda x: x["t"]["tx_date"])
        g["before"] = any(trades_mod.days_between(x["t"]["tx_date"], a["date"]) >= 0 for x in g["trades"] for a in g["actions"])
        found.append(g)
    found.sort(key=lambda g: (g["overlap"] is None, not g["before"], -g["actions"][0]["amount"]))

    for g in found:
        top = g["actions"][0]
        for x in g["trades"]:
            t = x["t"]
            signals.add(f"trade_award|{g['who']}|{g['core']}|{t['tx_date']}|{t['type']}", "trade_award", g["who"], t["tx_date"],
                        f"{t['type']} of {t['asset'][:60]} ({t['amount']}) within {WINDOW_DAYS} days of {len(g['actions'])} contract action(s) for "
                        f"{top['recipient']}, largest {money(top['amount'])} from {top['sub_agency'] or top['agency']}", top["url"], company=g["core"])

    notes = [f"Checked {len(awards_by_issuer)} of {len(by_issuer)} issuers from this run's trades: new contract awards of $10M+ and "
             f"modifications of $50M+ within {WINDOW_DAYS} days of a trade, to a recipient whose name equals the issuer's. "
             "Subsidiaries (Boeing Defense, GDIT) and renamed parents are missed; timing alone does not show anyone acted on non-public information."]
    if skipped:
        notes.append(f"{skipped} issuer(s) were not checked (time budget or USAspending errors).")
    if errors:
        notes.append("Errors: " + esc("; ".join(errors[:3])))
    if not found:
        write_extra("conflicts", "trade_awards", [], f"Trades near awards: none among {len(by_issuer)} issuers checked.")
        return
    rows = []
    for g in found:
        trade_cell = "<br>".join(f"{esc(x['t']['type'])} {mdY(x['t']['tx_date'])}, {esc(x['t']['amount'])} ({link('filing', x['url'])})" for x in g["trades"])
        acts = []
        for a in g["actions"][:3]:
            gap = trades_mod.days_between(g["trades"][0]["t"]["tx_date"], a["date"])
            when = f"{gap}d after the first trade" if gap > 0 else "same day as the first trade" if gap == 0 else f"{-gap}d before the first trade"
            acts.append(f"{link(a['recipient'].title(), a['url'])}: {money(a['amount'])}{' (modification)' if a.get('mod') else ''}, "
                        f"{esc(a['sub_agency'] or a['agency'])}, {mdY(a['date'])} ({when})<br><small>{esc(a['description'][:100])}</small>")
        more = f"<br><small>+ {len(g['actions']) - 3} more actions in the window</small>" if len(g["actions"]) > 3 else ""
        rows.append([f"<strong>{esc(g['who'])}</strong>" + (f"<br><small>on {esc(g['overlap'])}, which oversees this area</small>" if g["overlap"] else ""),
                     f"{esc(g['trades'][0]['t']['asset'][:50])}<br>{trade_cell}"
                     + ("<br><small>matched by name only; confirm it is the same company</small>" if " " not in g["core"] else ""),
                     "<br>".join(acts) + more])
    blocks = [{"type": "table", "headers": ["Filer", "Trade", "Contract actions near it"], "rows": rows},
              {"type": "note", "text": " ".join(notes)}]
    sec = section("Trades Near Contract Awards",
                  f"{len(found)} member{'s' if len(found) != 1 else ''} traded a company's stock within {WINDOW_DAYS} days of a major federal contract action for it",
                  [f"{sum(1 for g in found if g['before'])} of these had a trade before at least one of the actions; "
                   f"{sum(1 for g in found if g['overlap'])} involve a filer on a committee that oversees the awarding agency's area."], blocks)
    top = found[0]
    write_extra("conflicts", "trade_awards", [sec],
                f"Trades near awards: {len(found)} hit(s). Top: {top['who']} / {top['actions'][0]['recipient']} {money(top['actions'][0]['amount'])}.")
