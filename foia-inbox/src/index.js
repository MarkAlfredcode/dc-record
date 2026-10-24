// foia-inbox: Email Worker behind foia@markalfred.news.
//
// 1. Forward to Mark's Yahoo inbox first, so mail keeps arriving even if logging fails.
// 2. Parse the message and store it in D1 table `inbox` (FOIA tracker database).
// 3. Match it to a request (sender address, then a tracking number, then sender domain, then what the
//    message and its attachments say: foia.gov confirmation #, tracking #, the request's own title),
//    log it in `messages`, bump the request's last activity / acknowledgment.
// 4. Flag messages that ask for an answer (still interested? clarify? certify?) so the
//    tracker's "Replies & appeals" tab and the dc-record drafting pass pick them up.

import PostalMime from 'postal-mime';
import { readAll, toBase64 } from './attachments.js';
import { matchMessage, extractTracking, newsworthy } from './match.js';

const FORWARD_TO = 'markdalfred10@yahoo.com';
const SELF = /@markalfred\.news$/i;
const TRACKER = 'https://markalfred.news/foia';

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

// Agency has refused, denied or closed something (including an expedited-processing denial). These go to the
// drafting pass as appeal candidates even when the letter can't be tied to a tracker request.
const DENIED = new RegExp([
  'neither confirm nor deny', 'expedited (processing|treatment)[^.]{0,80}(is |are |has been |have been |is hereby )?(denied|not granted)',
  '(deny|denying|denied|denial of) (your )?(request for )?expedit', 'your request (is|has been) (hereby )?(denied|closed)',
  'we (are )?(den(y|ying)|unable to grant)', 'no (responsive )?records (were|was) (found|located)', 'did not locate any (responsive )?records',
  'adverse determination', 'withh(old|eld|olding) (the records )?in full', '(request|it) (is|has been) administratively closed'
].join('|'), 'i');

// Agency says it won't process an emailed request at all; it has to be refiled elsewhere.
const REJECTED = new RegExp([
  'not accepting (foia )?requests (via|by|through) e-?mail', 'requests? (submitted|sent|received) (via|by) e-?mail (will|are|is) not',
  'you must submit a (freedom of information act( \\(foia\\))?|foia) request', 'does not accept (foia )?requests (via|by) e-?mail'
].join('|'), 'i');

function bareSubject(s) {
  return (s || '').toLowerCase().replace(/\[[^\]]*\]/g, ' ').replace(/^(\s*(re|fw|fwd)\s*:\s*)+/, ' ')
    .replace(/\s+/g, ' ').trim();
}

// Several requests often go to one FOIA address. Pick the one whose title is in the reply's
// subject, then one whose tracking number appears; with no unique hit, return null for Mark.
function pick(rows, subject, hay) {
  if (rows.length === 1) return rows[0].id;
  const subj = bareSubject(subject);
  const titled = rows.filter(function (r) { return r.summary_short && r.summary_short.length >= 12 && subj.includes(r.summary_short.toLowerCase()); });
  if (titled.length) return titled.sort(function (a, b) { return b.summary_short.length - a.summary_short.length; })[0].id;
  const tracked = rows.filter(function (r) { return r.tracking && String(r.tracking).length >= 4 && hay.includes(String(r.tracking).toLowerCase()); });
  return tracked.length === 1 ? tracked[0].id : null;
}

async function matchRequest(db, from, subject, text) {
  const hay = (subject + ' ' + text.slice(0, 4000)).toLowerCase();
  const byAddr = await db.prepare('SELECT id, summary_short, tracking FROM requests WHERE lower(email) = ? ORDER BY sent_on DESC').bind(from).all();
  if (byAddr.results.length) return pick(byAddr.results, subject, hay);
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
    const rows = await db.prepare("SELECT id, summary_short, tracking FROM requests WHERE lower(email) LIKE ? OR lower(email) LIKE ? ORDER BY sent_on DESC")
      .bind('%@' + d, '%.' + d).all();
    if (rows.results.length) return pick(rows.results, subject, hay);  // null if ambiguous: leave for Mark to assign
  }
  return null;
}

async function record(raw, env) {
  const db = env.FOIA_DB;
  const email = await PostalMime.parse(raw);
  const from = ((email.from && email.from.address) || '').toLowerCase();
  if (!from || SELF.test(from)) return;
  const subject = (email.subject || '').slice(0, 500);
  const bodyText = (email.text || stripHtml(email.html) || '').slice(0, 30000);
  // Read the attachments too: PDFs, Word files and ZIPs hold the agency's actual determination.
  const atts = await readAll(email.attachments).catch(function (e) { console.error('foia-inbox: attachments failed', e && e.message); return []; });
  const attText = atts.filter(function (a) { return a.status === 'read' && a.text; }).map(function (a) { return a.text; }).join('\n\n');
  const text = (bodyText + (attText ? '\n\n[ATTACHMENT TEXT]\n' + attText : '')).slice(0, 60000);
  const header = function (name) {
    const h = (email.headers || []).find(function (x) { return x.key === name; });
    return h ? h.value : null;
  };
  const auto = AUTO.test(subject) || /^auto-(replied|generated)/i.test(header('auto-submitted') || '') ? 1 : 0;
  const lead = snippetOf(bodyText);  // the agency's own words, not our quoted request below them
  const rejected = !auto && REJECTED.test(lead);
  const noAck = auto || rejected ? 1 : 0;  // a refusal isn't an acknowledgment
  const m = auto || rejected ? null : (subject + '\n' + bodyText.slice(0, 6000) + '\n' + attText.slice(0, 6000)).match(NEEDS_REPLY);
  const den = auto || rejected ? null : (subject + '\n' + bodyText.slice(0, 8000) + '\n' + attText.slice(0, 8000)).match(DENIED);
  const filenames = (email.attachments || []).map(function (a) { return a.filename; }).filter(Boolean);
  let requestId = await matchRequest(db, from, subject, text + '\n' + filenames.join(' '));
  if (!requestId) {
    // Sender alone didn't settle it (FAA staff, SecureRelease, State's ack desk). Read what the letter says.
    // Never let this step stop the message being logged.
    try {
      const reqs = (await db.prepare("SELECT id, agency, email, tracking, summary, summary_short FROM requests WHERE send_status = 'Sent'").all()).results;
      const confs = {};
      (await db.prepare("SELECT source_id, request_id FROM messages WHERE source = 'foia.gov' AND source_id IS NOT NULL").all()).results.forEach(function (c) { confs[c.source_id] = c.request_id; });
      const hit = matchMessage({ from: from, subject: subject, filenames: filenames, text: text }, reqs, confs);
      if (hit && hit.sure) requestId = hit.id;
    } catch (e) { console.error('foia-inbox: content match failed', e && e.message); }
  }
  const now = new Date().toISOString();
  const snippet = lead || snippetOf(attText);
  const attachments = (email.attachments || []).map(function (a) { return a.filename; }).filter(Boolean).join('; ');

  const ins = await db.prepare('INSERT INTO inbox (received_at, from_addr, from_name, to_addr, subject, message_id, in_reply_to, refs, text, snippet, attachments, request_id, auto, needs_reply, reason, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)')
    .bind(now, from, (email.from && email.from.name) || null, (email.to || []).map(function (t) { return t.address; }).join(', '),
      subject, email.messageId || header('message-id'), email.inReplyTo || header('in-reply-to'), email.references || header('references'),
      text, snippet, attachments || null, requestId, auto, m || den ? 1 : 0, den ? 'Denial: ' + den[0] : m ? m[0] : rejected ? 'Resubmit required' : null, 'new').run();
  const inboxId = ins.meta && ins.meta.last_row_id;
  if (inboxId && atts.length) {
    await db.batch(atts.map(function (a) {
      return db.prepare('INSERT INTO inbox_attachments (inbox_id, filename, mime, size, status, note, text, data) VALUES (?, ?, ?, ?, ?, ?, ?, ?)')
        .bind(inboxId, a.filename, a.mime, a.size, a.status, a.note || null, a.text || null, a.bytes ? toBase64(a.bytes) : null);
    }));
  }

  // A reply that is itself news: push an alert (agency + why only, never the letter) and let the tracker banner it.
  try {
   if (!auto && !rejected && env.NTFY_TOPIC) {
    const own = requestId ? await db.prepare('SELECT agency, summary FROM requests WHERE id = ?').bind(requestId).first() : null;
    const nw = newsworthy(bodyText.slice(0, 8000) + '\n' + attText.slice(0, 20000), own && own.summary);
    if (nw) {
      await fetch('https://ntfy.sh/' + env.NTFY_TOPIC, {
        method: 'POST', headers: { Title: 'Newsworthy FOIA reply', Tags: 'rotating_light', Priority: '4', Click: TRACKER },
        body: ((own && own.agency) || (email.from && email.from.name) || from) + ' cites ' + nw.label + '.'
      }).catch(function (e) { console.error('foia-inbox: ntfy failed', e && e.message); });
    }
   }
  } catch (e) { console.error('foia-inbox: alert failed', e && e.message); }

  if (requestId) {
    // First mail carrying the agency's own number: remember it so follow-ups match by tracking number.
    const trk = extractTracking(subject + ' ' + filenames.join(' '));
    if (trk) await db.prepare("UPDATE requests SET tracking = ? WHERE id = ? AND (tracking IS NULL OR tracking = '')").bind(trk, requestId).run();
    const today = new Intl.DateTimeFormat('en-CA', { timeZone: 'America/Los_Angeles' }).format(new Date());
    await db.batch([
      db.prepare('INSERT INTO messages (request_id, direction, sent_at, from_addr, subject, snippet, source, source_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)')
        .bind(requestId, 'in', now.slice(0, 16).replace('T', ' '), (email.from && email.from.name) || from, subject, snippet, 'foia-inbox', email.messageId || null),
      db.prepare("UPDATE requests SET last_activity_on = ?, last_activity = ?, acknowledged = CASE WHEN ? = 0 AND acknowledged != 'Yes' THEN 'Yes' ELSE acknowledged END, ack_on = CASE WHEN ? = 0 AND ack_on IS NULL THEN ? ELSE ack_on END, status = CASE WHEN ? = 0 AND status = 'Awaiting reply' THEN 'Acknowledged' ELSE status END WHERE id = ?")
        .bind(today, subject, noAck, noAck, today, noAck, requestId)
    ].concat(rejected ? [
      db.prepare("UPDATE requests SET status = 'Resubmit required', status_detail = ? WHERE id = ?")
        .bind('Agency would not accept the emailed request: ' + snippet.slice(0, 300), requestId)
    ] : []));
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
