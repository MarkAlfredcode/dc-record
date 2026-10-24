#!/usr/bin/env python3
"""Draft replies and appeals for the FOIA tracker (added 2026-10-12).

Pulls the tracker's queue (agency emails flagged as needing an answer by the
foia-inbox worker, plus recent denials), runs one no-tools `claude -p` pass
with prompt-foia-replies.txt, and posts the drafts back. They wait in the
tracker's "Replies & appeals" tab for Mark to edit and send; nothing is emailed
from here. Never fails the run: errors are printed and the script exits 0.

Env: CLAUDE_CODE_OAUTH_TOKEN, FOIA_INGEST_TOKEN (required), FOIA_SITE
(default https://markalfred.news), FOIA_MODEL (default sonnet).
"""
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SITE = os.environ.get("FOIA_SITE", "https://markalfred.news")
SYSTEM = ("You draft emails for a reporter's public records requests. Follow the instructions exactly. "
          "Reply with the JSON object only: no prose before or after it, no code fences.")


def api(path, payload=None):
    req = urllib.request.Request(SITE + path, method="POST" if payload is not None else "GET",
                                 data=json.dumps(payload).encode() if payload is not None else None,
                                 headers={"Authorization": "Bearer " + os.environ["FOIA_INGEST_TOKEN"],
                                          "Content-Type": "application/json", "User-Agent": "dc-record-foia/1"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def thread_text(thread):
    return "\n".join(f"  {m['sent_at'][:10]} {'SENT' if m['direction'] == 'out' else 'FROM ' + (m.get('from_addr') or '')}: "
                     f"{m.get('subject') or ''} | {(m.get('snippet') or '')[:300]}" for m in thread)


def main():
    if not os.environ.get("FOIA_INGEST_TOKEN"):
        print("foia-replies: FOIA_INGEST_TOKEN not set; skipping", file=sys.stderr)
        return
    q = api("/api/foia-queue")
    replies, appeals = q.get("replies") or [], q.get("appeals") or []
    if not replies and not appeals:
        print("foia-replies: queue empty", file=sys.stderr)
        return
    parts = ["\n=== REPLIES ==="]
    for it in replies:
        m, r = it["inbox"], it["request"] or {}
        atts = "\n".join(f"  ATTACHMENT {x['filename']} [{x['status']}{': ' + x['note'] if x.get('note') else ''}]\n{(x.get('text') or '')[:3500]}"
                         for x in it.get("attachments") or [])
        parts.append(f"\n[inbox {m['id']}] Request {r.get('id')}: {r.get('agency')} ({r.get('statute') or ''}), sent {r.get('sent_on')}, "
                     f"tracking {r.get('tracking') or 'none'}, status {r.get('status')}\nRequest: {(r.get('summary') or '')[:600]}\n"
                     f"FLAG: {m.get('reason') or ''}\n"
                     f"Thread:\n{thread_text(it['thread'])}\nTHE EMAIL TO ANSWER (from {m['from_addr']}, {m['received_at'][:10]}): "
                     f"{m.get('subject')}\n{(m.get('text') or '')[:5000]}\n{atts}")
    parts.append("\n=== APPEALS ===")
    for it in appeals:
        r = it["request"]
        parts.append(f"\n[request {r['id']}] {r['agency']} ({r.get('statute') or ''}, {r.get('state') or ''}), sent {r.get('sent_on')}, "
                     f"tracking {r.get('tracking') or 'none'}\nRequest: {(r.get('summary') or '')[:600]}\n"
                     f"Status detail: {r.get('status_detail') or ''}\nThread:\n{thread_text(it['thread'])}")
    with open(os.path.join(ROOT, "prompt-foia-replies.txt")) as f:
        prompt = f.read() + "\n".join(parts)

    exe = shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")
    proc = subprocess.run([exe, "-p", "--tools", "", "--system-prompt", SYSTEM, "--output-format", "json",
                           "--effort", "medium", "--model", os.environ.get("FOIA_MODEL", "sonnet")],
                          input=prompt, capture_output=True, text=True, timeout=900,
                          env={**os.environ, "DISABLE_PROMPT_CACHING": "1"})
    envl = json.loads(proc.stdout)
    print(f"foia-replies cost_usd={envl.get('total_cost_usd')}", file=sys.stderr)
    if envl.get("is_error"):
        raise RuntimeError(str(envl.get("result"))[:300])
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", (envl.get("result") or "").strip())
    reply = json.loads(text[text.find("{"): text.rfind("}") + 1])

    by_inbox = {it["inbox"]["id"]: it for it in replies}
    by_req = {it["request"]["id"]: it for it in appeals}
    out = []
    for d in reply.get("drafts") or []:
        if d.get("skip"):
            print(f"foia-replies: skipped {d.get('queue')} {d.get('ref')}: {d.get('skip_reason')}", file=sys.stderr)
            continue
        try:
            ref = int(d["ref"])
            if d["queue"] == "reply" and ref in by_inbox:
                item = {"request_id": by_inbox[ref]["inbox"]["request_id"], "inbox_id": ref}
            elif d["queue"] == "appeal" and ref in by_req:
                item = {"request_id": ref, "context": by_req[ref]["request"].get("status_detail")}
            else:
                raise ValueError("unknown ref")
            kind = d.get("kind") if d.get("kind") in ("reply", "appeal", "resend") else ("reply" if d["queue"] == "reply" else "appeal")
            item.update(kind=kind, subject=d["subject"].strip(), body=d["body"].strip() + "\n\nMark Alfred\nfoia@markalfred.news",
                        note=(d.get("note") or "").strip() or None, deadline=(d.get("deadline") or "").strip() or None)
            out.append(item)
        except (KeyError, ValueError, TypeError) as e:
            print(f"foia-replies: dropped malformed draft ({e}): {str(d)[:200]}", file=sys.stderr)
    if out:
        print(f"foia-replies: posted {api('/api/foia-drafts', {'drafts': out})}", file=sys.stderr)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001 -- drafting must never break an edition run
        print(f"foia-replies: skipped ({type(e).__name__}: {e})", file=sys.stderr)
    sys.exit(0)
