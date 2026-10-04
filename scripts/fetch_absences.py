#!/usr/bin/env python3
"""Missed Votes -- who isn't showing up (added 2026-10-04). ("McConnell's
Absence Is Grinding Some Senate Business to a Halt"; "Congress Leaving ...
Dark Through Elections.")

Reads every House and Senate roll call from the Clerk's / Secretary of the
Senate's keyless XML and keeps a running tally per member in
data/vote-tally.json (incremental: only rolls newer than `last_roll` are
fetched; the file also keeps a 21-day list of roll-call summaries so the
window report is stateless). "Not Voting" counts as missed; it includes
excused absences, illness and leave -- the roll call doesn't say which.

Section, built with no LLM:
  * members who skipped half or more of the votes in the last 2 days
  * votes where more members skipped than the margin of the outcome
  * AM only: members on a long unbroken streak of missed votes, and
    year-to-date missed-vote leaders per chamber

Output: extras/congress/absences.json
"""
import os
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta

from dc_common import days_ago_la, http_get, load_manifest, save_manifest, today_la
from dc_extras import esc, link, mdY, run_mode, section, write_extra
from members import house_roster

PATH = "data/vote-tally.json"
UA = {"User-Agent": "Mozilla/5.0"}
CONGRESS = 119
KEEP_ROLL_DAYS = 21
WINDOW_DAYS = 2
MIN_WINDOW_VOTES = 3
STREAK_MIN = 8
YTD_MIN_VOTES = 50
MAX_NEW_PER_RUN = 120  # per chamber per run; the first run backfills the year over a few runs


def house_url(year, n):
    return f"https://clerk.house.gov/evs/{year}/roll{n:03d}.xml"


def senate_url(year, n):
    return f"https://www.senate.gov/legislative/LIS/roll_call_votes/vote{CONGRESS}{year - 2024}/vote_{CONGRESS}_{year - 2024}_{n:05d}.xml"


def curl_get(url):
    """curl, not urllib: house.gov's and senate.gov's WAFs 403 Python's TLS fingerprint (confirmed 2026-10-04:
    same URL, same moment, curl 200 / urllib 403)."""
    for attempt in range(3):
        r = subprocess.run(["curl", "-s", "-L", "--max-time", "40", "-A", "Mozilla/5.0", "-w", "\n%{http_code}", url],
                           capture_output=True)
        body, _, code = r.stdout.rpartition(b"\n")
        status = int(code or 0)
        if status != 403 or attempt == 2:
            return status, body
        time.sleep(3)
    return status, body


def fetch_xml(url, chamber):
    """None = no such roll yet. House answers a not-yet-held roll with a 403 from its CDN; the Senate
    404s but also 403s (with an HTML apology page) when it rate-limits, which must NOT look like the end."""
    status, body = curl_get(url)
    if status == 404 or (status == 403 and chamber == "house"):
        return None
    if status >= 400:
        raise RuntimeError(f"{url} -> HTTP {status} (rate-limited or blocked)")
    if b"<roll_call_vote" not in body[:400] and b"<rollcall-vote" not in body[:400]:
        return None  # senate.gov 301s a not-yet-held roll to an HTML "not available" page
    return ET.fromstring(body)


def parse_house(root, n, year):
    md = root.find("vote-metadata")
    if (md.findtext("vote-type") or "").upper() == "QUORUM":
        return None
    d = datetime.strptime(md.findtext("action-date").strip(), "%d-%b-%Y").date().isoformat()
    members = []
    for rv in root.iter("recorded-vote"):
        leg = rv.find("legislator")
        members.append((leg.get("name-id"), (rv.findtext("vote") or "").strip(), leg.get("party"), leg.get("state"),
                        leg.get("unaccented-name") or (leg.text or "")))
    return {
        "chamber": "house", "num": n, "date": d,
        "question": (md.findtext("vote-question") or "").strip(),
        "subject": ((md.findtext("legis-num") or "").strip() + " " + (md.findtext("vote-desc") or "").strip()).strip(),
        "result": (md.findtext("vote-result") or "").strip(),
        "yea": sum(1 for m in members if m[1] in ("Yea", "Aye")), "nay": sum(1 for m in members if m[1] in ("Nay", "No")),
        "url": f"https://clerk.house.gov/Votes/{year}{n}",
    }, members


def parse_senate(root, n, year):
    m = re.match(r"[A-Za-z]+ \d+, \d{4}", root.findtext("vote_date") or "")
    d = datetime.strptime(m.group(0), "%B %d, %Y").date().isoformat()
    cnt = root.find("count")
    members = []
    for mem in root.iter("member"):
        members.append((mem.findtext("lis_member_id"), (mem.findtext("vote_cast") or "").strip(), mem.findtext("party"),
                        mem.findtext("state"), f"{mem.findtext('first_name')} {mem.findtext('last_name')}"))
    return {
        "chamber": "senate", "num": n, "date": d,
        "question": (root.findtext("question") or "").strip(),
        "subject": (root.findtext("vote_title") or root.findtext("vote_document_text") or "").strip(),
        "result": (root.findtext("vote_result") or "").strip(),
        "yea": int(cnt.findtext("yeas") or 0) if cnt is not None else 0,
        "nay": int(cnt.findtext("nays") or 0) if cnt is not None else 0,
        "url": f"https://www.senate.gov/legislative/LIS/roll_call_votes/vote{CONGRESS}{year - 2024}/vote_{CONGRESS}_{year - 2024}_{n:05d}.htm",
    }, members


def pull_new(chamber, year, last):
    """Fetch rolls last+1, last+2, ... until the first missing one. Gentle on purpose: senate.gov's WAF
    403s a burst of ~8 parallel requests within seconds. Returns ([(num, parsed)], error-or-None); a
    partial list is still usable, the next run resumes from last_roll."""
    url_fn, parse = (house_url, parse_house) if chamber == "house" else (senate_url, parse_senate)
    out, n = [], last + 1
    with ThreadPoolExecutor(2) as ex:
        while len(out) < MAX_NEW_PER_RUN:
            batch = list(range(n, n + 4))
            try:
                roots = list(ex.map(lambda i: fetch_xml(url_fn(year, i), chamber), batch))
            except Exception as e:  # noqa: BLE001
                return out, str(e)
            for i, root in zip(batch, roots):
                if root is None:
                    return out, None
                out.append((i, parse(root, i, year)))  # parsed is None for quorum calls
            n += 4
            time.sleep(0.4)
    return out, None


def apply(state, rolls_out, pulled):
    """Fold newly fetched rolls into one chamber's running tally."""
    for n, parsed in pulled:
        state["last_roll"] = max(state["last_roll"], n)
        if not parsed:
            continue
        summary, members = parsed
        ids = []
        for mid, vote, party, st, nm in members:
            e = state["members"].setdefault(mid, {"name": nm, "party": party, "state": st, "cast": 0, "missed": 0,
                                                  "streak": 0, "last_cast": None})
            ids.append(mid)
            if vote == "Not Voting":
                e["missed"] += 1
                e["streak"] += 1
            else:
                e["cast"] += 1
                e["streak"] = 0
                e["last_cast"] = summary["date"]
        state["present"] = ids  # members on the most recent roll = current members
        summary["not_voting"] = [mid for mid, vote, *_ in members if vote == "Not Voting"]
        rolls_out.append(summary)


def main():
    year = int(today_la()[:4])
    data = load_manifest(PATH)
    if data.get("year") != year:
        data = {"year": year}
    data.setdefault("rolls", [])
    for ch in ("house", "senate"):
        data.setdefault(ch, {"last_roll": 0, "members": {}, "present": []})
    errors = []
    for ch in ("house", "senate"):
        try:
            pulled, err = pull_new(ch, year, data[ch]["last_roll"])
            apply(data[ch], data["rolls"], pulled)
            if err:
                errors.append(f"{ch}: {err}")
            print(f"{ch}: {sum(1 for _, p in pulled if p)} new roll call(s), last_roll={data[ch]['last_roll']}", file=sys.stderr)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{ch}: {e}")
            print(f"{ch} fetch failed: {e}", file=sys.stderr)
    cutoff = days_ago_la(KEEP_ROLL_DAYS)
    data["rolls"] = [r for r in data["rolls"] if r["date"] >= cutoff]
    if not os.environ.get("DC_NO_SAVE"):
        save_manifest(PATH, data)

    roster = house_roster()

    def who(ch, mid):
        e = data[ch]["members"].get(mid, {})
        if ch == "house":
            r = roster.get(mid, {})
            nm = r.get("name") or e.get("name", mid)
            return f"Rep. {nm} ({e.get('party', '?')}-{e.get('state', '?')})"
        return f"Sen. {e.get('name', mid)} ({e.get('party', '?')}-{e.get('state', '?')})"

    since = days_ago_la(WINDOW_DAYS)
    win = [r for r in data["rolls"] if r["date"] >= since]
    blocks, paras = [], []
    cham = {"house": "House", "senate": "Senate"}

    # 1. who skipped most of the recent votes
    items = []
    for ch in ("house", "senate"):
        rs = [r for r in win if r["chamber"] == ch]
        if len(rs) < MIN_WINDOW_VOTES:
            continue
        cur = set(data[ch]["present"])
        missed = {}
        for r in rs:
            for mid in r["not_voting"]:
                if mid in cur:
                    missed[mid] = missed.get(mid, 0) + 1
        for mid, k in missed.items():
            if k / len(rs) >= 0.5 and k >= MIN_WINDOW_VOTES:
                items.append((k / len(rs), k, ch, mid, len(rs)))
    items.sort(reverse=True)
    if items:
        blocks.append({"type": "subhead", "text": f"Skipped half or more of the recent votes (since {mdY(since)})"})
        blocks.append({"type": "list", "items": [
            f"<strong>{esc(who(ch, mid))}</strong> missed {k} of {n} {cham[ch]} roll-call votes"
            for _, k, ch, mid, n in items[:40]]})

    # 2. votes where absences outweighed the margin
    close = []
    for r in win:
        nv, margin = len(r["not_voting"]), abs(r["yea"] - r["nay"])
        if r["yea"] + r["nay"] and nv > margin and margin <= (12 if r["chamber"] == "house" else 4):
            close.append((margin, r, nv))
    close.sort(key=lambda t: t[0])
    if close:
        blocks.append({"type": "subhead", "text": "Votes where more members skipped than the margin"})
        blocks.append({"type": "list", "items": [
            link(cham[r["chamber"]] + " roll call " + str(r["num"]), r["url"]) + f" ({mdY(r['date'])}): "
            f"{esc(r['question'])} on {esc(r['subject'][:140])} -- {esc(r['result'])}, {r['yea']}-{r['nay']}, "
            f"with {nv} not voting"
            for margin, r, nv in close[:20]]})

    am = run_mode() == "am"
    # 3. long unbroken streaks (AM only; the list barely moves between editions)
    if am:
        streaks = []
        for ch in ("house", "senate"):
            cur = set(data[ch]["present"])
            for mid, e in data[ch]["members"].items():
                if mid in cur and e["streak"] >= STREAK_MIN:
                    streaks.append((e["streak"], ch, mid, e["last_cast"]))
        streaks.sort(reverse=True)
        if streaks:
            blocks.append({"type": "subhead", "text": "On a long unbroken streak of missed votes"})
            blocks.append({"type": "list", "items": [
                f"<strong>{esc(who(ch, mid))}</strong> has missed the last {k} {cham[ch]} roll-call votes in a row"
                + (f"; last vote cast {mdY(last)}" if last else "; no vote cast this year")
                for k, ch, mid, last in streaks[:25]]})
        # 4. year-to-date leaders
        for ch in ("house", "senate"):
            cur = set(data[ch]["present"])
            rows = [(e["missed"] / (e["cast"] + e["missed"]), e["missed"], e["cast"] + e["missed"], mid)
                    for mid, e in data[ch]["members"].items() if mid in cur and e["cast"] + e["missed"] >= YTD_MIN_VOTES]
            rows.sort(reverse=True)
            if rows and rows[0][1] >= 5:
                blocks.append({"type": "subhead", "text": f"{cham[ch]}: highest missed-vote rate in {year}"})
                blocks.append({"type": "list", "items": [
                    f"<strong>{esc(who(ch, mid))}</strong>: {m} of {t} votes missed ({rate:.0%})"
                    for rate, m, t, mid in rows[:15] if m >= 5]})

    if blocks:
        n_h = sum(1 for r in win if r["chamber"] == "house")
        n_s = sum(1 for r in win if r["chamber"] == "senate")
        if win:
            paras.append(f"The House held {n_h} and the Senate {n_s} roll-call votes since {mdY(since)}.")
        else:
            paras.append(f"No roll-call votes since {mdY(since)}; the standing absence lists below are as of the last votes held.")
        blocks.append({"type": "note", "text": "Counts every roll call in the House Clerk's and Senate's published XML; \"missed\" means recorded as Not Voting, "
                       "which covers illness, leave, campaigning and excused absences alike -- the record doesn't say which. "
                       f"Tally runs from January 1, {year}; quorum calls excluded."})
        headline = "Who's skipping the votes"
        if items:
            ch, mid = items[0][2], items[0][3]
            headline = f"{who(ch, mid).split(' (')[0]} missed {items[0][1]} of {items[0][4]} recent votes"
        write_extra("congress", "absences", [section("Missed Votes", headline, paras, blocks)],
                    f"{len(items)} members skipped half+ of recent votes; {len(close)} votes with absences > margin")
    else:
        write_extra("congress", "absences", [], "no notable absences")
    if errors:
        print("ERRORS: " + "; ".join(errors), file=sys.stderr)


if __name__ == "__main__":
    main()
