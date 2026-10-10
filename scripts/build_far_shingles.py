#!/usr/bin/env python3
"""Rebuilds data/far-clause-shingles.bin, the boilerplate reference that
sam_boilerplate.py strips from SAM.gov attachments: every 8-word shingle
(CRC32, sorted uint32 array) of the solicitation-provisions-and-clauses
parts of the FAR and the agency supplements acquisition.gov publishes as one
page each, plus FAR Parts 12 and 15 (commercial-item and negotiated-
procurement text that solicitations quote at length).

Run by hand when the FAR changes materially (a FAC that rewrites clauses):
    python3 scripts/build_far_shingles.py
Built 2026-10-10: 860K words -> 616,587 shingles, 2.5 MB. acquisition.gov
slugs that 404 (HSAR, AFFARS, DLAD, AGAR, ...) are skipped; their clauses
mostly quote the FAR anyway, and the same-run cross-solicitation check in
sam_boilerplate.py catches their templates.
"""
import array
import html
import os
import re
import subprocess
import sys
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sam_boilerplate import K, SHINGLES_PATH  # noqa: E402

PAGES = [
    "far/part-52", "far/part-12", "far/part-15",
    "dfars/part-252-solicitation-provisions-and-contract-clauses",
    "vaar/part-852-solicitation-provisions-and-contract-clauses",
    "gsam/part-552",
    "diar/part-1452-solicitation-provisions-and-contract-clauses",
    "afars/part-5152-solicitation-provisions-and-contract-clauses",
    "nmcars/part-5252-solicitation-provisions-and-contract-clauses",
    "nfs/part-1852-solicitation-provisions-and-contract-clauses",
    "hhsar/part-352-solicitation-provisions-and-contract-clauses",
    "dosar/part-652-solicitation-provisions-and-contract-clauses",
]


def main():
    shingles, total = set(), 0
    for page in PAGES:
        r = subprocess.run(["curl", "-sL", "-A", "Mozilla/5.0", "--max-time", "120", "-w", "\n%{http_code}",
                            f"https://www.acquisition.gov/{page}"], capture_output=True)
        body, _, code = r.stdout.decode("utf-8", "replace").rpartition("\n")
        if code != "200":
            print(f"skip {page}: HTTP {code}", file=sys.stderr)
            continue
        body = re.sub(r"(?s)<script.*?</script>|<style.*?</style>", "", body)
        words = re.findall(r"[a-z0-9]+", html.unescape(re.sub(r"<[^>]+>", " ", body)).lower())
        total += len(words)
        for i in range(len(words) - K + 1):
            shingles.add(zlib.crc32(" ".join(words[i:i + K]).encode()))
        print(f"{page}: {len(words):,} words", file=sys.stderr)
    if total < 500_000:
        sys.exit(f"only {total:,} words fetched -- not overwriting {SHINGLES_PATH}")
    with open(SHINGLES_PATH, "wb") as f:
        f.write(array.array("I", sorted(shingles)).tobytes())
    print(f"{total:,} words -> {len(shingles):,} shingles -> {SHINGLES_PATH}", file=sys.stderr)


if __name__ == "__main__":
    main()
