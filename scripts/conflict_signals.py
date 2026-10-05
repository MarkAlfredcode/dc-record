"""Rolling ledger of conflict-of-interest signals for the web-only Conflicts
edition (added 2026-10-04). Every Conflicts scanner records one entry per
finding here -- who it's about (a member/official), which company it
touches, what kind of signal, and the date of the underlying event -- so
build_conflicts.py can rank people and companies that show up in more than
one scanner, which no single run can see.

Persisted in data/conflict-signals.json (committed; keeps 120 days).
"""
import json
import os
import re

from dc_common import days_ago_la, today_la
from dc_extras import norm

PATH = "data/conflict-signals.json"
KEEP_DAYS = 120

KIND_LABELS = {
    "late_trade": "late trade disclosure",
    "event_timed": "trade before a market-moving event",
    "oversight_trade": "trade in own committee's jurisdiction",
    "trade_award": "trade near a federal contract award",
    "donor_award": "donor-linked contract award",
}

_SUFFIX = {"inc", "incorporated", "corp", "corporation", "co", "company", "ltd", "limited", "plc", "llc", "lp", "holdings",
           "holding", "group", "the", "class", "common", "stock", "ordinary", "shares", "sa", "nv", "ag", "de", "new"}


def company_key(name):
    """Normalized company name for matching across scanners: lowercase, no
    punctuation, no corporate suffixes ("Lockheed Martin Corporation" and
    "LOCKHEED MARTIN CORP" both -> "lockheed martin")."""
    s = re.sub(r"\[[A-Z]{1,3}\]|\([A-Z.\-]{1,7}\)", " ", name or "")
    toks = [t for t in norm(s).split() if t not in _SUFFIX]
    return " ".join(toks)


class Signals:
    def __init__(self, path=PATH):
        self.path = path
        try:
            with open(path) as f:
                self.rows = json.load(f)
        except (FileNotFoundError, ValueError):
            self.rows = []
        self.index = {r["key"] for r in self.rows}

    def add(self, key, kind, who, date, detail, url="", company=None):
        if key in self.index:
            return False
        self.index.add(key)
        self.rows.append({"key": key, "kind": kind, "who": who, "company": company_key(company) if company else None,
                          "date": (date or today_la())[:10], "detail": detail, "url": url, "recorded": today_la()})
        return True

    def save(self):
        if os.environ.get("DC_NO_SAVE"):
            return
        cutoff = days_ago_la(KEEP_DAYS)
        self.rows = [r for r in self.rows if r["date"] >= cutoff]
        with open(self.path, "w") as f:
            json.dump(self.rows, f, indent=1)
            f.write("\n")
