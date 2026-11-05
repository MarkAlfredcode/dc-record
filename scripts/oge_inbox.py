#!/usr/bin/env python3
"""Read oge@markalfred.news deliveries and put what they contain at the top of the Officials edition (added 2026-11-05).

OGE emails the ethics documents requested by scripts/oge_requests.py (Form 201) to oge@markalfred.news. The foia-inbox
Email Worker forwards each mail to Yahoo and logs it, with the text of its attachments, in D1 `oge_inbox`. This script,
a step in the Officials workflow:

  1. pulls unsummarized mail from the site's /api/oge-queue (bearer FOIA_INGEST_TOKEN),
  2. reads every attachment again from its stored original with sam_reader.read_file (OCR for scans, and
     doc_metadata's AI-fingerprint scan of PDF link annotations / properties, which the Worker can't do) and downloads
     any oge.gov links in the body,
  3. has Claude (Gemini as backup) summarize each mail with prompt-oge-inbox.txt,
  4. posts the summary back, and
  5. writes extras/officials/oge_received.json: ONE section flagged `priority`, so assemble.py puts it first. With nothing
     to show it writes `sections: []` and the edition has no such section at all.

Each delivery is shown once per AM/PM slot (manifest data/oge-inbox-shown.json {mail id: [modes shown in]}); the workflow
reverts that manifest on site-refresh-only runs like the other seen-manifests.

Env: FOIA_INGEST_TOKEN (required; skips without it), CLAUDE_CODE_OAUTH_TOKEN, GEMINI_API_KEY (backup), FOIA_SITE.
Never fails the job: errors are printed and the script exits 0.
"""
import base64
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dc_extras  # noqa: E402
import sam_reader  # noqa: E402
from dc_common import load_manifest, save_manifest  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SITE = os.environ.get("FOIA_SITE", "https://markalfred.news")
ET = ZoneInfo("America/New_York")
SHOWN = "data/oge-inbox-shown.json"
LEDGER = "data/oge-requests-seen.json"
COLLECTION = "https://www.oge.gov/web/oge.nsf/Officials%20Individual%20Disclosures%20Search%20Collection"
SYSTEM = ("You summarize ethics documents for a reporter. Follow the instructions exactly. "
          "Reply with the JSON object only: no prose before or after it, no code fences.")
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
MAX_CHARS_PER_FILE = 45000
MAX_CHARS_PER_MAIL = 110000


def api(path, payload=None):
    req = urllib.request.Request(SITE + path, method="POST" if payload is not None else "GET",
                                 data=json.dumps(payload).encode() if payload is not None else None,
                                 headers={"Authorization": "Bearer " + os.environ["FOIA_INGEST_TOKEN"],
                                          "Content-Type": "application/json", "User-Agent": "dc-record-oge/1"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


def ai_flags(info):
    """doc_metadata findings that are AI-chatbot fingerprints (the others -- tracking params, paths -- are not what Mark asked for)."""
    return [f for f in (info.get("md_flags") or []) if str(f.get("finding", "")).startswith("AI") or "chatbot" in str(f.get("finding", "")).lower()]


def read_mail(m, work):
    """-> (text for the model, ai findings [{where, finding, quote}], notes)."""
    parts, ai, notes = [], [], []
    for n, f in enumerate(m.get("files") or []):
        name = f.get("filename") or f"file{n}"
        text, flags = f.get("text") or "", []
        if f.get("data"):
            path = os.path.join(work, f"{m['id']}-{n}-{re.sub(r'[^A-Za-z0-9._-]', '_', name)[-80:]}")
            with open(path, "wb") as fh:
                fh.write(base64.b64decode(f["data"]))
            t2, method, info = sam_reader.read_file(path, name)
            if len(t2.strip()) > len(text.strip()):
                text = t2
            flags = ai_flags(info)
            if method in ("ocr+pdf", "ocr-image"):
                notes.append(f"{name}: read by OCR")
        elif f.get("status") != "read":
            notes.append(f"{name}: {f.get('status')} ({f.get('note') or 'no text'})")
        for fl in flags:
            ai.append({"where": name, "finding": fl.get("finding"), "quote": fl.get("quote")})
        parts.append(f"--- ATTACHMENT {name} ({f.get('size')} bytes, {f.get('status')}) ---\n{text[:MAX_CHARS_PER_FILE]}")
    # Download links from OGE itself (a delivery may be a link instead of an attachment).
    try:
        links = json.loads(m.get("links") or "[]")
    except ValueError:
        links = []
    got = 0
    for u in links:
        if got >= 4 or not re.match(r"https?://([^/]*\.)?oge\.gov/", u, re.I):
            continue
        if not re.search(r"\.(pdf|docx?|zip)(\?|$)|\$file|/download|OpenElement|FieldElemFormat", u, re.I):
            continue
        try:
            rq = urllib.request.Request(u, headers={"User-Agent": UA})
            with urllib.request.urlopen(rq, timeout=60) as r:
                data = r.read(25_000_000)
            path = os.path.join(work, f"{m['id']}-link{got}")
            with open(path, "wb") as fh:
                fh.write(data)
            text, method, info = sam_reader.read_file(path, u.split("?")[0].rsplit("/", 1)[-1])
            if text.strip():
                parts.append(f"--- DOWNLOADED FROM {u} ({len(data)} bytes) ---\n{text[:MAX_CHARS_PER_FILE]}")
                got += 1
                for fl in ai_flags(info):
                    ai.append({"where": u, "finding": fl.get("finding"), "quote": fl.get("quote")})
        except Exception as e:  # noqa: BLE001
            notes.append(f"link {u[:100]}: {type(e).__name__}")
    for am in json.loads(m.get("ai_markers") or "[]"):  # the Worker's text scan of body + attachment text
        ai.append({"where": "email text", "finding": f"{am['kind']}: {am['marker']}", "quote": am.get("excerpt")})
    body = (f"[mail id {m['id']}] received {m['received_at']}\nFrom: {m.get('from_name') or ''} <{m.get('from_addr')}>\n"
            f"Subject: {m.get('subject')}\nLinks in the mail: {json.dumps(json.loads(m.get('links') or '[]')[:12])}\n"
            f"Attachments: {m.get('attachments') or 'none'}\nBody:\n{(m.get('text') or '')[:6000]}\n" + "\n".join(parts))
    return body[:MAX_CHARS_PER_MAIL], ai, notes


def ask_model(prompt):
    exe = shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or os.path.exists(exe):
        try:
            proc = subprocess.run([exe, "-p", "--tools", "", "--system-prompt", SYSTEM, "--output-format", "json",
                                   "--effort", "medium", "--model", os.environ.get("OGE_MODEL", "sonnet")],
                                  input=prompt, capture_output=True, text=True, timeout=900,
                                  env={**os.environ, "DISABLE_PROMPT_CACHING": "1"})
            envl = json.loads(proc.stdout)
            if envl.get("is_error"):
                raise RuntimeError(str(envl.get("result"))[:300])
            print(f"oge-inbox cost_usd={envl.get('total_cost_usd')}", file=sys.stderr)
            return envl.get("result") or ""
        except Exception as e:  # noqa: BLE001
            print(f"oge-inbox: Claude failed ({e}); trying Gemini", file=sys.stderr)
    import gemini
    text, _, model = gemini.generate(prompt, SYSTEM, gemini.MAIN_MODELS, budget=600)
    print(f"oge-inbox: summarized with {model}", file=sys.stderr)
    return text


def parse_json(text):
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    return json.loads(text[text.find("{"): text.rfind("}") + 1])


def et_day(iso):
    d = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(ET)


def esc(s):
    return html.escape(str(s or ""), quote=False)


def item_html(entry, mail):
    """One list item for a delivered document, or a note for a problem."""
    got = et_day(mail["received_at"]).strftime("%b %-d")
    out = []
    for d in entry.get("documents") or []:
        who = f"<strong>{esc(d.get('official'))}</strong>" + (f" ({esc(d.get('position'))})" if d.get("position") else "")
        bits = [f"{who}: {esc(d.get('doc_type'))}" + (f", dated {esc(d.get('doc_date'))}" if d.get("doc_date") else "") + f". Arrived {got}."]
        if d.get("summary"):
            bits.append(esc(d["summary"]))
        if d.get("late"):
            bits.append(f"<strong>Late disclosure:</strong> {esc(d['late'])}")
        notable = [esc(n) for n in (d.get("notable") or [])[:4] if n]
        if notable:
            bits.append("Worth a look: " + "; ".join(notable) + ".")
        url = d.get("source_url") or COLLECTION
        bits.append(f'<a href="{html.escape(url, quote=True)}">{"Document" if d.get("source_url") else "OGE collection"}</a>.')
        out.append(" ".join(bits))
    if entry.get("kind") == "problem":
        out.append(f"<strong>OGE could not deliver:</strong> {esc(entry.get('note') or entry.get('headline'))} Arrived {got}. "
                   f'<a href="{COLLECTION}">OGE collection</a>.')
    return out


def build_section(mails, mode):
    """mails: [(mail row, summary dict, ai findings)] not yet shown in this slot."""
    items, flags = [], []
    for row, s, ai in mails:
        items.extend(item_html(s, row))
        for a in ai:
            who = next((d.get("official") for d in s.get("documents") or [] if d.get("official")), None) or row.get("subject")
            flags.append(f"<strong>AI citation marker</strong> in the document sent for {esc(who)}: "
                         f"{esc(a.get('finding'))}" + (f" (&ldquo;{esc(str(a.get('quote'))[:160])}&rdquo;)" if a.get("quote") else "") +
                         f" [{esc(a.get('where'))}]. This reports what the file shows, nothing about who prepared it.")
    if not items and not flags:
        return None
    n = len(items)
    return {
        "eyebrow": "OGE Documents You Requested",
        "headline": (f"{n} ethics document{'s' if n != 1 else ''} you requested {'have' if n != 1 else 'has'} arrived from OGE" if n
                     else "OGE sent mail with AI citation markers"),
        "priority": True,
        "above_top": True,
        "paragraphs": ["Delivered to oge@markalfred.news in answer to automatically filed OGE Form 201 requests. Summaries are of the documents themselves."],
        "blocks": [{"type": "list", "items": items + flags}],
    }


def main():
    if not os.environ.get("FOIA_INGEST_TOKEN"):
        print("oge-inbox: FOIA_INGEST_TOKEN not set; skipping", file=sys.stderr)
        return
    mode = dc_extras.run_mode()
    try:
        q = api("/api/oge-queue")
    except Exception as e:  # noqa: BLE001
        print(f"oge-inbox: queue unreachable ({e}); leaving the section as it was", file=sys.stderr)
        return

    pending = q.get("pending") or []
    ai_by_id = {}
    if pending:
        work = tempfile.mkdtemp(prefix="oge-inbox-")
        texts = {}
        for m in pending:
            try:
                texts[m["id"]], ai_by_id[m["id"]], notes = read_mail(m, work)
                if notes:
                    texts[m["id"]] += "\nREADER NOTES: " + "; ".join(notes)
            except Exception as e:  # noqa: BLE001
                print(f"oge-inbox: mail {m['id']} unreadable ({e})", file=sys.stderr)
        if texts:
            try:
                prompt = open(os.path.join(ROOT, "prompt-oge-inbox.txt")).read() + "\n\n".join(texts.values())
                reply = parse_json(ask_model(prompt))
                done = set()
                for e in reply.get("mails") or []:
                    mid = int(e["id"])
                    if mid not in texts:
                        continue
                    status = "noise" if e.get("kind") == "receipt" else "summarized"
                    e["ai"] = ai_by_id.get(mid, [])
                    api("/api/oge-queue", {"id": mid, "summary": e, "status": status})
                    done.add(mid)
                print(f"oge-inbox: summarized {len(done)} of {len(pending)} pending mail(s)", file=sys.stderr)
            except Exception as e:  # noqa: BLE001
                print(f"oge-inbox: summary pass failed ({type(e).__name__}: {e}); will retry next run", file=sys.stderr)
        shutil.rmtree(work, ignore_errors=True)

    # The section: summarized deliveries from the last 14 days not yet shown in this AM/PM slot.
    try:
        recent = api("/api/oge-queue").get("recent") or []
    except Exception:  # noqa: BLE001
        recent = q.get("recent") or []
    shown = load_manifest(SHOWN)
    todo = []
    for row in sorted(recent, key=lambda r: r["received_at"]):
        try:
            s = json.loads(row["summary"])
        except (TypeError, ValueError):
            continue
        if mode in shown.get(str(row["id"]), []) or s.get("kind") == "receipt":
            continue
        todo.append((row, s, s.get("ai") or []))
    section = build_section(todo, mode)
    summary = (f"{len(todo)} OGE delivery mail(s) summarized for the top of the edition." if section
               else "Nothing new from oge@markalfred.news.")
    dc_extras.write_extra("officials", "oge_received", [section] if section else [], summary)
    if section:
        for row, _, _ in todo:
            shown.setdefault(str(row["id"]), []).append(mode)
        keep = {k: v for k, v in shown.items() if int(k) >= min(r["id"] for r, _, _ in todo) - 400}
        save_manifest(SHOWN, keep)
    # Requests filed more than 4 days ago with no mail at all since: say so in the log (Mark asked to be told of problems).
    try:
        led = load_manifest(LEDGER)
        mail_text = " ".join((m.get("subject") or "") + " " + (m.get("text") or "") for m in q.get("mail_since") or []).lower()
        for f in led.get("filed", []):
            age = (datetime.now(ET) - datetime.fromisoformat(f["at"])).days
            last = f["name"].partition(",")[0].lower()
            if 4 <= age <= 21 and last not in mail_text:
                print(f"oge-inbox: NO MAIL YET for {f['name']} ({', '.join(f['docs'])}) filed {f['at'][:10]}", file=sys.stderr)
    except Exception:  # noqa: BLE001
        pass


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        print(f"oge-inbox: failed ({type(e).__name__}: {e})", file=sys.stderr)
