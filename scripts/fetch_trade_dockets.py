#!/usr/bin/env python3
"""Section 232 Dockets -- new activity in Commerce's Section 232 tariff
dockets on regulations.gov (Bureau of Industry and Security + International
Trade Administration). Added 2026-10-12.

What it reports, newest first:
  - New comment windows: a 232 docket whose first document was posted in the
    lookback (a new investigation, inclusions window or derivatives proposal),
    with the comment deadline.
  - Inclusion requests: companies asking Commerce to extend 232 tariffs to
    more products. The requester and HTSUS codes come from the document record
    itself (BIS puts "HTSUS Classification(s): ..." in its comment field; ITA's
    auto-parts submissions name the company in the title).
  - Who's commenting: new public comments per docket, organizations named,
    individuals counted. In inclusion dockets each comment is tied to the
    request it answers (commentOnDocumentId), so "X asked BIS to deny Y's
    request" can be stated from the record.
  - Federal Register: proclamations, rules and notices that say "Section 232"
    from the White House, BIS, ITA or USTR.
  - Open now (AM only): 232 comment windows still accepting comments.

regulations.gov is an api.data.gov API, so the FEC_KEYS pool works (keys
rotate per request). Comments are posted in bulk days after submission, so the
lookback is wide (7 days) and data/trade-dockets-seen.json keeps anything
from repeating; postedDate is the agency's posting date, receiveDate when it
was filed.

Output: extras/feds/trade_dockets.json
"""
import html
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from urllib.parse import urlencode

from dc_common import http_get, today_la
from dc_extras import SeenManifest, esc, fec_key_cycle, link, lookback, mdY, run_mode, section, write_extra

API = "https://api.regulations.gov/v4/"
FR_API = "https://www.federalregister.gov/api/v1/documents.json"
AGENCIES = ("BIS", "ITA")
LOOKBACK_DAYS = 7
MAX_COMMENT_DETAILS = int(os.environ.get("DC_DOCKETS_MAX_DETAILS") or 150)
FR_AGENCIES = {"industry-and-security-bureau", "international-trade-administration", "trade-representative-office-of-united-states",
               "executive-office-of-the-president"}
DOCKET_URL = "https://www.regulations.gov/docket/{}"
DOC_URL = "https://www.regulations.gov/document/{}"
COMMENT_URL = "https://www.regulations.gov/comment/{}"

_keys = None


def rg(path, params):
    """regulations.gov GET; rotates keys, backs off on 429."""
    global _keys
    if _keys is None:
        _keys = fec_key_cycle()
    for attempt in range(4):
        url = f"{API}{path}?{urlencode({**params, 'api_key': next(_keys)})}"
        status, body = http_get(url, headers={"User-Agent": "dc-record-bot"}, timeout=45)
        if status == 429:
            time.sleep(3 * (attempt + 1))
            continue
        if status >= 400:
            raise RuntimeError(f"regulations.gov {path} -> HTTP {status}: {body[:200]!r}")
        return json.loads(body)
    raise RuntimeError(f"regulations.gov {path} rate-limited on every key tried")


def paged(path, params, max_pages=20):
    for page in range(1, max_pages + 1):
        d = rg(path, {**params, "page[size]": 250, "page[number]": page})
        yield from d.get("data", [])
        if not d.get("meta", {}).get("hasNextPage"):
            return


def topic(title):
    """Docket title -> the product it's about."""
    t = re.sub(r"\s+", " ", title or "").strip()
    t = re.sub(r"^(Notice of )?Request for Public Comments on (the )?", "", t, flags=re.I)
    t = re.sub(r"^Section 232 National Security Investigation of (Imports of )?", "", t, flags=re.I)
    t = re.sub(r"^Adoption and Procedures of the ", "", t, flags=re.I)
    return t[:1].upper() + t[1:] if t else title


def short_topic(title):
    t = topic(title)
    t = re.sub(r"^(Proposed )?Implementation of Duties on ", "", t, flags=re.I)
    t = re.sub(r"\s+Under Section 232$", "", t, flags=re.I)
    return t


def is_232(title):
    return bool(re.search(r"\b232\b", title or "")) or "Proclamation" in (title or "")


def is_inclusion_docket(title):
    return "inclusion" in (title or "").lower()


# Tokens that end in a period but don't end the name ("U.S. Steel", "Sara R. Bennett"). Corporate
# suffixes (Inc., Co., Ltd.) DO end the name, so they're not here.
ABBR = re.compile(r"(?:\b[A-Z]|U\.S|Jr|Sr|St|No|Mr|Ms|Mrs|Dr|Rep|Sen|Gov|Hon|Assn|Intl)\.$")
OFFICIAL = re.compile(r"House of Representatives|Senate|Senator|Governor|Embassy|Government of|Ministry|Mission of|"
                      r"State of|Commonwealth of|Congress", re.I)
ORG_WORDS = re.compile(r"Association|Institute|Council|Coalition|Federation|Alliance|Chamber|Union|Society|Corporation|"
                       r"\bInc\b|\bLLC\b|Company|Group|Industries|Manufactur|\bCorp\b|\bLtd\b|\bCo\b|Steel|Alumin|Copper", re.I)


def parse_title(title):
    """'U.S. House of Representatives. Rep. Barry Moore. 8/17/2026' -> ('U.S. House of Representatives', 'Rep. Barry Moore')
    'Individual. Rachel Maxwell. 8/20/2026' -> (None, 'Rachel Maxwell')"""
    t = re.sub(r"^(Public Comment|Other Submission|Comment)\s*#?\s*\d+\.\s*", "", title or "", flags=re.I).strip()
    t = re.sub(r"^Section 232 Inclusion Request\s*-\s*", "", t, flags=re.I)
    parts, cur = [], ""
    for tok in re.split(r"(?<=\.)\s+", t):
        cur = f"{cur} {tok}".strip() if cur else tok
        if not ABBR.search(cur):
            parts.append(cur.rstrip("."))
            cur = ""
    if cur:
        parts.append(cur.rstrip("."))
    parts = [re.sub(r"\s+(\d{1,2}/\d{1,2}/\d{2,4}|HTSUS\b.*)$", "", x.strip()) for x in parts]
    parts = [x for x in parts if x and not re.fullmatch(r"[\d/ ,A-Za-z]*\d{2,4}", x)]
    if not parts:
        return None, None
    org = parts[0]
    person = parts[1] if len(parts) > 1 else None
    if org.lower().startswith(("individual", "anonymous", "comment on")):
        return None, person
    return org, person


def who_from_title(title):
    return parse_title(title)[0]



def hts_list(raw, keep=6):
    """'7616.99.5175&amp;nbsp;' / long lists -> '7616.99.5175' / 'A, B, C, D, E, F and 7 more'"""
    codes = re.findall(r"\b\d{4}(?:\.\d{2}){1,2}(?:\.?\d{2,4})?", html.unescape(html.unescape(raw or "")))
    if not codes:
        return None
    return ", ".join(codes[:keep]) + (f" and {len(codes) - keep} more" if len(codes) > keep else "")


def clean_who(who):
    """ITA uploads are titled by filename: '2026.04.29 - Auto Parts Inclusions Request Comments_Autos Drive America'"""
    if not who:
        return who
    w = re.sub(r"^\d{4}[.\-]\d{2}[.\-]\d{2}\s*-\s*", "", who)
    if "_" in w:
        w = w.rsplit("_", 1)[-1]
    return w.strip(" -.") or who


FR_NOISE = re.compile(r"Antidumping|Countervailing|Less Than Fair Value|Circumvention|Administrative Review", re.I)


def fr_actions(since):
    q = {"conditions[term]": '"section 232"', "conditions[publication_date][gte]": since, "per_page": 50,
         "order": "newest", "fields[]": ["title", "publication_date", "type", "agencies", "html_url", "document_number",
                                         "abstract", "comments_close_on"]}
    status, body = http_get(f"{FR_API}?{urlencode(q, doseq=True)}", headers={"User-Agent": "dc-record-bot"}, timeout=45)
    if status >= 400:
        raise RuntimeError(f"federalregister.gov HTTP {status}")
    out = []
    for d in json.loads(body).get("results", []):
        slugs = {a.get("slug") for a in d.get("agencies") or []}
        if not slugs & FR_AGENCIES or FR_NOISE.search(d.get("title") or ""):
            continue
        out.append(d)
    return out


def main():
    since = lookback(LOOKBACK_DAYS)
    seen = SeenManifest("data/trade-dockets-seen.json", keep_days=60)
    errors = []

    # 1. 232 dockets at BIS/ITA touched recently (a posted comment bumps lastModifiedDate)
    dockets = {}
    for ag in AGENCIES:
        try:
            for d in rg("dockets", {"filter[agencyId]": ag, "filter[searchTerm]": "232", "sort": "-lastModifiedDate",
                                    "page[size]": 50})["data"]:
                a = d["attributes"]
                if is_232(a.get("title")):
                    dockets[d["id"]] = {"id": d["id"], "title": re.sub(r"\s+", " ", a.get("title") or ""),
                                        "modified": a.get("lastModifiedDate") or ""}
        except Exception as e:  # noqa: BLE001
            errors.append(f"dockets {ag}: {e}")
    active = {k: v for k, v in dockets.items() if v["modified"][:10] >= since}

    new_windows, requests, open_now = [], [], []
    comments_by_docket = defaultdict(list)
    for did, dk in sorted(active.items(), key=lambda kv: kv[1]["modified"], reverse=True):
        try:
            docs = list(paged("documents", {"filter[docketId]": did, "sort": "postedDate"}, max_pages=8))
        except Exception as e:  # noqa: BLE001
            errors.append(f"{did} documents: {e}")
            docs = []
        if docs and (docs[0]["attributes"].get("postedDate") or "")[:10] >= since and f"win:{did}" not in seen:
            a = docs[0]["attributes"]
            new_windows.append({"docket": dk, "doc_id": docs[0]["id"], "posted": a.get("postedDate", "")[:10],
                                "closes": (a.get("commentEndDate") or "")[:10]})
        for d in docs:
            a = d["attributes"]
            if (a.get("postedDate") or "")[:10] < since or f"doc:{d['id']}" in seen:
                continue
            if re.search(r"Inclusion Request|Other Submission", a.get("title") or "", re.I):
                rec = {"docket": dk, "id": d["id"], "posted": a["postedDate"][:10], "title": a.get("title"),
                       "who": clean_who(who_from_title(a.get("title"))), "hts": None, "closes": (a.get("commentEndDate") or "")[:10]}
                try:  # the record's own fields name the requester and HTS codes
                    det = rg(f"documents/{d['id']}", {})["data"]["attributes"]
                    rec["who"] = clean_who(det.get("organization")) or rec["who"]
                    rec["hts"] = hts_list(det.get("comment")) or hts_list(a.get("title"))
                except Exception as e:  # noqa: BLE001
                    errors.append(f"{d['id']}: {e}")
                requests.append(rec)
        if run_mode() == "am":
            for d in docs:
                a = d["attributes"]
                if a.get("openForComment") and a.get("documentType") in ("Notice", "Proposed Rule", "Other"):
                    open_now.append({"docket": dk, "doc_id": d["id"], "closes": (a.get("commentEndDate") or "")[:10]})
                    break
        try:
            for c in paged("comments", {"filter[docketId]": did, "filter[postedDate][ge]": since, "sort": "-postedDate"}):
                if f"com:{c['id']}" not in seen:
                    comments_by_docket[did].append(c)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{did} comments: {e}")

    # inclusion dockets: tie each new organizational comment to the request it answers
    request_names = {}
    responses = []
    details_left = MAX_COMMENT_DETAILS
    for did, cs in comments_by_docket.items():
        if not is_inclusion_docket(active[did]["title"]):
            continue
        for c in cs:
            who = who_from_title(c["attributes"].get("title"))
            if not who or details_left <= 0:
                continue
            details_left -= 1
            try:
                det = rg(f"comments/{c['id']}", {})["data"]["attributes"]
            except Exception as e:  # noqa: BLE001
                errors.append(f"{c['id']}: {e}")
                continue
            target = det.get("commentOnDocumentId")
            if target and target not in request_names:
                try:
                    t = rg(f"documents/{target}", {})["data"]["attributes"]
                    nm = clean_who(t.get("organization") or who_from_title(t.get("title")))
                    if not nm or re.match(r"^(Other )?Submission\s*\d+$", nm, re.I):
                        raise ValueError("unnamed request")
                    request_names[target] = (nm,
                                             hts_list(t.get("comment"), keep=4) or hts_list(t.get("title"), keep=4))
                except Exception:  # noqa: BLE001
                    request_names[target] = None
            text = (det.get("comment") or "").lower()
            stance = ("opposes" if re.search(r"\bden(y|ial)|oppos|reject", text) else
                      "supports" if re.search(r"\bsupport|grant|approv", text) else "comments on")
            responses.append({"who": clean_who(det.get("organization") or who), "stance": stance, "target": target,
                              "id": c["id"], "received": (det.get("receiveDate") or "")[:10], "docket": active[did]})

    try:
        fr = [d for d in fr_actions(since) if f"fr:{d['document_number']}" not in seen]
    except Exception as e:  # noqa: BLE001
        errors.append(f"Federal Register: {e}")
        fr = []

    # ---- build the section
    blocks, items_count = [], 0
    if new_windows:
        blocks.append({"type": "subhead", "text": "New comment windows"})
        blocks.append({"type": "list", "items": [
            f"<strong>{esc(topic(w['docket']['title']))}</strong>: Commerce opened docket {link(w['docket']['id'], DOCKET_URL.format(w['docket']['id']))} on {mdY(w['posted'])}"
            + (f"; comments due {mdY(w['closes'])}" if w["closes"] else "") + "." for w in new_windows]})
        items_count += len(new_windows)
    if fr:
        blocks.append({"type": "subhead", "text": "Federal Register"})
        blocks.append({"type": "list", "items": [
            f"{esc(d['type'])}, {mdY(d['publication_date'])}: {link(d['title'], d['html_url'])}"
            + (f" Comments close {mdY(d['comments_close_on'])}." if d.get("comments_close_on") else "") for d in fr[:10]]})
        items_count += len(fr)
    if requests:
        blocks.append({"type": "subhead", "text": f"New inclusion requests ({len(requests)})"})
        blocks.append({"type": "list", "items": [
            f"<strong>{esc(r['who'] or 'Unnamed requester')}</strong> asks Commerce to add "
            + (f"HTSUS {esc(r['hts'])}" if r["hts"] else "more products")
            + f" to the {esc(topic(r['docket']['title']).replace('Section 232 ', ''))} ({mdY(r['posted'])}"
            + (f"; comments due {mdY(r['closes'])}" if r["closes"] else "") + f"). {link('Request', DOC_URL.format(r['id']))}"
            for r in requests[:25]]})
        if len(requests) > 25:
            blocks.append({"type": "note", "text": f"Plus {len(requests) - 25} more in {link('the docket', DOCKET_URL.format(requests[25]['docket']['id']))}."})
        items_count += len(requests)
    if responses:
        named = [r for r in responses if r["target"]]
        blocks.append({"type": "subhead", "text": "Who's answering the inclusion requests"})
        blocks.append({"type": "list", "items": [
            f"<strong>{esc(r['who'])}</strong> {r['stance']} "
            + (f"{esc(request_names[r['target']][0])}'s request" + (f" (HTSUS {esc(request_names[r['target']][1])})" if request_names.get(r['target']) and request_names[r['target']][1] else "")
               if request_names.get(r["target"]) else "a request")
            + f" ({'filed ' + mdY(r['received']) if r['received'] else 'posted'}). {link('Comment', COMMENT_URL.format(r['id']))}"
            for r in named[:20]]})
        items_count += len(named)
    other_dockets = [(did, cs) for did, cs in comments_by_docket.items() if cs]
    if other_dockets:
        lines = []
        for did, cs in sorted(other_dockets, key=lambda kv: -len(kv[1])):
            parsed = [parse_title(c["attributes"].get("title")) for c in cs]
            officials = sorted({(p or o) for o, p in parsed if o and OFFICIAL.search(o)})
            orgs = Counter(o for o, _ in parsed if o and not OFFICIAL.search(o))
            indiv = sum(1 for o, _ in parsed if not o)
            ranked = sorted(orgs, key=lambda o: (not ORG_WORDS.search(o), o.lower()))
            names = ", ".join(esc(o) for o in ranked[:12])
            if officials:
                lines.append(f"<strong>{esc(topic(active[did]['title']))}</strong>, comments from officials: {esc('; '.join(officials))}. {link('Docket', DOCKET_URL.format(did))}")
            lines.append(f"<strong>{esc(topic(active[did]['title']))}</strong>: {len(cs)} new comment{'s' if len(cs) != 1 else ''} posted"
                         + (f", including {names}" + (f" and {len(orgs) - 12} more organizations" if len(orgs) > 12 else "") if orgs else "")
                         + (f"; {indiv} from individuals" if indiv else "") + f". {link('Docket', DOCKET_URL.format(did))}")
        blocks.append({"type": "subhead", "text": "New comments by docket"})
        blocks.append({"type": "list", "items": lines})
        items_count += sum(len(cs) for _, cs in other_dockets)
    if open_now:
        blocks.append({"type": "subhead", "text": "Still open for comment"})
        blocks.append({"type": "list", "items": [
            f"{esc(topic(o['docket']['title']))}: comments due {mdY(o['closes']) if o['closes'] else 'date not set'}. {link('Notice', DOC_URL.format(o['doc_id']))}"
            for o in sorted(open_now, key=lambda o: o["closes"] or "9")]})
    blocks.append({"type": "note", "text": "Commerce's Section 232 dockets at regulations.gov (Bureau of Industry and Security and International Trade Administration) and Federal Register documents citing Section 232. "
                                          "Comments are posted in batches, often weeks after they're filed; dates in parentheses are filing dates when the record has one. "
                                          "Stance is read from the comment's own text and should be checked against the attachment."
                                          + (" Partial run: " + esc(" | ".join(errors[:3])) if errors else "")})

    if not items_count:
        write_extra("feds", "trade_dockets", [], "No new activity in Commerce's Section 232 dockets." + (" Errors: " + "; ".join(errors[:3]) if errors else ""))
        return

    actions = [d for d in fr if d["type"] in ("Presidential Document", "Rule")]
    if new_windows:
        headline = f"Commerce opens a Section 232 comment window: {short_topic(new_windows[0]['docket']['title'])}"
    elif actions:
        headline = actions[0]["title"]
    elif requests:
        headline = f"{len(requests)} new requests to widen Section 232 tariffs to more products"
    elif fr and not other_dockets:
        headline = fr[0]["title"]
    else:
        did, cs = max(other_dockets, key=lambda kv: len(kv[1]))
        headline = f"{len(cs)} new comments in Commerce's Section 232 docket on {short_topic(active[did]['title'])}"
    parts = []
    if new_windows:
        parts.append(f"{len(new_windows)} new comment window{'s' if len(new_windows) != 1 else ''}")
    if fr:
        parts.append(f"{len(fr)} Federal Register document{'s' if len(fr) != 1 else ''}")
    if requests:
        parts.append(f"{len(requests)} inclusion request{'s' if len(requests) != 1 else ''}")
    n_com = sum(len(cs) for _, cs in other_dockets)
    if n_com:
        parts.append(f"{n_com} public comment{'s' if n_com != 1 else ''} across {len(other_dockets)} docket{'s' if len(other_dockets) != 1 else ''}")
    para = "New in Commerce's Section 232 tariff dockets since the last edition: " + ", ".join(parts) + "."
    write_extra("feds", "trade_dockets", [section("Section 232 Dockets", esc(headline), [para], blocks)], para + " Lead: " + headline + ".")

    if not os.environ.get("DC_NO_SAVE"):
        for w in new_windows:
            seen.add(f"win:{w['docket']['id']}")
        for r in requests:
            seen.add(f"doc:{r['id']}")
        for cs in comments_by_docket.values():
            for c in cs:
                seen.add(f"com:{c['id']}")
        for d in fr:
            seen.add(f"fr:{d['document_number']}")
        seen.save()


if __name__ == "__main__":
    main()
