#!/usr/bin/env python3
"""Document metadata and hidden-link scan (added 2026-10-19).

sam_reader.read_file() / read_pdf() call attach() for every file they read --
SAM.gov attachments (J&As, statements of work, price schedules, amendments),
FARA filings, and every member of a ZIP -- so the fingerprints a document
carries beyond its visible text are checked on every run, with no LLM.

What it looks at, and why:
- AI-tool fingerprints (news 5/4): tracking parameters ChatGPT and other
  assistants append to the links they cite (`?utm_source=chatgpt.com`,
  `utm_source=openai`), ChatGPT-only link and citation artifacts
  (`sandbox:/mnt/data`, `contentReference[oaicite:0]`, `citeturn0search1`),
  and an AI tool named as a file's Creator / Producer / Application. These
  mean text was pasted from a chatbot into a federal document. Checked in
  PDF link annotations (the click targets, which the visible text never
  shows), Office hyperlink relationships, document properties, XMP, and the
  extracted text itself.
- Other tracking parameters (news 3): utm_*, gclid, fbclid, mc_cid... mean
  text or links were copied from a web page, newsletter or ad.
- Leaked work product (news 4): Word tracked changes (who deleted what) and
  comments, hidden Excel sheets.
- Leaked provenance (news 3): file paths with a username or a UNC server
  name in the properties or link targets.
- Provenance of J&As: Author / Last Modified By / Company, so the main pass
  can compare them to the named vendor (screen_sam_docs.provenance_finding).

Output: attach(path, text, info) puts `info["meta"]` (compact property
fields; kept in the gitignored per-run data/sam_files.json) and
`info["md_flags"]` (findings in screen_sam_docs' format: news, finding,
quote, verified, context) on the read_file info dict. Never raises.
"""
import html
import re
import subprocess
import zlib
import zipfile

AI_NAMES = (r"chatgpt|chat\s?gpt|openai|open\s?ai\b|gpt-?[345]\w*|anthropic|claude|copilot|gemini|bard\b|"
            r"perplexity|grok\b|deepseek|llama|mistral|jasper\.ai|writesonic|quillbot|notion\s?ai|copy\.ai")
# Names that are also ordinary words or company names: only trusted in the
# fields that name the producing software.
AI_AMBIGUOUS = re.compile(r"^(claude|gemini|grok|llama|mistral|bard|copilot)$", re.I)
AI_TOOL_FIELD = re.compile(rf"\b({AI_NAMES})", re.I)
TOOL_FIELDS = ("Creator", "Producer", "Application", "CreatorTool", "AppVersion")
AUTHOR_FIELDS = ("Author", "LastModifiedBy", "Title", "Subject", "Keywords", "Comments", "Company", "Manager")

AI_SOURCE = r"chatgpt(?:\.com)?|openai(?:\.com)?|perplexity(?:\.ai)?|claude(?:\.ai)?|copilot(?:\.microsoft\.com)?|gemini(?:\.google\.com)?|grok|deepseek|phind|poe(?:\.com)?|you\.com|bing\.com/chat"
AI_PARAM = re.compile(rf"[?&#;](?:utm_source|utm_medium|utm_campaign|utm_content|ref|referrer|source|src)=(?:{AI_SOURCE})(?![\w.-])", re.I)
AI_URL = re.compile(r"chat\.openai\.com/|chatgpt\.com/(?:c|g|share)/|oaiusercontent\.com|claude\.ai/(?:chat|share)/|"
                    r"perplexity\.ai/search/|gemini\.google\.com/(?:app|share)/|sandbox:/mnt/data|"
                    r"\bcontentReference\[oaicite:\d+\]|\bciteturn\d+(?:search|view|news)\d+|【\d+:\d+†[^】]{0,60}】|"
                    r"\bturn\d+(?:search|view|news)\d+\b|:contentReference\[", re.I)
# Said by a chatbot, never by a contracting officer.
AI_PHRASE = re.compile(r"\bas an ai language model\b|\bi (?:cannot|can't|am unable to) (?:fulfill|assist with) (?:this|that) request\b|"
                       r"\bhere(?:'s| is) (?:a|the|your) (?:revised|updated|draft|sample) (?:version|draft|statement of work|sow|j&a)\b.{0,40}\bhope this helps\b|"
                       r"\bChatGPT said:|\bregenerate response\b|\bcertainly! here", re.I | re.S)
TRACK_PARAM = re.compile(r"[?&](utm_(?:source|medium|campaign|content|term)|gclid|fbclid|msclkid|mc_cid|mc_eid|_hsenc|_hsmi|igshid|"
                         r"mkt_tok|vero_id|ck_subscriber_id)=([^&#\s\"'<>)\]]{1,40})", re.I)
URL = re.compile(r"https?://[^\s\"'<>)\]]{4,400}", re.I)
LOCAL_PATH = re.compile(r"(?:\b[A-Za-z]:\\Users\\([^\\\s\"'<>]+)\\[^\s\"'<>]*|file:///[A-Za-z]:/Users/([^/\s\"'<>]+)/[^\s\"'<>]*|"
                        r"(?<![\w/])/(?:Users|home)/([^/\s\"'<>]+)/[^\s\"'<>]*|\\\\[A-Za-z0-9._-]+\\[^\s\"'<>]+)")
SKIP_USERS = {"public", "default", "all users", "user", "admin", "administrator", "guest", "shared"}
MAX_FLAGS = 8
PDF_STREAM_CAP = 40_000_000   # bytes of file we will inflate looking for link annotations
INFLATE_CAP = 4_000_000


def _clean(s, n=200):
    return " ".join(str(s).split())[:n]


def _flag(news, finding, quote, context=""):
    return {"news": news, "finding": _clean(finding, 300), "quote": _clean(quote, 250), "verified": True,
            "context": _clean(context or quote, 700), "kind": "metadata"}


# ---------------------------------------------------------------- PDFs

def _pdf_meta(path):
    """Info dictionary (pdfinfo) plus XMP (pdfinfo -meta, which prints only
    the XML)."""
    meta = {}
    try:
        head = subprocess.run(["pdfinfo", "-isodates", path], capture_output=True, timeout=60).stdout.decode("utf-8", "replace")
        xmp = subprocess.run(["pdfinfo", "-meta", path], capture_output=True, timeout=60).stdout.decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return meta, ""
    for line in head.splitlines():
        k, sep, v = line.partition(":")
        if sep and k.strip() in ("Title", "Subject", "Keywords", "Author", "Creator", "Producer", "CreationDate", "ModDate"):
            if v.strip():
                meta[k.strip()] = v.strip()
    for tag, key in (("xmp:CreatorTool", "CreatorTool"), ("pdf:Producer", "Producer"), ("dc:creator", "Author"),
                     ("xmp:CreateDate", "CreationDate"), ("xmp:ModifyDate", "ModDate")):
        m = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", xmp, re.S)
        if m:
            v = _clean(html.unescape(re.sub(r"<[^>]+>", " ", m.group(1))))
            if v and key not in meta:
                meta[key] = v
    return meta, xmp


def _pdf_uris(path):
    """Link-annotation targets: plain /URI strings, plus those inside
    FlateDecode streams (object streams hold most annotations)."""
    try:
        with open(path, "rb") as f:
            data = f.read(PDF_STREAM_CAP + 1)
    except OSError:
        return []
    found = []
    pat = re.compile(rb"/URI\s*\(((?:\\.|[^\\)])*)\)")

    def grab(blob):
        for m in pat.finditer(blob):
            s = re.sub(rb"\\(.)", rb"\1", m.group(1)).decode("latin-1", "replace")
            found.append(s)
    grab(data)
    if len(data) <= PDF_STREAM_CAP:
        for m in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", data, re.S):
            blob = m.group(1)
            if blob[:1] != b"x":     # zlib header; skips images/fonts in other encodings
                continue
            try:
                inflated = zlib.decompressobj().decompress(blob, INFLATE_CAP)
            except Exception:  # noqa: BLE001
                continue
            if b"/URI" in inflated:
                grab(inflated)
    return list(dict.fromkeys(found))


# ---------------------------------------------------------------- Office

def _core_props(z):
    meta = {}
    for part in ("docProps/core.xml", "docProps/app.xml", "docProps/custom.xml"):
        if part not in z.namelist():
            continue
        xml = z.read(part).decode("utf-8", "replace")
        for m in re.finditer(r"<(?:dc:|cp:|dcterms:|ep:|vt:)?(\w+)(?:\s[^>]*)?>([^<]{1,300})</", xml):
            k, v = m.group(1), html.unescape(m.group(2)).strip()
            if v and k in ("title", "subject", "creator", "keywords", "description", "lastModifiedBy", "revision",
                           "created", "modified", "Application", "AppVersion", "Company", "Manager", "Template", "TotalTime"):
                meta[{"creator": "Author", "title": "Title", "subject": "Subject", "keywords": "Keywords",
                      "description": "Comments", "created": "CreationDate", "modified": "ModDate",
                      "lastModifiedBy": "LastModifiedBy", "revision": "Revision"}.get(k, k)] = v
        if part.endswith("custom.xml"):
            for m in re.finditer(r'<property[^>]*\bname="([^"]{1,60})"[^>]*>.*?<vt:\w+>([^<]{1,200})</vt:', xml, re.S):
                meta[f"custom:{html.unescape(m.group(1))}"] = html.unescape(m.group(2)).strip()
    return meta


def _office_links(z):
    out = []
    for n in z.namelist():
        if n.endswith(".rels"):
            xml = z.read(n).decode("utf-8", "replace")
            for m in re.finditer(r'<Relationship\b[^>]*?>', xml):
                tag = m.group(0)
                if 'TargetMode="External"' in tag:
                    t = re.search(r'\bTarget="([^"]*)"', tag)
                    if t:
                        out.append(html.unescape(t.group(1)))
    return list(dict.fromkeys(out))


def _word_extras(z):
    """Tracked changes and comments: who deleted what."""
    flags = []
    if "word/document.xml" not in z.namelist():
        return flags
    xml = z.read("word/document.xml").decode("utf-8", "replace")
    dels = re.findall(r"<w:del\b[^>]*\bw:author=\"([^\"]*)\"[^>]*>(.*?)</w:del>", xml, re.S)
    ins = re.findall(r"<w:ins\b[^>]*\bw:author=\"([^\"]*)\"", xml)
    deleted = [(a, " ".join(html.unescape(re.sub(r"<[^>]+>", "", " ".join(re.findall(r"<w:delText[^>]*>(.*?)</w:delText>", body, re.S)))).split()))
               for a, body in dels]
    deleted = [(a, t) for a, t in deleted if len(t) >= 12]
    authors = sorted({a for a, _ in deleted} | set(ins))
    if deleted:
        a, t = max(deleted, key=lambda x: len(x[1]))
        flags.append(_flag(4, f"Word file still holds {len(deleted)} tracked deletion(s)"
                              f"{' and ' + str(len(ins)) + ' insertion(s)' if ins else ''} by {', '.join(authors[:4])}; "
                              f"the longest deleted passage is quoted",
                           t[:240], f"Deleted by {a}: {t[:600]}"))
    elif ins and authors:
        flags.append(_flag(3, f"Word file still holds {len(ins)} tracked insertion(s) by {', '.join(authors[:4])}",
                           f"tracked changes by {authors[0]}"))
    if "word/comments.xml" in z.namelist():
        cx = z.read("word/comments.xml").decode("utf-8", "replace")
        cs = re.findall(r"<w:comment\b[^>]*\bw:author=\"([^\"]*)\"[^>]*>(.*?)</w:comment>", cx, re.S)
        texts = [(a, " ".join(html.unescape(re.sub(r"<[^>]+>", " ", b)).split())) for a, b in cs]
        texts = [(a, t) for a, t in texts if len(t) >= 12]
        if texts:
            a, t = max(texts, key=lambda x: len(x[1]))
            flags.append(_flag(4, f"Word file still carries {len(texts)} reviewer comment(s) by {', '.join(sorted({x for x, _ in texts})[:4])}",
                               t[:240], f"Comment by {a}: {t[:600]}"))
    return flags


def _hidden_sheets(z):
    if "xl/workbook.xml" not in z.namelist():
        return []
    wb = z.read("xl/workbook.xml").decode("utf-8", "replace")
    hid = re.findall(r"<sheet\b[^>]*\bname=\"([^\"]+)\"[^>]*\bstate=\"(hidden|veryHidden)\"", wb)
    if not hid:
        return []
    names = ", ".join(html.unescape(n) for n, _ in hid[:5])
    return [_flag(3, f"Excel workbook has {len(hid)} hidden sheet(s): {names}", f"hidden sheet: {html.unescape(hid[0][0])}")]


# ---------------------------------------------------------------- scanning

def _urls(text):
    return [u.rstrip(".,;:") for u in URL.findall(text or "")]


def _scan(meta, links, text, tool_fields_only_ai=True):
    flags = []
    seen = set()

    def add(f):
        key = (f["finding"][:80], f["quote"][:60])
        if key not in seen and len(flags) < MAX_FLAGS:
            seen.add(key)
            flags.append(f)

    # 1. AI tracking parameters / artifacts, in link targets and in the text.
    hits = []
    for src, items in (("a link target in the file", links), ("the text", _urls(text))):
        for u in items:
            if AI_PARAM.search(u) or AI_URL.search(u):
                hits.append((src, u))
    for m in AI_URL.finditer(text or ""):
        hits.append(("the text", text[max(0, m.start() - 30):m.end() + 30]))
    for m in AI_PARAM.finditer(text or ""):
        s = text[max(0, m.start() - 80):m.end() + 20]
        hits.append(("the text", s))
    if hits:
        src, u = hits[0]
        where = sorted({s for s, _ in hits})
        add(_flag(5, f"AI-chatbot fingerprint in {' and '.join(where)}: {len(hits)} link/citation marker(s) such as "
                     f"utm_source=chatgpt.com, which ChatGPT appends to links it cites — suggests text or links were copied from a chatbot",
                  u, u))
    m = AI_PHRASE.search(text or "")
    if m:
        add(_flag(5, "Chatbot boilerplate phrase in the document text", m.group(0), text[max(0, m.start() - 150):m.end() + 150]))

    # 2. AI tool named in the file's properties.
    for k in TOOL_FIELDS + AUTHOR_FIELDS:
        v = meta.get(k) or ""
        mm = AI_TOOL_FIELD.search(v)
        if not mm:
            continue
        if k in AUTHOR_FIELDS and AI_AMBIGUOUS.match(mm.group(1)):
            continue
        add(_flag(4 if k in TOOL_FIELDS else 5,
                  f"File properties name an AI tool ({k}: {_clean(v, 80)})", f"{k}: {_clean(v, 120)}"))

    # 3. Other tracking parameters (text copied from a web page or newsletter).
    params = {}
    for u in list(links) + _urls(text):
        for pm in TRACK_PARAM.finditer(u):
            if not AI_PARAM.search(pm.group(0)):
                params.setdefault(f"{pm.group(1).lower()}={pm.group(2)}", u)
    if params:
        k0 = next(iter(params))
        add(_flag(3, f"Tracking parameters in links ({', '.join(list(params)[:5])}): content was copied from a web page, newsletter or ad",
                  params[k0], params[k0]))

    # 4. Local paths / servers leaked in properties or link targets.
    for s in list(meta.values()) + list(links):
        pm = LOCAL_PATH.search(str(s))
        if pm:
            user = next((g for g in pm.groups() if g), None)
            if user and user.lower() in SKIP_USERS:
                continue
            add(_flag(3, f"Leaked local path or file server in the file ({'user ' + user if user else 'UNC path'})",
                      pm.group(0)[:200], pm.group(0)))
            break
    return flags


def attach(path, text, info, method=""):
    """Adds info["meta"] / info["md_flags"] for the file at path. `text` is
    what read_file extracted. Skips ZIP containers (their members were each
    attached) and files already attached."""
    if "meta" in info or method == "zip":
        return
    meta, links, flags = {}, [], []
    try:
        with open(path, "rb") as fh:
            head = fh.read(8)
        if head.startswith(b"%PDF"):
            meta, _ = _pdf_meta(path)
            links = _pdf_uris(path)
        elif head.startswith(b"PK"):
            z = zipfile.ZipFile(path)
            names = set(z.namelist())
            if names & {"word/document.xml", "xl/workbook.xml", "ppt/presentation.xml"}:
                meta = _core_props(z)
                links = _office_links(z)
                flags += _word_extras(z) + _hidden_sheets(z)
        flags = _scan(meta, links, text) + flags
    except Exception as e:  # noqa: BLE001
        info["meta_error"] = f"{type(e).__name__}: {e}"[:200]
    info["meta"] = {k: _clean(v, 120) for k, v in list(meta.items())[:24]}
    if links:
        info["meta"]["links"] = len(links)
    if flags:
        info["md_flags"] = (info.get("md_flags") or []) + flags[:MAX_FLAGS + 4]


def provenance_finding(meta):
    """One finding naming who a file's properties say wrote it, for J&As:
    the main pass compares it with the vendor named in the justification."""
    who = {k: meta[k] for k in ("Author", "LastModifiedBy", "Company", "Manager") if meta.get(k)}
    if not who:
        return None
    s = "; ".join(f"{k}: {v}" for k, v in who.items())
    return {**_flag(3, "Justification file properties (who wrote or last saved it): " + s, s, s), "kind": "provenance"}


if __name__ == "__main__":
    import json
    import sys
    sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))
    import sam_reader
    for p in sys.argv[1:]:
        t, m, i = sam_reader.read_file(p)
        print(p, m, json.dumps({k: i.get(k) for k in ("meta", "md_flags", "meta_error")}, indent=1))
