#!/usr/bin/env python3
"""Renders content-TODAY.json (from build_conflicts.py) into the web-only
"DC Record • Conflicts" page (added 2026-10-04): deep teal / amber accent,
modern CSS, no email constraints.

Usage: python3 render_conflicts.py content-TODAY.json newsletter-TODAY.html
"""
import html
import json
import sys

from dc_common import strip_bold_tags, strip_li_wrapper

MODE_LABEL = {"am": "Morning run", "pm": "Afternoon run"}

HEAD = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>The DC Record • Conflicts</title>
<meta name="color-scheme" content="light only">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Newsreader:wght@700&display=swap">
<style>
  :root { color-scheme: light only; --teal: #0f5c63; --amber: #b7791f; --ink: #161616; --muted: #6b6b64; --line: #dcd5c7; --wash: #faf8f5; }
  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; background: #f7f5f2; color: #262626; }
  body { font-family: Arial, Helvetica, sans-serif; font-size: 16px; line-height: 1.5; }
  .wrap { max-width: 600px; margin: 0 auto; background: #ffffff; padding: 0 20px 30px; }
  @media (min-width: 601px) { .wrap { max-width: 860px; padding: 0 32px 40px; } }
  .rule-top { height: 4px; background: var(--teal); margin: 0 -20px; }
  .rule-bottom { height: 3px; background: var(--amber); margin: 24px -20px 0; }
  @media (min-width: 601px) { .rule-top, .rule-bottom { margin-left: -32px; margin-right: -32px; } }
  .masthead { text-align: center; padding: 20px 0 18px; }
  .date { font-size: 13px; letter-spacing: 1px; text-transform: uppercase; color: var(--muted); margin-bottom: 6px; }
  .nameplate { font-family: 'Newsreader', Georgia, 'Times New Roman', serif; font-size: 34px; font-weight: bold; color: var(--ink); letter-spacing: 0.5px; margin: 0; }
  .nameplate .dc { color: var(--teal); }
  .tagline { font-family: Georgia, 'Times New Roman', serif; font-style: italic; font-size: 15px; color: #3c4858; margin-top: 4px; }
  .eyebrow { font-size: 12px; font-weight: bold; letter-spacing: 1.5px; color: var(--teal); text-transform: uppercase; padding-top: 22px; padding-bottom: 8px; }
  h2.headline { font-family: 'Newsreader', Georgia, 'Times New Roman', serif; font-size: 21px; line-height: 27px; color: var(--ink); margin: 0 0 10px 0; }
  h3.sub { font-size: 13px; text-transform: uppercase; letter-spacing: 0.5px; color: var(--teal); margin: 18px 0 6px 0; }
  p { margin: 0 0 12px 0; }
  ul { margin: 0 0 14px 0; padding-left: 20px; }
  li { margin-bottom: 10px; }
  a { color: var(--teal); }
  table.data { width: 100%; border-collapse: collapse; font-size: 14px; margin-bottom: 14px; }
  table.data th { text-align: left; border-bottom: 2px solid var(--teal); padding: 6px 8px; }
  table.data td { padding: 7px 8px; border-bottom: 1px solid var(--line); vertical-align: top; }
  @media (max-width: 600px) { .tablewrap { overflow-x: auto; } table.data { min-width: 560px; } }
  .card { border-left: 3px solid var(--teal); background: var(--wash); padding: 10px 14px; margin-bottom: 12px; }
  .card .meta { font-size: 12px; text-transform: uppercase; color: #3c4858; }
  .note { font-size: 13px; color: var(--muted); }
  .stats { display: flex; flex-wrap: wrap; gap: 8px; margin: 4px 0 6px; }
  .stat { flex: 1 1 120px; border: 1px solid var(--line); background: var(--wash); padding: 8px 10px; }
  .stat b { display: block; font-family: 'Newsreader', Georgia, serif; font-size: 26px; line-height: 1.1; color: var(--teal); }
  .stat span { font-size: 12px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.4px; }
  .stat.zero b { color: #b5b0a4; }
  .runlabel { display: inline-block; font-size: 11px; letter-spacing: 1px; text-transform: uppercase; background: var(--amber); color: #fff; padding: 1px 7px; margin-bottom: 6px; }
  .repeat { border: 1px solid var(--line); border-left: 4px solid var(--amber); padding: 10px 14px; margin-bottom: 12px; }
  .repeat .name { font-weight: bold; font-size: 16px; }
  .repeat .kinds { font-size: 13px; color: #3c4858; margin: 2px 0 6px; }
  .repeat ul { margin: 0; font-size: 14px; }
  .repeat li { margin-bottom: 4px; }
  .newtag { font-size: 11px; font-weight: bold; color: #fff; background: var(--teal); padding: 1px 6px; margin-left: 6px; letter-spacing: 0.5px; }
  details.day { border-top: 1px solid var(--line); padding: 8px 0; }
  details.day > summary { cursor: pointer; font-weight: bold; color: var(--teal); }
  .callout { border: 1px solid var(--teal); border-left: 4px solid var(--teal); padding: 12px 16px; margin: 16px 0; font-size: 15px; }
  .footer { margin-top: 28px; padding-top: 16px; border-top: 1px solid var(--line); font-size: 13px; color: var(--muted); }
  .footer p { margin: 0 0 8px 0; }
  .footer ul { font-size: 13px; }
  .signoff { font-family: Georgia, 'Times New Roman', serif; font-style: italic; margin-top: 10px; }
</style>
</head>
<body>
<div class="wrap">
  <div class="rule-top"></div>
  <div class="masthead">
    <div class="date">__DATE__</div>
    <div class="nameplate">THE <span class="dc">DC</span> RECORD</div>
    <div class="tagline">Conflict scanner: donors, contracts, trades and disclosures.</div>
  </div>
"""

FOOT = """</div>
</body>
</html>
"""

METHOD = [
    ("Donor Awards", "Federal contracts of $1M+ signed in the 30 days before a Cabinet-level head change, plus new $2M+ awards at those agencies, where "
                     "FEC data shows the vendor's owner-titled donors or the vendor itself gave to the agency head's committees or to Trump-world committees."),
    ("Trades Near Contract Awards", "Every individual-stock trade in newly filed House and Senate periodic transaction reports, matched by company name to "
                                    "$10M+ base contract awards on USAspending within 30 days either side."),
    ("Disclosure Red Flags", "Trades disclosed more than 45 days late, trades before market-moving events, trades in the filer's own committee's "
                             "jurisdiction, outside income and positions in new annual reports, and line-by-line diffs of amended reports."),
    ("Repeat names", "People and companies that show up in more than one scanner (or more than once in one) over the last 60 days."),
]


def esc(s):
    return html.escape(str(s if s is not None else ""), quote=False)


def render_blocks(blocks):
    out = []
    for b in blocks:
        t = b.get("type")
        if t == "paragraph":
            out.append(f'<p>{b["text"]}</p>')
        elif t == "note":
            out.append(f'<p class="note">{b["text"]}</p>')
        elif t == "list":
            out.append("<ul>" + "".join(f"<li>{strip_li_wrapper(i)}</li>" for i in b["items"]) + "</ul>")
        elif t == "subhead":
            out.append(f'<h3 class="sub">{esc(strip_bold_tags(b["text"]))}</h3>')
        elif t == "table":
            heads = "".join(f"<th>{esc(h)}</th>" for h in b["headers"])
            rows = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>" for row in b["rows"])
            out.append(f'<div class="tablewrap"><table class="data"><tr>{heads}</tr>{rows}</table></div>')
        elif t == "card":
            meta = " · ".join(esc(strip_bold_tags(m)) for m in b.get("meta") or [] if m)
            links = " | ".join(f'<a href="{html.escape(l["url"], quote=True)}">{esc(l["text"])}</a>' for l in b.get("links") or [] if l.get("url"))
            out.append('<div class="card">' + f'<div><strong>{b.get("title", "")}</strong></div>'
                       + (f'<div class="meta">{meta}</div>' if meta else "") + f'<div>{b.get("text", "")}</div>'
                       + (f'<div class="note">{links}</div>' if links else "") + "</div>")
    return "\n".join(out)


def render_section(sec, nested=False):
    """nested=True (the collapsed Earlier days) uses an <h3> instead of an
    .eyebrow so the site's table of contents lists each scanner once."""
    runs = sec["runs"]
    parts = [f'<h3 class="sub">{esc(sec["eyebrow"])}</h3>' if nested else f'<div class="eyebrow">{esc(sec["eyebrow"])}</div>']
    for r in runs:
        if len(runs) > 1:
            parts.append(f'<div class="runlabel">{MODE_LABEL.get(r["mode"], r["mode"])}</div>')
        parts.append(f'<h2 class="headline">{r["headline"]}</h2>')
        parts.extend(f"<p>{p}</p>" for p in r.get("paragraphs", []))
        parts.append(render_blocks(r.get("blocks", [])))
    return "\n".join(parts)


def render_stats(new_today):
    kinds = ["donor-linked contract award", "trade near a federal contract award", "late trade disclosure",
             "trade before a market-moving event", "trade in own committee's jurisdiction"]
    short = {"donor-linked contract award": "Donor awards", "trade near a federal contract award": "Trades near awards",
             "late trade disclosure": "Late disclosures", "trade before a market-moving event": "Event-timed trades",
             "trade in own committee's jurisdiction": "Own-jurisdiction trades"}
    cells = "".join(f'<div class="stat{" zero" if not new_today.get(k) else ""}"><b>{new_today.get(k, 0)}</b><span>{short[k]}</span></div>' for k in kinds)
    return f'<div class="eyebrow">New flags today</div><div class="stats">{cells}</div>'


def render_rollup(r):
    if not r["people"] and not r["companies"]:
        return (f'<div class="eyebrow">Repeat names</div><p class="note">No person or company has appeared in more than one flag in the last '
                f'{r["window_days"]} days yet ({r["tracked"]} flag{"s" if r["tracked"] != 1 else ""} tracked). This view fills in as the scanners accumulate history.</p>')

    def card(p):
        kinds = " · ".join(f"{n}× {k}" for k, n in p["kinds"].items())
        lis = "".join(f'<li>{esc(s["date"])}: {esc(s["detail"])}' + (f' (<a href="{html.escape(s["url"], quote=True)}">source</a>)' if s["url"] else "") + "</li>"
                      for s in p["signals"])
        more = f'<li class="note">+ {p["more"]} more</li>' if p["more"] else ""
        return (f'<div class="repeat"><div class="name">{esc(p["name"])}' + ('<span class="newtag">NEW</span>' if p["new_today"] else "")
                + f'</div><div class="kinds">{esc(kinds)}</div><ul>{lis}{more}</ul></div>')

    out = [f'<div class="eyebrow">Repeat names</div><h2 class="headline">People and companies flagged more than once in the last {r["window_days"]} days</h2>']
    if r["people"]:
        out.append('<h3 class="sub">Members and officials</h3>' + "".join(card(p) for p in r["people"]))
    if r["companies"]:
        out.append('<h3 class="sub">Companies</h3>' + "".join(card(p) for p in r["companies"]))
    return "\n".join(out)


def render_earlier(earlier):
    if not earlier:
        return ""
    out = ['<div class="eyebrow">Earlier flags</div>']
    for day in earlier:
        body = "\n".join(render_section(s, nested=True) for s in day["sections"])
        names = ", ".join(s["eyebrow"] for s in day["sections"])
        out.append(f'<details class="day"><summary>{esc(day["date"])} <span class="note">— {esc(names)}</span></summary>{body}</details>')
    return "\n".join(out)


def render_footer(content):
    status = "".join(f"<li><strong>{esc(k)}:</strong> {esc(v)}</li>" for k, v in content.get("scanner_status", {}).items())
    method = "".join(f"<li><strong>{esc(k)}.</strong> {esc(v)}</li>" for k, v in METHOD)
    return f"""  <div class="footer">
    <p><strong>This run</strong></p><ul>{status or "<li>No scanner output this run.</li>"}</ul>
    <p><strong>What the scanner checks</strong></p><ul>{method}</ul>
    <p>A flag is a lead, not a finding. Overlaps in timing, committee membership or donations do not show that anyone acted on non-public information or
    that a decision was influenced. Company matches are by name; subsidiaries and renamed parents are missed. House PTRs that are scanned images and
    paper Senate filings are not read. The DC Record cannot see trades in blind trusts or sales that were never disclosed.</p>
    <p><strong>Sources:</strong> House Clerk and Senate eFD financial disclosures, FEC, USAspending, congress.gov nominations, LDA.</p>
    <div class="signoff">— The DC Record</div>
  </div>
  <div class="rule-bottom"></div>
"""


def render(content):
    parts = [HEAD.replace("__DATE__", esc(content["date"])), render_stats(content["new_today"])]
    if content["today"]:
        parts.extend(render_section(s) for s in content["today"])
    else:
        parts.append('<div class="callout">No new conflict flags in today\'s runs. Standing findings are below.</div>')
    parts.append(render_rollup(content["rollup"]))
    parts.append(render_earlier(content["earlier"]))
    parts.append(render_footer(content))
    parts.append(FOOT)
    return "\n".join(parts)


def main():
    if len(sys.argv) != 3:
        print("usage: render_conflicts.py content.json output.html", file=sys.stderr)
        sys.exit(1)
    with open(sys.argv[1]) as f:
        content = json.load(f)
    out = render(content)
    with open(sys.argv[2], "w") as f:
        f.write(out)
    print(f"rendered {sys.argv[2]} ({len(out)} bytes)", file=sys.stderr)


if __name__ == "__main__":
    main()
