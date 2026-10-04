#!/usr/bin/env python3
"""Deadline Watch -- who DIDN'T file, and who filed late (added 2026-10-03).

Every other DC Record section reports filings that exist. This one checks
filings that should exist against the people obliged to file them, using
data/deadlines.json for the dates. Pure data, no LLM.

--edition officials:
  House + Senate annual financial disclosures for pfd_annual.report_year,
  checked against every sitting member (unitedstates/congress-legislators).
  Reproduces the "Three Republicans Failed to File" story mechanically:
  validated 2026-10-03 -- McCaul and Crenshaw (extension, no report),
  Boebert (filed 9/24, after the Aug. 13 final extension), McBath (9/11).
--edition campaigns:
  fec_checks (e.g. Q3) -- sitting members running this cycle whose principal
  campaign committee has no report of that type on file once it's past due.

Both: "coming due" calendar notes (AM edition only), and the standing
"still missing" lists also print only in the AM edition so the PM edition
doesn't repeat them -- newly-late filings print in both.

Output: extras/<edition>/deadlines.json
Usage: fetch_deadlines.py --edition officials|campaigns [--test-check Q2:2026:2026-07-15:2026-06-30]
"""
import argparse
import csv
import io
import json
import re
import sys
import zipfile
from datetime import date, timedelta

from dc_common import http_get, today_la
from dc_extras import (
    esc, fec_get, fec_key_cycle, link, load_json, lookback as lookback_date, mdY, norm, run_mode,
    section, write_extra,
)

LEGISLATORS_URL = "https://unitedstates.github.io/congress-legislators/legislators-current.json"
HOUSE_ZIP = "https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}FD.zip"
HOUSE_PDF = "https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}/{doc}.pdf"
SENATE = "https://efdsearch.senate.gov"


def current_members():
    status, body = http_get(LEGISLATORS_URL, timeout=60)
    if status >= 400:
        raise RuntimeError(f"legislators-current HTTP {status}")
    out = []
    for l in json.loads(body):
        t = l["terms"][-1]
        nm = l["name"]
        out.append({
            "name": nm.get("official_full") or f"{nm['first']} {nm['last']}",
            "first": nm.get("nickname") or nm["first"], "first_legal": nm["first"], "last": nm["last"],
            "chamber": "House" if t["type"] == "rep" else "Senate",
            "state": t["state"], "district": t.get("district"), "party": (t.get("party") or "?")[0],
            "first_start": l["terms"][0]["start"], "fec_ids": l["id"].get("fec", []),
        })
    return out


def label_member(m):
    title = "Rep." if m["chamber"] == "House" else "Sen."
    where = m["state"] if m["chamber"] == "Senate" else f"{m['state']}-{m['district'] if m['district'] else 'AL'}"
    return f"{title} {m['name']} ({m['party']}-{where})"


def iso_from_mdy(s):
    try:
        mth, d, y = s.strip().split("/")
        return f"{int(y):04d}-{int(mth):02d}-{int(d):02d}"
    except ValueError:
        return None


def days(a, b):
    return (date.fromisoformat(b) - date.fromisoformat(a)).days


# ---------------- House / Senate annual reports ----------------
def house_index(year):
    status, body = http_get(HOUSE_ZIP.format(year=year), timeout=120)
    if status >= 400:
        raise RuntimeError(f"House FD index {year} HTTP {status}")
    z = zipfile.ZipFile(io.BytesIO(body))
    name = next(n for n in z.namelist() if n.endswith(".txt"))
    text = z.read(name).decode("utf-8-sig", errors="replace")
    return list(csv.DictReader(io.StringIO(text), delimiter="\t"))


def match_rows(member, rows, last_key, first_key, state_of):
    """Rows for this member: same state, surname appears in the row's
    last-or-first field (married names file as e.g. 'Arenholz, Ashley
    Hinson'), disambiguated by first name when the state has two filers
    with that surname."""
    ml = norm(member["last"]).split()
    ml = ml[-1] if ml else ""
    cands = [r for r in rows if state_of(r) == member["state"]
             and ml and ml in norm(r[last_key] + " " + r[first_key]).split()]
    if not cands:
        # Fallback: some members file under a different part of their full
        # name (Del. Aumua Amata Coleman Radewagen files as "Amata, Aumua").
        toks = {t for t in norm(member["name"]).split() if len(t) >= 4} - {norm(member["first"])}
        cands = [r for r in rows if state_of(r) == member["state"] and norm(r[last_key]) in toks]
    people = {(norm(r[last_key]), norm(r[first_key])[:3]) for r in cands}
    if len(people) > 1:
        firsts = {norm(member["first"])[:3], norm(member["first_legal"])[:3]}
        narrowed = [r for r in cands if norm(r[first_key])[:3] in firsts]
        cands = narrowed or cands
    return cands


def senate_session():
    import requests  # only the Senate flow needs a cookie session

    s = requests.Session()
    s.headers.update({"User-Agent": "Mozilla/5.0 (research)"})
    r = s.get(f"{SENATE}/search/", timeout=30)
    tok = re.search(r"name=['\"]csrfmiddlewaretoken['\"] value=['\"]([^'\"]+)['\"]", r.text).group(1)
    s.post(f"{SENATE}/search/home/", data={"csrfmiddlewaretoken": tok, "prohibition_agreement": "1"},
           headers={"Referer": f"{SENATE}/search/"}, timeout=30)
    return s


def senate_search(s, start_mdY, filer_types="[1]", report_types="[]", last_name=""):
    rows, start = [], 0
    while True:
        data = {"start": start, "length": 100, "report_types": report_types, "filer_types": filer_types,
                "submitted_start_date": f"{start_mdY} 00:00:00", "submitted_end_date": "",
                "candidate_state": "", "senator_state": "", "office_id": "", "first_name": "",
                "last_name": last_name, "csrfmiddlewaretoken": s.cookies.get("csrftoken")}
        j = s.post(f"{SENATE}/search/report/data/", data=data, timeout=60,
                   headers={"Referer": f"{SENATE}/search/", "X-Requested-With": "XMLHttpRequest"}).json()
        rows.extend(j["data"])
        start += 100
        if start >= j.get("recordsTotal", 0):
            return rows


def senate_label(r):
    return re.sub(r"<[^>]+>", "", r[3]).strip()


def senate_href(r):
    m = re.search(r'href="([^"]+)"', r[3])
    return SENATE + m.group(1) if m else None


def annual_status(cfg, members, lookback, today):
    year = cfg["report_year"]
    due, final_ext = cfg["due"], cfg["final_extension"]
    exempt_after = f"{year}-11-01"  # served < 60 days in the report year -> no annual report owed
    missing, late_new, on_ext, errors = [], [], 0, []

    # House
    try:
        rows = house_index(year)
        house = [m for m in members if m["chamber"] == "House" and m["first_start"] < exempt_after]
        for m in house:
            mine = match_rows(m, rows, "Last", "First", lambda r: (r.get("StateDst") or "")[:2])
            o = sorted((iso_from_mdy(r["FilingDate"]), r["DocID"]) for r in mine if r["FilingType"] == "O")
            x = sorted(iso_from_mdy(r["FilingDate"]) for r in mine if r["FilingType"] == "X")
            if o:
                filed, doc = o[0]
                limit = final_ext if x else due
                if filed and filed > limit and filed >= lookback:
                    late_new.append({"m": m, "filed": filed, "late_by": days(limit, filed), "ext": bool(x),
                                     "url": HOUSE_PDF.format(year=year, doc=doc)})
            elif today > final_ext:
                missing.append({"m": m, "ext": x})
            elif today > due:
                on_ext += 1
    except Exception as e:  # noqa: BLE001
        errors.append(f"House annual-report check failed: {e}")

    # Senate
    try:
        s = senate_session()
        start = f"01/01/{year + 1}"
        rows = senate_search(s, start)
        senators = [m for m in members if m["chamber"] == "Senate" and m["first_start"] < exempt_after]
        for m in senators:
            ml = norm(m["last"]).split()[-1]
            mine = [r for r in rows if ml in norm(r[1] + " " + r[2]).split()]
            annual = sorted((iso_from_mdy(r[4]), senate_href(r)) for r in mine
                            if senate_label(r).startswith("Annual Report")
                            and "Amendment" not in senate_label(r) and "Extension" not in senate_label(r)
                            and ("CY" not in senate_label(r) or f"CY {year}" in senate_label(r)))
            ext = sorted(iso_from_mdy(r[4]) for r in mine if "Due Date Extension" in senate_label(r)
                         and f"for {year}" in senate_label(r))
            if annual:
                filed, href = annual[0]
                limit = final_ext if ext else due
                if filed > limit and filed >= lookback:
                    late_new.append({"m": m, "filed": filed, "late_by": days(limit, filed), "ext": bool(ext), "url": href})
            elif today > final_ext:
                missing.append({"m": m, "ext": ext})
            elif today > due:
                on_ext += 1
    except Exception as e:  # noqa: BLE001
        errors.append(f"Senate annual-report check failed: {e}")

    return missing, late_new, on_ext, errors


# ---------------- FEC report checks ----------------
def fec_check(chk, members, lookback, today, keys):
    """Sitting members running in report_year whose principal committee has
    no report of chk['report_type'] on file."""
    year = chk["report_year"]
    ids = sorted({f for m in members for f in m["fec_ids"]})
    by_fec = {f: m for m in members for f in m["fec_ids"]}
    running = {}  # committee_id -> member
    for i in range(0, len(ids), 50):
        data = fec_get("candidates/search/", {"candidate_id": ids[i:i + 50], "per_page": 100}, keys)
        for c in data.get("results", []):
            if year not in (c.get("election_years") or []):
                continue
            # Senators are listed under their House IDs too; only the ID for
            # the seat they hold now counts.
            m = by_fec.get(c["candidate_id"])
            if not m or c.get("office") != m["chamber"][0]:
                continue
            for pc in c.get("principal_committees") or []:
                if pc.get("designation") == "P" and pc.get("filing_frequency") not in ("T", "A", "D"):
                    running[pc["committee_id"]] = m
    # Any report whose coverage runs through the period end satisfies the
    # check -- under FEC rules a pre-primary (12P) report can stand in for a
    # quarterly that falls near a primary, so matching on report_type alone
    # false-flags e.g. every Arizona/Oklahoma member for Q2 (seen in testing).
    through = chk["covers_through"]
    filed = {}
    cids = sorted(running)
    for i in range(0, len(cids), 20):
        page = 1
        while True:
            data = fec_get("filings/", {"committee_id": cids[i:i + 20], "form_type": "F3", "report_year": year,
                                        "per_page": 100, "page": page, "sort": "receipt_date"}, keys)
            for f in data.get("results", []):
                if (f.get("coverage_end_date") or "")[:10] < through:
                    continue
                prev = filed.get(f["committee_id"])
                rd = (f.get("receipt_date") or "")[:10]
                if not prev or rd < prev["receipt_date"]:
                    filed[f["committee_id"]] = {"receipt_date": rd, "pdf_url": f.get("pdf_url"),
                                                "report": f.get("report_type_full") or f.get("report_type")}
            pages = (data.get("pagination") or {}).get("pages") or 1
            if page >= pages:
                break
            page += 1
    missing = [{"m": running[c], "committee_id": c} for c in cids if c not in filed]
    late_new = [{"m": running[c], "committee_id": c, **filed[c]} for c in cids
                if c in filed and filed[c]["receipt_date"] > chk["due"] and filed[c]["receipt_date"] >= lookback]
    return missing, late_new, len(running)


# ---------------- assembly ----------------
def calendar_block(cal, edition, today):
    soon = [c for c in cal if c["edition"] == edition and today <= c["date"] <= (date.fromisoformat(today) + timedelta(days=14)).isoformat()]
    if not soon:
        return None
    items = [f"<strong>{esc(mdY(c['date']))}</strong> ({(date.fromisoformat(c['date']) - date.fromisoformat(today)).days} days): {esc(c['label'])}" for c in sorted(soon, key=lambda c: c["date"])]
    return [{"type": "subhead", "text": "Coming due"}, {"type": "list", "items": items}]


def new_extensions(lookback, year):
    """Disclosure extensions requested in the window, by members and
    candidates -- the "Letlow, Cassidy Punt Personal Finance Disclosures
    Until After Louisiana Primary" pattern."""
    out = []
    try:
        for r in house_index(year) + house_index(year - 1):
            filed = iso_from_mdy(r.get("FilingDate", "")) or ""
            if r.get("FilingType") == "X" and filed >= lookback:
                who = ("Rep. " if "Hon" in (r.get("Prefix") or "") else "") + f"{r['First']} {r['Last']}"
                role = "member" if "Hon" in (r.get("Prefix") or "") else "House candidate"
                out.append({"who": who, "role": role, "where": r["StateDst"], "filed": filed,
                            "what": f"extension for {r['Year']} report", "url": HOUSE_PDF.format(year=r["Year"], doc=r["DocID"])})
    except Exception as e:  # noqa: BLE001
        out.append({"error": f"House extensions: {e}"})
    try:
        s = senate_session()
        y, m, d = lookback.split("-")
        for r in senate_search(s, f"{m}/{d}/{y}", filer_types="[]"):
            lab = senate_label(r)
            if "Extension" in lab:
                role = re.search(r"\(([^)]+)\)\s*$", r[2] or "")
                out.append({"who": f"{r[0]} {r[1]}", "role": (role.group(1) if role else "filer").lower(), "where": "",
                            "filed": iso_from_mdy(r[4]), "what": lab, "url": senate_href(r)})
    except Exception as e:  # noqa: BLE001
        out.append({"error": f"Senate extensions: {e}"})
    return out


def build_officials(cfg, members, lookback, today, am):
    missing, late_new, on_ext, errors = annual_status(cfg["pfd_annual"], members, lookback, today)
    exts = new_extensions(lookback, int(today[:4]))
    errors += [e["error"] for e in exts if "error" in e]
    exts = [e for e in exts if "error" not in e]
    y = cfg["pfd_annual"]["report_year"]
    blocks, paras = [], []
    if late_new:
        blocks.append({"type": "subhead", "text": f"Late disclosures: CY{y} annual reports filed after the deadline"})
        blocks.append({"type": "list", "items": [
            f"<strong>{esc(label_member(x['m']))}</strong> filed {link('the report', x['url'])} on {mdY(x['filed'])}, "
            f"{x['late_by']} days after the {'final extension date' if x['ext'] else 'May 15 deadline'}"
            f"{' (no extension on record)' if not x['ext'] else ''}."
            for x in late_new]})
    if exts:
        blocks.append({"type": "subhead", "text": "New disclosure extensions"})
        blocks.append({"type": "list", "items": [
            f"<strong>{esc(e['who'])}</strong> ({esc(e['role'])}{', ' + esc(e['where']) if e['where'] else ''}): {link(e['what'], e['url'])}, filed {mdY(e['filed'])}."
            for e in sorted(exts, key=lambda e: (e['role'] != 'member' and 'senator' not in e['role'], e['who']))[:30]]})
        blocks.append({"type": "note", "text": "An extension pushes a financial disclosure back up to 90 days. Worth checking whether it carries the report past the filer's primary or general election."})
        paras.append(f"{len(exts)} new disclosure extension request{'s' if len(exts) != 1 else ''}.")
    if missing and am:
        blocks.append({"type": "subhead", "text": f"Still missing: no CY{y} annual report on file"})
        blocks.append({"type": "list", "items": [
            f"<strong>{esc(label_member(x['m']))}</strong>: "
            + (f"extension{'s' if len(x['ext']) > 1 else ''} filed {', '.join(mdY(e) for e in x['ext'] if e)}; " if x["ext"] else "no extension on record; ")
            + f"{days(cfg['pfd_annual']['final_extension'], today)} days past the {mdY(cfg['pfd_annual']['final_extension'])} final extension date."
            for x in missing]})
        blocks.append({"type": "note", "text": "Checked against every sitting member's filings in the Clerk of the House index and Senate eFD. Members sworn in after Nov. 1 of the report year owe no annual report and are excluded. Spot-check a name before publishing: a filing under an unexpected name spelling can read as missing."})
    cal = calendar_block(cfg.get("calendar", []), "officials", today) if am else None
    if cal:
        blocks.extend(cal)
    if late_new:
        paras.append(f"{len(late_new)} member{'s' if len(late_new) != 1 else ''} filed a CY{y} annual disclosure late in this window.")
    if missing and am:
        paras.append(f"{len(missing)} sitting member{'s' if len(missing) != 1 else ''} still {'have' if len(missing) != 1 else 'has'} no CY{y} annual financial disclosure on file, past every extension.")
    if not blocks:
        return [], "No deadline findings.", errors
    headline = ("Missing, late and delayed financial disclosures" if (late_new or exts or (missing and am)) else "Upcoming filing deadlines")
    summary = "; ".join(paras) or "Coming-due calendar only."
    return [section("Deadline Watch", headline, paras, blocks, no_news=not (late_new or exts or (missing and am)))], summary, errors


def build_campaigns(cfg, members, lookback, today, am, keys, test_check=None):
    blocks, paras, errors = [], [], []
    checks = cfg.get("fec_checks", [])
    if test_check:
        rt, ry, due, through = test_check.split(":")
        checks = [{"report_type": rt, "report_year": int(ry), "due": due, "covers_through": through, "label": rt, "check_days": 9999}]
    for chk in checks:
        due = chk["due"]
        if not (today > due and today <= (date.fromisoformat(due) + timedelta(days=chk.get("check_days", 30))).isoformat()):
            continue
        try:
            missing, late_new, n = fec_check(chk, members, lookback, today, keys)
        except Exception as e:  # noqa: BLE001
            errors.append(f"FEC {chk['report_type']} check failed: {e}")
            continue
        if late_new:
            blocks.append({"type": "subhead", "text": f"Filed late: {chk['label']} reports received after {mdY(due)}"})
            blocks.append({"type": "list", "items": [
                f"<strong>{esc(label_member(x['m']))}</strong>: {link(x.get('report') or 'report', x.get('pdf_url'))} received {mdY(x['receipt_date'])}, {days(due, x['receipt_date'])} days late."
                for x in late_new]})
        if missing and am:
            blocks.append({"type": "subhead", "text": f"No {chk['label']} report (or report covering the same period) on file yet (due {mdY(due)})"})
            blocks.append({"type": "list", "items": [
                f"<strong>{esc(label_member(x['m']))}</strong>: principal campaign committee "
                f"{link(x['committee_id'], 'https://www.fec.gov/data/committee/' + x['committee_id'] + '/')}"
                for x in missing]})
            blocks.append({"type": "note", "text": f"Checked {n} sitting members' principal campaign committees that list {chk['report_year']} as an election year. FEC processing can lag e-filing by a day; check the committee page before publishing."})
            paras.append(f"{len(missing)} sitting member{'s' if len(missing) != 1 else ''}' campaigns had no report covering through {mdY(chk['covers_through'])} on file as of this run.")
        if late_new:
            paras.append(f"{len(late_new)} sitting member{'s' if len(late_new) != 1 else ''}' campaigns filed the {chk['label']} report late.")
    cal = calendar_block(cfg.get("calendar", []), "campaigns", today) if am else None
    if cal:
        blocks.extend(cal)
    if not blocks:
        return [], "No deadline findings.", errors
    has_news = bool(paras)
    return ([section("Deadline Watch", "Missing and late FEC reports" if has_news else "Upcoming FEC deadlines", paras, blocks, no_news=not has_news)],
            "; ".join(paras) or "Coming-due calendar only.", errors)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--edition", required=True, choices=["officials", "campaigns"])
    ap.add_argument("--test-check", help="REPORT_TYPE:YEAR:DUE:COVERS_THROUGH -- run one FEC check regardless of the calendar (testing)")
    args = ap.parse_args()
    cfg = load_json("data/deadlines.json", {})
    today, lookback, am = today_la(), lookback_date(), run_mode() == "am"
    try:
        members = current_members()
    except Exception as e:  # noqa: BLE001
        write_extra(args.edition, "deadlines", [], f"Deadline Watch unavailable: {e}")
        return
    if args.edition == "officials":
        sections, summary, errors = build_officials(cfg, members, lookback, today, am)
    else:
        sections, summary, errors = build_campaigns(cfg, members, lookback, today, am, fec_key_cycle(), args.test_check)
    if errors:
        print("errors: " + " | ".join(errors), file=sys.stderr)
        summary += " Errors: " + " | ".join(errors)
        if sections:
            sections[0]["blocks"].append({"type": "note", "text": "Partial check: " + esc(" | ".join(errors))})
    write_extra(args.edition, "deadlines", sections, summary)


if __name__ == "__main__":
    main()
