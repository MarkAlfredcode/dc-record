#!/usr/bin/env python3
"""Lobbying Clients Winning Contracts -- joins this edition's new USAspending
awards (data/usaspending_candidates.json) to the Lobbying Disclosure Act
("Private Prisons Spend Millions on Lobbying, Get Billions in Immigration
Detention Contracts"). Added 2026-10-04.

For the largest new awards, look up each recipient as an LDA *client* for the
current filing year, confirm the name match locally (lda.gov's client_name
search is loose), and report year-to-date lobbying spend, issue areas and
firms next to the award. Limitation, stated in the section: matching is by
recipient name, so a contractor lobbying through a parent/subsidiary under a
different name is missed.

lda.gov's anonymous limit is ~15 req/min, so lookups are throttled and
cached 30 days (data/lobby-contracts-cache.json, positives and negatives).
Dedupe: data/lobby-contracts-seen.json (award_id).

Output: extras/feds/lobby_contracts.json
"""
import json
import os
import re
import sys
import time
from urllib.parse import urlencode

from dc_common import http_get, today_la
from dc_extras import SeenManifest, esc, link, load_json, mdY, money, norm, section, write_extra
from dc_common import days_ago_la, save_manifest

API = "https://lda.gov/api/v1/filings/"
THROTTLE = 4.3
MIN_AWARD = 1_000_000
MAX_RECIPIENTS = int(os.environ.get("DC_LOBBY_MAX_RECIPIENTS") or 40)
BUDGET_SEC = int(os.environ.get("DC_LOBBY_BUDGET_SEC") or 420)
CACHE_PATH = "data/lobby-contracts-cache.json"
CACHE_DAYS = 30
SUFFIX = {"inc", "incorporated", "llc", "llp", "lp", "ltd", "corp", "corporation", "company", "co", "the", "plc", "pllc", "pc", "of", "and"}
_last = [0.0]
QUARTERS = ("first_quarter", "second_quarter", "third_quarter", "fourth_quarter")


def lda(params):
    for _ in range(3):
        wait = THROTTLE - (time.time() - _last[0])
        if wait > 0:
            time.sleep(wait)
        _last[0] = time.time()
        status, body = http_get(f"{API}?{urlencode(params)}", headers={"User-Agent": "dc-record-bot"}, timeout=60)
        if status == 429:
            time.sleep(61)
            continue
        if status >= 400:
            raise RuntimeError(f"lda.gov HTTP {status}")
        return json.loads(body)
    raise RuntimeError("lda.gov rate limit")


def core(name):
    toks = [t for t in norm(name).split() if t not in SUFFIX]
    return " ".join(toks)


def names_match(recipient, client):
    a, b = core(recipient), core(client)
    if not a or not b:
        return False
    if a == b:
        return True
    ta, tb = a.split(), b.split()
    short, long_ = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    # "BOEING" vs "BOEING CO" handled by suffix strip; allow a multi-word name
    # to be the leading words of the other ("GEO GROUP" / "GEO GROUP SERVICES")
    return len(short) >= 2 and long_[:len(short)] == short


def amount(f):
    v = f.get("income") or f.get("expenses")
    try:
        return float(v) if v is not None else 0.0
    except ValueError:
        return 0.0


def lookup(recipient, year):
    """YTD lobbying for `recipient` as an LDA client. Returns dict or None."""
    d = lda({"client_name": recipient, "filing_year": year, "page_size": 25})
    fs = [f for f in d.get("results", []) if names_match(recipient, (f.get("client") or {}).get("name") or "")]
    if not fs:
        return None
    name = fs[0]["client"]["name"]
    per_period, issues, firms = {}, {}, set()
    for f in fs:
        if f.get("filing_period") in QUARTERS and (f.get("filing_type") or "").startswith(("Q", "1A", "2A", "3A", "4A")):
            per_period[f["filing_period"]] = max(per_period.get(f["filing_period"], 0), amount(f))
        firms.add((f.get("registrant") or {}).get("name") or "")
        for a in f.get("lobbying_activities") or []:
            iss = a.get("general_issue_code_display")
            if iss:
                issues[iss] = issues.get(iss, 0) + 1
    # in-house filers report expenses, outside firms report income; per_period
    # keeps the max per quarter so a client's several firms are NOT summed --
    # state it as "at least"
    return {"client": name, "ytd": sum(per_period.values()), "quarters": len(per_period),
            "issues": [k for k, _ in sorted(issues.items(), key=lambda kv: -kv[1])[:4]],
            "firms": sorted(x for x in firms if x)[:3], "n_filings": len(fs),
            "url": fs[0].get("filing_document_url")}


def main():
    cand = load_json("data/usaspending_candidates.json", {}) or {}
    awards = [a for a in cand.get("new_awards", []) if (a.get("transaction_amount") or 0) >= MIN_AWARD and a.get("recipient")]
    seen = SeenManifest("data/lobby-contracts-seen.json", keep_days=30)
    awards = [a for a in awards if a["award_id"] not in seen]
    if not awards:
        write_extra("feds", "lobby_contracts", [], "No new $1M+ awards to check against lobbying clients.")
        return

    by_rec = {}
    for a in awards:
        by_rec.setdefault(core(a["recipient"]), []).append(a)
    order = sorted(by_rec, key=lambda k: -max(x["transaction_amount"] for x in by_rec[k]))[:MAX_RECIPIENTS]

    cache = load_json(CACHE_PATH, {}) or {}
    cutoff = days_ago_la(CACHE_DAYS)
    cache = {k: v for k, v in cache.items() if v.get("checked", "") >= cutoff}
    year = int(today_la()[:4])
    started, errors, checked = time.time(), [], 0
    for key in order:
        if key in cache:
            continue
        if time.time() - started > BUDGET_SEC:
            errors.append(f"time budget hit after {checked} lookups; remaining recipients checked next run")
            break
        rec = by_rec[key][0]["recipient"]
        try:
            res = lookup(rec, year)
            cache[key] = {"checked": today_la(), "result": res}
            checked += 1
        except Exception as e:  # noqa: BLE001
            errors.append(f"{rec}: {e}")
    if not os.environ.get("DC_NO_SAVE"):
        save_manifest(CACHE_PATH, cache)

    matched = []
    for key in order:
        res = (cache.get(key) or {}).get("result")
        if res:
            matched.append((key, res))
    if not matched:
        write_extra("feds", "lobby_contracts", [],
                    f"Checked {len(order)} large new awardees against LDA clients; none are registered lobbying clients."
                    + (" Errors: " + "; ".join(errors[:3]) if errors else ""))
        return

    matched.sort(key=lambda m: -max(x["transaction_amount"] for x in by_rec[m[0]]))
    items = []
    for key, res in matched:
        spend = (f"{money(res['ytd'])} in lobbying reported so far in {year} ({res['quarters']} quarter{'s' if res['quarters'] != 1 else ''})"
                 if res["ytd"] else f"a registered lobbying client in {year}, no quarterly spend reported yet")
        line = (f"<strong>{esc(by_rec[key][0]['recipient'].title())}</strong>: {spend}; issues: {esc(', '.join(res['issues']) or 'n/a')}; "
                f"firms: {esc(', '.join(res['firms']) or 'in-house')}. {link('latest filing', res['url'])}. New awards: ")
        aw = []
        for a in sorted(by_rec[key], key=lambda x: -x["transaction_amount"])[:3]:
            desc = (a.get("raw_description") or a.get("product_or_service_description") or "").strip().rstrip(".").capitalize()
            aw.append(f"{money(a['transaction_amount'])} from {esc(a.get('awarding_sub_agency') or a.get('awarding_agency'))} "
                      f"({mdY(a.get('action_date') or '')}, {esc(desc[:140])}) {link('award', a.get('url'))}")
        more = len(by_rec[key]) - 3
        items.append(line + "; ".join(aw) + (f"; plus {more} more" if more > 0 else "") + ".")
    total = sum(max(x["transaction_amount"] for x in by_rec[k]) for k, _ in matched)
    paras = [f"{len(matched)} of {len(order)} large new awardees checked are also registered federal lobbying clients, collectively landing {money(total)} or more in new awards."]
    blocks = [{"type": "list", "items": items[:20]},
              {"type": "note", "text": "Awards of $1M+ from this edition's USAspending pull, matched by company name to Lobbying Disclosure Act clients for the current year. "
                                       "Spend is the largest report per quarter (a client with several firms may have spent more). "
                                       "Contractors lobbying under a parent or subsidiary name will not match."
                                       + (" Partial run: " + esc(" | ".join(errors[:2])) if errors else "")}]
    write_extra("feds", "lobby_contracts",
                [section("Lobbying × Contracts", "Companies that lobby Washington and just won federal money", paras, blocks)],
                paras[0] + " Top: " + ", ".join(res["client"] for _, res in matched[:4]) + ".")
    if not os.environ.get("DC_NO_SAVE"):
        for key, _ in matched:
            for a in by_rec[key]:
                seen.add(a["award_id"])
        seen.save()


if __name__ == "__main__":
    main()
