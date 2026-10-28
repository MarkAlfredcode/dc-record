#!/usr/bin/env python3
"""Draft the FOIA requests Mark asked for in the tracker (added 2026-10-27).

Mark types a topic, contract, person or link into "Ask for a FOIA" on markalfred.news/foia (Suggested tab). This
script, a step in the Congress workflow (AM and PM), claims every waiting ask from /api/foia-asks, reads the link
he gave (USAspending awards and SAM.gov notices through their APIs, anything else as page text), and runs one
no-tools `claude -p` turn (prompt-foia-ask.txt) that picks the office holding the records and writes the request.
Each draft gets the standard letter (suggest_foias.letter) and goes back to the tracker as a suggestion
(edition 'ask'). Offices that only take online requests (State, DOE, Education, DHS and its components, Interior,
VA) are included; the tracker routes those to Suggested > Online. Nothing is emailed from here.

No asks waiting -> no model call. Never fails the run: every error is printed and the script exits 0; an ask
claimed by a run that dies is reclaimed by the next run an hour later.

Env: CLAUDE_CODE_OAUTH_TOKEN, FOIA_INGEST_TOKEN (required), FOIA_SITE, FOIA_MODEL (default sonnet),
     FOIA_DRY_RUN=1 (print the drafts; asks stay claimed until the next run reclaims them).
"""
import datetime as dt
import html
import json
import os
import re
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dc_common import http_get, http_get_json  # noqa: E402
from suggest_foias import ROOT, agency_named, letter, run_model  # noqa: E402

SITE = os.environ.get("FOIA_SITE", "https://markalfred.news")
FEDERAL = {"level": "Federal", "statute": "Freedom of Information Act (5 U.S.C. § 552)",
           "rule": {"days": 20, "kind": "bd", "text": "Federal FOIA: 20 business days (10 more in unusual circumstances)"}}
# Offices that refuse emailed requests. The address only routes the draft (the tracker's portalOnly() sends these
# domains to Suggested > Online, and foia-chrome files them on foia.gov by agency name); it is never emailed.
PORTAL = [("Department of State", None, "foiarequest@state.gov"),
          ("Department of Energy", None, "foia-central@hq.doe.gov"),
          ("Department of Education", None, "EDFOIAManager@ed.gov"),
          ("Department of Homeland Security, Privacy Office", None, "foia@hq.dhs.gov"),
          ("U.S. Immigration and Customs Enforcement", "Department of Homeland Security", "ice-foia@ice.dhs.gov"),
          ("U.S. Customs and Border Protection", "Department of Homeland Security", "cbpfoiapublicliaison@cbp.dhs.gov"),
          ("U.S. Citizenship and Immigration Services", "Department of Homeland Security", "foiapaquestions@uscis.dhs.gov"),
          ("Federal Emergency Management Agency", "Department of Homeland Security", "fema-foia@fema.dhs.gov"),
          ("Transportation Security Administration", "Department of Homeland Security", "foia@tsa.dhs.gov"),
          ("U.S. Secret Service", "Department of Homeland Security", "FOIA@usss.dhs.gov"),
          ("Department of the Interior, Office of the Secretary", None, "osfoia@ios.doi.gov"),
          ("Department of Veterans Affairs, VA Central Office", None, "foiahelp@va.gov"),
          ("Veterans Health Administration", "Department of Veterans Affairs", "vhafoiahelp@va.gov"),
          ("Veterans Benefits Administration", "Department of Veterans Affairs", "foia.vbaco@va.gov")]
_TAG = re.compile(r"<[^>]+>")


def api(path, payload=None):
    req = urllib.request.Request(SITE + path, method="POST" if payload is not None else "GET",
                                 data=json.dumps(payload).encode() if payload is not None else None,
                                 headers={"Authorization": "Bearer " + os.environ["FOIA_INGEST_TOKEN"],
                                          "Content-Type": "application/json", "User-Agent": "dc-record-foia/1"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def plain(s, n):
    return re.sub(r"\s+", " ", html.unescape(_TAG.sub(" ", s or ""))).strip()[:n]


def link_context(url):
    """What Mark's link says, as text the model can use. (text, problem)"""
    if not url:
        return "", None
    try:
        m = re.search(r"usaspending\.gov/award/([^/?#]+)", url)
        if m:
            a = http_get_json(f"https://api.usaspending.gov/api/v2/awards/{m.group(1)}/", timeout=40)
            keep = {k: a.get(k) for k in ("piid", "fain", "category", "type_description", "description", "total_obligation",
                                          "base_and_all_options_value", "date_signed", "parent_award", "subaward_count")}
            keep["recipient"] = (a.get("recipient") or {}).get("recipient_name")
            for side in ("awarding_agency", "funding_agency"):
                ag = a.get(side) or {}
                keep[side] = " / ".join(x for x in ((ag.get("toptier_agency") or {}).get("name"), (ag.get("subtier_agency") or {}).get("name"), ag.get("office_agency_name")) if x)
            keep["period"] = a.get("period_of_performance")
            ld = a.get("latest_transaction_contract_data") or {}
            keep.update({k: ld.get(k) for k in ("extent_competed_description", "solicitation_identifier", "other_than_full_and_open_description", "number_of_offers_received")})
            return "USAspending award: " + json.dumps(keep, default=str)[:5000], None
        m = re.search(r"sam\.gov/(?:opp|workspace/contract/opp)/([0-9a-f]{32})", url)
        if m:
            n = http_get_json(f"https://sam.gov/api/prod/opps/v2/opportunities/{m.group(1)}", timeout=40)
            d = n.get("data2") or n.get("data") or n
            desc = " ".join(plain(x.get("body"), 3000) for x in (n.get("description") or []) if isinstance(x, dict))
            org = d.get("organizationId")
            try:  # the notice only carries an org key; the federal hierarchy names the office and its department
                o = ((http_get_json(f"https://sam.gov/api/prod/federalorganizations/v1/organizations/{org}", timeout=30).get("_embedded") or [{}])[0].get("org") or {})
                org = (o.get("fullParentPathName") or o.get("name") or org)
            except Exception:  # noqa: BLE001
                pass
            keep = {"title": d.get("title"), "solicitation": d.get("solicitationNumber"), "type": (d.get("type") or {}).get("value") if isinstance(d.get("type"), dict) else d.get("type"),
                    "organization": org, "award": d.get("award")}
            return "SAM.gov notice: " + json.dumps(keep, default=str)[:2500] + " Description: " + desc[:3500], None
        status, body = http_get(url, headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"}, timeout=30)
        if status >= 400:
            return "", f"the link returned HTTP {status}"
        text = body.decode("utf-8", "replace")
        text = re.sub(r"(?is)<(script|style|nav|footer|header)[^>]*>.*?</\1>", " ", text)
        title = plain((re.search(r"(?is)<title[^>]*>(.*?)</title>", text) or [None, ""])[1], 200)
        out = plain(text, 6000)
        return (f"Page: {title}. {out}" if out else ""), (None if len(out) > 200 else "the link's page had almost no readable text")
    except Exception as e:  # noqa: BLE001
        return "", f"the link couldn't be read ({type(e).__name__})"


def main():
    if not os.environ.get("FOIA_INGEST_TOKEN"):
        print("foia-asks: FOIA_INGEST_TOKEN not set; skipping", file=sys.stderr)
        return
    asks = api("/api/foia-asks").get("asks") or []
    if not asks:
        print("foia-asks: none waiting", file=sys.stderr)
        return
    today = dt.datetime.now(dt.timezone(dt.timedelta(hours=-7))).date()
    with open(os.path.join(ROOT, "data", "foia_contacts.json")) as f:
        contacts = {str(c["id"]): c for c in json.load(f)}
    portal = {f"P{i}": {"agency": a, "parent": p, "email": e, **FEDERAL} for i, (a, p, e) in enumerate(PORTAL, 1)}
    with open(os.path.join(ROOT, "prompt-foia-ask.txt")) as f:
        prompt = f.read()

    problems = {}
    lines = [f"\n=== ASKS ({today}) ==="]
    for a in asks:
        ctx, prob = link_context(a.get("link"))
        if prob:
            problems[a["id"]] = prob
        lines.append(f"\n#{a['id']}: {a['text']}" + (f"\n  Link: {a['link']}" if a.get("link") else "") + (f"\n  Agency Mark named: {a['agency']}" if a.get("agency") else ""))
        if ctx:
            lines.append(f"  LINK CONTEXT: {ctx}")
        elif prob:
            lines.append(f"  LINK CONTEXT: none ({prob})")
    lines.append("\n=== FEDERAL FOIA OFFICES THAT TAKE EMAIL (id | office | parent) ===")
    lines += [f"{c['id']} | {c['agency']} | {c.get('parent') or ''}" for c in contacts.values()]
    lines.append("\n=== OFFICES THAT ONLY TAKE ONLINE REQUESTS (id | office | parent) ===")
    lines += [f"{k} | {c['agency']} | {c.get('parent') or ''}" for k, c in portal.items()]
    reply = run_model(prompt + "\n".join(lines))

    by_ask = {int(r.get("ask") or 0): r for r in reply.get("asks") or [] if str(r.get("ask") or "").isdigit()}
    results = []
    for a in asks:
        r = by_ask.get(a["id"]) or {}
        notes = [str(r.get("note") or "").strip()] if str(r.get("note") or "").strip() else []
        out = []
        for s in (r.get("suggestions") or [])[:3]:
            contact = portal.get(str(s.get("portal_id") or "").upper()) or contacts.get(str(s.get("contact_id") or ""))
            if not contact or any(not str(s.get(k) or "").strip() for k in ("subject", "records", "topic", "why")):
                print(f"foia-asks: dropped a malformed draft for #{a['id']}: {str(s)[:200]}", file=sys.stderr)
                continue
            s["records"] = re.sub(r"^\s*(?:I (?:hereby )?request the following records:?)\s*", "", str(s["records"]), flags=re.I)  # the letter already says it
            office = contact["agency"] + (f" ({contact['parent']})" if contact.get("parent") else "")
            hay = " ".join(str(s.get(k) or "") for k in ("holder", "subject", "records", "why")) + " " + (a.get("agency") or "")
            if not agency_named(office, hay):
                print(f"foia-asks: dropped #{a['id']} draft, {office} doesn't hold it (holder: {s.get('holder')!r})", file=sys.stderr)
                notes.append(f"A draft for {office} was dropped because the records look like they belong to {s.get('holder') or 'another office'}.")
                continue
            out.append({"run_date": today.isoformat(), "source_url": a.get("link") or None,
                        "item_title": str(s.get("story") or "").strip() or a["text"], "gets": str(s.get("gets") or "").strip(),
                        "agency": office, "email": contact["email"], "level": contact["level"], "statute": contact["statute"],
                        "rule_days": contact["rule"]["days"], "rule_kind": contact["rule"]["kind"], "rule_text": contact["rule"]["text"],
                        "unverified": bool(contact.get("unverified")), "subject": s["subject"].strip(), "why": s["why"].strip(),
                        "records": s["records"].strip(), "letter": letter(contact, s, today)})
        if a["id"] in problems and not out:
            notes.append(f"Note: {problems[a['id']]}.")
        if not r:
            notes.append("The drafting pass skipped this ask; add it again.")
        results.append({"id": a["id"], "status": "drafted" if out else "failed", "note": " ".join(notes)[:600], "suggestions": out})
        print(f"foia-asks: #{a['id']} -> {len(out)} draft(s){' | ' + ' '.join(notes)[:200] if notes else ''}", file=sys.stderr)
    if os.environ.get("FOIA_DRY_RUN"):
        print(json.dumps(results, indent=1))
        return
    print(f"foia-asks: posted {api('/api/foia-asks', {'results': results})}", file=sys.stderr)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001 -- asks must never break the edition
        print(f"foia-asks: skipped ({type(e).__name__}: {e})", file=sys.stderr)
    sys.exit(0)
