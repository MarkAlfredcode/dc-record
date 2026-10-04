"""Running ledger of late-disclosure (STOCK Act) filings DC Record has
flagged, per filer, so a new flag can say "third late filing flagged since
<date>" -- the "Violated the STOCK Act -- Again" / "for the Second Time This
Year" angle. Persisted in data/stock-act-ledger.json (committed; reverted on
site-refresh-only runs like the seen-manifests). Counts only filings this
pipeline has flagged since the ledger started, and says so.
"""
import json

from dc_common import today_la

PATH = "data/stock-act-ledger.json"


class Ledger:
    def __init__(self, path=PATH):
        self.path = path
        try:
            with open(path) as f:
                self.data = json.load(f)
        except FileNotFoundError:
            self.data = {}

    def record(self, filer, filing_url, filed, n_late):
        """Returns the filer's history BEFORE this filing: (count, first_date)."""
        entries = self.data.setdefault(filer, [])
        prior = [e for e in entries if e["url"] != filing_url]
        if not any(e["url"] == filing_url for e in entries):
            entries.append({"url": filing_url, "filed": filed, "n_late": n_late, "flagged": today_la()})
        first = min((e["filed"] for e in prior), default=None)
        return len(prior), first

    def save(self):
        with open(self.path, "w") as f:
            json.dump(self.data, f, indent=1, sort_keys=True)
            f.write("\n")


def history_phrase(prior_count, first_date):
    if not prior_count:
        return ""
    from dc_extras import mdY
    nth = {1: "second", 2: "third", 3: "fourth", 4: "fifth"}.get(prior_count, f"{prior_count + 1}th")
    return f" This is the {nth} filing with late trades DC Record has flagged for this filer since {mdY(first_date)}."
