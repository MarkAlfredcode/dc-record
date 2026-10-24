#!/usr/bin/env python3
"""Suggest FOIA requests from today's assembled edition (added 2026-10-12).

One no-tools `claude -p` turn reads the edition's items plus the federal FOIA
offices that accept email (data/foia_contacts.json, built in the
markalfredsite repo's foia/pipeline/build_contacts.py) and returns up to
FOIA_MAX narrow requests. This script wraps each in the standard letter and
posts them to the private tracker (markalfred.news/foia, "Suggested" tab),
where Mark edits and sends them. Nothing is emailed from here.

Never fails the edition: every error is printed and the script exits 0.

Usage: suggest_foias.py <edition> <content json>
Env: CLAUDE_CODE_OAUTH_TOKEN, FOIA_INGEST_TOKEN (required to post),
     FOIA_INGEST_URL, FOIA_MODEL (default sonnet), FOIA_MAX (default 3),
     FOIA_AFFILIATION, FOIA_DRY_RUN=1 (print instead of posting).
"""
import datetime as dt
import html
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INGEST_URL = os.environ.get("FOIA_INGEST_URL", "https://markalfred.news/api/foia-suggestions")
MAX = int(os.environ.get("FOIA_MAX", "3"))
AFFILIATION = os.environ.get("FOIA_AFFILIATION", "The Washington Sun")
SYSTEM = ("You suggest Freedom of Information Act requests for a reporter. Follow the instructions exactly. "
          "Reply with the JSON object only: no prose before or after it, no code fences.")
_TAG = re.compile(r"<[^>]+>")
_FEC = re.compile(r"^https?://([a-z0-9-]+\.)*fec\.gov(/|$)", re.I)
_HREF = re.compile(r"""href=["'](https?://[^"']+)["']""")


def strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, list):
        for v in obj:
            yield from strings(v)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if k in ("no_news",):
                continue
            yield from strings(v)


def links(obj):
    out = []
    for s in strings(obj):
        out += _HREF.findall(s)
        if s.startswith("http"):
            out.append(s)
    seen, uniq = set(), []
    for u in out:
        if u not in seen:
            seen.add(u)
            uniq.append(html.unescape(u))
    return uniq


def items_from(content):
    parts = []
    if content.get("top_story"):
        parts.append(content["top_story"])
    parts += [s for s in content.get("sections") or [] if not s.get("no_news")]
    items = []
    for p in parts:
        text = " ".join(_TAG.sub("", html.unescape(s)) for s in strings(p) if not s.startswith("http"))
        text = re.sub(r"\s+", " ", text).strip()
        urls = links(p)[:15]
        if text and urls:
            items.append({"title": _TAG.sub("", html.unescape(p.get("headline") or p.get("eyebrow") or "")),
                          "section": p.get("eyebrow") or "", "text": text[:2200], "links": urls})
    return items


def run_model(prompt):
    env = {**os.environ, "DISABLE_PROMPT_CACHING": "1"}
    exe = shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")
    cmd = [exe, "-p", "--tools", "", "--system-prompt", SYSTEM, "--output-format", "json",
           "--effort", os.environ.get("FOIA_EFFORT", "low"), "--model", os.environ.get("FOIA_MODEL", "sonnet")]
    proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=900, env=env)
    envl = json.loads(proc.stdout)
    print(f"foia pass cost_usd={envl.get('total_cost_usd')} duration_ms={envl.get('duration_ms')}", file=sys.stderr)
    if envl.get("is_error"):
        raise RuntimeError(str(envl.get("result"))[:300])
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", (envl.get("result") or "").strip())
    return json.loads(text[text.find("{"): text.rfind("}") + 1])


def letter(contact, s, today):
    office = contact["agency"] + (f", {contact['parent']}" if contact.get("parent") else "")
    return f"""{today:%B} {today.day}, {today.year}

FOIA Officer
{office}
{contact['email']}

Re: Freedom of Information Act request: {s['subject']}

My name is Mark Alfred. I'm a reporter with {AFFILIATION}.

Pursuant to the Freedom of Information Act, 5 U.S.C. § 552, I request the following records:

{s['records'].strip()}

Expedited processing requested
I am employed as a full-time reporter. As a member of the news media engaged primarily in the dissemination of information, I request expedited processing on the grounds that these records are urgently needed to inform the public about {s['topic'].strip().rstrip('.')}. Courts consider three factors in determining whether there is an "urgency" to inform: (1) the request concerns a matter of current exigency to the American public; (2) the consequences of delaying a response would compromise a significant recognized interest; and (3) the request concerns federal government activity. Al-Fayed v. C.I.A., 254 F.3d 300, 310 (D.C. Cir. 2001). I certify that the statements in this expedited processing request are true and correct to the best of my knowledge and belief.

Fees
As a representative of the news media, I ask to be placed in the news media fee category and request a waiver of all fees, because disclosure is in the public interest and likely to contribute significantly to public understanding of government operations. If fees are not waived, please contact me before incurring any above $50.

Exclusion of media clippings
The agency may exclude any record that consists solely of media clips or clippings from a media monitoring service to speed processing. Records of government officials commenting on such clippings, and interactions between officials and members of the news media, should not be excluded.

I am not seeking documents detailing internal deliberations; I am only seeking the records themselves.

Preservation hold requested
Please institute a preservation hold on any records responsive to this request. I intend to pursue all legal avenues for my right of access to these records.

Release of segregable portions
FOIA permits withholding only where the agency reasonably foresees that disclosure would harm an interest protected by an exemption, or where disclosure is prohibited by law. If any part of this request is denied, please cite the specific exemption for each withholding and release all reasonably segregable non-exempt portions. I reserve the right to appeal.

Please send records electronically to foia@markalfred.news. I look forward to your acknowledgment and tracking number.

Sincerely,
Mark Alfred
{AFFILIATION}
foia@markalfred.news
"""


def main():
    edition, content_path = sys.argv[1:3]
    if not os.environ.get("FOIA_INGEST_TOKEN") and not os.environ.get("FOIA_DRY_RUN"):
        print("foia: FOIA_INGEST_TOKEN not set; skipping (no model call)", file=sys.stderr)
        return
    today = dt.datetime.now(dt.timezone(dt.timedelta(hours=-7))).date()
    if edition == "campaigns":
        print("foia: no suggestions from the campaigns edition (FEC filings; Mark's rule 2026-10-14)", file=sys.stderr)
        return
    with open(content_path) as f:
        content = json.load(f)
    # No FOIAs built on FEC filings, from any edition.
    items = [it for it in items_from(content) if not any(_FEC.search(u) for u in it["links"])]
    if not items:
        print("foia: no items with links; nothing to suggest", file=sys.stderr)
        return
    with open(os.path.join(ROOT, "data", "foia_contacts.json")) as f:
        contacts = {c["id"]: c for c in json.load(f)}
    with open(os.path.join(ROOT, "prompt-foia.txt")) as f:
        prompt = f.read().replace("MAX_SUGGESTIONS", str(MAX))

    lines = [f"\n=== ITEMS ({edition} edition, {today}) ==="]
    for i, it in enumerate(items, 1):
        lines.append(f"\n[{i}] {it['section']}: {it['title']}\n{it['text']}")
        lines += [f"  [L{j}] {u}" for j, u in enumerate(it["links"], 1)]
    lines.append("\n=== FEDERAL FOIA OFFICES THAT ACCEPT EMAIL (id | office | parent) ===")
    lines += [f"{c['id']} | {c['agency']} | {c.get('parent') or ''}" for c in contacts.values()]
    reply = run_model(prompt + "\n".join(lines))

    out = []
    for s in (reply.get("suggestions") or [])[:MAX]:
        try:
            item = items[int(s["item"]) - 1]
            contact = contacts[int(s["contact_id"])]
            m = re.match(r"L?(\d+)", str(s.get("source_link") or "L1"))
            url = item["links"][int(m.group(1)) - 1] if m and int(m.group(1)) <= len(item["links"]) else item["links"][0]
            for k in ("subject", "records", "topic", "why"):
                if not str(s.get(k) or "").strip():
                    raise ValueError(f"missing {k}")
        except (KeyError, ValueError, IndexError, TypeError) as e:
            print(f"foia: dropped a malformed suggestion ({e}): {str(s)[:200]}", file=sys.stderr)
            continue
        out.append({
            "edition": edition, "run_date": today.isoformat(), "item_title": item["title"], "source_url": url,
            "agency": contact["agency"] + (f" ({contact['parent']})" if contact.get("parent") else ""),
            "email": contact["email"], "level": contact["level"], "statute": contact["statute"],
            "rule_days": contact["rule"]["days"], "rule_kind": contact["rule"]["kind"], "rule_text": contact["rule"]["text"],
            "unverified": bool(contact.get("unverified")), "subject": s["subject"].strip(), "why": s["why"].strip(),
            "records": s["records"].strip(), "letter": letter(contact, s, today)})
    print(f"foia: {len(out)} suggestion(s) from {len(items)} items", file=sys.stderr)
    if not out:
        return
    if os.environ.get("FOIA_DRY_RUN"):
        print(json.dumps(out, indent=1))
        return
    req = urllib.request.Request(INGEST_URL, data=json.dumps({"suggestions": out}).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": "dc-record-foia/1",
                                          "Authorization": "Bearer " + os.environ["FOIA_INGEST_TOKEN"]})
    with urllib.request.urlopen(req, timeout=30) as r:
        print(f"foia: posted, {r.status} {r.read().decode()[:200]}", file=sys.stderr)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001 -- suggestions must never break an edition
        print(f"foia: skipped ({type(e).__name__}: {e})", file=sys.stderr)
    sys.exit(0)
