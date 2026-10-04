#!/usr/bin/env python3
"""Downloads and reads the actual Justification & Approval documents attached
to SAM.gov justification notices (added 2026-10-03).

The notice `description` that fetch_sam.py used to rely on is a short blurb
(median ~190 characters on 2026-10-01's 79 notices). The signed J&A itself --
vendor, estimated value, the FAR authority cited, the agency's reasoning,
market research, any protest -- is an attached PDF/DOCX on 76 of those 79.
Measured that day: a GE engine bridge on a $125M sole-source ceiling and an
"urgent and compelling" organ-transplant-network bridge both went out as
dollar-less one-liners because only the blurb was read.

Everything here uses SAM.gov's own unauthenticated frontend API, so it costs
none of the SAM_API_KEY quota (see reference_sam_gov_api memory: that key's
throttle self-extends 24h on any call made while throttled).

Text extraction handles the three failure modes seen in real J&As:
- image-only PDFs (redaction tools often flatten pages) -> OCR via tesseract
  when it's installed (15 of 75 PDFs on 2026-10-01);
- PDFs with a shifted font encoding, where every glyph comes out 29 code
  points low ("$SSURYDO" = "Approval") -> decoded line by line;
- DOCX attachments -> word/document.xml text.

The text itself goes to a separate file, data/sam_documents.json
({noticeId: text}), so data/sam_candidates.json stays small enough for the
main pass's fallback path to read; extract_ja.py is the only reader of it.

CLI (enrich an existing candidates file in place, e.g. to test without
re-calling api.sam.gov):
    python3 scripts/sam_docs.py data/sam_candidates.json [data/sam_documents.json]
"""
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dc_common import download, http_get_json  # noqa: E402

RESOURCES = "https://sam.gov/api/prod/opps/v3/opportunities/{}/resources"
FILE_URL = "https://sam.gov/api/prod/opps/v3/opportunities/resources/files/{}/download"
# A J&A is 1,000-2,500 words typically (max ~4,000 seen); this keeps the
# value/authority/rationale/market-research sections of even long ones.
TEXT_CAP = 24000
OCR_PAGES = 8
_COMMON = re.compile(r"\b(the|and|of|to|for|is|this|that|with)\b", re.IGNORECASE)
_WS = re.compile(r"[ \t]+")


def notice_id_from_ui_link(ui_link):
    m = re.search(r"contract/opp/([^/]+)/view", ui_link or "")
    return m.group(1) if m else None


def _unshift_line(line):
    shifted = "".join(chr(ord(c) + 29) if 3 <= ord(c) <= 93 else c for c in line)
    return shifted if len(_COMMON.findall(shifted)) > len(_COMMON.findall(line)) else line


def _ocr(pdf_path):
    if not (shutil.which("tesseract") and shutil.which("pdftoppm")):
        return ""
    with tempfile.TemporaryDirectory() as d:
        subprocess.run(["pdftoppm", "-r", "200", "-gray", "-l", str(OCR_PAGES), pdf_path, f"{d}/p"],
                       capture_output=True, timeout=120)
        pages = []
        for p in sorted(glob.glob(f"{d}/p*")):
            r = subprocess.run(["tesseract", p, "-", "--psm", "6"], capture_output=True, text=True, timeout=120)
            pages.append(r.stdout)
        return "\n".join(pages)


def file_text(path, name=""):
    """Returns (text, method). Never raises."""
    ext = os.path.splitext(name or path)[1].lower()
    try:
        with open(path, "rb") as f:
            head = f.read(5)
        if head.startswith(b"%PDF") or ext == ".pdf":
            r = subprocess.run(["pdftotext", "-layout", path, "-"], capture_output=True, timeout=60)
            text = r.stdout.decode("utf-8", "replace")
            if len(text.split()) < 30:
                return _ocr(path), "ocr"
            return "\n".join(_unshift_line(l) for l in text.split("\n")), "pdf"
        if head.startswith(b"PK") or ext == ".docx":
            xml = zipfile.ZipFile(path).read("word/document.xml").decode("utf-8", "replace")
            return re.sub(r"<[^>]+>", "", xml.replace("</w:p>", "\n")), "docx"
        if ext in (".txt", ".htm", ".html"):
            with open(path, errors="replace") as f:
                return re.sub(r"<[^>]+>", " ", f.read()), "text"
    except Exception as e:  # noqa: BLE001
        return "", f"error: {e}"
    return "", f"unsupported {ext or 'type'}"


def clean(text):
    lines = (_WS.sub(" ", l).strip() for l in text.split("\n"))
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def fetch_documents(notice_id):
    """{attachments: [{name, url}], document_text, document_source, document_error}"""
    out = {"attachments": [], "document_text": None, "document_source": None, "document_error": None}
    try:
        data = http_get_json(RESOURCES.format(notice_id), timeout=20)
    except Exception as e:  # noqa: BLE001
        out["document_error"] = f"attachment list: {e}"
        return out
    files = [a for grp in (data.get("_embedded") or {}).get("opportunityAttachmentList", [])
             for a in grp.get("attachments", [])]
    texts, methods = [], []
    with tempfile.TemporaryDirectory() as d:
        for a in files:
            rid = a.get("resourceId")
            if a.get("type") == "link" or not rid:
                continue
            if a.get("accessLevel") != "public":
                out["attachments"].append({"name": a.get("name"), "url": None, "access": a.get("accessLevel")})
                continue
            url = FILE_URL.format(rid)
            out["attachments"].append({"name": a.get("name"), "url": url})
            if sum(len(t) for t in texts) >= TEXT_CAP:
                continue
            path = os.path.join(d, rid)
            try:
                download(url, path, timeout=60)
            except Exception as e:  # noqa: BLE001
                out["document_error"] = f"download {a.get('name')}: {e}"
                continue
            text, method = file_text(path, a.get("name") or "")
            if text.strip():
                texts.append(f"[{a.get('name')}]\n{clean(text)}")
                methods.append(method)
    if texts:
        out["document_text"] = "\n\n".join(texts)[:TEXT_CAP]
        out["document_source"] = "+".join(sorted(set(methods)))
    elif not out["document_error"]:
        out["document_error"] = "no readable attachment" if files else "no attachments"
    return out


DOCS_PATH = "data/sam_documents.json"


def enrich(justifications, workers=6):
    """Adds noticeId/attachments/document_source/document_error to each
    justification in place; returns {noticeId: document_text}."""
    def one(j):
        nid = notice_id_from_ui_link(j.get("uiLink"))
        return fetch_documents(nid) if nid else {"attachments": [], "document_text": None,
                                                  "document_source": None, "document_error": "no noticeId"}
    texts = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for j, docs in zip(justifications, ex.map(one, justifications)):
            j["noticeId"] = notice_id_from_ui_link(j.get("uiLink"))
            text = docs.pop("document_text")
            j.update(docs)
            j["document_chars"] = len(text or "")
            if text and j["noticeId"]:
                texts[j["noticeId"]] = text
    ocr = sum(1 for j in justifications if "ocr" in (j.get("document_source") or ""))
    print(f"J&A documents: read {len(texts)}/{len(justifications)} ({ocr} via OCR)", file=sys.stderr)
    return texts


def write_documents(texts, path=DOCS_PATH):
    with open(path, "w") as f:
        json.dump(texts, f)
        f.write("\n")


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "data/sam_candidates.json"
    docs_path = sys.argv[2] if len(sys.argv) > 2 else DOCS_PATH
    with open(path) as f:
        cands = json.load(f)
    texts = enrich(cands.get("justifications") or [])
    write_documents(texts, docs_path)
    cands.setdefault("counts", {})["justification_documents_read"] = len(texts)
    with open(path, "w") as f:
        json.dump(cands, f, indent=2)
        f.write("\n")


if __name__ == "__main__":
    main()
