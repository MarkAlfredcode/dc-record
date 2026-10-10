#!/usr/bin/env python3
"""Text extraction for every kind of file attached to SAM.gov notices (added
2026-10-10, when the Feds edition started reading every attachment on every
notice, not just J&As).

Measured on the 526 new attachments of 2026-10-09's 632 notices: 60% PDF,
17% DOCX, 10% XLSX (price schedules, Q&A sheets, workload estimates -- the
old reader returned nothing for any of them), plus PPTX, JPG/PNG photos,
TXT, ZIP bundles, legacy .doc/.xls and the odd MP4. read_file() handles all
of them except audio/video/CAD, which it reports as unsupported by name so
the edition can say exactly what went unread.

PDFs are OCR'd page by page: any page with under OCR_MIN_WORDS words of text
layer that has an image on it (redaction tools flatten pages; scanned
statements of work and signed J&As are common), or every page when the
whole document is nearly empty (some scans don't register as page images).
The old whole-document test missed a redacted J&A with a 546-character
cover page in front of image-only pages.
"""
import glob
import html
import os
import re
import shutil
import subprocess
import tempfile
import time
import zipfile

OCR_MIN_WORDS = 40
OCR_MAX_PAGES = 150     # per document; the rest are reported as unread pages
ZIP_MAX_BYTES = 400_000_000
XLSX_MAX_ROWS = 3000    # per sheet; the rest are reported as truncated rows

_COMMON = re.compile(r"\b(the|and|of|to|for|is|this|that|with)\b", re.IGNORECASE)

TEXT_EXT = {".txt", ".csv", ".htm", ".html", ".xml", ".json", ".md"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".gif"}
NO_TEXT_EXT = {".mp4", ".mov", ".avi", ".wmv", ".m4v", ".mp3", ".wav", ".m4a", ".dwg", ".dxf", ".dgn",
               ".rvt", ".ifc", ".kmz", ".kml", ".shp", ".exe", ".msi", ".iso", ".stp", ".step", ".igs",
               ".stl", ".sldprt", ".sldasm"}


def _unshift_line(line):
    """Some PDFs use a font encoding where every glyph comes out 29 code
    points low ("$SSURYDO" = "Approval")."""
    shifted = "".join(chr(ord(c) + 29) if 3 <= ord(c) <= 93 else c for c in line)
    return shifted if len(_COMMON.findall(shifted)) > len(_COMMON.findall(line)) else line


def _run(cmd, timeout=120):
    return subprocess.run(cmd, capture_output=True, timeout=timeout)


def _image_pages(pdf):
    try:
        r = _run(["pdfimages", "-list", pdf], timeout=60)
    except Exception:  # noqa: BLE001
        return set()
    pages = set()
    for line in r.stdout.decode("utf-8", "replace").splitlines()[2:]:
        parts = line.split()
        if parts and parts[0].isdigit():
            pages.add(int(parts[0]))
    return pages


def _ocr_page(pdf, page, d):
    _run(["pdftoppm", "-r", "200", "-gray", "-f", str(page), "-l", str(page), pdf, f"{d}/p{page}"])
    out = []
    for img in sorted(glob.glob(f"{d}/p{page}-*") + glob.glob(f"{d}/p{page}.*")):
        out.append(_run(["tesseract", img, "-", "--psm", "6"]).stdout.decode("utf-8", "replace"))
        os.remove(img)
    return "\n".join(out)


def read_pdf(path, deadline=None):
    r = _run(["pdftotext", "-layout", path, "-"], timeout=300)
    pages = r.stdout.decode("utf-8", "replace").split("\f")
    if pages and not pages[-1].strip():
        pages.pop()
    if not pages:
        pages = [""]
    pages = ["\n".join(_unshift_line(l) for l in p.split("\n")) for p in pages]
    info = {"pages": len(pages), "ocr_pages": 0, "unread_pages": 0}
    low = [i for i, p in enumerate(pages, 1) if len(p.split()) < OCR_MIN_WORDS]
    if not low:
        return "\n".join(pages), info
    if not (shutil.which("tesseract") and shutil.which("pdftoppm")):
        info["unread_pages"] = len(low)
        info["error"] = "no OCR tools installed"
        return "\n".join(pages), info
    nearly_empty = sum(len(p.split()) for p in pages) < 30 * len(pages)
    if not nearly_empty:
        imgs = _image_pages(path)
        low = [i for i in low if i in imgs]
    with tempfile.TemporaryDirectory() as d:
        for n, i in enumerate(low):
            if n >= OCR_MAX_PAGES or (deadline and time.time() > deadline):
                info["unread_pages"] = len(low) - n
                break
            try:
                t = _ocr_page(path, i, d)
            except Exception:  # noqa: BLE001
                info["unread_pages"] += 1
                continue
            if len(t.split()) > len(pages[i - 1].split()):
                pages[i - 1] = t
            info["ocr_pages"] += 1
    return "\n".join(pages), info


def _xml_text(xml):
    xml = xml.replace("</w:p>", "\n").replace("</a:p>", "\n").replace("</w:tc>", "\t")
    return html.unescape(re.sub(r"<[^>]+>", "", xml))


def read_docx(z):
    names = [n for n in z.namelist()
             if re.match(r"word/(document|header\d*|footer\d*|footnotes|endnotes|comments)\.xml$", n)]
    names.sort(key=lambda n: (not n.startswith("word/document"), n))
    return "\n".join(_xml_text(z.read(n).decode("utf-8", "replace")) for n in names), {}


def read_pptx(z):
    slides = [n for n in z.namelist() if re.match(r"ppt/(slides/slide|notesSlides/notesSlide)\d+\.xml$", n)]
    slides.sort(key=lambda n: ("notes" in n, int(re.search(r"(\d+)\.xml$", n).group(1))))
    return "\n".join(_xml_text(z.read(n).decode("utf-8", "replace")) for n in slides), {"pages": len(slides)}


_CELL = re.compile(r"<c\b([^>]*?)(?:/>|>(.*?)</c>)", re.S)
_T = re.compile(r"<t[^>]*>(.*?)</t>", re.S)


def read_xlsx(z):
    names = z.namelist()
    shared = []
    if "xl/sharedStrings.xml" in names:
        sx = z.read("xl/sharedStrings.xml").decode("utf-8", "replace")
        shared = [html.unescape("".join(_T.findall(si))) for si in re.findall(r"<si>(.*?)</si>", sx, re.S)]
    sheet_names = {}
    if "xl/workbook.xml" in names:
        wb = z.read("xl/workbook.xml").decode("utf-8", "replace")
        for i, m in enumerate(re.finditer(r"<sheet\b[^>]*\bname=\"([^\"]+)\"", wb), 1):
            sheet_names[i] = html.unescape(m.group(1))
    out, truncated = [], 0
    sheets = sorted((n for n in names if re.match(r"xl/worksheets/sheet\d+\.xml$", n)),
                    key=lambda n: int(re.search(r"(\d+)\.xml$", n).group(1)))
    for n in sheets:
        idx = int(re.search(r"(\d+)\.xml$", n).group(1))
        out.append(f"--- sheet: {sheet_names.get(idx, idx)} ---")
        rows = re.findall(r"<row\b[^>]*>(.*?)</row>", z.read(n).decode("utf-8", "replace"), re.S)
        for k, row in enumerate(rows):
            if k >= XLSX_MAX_ROWS:
                truncated += len(rows) - k
                break
            vals = []
            for attrs, inner in _CELL.findall(row):
                inner = inner or ""
                v = re.search(r"<v>(.*?)</v>", inner, re.S)
                if 't="s"' in attrs and v:
                    try:
                        vals.append(shared[int(v.group(1))])
                    except (ValueError, IndexError):
                        pass
                elif 't="inlineStr"' in attrs:
                    vals.append(html.unescape("".join(_T.findall(inner))))
                elif v:
                    vals.append(html.unescape(v.group(1)))
            vals = [x.strip() for x in vals if x.strip()]
            if vals:
                out.append("\t".join(vals))
    return "\n".join(out), {"truncated_rows": truncated} if truncated else {}


def read_legacy_office(path, ext):
    """.doc / .xls / .ppt (OLE2). catdoc's tools on the Linux runner,
    textutil on macOS, printable-run scraping as a last resort."""
    tools = {".xls": ["xls2csv"], ".ppt": ["catppt"], ".doc": ["catdoc", "-w"]}
    cmd = tools.get(ext, tools[".doc"])
    if shutil.which(cmd[0]):
        text = _run(cmd + [path]).stdout.decode("utf-8", "replace")
        if text.strip():
            return text, {}
    if ext == ".doc" and shutil.which("textutil"):
        text = _run(["textutil", "-convert", "txt", "-stdout", path]).stdout.decode("utf-8", "replace")
        if text.strip():
            return text, {}
    with open(path, "rb") as f:
        raw = f.read()
    runs = [m.decode("latin-1") for m in re.findall(rb"[\x20-\x7e\r\n\t]{8,}", raw)]
    runs += [m.decode("utf-16-le", "ignore") for m in re.findall(rb"(?:[\x20-\x7e]\x00){8,}", raw)]
    return "\n".join(runs), {"note": "recovered by string scan"}


def read_rtf(path):
    with open(path, errors="replace") as f:
        s = f.read()
    s = re.sub(r"\\'([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), s)
    s = re.sub(r"\\(par|line)d?\b", "\n", s)
    s = re.sub(r"\\[a-zA-Z]+-?\d* ?|[{}]", "", s)
    return s, {}


def read_image(path):
    if not shutil.which("tesseract"):
        return "", {"error": "no OCR tools installed"}
    return _run(["tesseract", path, "-", "--psm", "3"], timeout=180).stdout.decode("utf-8", "replace"), {"ocr_pages": 1}


def read_zip(z, deadline=None, depth=0):
    if depth >= 2:
        return "", {"error": "zip nested too deep"}
    infos = [i for i in z.infolist() if not i.is_dir()]
    if sum(i.file_size for i in infos) > ZIP_MAX_BYTES:
        return "", {"error": f"zip expands to over {ZIP_MAX_BYTES // 1_000_000} MB"}
    parts, members = [], []
    with tempfile.TemporaryDirectory() as d:
        for k, i in enumerate(infos):
            p = os.path.join(d, f"m{k}")
            with z.open(i) as src, open(p, "wb") as dst:
                shutil.copyfileobj(src, dst)
            t, m, inf = read_file(p, i.filename, deadline, depth + 1)
            members.append({"name": i.filename, "method": m, "chars": len(t),
                            **({"error": inf["error"]} if inf.get("error") else {})})
            if t.strip():
                parts.append(f"[{i.filename}]\n{t}")
            os.remove(p)
    return "\n\n".join(parts), {"members": members}


def read_file(path, name="", deadline=None, depth=0):
    """Returns (text, method, info). method is one of pdf, ocr+pdf, docx,
    xlsx, pptx, zip, legacy, rtf, ocr-image, text, unsupported, error. info
    may carry pages / ocr_pages / unread_pages / truncated_rows / members /
    error. Never raises."""
    ext = os.path.splitext(name or path)[1].lower()
    try:
        with open(path, "rb") as f:
            head = f.read(8)
        if head.startswith(b"%PDF"):
            t, i = read_pdf(path, deadline)
            return t, "ocr+pdf" if i.get("ocr_pages") else "pdf", i
        if head.startswith(b"PK"):
            z = zipfile.ZipFile(path)
            names = set(z.namelist())
            if "word/document.xml" in names:
                return _tag(read_docx(z), "docx")
            if "xl/workbook.xml" in names:
                return _tag(read_xlsx(z), "xlsx")
            if "ppt/presentation.xml" in names:
                return _tag(read_pptx(z), "pptx")
            return _tag(read_zip(z, deadline, depth), "zip")
        if head.startswith(b"\xd0\xcf\x11\xe0"):
            return _tag(read_legacy_office(path, ext if ext in (".doc", ".xls", ".ppt") else ".doc"), "legacy")
        if head.startswith(b"{\\rtf"):
            return _tag(read_rtf(path), "rtf")
        if (ext in IMAGE_EXT or head[:3] == b"\xff\xd8\xff" or head.startswith(b"\x89PNG")
                or head[:4] in (b"II*\x00", b"MM\x00*")):
            return _tag(read_image(path), "ocr-image")
        if ext in NO_TEXT_EXT:
            return "", "unsupported", {"error": f"{ext} file (no text to extract)"}
        if ext in TEXT_EXT or not ext or b"\x00" not in head:
            with open(path, errors="replace") as f:
                s = f.read()
            if ext in (".htm", ".html", ".xml") or s.lstrip()[:1] == "<":
                s = html.unescape(re.sub(r"<[^>]+>", " ", s))
            return s, "text", {}
        return "", "unsupported", {"error": f"unsupported {ext or 'binary'} file"}
    except Exception as e:  # noqa: BLE001
        return "", "error", {"error": f"{type(e).__name__}: {e}"[:300]}


def _tag(result, method):
    text, info = result
    return text, method, info
