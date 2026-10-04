"""Oversight-area lookups for trade checks (added 2026-10-03): which sectors
a member's committees, or an official's agency, oversee (data/jurisdiction.json),
and whether a given trade falls in one of them.

Committee assignments come from unitedstates/congress-legislators
(committee-membership-current.json, keyed by thomas_id -> members with
bioguide ids), matched to filers by surname + state (House) or surname +
first name (Senate, whose eFD rows carry no state).
"""
import json
import re

from dc_common import http_get
from dc_extras import load_json, norm

BASE = "https://unitedstates.github.io/congress-legislators/"


class Jurisdiction:
    def __init__(self):
        cfg = load_json("data/jurisdiction.json", {}) or {}
        self.committee_sectors = cfg.get("committees", {})
        self.agency_sectors = cfg.get("agencies", {})
        sectors = dict((load_json("data/events.json", {}) or {}).get("sectors", {}))
        sectors.update(cfg.get("extra_sectors", {}))
        self.matchers = {
            name: (re.compile(r"\b(?:" + "|".join(s["keywords"]) + r")", re.I) if s.get("keywords") else None, set(s.get("tickers", [])))
            for name, s in sectors.items()
        }
        self._members = None
        self._committee_names = {}

    def _load_members(self):
        if self._members is not None:
            return
        self._members = []
        try:
            st1, b1 = http_get(BASE + "legislators-current.json", timeout=60)
            st2, b2 = http_get(BASE + "committee-membership-current.json", timeout=60)
            st3, b3 = http_get(BASE + "committees-current.json", timeout=60)
            if max(st1, st2, st3) >= 400:
                return
        except Exception:  # noqa: BLE001
            return
        self._committee_names = {c["thomas_id"]: c["name"] for c in json.loads(b3)}
        member_of = {}
        for cid, members in json.loads(b2).items():
            if cid in self.committee_sectors:  # full committees only
                for m in members:
                    member_of.setdefault(m.get("bioguide"), set()).add(cid)
        for l in json.loads(b1):
            t = l["terms"][-1]
            self._members.append({"last": norm(l["name"]["last"]).split()[-1], "first": norm(l["name"].get("nickname") or l["name"]["first"])[:3],
                                  "first_legal": norm(l["name"]["first"])[:3], "state": t["state"], "chamber": t["type"],
                                  "committees": sorted(member_of.get(l["id"]["bioguide"], []))})

    def member_committees(self, last, first, state=None):
        self._load_members()
        ln = norm(last).split()
        ln = [t for t in ln if t not in ("ii", "iii", "jr", "sr")]
        if not ln:
            return []
        fn = norm(first)[:3]
        cands = [m for m in self._members if m["last"] in ln and (state is None or m["state"] == state)]
        if len(cands) > 1:
            cands = [m for m in cands if fn in (m["first"], m["first_legal"])] or cands
        return cands[0]["committees"] if len(cands) == 1 else []

    def committee_name(self, cid):
        return self._committee_names.get(cid, cid)

    def sectors_for_committees(self, cids):
        return {s: c for c in cids for s in self.committee_sectors.get(c, [])}

    def sectors_for_agency(self, agency):
        a = (agency or "").lower()
        return sorted({s for k, secs in self.agency_sectors.items() if k.lower() in a for s in secs})

    def trade_sector(self, trade, sectors):
        for s in sectors:
            rx, tickers = self.matchers.get(s, (None, set()))
            if (trade.get("ticker") and trade["ticker"] in tickers) or (rx and rx.search(trade.get("asset") or "")):
                return s
        return None
