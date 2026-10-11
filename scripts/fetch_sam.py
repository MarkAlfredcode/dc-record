#!/usr/bin/env python3
"""Task 10 (SAM.gov) fetch for the Feds edition. Lists every notice of every
type that matters in the window, pulls each one's description (and award
block), and reads every attached file (sam_docs.py).

Keyless since 2026-10-10: everything comes from SAM.gov's own frontend API,
none of it from api.sam.gov. The SAM_API_KEY throttle self-extends 24h on any
call made while throttled (reference_sam_gov_api memory) and cut this
edition short -- on 2026-10-08 it stopped 320 of 1,320 solicitations short.
The GitHub secret stays in place, unused.

Notice types (frontend `notice_type` codes), each its own section of the
edition since 2026-10-10:
  u  Justification (J&A)            a  Award Notice
  s  Special Notice                 r  Sources Sought
  o  Solicitation                   k  Combined Synopsis/Solicitation
  p  Presolicitation
s/a/r were added after the edition missed an FBI sole-source J&A to UFC for
MMA training of special agents (posted 2026-09-30 as a Special Notice). Since
9/1 those types carried at least 82 J&A-style attachments and ~325 sole-
source titles. An s/a/r notice whose title reads as sole-source, or that has
a J&A-looking attachment, is also handed to the justification pipeline
(extraction of vendor/value/authority) with `source_type` saying how it was
posted.

Window: modifiedDate since yesterday 00:00 Pacific for active notices (an
amended notice resurfaces and goes through the same screens; data/sam-seen.json
dedupes by noticeId). Inactive notices are listed too -- some recent ones are
already inactive -- but SAM.gov's nightly archive job restamps ~500/type/day
of old notices' modifiedDate, so for inactive ones publishDate must also be
in the window. (The search can't sort by publishDate.)

Output: data/sam_candidates.json
  justifications: [row]   u notices + promoted s/a/r
  notices:        [row]   every s/a/r/o/k/p notice (promoted ones too)
"""
import html
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

import sam_docs
from dc_common import check_reachable, days_ago_la, http_get_json, today_la, write_candidates

OUT_PATH = "data/sam_candidates.json"
SEARCH = ("https://sam.gov/api/prod/sgs/v1/search/?index=opp&notice_type={t}&is_active={a}"
          "&sort=-modifiedDate&mode=search&size=500&page={p}")
NOTICE_DETAIL = "https://sam.gov/api/prod/opps/v2/opportunities/{}"
UI_LINK = "https://sam.gov/opp/{}/view"
MAX_PAGES = 40
TYPES = {"u": "Justification", "a": "Award Notice", "s": "Special Notice", "r": "Sources Sought",
         "o": "Solicitation", "k": "Combined Synopsis/Solicitation", "p": "Presolicitation"}
PROMOTABLE = ("s", "a", "r")
SOLE_SOURCE_TITLE = re.compile(
    r"sole[\s-]*source|single[\s-]*source|justification|\bj\s*&\s*a\b|\bjofoc\b|brand[\s-]*name|"
    r"only one (responsible )?source|limited[\s-]*source|non[\s-]*competitive|other than full|intent to award|"
    r"intent to sole|exception to fair opportunity", re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
# o/k/p descriptions are mostly NSN/quantity boilerplate but a few run 25K+;
# the attachments are read in full separately (sam_docs.py).
NOTICE_DESC_CAP = 800


def notice_id_from_ui_link(ui_link):
    m = re.search(r"/opp/([^/]+)/view", ui_link or "")
    return m.group(1) if m else None


def type_lookback_days():
    """DC_SAM_TYPE_LOOKBACK="a:7,s:7,r:7" widens the window for those types
    (a manual backfill); every other type keeps the normal 1 day."""
    out = {}
    for part in (os.environ.get("DC_SAM_TYPE_LOOKBACK") or "").split(","):
        t, _, n = part.strip().partition(":")
        if t in TYPES and n.strip().isdigit():
            out[t] = int(n)
    return out


def window_start_utc(days=1):
    start = datetime.combine(datetime.fromisoformat(days_ago_la(days)).date(), dtime(0),
                             tzinfo=ZoneInfo("America/Los_Angeles"))
    return start.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%S")


def list_type(t, since):
    """Every notice of type t in the window, active and inactive. Returns
    (rows, error)."""
    out, err = [], None
    for active in ("true", "false"):
        for page in range(MAX_PAGES):
            try:
                data = http_get_json(SEARCH.format(t=t, a=active, p=page), timeout=40)
            except Exception as e:  # noqa: BLE001
                err = f"{TYPES[t]} ({'active' if active == 'true' else 'inactive'}) page {page}: {e}"
                break
            rows = (data.get("_embedded") or {}).get("results") or []
            for r in rows:
                if (r.get("modifiedDate") or "") < since:
                    continue
                if active == "false" and (r.get("publishDate") or "") < since:
                    continue
                out.append(r)
            if not rows or (rows[-1].get("modifiedDate") or "") < since:
                break
        else:
            err = f"{TYPES[t]}: stopped at {MAX_PAGES} pages"
    seen, uniq = set(), []
    for r in out:
        if r.get("_id") not in seen:
            seen.add(r.get("_id"))
            uniq.append(r)
    return uniq, err


def fetch_detail(nid):
    """(description text, data2, award block) from the keyless notice detail."""
    data = http_get_json(NOTICE_DETAIL.format(nid), timeout=20)
    bodies = [d.get("body", "") for d in data.get("description", []) if isinstance(d, dict)]
    text = _WS_RE.sub(" ", html.unescape(_TAG_RE.sub(" ", " ".join(bodies)))).strip() or None
    d2 = data.get("data2") or {}
    return text, d2, d2.get("award") or {}


def agency_path(r):
    levels = sorted(r.get("organizationHierarchy") or [], key=lambda o: o.get("level") or 0)
    return ".".join(o.get("name") or "" for o in levels) or None


def to_row(r, detail):
    desc, d2, award = detail
    t = (r.get("type") or {}).get("code")
    naics = d2.get("naics") or []
    row = {
        "noticeId": r["_id"],
        "title": r.get("title"),
        "agency": agency_path(r),
        "solicitationNumber": r.get("solicitationNumber"),
        "postedDate": r.get("publishDate"),
        "modifiedDate": r.get("modifiedDate"),
        "isActive": r.get("isActive"),
        "isCanceled": r.get("isCanceled"),
        "naicsCode": (naics[0].get("code") or [None])[0] if naics and isinstance(naics[0], dict) else None,
        "classificationCode": d2.get("classificationCode"),
        "pointOfContact": d2.get("pointOfContact"),
        "uiLink": UI_LINK.format(r["_id"]),
        "noticeType": t,
        "noticeTypeLabel": TYPES.get(t, t),
        "description_text": desc,
    }
    awardee = award.get("awardee") or {}
    if t == "a" or award.get("amount") or awardee.get("name"):
        row["award"] = {"amount": award.get("amount"), "date": award.get("date"), "number": award.get("number"),
                        "awardee": awardee.get("name"), "uei": awardee.get("ueiSAM")}
    return row


def main():
    since = window_start_utc()
    lookback = type_lookback_days()
    since_by_type = {t: window_start_utc(lookback.get(t, 1)) for t in TYPES}
    reachable = check_reachable("sam.gov")
    out = {
        "window": {"from": days_ago_la(1), "to": today_la(), "modified_since_utc": since,
                   **({"backfill": {TYPES[t]: f"from {days_ago_la(n)}" for t, n in lookback.items()}}
                      if lookback else {})},
        "reachable": reachable,
        "justifications": [],
        "notices": [],
        "errors": [],
        "counts": {},
    }
    if not reachable:
        out["errors"].append("sam.gov unreachable")
        write_candidates(OUT_PATH, out)
        return

    with ThreadPoolExecutor(max_workers=len(TYPES)) as ex:
        listed = dict(zip(TYPES, ex.map(lambda t: list_type(t, since_by_type[t]), TYPES)))
    raw = []
    for t, (rows, err) in listed.items():
        if err:
            out["errors"].append(f"listing {err}")
        out["counts"][f"listed_{t}"] = len(rows)
        raw += rows
        print(f"SAM {TYPES[t]}: {len(rows)}" + (f" (error: {err})" if err else ""), file=sys.stderr)

    def detail(r):
        try:
            return fetch_detail(r["_id"]), None
        except Exception as e:  # noqa: BLE001
            return (None, {}, {}), f"detail fetch failed for {r['_id']}: {e}"

    with ThreadPoolExecutor(max_workers=8) as ex:
        details = list(ex.map(detail, raw))
    rows = []
    for r, (d, err) in zip(raw, details):
        if err:
            out["errors"].append(err)
        rows.append(to_row(r, d))

    # s/a/r: promote sole-source ones into the justification pipeline. The
    # attachment listing is reused by sam_docs.enrich below.
    promo = [x for x in rows if x["noticeType"] in PROMOTABLE]
    with ThreadPoolExecutor(max_workers=8) as ex:
        listings = dict(zip((x["noticeId"] for x in promo),
                            ex.map(lambda x: sam_docs.list_attachments(x["noticeId"]), promo)))
    for x in rows:
        if x["noticeType"] == "u":
            out["justifications"].append(x)
            continue
        if x["noticeType"] in ("o", "k", "p") and len(x.get("description_text") or "") > NOTICE_DESC_CAP:
            x["description_text"] = x["description_text"][:NOTICE_DESC_CAP]
            x["description_truncated"] = True
        out["notices"].append(x)
        if x["noticeType"] in PROMOTABLE:
            files = listings.get(x["noticeId"], ([], [], None))[0]
            why = ("title" if SOLE_SOURCE_TITLE.search(x.get("title") or "") else
                   "attachment" if any(sam_docs.JA_FILE_NAME.search(a.get("name") or "") for a in files) else None)
            if why:
                out["justifications"].append({**x, "source_type": x["noticeTypeLabel"], "promoted_by": why})
    out["counts"]["justifications_total"] = len(out["justifications"])
    out["counts"]["justifications_promoted"] = sum(1 for j in out["justifications"] if j.get("source_type"))
    out["counts"]["notices_total"] = len(out["notices"])

    # Every file attached to every notice (the signed J&As, statements of
    # work, specs, price schedules, award documents, Q&As) -- see sam_docs.py.
    texts = sam_docs.enrich(out["justifications"], out["notices"], prelisted=listings)
    sam_docs.write_documents(texts)
    out["counts"]["justification_documents_read"] = len(texts)

    write_candidates(OUT_PATH, out)


if __name__ == "__main__":
    main()
