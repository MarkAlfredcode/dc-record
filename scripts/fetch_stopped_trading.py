#!/usr/bin/env python3
"""Stopped Trading -- House members who were prolific traders and have gone
quiet (added 2026-10-04). ("Rep. Rob Bresnahan Was a Prolific Stock Trader.
Now He's Quit Cold Turkey"; "Vermont's Congressional Members Are Rich. But
They Don't Buy Stocks.")

Uses only the House Clerk's financial-disclosure INDEX (the yearly
<year>FD.zip listing of every filing, including each Periodic Transaction
Report), so it needs no PDF parsing. A member is flagged when they
  * filed at least 6 PTRs in the 12 months before the quiet window, and
  * have filed none in the last 90 days, and
  * are a current House member (matched against the Clerk's roster).
Each member is reported once (data/stopped-trading-seen.json).

What it can't tell you: why. A member who stops filing may have divested,
moved assets into a blind trust or an index fund, been covered by a new
stock-trading law, or simply not traded. And PTRs are due within 45 days of
a trade, so the most recent trades may not be filed yet. House only -- the
Senate's e-filing search needs a session login and isn't indexed here.

Output: extras/officials/stopped_trading.json
"""
import csv
import io
import re
import subprocess
import sys
import zipfile
from collections import defaultdict
from datetime import date, datetime, timedelta

from dc_common import today_la
from dc_extras import SeenManifest, esc, link, mdY, norm, section, write_extra
from members import house_roster

ZIP = "https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}FD.zip"
MIN_PRIOR = 6
QUIET_DAYS = 90
BASELINE_DAYS = 365
MAX_LISTED = 8


def curl_bytes(url):
    r = subprocess.run(["curl", "-s", "-L", "--max-time", "120", "-A", "Mozilla/5.0", url], capture_output=True)
    return r.stdout


def index_rows(year):
    body = curl_bytes(ZIP.format(year=year))
    try:
        z = zipfile.ZipFile(io.BytesIO(body))
    except zipfile.BadZipFile:
        return []
    name = next(n for n in z.namelist() if n.endswith(".txt"))
    return list(csv.DictReader(io.StringIO(z.read(name).decode("utf-8-sig", errors="replace")), delimiter="\t"))


def parse(s):
    try:
        return datetime.strptime(s.strip(), "%m/%d/%Y").date()
    except ValueError:
        return None


def main():
    today = date.fromisoformat(today_la())
    quiet_from = today - timedelta(days=QUIET_DAYS)
    base_from = quiet_from - timedelta(days=BASELINE_DAYS)
    roster = house_roster()
    if not roster:
        print("House roster unavailable; skipping", file=sys.stderr)
        write_extra("officials", "stopped_trading", [], "roster unavailable")
        return
    by_key = {}
    for bio, r in roster.items():
        by_key[(r["dst"], norm(r["last"]))] = bio
    ptrs = defaultdict(list)  # bioguide -> [filing dates]
    seen_years = 0
    for year in range(today.year - 2, today.year + 1):
        rows = index_rows(year)
        seen_years += bool(rows)
        for r in rows:
            if r["FilingType"] != "P" or not (r["Prefix"] or "").strip().startswith("Hon"):
                continue
            d = parse(r["FilingDate"])
            sd = r["StateDst"]
            dst = f"{sd[:2]}-{sd[2:].lstrip('0') or 'AL'}"
            bio = by_key.get((dst, norm(r["Last"])))
            if d and bio:
                ptrs[bio].append(d)
    if not seen_years:
        print("no disclosure index could be downloaded", file=sys.stderr)
        write_extra("officials", "stopped_trading", [], "disclosure index unavailable")
        return

    flagged = []
    for bio, ds in ptrs.items():
        prior = [d for d in ds if base_from <= d < quiet_from]
        recent = [d for d in ds if d >= quiet_from]
        if len(prior) >= MIN_PRIOR and not recent:
            last = max(ds)
            flagged.append((len(prior), bio, last, (today - last).days))
    flagged.sort(reverse=True)

    seen = SeenManifest("data/stopped-trading-seen.json", keep_days=120)
    fresh = [f for f in flagged if f[1] not in seen]
    shown = fresh[:MAX_LISTED]
    for f in shown:
        seen.add(f[1])
    seen.save()

    if shown:
        items = []
        for n, bio, last, gap in shown:
            r = roster[bio]
            items.append(f"<strong>Rep. {esc(r['name'])} ({r['party']}-{esc(r['dst'])})</strong> filed {n} periodic transaction reports "
                         f"in the 12 months from {mdY(str(base_from))} to {mdY(str(quiet_from))}, and none in the {gap} days since the last, on {mdY(str(last))}")
        blocks = [{"type": "list", "items": items},
                  {"type": "note", "text": f"House members who filed {MIN_PRIOR} or more periodic transaction reports (STOCK Act trade disclosures) in the year before the last {QUIET_DAYS} days "
                   f"and none since, per the House Clerk's disclosure index. Silence doesn't say why: the member may have sold off holdings, moved assets into a blind trust "
                   "or index fund, been covered by a new law, or just not traded -- and trades are reportable up to 45 days late, so the latest ones may not be filed yet. "
                   "House members only; the Senate's e-filing search isn't covered. "
                   + (f"{len(fresh) - len(shown)} more members meet the test and will be listed in later editions." if len(fresh) > len(shown) else "")}]
        top_r = roster[shown[0][1]]
        headline = f"Rep. {top_r['name']} filed {shown[0][0]} trade reports last year, none in {shown[0][3]} days"
        paras = [f"{len(shown)} House member(s) with a history of frequent stock trading have stopped filing trade reports."]
        write_extra("officials", "stopped_trading", [section("Stopped Trading", headline, paras, blocks)],
                    f"{len(shown)} prolific House traders have gone quiet for {QUIET_DAYS}+ days")
    else:
        write_extra("officials", "stopped_trading", [], "no newly quiet prolific traders")
    print(f"{len(flagged)} members meet the test ({len(fresh)} not yet reported)", file=sys.stderr)


if __name__ == "__main__":
    main()
