#!/usr/bin/env python3
"""Renders content-TODAY.json into the "DC Record • Officials" email HTML
design (forest green/brass gold, Georgia/Arial + Newsreader-headline
exception, email-safe table layout, light-only lock).

Usage: python3 render_officials.py content-TODAY.json newsletter-TODAY.html
"""
import html
import json
import sys

from dc_common import strip_bold_tags, strip_li_wrapper
from render_common import email_boxes

GREEN = "#2f5233"
GOLD = "#9c7a29"
INK = "#161616"
BODY_INK = "#262626"
TABLE_BORDER = "#e6e2d8"
DIVIDER = "#d8d3c8"
GRAY = "#555"

HEAD = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="color-scheme" content="light only">
<meta name="supported-color-schemes" content="light">
<title>DC Record • Officials • {date}</title>
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Newsreader:wght@700&display=swap">
<style>
  :root {{ color-scheme: light only; supported-color-schemes: light; }}
  html, body {{ background: #f7f5f2 !important; color: #262626 !important; margin:0; padding:0; }}
  [data-ogsc] body, [data-ogsc] table, [data-ogsc] td {{ background-color: inherit !important; color: inherit !important; }}
  table {{ border-collapse: collapse; }}
  a {{ color: {green}; }}
  .headline {{ font-family: 'Newsreader', Georgia, 'Times New Roman', serif; }}
  @media (max-width: 480px) {{
    .px {{ padding-left:14px !important; padding-right:14px !important; }}
    .headline {{ font-size:21px !important; }}
    .body-copy {{ font-size:16px !important; }}
  }}
  @media (min-width: 601px) {{
    .container {{ max-width: 760px !important; }}
  }}
</style>
</head>
<body style="margin:0; padding:0; background:#f7f5f2;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f7f5f2;">
<tr><td align="center" style="padding:20px 10px;">
<table role="presentation" class="container" width="100%" cellpadding="0" cellspacing="0" style="width:100%; max-width:600px; margin:0 auto; background:#ffffff;">
""".replace("{green}", GREEN)

MASTHEAD = """<!-- MASTHEAD -->
<tr><td style="border-top:4px solid {green}; padding:0;"></td></tr>
<tr><td class="px" style="padding:20px 18px 6px 18px; text-align:center;">
  <div style="font-family:Georgia,'Times New Roman',serif; font-size:14px; letter-spacing:2px; color:{gold}; text-transform:uppercase;">{date_upper}</div>
  <div class="headline" style="font-family:'Newsreader', Georgia,'Times New Roman',serif; font-size:34px; font-weight:bold; color:{ink}; letter-spacing:1px; padding:6px 0 4px 0;">THE <span style="color:{gold};">DC</span> RECORD</div>
  <div style="font-family:Georgia,'Times New Roman',serif; font-style:italic; font-size:14px; color:{body};">Ethics filings, lobbying, and personal financial disclosures, every day.</div>
</td></tr>
<tr><td style="padding:10px 18px 0 18px;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>
    <td width="50%" style="border-bottom:3px solid {green}; font-size:1px; line-height:1px;">&nbsp;</td>
    <td width="50%" style="border-bottom:3px solid {gold}; font-size:1px; line-height:1px;">&nbsp;</td>
  </tr></table>
</td></tr>
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
    return (
        '<tr><td style="padding:0 18px;"><table role="presentation" width="100%" cellpadding="0" cellspacing="0">'
        f'<tr><td style="border-top:1px solid {DIVIDER}; font-size:1px; line-height:1px;">&nbsp;</td></tr></table></td></tr>'
    )


def render_table(b, header_color):
    headers = "".join(
        f'<th align="left" style="background:#f7f5f2; border-bottom:2px solid {header_color}; padding:8px 6px; color:{INK};">{esc(h)}</th>'
        for h in b["headers"]
    )
    rows = "".join(
        "<tr>" + "".join(
            f'<td style="padding:8px 6px; border-bottom:1px solid {TABLE_BORDER}; vertical-align:top;">{cell}</td>' for cell in row
        ) + "</tr>"
        for row in b["rows"]
    )
    return (
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="font-family:Arial,Helvetica,sans-serif; font-size:14px;">{headers}{rows}</table>'
    )


def render_blocks(blocks):
    out = []
    table_idx = 0
    for b in blocks:
        t = b.get("type")
        if t == "paragraph":
            out.append(f'<p style="margin:0 0 14px 0;">{b["text"]}</p>')
        elif t == "subhead":
            out.append(f'<p data-toc-sub="1" style="margin:0 0 6px 0; font-weight:bold;">{esc(strip_bold_tags(b["text"]))}</p>')
        elif t == "note":
            out.append(f'<p style="margin:0 0 8px 0;">{b["text"]}</p>')
        elif t == "list":
            items = b["items"]
            lis = "".join(
                f'<li style="margin-bottom:{0 if i == len(items) - 1 else 6}px;">{strip_li_wrapper(item)}</li>'
                for i, item in enumerate(items)
            )
            out.append(f'<ul style="margin:0 0 16px 0; padding-left:20px;">{lis}</ul>')
        elif t == "table":
            header_color = GREEN if table_idx == 0 else GOLD
            out.append(render_table(b, header_color))
            table_idx += 1
    return "\n".join(out)


def render_top_story(ts):
    parts = [
        '<!-- TOP STORY -->',
        '<tr><td class="px" style="padding:28px 18px 0 18px;">',
        f'<div style="font-family:Arial,Helvetica,sans-serif; font-size:12px; letter-spacing:1.5px; color:{GOLD}; '
        f'text-transform:uppercase; font-weight:bold;">{esc(ts.get("eyebrow", "Top Story"))}</div>',
        f'<div class="headline" style="font-family:\'Newsreader\', Georgia,\'Times New Roman\',serif; font-size:26px; '
        f'line-height:1.25; color:{INK}; font-weight:bold; padding:8px 0 6px 0;">{ts["headline"]}</div>',
    ]
    if ts.get("dek"):
        parts.append(
            f'<div style="font-family:Georgia,\'Times New Roman\',serif; font-style:italic; font-size:15px; '
            f'color:{GOLD}; padding-bottom:12px;">{ts["dek"]}</div>'
        )
    parts.append('</td></tr>')
    parts.append(
        f'<tr><td class="px body-copy" style="padding:0 18px; font-family:Arial,Helvetica,sans-serif; '
        f'font-size:16px; line-height:1.55; color:{BODY_INK};">'
        + "".join(f'<p style="margin:0 0 14px 0;">{p}</p>' for p in ts.get("paragraphs", []))
        + '</td></tr>'
    )
    callout = ts.get("callout")
    if callout:
        links_html = "<br>\n".join(f'<a href="{l["url"]}" style="color:{GREEN};">{esc(l["text"])}</a>' for l in callout["links"])
        parts.append(
            f'<tr><td class="px" style="padding:16px 18px 28px 18px;">'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-top:2px solid {GOLD}; border-bottom:2px solid {GOLD};">'
            f'<tr><td style="padding:14px 4px; font-family:Arial,Helvetica,sans-serif; font-size:15px; color:{BODY_INK};">'
            f'<strong style="color:{INK};">{esc(callout.get("label", "Read the filing"))}:</strong><br>{links_html}'
            f'</td></tr></table></td></tr>'
        )
    return "\n".join(parts)


def render_section(section):
    eyebrow = esc(section["eyebrow"])
    parts = [
        divider(),
        '<tr><td class="px" style="padding:26px 18px 0 18px;">',
        f'<div style="font-family:Arial,Helvetica,sans-serif; font-size:12px; letter-spacing:1.5px; color:{GOLD}; '
        f'text-transform:uppercase; font-weight:bold;">{eyebrow}</div>',
        f'<div class="headline" style="font-family:\'Newsreader\', Georgia,\'Times New Roman\',serif; font-size:21px; '
        f'line-height:1.3; color:{INK}; font-weight:bold; padding:8px 0 10px 0;">{section["headline"]}</div>',
        '</td></tr>',
    ]
    if section.get("paragraphs"):
        parts.append(
            f'<tr><td class="px body-copy" style="padding:0 18px; font-family:Arial,Helvetica,sans-serif; '
            f'font-size:16px; line-height:1.55; color:{BODY_INK};">'
            + "".join(f'<p style="margin:0 0 14px 0;">{p}</p>' for p in section["paragraphs"])
            + '</td></tr>'
        )
    blocks = section.get("blocks", [])
    if blocks:
        parts.append(f'<tr><td class="px" style="padding:6px 18px 20px 18px;">{render_blocks(blocks)}</td></tr>')
    return "\n".join(parts)


def render_footer(footer):
    return f"""<!-- FOOTER -->
<tr><td style="padding:0 18px;"><table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr><td style="border-top:3px solid {GREEN}; font-size:1px; line-height:1px;">&nbsp;</td></tr></table></td></tr>
<tr><td class="px body-copy" style="padding:16px 18px 26px 18px; font-family:Arial,Helvetica,sans-serif; font-size:13px; line-height:1.6; color:{GRAY};">
<p style="margin:0 0 8px 0;"><strong>Coverage window:</strong> {footer["coverage_window"]}</p>
<p style="margin:0 0 8px 0;"><strong>Sources:</strong> {footer["sources"]}</p>
<p style="margin:0 0 8px 0;">{footer["known_gaps"]}</p>
<p style="margin:0 0 8px 0; font-size:12px;">To change which editions/times you get, or to unsubscribe, visit <a href="https://markalfred.news/dc-record" style="color:{GREEN};">markalfred.news/dc-record</a> and enter the email address this was sent to.</p>
<p style="margin:0;">— The DC Record</p>
</td></tr>
"""


def render(content):
    date_str = content["date"]
    parts = [HEAD.format(date=date_str), MASTHEAD.format(green=GREEN, gold=GOLD, ink=INK, body=BODY_INK, date_upper=date_str.upper())]
    parts.append(email_boxes(content, GREEN, INK, BODY_INK))
    parts.append(render_top_story(content["top_story"]))
    sections = content.get("sections", [])
    news_sections = [s for s in sections if not s.get("no_news")]
    quiet_sections = [s for s in sections if s.get("no_news")]
    for s in news_sections + quiet_sections:
        parts.append(f'\n<!-- SECTION: {esc(s["eyebrow"]).upper()} -->')
        parts.append(render_section(s))
    parts.append(render_footer(content["footer"]))
    parts.append(FOOT)
    return "\n".join(parts)


def main():
    if len(sys.argv) != 3:
        print("usage: render_officials.py content.json output.html", file=sys.stderr)
        sys.exit(1)
    with open(sys.argv[1]) as f:
        content = json.load(f)
    html_out = render(content)
    with open(sys.argv[2], "w") as f:
        f.write(html_out)
    print(f"rendered {sys.argv[2]} ({len(html_out)} bytes)", file=sys.stderr)


if __name__ == "__main__":
    main()
