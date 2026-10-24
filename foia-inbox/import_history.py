#!/usr/bin/env python3
"""One-off: pull FOIA mail from before the foia-inbox Worker started logging (2026-10-12) out of Yahoo and
add it to the tracker's `inbox` table with its attachment text, matched to requests where the match is certain.

  YAHOO_APP_PASSWORD=...  python3 import_history.py --since 2026-06-01 --before 2026-10-13 [--dry-run]

What it does and doesn't do:
- Reads Yahoo over IMAP read-only (BODY.PEEK) for mail addressed to foia@markalfred.news.
- Skips anything already in `inbox` (by Message-ID) and anything from @markalfred.news.
- Inserts into `inbox` as status 'handled' (so old mail never floods "needs a look" or the drafting queue) with the
  mail's real date, and attachment text into `inbox_attachments`. request_id is set only on a `sure` match
  (src/match.js, run through node).
- Never touches `requests` or `messages` (the correspondence log already has this mail from the earlier import).
- Prints what it found that matters: newsworthy replies and denials, with dates and agencies.
The newsworthy banner in the tracker covers the last 60 days of inbox rows, so older hits are only in this report.
"""
import argparse, datetime as dt, email, email.policy, email.utils, imaplib, json, os, re, subprocess, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backfill_attachments as _bf  # noqa: E402
from backfill_attachments import read_bytes, clean, DENIED  # noqa: E402

SKIP_FROM = re.compile(r"@(yahoo\.com|([a-z0-9-]+\.)*resend\.com|([a-z0-9-]+\.)*cloudflare\.com)$", re.I)  # Mark's own mail, our tools' notices


def d1(sql, params=None):
    """backfill's d1() with one retry after refreshing wrangler's OAuth token (it lasts about an hour)."""
    try:
        return _bf.d1(sql, params)
    except RuntimeError as e:
        if "Authentication" not in str(e):
            raise
        subprocess.run(["npx", "wrangler", "whoami"], capture_output=True, cwd=HERE, timeout=120)
        return _bf.d1(sql, params)

HERE = os.path.dirname(os.path.abspath(__file__))
AUTO = re.compile(r"automatic reply|auto(-|matic)?\s?reply|out of (the )?office|autoreply", re.I)


def body_text(msg):
    part = msg.get_body(preferencelist=("plain", "html"))
    if not part:
        return ""
    t = part.get_content()
    if part.get_content_type() == "text/html":
        t = re.sub(r"<style[\s\S]*?</style>|<script[\s\S]*?</script>", " ", t)
        t = re.sub(r"<br\s*/?>|</p>|</div>", "\n", t)
        t = re.sub(r"<[^>]+>", " ", t)
    return clean(t)[:30000]


def parse(msg):
    mid = (msg["Message-ID"] or "").strip()
    frm = email.utils.parseaddr(str(msg["From"] or ""))
    try:
        when = email.utils.parsedate_to_datetime(msg["Date"]).astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    except Exception:  # noqa: BLE001
        when = None
    atts = []
    for part in msg.walk():
        fn = part.get_filename()
        if fn and not part.is_multipart():
            atts += read_bytes(fn, part.get_payload(decode=True) or b"")
    body = body_text(msg)
    att_text = "\n\n".join(t for _, s, _, t in atts if s == "read" and t)
    return {"mid": mid, "from": frm[1].lower(), "name": frm[0] or None, "to": str(msg["To"] or "")[:300], "subject": str(msg["Subject"] or "")[:500],
            "date": when, "in_reply_to": (msg["In-Reply-To"] or None), "refs": (msg["References"] or None), "body": body, "att_text": att_text,
            "atts": atts, "filenames": [a[0] for a in atts if " > " not in a[0]]}


def node_match(requests, confs, msgs):
    payload = json.dumps({"requests": requests, "confs": confs, "messages": [
        {"key": i, "from": m["from"], "subject": m["subject"], "filenames": m["filenames"], "text": (m["body"] + "\n\n" + m["att_text"])[:60000], "own": ""} for i, m in enumerate(msgs)]})
    out = subprocess.run(["node", os.path.join(HERE, "import_match.mjs")], input=payload, capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-06-01")
    ap.add_argument("--before", default="2026-10-13")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    pw = os.environ.get("YAHOO_APP_PASSWORD") or sys.exit("Set YAHOO_APP_PASSWORD to a Yahoo app password.")
    fmt = lambda s: dt.date.fromisoformat(s).strftime("%d-%b-%Y")
    M = imaplib.IMAP4_SSL("imap.mail.yahoo.com")
    M.login("markdalfred10@yahoo.com", pw)
    M.select("INBOX", readonly=True)
    _, ids = M.search(None, f'(SINCE {fmt(a.since)} BEFORE {fmt(a.before)} TO "foia@markalfred.news")')
    ids = ids[0].split()
    print(f"{len(ids)} messages in Yahoo for {a.since} to {a.before}")
    have = {r[0] for r in d1("SELECT message_id FROM inbox WHERE message_id IS NOT NULL")}
    msgs = []
    for i in ids:
        _, d = M.fetch(i, "(BODY.PEEK[])")
        m = parse(email.message_from_bytes(d[0][1], policy=email.policy.default))
        if not m["mid"] or m["mid"] in have or not m["from"] or m["from"].endswith("@markalfred.news") or SKIP_FROM.search(m["from"]):
            continue
        msgs.append(m)
    M.logout()
    print(f"{len(msgs)} new to import")
    cols = "id, agency, email, tracking, summary, summary_short"
    requests = [dict(zip(cols.split(", "), r)) for r in d1(f"SELECT {cols} FROM requests WHERE send_status = 'Sent'")]
    confs = {str(s): r for s, r in d1("SELECT source_id, request_id FROM messages WHERE source = 'foia.gov' AND source_id IS NOT NULL")}
    res = {r["key"]: r for r in node_match(requests, confs, msgs)}
    by_id = {r["id"]: r for r in requests}

    sure = unmatched = 0
    report = []
    for i, m in enumerate(msgs):
        r = res.get(i, {})
        rid = r["match"]["id"] if r.get("match") and r["match"]["sure"] else None
        sure += bool(rid); unmatched += not rid
        full = m["body"] + (("\n\n[ATTACHMENT TEXT]\n" + m["att_text"]) if m["att_text"] else "")
        den = DENIED.search(full)
        if r.get("news") or den:
            report.append((m["date"], m["from"], m["subject"][:90], by_id[rid]["agency"] if rid else "(unmatched)",
                           ((r.get("news") or {}).get("label") + ': "…' + r["news"]["excerpt"][:230] + '…"') if r.get("news") else ("denial: " + den.group(0))))
        if a.dry_run:
            continue
        auto = 1 if AUTO.search(m["subject"]) else 0
        d1("INSERT INTO inbox (received_at, from_addr, from_name, to_addr, subject, message_id, in_reply_to, refs, text, snippet, attachments, request_id, auto, needs_reply, reason, status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,0,NULL,'handled')",
           [m["date"] or dt.datetime.now(dt.timezone.utc).isoformat(), m["from"], m["name"], m["to"], m["subject"], m["mid"], m["in_reply_to"], m["refs"], full[:60000],
            (m["body"][:400] or None), "; ".join(m["filenames"]) or None, rid, auto])
        row = d1("SELECT id FROM inbox WHERE message_id = ?", [m["mid"]])
        for name, st, note, txt in m["atts"]:
            d1("INSERT INTO inbox_attachments (inbox_id, filename, status, note, text) VALUES (?,?,?,?,?)", [row[0][0], name, st, note, txt])
    print(f"{'Would import' if a.dry_run else 'Imported'} {len(msgs)}: {sure} matched to a request, {unmatched} left unmatched (stored as handled).")
    if report:
        print("\nWorth a look (newsworthy language or a denial):")
        for d_, f, s, ag, why in sorted(report):
            print(f"  {d_[:10]}  {ag[:40]:40}  {why}\n      {f} | {s}")


if __name__ == "__main__":
    main()
