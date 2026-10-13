#!/usr/bin/env python3
"""Finds the President, the Vice President and cabinet-level officials in
filing text (added 2026-10-12): FARA supplemental statements log every text,
call, email and meeting an agent had for a foreign principal, and
informational materials include letters to officials. The roster, with
tenures and prior titles, is data/top_officials.json.

scan(text) returns one hit per matching line:
  {"who": "Scott Bessent", "role": ..., "tier": "name"|"title"|"office",
   "method": "text"|"email"|"call"|"meeting"|"letter"|"",
   "snippet": the line with one line of context either side}
  name    the person is named: full name, "Last, First", title + surname
          ("Sec. Lutnick", "Amb. Greer"), or a distinctive surname alone
  title   POTUS / VPOTUS / "President of the United States" / "the Vice
          President" without a name (lines that record a communication only)
  office  their immediate office: Office of the Vice President / OVP, Oval
          Office, Office of the Secretary of <department> (communication
          lines only; staff contacts, the model keeps the ones about the
          principal)

Backtest 2026-10-12 on story-engines' 3,831 cached FARA filings (2025-26):
the unnamed "Secretary of State"/"Attorney General"/"U.S. Trade
Representative" patterns were nearly all noise (Deputy Assistant
Secretaries split across OCR lines, FARA boilerplate "filed with the
Attorney General", agreement narrative) and were dropped; contact logs name
the person.

  python3 scripts/top_officials.py <text file>...   prints the hits
"""
import json
import os
import re
import sys

ROSTER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "top_officials.json")
SNIPPET_CHARS = 420
TITLES = ["Secretary", "Sec.", "Director", "Administrator", "Ambassador", "Amb.", "Attorney General",
          "Chief of Staff", "CoS", "President", "Vice President", "The Honorable", "Hon."]

# Methods, most specific first: texts are what the reader asked to see first.
METHODS = [
    ("text", r"\btext(?:ed|s|ing| message| messages| exchange)?\b|\bsms\b|i ?message|whats ?app|\bsignal\b"),
    ("email", r"e-?mail"),
    ("call", r"\b(?:phone|tele(?:phone|con)|call(?:ed|s)?|video ?conference|zoom|teams|virtual)\b"),
    ("meeting", r"\b(?:meet(?:ing|s)?|met|in[- ]person|dinner|lunch|breakfast|reception|visit)\b"),
    ("letter", r"\b(?:letter|dear|correspondence|memorandum)\b"),
]
METHODS = [(m, re.compile(p, re.I)) for m, p in METHODS]

# Matched against the previous line + this one, so a title broken across an
# OCR line ("Deputy Assistant / Secretary of War") still reads as one.
NOT_PRINCIPAL = r"(?<!to the )(?<!Deputy )(?<!Assistant )(?<!Under )(?<!Associate )(?<!Principal )(?<!Advisor )"
DEPTS = (r"State|the Treasury|Treasury|Defense|War|the Interior|Interior|Agriculture|Commerce|Labor|"
         r"Health and Human Services|Housing and Urban Development|Transportation|Energy|Education|"
         r"Veterans Affairs|Homeland Security")
TITLE_PATTERNS = [
    ("President", r"\bPOTUS\b|" + NOT_PRINCIPAL + r"\bPresident of the United States\b"),
    ("Vice President", r"\bVPOTUS\b|" + NOT_PRINCIPAL + r"(?<!Office of )\b(?:the|U\.S\.) Vice President\b(?! of)"
                       r"(?!,? (?:Government|Public|Policy|Strategic|Global|Corporate|External|Legal|Operations|"
                       r"Communications|Business|International|Federal|Sales))"),
]
OFFICE_PATTERNS = [
    ("Vice President", r"\bOffice of the Vice President\b|\bOVP\b"),
    ("President", r"\bOval Office\b"),
    ("cabinet secretary", r"(?<!Deputy )(?<!Under )(?<!Assistant )\bOffice of the Secretary of (?:" + DEPTS + r")\b"),
]
TITLE_PATTERNS = [(post, re.compile(rx)) for post, rx in TITLE_PATTERNS]
OFFICE_PATTERNS = [(post, re.compile(rx)) for post, rx in OFFICE_PATTERNS]
# Every title/office pattern contains one of these; other lines skip them.
TKEY = re.compile(r"POTUS|President|OVP|Oval Office|Office of the Secretary")
# A member of Congress tag right after the name: "Marco Rubio (R-FL)".
HILL_TAG = re.compile(r"^\W{0,3}\(?\s*[RDI]\s?-\s?[A-Z]{2}\b")
# Item 14 political contributions: "Michael waltz for Congress Credit Card $ 105.00".
CONTRIBUTION = re.compile(r"\$\s?[\d,]+\.\d\d|\b(?:for Congress|for Senate|Victory Fund|Credit Card|Check)\b", re.I)


def _rx(phrase):
    """Phrase -> regex tolerant of OCR spacing and line breaks, with an
    optional trailing period on abbreviations."""
    words = phrase.split()
    return r"\s+".join(re.escape(w.rstrip(".")) + (r"\.?" if w.endswith(".") else "") for w in words)


def load_roster(path=ROSTER):
    with open(path) as f:
        return json.load(f)


def compile_roster(roster):
    people = []
    for p in roster["people"]:
        surname = re.sub(r",?\s+Jr\.?$", "", p["name"]).split()[-1]
        names = list(p.get("full") or [])
        names.append(f"{surname}, {p['name'].split()[0]}")
        names += p.get("titled") or []
        names += [f"{t} {surname}" for t in TITLES]
        alts = sorted({_rx(n) for n in names}, key=len, reverse=True)
        if p.get("bare"):
            alts.append(_rx(p["bare"]))
        tail = "".join(rf"(?!,?\s*{re.escape(x)})" for x in p.get("not_after") or [])
        rx = re.compile(r"(?<![\w-])(?:" + "|".join(alts) + r")(?![\w-])" + tail, re.I)
        # "Sen. Markwayne Mullin", "office of Senator [other column] / Markwayne
        # Mullin": a prior title anywhere in the three-line context, with
        # nothing there from the cabinet-era post.
        prior = None
        if p.get("prior"):
            prior = re.compile(r"(?<!\w)(?:" + "|".join(_rx(t.rstrip(".")) + r"\.?" for t in p["prior"])
                               + r")(?!\w)", re.I)
        current = re.compile(p["current"]) if p.get("current") else None
        people.append({"who": p["name"], "role": p["role"], "years": p["years"], "rx": rx, "prior": prior,
                       "current": current,
                       "key": surname.lower(), "alt_key": "rfk" if "RFK" in " ".join(names) else None})
    return people


_PEOPLE = None
_KEY = None


def people():
    """Compiled roster, plus a one-pass prefilter: every pattern for a person
    contains their surname (or RFK)."""
    global _PEOPLE, _KEY
    if _PEOPLE is None:
        _PEOPLE = compile_roster(load_roster())
        keys = {p["key"] for p in _PEOPLE} | {p["alt_key"] for p in _PEOPLE if p["alt_key"]}
        _KEY = re.compile("|".join(re.escape(k) for k in sorted(keys)), re.I)
    return _PEOPLE


def method_of(s):
    return next((m for m, rx in METHODS if rx.search(s)), "")


def scan(text, tiers=("name", "title", "office")):
    lines = [" ".join(x.split()) for x in (text or "").split("\n")]
    lines = [x for x in lines if x]
    hits, seen = [], set()
    people()

    def add(i, who, role, tier, method):
        if (i, who) in seen:
            return
        seen.add((i, who))
        snip = " / ".join(lines[max(0, i - 1):i + 2])
        if len(snip) > SNIPPET_CHARS:
            j = snip.find(lines[i])
            start = max(0, min(j - 80, len(snip) - SNIPPET_CHARS))
            snip = ("..." if start else "") + snip[start:start + SNIPPET_CHARS] + "..."
        hits.append({"who": who, "role": role, "tier": tier, "line": i, "method": method, "snippet": snip})

    for i, line in enumerate(lines):
        prev = lines[i - 1] if i else ""
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        method = method_of(" ".join((prev, line, nxt)))
        if "name" in tiers and _KEY.search(line + " " + nxt):
            # prev + line + next: a name or prior title broken across OCR
            # lines. A match must start on this line.
            ctx = prev + " " + line + " " + nxt
            lo, hi = len(prev) + 1, len(prev) + 1 + len(line)
            low = ctx.lower()
            for p in people():
                if p["key"] not in low and not (p["alt_key"] and p["alt_key"] in low):
                    continue
                for m in p["rx"].finditer(ctx):
                    if not lo <= m.start() < hi:
                        continue
                    if p["prior"] and (p["prior"].search(ctx) or HILL_TAG.search(ctx[m.end():m.end() + 12])) \
                            and not (p["current"] and p["current"].search(ctx)):
                        continue
                    if CONTRIBUTION.search(prev + " " + line):
                        continue
                    add(i, p["who"], f"{p['role']}, {p['years']}", "name", method)
                    break
        if not method or not TKEY.search(line):
            continue
        two = prev + " " + line
        for tier, pats in (("title", TITLE_PATTERNS), ("office", OFFICE_PATTERNS)):
            if tier not in tiers:
                continue
            for post, rx in pats:
                if any(m.start() > len(prev) for m in rx.finditer(two)):
                    add(i, f"({post}, unnamed)", post, tier, method)
    # A line naming the person outranks a title/office hit on the same line.
    named = {h["line"] for h in hits if h["tier"] == "name"}
    hits = [h for h in hits if h["tier"] == "name" or h["line"] not in named]
    return sorted(hits, key=lambda h: h["line"])


def summarize(hits, cap=12):
    """Hits for one document, named ones first, capped; returns (kept,
    number dropped)."""
    order = {"name": 0, "office": 1, "title": 2}
    kept = sorted(hits, key=lambda h: (order[h["tier"]], h["line"]))[:cap]
    return sorted(kept, key=lambda h: h["line"]), max(0, len(hits) - cap)


if __name__ == "__main__":
    for path in sys.argv[1:]:
        for h in scan(open(path, errors="replace").read()):
            print(f"{path}:{h['line']} [{h['tier']}] {h['who']} ({h['method'] or '-'}) :: {h['snippet']}")
