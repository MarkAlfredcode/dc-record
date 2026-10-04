"""Current House roster (full names, party, district) from the House Clerk's
MemberData.xml, keyed by bioguide id -- the roll-call XML only carries last
names, and the PTR index only carries First/Last."""
import re
import subprocess
import xml.etree.ElementTree as ET

URL = "https://clerk.house.gov/xml/lists/MemberData.xml"


def house_roster():
    """{bioguide: {"name": "Nicholas J. Begich III", "party": "R", "state": "AK", "dst": "AK-AL"}}"""
    # curl, not urllib: clerk.house.gov's WAF 403s Python's TLS fingerprint
    r = subprocess.run(["curl", "-s", "-L", "--max-time", "60", "-A", "Mozilla/5.0", URL], capture_output=True)
    body = r.stdout
    if not body.startswith(b"<?xml"):
        return {}
    root = ET.fromstring(body)
    out = {}
    for m in root.iter("member"):
        info = m.find("member-info")
        if info is None:
            continue
        bio = (info.findtext("bioguideID") or "").strip()
        if not bio:  # vacant seat
            continue
        sd = (m.findtext("statedistrict") or "").strip()
        dist = sd[2:].lstrip("0") or "AL"
        out[bio] = {
            "name": re.sub(r"\s+", " ", info.findtext("official-name") or info.findtext("namelist") or "").strip(),
            "first": (info.findtext("firstname") or "").strip(),
            "last": (info.findtext("lastname") or "").strip(),
            "party": (info.findtext("party") or "").strip(),
            "state": sd[:2],
            "dst": f"{sd[:2]}-{dist}",
        }
    return out
