#!/usr/bin/env python3
"""Watchdogs (added 2026-11-06): new inspector general reports (oversight.gov), GAO reports, CBO publications,
House Ethics Committee statements and Office of Congressional Conduct (OCC) referrals.

Deterministic, no LLM. Everything new from Ethics, OCC and GAO is listed; IG reports and CBO scores are high volume,
so only items matching WATCH or naming a sitting member are listed and the rest are counted.
GAO's bid-protest pages refuse scripted access (Access Denied), so bid protests are not covered.

Seen state: data/watchdogs-seen.json. `--seed` marks everything currently listed as seen without reporting it.
Output: extras/officials/watchdogs.json
"""
import html as htmllib
import json
import os
import re
import sys
import xml.etree.ElementTree as E
from email.utils import parsedate_to_datetime

from dc_common import http_get, load_manifest, save_manifest, today_la
from dc_extras import esc, link, mdY, section, write_extra
from members import house_roster

SEEN = "data/watchdogs-seen.json"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"}
IG_PAGES = 5
WATCH = re.compile(r"\b(contract(or|ing|s)?|procurement|sole.source|no.bid|fraud|bribe|kickback|conflicts? of interest|whistleblower|retaliat\w+|misconduct|"
                   r"ICE|Immigration and Customs|detention|deport\w+|Border Patrol|DOGE|Epstein|Maxwell|White House|Secret Service|FEMA|"
                   r"Iran|Golden Dome|munitions|Pentagon|Defense Logistics|tariffs?|crypto\w*|Qatar|pardon\w*|grant terminations?)\b", re.I)


def get(url):
    status, body = http_get(url, headers=UA, timeout=60)
    if status >= 400:
        raise RuntimeError(f"{url} -> HTTP {status}")
    return body.decode("utf-8", "replace")


def plain(s):
    return re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


def rss(url):
    out = []
    for i in E.fromstring(get(url)).findall(".//item"):
        try:
            d = parsedate_to_datetime(i.findtext("pubDate")).date().isoformat()
        except Exception:  # noqa: BLE001
            d = ""
        out.append({"key": i.findtext("link"), "title": plain(i.findtext("title")), "url": i.findtext("link"), "date": d})
    return out


def ig():
    out = []
    for p in range(IG_PAGES):
        t = get(f"https://www.oversight.gov/reports/federal?page={p}")
        for row in re.findall(r'<tr class="listing-table__row table-row">(.*?)</tr>', t, re.S):
            cell = lambda name: plain((re.search(rf'views-field-{name}"[^>]*>(.*?)</td>', row, re.S) or [None, ""])[1])  # noqa: E731
            href = re.search(r'href="(/reports/[^"]+)"', row)
            date = re.search(r'datetime="(\d{4}-\d\d-\d\d)', row)
            if href:
                u = "https://www.oversight.gov" + href.group(1)
                out.append({"key": u, "title": cell("title"), "url": u, "date": date.group(1) if date else "",
                            "agency": cell("field-report-agency-reviewed"), "kind": cell("field-report-type")})
    return out


def listing(url, base, pattern):
    t = get(url)
    seen, out = set(), []
    for href, title in re.findall(r'<a [^>]*href="([^"]+)"[^>]*>\s*([^<]{8,240}?)\s*</a>', t, re.S):
        if not re.search(pattern, href):
            continue
        u = href if href.startswith("http") else base + href
        if u not in seen:
            seen.add(u)
            out.append({"key": u, "title": plain(title), "url": u, "date": ""})
    return out


def main():
    seed = "--seed" in sys.argv
    errors, src = [], {}
    for name, fn in (("ig", ig),
                     ("gao", lambda: rss("https://www.gao.gov/rss/reports.xml")),
                     ("cbo", lambda: rss("https://www.cbo.gov/publications/all/rss.xml")),
                     ("ethics", lambda: listing("https://ethics.house.gov/press-releases", "https://ethics.house.gov", r"/press-releases/[a-z0-9-]+/?$")),
                     ("occ", lambda: listing("https://conduct.house.gov/reports/investigations", "https://conduct.house.gov", r"/reports/investigations/[a-z0-9-]+$"))):
        try:
            src[name] = fn()
        except Exception as e:  # noqa: BLE001
            src[name] = []
            errors.append(f"{name}: {e}")
    seen = load_manifest(SEEN)
    if seed:
        for items in src.values():
            for it in items:
                seen[it["key"]] = {"reported": today_la()}
        save_manifest(SEEN, seen)
        print("seeded", {k: len(v) for k, v in src.items()}, "errors", errors)
        return
    new = {k: [i for i in v if i["key"] not in seen] for k, v in src.items()}
    names = []
    try:
        names = [re.escape(m["name"]) for m in house_roster().values() if m.get("name")]
    except Exception as e:  # noqa: BLE001
        errors.append(f"roster: {e}")
    member_rx = re.compile("|".join(names)) if names else None
    def hit(i):
        return bool(WATCH.search(i["title"]) or (member_rx and member_rx.search(i["title"])))
    def line(i, tail=""):
        return link(i["title"], i["url"]) + tail + (f" ({mdY(i['date'])})" if i["date"] else "")
    blocks, paras = [], []
    if new["occ"]:
        blocks += [{"type": "subhead", "text": "Office of Congressional Conduct: new referrals"}, {"type": "list", "items": [line(i) for i in new["occ"]]}]
    if new["ethics"]:
        blocks += [{"type": "subhead", "text": "House Ethics Committee: new statements"}, {"type": "list", "items": [line(i) for i in new["ethics"]]}]
    ig_hit = [i for i in new["ig"] if hit(i)]
    if ig_hit:
        blocks += [{"type": "subhead", "text": "Inspectors general: matched reports"},
                   {"type": "list", "items": [line(i, f". {esc(i.get('agency'))}, {esc(i.get('kind'))}") for i in ig_hit]}]
    gao = [i for i in new["gao"] if "/products/gao-" in i["url"]]  # skips letters on auditing standards and similar
    if gao:
        blocks += [{"type": "subhead", "text": "GAO: new reports"}, {"type": "list", "items": [line(i) for i in gao]}]
    cbo_hit = [i for i in new["cbo"] if hit(i) or not re.match(r"(H\.R\.|S\.|H\.J\.|S\.J\.)", i["title"])]
    if cbo_hit:
        blocks += [{"type": "subhead", "text": "CBO: new reports"}, {"type": "list", "items": [line(i) for i in cbo_hit]}]
    rest = len(new["ig"]) - len(ig_hit)
    if rest or len(new["cbo"]) > len(cbo_hit):
        blocks.append({"type": "note", "text": f"Not listed: {rest} other new inspector general report(s) and {len(new['cbo']) - len(cbo_hit)} CBO bill score(s). "
                       + link("All IG reports", "https://www.oversight.gov/reports/federal") + "."})
    n = sum(len(v) for v in new.values())
    if len(blocks) > (1 if rest or len(new["cbo"]) > len(cbo_hit) else 0):
        top = (new["occ"] or new["ethics"] or ig_hit or gao or cbo_hit or [{"title": "New watchdog reports"}])[0]["title"]
        paras = [f"{len(new['occ'])} OCC referral(s), {len(new['ethics'])} House Ethics statement(s), {len(ig_hit)} matched IG report(s), {len(gao)} GAO report(s)."]
        if errors:
            paras.append("Could not check: " + esc("; ".join(errors)) + ".")
        write_extra("officials", "watchdogs", [section("Watchdogs", top, paras, blocks)], f"watchdogs: {n} new; lead: {top}")
    else:
        write_extra("officials", "watchdogs", [], "no new watchdog reports" + (f"; errors: {errors}" if errors else ""))
    for items in new.values():
        for it in items:
            seen[it["key"]] = {"reported": today_la()}
    save_manifest(SEEN, seen)
    if errors:
        print("ERRORS: " + "; ".join(errors), file=sys.stderr)


if __name__ == "__main__":
    main()
