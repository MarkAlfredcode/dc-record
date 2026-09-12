#!/usr/bin/env python3
"""Sends the rendered newsletter via Resend and writes the STATUS FLAG file
based on the REAL HTTP response -- not Claude self-reporting after reading
the response in its own context. This removes an entire self-reporting
hallucination surface (and the RESEND_API_KEY/Resend-handling instructions
entirely from Claude's prompt/token budget).

Usage: python3 send_email.py newsletter.html "DC Record • Executive Branch • <date>" [--to email1,email2,...]
Requires: RESEND_API_KEY env var.
Resend sits behind a Cloudflare bot check that blocks the default
urllib/requests user agent (403 / error 1010) but accepts plain curl -- so
this shells out to curl, same as the prompt used to instruct Claude to do.
"""
import json
import os
import subprocess
import sys

DEFAULT_TO = ["markalfred@notus.com", "taylorgiorno@notus.com", "annakramer@notus.com"]
FROM = "DC Record • Executive Branch <newsletter@markalfred.news>"


def make_text_summary(html_path):
    # A short plain-text fallback; doesn't need to be fancy, Resend just wants something.
    return "This edition is best viewed in an HTML-capable mail client. See the full newsletter above."


def main():
    if len(sys.argv) < 3:
        print("usage: send_email.py newsletter.html subject [--to email1,email2]", file=sys.stderr)
        sys.exit(1)
    html_path, subject = sys.argv[1], sys.argv[2]
    to = DEFAULT_TO
    if "--to" in sys.argv:
        to = sys.argv[sys.argv.index("--to") + 1].split(",")

    api_key = os.environ.get("RESEND_API_KEY")
    if not api_key:
        with open("email_failed.flag", "w") as f:
            f.write("RESEND_API_KEY not set")
        sys.exit(1)

    with open(html_path) as f:
        html_content = f.read()

    payload = {
        "from": FROM, "to": to, "subject": subject,
        "html": html_content, "text": make_text_summary(html_path),
    }
    proc = subprocess.run(
        ["curl", "-s", "https://api.resend.com/emails",
         "-H", f"Authorization: Bearer {api_key}",
         "-H", "Content-Type: application/json",
         "-d", json.dumps(payload)],
        capture_output=True, text=True, timeout=60,
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
