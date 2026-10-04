#!/usr/bin/env python3
"""Discharge Petitions (added 2026-10-04). ("Discharge Petitions Are Surging.
Republican Defectors Say It's Not Mike Johnson's Fault"; "Moderate Republicans
Help Democrats Force a Vote on Extending Health Care Tax Credits".)

A discharge petition forces a floor vote once 218 members sign it. The House
Clerk publishes each petition's signature list (clerk.house.gov/DischargePetition,
HTML fragments, no key). Every run reads the 119th Congress list, then each
petition that hasn't already hit 218, and reports:
  * signatures added in the last 3 days, named, flagged when a signer is of
    the other party than the petition's sponsor (the "Republican defectors" angle)
  * petitions that reached 218 in the window
  * new petitions filed in the window
  * AM only: the open petitions closest to 218

Stateless for the report (signature dates come from the Clerk); the only
persisted file, data/discharge-petitions.json, remembers which petitions have
already reached 218 so they aren't re-downloaded (each detail page is ~0.5MB).

Output: extras/congress/discharge_petitions.json
"""
import html as htmllib
import os
import re
import subprocess
import sys
import time
from datetime import datetime

from dc_common import load_manifest, save_manifest
from dc_extras import esc, link, lookback, mdY, run_mode, section, write_extra

BASE = "https://clerk.house.gov"
CACHE = "data/discharge-petitions.json"
CONGRESS = 119
THRESHOLD = 218
NEAR = 15  # "within N signatures of 218"


def curl(url):
    """curl, not urllib: the Clerk's WAF 403s Python's TLS fingerprint."""
    for attempt in range(3):
        r = subprocess.run(["curl", "-s", "-L", "--max-time", "60", "-A", "Mozilla/5.0", "-w", "\n%{http_code}", url], capture_output=True)
        body, _, code = r.stdout.rpartition(b"\n")
        if code.strip() == b"200":
            return body.decode("utf-8", "replace")
        time.sleep(3)
    raise RuntimeError(f"{url} -> HTTP {code.decode()}")


def text(s):
    return re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


def list_petitions():
    out, page, pages = [], 1, 1
    while page <= pages:
        frag = curl(f"{BASE}/DischargePetition/DischargePetitions?Page={page}&CongressNum={CONGRESS}")
        pages = max([int(x) for x in re.findall(r"page \d+ of (\d+) pages", frag)] or [1])
        for blk in frag.split('class="role-call-vote discharge-petition-result"')[1:]:
            no = re.search(r"Discharge Petition No\. (\d+)", blk)
            href = re.search(r'href="(/DischargePetition/\d+)(?:\?[^"]*)?"', blk)
            if not (no and href):
                continue
            t = text(blk)
            def field(label):
                m = re.search(label + r":\s*(.+?)(?:\s+(?:View Petition|Bill Number|Petition Date|Sponsor|Referral Date|Description)\b|$)", t)
                return m.group(1).strip() if m else ""
            out.append({
                "no": int(no.group(1)), "href": href.group(1),
                "desc": field("Description"), "bill": field("Bill Number"),
                "petition_date": field("Petition Date"), "sponsor": field("Sponsor"),
                "bill_url": (re.search(r'href="?(https://www\.congress\.gov/bill/[^" >]+)', blk) or [None, ""])[1],
            })
        page += 1
        time.sleep(0.4)
    return out


def ordinal_date(s):
    m = re.match(r"([A-Za-z]+) (\d+)(?:st|nd|rd|th), (\d{4})", s or "")
    return datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}", "%B %d %Y").date().isoformat() if m else ""


def signatures(detail_html):
    sigs = []
    body = detail_html[detail_html.find('id="member-signatures"'):]
    for row in re.findall(r"<tr>(.*?)</tr>", body, flags=re.S):
        name = re.search(r'data-label="Representative">.*?>([^<>]+)</a>', row, flags=re.S)
        st = re.search(r'<td style="display:none;">([A-Z]{2})</td>', row)
        party = re.search(r'data-label="Party">\s*([A-Za-z]+)', row)
        when = re.search(r"<span style=\"display:none;\">(\d\d)/(\d\d)/(\d{4})", row)
        if name and when:
            sigs.append({"name": text(name.group(1)), "state": st.group(1) if st else "",
                         "party": {"Republican": "R", "Democratic": "D", "Independent": "I"}.get((party.group(1) if party else ""), "?"),
                         "date": f"{when.group(3)}-{when.group(1)}-{when.group(2)}"})
    return sigs


def main():
    since = lookback(3)
    cache = load_manifest(CACHE)
    petitions = list_petitions()
    print(f"{len(petitions)} petitions listed", file=sys.stderr)
    items, reached, filed, near_rows, errors = [], [], [], [], []
    for p in petitions:
        pet_date = ordinal_date(p["petition_date"])
        c = cache.get(str(p["no"]), {})
        if c.get("done") and c.get("done_date", "9999") < since:
            continue
        try:
            sigs = signatures(curl(BASE + p["href"]))
        except Exception as e:  # noqa: BLE001
            errors.append(f"petition {p['no']}: {e}")
            continue
        time.sleep(0.4)
        sigs.sort(key=lambda s: s["date"])
        n = len(sigs)
        sponsor_party = sigs[0]["party"] if sigs else "?"
        new = [s for s in sigs if s["date"] >= since]
        p.update(count=n, sponsor_party=sponsor_party, new=new, url=BASE + p["href"], pet_date=pet_date)
        if n >= THRESHOLD:
            done_date = sigs[THRESHOLD - 1]["date"]
            cache[str(p["no"])] = {"count": n, "done": True, "done_date": done_date}
            if done_date >= since:
                p["done_date"] = done_date
                reached.append(p)
            continue
        cache[str(p["no"])] = {"count": n, "done": False}
        if pet_date >= since:
            filed.append(p)
        elif new:
            items.append(p)
        if THRESHOLD - n <= NEAR and p not in items and p not in filed:
            near_rows.append(p)
    if not os.environ.get("DC_NO_SAVE"):
        save_manifest(CACHE, cache)

    def signer(s):
        return f"{esc(s['name'])} ({s['party']}-{s['state']})"

    def card(p):
        crossed = [s for s in p["new"] if s["party"] not in ("?", p["sponsor_party"])]
        line = (f"<strong>{link('Discharge Petition No. ' + str(p['no']), p['url'])}</strong>: {esc(p['desc'][:200])} "
                f"({link(p['bill'], p['bill_url']) if p['bill_url'] else esc(p['bill'])}, sponsor {esc(p['sponsor'])}) -- "
                f"{p['count']} of {THRESHOLD} signatures" + (f", {THRESHOLD - p['count']} to go" if p["count"] < THRESHOLD else ""))
        if p["new"]:
            line += f". Signed since {mdY(since)}: " + "; ".join(signer(s) + f" on {mdY(s['date'])}" for s in p["new"][:12])
            if len(p["new"]) > 12:
                line += f"; and {len(p['new']) - 12} more"
        if crossed:
            line += f". <strong>Across party lines:</strong> " + ", ".join(signer(s) for s in crossed[:6]) + \
                    f" signed a petition sponsored by a {'Republican' if p['sponsor_party'] == 'R' else 'Democrat'}"
        return line

    blocks = []
    if reached:
        blocks += [{"type": "subhead", "text": "Hit 218: a floor vote can now be forced"},
                   {"type": "list", "items": [card(p) + f". Reached 218 on {mdY(p['done_date'])}" for p in reached]}]
    if items:
        items.sort(key=lambda p: (-len(p["new"]), -p["count"]))
        blocks += [{"type": "subhead", "text": "New signatures"}, {"type": "list", "items": [card(p) for p in items[:8]]}]
    if filed:
        blocks += [{"type": "subhead", "text": "New petitions"}, {"type": "list", "items": [card(p) for p in filed]}]
    if run_mode() == "am" and near_rows:
        near_rows.sort(key=lambda p: -p["count"])
        blocks += [{"type": "subhead", "text": f"Within {NEAR} signatures of 218"},
                   {"type": "list", "items": [card(p) for p in near_rows[:6]]}]
    if blocks:
        blocks.append({"type": "note", "text": "Signature counts and dates come from the House Clerk's Discharge Petitions page. A petition needs 218 signatures "
                       "(a majority of the full House); signatures can be added only while the House is in session. "
                       "\"Across party lines\" compares each signer to the petition's first signer, its sponsor."})
        top = reached[0] if reached else (items[0] if items else (filed[0] if filed else near_rows[0]))
        headline = (f"Discharge Petition No. {top['no']} reaches 218" if reached else
                    f"Discharge Petition No. {top['no']}: {top['count']} of {THRESHOLD} signatures")
        paras = [f"{len(items)} open petition(s) gained signatures, {len(filed)} were filed, and {len(reached)} reached 218 since {mdY(since)}."]
        write_extra("congress", "discharge_petitions", [section("Discharge Petitions", headline, paras, blocks)],
                    f"discharge petitions: {len(reached)} hit 218, {len(items)} gained signers, {len(filed)} new")
    else:
        write_extra("congress", "discharge_petitions", [], "no discharge petition activity")
    if errors:
        print("ERRORS: " + "; ".join(errors), file=sys.stderr)


if __name__ == "__main__":
    main()
