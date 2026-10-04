"""Shared render pieces for the boxes every edition gets at the top
(added 2026-10-03):
- "Your Beat": every mention of a data/watchlist.json name anywhere in the
  edition, pulled up top (computed by assemble.py into content["beat"]).
- "Leads": up to 3 leads Claude writes into content["leads"], each with the
  angle and the next document to pull.

Email editions (campaigns/officials/feds) use table rows; the web-only
Congress edition uses plain divs.
"""
import html


def _esc(s):
    return html.escape(str(s or ""), quote=False)


def _lead_html(lead, link_color):
    nd = lead.get("next_doc") or {}
    nxt = ""
    if nd.get("url"):
        nxt = f'<br><span style="font-size:13px;">Next: <a href="{html.escape(nd["url"], quote=True)}" style="color:{link_color};">{_esc(nd.get("text") or "document")}</a></span>'
    elif nd.get("text"):
        nxt = f'<br><span style="font-size:13px;">Next: {_esc(nd["text"])}</span>'
    return f'<strong>{lead.get("headline", "")}</strong><br>{lead.get("angle", "")}{nxt}'


def email_boxes(content, accent, ink, body_ink, pad="18px"):
    """<tr> rows for the Your Beat + Leads boxes, or '' if neither has content."""
    out = []
    beat = content.get("beat") or []
    if beat:
        items = []
        for b in beat:
            mentions = "".join(f'<li style="margin:4px 0 0 0;">{m["html"]} <span style="color:#777; font-size:12px;">({_esc(m["section"])})</span></li>' for m in b["mentions"])
            items.append(f'<li style="margin-bottom:10px;"><strong>{_esc(b["name"])}</strong>'
                         + (f' <span style="color:#777;">— {_esc(b["why"])}</span>' if b.get("why") else "")
                         + f'<ul style="margin:2px 0 0 0; padding-left:18px;">{mentions}</ul></li>')
        out.append(_email_box("Your Beat", "".join(items), accent, ink, body_ink, pad))
    leads = [l for l in (content.get("leads") or []) if l.get("headline")][:3]
    if leads:
        items = "".join(f'<li style="margin-bottom:10px;">{_lead_html(l, accent)}</li>' for l in leads)
        out.append(_email_box("Leads", items, accent, ink, body_ink, pad))
    return "\n".join(out)


def _email_box(label, items_html, accent, ink, body_ink, pad):
    return (
        f'<tr><td class="px" style="padding:16px {pad} 0 {pad};">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#faf8f3; border-left:4px solid {accent};">'
        f'<tr><td style="padding:12px 14px; font-family:Arial,Helvetica,sans-serif; font-size:14px; line-height:1.5; color:{body_ink};">'
        f'<div style="font-size:12px; letter-spacing:1.5px; text-transform:uppercase; font-weight:bold; color:{accent}; padding-bottom:6px;">{label}</div>'
        f'<ul style="margin:0; padding-left:18px;">{items_html}</ul>'
        f'</td></tr></table></td></tr>'
    )


def web_boxes(content, accent):
    out = []
    beat = content.get("beat") or []
    if beat:
        lis = "".join(
            f'<li><strong>{_esc(b["name"])}</strong>' + (f' — <em>{_esc(b["why"])}</em>' if b.get("why") else "")
            + "<ul>" + "".join(f'<li>{m["html"]} <small>({_esc(m["section"])})</small></li>' for m in b["mentions"]) + "</ul></li>"
            for b in beat)
        out.append(_web_box("Your Beat", lis, accent))
    leads = [l for l in (content.get("leads") or []) if l.get("headline")][:3]
    if leads:
        out.append(_web_box("Leads", "".join(f"<li>{_lead_html(l, accent)}</li>" for l in leads), accent))
    return "\n".join(out)


def _web_box(label, lis, accent):
    return (f'  <div style="background:#faf8f3; border-left:4px solid {accent}; padding:12px 14px; margin:16px 0; font-size:14px;">'
            f'<div class="eyebrow" style="padding-top:0;">{label}</div><ul style="margin:0;">{lis}</ul></div>')
