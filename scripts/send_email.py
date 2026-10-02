#!/usr/bin/env python3
"""Sends the rendered newsletter via Resend and writes the STATUS FLAG file
based on the REAL HTTP response -- not Claude self-reporting after reading
the response in its own context. This removes an entire self-reporting
hallucination surface (and the RESEND_API_KEY/Resend-handling instructions
entirely from Claude's prompt/token budget).

Usage: python3 send_email.py newsletter.html "DC Record • Executive Branch • <date>" \
    --to email1,email2,... [--from "DC Record • Executive Branch <newsletter@markalfred.news>"]
Requires: RESEND_API_KEY env var.
Resend sits behind a Cloudflare bot check that blocks the default
urllib/requests user agent (403 / error 1010) but accepts plain curl -- so
this shells out to curl, same as the prompt used to instruct Claude to do.

Shared across all emailed DC Record editions (Executive Branch, Campaigns &
Congress, Lobbying) -- pass --from to identify which edition is sending.
--to is required as of 2026-09-16: the caller workflow now always looks it
up live from markalfred.news/dc-record-recipients (real subscribers, per
edition/AM-PM) before calling this script, rather than this script falling
back to a hardcoded 3-person list -- see dc-record-recipients.js in the
markalfredsite repo.
"""
import json
import os
import subprocess
import sys

DEFAULT_FROM = "DC Record • Executive Branch <newsletter@markalfred.news>"


def make_text_summary(html_path):
    # A short plain-text fallback; doesn't need to be fancy, Resend just wants something.
    return "This edition is best viewed in an HTML-capable mail client. See the full newsletter above."


def main():
    if len(sys.argv) < 3 or "--to" not in sys.argv:
        print("usage: send_email.py newsletter.html subject --to email1,email2 [--from ...]", file=sys.stderr)
        sys.exit(1)
    html_path, subject = sys.argv[1], sys.argv[2]
    to = sys.argv[sys.argv.index("--to") + 1].split(",")
    to = [e.strip() for e in to if e.strip()]
    if not to:
        print("usage: send_email.py newsletter.html subject --to email1,email2 [--from ...]", file=sys.stderr)
        sys.exit(1)
    from_addr = DEFAULT_FROM
    if "--from" in sys.argv:
        from_addr = sys.argv[sys.argv.index("--from") + 1]

    api_key = os.environ.get("RESEND_API_KEY")
    if not api_key:
        with open("email_failed.flag", "w") as f:
            f.write("RESEND_API_KEY not set")
        sys.exit(1)

    with open(html_path) as f:
        html_content = f.read()

    payload = {
        "from": from_addr, "to": to, "subject": subject,
        "html": html_content, "text": make_text_summary(html_path),
    }
    # Pass the payload via stdin (-d @-), not as a command-line argument --
    # a large edition's HTML can exceed the OS's exec argument-size limit
    # (ARG_MAX), which fails with "OSError: [Errno 7] Argument list too
    # long" before curl even runs. Confirmed this hit The Feds (176KB
    # rendered HTML) on 2026-10-02 while the three smaller editions were
    # fine. Stdin has no such limit.
    proc = subprocess.run(
        ["curl", "-s", "https://api.resend.com/emails",
         "-H", f"Authorization: Bearer {api_key}",
         "-H", "Content-Type: application/json",
         "-d", "@-"],
        input=json.dumps(payload), capture_output=True, text=True, timeout=60,
    )
    try:
        resp = json.loads(proc.stdout)
    except json.JSONDecodeError:
        resp = {"raw": proc.stdout}

    if isinstance(resp, dict) and resp.get("id"):
        with open("email_sent.flag", "w") as f:
            f.write(subject)
        print(f"sent: {resp['id']}", file=sys.stderr)
    else:
        with open("email_failed.flag", "w") as f:
            f.write(json.dumps(resp))
        print(f"send failed: {resp}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
