#!/usr/bin/env python3
"""Every-10-days events-calendar refresh, step 3 of 3 (added 2026-10-03).

Merges Claude's picks (events_additions.json, written by the prompt-events.txt
pass) into data/events.json. The model only chooses WHICH candidates matter
and how to tag them; each event's date, source URL and timeline text are
copied here from data/wiki_candidates.json by candidate id, so a hallucinated
date or link can't get in. Anything malformed is skipped and reported.

Writes events_summary.txt (one line per added event, plus Claude's notes)
for the commit message and ntfy push.
"""
import json
import re
import sys

EVENTS = "data/events.json"
CANDS = "data/wiki_candidates.json"
ADDS = "events_additions.json"
SUMMARY = "events_summary.txt"


def main():
    events = json.load(open(EVENTS))
    cands = {c["id"]: c for c in json.load(open(CANDS))["candidates"]}
    try:
        adds = json.load(open(ADDS))
    except (FileNotFoundError, json.JSONDecodeError) as e:
        open(SUMMARY, "w").write(f"No usable additions file ({e}); events.json unchanged.\n")
        return
    sectors = events["sectors"]
    problems, added = [], []

    for name, spec in (adds.get("new_sectors") or {}).items():
        if not re.fullmatch(r"[a-z][a-z0-9_]{2,30}", name) or name in sectors:
            problems.append(f"new sector '{name}' rejected (bad name or exists)")
            continue
        kws = [k for k in spec.get("keywords", []) if isinstance(k, str) and 2 <= len(k) <= 40]
        tks = [t for t in spec.get("tickers", []) if isinstance(t, str) and re.fullmatch(r"[A-Z.]{1,6}", t)]
        if kws or tks:
            sectors[name] = {"keywords": kws, "tickers": tks}

    existing = {(e["date"], e["label"].lower()) for e in events["events"]}
    for a in adds.get("events", []):
        c = cands.get(a.get("candidate_id"))
        if not c:
            problems.append(f"unknown candidate_id {a.get('candidate_id')!r}")
            continue
        label = (a.get("label") or "").strip()[:140]
        secs = [s for s in a.get("sectors", []) if s in sectors]
        kws = [k for k in a.get("keywords", []) if isinstance(k, str) and 2 <= len(k) <= 40]
        tks = [t for t in a.get("tickers", []) if isinstance(t, str) and re.fullmatch(r"[A-Z.]{1,6}", t)]
        any_asset = bool(a.get("any_asset"))
        if not label or not (secs or kws or tks or any_asset):
            problems.append(f"{c['id']}: no label or nothing to match on")
            continue
        for bad in kws:
            try:
                re.compile(bad)
            except re.error:
                problems.append(f"{c['id']}: bad keyword regex {bad!r}")
                kws.remove(bad)
        if (c["date"], label.lower()) in existing:
            continue
        ev = {"label": label, "date": c["date"], "sectors": secs,
              "window_before_days": max(1, min(int(a.get("window_before_days", 14)), 30)),
              "window_after_days": max(0, min(int(a.get("window_after_days", 3)), 10)),
              "source": c.get("url") or c["page"], "timeline": c["page"], "timeline_text": c["text"][:300],
              "added_by": "every-10-days refresh"}
        if kws:
            ev["keywords"] = kws
        if tks:
            ev["tickers"] = tks
        if any_asset:
            ev["any_asset"] = True
        events["events"].append(ev)
        existing.add((c["date"], label.lower()))
        added.append(ev)

    events["events"].sort(key=lambda e: e.get("date") or "9999")
    with open(EVENTS, "w") as f:
        json.dump(events, f, indent=1)
        f.write("\n")
    lines = [f"Added {len(added)} event(s) to the calendar."]
    lines += [f"- {e['date']}: {e['label']} [{', '.join(e['sectors'] + e.get('keywords', [])[:3]) or 'market-wide'}]" for e in added]
    if adds.get("notes"):
        lines += ["", "Notes: " + str(adds["notes"])[:1500]]
    if problems:
        lines += ["", "Skipped: " + "; ".join(problems[:20])]
    open(SUMMARY, "w").write("\n".join(lines) + "\n")
    print("\n".join(lines), file=sys.stderr)


if __name__ == "__main__":
    main()
