"""Shared helpers for DC Record's fetch/filter/render scripts.

These scripts run BEFORE `claude -p` in the GitHub Actions workflow and do
all the fully-mechanical fetch/paginate/filter/dedupe/flag work that used to
happen inside the model's own reasoning -- see
/Users/mark/.claude/plans/imperative-brewing-lerdorf.md for the design
rationale. Claude's job narrows to reading each script's pre-digested
candidates JSON and writing the actual prose/judgment calls.
"""
import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

LA_TZ = ZoneInfo("America/Los_Angeles")

# Force IPv4 for all urllib requests in this process. Confirmed by hand: one
# of api.sam.gov's several IPv6 addresses was unreachable from this network
# (a real SYN_SENT hang that outlasted urllib's own `timeout=` -- Python's
# socket timeout covers the connect() call on ONE address, but urllib/
# getaddrinfo can still pick a routable-looking-but-actually-dead IPv6
# address and stall well past that timeout working through it). Since every
# API this project touches works fine over IPv4, there's no reason to risk
# this class of hang in any environment (local, GitHub Actions, or
# otherwise) -- monkeypatching getaddrinfo to filter to AF_INET is the
# standard fix and is effectively free here.
_orig_getaddrinfo = socket.getaddrinfo


def _ipv4_only_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    return _orig_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)


socket.getaddrinfo = _ipv4_only_getaddrinfo

# Absolute failsafe: no socket in this process should ever be able to hang
# past 45s, regardless of what timeout (if any) an individual call site
# passes. Per-call `timeout=` params below still apply where shorter.
socket.setdefaulttimeout(45)


def today_la():
    """TODAY as YYYY-MM-DD in US-Pacific, matching the prompts' own
    `TZ=America/Los_Angeles date +%Y-%m-%d` convention (GitHub Actions
    runners default to UTC)."""
    return datetime.now(LA_TZ).strftime("%Y-%m-%d")


def yesterday_la():
    return (datetime.now(LA_TZ) - timedelta(days=1)).strftime("%Y-%m-%d")


def days_ago_la(n):
    return (datetime.now(LA_TZ) - timedelta(days=n)).strftime("%Y-%m-%d")


def http_get(url, headers=None, timeout=30, max_time_retries=2):
    """GET with a couple retries on transient failures. Returns (status,
    bytes) -- never raises for a real HTTP error response (so callers can
    inspect 4xx/5xx), only for genuine connection failures after retries."""
    req = urllib.request.Request(url, headers=headers or {})
    last_err = None
    for attempt in range(max_time_retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
        except Exception as e:  # noqa: BLE001 - genuine connect/timeout failure
            last_err = e
            if attempt < max_time_retries:
                time.sleep(2)
    raise ConnectionError(f"GET {url} failed after retries: {last_err}")


def http_get_json(url, headers=None, timeout=30):
    status, body = http_get(url, headers=headers, timeout=timeout)
    if status >= 400:
        raise RuntimeError(f"GET {url} -> HTTP {status}: {body[:300]!r}")
    return json.loads(body)


def http_post_json(url, payload, headers=None, timeout=30, max_time_retries=2):
    """POST with a couple retries on transient failures -- mirrors http_get's
    retry behavior, which this lacked (confirmed live 2026-09-16: a single
    unretried 30s timeout on USAspending's search endpoint silently zeroed
    the whole Executive Branch USAspending section for the day, since the
    caller's own try/except treated ANY exception, including a plain
    timeout, as "stop paginating, report 0 results" rather than distinguishing
    a real transient failure worth retrying)."""
    body = json.dumps(payload).encode()
    hdrs = {"Content-Type": "application/json"}
    hdrs.update(headers or {})
    req = urllib.request.Request(url, data=body, headers=hdrs, method="POST")
    last_err = None
    for attempt in range(max_time_retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")
        except Exception as e:  # noqa: BLE001 - genuine connect/timeout failure
            last_err = e
            if attempt < max_time_retries:
                time.sleep(2)
    raise ConnectionError(f"POST {url} failed after retries: {last_err}")


def check_reachable(host, timeout=10):
    """Mirrors the prompts' existing connectivity-check semantics: a 404/301/
    403 from the site itself means the host IS reachable; only a genuine
    connect/timeout failure counts as down."""
    try:
        req = urllib.request.Request(f"https://{host}/", method="GET")
        urllib.request.urlopen(req, timeout=timeout)
        return True
    except urllib.error.HTTPError:
        return True  # any HTTP response = reachable
    except Exception:
        return False


def load_manifest(path):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {}


def save_manifest(path, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")


def write_candidates(path, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    print(f"wrote {path}", file=sys.stderr)


_LI_WRAP_RE = re.compile(r"^\s*<li(?:\s[^>]*)?>(.*)</li>\s*$", re.IGNORECASE | re.DOTALL)


def strip_li_wrapper(item):
    """Defensively strips a redundant outer <li>...</li> the model sometimes
    writes into a `list` block's item string despite the prompts asking for
    bare inner HTML -- every render_*.py already wraps each item in its own
    <li>, so a model-supplied wrapper produces nested <li><li>...</li></li>,
    which browsers silently mis-render as doubled bullets (confirmed live in
    the 2026-09-16 Executive Branch edition's OGE/SAM/Federal Register
    sections). A genuinely bare item just doesn't match and passes through
    unchanged."""
    m = _LI_WRAP_RE.match(item)
    return m.group(1).strip() if m else item


_BOLD_TAG_RE = re.compile(r"</?(?:strong|b)(?:\s[^>]*)?>", re.IGNORECASE)


def strip_bold_tags(text):
    """Removes <strong>/<b> tags the model sometimes writes into a `subhead`
    block's text. Every render_*.py HTML-escapes subhead text and already
    renders it bold, so a model-supplied <strong> showed up as literal
    "&lt;strong&gt;" text (confirmed live in the 2026-10-01 Officials PM
    edition's PTR member subheads)."""
    return _BOLD_TAG_RE.sub("", text)


def pdftotext(pdf_path):
    """Run pdftotext -layout, return the extracted text (empty string on
    failure -- callers should treat that as 'text not available', matching
    the prompts' existing 'say plainly you couldn't read it' rule)."""
    import subprocess

    txt_path = pdf_path + ".txt"
    try:
        subprocess.run(
            ["pdftotext", "-layout", pdf_path, txt_path],
            check=True, timeout=60, capture_output=True,
        )
        with open(txt_path, errors="replace") as f:
            return f.read()
    except Exception as e:  # noqa: BLE001
        print(f"pdftotext failed for {pdf_path}: {e}", file=sys.stderr)
        return ""


def download(url, dest_path, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": "dc-record-bot"})
    with urllib.request.urlopen(req, timeout=timeout) as resp, open(dest_path, "wb") as out:
        out.write(resp.read())
    return dest_path
