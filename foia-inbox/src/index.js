// foia-inbox: Email Worker behind foia@markalfred.news.
//
// 1. Forward to Mark's Yahoo inbox first, so mail keeps arriving even if logging fails.
// 2. Parse the message and store it in D1 table `inbox` (FOIA tracker database).
// 3. Match it to a request. What the letter says comes first (foia.gov confirmation #, the agency's tracking #,
//    the request's title, the request text quoted back), because that names the request. The sender address is
//    the fallback, and is distrusted when it can't tell requests apart: a central agency mailbox that sends the
//    acknowledgments for 30 requests. Log it in `messages`, bump the request's last activity / acknowledgment,
//    and remember the agency's tracking number so follow-ups match by it.
// 4. Flag messages that ask for an answer (still interested? clarify? certify?) so the tracker's
//    "Replies & appeals" tab and the dc-record drafting pass pick them up. Only real asks count: not the agency
//    describing its own deadline, not our own request quoted back (see asksForReply in match.js).
// 5. Read what the agency said into plain English (explainReply in match.js) and store it with the message for the
//    tracker. Two kinds of reply ring Mark's phone (ntfy): newsworthy ones (agency + reason only), and ones that
//    produce records (kind 'released': agency, case number, page count if stated). No other reply alerts.
// 6. Learn where requests should go (learnRoute): "use our portal", "this belongs with X", "we passed it to X" become a
//    row in D1 `routes` for the address we wrote to, which the tracker applies to the next request for that office.

import PostalMime from 'postal-mime';
import { readAll, toBase64 } from './attachments.js';
import { resolveRequest, newsworthy, outcomeOf, decode, stripOwn, asksForReply, explainReply, learnRoute, regDomain } from './match.js';

const FORWARD_TO = 'markdalfred10@yahoo.com';
const SELF = /@markalfred\.news$/i;
const TRACKER = 'https://markalfred.news/foia';

const AUTO = /(automatic reply|auto(-|matic)?\s?reply|out of (the )?office|autoreply)/i;
function snippetOf(text) {
  const t = (text || '').replace(/\r/g, '');
  const cut = t.split(/\n(?:From: |-{2,} ?Original Message|On .{5,80} wrote:|________________)/)[0];
  return cut.replace(/\s+/g, ' ').trim().slice(0, 400);
}

function stripHtml(html) {
  return decode((html || '').replace(/<style[\s\S]*?<\/style>|<script[\s\S]*?<\/script>/gi, ' ')
    .replace(/<br\s*\/?>|<\/p>|<\/div>/gi, '\n').replace(/<[^>]+>/g, ' '));
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

// Everything the matcher compares a message with, read once per message: the requests we have sent, the full
// letters behind them (the tracker's summary is often a one-line paraphrase of what the agency quotes back),
// letters that never became a request (they only help tell boilerplate from the request itself) and foia.gov confirmation numbers.
async function loadContext(db) {
  const reqs = (await db.prepare("SELECT id, agency, email, tracking, summary, summary_short FROM requests WHERE send_status = 'Sent' ORDER BY sent_on DESC").all()).results;
  const byId = {};
  reqs.forEach(function (r) { r.letters = []; byId[r.id] = r; });
  const extra = [];
  (await db.prepare('SELECT request_id, substr(letter, 1, 6000) AS letter FROM suggestions').all()).results.forEach(function (s) {
    if (s.request_id && byId[s.request_id]) byId[s.request_id].letters.push(s.letter); else extra.push(s.letter);
  });
  const confs = {};
  (await db.prepare("SELECT source_id, request_id FROM messages WHERE source = 'foia.gov' AND source_id IS NOT NULL").all()).results.forEach(function (c) { confs[c.source_id] = c.request_id; });
  return { reqs: reqs, byId: byId, extra: extra, confs: confs };
}

// Account mail from an agency portal (State's PAL, GovQA...) that is neither a reply nor news.
const PORTAL_NOISE = /recovery link|account is ready|reset your password|verify your (?:e-?mail|account)|activate your account/i;

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
  const filenames = (email.attachments || []).map(function (a) { return a.filename; }).filter(Boolean);

  // Which request is this? The letter first (it names the request), the sender address second.
  const ctx = await loadContext(db);
  const res = resolveRequest({ from: from, subject: subject, filenames: filenames, text: text }, ctx);
  const requestId = res.requestId, trk = res.trk;

  // What the agency asked or decided, judged on its own words: not our request quoted back, not the earlier thread under it.
  const mine = requestId ? [ctx.byId[requestId].summary].concat(ctx.byId[requestId].letters) : ctx.reqs.map(function (r) { return r.summary; }).concat(ctx.extra);
  const fresh = bodyText.split(/\n(?:From: |-{2,} ?Original Message|On .{5,80} wrote:|________________)/)[0];
  const theirs = fresh.length >= 40 ? fresh : bodyText;
  const ask = auto || rejected ? null : asksForReply(subject + '\n' + theirs.slice(0, 6000) + '\n' + attText.slice(0, 6000), mine);
  const den = auto || rejected ? null : (subject + '\n' + stripOwn(theirs.slice(0, 8000), mine) + '\n' + stripOwn(attText.slice(0, 8000), mine)).match(DENIED);
  // Our appeal of an expedited-processing denial was itself denied: that's the end of it. Don't queue another appeal.
  let expediteFinal = false;
  if (requestId && den && /expedit/i.test(den[0])) {
    try {
      expediteFinal = !!(await db.prepare("SELECT 1 FROM messages WHERE request_id = ? AND direction = 'out' AND subject LIKE '%ppeal%' AND subject LIKE '%xpedit%' LIMIT 1").bind(requestId).first());
    } catch (e) { console.error('foia-inbox: expedite check failed', e && e.message); }
  }
  const now = new Date().toISOString();
  let plain = null;
  try { plain = auto ? null : explainReply(subject, theirs.slice(0, 12000) + '\n' + attText.slice(0, 20000), mine, now); } catch (e) { console.error('foia-inbox: explain failed', e && e.message); }
  const snippet = lead || snippetOf(attText);
  const attachments = (email.attachments || []).map(function (a) { return a.filename; }).filter(Boolean).join('; ');

  const ins = await db.prepare('INSERT INTO inbox (received_at, from_addr, from_name, to_addr, subject, message_id, in_reply_to, refs, text, snippet, attachments, request_id, auto, needs_reply, reason, status, plain) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)')
    .bind(now, from, (email.from && email.from.name) || null, (email.to || []).map(function (t) { return t.address; }).join(', '),
      subject, email.messageId || header('message-id'), email.inReplyTo || header('in-reply-to'), email.references || header('references'),
      text, snippet, attachments || null, requestId, auto, expediteFinal ? 0 : ask || den ? 1 : 0, expediteFinal ? 'Expedite appeal denied: nothing further' : den ? 'Denial: ' + den[0] : ask ? ask.phrase : rejected ? 'Resubmit required' : null,
      expediteFinal || (!requestId && !ask && !den && (PORTAL_NOISE.test(subject) || (plain && plain.kind === 'account'))) ? 'handled' : 'new', plain ? JSON.stringify(plain) : null).run();
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
    const own = requestId ? ctx.byId[requestId] : null;
    const nw = newsworthy(bodyText.slice(0, 8000) + '\n' + attText.slice(0, 20000), mine.join(' '));
    if (nw) {
      await fetch('https://ntfy.sh/' + env.NTFY_TOPIC, {
        method: 'POST', headers: { Title: 'Newsworthy FOIA reply', Tags: 'rotating_light', Priority: '4', Click: TRACKER },
        body: ((own && own.agency) || (email.from && email.from.name) || from) + ' cites ' + nw.label + '.'
      }).catch(function (e) { console.error('foia-inbox: ntfy failed', e && e.message); });
    }
   }
  } catch (e) { console.error('foia-inbox: alert failed', e && e.message); }

  // Learn where this office wants requests: a portal, another office, or (a transfer) a hint. The newest letter wins,
  // except that a hint never overwrites a portal or office rule.
  try {
    const route = auto ? null : learnRoute(plain, theirs.slice(0, 12000) + '\n' + attText.slice(0, 20000), from);
    if (route) {
      const own = requestId ? ctx.byId[requestId] : null;
      const addr = String((own && own.email) || from).toLowerCase().trim();
      await db.prepare("INSERT INTO routes (addr, domain, agency, kind, portal, office, office_email, quote, inbox_id, request_id, learned_at, active) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1) " +
        "ON CONFLICT(addr) DO UPDATE SET kind = excluded.kind, portal = COALESCE(excluded.portal, routes.portal), office = excluded.office, office_email = excluded.office_email, quote = excluded.quote, inbox_id = excluded.inbox_id, request_id = excluded.request_id, learned_at = excluded.learned_at, active = 1 " +
        "WHERE excluded.kind != 'hint' OR routes.kind = 'hint'")
        .bind(addr, regDomain(addr), (own && own.agency) || (email.from && email.from.name) || null, route.kind, route.portal, route.office, route.office_email, route.quote, inboxId || null, requestId || null, now).run();
    }
  } catch (e) { console.error('foia-inbox: route step failed', e && e.message); }

  // Records produced: always ring the phone, and open the tracker's Records tab.
  if (plain && plain.kind === 'released' && env.NTFY_TOPIC) {
    const own = requestId ? ctx.byId[requestId] : null;
    const caseNo = own && own.tracking ? own.tracking : ((plain.points || []).map(function (p) { return /^Their case number: (.+)\.$/.exec(p); }).filter(Boolean)[0] || [])[1];
    const sent = (plain.points || []).filter(function (p) { return /^They sent records/.test(p); })[0];
    await fetch('https://ntfy.sh/' + env.NTFY_TOPIC, {
      method: 'POST', headers: { Title: plain.headline, Tags: 'page_facing_up', Priority: '4', Click: TRACKER + '#records' },
      body: ((own && own.agency) || (email.from && email.from.name) || from) + (caseNo ? ' (' + caseNo + ')' : '') + ': ' +
        (sent ? sent.replace(/^They sent records:?\s*/, '').replace(/\.$/, '') || 'records' : 'records') + ' produced' +
        (own ? '' : '. Not matched to a request yet') + '.'
    }).catch(function (e) { console.error('foia-inbox: records ntfy failed', e && e.message); });
  }

  if (requestId) {
    // First mail carrying the agency's own number: remember it so follow-ups match by tracking number.
    // A refusal of the request itself (Glomar, withheld in full, no records) moves the status; an expedite denial doesn't.
    const outcome = den ? outcomeOf(den[0]) : null;
    if (outcome) await db.prepare("UPDATE requests SET status = ?, status_detail = ? WHERE id = ? AND status IN ('Awaiting reply', 'Acknowledged', 'In progress', 'Extended', 'Needs clarification')").bind(outcome, snippet.slice(0, 300), requestId).run();
    else if (plain && plain.kind === 'released') await db.prepare("UPDATE requests SET status = 'Records released', status_detail = ? WHERE id = ? AND status IN ('Awaiting reply', 'Acknowledged', 'In progress', 'Extended', 'Needs clarification', 'Fee estimate')").bind(plain.headline + ': ' + snippet.slice(0, 280), requestId).run();
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
    try { await learnTracking(db, requestId, trk, ctx, inboxId); } catch (e) { console.error('foia-inbox: tracking step failed', e && e.message); }
  }
}

// The agency's own number, first seen on a letter we could match by its text. Remember it so later mail matches by
// number, and sweep up earlier mail that carried the number but nothing else ("Ref: F-2027-01273" from a central mailbox).
async function learnTracking(db, requestId, trk, ctx, inboxId) {
  const r = ctx.byId[requestId];
  if (!trk || !r || (r.tracking && String(r.tracking).trim())) return;
  if (ctx.reqs.some(function (x) { return x.id !== requestId && x.tracking && String(x.tracking).toUpperCase() === trk.toUpperCase(); })) return;  // another request already has it
  await db.prepare("UPDATE requests SET tracking = ? WHERE id = ? AND (tracking IS NULL OR tracking = '')").bind(trk, requestId).run();
  const early = (await db.prepare("SELECT id, received_at, from_addr, from_name, subject, snippet, message_id FROM inbox WHERE request_id IS NULL AND id != ? AND (subject LIKE ? OR attachments LIKE ?) LIMIT 20")
    .bind(inboxId || 0, '%' + trk + '%', '%' + trk + '%').all()).results;
  for (const e of early) {
    await db.batch([
      db.prepare('UPDATE inbox SET request_id = ? WHERE id = ?').bind(requestId, e.id),
      db.prepare('INSERT INTO messages (request_id, direction, sent_at, from_addr, subject, snippet, source, source_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)')
        .bind(requestId, 'in', String(e.received_at).slice(0, 16).replace('T', ' '), e.from_name || e.from_addr, e.subject, e.snippet, 'foia-inbox', e.message_id || null)
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
