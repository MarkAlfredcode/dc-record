#!/usr/bin/env python3
"""Auto-file OGE Online Form 201 requests for newly requestable ethics documents (added 2026-11-05).

OGE's "Officials' Individual Disclosures" index (oge_index.py) marks every
document either as a direct download or as "(Request this Document)": those
must be asked for through Form 201
(https://extapps2.oge.gov/201/Presiden.nsf/201%20Request?OpenForm&Filer=<LastName>)
and OGE emails them within ~2 business days. Mark's standing instruction:
whenever a new document appears in that "Request this Document" state, file
the form for it, as News media / Journalist at The Washington Sun, with
oge@markalfred.news as the delivery address. Mail to that address is handled
by the foia-inbox Email Worker and summarized by scripts/oge_inbox.py into a
section at the top of the Officials edition.

How a filing works (reverse-engineered from the form's own JavaScript):
  1. POST dlgDocumentListnew?ln=<last name>            -> radio list of people with that last name
  2. POST the same URL with $Refresh + Filer=<radio>   -> checkbox list of that person's documents,
     value "<description>~<UNID>"; downloadable ones say "(click to download)", requestable ones don't
  3. POST the main form with Filer, Name, Email, Occupation, OnBehalfYesNo=No,
     RequestorOrgType=News media, CheckBoxAgree (the statutes acknowledgment),
     DisplayDocs (descriptions, newline-joined) and RequestedDocs (UNIDs, newline-joined). Max 5 docs per request.

Ledger / dedupe: data/oge-requests-seen.json
  {"seed": [row keys], "seen": {key: date}, "filed": [...], "unmatched": {key: {...}}}
The first run (or --seed-only) puts every requestable row dated before --seed-before (default: today ET) in `seed`, so the
~10,900 historical rows are never filed. Keys, not a date cutoff: a backdated document that first shows up later is still new.

Usage:
  oge_requests.py                    dry run: show what would be filed (default; nothing is submitted)
  oge_requests.py --live             actually submit the forms
  oge_requests.py --days 60          dry-run/live over rows dated in the last N days even if unseen-seeded (testing)
Env: OGE_LIVE=1 is the same as --live (the Actions workflow sets it). OGE_REQUESTER_EMAIL overrides the address.
"""
import argparse
import hashlib
import html as htmllib
import http.cookiejar
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import oge_index  # noqa: E402

BASE = "https://extapps2.oge.gov/201/Presiden.nsf"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
LEDGER = "data/oge-requests-seen.json"
LEDGER_SEED = "data/oge-requests-seed.json"   # kept apart: one 175 KB line that never changes after the first run
ET = ZoneInfo("America/New_York")

REQUESTER = {
    "Name": "Mark Alfred",
    "Email": os.environ.get("OGE_REQUESTER_EMAIL", "oge@markalfred.news"),
    "Occupation": "Journalist, The Washington Sun",
    "OnBehalfYesNo": "No",
    "RequestorOrgType": "News media",
}
AGREE = "I am aware of the above statutes and regulations. (required)"
MAX_DOCS = 5
GIVE_UP_AFTER_DAYS = 10   # a row we can't match to a checkbox is retried until this old, then reported once


def today_et():
    return datetime.now(ET).date().isoformat()


def load_ledger():
    try:
        with open(LEDGER) as f:
            d = json.load(f)
    except (OSError, ValueError):
        d = {}
    try:
        with open(LEDGER_SEED) as f:
            d["seed"] = json.load(f)
    except (OSError, ValueError):
        pass
    d.setdefault("seed", [])
    d.setdefault("seen", {})
    d.setdefault("filed", [])
    d.setdefault("unmatched", {})
    return d


def save_ledger(d):
    os.makedirs(os.path.dirname(LEDGER), exist_ok=True)
    with open(LEDGER, "w") as f:
        json.dump({k: v for k, v in d.items() if k != "seed"}, f, indent=1, sort_keys=True)
        f.write("\n")
    with open(LEDGER_SEED, "w") as f:
        json.dump(sorted(d.get("seed") or []), f, separators=(",", ":"))
        f.write("\n")


def row_key(r):
    raw = "|".join([r.get("name", ""), r.get("agency", ""), r.get("title", ""), oge_index.label(r.get("type")),
                    (r.get("docDate") or "")[:10], r.get("amended") or ""])
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def norm(s):
    s = htmllib.unescape(re.sub(r"<[^>]+>", " ", s or ""))
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


class Session:
    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.op.addheaders = [("User-Agent", UA)]

    def get(self, url, timeout=45):
        with self.op.open(url, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace"), r.geturl()

    def post(self, url, fields, timeout=60):
        data = urllib.parse.urlencode(fields).encode()
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/x-www-form-urlencoded",
                                                               "Referer": url, "Origin": "https://extapps2.oge.gov"})
        with self.op.open(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace"), r.geturl()


def inputs(page):
    out = []
    for m in re.finditer(r"<input\b[^>]*>", page, re.I):
        tag = m.group(0)
        g = lambda a: (re.search(a + r'\s*=\s*"([^"]*)"', tag, re.I) or [None, None])[1]
        out.append({"name": g("name"), "type": (g("type") or "text").lower(), "value": htmllib.unescape(g("value") or ""),
                    "checked": bool(re.search(r"\bchecked\b", tag, re.I))})
    return out


def dialog_url(last):
    return f"{BASE}/dlgDocumentListnew?OpenForm&Seq=1&ln={urllib.parse.quote(last)}"


def people_for(sess, last):
    """Radio values (the 'Filer' strings) for everyone OGE lists under this last name."""
    _, page, _ = sess.get(f"{BASE}/dlgDocumentListnew?OpenForm&ln={urllib.parse.quote(last)}")
    return [i["value"] for i in inputs(page) if i["name"] == "Filer" and i["type"] == "radio"]


def docs_for(sess, last, filer):
    """[(description, unid, downloadable)] for one person."""
    _, page, _ = sess.post(dialog_url(last), {"__Click": "$Refresh", "%%Surrogate_Filer": "1", "Filer": filer})
    out = []
    for m in re.finditer(r'<input[^>]*name="isselected"[^>]*value="([^"]+)"[^>]*>(.*?)(?=<input|</tr>|<tr|$)', page, re.I | re.S):
        val, tail = htmllib.unescape(m.group(1)), re.sub(r"<[^>]+>", " ", m.group(2))
        desc, _, unid = val.rpartition("~")
        out.append((desc.strip(), unid.strip(), "click to download" in tail.lower()))
    # the "(click to download)" text sits in the same cell as the checkbox in some layouts, inside the value in none;
    # fall back to scanning the visible line for each description
    text = htmllib.unescape(re.sub(r"<[^>]+>", " ", page))
    out = [(d, u, dl or bool(re.search(re.escape(d) + r"\s*\(click to download\)", text, re.I))) for d, u, dl in out]
    return out


def pick_person(people, row):
    """Which radio is this index row's person? Name first, then title / agency words."""
    last, _, first = row["name"].partition(",")
    want = norm(last + " " + first.strip().split(" ")[0])
    cands = [p for p in people if norm(p).startswith(want)]
    if len(cands) <= 1:
        return cands
    t = norm(row.get("title"))
    exact = [p for p in cands if t and t in norm(p)]
    return exact or cands


DATE_ADDED = re.compile(r"\(\s*date added\s*:?\s*(\d{1,2})/(\d{1,2})/(\d{4})\s*\)", re.I)


def match_doc(docs, row, used):
    """Checkbox for this index row. The picker decorates some documents ("278 Transaction (Date Added: 10/23/2026)"),
    so: a dated checkbox must be within a day of the index row's date; otherwise exact label, then contained text.
    A checkbox is never given to two rows (`used` holds the UNIDs already taken)."""
    lab = norm(oge_index.base_label(row["type"]))
    rd = datetime.fromisoformat((row.get("docDate") or "")[:10]).date() if row.get("docDate") else None
    exact, loose = [], []
    for d, u, dl in docs:
        if u in used:
            continue
        m = DATE_ADDED.search(d)
        core = norm(DATE_ADDED.sub("", d))
        if m:
            try:
                dd = datetime(int(m.group(3)), int(m.group(1)), int(m.group(2))).date()
            except ValueError:
                continue
            if rd is None or abs((dd - rd).days) > 1:
                continue
        if core == lab:
            exact.append((d, u, dl))
        elif core and (lab in core or core in lab):
            loose.append((d, u, dl))
    hit = (exact or loose or [None])[0]
    if hit:
        used.add(hit[1])
    return hit


def submit(sess, last, filer, picks, live):
    """Fill and (if live) post Form 201. picks = [(desc, unid)]. Returns (ok, detail)."""
    page_url = f"{BASE}/201%20Request?OpenForm&Filer={urllib.parse.quote(last)}"
    _, page, _ = sess.get(page_url)
    fields = []
    for i in inputs(page):
        n = i["name"]
        if not n or i["type"] in ("button", "submit", "image", "radio", "checkbox"):
            if i["type"] in ("radio", "checkbox") and n and i["checked"]:
                fields.append((n, i["value"]))
            continue
        fields.append((n, i["value"]))
    d = dict(fields)
    d.update({"Filer": filer, "Name": REQUESTER["Name"], "Email": REQUESTER["Email"], "Occupation": REQUESTER["Occupation"],
              "OnBehalfYesNo": "No", "%%Surrogate_OnBehalfYesNo": "1", "%%Surrogate_RequestorOrgType": "1",
              "RequestorOrgType": REQUESTER["RequestorOrgType"], "%%Surrogate_CheckBoxAgree": "1", "CheckBoxAgree": AGREE,
              "DisplayDocs": "\n".join(p[0] for p in picks), "RequestedDocs": "\n".join(p[1] for p in picks),
              "SpecialFiler": d.get("SpecialFiler", "")})
    action = re.search(r'<form[^>]*name="_DominoForm"[^>]*action="([^"]+)"|<form[^>]*action="([^"]+)"[^>]*name="_DominoForm"', page, re.I)
    action = htmllib.unescape((action.group(1) or action.group(2)) if action else "/201/Presiden.nsf/201%20Request?OpenForm&Seq=1")
    url = urllib.parse.urljoin(page_url, action)
    if not live:
        return True, "dry run: " + json.dumps({k: v for k, v in d.items() if v and not k.startswith("%%") and k != "__Click"})[:500]
    status, body, final = sess.post(url, d)
    text = re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<script.*?</script>|<style.*?</style>|<[^>]+>", " ", body, flags=re.S | re.I))).strip()
    bad = re.search(r"\b(error 500|http status 4|an error occurred|there was a problem|invalid|not authorized|cannot be processed)\b", text, re.I)
    # The page header is identical on every OGE page; a confirmation or an error message lives further down, so keep both ends.
    said = re.search(r"[^.]{0,120}\b(submitted|received your request|thank you|confirmation|will receive)\b[^.]{0,160}", text[400:], re.I)
    return (status < 400 and not bad), f"HTTP {status} -> {final} :: CONFIRMATION-LIKE: {said.group(0).strip() if said else 'none seen'} :: TAIL: {text[-260:]}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--seed-only", action="store_true", help="record the historical rows as seen and exit (files nothing)")
    ap.add_argument("--seed-before", default=None, help="first run: treat rows dated before this as already seen (default today ET)")
    ap.add_argument("--days", type=int, default=None, help="testing: consider unseen rows dated in the last N days, ignoring the seed")
    ap.add_argument("--limit", type=int, default=None, help="file at most N request batches this run")
    ap.add_argument("--only", default=None, help="only this last name (testing)")
    ap.add_argument("--agency", default=None, help="targeted pull: only rows whose agency contains this text; ignores the historical seed (rows already filed/seen are still skipped)")
    ap.add_argument("--since", default=None, help="with --agency: only rows dated on/after this YYYY-MM-DD")
    a = ap.parse_args()
    live = a.live or os.environ.get("OGE_LIVE") == "1"

    led = load_ledger()
    rows = oge_index.fetch_all(max_age_sec=0)
    req = [r for r in rows if "Request this Document" in (r.get("type") or "")]
    first_run = not led["seed"] and not led["seen"] and not led["filed"]
    cutoff = a.seed_before or today_et()
    if (first_run or a.seed_only) and a.days is None:
        led["seed"] = sorted({row_key(r) for r in req if (r.get("docDate") or "")[:10] < cutoff})
        print(f"oge-requests: seeded {len(led['seed'])} historical requestable rows (dated before {cutoff}).", file=sys.stderr)
        if live or a.seed_only:
            save_ledger(led)
        if a.seed_only:
            return
    known = set(led["seen"]) if a.agency else set(led["seed"]) | set(led["seen"])

    horizon = (datetime.now(ET).date() - timedelta(days=a.days)).isoformat() if a.days else None
    new = [r for r in req if row_key(r) not in known and (horizon is None or (r.get("docDate") or "")[:10] >= horizon)]
    if a.agency:
        new = [r for r in new if a.agency.lower() in (r.get("agency") or "").lower()
               and (not a.since or (r.get("docDate") or "")[:10] >= a.since)]
    if a.only:
        new = [r for r in new if r["name"].lower().startswith(a.only.lower())]
    print(f"oge-requests: {len(req)} requestable rows in OGE's index, {len(new)} new. {'LIVE' if live else 'DRY RUN'}.", file=sys.stderr)
    if not new:
        return

    by_person = {}
    for r in new:
        by_person.setdefault((r["name"], r.get("agency"), r.get("title")), []).append(r)

    sess = Session()
    batches = 0
    for (name, agency, title), rs in sorted(by_person.items()):
        last = name.partition(",")[0].strip()
        try:
            people = pick_person(people_for(sess, last), rs[0])
            time.sleep(1)
            matched, unmatched = [], []
            filer = None
            for filer_cand in people:
                docs = docs_for(sess, last, filer_cand)
                time.sleep(1)
                used = set()
                hits = [(r, match_doc(docs, r, used)) for r in rs]
                if any(h for _, h in hits):
                    filer = filer_cand
                    matched = [(r, h) for r, h in hits if h and not h[2]]
                    unmatched = [r for r, h in hits if not h]
                    already = [(r, h) for r, h in hits if h and h[2]]   # became downloadable: nothing to request
                    for r, _ in already:
                        led["seen"][row_key(r)] = today_et()
                    break
            if not filer:
                unmatched = rs
        except Exception as e:  # network / layout surprise: leave unseen so the next run retries
            print(f"oge-requests: {name} failed to look up ({e}); will retry next run.", file=sys.stderr)
            continue

        for r in unmatched:
            k = row_key(r)
            u = led["unmatched"].setdefault(k, {"first": today_et(), "name": name, "doc": oge_index.label(r["type"]), "date": r["docDate"][:10]})
            age = (datetime.now(ET).date() - datetime.fromisoformat(u["first"]).date()).days
            if age >= GIVE_UP_AFTER_DAYS:
                led["seen"][k] = "gave-up"
                print(f"oge-requests: GAVE UP matching {name} / {u['doc']} to the request form after {age} days.", file=sys.stderr)
            else:
                print(f"oge-requests: {name} / {oge_index.label(r['type'])}: not on the request form's document list yet; will retry.", file=sys.stderr)
        for i in range(0, len(matched), MAX_DOCS):
            if a.limit is not None and batches >= a.limit:
                return
            chunk = matched[i:i + MAX_DOCS]
            picks = [(h[0], h[1]) for _, h in chunk]
            ok, detail = submit(sess, last, filer, picks, live)
            batches += 1
            print(f"oge-requests: {'FILED' if live and ok else ('would file' if not live else 'FAILED')} {filer}: "
                  f"{', '.join(p[0] for p in picks)} :: {detail[:200]}", file=sys.stderr)
            if live and ok:
                when = datetime.now(ET).isoformat(timespec="seconds")
                led["filed"].append({"at": when, "filer": filer, "name": name, "title": title, "agency": agency,
                                     "docs": [p[0] for p in picks], "doc_dates": [r["docDate"][:10] for r, _ in chunk],
                                     "unids": [p[1] for p in picks], "response": detail[:700]})
                for r, _ in chunk:
                    led["seen"][row_key(r)] = today_et()
                    led["unmatched"].pop(row_key(r), None)
                save_ledger(led)       # right away: a later crash must never cause a duplicate filing
            time.sleep(3)
    if live:
        save_ledger(led)


if __name__ == "__main__":
    main()
