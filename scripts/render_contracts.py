#!/usr/bin/env python3
"""Renders content-TODAY.json into the exact existing "DC Record • Executive
Branch" email HTML design (charcoal slate/rust, Georgia/Arial + Newsreader-
headline exception, email-safe table layout, mobile-first/601px widen,
light-only lock) -- see prompt-contracts.txt's former ASSEMBLY section, which
this replaces. Claude no longer generates this markup; it only writes
content-TODAY.json (see dc_common.py's module docstring for why).

Usage: python3 render_contracts.py content-TODAY.json newsletter-TODAY.html
"""
import html
import json
import sys

INK = "#161616"
BODY_INK = "#262626"
SLATE = "#3c4858"
RUST = "#b5563c"
BORDER = "#e3ddd3"
GRAY = "#565656"

HEAD = """<!DOCTYPE html>
<html lang="en" xmlns="http://www.w3.org/1999/xhtml">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="color-scheme" content="light only">
<meta name="supported-color-schemes" content="light">
<title>DC Record • Executive Branch • {date}</title>
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Newsreader:wght@700&display=swap">
<style>
  :root {{ color-scheme: light only; supported-color-schemes: light; }}
  html, body {{ background: #f7f5f2 !important; color: #262626 !important; }}
  [data-ogsc] body, [data-ogsc] table, [data-ogsc] td {{ background-color: inherit !important; color: inherit !important; }}
  body, table, td, a {{ -webkit-text-size-adjust: 100%; -ms-text-size-adjust: 100%; }}
  table, td {{ mso-table-lspace: 0pt; mso-table-rspace: 0pt; }}
  img {{ -ms-interpolation-mode: bicubic; border: 0; line-height: 100%; outline: none; text-decoration: none; }}
  a {{ color: #b5563c; }}
  .headline {{ font-family: 'Newsreader', Georgia, 'Times New Roman', serif; }}
  @media (max-width: 480px) {{
    .px {{ padding-left: 16px !important; padding-right: 16px !important; }}
    .h1 {{ font-size: 24px !important; line-height: 30px !important; }}
    .h2 {{ font-size: 19px !important; line-height: 25px !important; }}
  }}
  @media (min-width: 601px) {{
    .container {{ max-width: 760px !important; }}
  }}
</style>
</head>
<body style="margin:0; padding:0; background-color:#f7f5f2; color:#262626; font-family:Arial, Helvetica, sans-serif;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#f7f5f2; width:100%;">
<tr>
<td align="center" style="padding:20px 0;">

<table role="presentation" class="container" width="100%" cellpadding="0" cellspacing="0" border="0" style="width:100%; max-width:600px; margin:0 auto; background-color:#ffffff;">
"""

MASTHEAD = """<!-- MASTHEAD -->
<tr>
<td class="px" style="padding:24px 20px 16px 20px; border-top:3px solid #3c4858; border-bottom:3px solid #b5563c;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
<tr><td align="center" style="font-family:Georgia, 'Times New Roman', serif; font-size:13px; letter-spacing:2px; color:#3c4858; padding-bottom:6px;">{date_upper}</td></tr>
<tr><td align="center" class="headline" style="font-family:'Newsreader', Georgia, 'Times New Roman', serif; font-size:36px; font-weight:bold; color:#161616; padding-bottom:6px;">THE <span style="color:#b5563c;">DC</span> RECORD</td></tr>
<tr><td align="center" style="font-family:Georgia, 'Times New Roman', serif; font-style:italic; font-size:14px; color:#3c4858; padding-bottom:4px;">Federal contracting, apportionments, and regulatory action, every day.</td></tr>
<tr><td align="center" style="font-family:Arial, Helvetica, sans-serif; font-size:12px; letter-spacing:1px; color:#8a8a8a; text-transform:uppercase;">Executive Branch Edition</td></tr>
</table>
</td>
</tr>
"""

FOOT = """</table>
</td>
</tr>
</table>
</body>
</html>
"""


def esc(s):
    """Content already contains intentional inline <strong>/<a>/<em> -- don't
    HTML-escape it. Only used where we build our own literal text (labels)."""
    return html.escape(s, quote=False)


def render_blocks(blocks, body_font_size="16px", body_line_height="24px"):
    out = []
    for b in blocks:
        t = b.get("type")
        if t == "paragraph":
            out.append(f'<p style="margin:0 0 12px 0;">{b["text"]}</p>')
        elif t == "note":
            # smaller, gray "also filed/posted" rollup line
            out.append(
                f'<tr><td class="px" style="padding:0 20px 24px 20px; font-family:Arial, Helvetica, sans-serif; '
                f'font-size:14px; line-height:21px; color:{GRAY};">{b["text"]}</td></tr>'
            )
        elif t == "subhead":
            out.append(
                f'<tr><td class="px" style="padding:4px 20px 6px 20px;">'
                f'<div style="font-family:Arial, Helvetica, sans-serif; font-size:13px; font-weight:bold; '
                f'color:{SLATE}; text-transform:uppercase; letter-spacing:0.5px; padding:10px 0 6px 0;">{esc(b["text"])}</div>'
                f'</td></tr>'
            )
        elif t == "list":
            items = b["items"]
            lis = "".join(
                f'<li style="margin-bottom:{0 if i == len(items) - 1 else 10}px;">{item}</li>'
                for i, item in enumerate(items)
            )
            out.append(
                f'<ul style="margin:0; padding-left:20px; font-family:Arial, Helvetica, sans-serif; '
                f'font-size:15px; line-height:23px; color:#262626;">{lis}</ul>'
            )
        elif t == "table":
            headers = "".join(f'<th align="left" style="border-bottom:2px solid {RUST}; padding:6px 8px; font-size:13px;">{esc(h)}</th>' for h in b["headers"])
            rows = "".join(
                "<tr>" + "".join(f'<td style="padding:6px 8px; border-bottom:1px solid {BORDER}; font-size:14px;">{cell}</td>' for cell in row) + "</tr>"
                for row in b["rows"]
            )
            out.append(
                f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
                f'style="font-family:Arial, Helvetica, sans-serif; margin-bottom:12px;"><tr>{headers}</tr>{rows}</table>'
            )
    return "\n".join(out)


def render_section(section, is_first):
    eyebrow = esc(section["eyebrow"])
    headline = section["headline"]
    border_top = f' border-top:1px solid {BORDER};' if not is_first else ""
    parts = [
        f'<tr><td class="px" style="padding:10px 20px 6px 20px;{border_top}">',
        f'<div style="font-family:Arial, Helvetica, sans-serif; font-size:12px; font-weight:bold; '
        f'letter-spacing:1.5px; color:{RUST}; text-transform:uppercase; padding-top:16px; padding-bottom:8px;">{eyebrow}</div>',
        f'<h2 class="h2 headline" style="font-family:\'Newsreader\', Georgia, \'Times New Roman\', serif; '
        f'font-size:21px; line-height:27px; color:{INK}; margin:0 0 10px 0;">{headline}</h2>',
    ]
    para_blocks = [b for b in section.get("blocks", []) if b.get("type") == "paragraph" and b.get("_intro")]
    intro_paragraphs = section.get("paragraphs", [])
    if intro_paragraphs:
        parts.append(f'<div style="font-family:Arial, Helvetica, sans-serif; font-size:16px; line-height:24px; color:{BODY_INK};">')
        parts.append("".join(f'<p style="margin:0 0 12px 0;">{p}</p>' for p in intro_paragraphs))
        parts.append("</div>")
    parts.append("</td></tr>")

    # remaining blocks (subheads/lists/tables/notes) each get their own <tr><td>
    for b in section.get("blocks", []):
        t = b.get("type")
        if t == "subhead":
            parts.append(render_blocks([b]))
        elif t == "note":
            parts.append(render_blocks([b]))
        elif t in ("list", "table"):
            inner = render_blocks([b])
            parts.append(f'<tr><td class="px" style="padding:0 20px 20px 20px;">{inner}</td></tr>')
        elif t == "paragraph":
            parts.append(
                f'<tr><td class="px" style="padding:0 20px 12px 20px; font-family:Arial, Helvetica, sans-serif; '
                f'font-size:16px; line-height:24px; color:{BODY_INK};"><p style="margin:0;">{b["text"]}</p></td></tr>'
            )
    return "\n".join(parts)


def render_top_story(ts):
    parts = [
        '<!-- TOP STORY -->',
        '<tr><td class="px" style="padding:26px 20px 10px 20px;">',
        f'<div style="font-family:Arial, Helvetica, sans-serif; font-size:12px; font-weight:bold; '
        f'letter-spacing:1.5px; color:{RUST}; text-transform:uppercase; padding-bottom:8px;">{esc(ts.get("eyebrow", "Top Story"))}</div>',
        f'<h1 class="h1 headline" style="font-family:\'Newsreader\', Georgia, \'Times New Roman\', serif; '
        f'font-size:28px; line-height:34px; color:{INK}; margin:0 0 8px 0;">{ts["headline"]}</h1>',
    ]
    if ts.get("dek"):
        parts.append(
            f'<div style="font-family:Georgia, \'Times New Roman\', serif; font-style:italic; '
            f'font-size:16px; color:{RUST}; padding-bottom:14px;">{ts["dek"]}</div>'
        )
    parts.append(f'<div style="font-family:Arial, Helvetica, sans-serif; font-size:16px; line-height:24px; color:{BODY_INK};">')
    parts.append("".join(f'<p style="margin:0 0 14px 0;">{p}</p>' for p in ts.get("paragraphs", [])))
    parts.append("</div></td></tr>")
    callout = ts.get("callout")
    if callout:
        links_html = "<br>\n".join(f'<a href="{l["url"]}" style="color:{RUST};">{esc(l["text"])}</a>' for l in callout["links"])
        parts.append(
            f'<tr><td class="px" style="padding:16px 20px 26px 20px;">'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="border:1px solid {RUST};">'
            f'<tr><td style="padding:16px 18px;">'
            f'<div style="font-family:Arial, Helvetica, sans-serif; font-size:12px; font-weight:bold; letter-spacing:1px; '
            f'color:{RUST}; text-transform:uppercase; padding-bottom:8px;">{esc(callout.get("label", "Read the filings"))}</div>'
            f'<div style="font-family:Arial, Helvetica, sans-serif; font-size:15px; line-height:23px;">{links_html}</div>'
            f'</td></tr></table></td></tr>'
        )
    return "\n".join(parts)


def render_footer(footer):
    return f"""<!-- FOOTER -->
<tr>
<td class="px" style="padding:20px 20px 26px 20px; border-top:3px solid {SLATE};">
<div style="font-family:Arial, Helvetica, sans-serif; font-size:13px; line-height:20px; color:{GRAY};">
<p style="margin:0 0 10px 0;"><strong>Coverage window:</strong> {footer["coverage_window"]}</p>
<p style="margin:0 0 10px 0;"><strong>Sources:</strong> {footer["sources"]}</p>
<p style="margin:0 0 10px 0;"><strong>Known gaps this edition:</strong> {footer["known_gaps"]}</p>
<p style="margin:20px 0 0 0; font-family:Georgia, 'Times New Roman', serif; font-style:italic; color:{SLATE};">— The DC Record</p>
</div>
</td>
</tr>
"""


def render(content):
    date_str = content["date"]
    parts = [HEAD.format(date=date_str), MASTHEAD.format(date_upper=date_str.upper())]
    parts.append(render_top_story(content["top_story"]))
    # Reorder: sections with real news first (in their given order), then
    # no_news sections (in their given order) -- this used to be a rule
    # Claude had to apply by hand; now it's mechanical.
    sections = content.get("sections", [])
    news_sections = [s for s in sections if not s.get("no_news")]
    quiet_sections = [s for s in sections if s.get("no_news")]
    ordered = news_sections + quiet_sections
    for i, s in enumerate(ordered):
        parts.append(f'\n<!-- SECTION: {esc(s["eyebrow"]).upper()} -->')
        parts.append(render_section(s, is_first=False))
    parts.append(render_footer(content["footer"]))
    parts.append(FOOT)
    return "\n".join(parts)


def main():
    if len(sys.argv) != 3:
        print("usage: render_contracts.py content.json output.html", file=sys.stderr)
        sys.exit(1)
    with open(sys.argv[1]) as f:
        content = json.load(f)
    html_out = render(content)
    with open(sys.argv[2], "w") as f:
        f.write(html_out)
    print(f"rendered {sys.argv[2]} ({len(html_out)} bytes)", file=sys.stderr)


if __name__ == "__main__":
    main()
