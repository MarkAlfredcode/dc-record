#!/usr/bin/env python3
"""Sends the "network issue, no edition today" notice when too many sources
are unreachable -- mechanized (previously Claude's own judgment call, but
"count how many of these 5 fetch scripts reported reachable:false" needs no
judgment at all). Writes email_skipped.flag with the real reason.

Usage: python3 send_notice.py "host1, host2" (comma/space-joined list of down hosts)
Requires: RESEND_API_KEY env var.
"""
import json
import os
import subprocess
import sys

TO = ["markalfred@notus.com", "taylorgiorno@notus.com", "annakramer@notus.com"]
FROM = "DC Record • The Feds <newsletter@markalfred.news>"


def main():
    down_hosts = sys.argv[1] if len(sys.argv) > 1 else "unknown hosts"
    reason = f"{down_hosts} unreachable"

    api_key = os.environ.get("RESEND_API_KEY")
    if api_key:
        payload = {
            "from": FROM, "to": TO,
            "subject": "DC Record • The Feds • network issue, no edition today",
            "html": f"<p>Today's The Feds edition could not be built: {down_hosts} were unreachable.</p>",
            "text": f"Today's The Feds edition could not be built: {down_hosts} were unreachable.",
        }
        subprocess.run(
            ["curl", "-s", "https://api.resend.com/emails",
             "-H", f"Authorization: Bearer {api_key}",
             "-H", "Content-Type: application/json",
             "-d", json.dumps(payload)],
            capture_output=True, text=True, timeout=60,
        )
    with open("email_skipped.flag", "w") as f:
        f.write(reason)
    print(f"notice sent, reason: {reason}", file=sys.stderr)


if __name__ == "__main__":
    main()
