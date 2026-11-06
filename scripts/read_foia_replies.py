#!/usr/bin/env python3
"""Claude reads each new agency email and sets its description and the request's status (added 2026-11-06).

The foia-inbox Worker's rules (match.js) read mail the instant it arrives, but rules only know the wording someone has seen
(Education's portal says "updated to the following status 'Closed'", which no rule caught). This pass runs one no-tools
`claude -p` call per batch of unread messages (prompt-foia-read.txt) and POSTs the readings to the tracker's
/api/foia-reads, which stores them as inbox.plain and moves the request's status. Nothing is emailed.

  python3 scripts/read_foia_replies.py                  # Actions: read every unread message via the site API, write back
  python3 scripts/read_foia_replies.py --local --ids 17,67,312   # dry run from the live D1 via wrangler: print only

Env: CLAUDE_CODE_OAUTH_TOKEN, FOIA_INGEST_TOKEN (API mode), FOIA_SITE, FOIA_MODEL (default sonnet).
Never fails the run: errors are printed and the script exits 0.
"""
import json, os, re, shutil, subprocess, sys, urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SITE = os.environ.get("FOIA_SITE", "https://markalfred.news")
BATCH, MAX_BATCHES = 12, 4
SYSTEM = ("You read agency emails about a reporter's public records requests and report what they say. Follow the instructions exactly. "
          "Reply with the JSON object only: no prose before or after it, no code fences.")


def api(path, payload=None):
    req = urllib.request.Request(SITE + path, method="POST" if payload is not None else "GET",
                                 data=json.dumps(payload).encode() if payload is not None else None,
                                 headers={"Authorization": "Bearer " + os.environ["FOIA_INGEST_TOKEN"], "Content-Type": "application/json",
                                          "User-Agent": "dc-record-foia-read"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def local_fetch(ids):
    sys.path.insert(0, os.path.join(ROOT, "foia-inbox"))
    from import_history import d1
    rq = ["id", "agency", "email", "tracking", "status", "sent_on", "summary_short", "summary"]
    reqs = [dict(zip(rq, r)) for r in d1("SELECT id, agency, email, tracking, status, sent_on, summary_short, substr(summary, 1, 500) FROM requests WHERE send_status = 'Sent' ORDER BY sent_on DESC")]
    mq = ["id", "received_at", "from_addr", "from_name", "subject", "text", "request_id", "plain"]
    msgs = [dict(zip(mq, r)) for r in d1("SELECT id, received_at, from_addr, from_name, subject, substr(text, 1, 7000), request_id, plain FROM inbox WHERE id IN (%s) ORDER BY id" % ",".join("?" * len(ids)), ids)]
    return msgs, reqs


def build_prompt(msgs, reqs):
    lines = ["=== MARK'S REQUESTS ==="]
    for r in reqs:
        lines.append(f"[{r['id']}] {r['agency']} | case # {r.get('tracking') or 'none'} | {r.get('status')} | sent {r.get('sent_on')} | {(r.get('summary_short') or r.get('summary') or '')[:140]}")
    lines.append("\n=== MESSAGES ===")
    for m in msgs:
        try:
            was = (json.loads(m["plain"]) or {}).get("headline") if m.get("plain") else None
        except ValueError:
            was = None
        text = re.sub(r"\n{3,}", "\n\n", m.get("text") or "")[:5000]
        lines.append(f"\n[message {m['id']}] {m['received_at']} from {m.get('from_name') or ''} <{m['from_addr']}>\nSubject: {m['subject']}\n"
                     f"Currently linked to request: {m.get('request_id') or 'none'}; the rules' guess: {was or 'none'}\n{text}")
    with open(os.path.join(ROOT, "prompt-foia-read.txt")) as f:
        return f.read() + "\n" + "\n".join(lines)


def ask_claude(prompt):
    exe = shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")
    proc = subprocess.run([exe, "-p", "--tools", "", "--system-prompt", SYSTEM, "--output-format", "json", "--effort", "medium",
                           "--model", os.environ.get("FOIA_MODEL", "sonnet")], input=prompt, capture_output=True, text=True, timeout=600,
                          env={**os.environ, "DISABLE_PROMPT_CACHING": "1"})
    envl = json.loads(proc.stdout)
    print(f"foia-read cost_usd={envl.get('total_cost_usd')}", file=sys.stderr)
    if envl.get("is_error"):
        raise RuntimeError(str(envl.get("result"))[:300])
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", (envl.get("result") or "").strip())
    return json.loads(text[text.find("{"): text.rfind("}") + 1]).get("reads") or []


def main():
    args = sys.argv[1:]
    local = "--local" in args
    if local:
        ids = [int(x) for x in args[args.index("--ids") + 1].split(",")]
        msgs, reqs = local_fetch(ids)
        batches = [msgs[i:i + BATCH] for i in range(0, len(msgs), BATCH)]
    else:
        got = api(f"/api/foia-reads?limit={BATCH * MAX_BATCHES}")
        reqs = got["requests"]
        batches = [got["messages"][i:i + BATCH] for i in range(0, len(got["messages"]), BATCH)]
    if not batches:
        print("foia-read: nothing unread", file=sys.stderr)
        return
    for b in batches:
        reads = ask_claude(build_prompt(b, reqs))
        ok = {m["id"] for m in b}
        reads = [r for r in reads if isinstance(r, dict) and r.get("inbox_id") in ok]
        if local:
            for r in reads:
                print(f"#{r['inbox_id']} -> request {r.get('request_id')} | status {r.get('status')} | {r.get('kind')} | {r.get('headline')} | reply? {r.get('needs_reply')}")
                for p in r.get("points") or []:
                    print("     -", p)
                print("     status line:", r.get("status_detail"))
        else:
            print(f"foia-read: posted {api('/api/foia-reads', {'reads': reads})} ({len(reads)}/{len(b)} read)", file=sys.stderr)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # never fail the edition
        print(f"foia-read: failed: {e}", file=sys.stderr)
