#!/usr/bin/env python3
"""Replay the tracker's logged agency mail through the current matcher and fix what it got wrong.

  python3 rematch.py            # report only: what would be re-linked, learned or un-flagged
  python3 rematch.py --apply    # write those changes to D1

Why it exists: before the matcher read the letter first, a sender address that belonged to one request swallowed
every message from that sender (foiastatus@state.gov put 30 other requests' acknowledgments on one request), and State's
acknowledgments, which quote the full request, went unmatched. Also un-flags "needs a reply" on mail whose only trigger was
the agency describing its own deadline ("we will not be able to respond within 20 days") or our own request quoted back.

What --apply does, per message:
- (only mail logged by the Worker, 2026-10-12 onward; the Yahoo history import is left alone)
- inbox.request_id and the matching `messages` row move to the right request (a new `messages` row if there was none);
- a request gets the agency's tracking number when it has none (same rule as the Worker);
- a request still at 'Awaiting reply' becomes 'Acknowledged' when the re-linked mail was an acknowledgment;
- inbox.needs_reply/reason are cleared for mail still marked 'new' that holds no real ask.
It never touches mail that already has a draft, and never unlinks mail unless the letter names a different case number.
"""
import argparse, json, os, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from import_history import d1  # noqa: E402  (d1 with one retry after a wrangler OAuth refresh)


def cols(rows, names):
    return [dict(zip(names, r)) for r in rows]


def fetch():
    reqs = cols(d1("SELECT id, agency, email, tracking, summary, summary_short, sent_on FROM requests WHERE send_status = 'Sent'"),
                ["id", "agency", "email", "tracking", "summary", "summary_short", "sent_on"])
    letters = cols(d1("SELECT request_id, substr(letter, 1, 6000) FROM suggestions"), ["request_id", "letter"])
    confs = {str(sid): rid for sid, rid in d1("SELECT source_id, request_id FROM messages WHERE source = 'foia.gov' AND source_id IS NOT NULL")}
    names = ["id", "received_at", "from_addr", "subject", "message_id", "snippet", "attachments", "request_id", "auto", "needs_reply", "reason", "status", "text"]
    ids = [r[0] for r in d1("SELECT id FROM inbox ORDER BY id")]
    inbox = []
    for i in range(0, len(ids), 25):
        chunk = ids[i:i + 25]
        inbox += cols(d1("SELECT id, received_at, from_addr, subject, message_id, snippet, attachments, request_id, auto, needs_reply, reason, status, substr(text, 1, 60000) FROM inbox WHERE id IN (%s)" % ",".join("?" * len(chunk)), chunk), names)
    return {"requests": reqs, "letters": letters, "extra": [], "confs": confs, "inbox": inbox}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write the changes to D1 (default: report only)")
    args = ap.parse_args()

    data = fetch()
    out = subprocess.run(["node", os.path.join(HERE, "rematch_run.mjs")], input=json.dumps(data), capture_output=True, text=True, check=True, cwd=HERE)
    plan = json.loads(out.stdout)
    by_id = {m["id"]: m for m in data["inbox"]}
    reqs = {r["id"]: r for r in data["requests"]}
    drafted = {r[0] for r in d1("SELECT inbox_id FROM drafts WHERE inbox_id IS NOT NULL")}
    name = lambda i: ("#%s %s" % (i, (reqs[i]["summary_short"] or reqs[i]["agency"])[:58])) if i in reqs else str(i)

    print("Tracking numbers to learn (%d):" % len(plan["learned"]))
    for rid, v in plan["learned"].items():
        print("  %-12s  %s   <- inbox #%s" % (v["tracking"], name(int(rid)), v["inbox_id"]))
    relink = [r for r in plan["relink"] if r["id"] not in drafted]
    skipped = [r for r in plan["relink"] if r["id"] in drafted]
    print("\nMail to re-link (%d):" % len(relink))
    for r in relink:
        print("  inbox #%-4s %-34s %s\n      was %s\n      now %s   (%s)" % (r["id"], r["from"][:34], r["subject"][:60], name(r["was"]) if r["was"] else "unmatched", name(r["now"]) if r["now"] else "unmatched", r["why"]))
    if skipped:
        print("\nSkipped, already has a draft (%d): %s" % (len(skipped), ", ".join("#%s" % r["id"] for r in skipped)))
    print("\nFlags to clear, no real ask in the letter (%d):" % len(plan["unflag"]))
    for u in plan["unflag"]:
        print("  inbox #%-4s %-34s '%s'  %s" % (u["id"], u["from"][:34], u["reason"], u["subject"][:50]))
    if plan["leave"]:
        print("\nLeft as they are, no longer matched but nothing contradicts the old link (%d): %s" % (len(plan["leave"]), ", ".join("#%s" % r["id"] for r in plan["leave"])))

    if not args.apply:
        print("\nDry run. Nothing written. Re-run with --apply to make these changes.")
        return

    have = {(r[0], r[1]) for r in d1("SELECT source_id, request_id FROM messages WHERE source = 'foia-inbox' AND source_id IS NOT NULL")}
    n = 0
    for rid, v in plan["learned"].items():
        d1("UPDATE requests SET tracking = ? WHERE id = ? AND (tracking IS NULL OR tracking = '')", [v["tracking"], int(rid)]); n += 1
    for r in relink:
        m = by_id[r["id"]]
        d1("UPDATE inbox SET request_id = ? WHERE id = ?", [r["now"], r["id"]]); n += 1
        if not r["now"]:  # unlinked: messages.request_id can't be null, so drop the wrong log entry (the mail stays in inbox)
            if m["message_id"] and r["was"]:
                d1("DELETE FROM messages WHERE source = 'foia-inbox' AND source_id = ? AND request_id = ?", [m["message_id"], r["was"]])
        elif m["message_id"] and (m["message_id"], r["was"]) in have and r["was"]:
            d1("UPDATE messages SET request_id = ? WHERE source = 'foia-inbox' AND source_id = ? AND request_id = ?", [r["now"], m["message_id"], r["was"]])
        elif r["now"] and not (m["message_id"] and any(h[0] == m["message_id"] and h[1] == r["now"] for h in have)):
            d1("INSERT INTO messages (request_id, direction, sent_at, from_addr, subject, snippet, source, source_id) VALUES (?, 'in', ?, ?, ?, ?, 'foia-inbox', ?)",
               [r["now"], str(m["received_at"])[:16].replace("T", " "), m["from_addr"], m["subject"], m["snippet"], m["message_id"]])
        if r["now"] and m["reason"] != "Resubmit required":  # an acknowledgment: same bookkeeping the Worker does
            day = str(m["received_at"])[:10]
            d1("UPDATE requests SET acknowledged = 'Yes', ack_on = COALESCE(ack_on, ?), status = CASE WHEN status = 'Awaiting reply' THEN 'Acknowledged' ELSE status END, "
               "last_activity_on = CASE WHEN last_activity_on IS NULL OR last_activity_on < ? THEN ? ELSE last_activity_on END, "
               "last_activity = CASE WHEN last_activity_on IS NULL OR last_activity_on < ? THEN ? ELSE last_activity END WHERE id = ?",
               [day, day, day, day, m["subject"], r["now"]])
    for u in plan["unflag"]:
        d1("UPDATE inbox SET needs_reply = 0, reason = NULL WHERE id = ? AND status = 'new'", [u["id"]]); n += 1
    print("\nApplied %d changes." % n)


if __name__ == "__main__":
    main()
