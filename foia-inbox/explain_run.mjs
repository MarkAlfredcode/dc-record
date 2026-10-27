// Helper for explain_backfill.py: reads {requests, letters, inbox} on stdin and prints [{id, plain}] for every
// message, read exactly the way the Email Worker reads new mail (its own reply above the quoted thread, plus attachments).
import { explainReply } from './src/match.js';

let raw = '';
for await (const c of process.stdin) raw += c;
const { requests, letters, inbox } = JSON.parse(raw);
const byId = {};
requests.forEach(function (r) { r.letters = []; byId[r.id] = r; });
const extra = [];
letters.forEach(function (l) { if (l.request_id && byId[l.request_id]) byId[l.request_id].letters.push(l.letter); else extra.push(l.letter); });
const ATT = '\n\n[ATTACHMENT TEXT]\n';

const out = [];
for (const m of inbox) {
  if (m.auto) continue;
  const full = m.text || '', k = full.indexOf(ATT);
  const body = k >= 0 ? full.slice(0, k) : full, att = k >= 0 ? full.slice(k + ATT.length) : '';
  const fresh = body.split(/\n(?:From: |-{2,} ?Original Message|On .{5,80} wrote:|________________)/)[0];
  const theirs = fresh.length >= 40 ? fresh : body;
  const r = m.request_id && byId[m.request_id];
  const mine = r ? [r.summary].concat(r.letters) : requests.map(function (x) { return x.summary; }).concat(extra);
  out.push({ id: m.id, plain: explainReply(m.subject, theirs.slice(0, 12000) + '\n' + att.slice(0, 20000), mine, m.received_at) });
}
process.stdout.write(JSON.stringify(out));
