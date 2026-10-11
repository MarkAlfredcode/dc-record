#!/usr/bin/env python3
"""Sorts fetch_usaspending.py's $2M+ pull -- new awards, large modifications
and cuts -- into what the main pass writes up and what it doesn't
(added 2026-10-11, with the Defense backlog sweep and modifications: a
weekday can now bring 100+ new $2M+ awards, too many to hand the main pass
one by one).

Always featured (no screen): new awards of $25M+, modifications of +$100M
or more, cuts of -$25M or more, every termination, and anything awarded by
an Executive Office of the President component. Everything else is tiered
by Haiku (usaspending_common.screen) plus the regex safety net; flagged
ones are featured too.

Nothing is dropped: the unflagged remainder is listed, every award linked,
in a deterministic extras section ("USAspending: Every Other New Award",
extras/feds/usaspending_routine.json) that assemble.py appends -- the main
pass no longer spends output tokens naming routine awards.

Output: data/usaspending_triage.json (the main pass reads this instead of
data/usaspending_candidates.json; it falls back to that file if this one
is missing or the screen failed).
"""
import re
import sys
from collections import defaultdict

import usaspending_common as uc
from dc_extras import esc, link, load_json, mdY, section, write_extra
from dc_common import write_candidates

IN_PATH = "data/usaspending_candidates.json"
OUT_PATH = "data/usaspending_triage.json"
FEATURE_AWARD = 25_000_000
FEATURE_INCREASE = 100_000_000
FEATURE_CUT = 25_000_000
EOP_RE = re.compile(
    r"executive office of the president|white house|office of management and budget|national security council|"
    r"office of the vice president|office of administration|office of science and technology policy|"
    r"council of economic advisers|council on environmental quality|office of national drug control policy|"
    r"united states trade representative", re.IGNORECASE)


def usd(v):
    v = v or 0
    return f"-${abs(v):,.0f}" if v < 0 else f"${v:,.0f}"


SUFFIX_CASE = {"Llc": "LLC", "Lp": "LP", "Llp": "LLP", "Usa": "USA", "Inc": "Inc", "Jv": "JV", "Pllc": "PLLC",
               "Ii": "II", "Iii": "III", "Us": "US"}
SMALL_WORDS = {"Of", "The", "And", "For", "In", "On", "At", "To", "A", "An"}


def name(s):
    """'CHEVRON USA INC.' -> 'Chevron USA Inc.' (USAspending files names in capitals)."""
    words = (s or "?").title().split(" ")
    out = [SUFFIX_CASE.get(w.rstrip(".,"), w.rstrip(".,")) + w[len(w.rstrip(".,")):] for w in words]
    return " ".join(w.lower() if i and w in SMALL_WORDS else w for i, w in enumerate(out))


def size_reason(r, kind):
    amt = r.get("transaction_amount") or 0
    if EOP_RE.search(f"{r.get('awarding_agency') or ''} {r.get('awarding_sub_agency') or ''}"):
        return "white_house"
    if kind == "award" and amt >= FEATURE_AWARD:
        return "size"
    if kind == "modification" and amt >= FEATURE_INCREASE:
        return "size"
    if kind == "cut" and (amt <= -FEATURE_CUT or r.get("termination")):
        return "termination" if r.get("termination") else "size"
    return None


def routine_section(rest):
    """Every unfeatured award, grouped by agency, one list item per agency."""
    by = defaultdict(list)
    for kind, r in rest:
        # Defense is split by service/component, or it's one 40-link line
        agency = r.get("awarding_agency") or "Unknown agency"
        if uc.is_defense(r) and r.get("awarding_sub_agency"):
            agency = r["awarding_sub_agency"]
        by[(kind, agency)].append(r)
    titles = {"award": "new awards", "modification": "modifications", "cut": "cuts"}
    items = []
    for kind in ("award", "modification", "cut"):
        groups = sorted(((a, rs) for (k, a), rs in by.items() if k == kind),
                        key=lambda g: -sum(abs(r.get("transaction_amount") or 0) for r in g[1]))
        for agency, rs in groups:
            rs.sort(key=lambda r: -abs(r.get("transaction_amount") or 0))
            total = sum(r.get("transaction_amount") or 0 for r in rs)
            links = []
            for r in rs:
                label = f"{name(r.get('recipient'))} {usd(r.get('transaction_amount'))}"
                if r.get("sweep") == "defense_backlog":
                    label += f", signed {mdY(r.get('action_date') or '')}"
                links.append(link(label, r.get("url")))
            n = len(rs)
            items.append(f"<strong>{esc(agency)}</strong> — {n} {titles[kind] if n != 1 else titles[kind][:-1]}, "
                         f"{usd(total)}: " + "; ".join(links))
    if not items:
        return []
    n = len(rest)
    paras = [f"{n} more {'item' if n == 1 else 'items'} from today's USAspending pull that the screen judged routine "
             f"(spare parts, fuel, maintenance, option exercises, closeout de-obligations and the like), listed so "
             f"nothing is dropped. Defense Department awards are published about 90 days after signing; those "
             f"show the signing date."]
    return [section("USAspending: Every Other New Award", f"{n} routine contract actions, all linked",
                    paras, [{"type": "list", "items": items}], no_news=True)]


def main():
    cand = load_json(IN_PATH, {}) or {}
    if cand.get("skipped"):
        write_candidates(OUT_PATH, {"skipped": cand["skipped"], "reachable": True, "errors": []})
        write_extra("feds", "usaspending_routine", [], "USAspending skipped in PM (carried over from the AM edition).")
        return
    pool = ([("award", r) for r in cand.get("new_awards", [])]
            + [("modification", r) for r in cand.get("modifications", [])]
            + [("cut", r) for r in cand.get("cuts", [])])
    featured, to_screen = [], []
    for kind, r in pool:
        why = size_reason(r, kind)
        if why:
            featured.append((kind, {**r, "triage_tier": why, "triage_why": ""}))
        else:
            to_screen.append((kind, r))

    rows = [r for _, r in to_screen]
    tiers, unscreened, stats, errors = uc.screen(
        rows, "newly reported federal contract actions of $2 million or more: new awards, large modifications, "
              "and cuts (negative amounts)", rate="5%", cap=12)
    rest = []
    for i, (kind, r) in enumerate(to_screen):
        if i in tiers:
            featured.append((kind, {**r, "triage_tier": tiers[i]["tier"], "triage_why": tiers[i]["why"]}))
        elif i in unscreened:
            featured.append((kind, {**r, "triage_tier": "unscreened", "triage_why": "screening batch failed"}))
        else:
            rest.append((kind, r))

    order = {"white_house": 0, "priority": 1, "termination": 2, "size": 3, "notable": 4, "unscreened": 5, "keyword": 6}

    def pick(kind):
        xs = [r for k, r in featured if k == kind]
        return sorted(xs, key=lambda r: (order[r["triage_tier"]], -abs(r.get("transaction_amount") or 0)))

    out = {
        "reachable": cand.get("reachable", True),
        "window": cand.get("window"), "thresholds": cand.get("thresholds"),
        "featured_awards": pick("award"),
        "featured_modifications": pick("modification"),
        "featured_cuts": pick("cut"),
        "routine_counts_by_agency": {},
        "executive_order_citations": uc.eo_citations([r for _, r in pool]),
        "errors": cand.get("errors", []) + errors,
    }
    rc = defaultdict(lambda: {"awards": 0, "modifications": 0, "cuts": 0})
    for kind, r in rest:
        rc[uc.short_agency(r.get("awarding_agency"))][{"award": "awards", "modification": "modifications",
                                                      "cut": "cuts"}[kind]] += 1
    out["routine_counts_by_agency"] = dict(sorted(rc.items(), key=lambda kv: -sum(kv[1].values())))
    c = cand.get("counts", {})
    out["counts"] = {
        "new_awards": len(cand.get("new_awards", [])),
        "new_defense_backlog": c.get("new_defense_backlog", 0),
        "new_modifications": len(cand.get("modifications", [])),
        "new_cuts": len(cand.get("cuts", [])),
        "featured": len(featured), "routine": len(rest),
        "batches": stats["batches"], "screened_ok": stats["screened_ok"],
        "haiku_cost_usd": stats["haiku_cost_usd"],
    }
    print(f"USAspending $2M+ triage: {out['counts']}", file=sys.stderr)
    write_candidates(OUT_PATH, out)
    write_extra("feds", "usaspending_routine", routine_section(rest),
                f"{len(rest)} routine USAspending actions listed mechanically after the main section.")


if __name__ == "__main__":
    main()
