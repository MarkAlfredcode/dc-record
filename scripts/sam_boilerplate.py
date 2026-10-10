#!/usr/bin/env python3
"""Strips contracting boilerplate from SAM.gov attachment text before Haiku
screens it for news (added 2026-10-10).

One day of new attachments (2026-10-09: 526 files) is ~22.8M characters of
extracted text, nearly all of it material that can't carry news. Measured on
that day, each step below:
  22.8M  raw
  -0.8M  Department of Labor wage determinations (dropped whole)
  -7.6M  text repeated across 3+ different solicitations the same run
         (agency templates, standard instructions) -- matched as 8-word
         shingles so differently wrapped copies still match
  -5.1M  FAR / DFARS / agency-supplement clause text, matched against the
         clause parts of acquisition.gov (data/far-clause-shingles.bin,
         rebuilt by scripts/build_far_shingles.py)
  -1.2M  repeated lines within one document (page headers/footers, with
         digits ignored so "PAGE 33 OF 199" matches "PAGE 59 OF 199"), and
         OCR noise lines from engineering drawings
   8.2M  left: statements of work, specifications, price schedules,
         amendments, Q&As, J&A narratives -- the document-specific text
A line is dropped only when 60%+ of its words fall inside a boilerplate
shingle, so a sentence an agency added to a standard clause survives.
"""
import array
import os
import re
import zlib
from collections import Counter, defaultdict

K = 8
SHINGLES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "far-clause-shingles.bin")
LINE_COVER = 0.6
COMMON_NOTICES = 3

_W = re.compile(r"[a-z0-9]+")
_DIG = re.compile(r"\d+")
_WORDISH = re.compile(r"[A-Za-z]{2,}")
_NUMISH = re.compile(r"[$\d.,%/()-]+")
_VOWEL = re.compile(r"[aeiouy]", re.I)
WAGE_DETERMINATION = re.compile(
    r"wage determination no\.?|register of wage determinations|general decision number|"
    r"service contract act wage determination|wage determinations under the service contract act", re.I)

_far = None


def far_shingles():
    global _far
    if _far is None:
        a = array.array("I")
        try:
            with open(SHINGLES_PATH, "rb") as f:
                a.frombytes(f.read())
        except OSError:
            pass
        _far = frozenset(a)
    return _far


def shingle(words, i):
    return zlib.crc32(" ".join(words[i:i + K]).encode())


def is_wage_determination(text):
    return bool(WAGE_DETERMINATION.search(text[:4000]))


def _tokenize(text):
    lines = [" ".join(l.split()) for l in text.split("\n")]
    words, owner = [], []
    for li, l in enumerate(lines):
        for w in _W.findall(l.lower()):
            words.append(w)
            owner.append(li)
    return lines, words, owner


def common_shingles(docs):
    """docs: {key: (text, group)} where group is the solicitation number (or
    notice id). Returns the shingles found in COMMON_NOTICES+ groups -- a
    revised SOW attached to two amendments of one solicitation is one group,
    so it isn't mistaken for a template."""
    groups = defaultdict(set)
    for text, group in docs.values():
        _, words, _ = _tokenize(text)
        for i in range(len(words) - K + 1):
            groups[shingle(words, i)].add(group)
    return {h for h, g in groups.items() if len(g) >= COMMON_NOTICES}


def _ocr_junk(line):
    toks = line.split()
    if not toks:
        return True
    good = sum(1 for t in toks if (_WORDISH.fullmatch(t.strip(".,;:()'\"")) and _VOWEL.search(t))
               or _NUMISH.fullmatch(t))
    return good / len(toks) < 0.5


def clean(text, common=frozenset(), ocr=False):
    """Returns the document-specific remainder of `text`."""
    far = far_shingles()
    lines, words, owner = _tokenize(text)
    covered = bytearray(len(words))
    for i in range(len(words) - K + 1):
        h = shingle(words, i)
        if h in far or h in common:
            covered[i:i + K] = b"\x01" * K
    per_line, cov_line = Counter(owner), Counter()
    for j, li in enumerate(owner):
        cov_line[li] += covered[j]
    seen, keep = set(), []
    for li, l in enumerate(lines):
        if not per_line[li]:
            continue
        if cov_line[li] / per_line[li] >= LINE_COVER:
            continue
        key = _DIG.sub("#", l.lower())
        if key in seen:
            continue
        seen.add(key)
        if ocr and _ocr_junk(l):
            continue
        keep.append(l)
    return "\n".join(keep)
