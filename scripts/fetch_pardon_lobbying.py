#!/usr/bin/env python3
"""Pardon Lobbying -- new LDA filings whose issue text is about pardons,
clemency or commutations ("Lobbyists Make Millions in Pursuit of Trump
Pardons"; the Boosie Badazz / Burkman-Wohl stories). Added 2026-10-04.

lda.gov's `filing_specific_lobbying_issues` search is loose token matching and
also hits long bill-summary text, so every hit is confirmed locally: the
pardon/clemency/commutation word must sit in a short activity description
(<=400 chars). Reports client, firm, named lobbyists + their disclosed
covered (prior government) positions, and fee/expense, and whether a
registration is the client's first-ever LDA filing.

lda.gov anonymous limit is ~15 req/min, so calls are throttled (same as
fetch_lda_trends.py). Dedupe: data/pardon-lobbying-seen.json.

Output: extras/officials/pardon_lobbying.json
"""
import json
import os
import re
import time
from urllib.parse import urlencode

from dc_common import http_get
from dc_extras import SeenManifest, esc, link, lookback, money, norm, section, write_extra

API = "https://lda.gov/api/v1/filings/"
THROTTLE = 4.3
TERMS = ("pardon", "clemency", "commutation")
CONFIRM_RE = re.compile(r"\b(pardon\w*|clemency|commutation|commute)\b", re.I)
MAX_DESC = 400
MAX_FOLLOWUPS = 8
_last = [0.0]


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


def amount(f):
    v = f.get("income") or f.get("expenses")
    try:
        return float(v) if v is not None else None
    except ValueError:
        return None


def pardon_activities(f):
    return [a for a in f.get("lobbying_activities") or []
            if a.get("description") and len(a["description"]) <= MAX_DESC and CONFIRM_RE.search(a["description"])]


def lobbyists(acts):
    seen = {}
    for a in acts:
        for l in a.get("lobbyists") or []:
            p = l.get("lobbyist") or {}
            name = " ".join(x for x in (p.get("first_name"), p.get("last_name")) if x).title()
            if name:
                seen.setdefault(name, (l.get("covered_position") or "").strip())
    return seen


def main():
    since = lookback(3)
    seen = SeenManifest("data/pardon-lobbying-seen.json", keep_days=60)
    hits, errors = {}, []
    for term in TERMS:
        page = 1
        while page <= 4:
            try:
                d = lda({"filing_specific_lobbying_issues": term, "filing_dt_posted_after": since, "page_size": 25, "page": page})
            except Exception as e:  # noqa: BLE001
                errors.append(f"'{term}': {e}")
                break
            for f in d.get("results", []):
                if f["filing_uuid"] not in seen and pardon_activities(f):
                    hits[f["filing_uuid"]] = f
            if not d.get("next"):
                break
            page += 1

    rows, followups = [], 0
    for uuid, f in sorted(hits.items(), key=lambda kv: -(amount(kv[1]) or 0)):
        acts = pardon_activities(f)
        c = f.get("client") or {}
        row = {"uuid": uuid, "client": c.get("name"), "firm": (f.get("registrant") or {}).get("name"), "amount": amount(f),
               "type": f.get("filing_type_display") or f.get("filing_type"), "new": f.get("filing_type") == "RR",
               "desc": acts[0]["description"].strip(), "lobbyists": lobbyists(acts), "url": f.get("filing_document_url"),
               "period": f.get("filing_period_display") or "", "year": f.get("filing_year")}
        if row["new"] and followups < MAX_FOLLOWUPS:
            followups += 1
            try:
                prior = lda({"client_name": c.get("name"), "page_size": 25})
                earlier = [p for p in prior.get("results", []) if norm((p.get("client") or {}).get("name")) == norm(c.get("name"))
                           and (p.get("dt_posted") or "") < since]
                row["first_ever"] = not earlier
            except Exception as e:  # noqa: BLE001
                errors.append(f"history '{c.get('name')}': {e}")
        rows.append(row)

    if not rows:
        write_extra("officials", "pardon_lobbying", [],
                    "No new pardon/clemency lobbying filings." + (" Errors: " + "; ".join(errors) if errors else ""))
        return

    # a registration is usually posted once per lobbyist-bundle; collapse
    # identical client+firm+description rows into one line
    merged = {}
    for r in rows:
        k = (norm(r["client"]), norm(r["firm"]), norm(r["desc"]))
        if k in merged:
            merged[k]["lobbyists"].update(r["lobbyists"])
            merged[k]["amount"] = max(merged[k]["amount"] or 0, r["amount"] or 0) or None
            merged[k]["first_ever"] = merged[k].get("first_ever") or r.get("first_ever")
        else:
            merged[k] = r
    rows = list(merged.values())

    items = []
    for r in rows:
        tag = "new registration" if r["new"] else f"{esc(r['type'])}"
        s = f"<strong>{esc(r['client'])}</strong> via {esc(r['firm'])} ({tag}"
        if r.get("first_ever"):
            s += ", <strong>first-ever LDA filing for this client</strong>"
        s += ")"
        if r["amount"]:
            s += f", {money(r['amount'])} reported"
        s += f". Issue as filed: “{esc(r['desc'])}”"
        if r["lobbyists"]:
            parts = [esc(n) + (f" (prior role: {esc(p)})" if p and p.lower() not in ("none", "n/a") else "") for n, p in list(r["lobbyists"].items())[:5]]
            s += ". Lobbyists: " + "; ".join(parts)
        items.append(s + f". {link('Filing', r['url'])}")
    news = sum(1 for r in rows if r["new"])
    paras = [f"{len(rows)} new LDA filing{'s' if len(rows) != 1 else ''} mention pardons or clemency"
             + (f", {news} of them new registrations" if news else "") + "."]
    blocks = [{"type": "list", "items": items[:25]},
              {"type": "note", "text": "Found by searching new Lobbying Disclosure Act filings for pardon/clemency/commutation in the specific-issue text; "
                                       "only short activity descriptions count (long bill summaries are ignored)."
                                       + (" Partial run: " + esc(" | ".join(errors[:3])) if errors else "")}]
    write_extra("officials", "pardon_lobbying",
                [section("Pardon Lobbying", "Lobbyists hired to win pardons and clemency", paras, blocks)],
                paras[0] + " Clients: " + ", ".join(r["client"] for r in rows[:5]) + ".")
    if not os.environ.get("DC_NO_SAVE"):
        for r in rows:
            seen.add(r["uuid"])
        for u in hits:
            seen.add(u)
        seen.save()


if __name__ == "__main__":
    main()
