#!/usr/bin/env python3
"""Give every logged agency message its plain-English reading (inbox.plain), the way the Email Worker now reads new mail.

  python3 explain_backfill.py            # print each message's reading, write nothing
  python3 explain_backfill.py --apply    # store them in D1

Needs schema_plain.sql applied first. Safe to re-run: it rewrites `plain` from the current rules every time, so after
changing explainReply() in src/match.js, run it again to refresh old mail. Touches no other column.
"""
import argparse, json, os, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from import_history import d1  # noqa: E402  (d1 with one retry after a wrangler OAuth refresh)


def cols(rows, names):
    return [dict(zip(names, r)) for r in rows]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write to D1 (default: print only)")
    args = ap.parse_args()
    reqs = cols(d1("SELECT id, agency, summary, summary_short FROM requests WHERE send_status = 'Sent'"), ["id", "agency", "summary", "summary_short"])
    letters = cols(d1("SELECT request_id, substr(letter, 1, 6000) FROM suggestions"), ["request_id", "letter"])
    names = ["id", "received_at", "subject", "request_id", "auto", "text"]
    ids = [r[0] for r in d1("SELECT id FROM inbox ORDER BY id")]
    inbox = []
    for i in range(0, len(ids), 25):
        chunk = ids[i:i + 25]
        inbox += cols(d1("SELECT id, received_at, subject, request_id, auto, substr(text, 1, 60000) FROM inbox WHERE id IN (%s)" % ",".join("?" * len(chunk)), chunk), names)
    out = subprocess.run(["node", os.path.join(HERE, "explain_run.mjs")], input=json.dumps({"requests": reqs, "letters": letters, "inbox": inbox}),
                         capture_output=True, text=True, check=True, cwd=HERE)
    plans = json.loads(out.stdout)
    subj = {m["id"]: m["subject"] for m in inbox}
    for p in plans:
        pl = p["plain"]
        print("#%-4s %-60s -> %s" % (p["id"], (subj.get(p["id"]) or "")[:60], pl["headline"] if pl else "-"))
    if not args.apply:
        print("\nDry run, %d messages. Nothing written. Re-run with --apply." % len(plans))
        return
    for p in plans:
        d1("UPDATE inbox SET plain = ? WHERE id = ?", [json.dumps(p["plain"]) if p["plain"] else None, p["id"]])
    print("\nStored %d readings." % len(plans))


if __name__ == "__main__":
    main()
