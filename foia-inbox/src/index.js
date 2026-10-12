// foia-inbox: Email Worker behind foia@markalfred.news.
//
// 1. Forward to Mark's Yahoo inbox first, so mail keeps arriving even if logging fails.
// 2. Parse the message and store it in D1 table `inbox` (FOIA tracker database).
// 3. Match it to a request (sender address, then a tracking number, then sender domain),
//    log it in `messages`, bump the request's last activity / acknowledgment.
// 4. Flag messages that ask for an answer (still interested? clarify? certify?) so the
//    tracker's "Replies & appeals" tab and the dc-record drafting pass pick them up.

import PostalMime from 'postal-mime';

const FORWARD_TO = 'markdalfred10@yahoo.com';
const SELF = /@markalfred\.news$/i;

const AUTO = /(automatic reply|auto(-|matic)?\s?reply|out of (the )?office|autoreply)/i;
const NEEDS_REPLY = new RegExp([
  'still interested', 'wish to (proceed|continue|pursue)', 'would you like (us )?to (proceed|continue)',
  'please (confirm|clarify|narrow|specify|identify|provide|let us know|advise|respond)',
  '(need|require)s? (additional |more |some )?(clarification|information|clarifying)',
  'clarif(y|ication) (of|regarding|your)', 'narrow (your|the) (request|search|scope)',
  'certif(y|ication)', 'how you would like (us )?to proceed', 'respond (by|within|no later)',
  'will be (closed|considered withdrawn|administratively closed)', 'abandon', 'deposit', 'cost estimate',
  'are there any (policy areas|keywords|custodians)', 'which (custodians|offices|employees|departments)'
].join('|'), 'i');

function snippetOf(text) {
  const t = (text || '').replace(/\r/g, '');
  const cut = t.split(/\n(?:From: |-{2,} ?Original Message|On .{5,80} wrote:|________________)/)[0];
  return cut.replace(/\s+/g, ' ').trim().slice(0, 400);
}

function stripHtml(html) {
  return (html || '').replace(/<style[\s\S]*?<\/style>|<script[\s\S]*?<\/script>/gi, ' ')
    .replace(/<br\s*\/?>|<\/p>|<\/div>/gi, '\n').replace(/<[^>]+>/g, ' ')
    .replace(/&nbsp;/g, ' ').replace(/&amp;/g, '&').replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&#39;|&rsquo;/g, "'");
}

async function matchRequest(db, from, subject, text) {
  const byAddr = await db.prepare('SELECT id FROM requests WHERE lower(email) = ? ORDER BY sent_on DESC LIMIT 1').bind(from).first();
  if (byAddr) return byAddr.id;
  const hay = (subject + ' ' + text.slice(0, 4000)).toLowerCase();
  const tracked = await db.prepare("SELECT id, tracking FROM requests WHERE tracking IS NOT NULL AND length(tracking) >= 4").all();
  for (const r of tracked.results) {
    if (hay.includes(String(r.tracking).toLowerCase())) return r.id;
  }
  const domain = from.split('@')[1] || '';
  const parts = domain.split('.');
  for (let i = 0; i < parts.length - 1; i++) {
    const d = parts.slice(i).join('.');
    // Stop before generic suffixes, or every .gov sender would match some request.
    if (['gov', 'com', 'org', 'net', 'edu', 'us', 'mil', 'k12.ga.us', 'state.ma.us', 'state.co.us'].includes(d)) break;
    const rows = await db.prepare("SELECT id FROM requests WHERE lower(email) LIKE ? OR lower(email) LIKE ? ORDER BY sent_on DESC LIMIT 2")
      .bind('%@' + d, '%.' + d).all();
    if (rows.results.length === 1) return rows.results[0].id;
    if (rows.results.length > 1) return null;  // ambiguous: leave for Mark to assign
  }
  return null;
}

async function record(raw, env) {
  const db = env.FOIA_DB;
  const email = await PostalMime.parse(raw);
  const from = ((email.from && email.from.address) || '').toLowerCase();
  if (!from || SELF.test(from)) return;
  const subject = (email.subject || '').slice(0, 500);
  const text = (email.text || stripHtml(email.html) || '').slice(0, 30000);
  const header = function (name) {
    const h = (email.headers || []).find(function (x) { return x.key === name; });
    return h ? h.value : null;
  };
  const auto = AUTO.test(subject) || /^auto-(replied|generated)/i.test(header('auto-submitted') || '') ? 1 : 0;
  const m = auto ? null : (subject + '\n' + text.slice(0, 6000)).match(NEEDS_REPLY);
  const requestId = await matchRequest(db, from, subject, text);
  const now = new Date().toISOString();
  const snippet = snippetOf(text);
  const attachments = (email.attachments || []).map(function (a) { return a.filename; }).filter(Boolean).join('; ');

  await db.prepare('INSERT INTO inbox (received_at, from_addr, from_name, to_addr, subject, message_id, in_reply_to, refs, text, snippet, attachments, request_id, auto, needs_reply, reason, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)')
    .bind(now, from, (email.from && email.from.name) || null, (email.to || []).map(function (t) { return t.address; }).join(', '),
      subject, email.messageId || header('message-id'), email.inReplyTo || header('in-reply-to'), email.references || header('references'),
      text, snippet, attachments || null, requestId, auto, m ? 1 : 0, m ? m[0] : null, 'new').run();

  if (requestId) {
    const today = new Intl.DateTimeFormat('en-CA', { timeZone: 'America/Los_Angeles' }).format(new Date());
    await db.batch([
      db.prepare('INSERT INTO messages (request_id, direction, sent_at, from_addr, subject, snippet, source, source_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)')
        .bind(requestId, 'in', now.slice(0, 16).replace('T', ' '), (email.from && email.from.name) || from, subject, snippet, 'foia-inbox', email.messageId || null),
      db.prepare("UPDATE requests SET last_activity_on = ?, last_activity = ?, acknowledged = CASE WHEN ? = 0 AND acknowledged != 'Yes' THEN 'Yes' ELSE acknowledged END, ack_on = CASE WHEN ? = 0 AND ack_on IS NULL THEN ? ELSE ack_on END, status = CASE WHEN ? = 0 AND status = 'Awaiting reply' THEN 'Acknowledged' ELSE status END WHERE id = ?")
        .bind(today, subject, auto, auto, today, auto, requestId)
    ]);
  }
}

export default {
  async email(message, env, ctx) {
    let raw = null;
    try {
      raw = await new Response(message.raw).arrayBuffer();
    } catch (e) {
      console.error('foia-inbox: could not read message', e && e.message);
    }
    await message.forward(FORWARD_TO);
    if (raw) {
      ctx.waitUntil(record(raw, env).catch(function (e) { console.error('foia-inbox: record failed', e && (e.stack || e.message)); }));
    }
  }
};
