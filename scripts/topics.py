#!/usr/bin/env python3
"""White House and D.C. topic tags for The Feds (added 2026-10-24): Mark wants
White House-related and D.C. contract activity (awards, justifications,
solicitations, every notice type) in two sections of their own instead of
scattered across the type-by-type sections.

classify(text_fields, agency_fields) -> ("white_house" | "dc" | None, hit)
is a regex first sort. build_feds_input.py moves what it tags out of the
home sections into two buckets; the main pass makes the final call and sends
a false positive (a vendor's mailing address, "White House" as a place name)
back to its home section.
"""
import re

# Executive Office of the President components, as awarding/buying agency.
WH_AGENCY_RE = re.compile(
    r"executive office of the president|white house|office of administration|national security council|"
    r"office of management and budget|office of the vice president|office of science and technology|"
    r"council of economic advisers|council on environmental quality|office of national drug control|"
    r"trade representative", re.I)

# Work for the President's own operations, whoever the buying agency is.
WH_TEXT_RE = re.compile(
    r"white house|executive office of the president|executive residence|camp david|air force one|marine one|"
    r"\bvc-25|\bvh-92|\bc-32|special air mission|presidential (?:aircraft|helicopter|protective|motorcade|"
    r"transport|inaugur|library|airlift|advance)|president'?s park|eisenhower executive office|blair house",
    re.I)

# Washington, D.C. proper: the District government and federal property in the city.
DC_RE = re.compile(
    r"district of columbia|washington,? d\.?\s?c\b|national capital region|national mall|d\.?c\.? national guard|"
    r"capitol hill|u\.s\. capitol|capitol police|smithsonian|national zoo|metropolitan police department|"
    r"union station|navy yard|anacostia|fort (?:lesley j\. )?mcnair|st\.? elizabeths|kennedy center|"
    r"pennsylvania avenue|lincoln memorial|washington monument|federal triangle|\bwmata\b|"
    r"hoover building|south capitol|n capitol st|navfac washington|military district of washington",
    re.I)


def _snip(text, m, pad=70):
    return " ".join(text[max(0, m.start() - pad):m.end() + pad].split())


def classify(text, agency=""):
    """text: everything worth scanning joined into one string; agency: the
    buying/awarding agency string(s). White House wins when both match."""
    m = WH_AGENCY_RE.search(agency or "")
    if m:
        return "white_house", f"agency: {agency[:80]}"
    m = WH_TEXT_RE.search(text or "")
    if m:
        return "white_house", _snip(text, m)
    m = DC_RE.search(f"{agency or ''} {text or ''}")
    if m:
        full = f"{agency or ''} {text or ''}"
        return "dc", _snip(full, m)
    return None, ""


def notice_topic(x):
    """A SAM.gov notice or justification record."""
    text = " ".join(str(x.get(k) or "") for k in ("title", "description_text", "what", "triage_why", "keyword_hit", "news_why"))
    text += " " + str(x.get("notice_text") or "")[:1500]
    text += " " + " ".join(str(f.get("finding") or "") for f in (x.get("document_findings") or []))
    return classify(text, x.get("agency") or "")


def award_topic(a):
    """A USAspending award, modification or cut."""
    if a.get("triage_tier") == "white_house":
        return "white_house", "awarded by an Executive Office of the President component"
    text = " ".join(str(a.get(k) or "") for k in ("raw_description", "product_or_service_description", "triage_why"))
    return classify(text, f"{a.get('awarding_agency') or ''} {a.get('awarding_sub_agency') or ''}")
