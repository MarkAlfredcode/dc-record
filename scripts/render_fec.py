#!/usr/bin/env python3
"""Renders content-TODAY.json into the exact existing "DC Record • Campaigns
& Congress" email HTML design (navy/red, Georgia/Arial + Newsreader-headline
exception, email-safe table layout, light-only lock) -- see
prompt-fec.txt's former ASSEMBLY section, which this replaces.

Usage: python3 render_fec.py content-TODAY.json newsletter-TODAY.html
"""
import html
import json
import sys

from dc_common import strip_li_wrapper

NAVY = "#1c3b5e"
RED = "#a6192e"
INK = "#161616"
BODY_INK = "#262626"
BORDER = "#e2ddd3"
GRAY = "#6b6b6b"

HEAD = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="color-scheme" content="light only">
<meta name="supported-color-schemes" content="light">
<title>DC Record • Campaigns & Congress • {date}</title>
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Newsreader:wght@700&display=swap">
<style>
  :root {{ color-scheme: light only; supported-color-schemes: light; }}
  html, body {{ background: #f7f5f2 !important; color: #262626 !important; }}
  [data-ogsc] body, [data-ogsc] table, [data-ogsc] td {{ background-color: inherit !important; color: inherit !important; }}
  a {{ color: #a6192e; }}
  .headline {{ font-family: 'Newsreader', Georgia, 'Times New Roman', serif; }}
  @media (max-width: 480px) {{
    .px {{ padding-left: 14px !important; padding-right: 14px !important; }}
  }}
  @media (min-width: 601px) {{
    .container {{ max-width: 760px !important; }}
  }}
</style>
</head>
<body style="margin:0; padding:0; background-color:#f7f5f2; color:#262626; font-family:Arial,Helvetica,sans-serif;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color:#f7f5f2;">
<tr><td align="center" style="padding:20px 10px;">
<table role="presentation" class="container" width="100%" cellpadding="0" cellspacing="0" style="width:100%; max-width:600px; margin:0 auto; background:#ffffff;">
"""

MASTHEAD = """<!-- MASTHEAD -->
<tr><td style="border-top:4px solid {navy}; padding:0;"></td></tr>
<tr><td class="px" style="padding:24px 18px 16px 18px; text-align:center;">
<div style="font-family:Georgia,'Times New Roman',serif; font-size:14px; letter-spacing:2px; color:#262626; text-transform:uppercase; margin-bottom:10px;">{date_upper}</div>
<div class="headline" style="font-family:'Newsreader', Georgia,'Times New Roman',serif; font-size:34px; font-weight:bold; color:{ink}; line-height:1.1;">THE <span style="color:{red};">DC</span> RECORD</div>
<div style="font-family:Georgia,'Times New Roman',serif; font-style:italic; font-size:14px; color:{red}; margin-top:8px;">Campaign finance compliance and Congress, every day.</div>
</td></tr>
<tr><td style="border-top:2px solid {navy}; border-bottom:3px solid {red}; padding:0;"></td></tr>
"""

FOOT = """</table>
</td></tr>
</table>
</body>
</html>
"""


def esc(s):
    return html.escape(s, quote=False)


def divider():
    return f'<tr><td style="border-top:1px solid {BORDER}; padding:0;"></td></tr>'


def render_blocks(blocks):
    out = []
    for b in blocks:
        t = b.get("type")
        if t == "paragraph":
            out.append(f'<p style="margin:0 0 14px 0;">{b["text"]}</p>')
        elif t == "subhead":
            out.append(f'<p style="margin:0 0 6px 0; font-weight:bold;">{esc(b["text"])}</p>')
        elif t == "note":
            out.append(f'<p style="margin:0 0 14px 0; font-size:14px; color:{GRAY};">{b["text"]}</p>')
        elif t == "list":
            items = b["items"]
            lis = "".join(
                f'<li style="margin-bottom:{0 if i == len(items) - 1 else 8}px;">{strip_li_wrapper(item)}</li>'
                for i, item in enumerate(items)
            )
            out.append(f'<ul style="margin:0 0 14px 0; padding-left:20px;">{lis}</ul>')
        elif t == "table":
            headers = "".join(f'<th align="left" style="border-bottom:2px solid {NAVY}; padding:6px 8px; font-size:13px;">{esc(h)}</th>' for h in b["headers"])
            rows = "".join(
                "<tr>" + "".join(f'<td style="padding:6px 8px; border-bottom:1px solid {BORDER}; font-size:14px;">{cell}</td>' for cell in row) + "</tr>"
                for row in b["rows"]
            )
            out.append(
                f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
                f'style="margin-bottom:14px;"><tr>{headers}</tr>{rows}</table>'
            )
    return "\n".join(out)


def render_top_story(ts):
    parts = [
        '<!-- TOP STORY -->',
        '<tr><td class="px" style="padding:26px 18px 8px 18px;">',
        f'<div style="font-family:Arial,Helvetica,sans-serif; font-size:12px; letter-spacing:1.5px; text-transform:uppercase; '
        f'color:{RED}; font-weight:bold; margin-bottom:6px;">{esc(ts.get("eyebrow", "Top Story"))}</div>',
        f'<div class="headline" style="font-family:\'Newsreader\', Georgia,\'Times New Roman\',serif; font-size:26px; '
        f'font-weight:bold; color:{INK}; line-height:1.25;">{ts["headline"]}</div>',
    ]
    if ts.get("dek"):
        parts.append(
            f'<div style="font-family:Georgia,\'Times New Roman\',serif; font-style:italic; font-size:16px; '
            f'color:{RED}; margin-top:10px; line-height:1.4;">{ts["dek"]}</div>'
        )
    parts.append('</td></tr>')
    parts.append(
        f'<tr><td class="px" style="padding:10px 18px 6px 18px; font-family:Arial,Helvetica,sans-serif; '
        f'font-size:16px; line-height:1.55; color:{BODY_INK};">'
        + "".join(f'<p style="margin:0 0 14px 0;">{p}</p>' for p in ts.get("paragraphs", []))
        + '</td></tr>'
    )
    callout = ts.get("callout")
    if callout:
        links_html = "<br>\n".join(f'<a href="{l["url"]}">{esc(l["text"])}</a>' for l in callout["links"])
        parts.append(
            f'<tr><td class="px" style="padding:6px 18px 24px 18px;">'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-left:3px solid {RED}; background:#ffffff;">'
            f'<tr><td style="padding:12px 16px;">'
            f'<div style="font-family:Arial,Helvetica,sans-serif; font-weight:bold; font-size:13px; color:{RED}; '
            f'text-transform:uppercase; letter-spacing:1px; margin-bottom:8px;">{esc(callout.get("label", "Read the filings"))}</div>'
            f'<div style="font-family:Arial,Helvetica,sans-serif; font-size:15px; line-height:1.6;">{links_html}</div>'
            f'</td></tr></table></td></tr>'
        )
    return "\n".join(parts)


def render_section(section):
    eyebrow = esc(section["eyebrow"])
    parts = [
        divider(),
        f'<tr><td class="px" style="padding:24px 18px 8px 18px;">',
        f'<div style="font-family:Arial,Helvetica,sans-serif; font-size:12px; letter-spacing:1.5px; text-transform:uppercase; '
        f'color:{NAVY}; font-weight:bold; margin-bottom:6px;">{eyebrow}</div>',
        f'<div class="headline" style="font-family:\'Newsreader\', Georgia,\'Times New Roman\',serif; font-size:21px; '
        f'font-weight:bold; color:{INK}; line-height:1.3;">{section["headline"]}</div>',
        '</td></tr>',
    ]
    body = []
    for p in section.get("paragraphs", []):
        body.append(f'<p style="margin:0 0 12px 0;">{p}</p>')
    for b in section.get("blocks", []):
        body.append(render_blocks([b]))
    if body:
        parts.append(
            f'<tr><td class="px" style="padding:8px 18px 20px 18px; font-family:Arial,Helvetica,sans-serif; '
            f'font-size:16px; line-height:1.55; color:{BODY_INK};">' + "\n".join(body) + '</td></tr>'
        )
    return "\n".join(parts)


def render_apod(apod):
    if not apod:
        return ""
    return f"""<!-- NASA APOD -->
{divider()}
<tr><td style="padding:24px 18px;">
<img src="{apod['image_url']}" alt="{esc(apod.get('title',''))}" width="100%" style="width:100%; height:auto; display:block; border:0;">
<p style="font-family:Arial,Helvetica,sans-serif; font-size:14px; line-height:1.5; color:{BODY_INK}; margin:12px 0 4px 0;"><strong>{esc(apod.get('title',''))}.</strong> {apod.get('caption','')}</p>
<p style="font-family:Arial,Helvetica,sans-serif; font-size:12px; color:{GRAY}; margin:0;">{esc(apod.get('credit',''))}</p>
</td></tr>
"""


def render_footer(footer):
    return f"""<!-- FOOTER -->
<tr><td style="border-top:2px solid {NAVY}; padding:22px 18px 26px 18px;" class="px">
<p style="font-family:Arial,Helvetica,sans-serif; font-size:13px; line-height:1.6; color:{GRAY}; margin:0 0 10px 0;">{footer["coverage_window"]}</p>
<p style="font-family:Arial,Helvetica,sans-serif; font-size:13px; line-height:1.6; color:{GRAY}; margin:0 0 10px 0;">Sources: {footer["sources"]}</p>
<p style="font-family:Arial,Helvetica,sans-serif; font-size:13px; line-height:1.6; color:{GRAY}; margin:0 0 10px 0;">{footer["known_gaps"]}</p>
<p style="font-family:Georgia,'Times New Roman',serif; font-style:italic; font-size:14px; color:{INK}; margin:14px 0 0 0;">— The DC Record</p>
</td></tr>
"""


def render(content):
    date_str = content["date"]
    parts = [HEAD.format(date=date_str), MASTHEAD.format(navy=NAVY, red=RED, ink=INK, date_upper=date_str.upper())]
    parts.append(render_top_story(content["top_story"]))
    sections = content.get("sections", [])
    news_sections = [s for s in sections if not s.get("no_news")]
    quiet_sections = [s for s in sections if s.get("no_news")]
    for s in news_sections + quiet_sections:
        parts.append(f'\n<!-- SECTION: {esc(s["eyebrow"]).upper()} -->')
        parts.append(render_section(s))
    parts.append(render_apod(content.get("apod")))
    parts.append(render_footer(content["footer"]))
    parts.append(FOOT)
    return "\n".join(parts)


def main():
    if len(sys.argv) != 3:
        print("usage: render_fec.py content.json output.html", file=sys.stderr)
        sys.exit(1)
    with open(sys.argv[1]) as f:
        content = json.load(f)
    html_out = render(content)
    with open(sys.argv[2], "w") as f:
        f.write(html_out)
    print(f"rendered {sys.argv[2]} ({len(html_out)} bytes)", file=sys.stderr)


if __name__ == "__main__":
    main()
