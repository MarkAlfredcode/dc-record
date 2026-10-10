#!/usr/bin/env python3
"""Downloads and reads every file attached to every SAM.gov notice in the
window -- justifications AND solicitations (J&As since 2026-10-03; all
notices since 2026-10-10).

Why every file: the notice blurb is a few hundred characters, the J&A, the
statement of work, the specifications, the price schedule and the Q&A
amendments are attachments. On 2026-10-09, 234 of 632 notices carried 1,402
public files, 526 of them new that day (~570 MB).

What this script does (no LLM; runs in the fetch step):
- lists each notice's attachments via SAM.gov's own unauthenticated
  frontend API (none of the SAM_API_KEY quota, see reference_sam_gov_api);
- skips any file already read by an earlier run: data/sam-docs-read.json,
  keyed by SAM resourceId, committed by every run (AM and PM) -- amendments
  re-attach the whole earlier file set, so most files on a notice usually
  were read before;
- downloads and extracts the rest with sam_reader.read_file (PDF with
  per-page OCR, DOCX, XLSX, PPTX, ZIP, legacy Office, images, text);
- stops starting new files at the time budget (DC_SAM_DOCS_BUDGET_SEC);
  anything left is "deferred" and goes to the pending queue
  (data/sam-docs-pending.json, kept by screen_sam_docs.py), which every run
  reads first -- nothing is silently skipped.

Outputs:
- data/sam_files.json   {resourceId: {name, url, nids, sols, titles, agency,
                         kind, method, info, text | error | deferred}} --
                         this run's files for screen_sam_docs.py (gitignored)
- data/sam_documents.json {noticeId: J&A text} for extract_ja.py, as before
- each notice record gains `attachments` [{rid, name, url, status}] and
  `links` (external pages such as DIBBS/PIEE/NECO, which aren't fetched).

CLI (re-read an existing candidates file, e.g. to test without re-calling
api.sam.gov):
    python3 scripts/sam_docs.py data/sam_candidates.json
"""
import json
import os
import re
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dc_common import download, http_get_json, load_manifest  # noqa: E402
from sam_reader import read_file  # noqa: E402

RESOURCES = "https://sam.gov/api/prod/opps/v3/opportunities/{}/resources"
FILE_URL = "https://sam.gov/api/prod/opps/v3/opportunities/resources/files/{}/download"
CACHE_PATH = "data/sam-docs-read.json"
# Files a run couldn't get to (time or screening budget, or a failed read),
# with their notice's details, so a later run reads them even after the notice
# has left the fetch window. Written by screen_sam_docs.py, committed.
PENDING_PATH = "data/sam-docs-pending.json"
FILES_PATH = "data/sam_files.json"
DOCS_PATH = "data/sam_documents.json"
# J&A text handed to extract_ja.py for vendor/value/authority. Every file is
# also screened in full by screen_sam_docs.py; this cap is only for the
# structured extraction, which needs the J&A itself, not its attachments.
JA_TEXT_CAP = 24000
# A J&A-looking file name (also used by fetch_sam.py to route Special/Award/
# Sources Sought notices carrying one into the justification pipeline).
JA_FILE_NAME = re.compile(r"\bj\s*&\s*a\b|\bj\s*and\s*a\b|\bja\b|justification|jofoc|\blsj\b|sole[\s_-]*source|brand[\s_-]*name|"
                          r"\bsssj\b|\bjefo\b|\bfoc\b|\bbnj\b|\bsss?j\b", re.I)
WORKERS = 4
BUDGET_SEC = int(os.environ.get("DC_SAM_DOCS_BUDGET_SEC") or 1200)


def notice_id_from_ui_link(ui_link):
    m = re.search(r"/opp/([^/]+)/view", ui_link or "")
    return m.group(1) if m else None


def list_attachments(nid):
    """([file attachments], [links], error)"""
    try:
        data = http_get_json(RESOURCES.format(nid), timeout=20)
    except Exception as e:  # noqa: BLE001
        return [], [], f"attachment list: {e}"
    files, links = [], []
    for grp in (data.get("_embedded") or {}).get("opportunityAttachmentList", []):
        for a in grp.get("attachments", []):
            if a.get("deletedFlag") == "1":
                continue
            if a.get("type") == "link":
                links.append({"url": a.get("uri"), "host": urlparse(a.get("uri") or "").netloc,
                              "description": a.get("description")})
            elif a.get("resourceId"):
                files.append(a)
    return files, links, None


def _read_one(rid, name, deadline):
    if time.time() > deadline:
        return {"deferred": True}
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "f")
        err = None
        # 3 of 470 test downloads hit connect timeouts that cleared on retry.
        for wait in (0, 5, 20):
            time.sleep(wait)
            try:
                download(FILE_URL.format(rid), path, timeout=180)
                err = None
                break
            except Exception as e:  # noqa: BLE001
                err = e
        if err:
            return {"error": f"download failed: {err}"[:300]}
        text, method, info = read_file(path, name, deadline=deadline + 300)
    out = {"method": method, "info": info, "text": text}
    if not text.strip() and info.get("error"):
        out["error"] = info["error"]
    return out


def enrich(justifications, notices=(), budget_sec=BUDGET_SEC, prelisted=None):
    """Attaches `attachments`/`links`/`document_*` fields to each notice in
    place, reads every file not already in the cache, writes FILES_PATH and
    returns {noticeId: J&A text} for extract_ja.py. `prelisted` = {noticeId:
    list_attachments() result} already fetched by the caller."""
    start = time.time()
    deadline = start + budget_sec
    cache = load_manifest(CACHE_PATH).get("files", {})
    pairs = [("justification", j) for j in justifications] + [("notice", n) for n in notices]
    for _, n in pairs:
        n["noticeId"] = n.get("noticeId") or notice_id_from_ui_link(n.get("uiLink"))
    listings = dict(prelisted or {})
    todo = list({n["noticeId"] for _, n in pairs if n["noticeId"] and n["noticeId"] not in listings})
    with ThreadPoolExecutor(max_workers=8) as ex:
        listings.update(zip(todo, ex.map(list_attachments, todo)))
    listed = [listings.get(n["noticeId"], ([], [], "no noticeId")) for _, n in pairs]
    notices = pairs

    # Which files need reading: everything not already read by an earlier
    # run, plus every file on a justification (extract_ja needs the J&A text
    # whether or not its news screen is cached).
    files = {}
    for (kind, n), (atts, links, err) in zip(notices, listed):
        n["attachments"], n["links"] = [], links
        if err:
            n["document_error"] = err
        for a in atts:
            rid, name = a["resourceId"], a.get("name") or ""
            public = a.get("accessLevel") == "public"
            cached = cache.get(rid)
            status = "restricted" if not public else (cached or {}).get("s") if cached and cached.get("s") != "retry" else "pending"
            n["attachments"].append({"rid": rid, "name": name, "url": FILE_URL.format(rid) if public else None,
                                     "size": a.get("size"), "posted": (a.get("postedDate") or "")[:10] or None,
                                     "status": status})
            if not public:
                continue
            need_screen = status == "pending"
            if need_screen or kind == "justification":
                f = files.setdefault(rid, {"name": name, "url": FILE_URL.format(rid), "size": a.get("size") or 0,
                                           "posted": a.get("postedDate"), "nids": [], "sols": [], "titles": [],
                                           "uiLinks": [], "agency": n.get("agency"), "kind": kind,
                                           "type": n.get("noticeTypeLabel"), "screen": False})
                f["screen"] = f["screen"] or need_screen
                f["nids"].append(n["noticeId"])
                f["sols"].append(n.get("solicitationNumber") or n["noticeId"])
                f["titles"].append(n.get("title"))
                f["uiLinks"].append(n.get("uiLink"))
                if kind == "justification":
                    f["kind"] = "justification"

    # Files earlier runs couldn't get to, whether or not their notice is
    # still in the window.
    queued = 0
    for rid, meta in load_manifest(PENDING_PATH).get("files", {}).items():
        if rid in files or (cache.get(rid) or {}).get("s", "retry") != "retry":
            continue
        files[rid] = {**meta, "screen": True, "from_queue": True}
        queued += 1

    # J&A files first (the edition always reports every justification), then
    # the queue (oldest first), then smallest first so a time-budget cutoff
    # defers the fewest files.
    order = sorted(files, key=lambda r: (files[r]["kind"] != "justification", not files[r].get("from_queue"),
                                         files[r].get("queued_since") or "", files[r]["size"]))
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        results = list(ex.map(lambda r: _read_one(r, files[r]["name"], deadline), order))
    for rid, res in zip(order, results):
        files[rid].update(res)

    texts = {}
    for kind, n in notices:
        for a in n["attachments"]:
            f = files.get(a["rid"])
            if f and a["status"] == "pending":
                a["status"] = "deferred" if f.get("deferred") else "error" if f.get("error") else "read"
        if kind != "justification":
            continue
        parts = sorted(((a["name"], files[a["rid"]].get("text") or "") for a in n["attachments"] if a["rid"] in files),
                       key=lambda p: not JA_FILE_NAME.search(p[0]))
        text = "\n\n".join(f"[{name}]\n{t.strip()}" for name, t in parts if t.strip())
        n["document_chars"] = len(text)
        n["document_source"] = "+".join(sorted({files[a["rid"]].get("method") for a in n["attachments"]
                                                if a["rid"] in files and files[a["rid"]].get("method")})) or None
        if text and n["noticeId"]:
            texts[n["noticeId"]] = text[:JA_TEXT_CAP]
        elif not n.get("document_error"):
            n["document_error"] = "no readable attachment" if n["attachments"] else "no attachments"

    with open(FILES_PATH, "w") as fh:
        json.dump(files, fh)
    read = sum(1 for f in files.values() if "text" in f)
    deferred = sum(1 for f in files.values() if f.get("deferred"))
    errors = sum(1 for f in files.values() if f.get("error"))
    ocr = sum(1 for f in files.values() if "ocr" in (f.get("method") or ""))
    cached = sum(1 for _, n in notices for a in n["attachments"] if a["status"] not in ("pending", "read", "deferred", "error", "restricted"))
    print(f"SAM attachments: {sum(len(n['attachments']) for _, n in notices)} files on "
          f"{sum(1 for _, n in notices if n['attachments'])} notices; read {read} now ({ocr} with OCR), "
          f"{cached} already read by earlier runs, {queued} from the queue, {deferred} deferred to next run, "
          f"{errors} failed; "
          f"{time.time() - start:.0f}s", file=sys.stderr)
    print(f"J&A documents: read {len(texts)}/{len(justifications)}", file=sys.stderr)
    return texts


def write_documents(texts, path=DOCS_PATH):
    with open(path, "w") as f:
        json.dump(texts, f)
        f.write("\n")


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "data/sam_candidates.json"
    with open(path) as f:
        cands = json.load(f)
    texts = enrich(cands.get("justifications") or [], cands.get("solicitations") or [])
    write_documents(texts)
    cands.setdefault("counts", {})["justification_documents_read"] = len(texts)
    with open(path, "w") as f:
        json.dump(cands, f, indent=2)
        f.write("\n")


if __name__ == "__main__":
    main()
