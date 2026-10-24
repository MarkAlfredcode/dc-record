#!/usr/bin/env python3
"""Backfill attachment text for FOIA mail the Worker logged before it could read attachments (added 2026-10-22).

The raw messages, with their attachments, only ever reached Mark's Yahoo inbox, so this pulls them back from
there over IMAP, reads each attachment (PDF text layer, scans via tesseract, .docx, .txt, ZIPs) and writes the
text into the tracker's D1 table `inbox_attachments`, matching rows in `inbox` by Message-ID. It also re-runs the
denial check so newly readable determinations show up in the drafting queue.

  YAHOO_APP_PASSWORD=...  python3 backfill_attachments.py --days 14
  python3 backfill_attachments.py --eml DIR            # same thing from saved .eml files (no IMAP)
  python3 backfill_attachments.py --days 150 --bytes   # also keep the original files (<= 1 MB) for mail already read, so appeals can re-attach them

Yahoo needs an app password (Account Security > Generate app password); the normal password won't work.
D1 writes use wrangler's OAuth token (run `npx wrangler whoami` first if it has expired).
Run with `python3 -I`-style hygiene in mind: attachments are untrusted, they are only parsed, never executed.
"""
import argparse, datetime as dt, email, email.policy, imaplib, io, json, os, re, subprocess, sys, tempfile, urllib.request, zipfile

ACCOUNT = "201dc660171a81b28b70ae1d0f95f69e"
DB = "9853f320-7a56-495c-ab09-5548c0efa955"
DENIED = re.compile(r"neither confirm nor deny|expedited (processing|treatment)[^.]{0,80}(denied|not granted)|(deny|denying|denied|denial of) (your )?(request for )?expedit|your request (is|has been) (hereby )?(denied|closed)|adverse determination|no (responsive )?records (were|was) (found|located)", re.I)
MAXT = 60000
KEEP_BYTES = 1000 * 1000
KEEP_EXT = ("pdf", "docx", "txt", "htm", "html", "csv")


BYTES_ONLY = False


def d1(sql, params=None):
    cfg = open(os.path.expanduser("~/Library/Preferences/.wrangler/config/default.toml")).read()
    tok = re.search(r'^oauth_token\s*=\s*"([^"]+)"', cfg, re.M).group(1)
    req = urllib.request.Request(f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT}/d1/database/{DB}/raw",
                                 data=json.dumps({"sql": sql, "params": params or []}).encode(),
                                 headers={"Authorization": "Bearer " + tok, "Content-Type": "application/json"})
    try:
        r = json.load(urllib.request.urlopen(req, timeout=60))
    except urllib.error.HTTPError as e:
        r = json.loads(e.read())
    if not r.get("success"):
        raise RuntimeError(r.get("errors"))
    return r["result"][0]["results"]["rows"]


def clean(t):
    return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+\n", "\n", t.replace("\r", ""))).strip()


def read_bytes(name, data, depth=0):
    """-> list of (name, status, note, text)"""
    ext = name.lower().rsplit(".", 1)[-1] if "." in name else ""
    try:
        if ext == "pdf":
            with tempfile.TemporaryDirectory() as td:
                p = os.path.join(td, "a.pdf")
                open(p, "wb").write(data)
                txt = subprocess.run(["pdftotext", "-layout", p, "-"], capture_output=True, text=True, timeout=120).stdout
                if len(txt.strip()) < 40:   # scan: OCR the first pages
                    subprocess.run(["pdftoppm", "-r", "200", "-l", "6", "-png", p, os.path.join(td, "pg")], timeout=180)
                    txt = "\n".join(subprocess.run(["tesseract", os.path.join(td, f), "-"], capture_output=True, text=True, timeout=120).stdout
                                    for f in sorted(os.listdir(td)) if f.startswith("pg") and f.endswith(".png"))
                    return [(name, "read" if len(txt.strip()) >= 40 else "no_text", "OCR (first 6 pages)" if txt.strip() else "no text found", clean(txt)[:MAXT])]
                return [(name, "read", None, clean(txt)[:MAXT])]
        if ext == "docx":
            x = zipfile.ZipFile(io.BytesIO(data)).read("word/document.xml").decode("utf8", "ignore")
            x = re.sub(r"</w:p>", "\n", x)
            x = re.sub(r"<[^>]+>", "", x).replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
            return [(name, "read", None, clean(x)[:MAXT])]
        if ext in ("txt", "csv", "htm", "html"):
            t = data.decode("utf8", "ignore")
            if ext.startswith("htm"):
                t = re.sub(r"<[^>]+>", " ", t)
            return [(name, "read", None, clean(t)[:MAXT])]
        if ext == "zip" and depth == 0:
            z = zipfile.ZipFile(io.BytesIO(data))
            names = [n for n in z.namelist() if not n.endswith("/") and not n.startswith("__MACOSX")][:40]
            out = [(name, "read", None, "ZIP containing: " + "; ".join(names))]
            for n in names:
                out += read_bytes(name + " > " + n.split("/")[-1], z.read(n), 1)
            return out
        return [(name, "skipped", f"not read: {ext or 'unknown type'}", "")]
    except Exception as e:  # noqa: BLE001
        return [(name, "error", str(e)[:200], "")]


def keep(name, data, depth=0):
    """-> [(display name, bytes)] for the originals worth keeping (same names read_bytes uses, ZIP members as 'zip > member')."""
    ext = name.lower().rsplit(".", 1)[-1] if "." in name else ""
    if ext == "zip" and depth == 0:
        try:
            z = zipfile.ZipFile(io.BytesIO(data))
            return [x for n in z.namelist() if not n.endswith("/") and not n.startswith("__MACOSX")
                    for x in keep(name + " > " + n.split("/")[-1], z.read(n), 1)]
        except Exception:  # noqa: BLE001
            return []
    return [(name, data)] if ext in KEEP_EXT and 0 < len(data) <= KEEP_BYTES else []


def store_bytes(inbox_id, msg):
    import base64
    n = 0
    for part in msg.walk():
        fn = part.get_filename()
        if fn and not part.is_multipart():
            for name, data in keep(fn, part.get_payload(decode=True) or b""):
                d1("UPDATE inbox_attachments SET data = ? WHERE inbox_id = ? AND filename = ? AND data IS NULL", [base64.b64encode(data).decode(), inbox_id, name])
                n += 1
    return n


def handle(msg):
    mid = (msg["Message-ID"] or "").strip()
    if not mid:
        return
    rows = d1("SELECT id, text FROM inbox WHERE message_id = ?", [mid])
    if not rows:
        return
    inbox_id, old_text = rows[0]
    if d1("SELECT 1 FROM inbox_attachments WHERE inbox_id = ? LIMIT 1", [inbox_id]):
        if BYTES_ONLY and store_bytes(inbox_id, msg):
            print(f"inbox {inbox_id}: kept original file(s) from {msg['Subject']!r}")
        return
    got = []
    for part in msg.walk():
        fn = part.get_filename()
        if fn and not part.is_multipart():
            got += read_bytes(fn, part.get_payload(decode=True) or b"")
    if not got:
        return
    for name, st, note, txt in got:
        d1("INSERT INTO inbox_attachments (inbox_id, filename, status, note, text) VALUES (?, ?, ?, ?, ?)", [inbox_id, name, st, note, txt])
    store_bytes(inbox_id, msg)
    att = "\n\n".join(t for _, s, _, t in got if s == "read" and t)
    if att:
        d1("UPDATE inbox SET text = ? WHERE id = ?", [((old_text or "") + "\n\n[ATTACHMENT TEXT]\n" + att)[:MAXT], inbox_id])
        m = DENIED.search(att)
        if m:
            d1("UPDATE inbox SET needs_reply = 1, reason = ? WHERE id = ? AND (reason IS NULL OR reason NOT LIKE 'Denial:%')", ["Denial: " + m.group(0), inbox_id])
    print(f"inbox {inbox_id}: {len(got)} attachment record(s) from {msg['Subject']!r}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--eml")
    ap.add_argument("--bytes", action="store_true", help="also store the original files for messages whose text was already read")
    a = ap.parse_args()
    global BYTES_ONLY
    BYTES_ONLY = a.bytes
    if a.eml:
        for f in sorted(os.listdir(a.eml)):
            if f.endswith(".eml"):
                handle(email.message_from_binary_file(open(os.path.join(a.eml, f), "rb"), policy=email.policy.default))
        return
    pw = os.environ.get("YAHOO_APP_PASSWORD")
    if not pw:
        sys.exit("Set YAHOO_APP_PASSWORD to a Yahoo app password.")
    M = imaplib.IMAP4_SSL("imap.mail.yahoo.com")
    M.login("markdalfred10@yahoo.com", pw)
    M.select("INBOX", readonly=True)
    since = (dt.date.today() - dt.timedelta(days=a.days)).strftime("%d-%b-%Y")
    _, ids = M.search(None, f'(SINCE {since} TO "foia@markalfred.news")')
    for i in ids[0].split():
        _, d = M.fetch(i, "(BODY.PEEK[])")
        handle(email.message_from_bytes(d[0][1], policy=email.policy.default))
    M.logout()


if __name__ == "__main__":
    main()
