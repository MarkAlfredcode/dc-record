// oge@markalfred.news: delivery address for OGE Online Form 201 requests (scripts/oge_requests.py in dc-record files them).
// OGE emails the requested ethics documents (financial disclosures, 278-Ts, certificates of divestiture, ethics agreements)
// within ~2 business days, as attachments or as download links. Same pattern as foia@: the Yahoo forward happens first in
// index.js, then this logs the message to D1 `oge_inbox` / `oge_attachments`. Nothing is matched to FOIA requests, and
// nothing here pushes a phone alert. scripts/oge_inbox.py summarizes the rows for the top of the Officials edition.

import PostalMime from 'postal-mime';
import { readAll, toBase64 } from './attachments.js';
import { aiMarkers, decode } from './match.js';

function stripHtml(html) {
  return decode((html || '').replace(/<style[\s\S]*?<\/style>|<script[\s\S]*?<\/script>/gi, ' ')
    .replace(/<br\s*\/?>|<\/p>|<\/div>/gi, '\n').replace(/<[^>]+>/g, ' '));
}

// Every http(s) link in the message (href targets of the HTML part included), oge.gov first.
export function linksOf(email) {
  const found = [];
  const add = function (u) { u = String(u || '').replace(/&amp;/g, '&').replace(/[)>.,;\]]+$/, ''); if (/^https?:\/\//i.test(u) && found.indexOf(u) < 0) found.push(u); };
  for (const m of String(email.html || '').matchAll(/href\s*=\s*["']([^"']+)["']/gi)) add(m[1]);
  for (const m of String(email.text || '').matchAll(/https?:\/\/[^\s<>"']+/gi)) add(m[0]);
  const rank = function (u) { return /(^|\.)oge\.gov/i.test(new URL(u).hostname) ? 0 : 1; };
  return found.sort(function (a, b) { return rank(a) - rank(b); }).slice(0, 25);
}

export async function recordOge(raw, env) {
  const db = env.FOIA_DB;
  const email = await PostalMime.parse(raw);
  const from = ((email.from && email.from.address) || '').toLowerCase();
  const subject = (email.subject || '').slice(0, 500);
  const bodyText = (email.text || stripHtml(email.html) || '').slice(0, 30000);
  const atts = await readAll(email.attachments).catch(function (e) { console.error('oge: attachments failed', e && e.message); return []; });
  const attText = atts.filter(function (a) { return a.status === 'read' && a.text; }).map(function (a) { return a.text; }).join('\n\n');
  const markers = aiMarkers(bodyText + '\n' + attText.slice(0, 200000));
  const header = function (name) { const h = (email.headers || []).find(function (x) { return x.key === name; }); return h ? h.value : null; };
  const ins = await db.prepare('INSERT INTO oge_inbox (received_at, from_addr, from_name, to_addr, subject, message_id, text, links, attachments, ai_markers) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)')
    .bind(new Date().toISOString(), from, (email.from && email.from.name) || null, (email.to || []).map(function (t) { return t.address; }).join(', '),
      subject, email.messageId || header('message-id'), bodyText, JSON.stringify(linksOf(email)),
      (email.attachments || []).map(function (a) { return a.filename; }).filter(Boolean).join('; ') || null,
      markers.length ? JSON.stringify(markers) : null).run();
  const id = ins.meta && ins.meta.last_row_id;
  if (id && atts.length) {
    await db.batch(atts.map(function (a) {
      return db.prepare('INSERT INTO oge_attachments (oge_id, filename, mime, size, status, note, text, data) VALUES (?, ?, ?, ?, ?, ?, ?, ?)')
        .bind(id, a.filename, a.mime, a.size, a.status, a.note || null, a.text || null, a.bytes ? toBase64(a.bytes) : null);
    }));
  }
}
