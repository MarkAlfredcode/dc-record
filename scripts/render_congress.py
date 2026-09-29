#!/usr/bin/env python3
"""Renders content-TODAY.json into the "DC Record • Congress" page design
(burgundy/gunmetal, modern non-email CSS since this edition is web-only).

Usage: python3 render_congress.py content-TODAY.json newsletter-TODAY.html
"""
import html
import json
import sys

from dc_common import strip_li_wrapper

HEAD = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>The DC Record • Congress</title>
<meta name="color-scheme" content="light only">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Newsreader:wght@700&display=swap">
<style>
  :root { color-scheme: light only; }
  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; background: #f7f5f2; color: #262626; }
  body { font-family: Arial, Helvetica, sans-serif; font-size: 16px; line-height: 1.5; }
  .wrap { max-width: 600px; margin: 0 auto; background: #ffffff; padding: 0 20px 30px; }
  @media (min-width: 601px) { .wrap { max-width: 760px; padding: 0 32px 40px; } }
  .rule-top { height: 4px; background: #6e2332; margin: 0 -20px; }
  .rule-bottom { height: 3px; background: #445164; margin: 24px -20px 0; }
  @media (min-width: 601px) { .rule-top, .rule-bottom { margin-left: -32px; margin-right: -32px; } }
  .masthead { text-align: center; padding: 20px 0 18px; }
  .date { font-family: Arial, Helvetica, sans-serif; font-size: 13px; letter-spacing: 1px; text-transform: uppercase; color: #6b6b64; margin-bottom: 6px; }
  .nameplate { font-family: 'Newsreader', Georgia, 'Times New Roman', serif; font-size: 34px; font-weight: bold; color: #161616; letter-spacing: 0.5px; margin: 0; }
  .nameplate .dc { color: #6e2332; }
  .tagline { font-family: Georgia, 'Times New Roman', serif; font-style: italic; font-size: 15px; color: #3c4858; margin-top: 4px; }
  .eyebrow { font-family: Arial, Helvetica, sans-serif; font-size: 12px; font-weight: bold; letter-spacing: 1.5px; color: #6e2332; text-transform: uppercase; padding-top: 20px; padding-bottom: 8px; }
  h1.headline { font-family: 'Newsreader', Georgia, 'Times New Roman', serif; font-size: 26px; line-height: 32px; color: #161616; margin: 0 0 8px 0; }
  .dek { font-family: Georgia, 'Times New Roman', serif; font-style: italic; font-size: 16px; color: #6e2332; margin: 0 0 14px 0; }
  h2.headline { font-family: 'Newsreader', Georgia, 'Times New Roman', serif; font-size: 21px; line-height: 27px; color: #161616; margin: 0 0 10px 0; }
  p { margin: 0 0 12px 0; }
  ul { margin: 0 0 14px 0; padding-left: 20px; }
  li { margin-bottom: 12px; }
  a { color: #445164; }
  .callout { border: 1px solid #6e2332; border-left: 4px solid #6e2332; padding: 12px 16px; margin: 16px 0; font-size: 15px; }
  .callout a { font-weight: bold; }
  .footer { margin-top: 24px; padding-top: 16px; border-top: 1px solid #dcd5c7; font-size: 13px; color: #6b6b64; }
  .footer p { margin: 0 0 8px 0; }
  .signoff { font-family: Georgia, 'Times New Roman', serif; font-style: italic; margin-top: 10px; }
</style>
</head>
<body>
<div class="wrap">
  <div class="rule-top"></div>
  <div class="masthead">
    <div class="date">__DATE__</div>
    <div class="nameplate">THE <span class="dc">DC</span> RECORD</div>
    <div class="tagline">Congressional Record digest, every day.</div>
  </div>
"""

FOOT = """</div>
</body>
</html>
"""


def esc(s):
    return html.escape(s, quote=False)


def render_blocks(blocks):
    out = []
    for b in blocks:
        t = b.get("type")
        if t == "paragraph":
            out.append(f'<p>{b["text"]}</p>')
        elif t == "note":
            out.append(f'<p>{b["text"]}</p>')
        elif t == "list":
            lis = "".join(f'<li>{strip_li_wrapper(item)}</li>' for item in b["items"])
            out.append(f'<ul>{lis}</ul>')
    return "\n".join(out)


def render_top_story(ts):
    parts = [
        f'  <div class="eyebrow">{esc(ts.get("eyebrow", "Top Story"))}</div>',
        f'  <h1 class="headline">{ts["headline"]}</h1>',
    ]
    if ts.get("dek"):
        parts.append(f'  <div class="dek">{ts["dek"]}</div>')
    parts.extend(f'  <p>{p}</p>' for p in ts.get("paragraphs", []))
    callout = ts.get("callout")
    if callout and callout.get("links"):
        # single-line callout matching existing design: "Read the bill: <link>"
        link = callout["links"][0]
        parts.append(f'  <div class="callout">{esc(callout.get("label", "Read the bill"))}: <a href="{link["url"]}">{esc(link["text"])}</a></div>')
    return "\n".join(parts)


def render_section(section):
    parts = [
        f'  <div class="eyebrow">{esc(section["eyebrow"])}</div>',
        f'  <h2 class="headline">{section["headline"]}</h2>',
    ]
    parts.extend(f'  <p>{p}</p>' for p in section.get("paragraphs", []))
    for b in section.get("blocks", []):
        parts.append("  " + render_blocks([b]))
    return "\n".join(parts)


def render_footer(footer):
    return f"""  <div class="footer">
    <p><strong>Coverage window:</strong> {footer["coverage_window"]}</p>
    <p><strong>Source:</strong> {footer["sources"]}</p>
    <p>{footer["known_gaps"]}</p>
    <div class="signoff">— The DC Record</div>
  </div>
  <div class="rule-bottom"></div>
"""


def render(content):
    parts = [HEAD.replace("__DATE__", esc(content["date"])), render_top_story(content["top_story"])]
    sections = content.get("sections", [])
    news_sections = [s for s in sections if not s.get("no_news")]
    quiet_sections = [s for s in sections if s.get("no_news")]
    for s in news_sections + quiet_sections:
        parts.append(render_section(s))
    parts.append(render_footer(content["footer"]))
    parts.append(FOOT)
    return "\n".join(parts)


def main():
    if len(sys.argv) != 3:
        print("usage: render_congress.py content.json output.html", file=sys.stderr)
        sys.exit(1)
    with open(sys.argv[1]) as f:
        content = json.load(f)
    html_out = render(content)
    with open(sys.argv[2], "w") as f:
        f.write(html_out)
    print(f"rendered {sys.argv[2]} ({len(html_out)} bytes)", file=sys.stderr)


if __name__ == "__main__":
    main()
