// Helper for rematch.py: reads {requests, letters, extra, confs, inbox} on stdin, replays every logged message through
// the Email Worker's decision (match.js resolveRequest + asksForReply) in arrival order, and prints what would change.
//
// Tracking numbers are learned as the replay goes, exactly as the Worker learns them, and the replay runs twice so that
// early bare "Ref: F-2027-01273" mail benefits from numbers learned from the acknowledgment that arrived later.
import { resolveRequest, asksForReply } from './src/match.js';

let raw = '';
for await (const c of process.stdin) raw += c;
const { requests, letters, extra, confs, inbox } = JSON.parse(raw);

const reqs = requests.slice().sort(function (a, b) { return String(b.sent_on).localeCompare(String(a.sent_on)); }).map(function (r) { return Object.assign({}, r, { letters: [] }); });
const byId = {};
reqs.forEach(function (r) { byId[r.id] = r; });
const unlinked = (extra || []).slice();
(letters || []).forEach(function (l) { if (l.request_id && byId[l.request_id]) byId[l.request_id].letters.push(l.letter); else unlinked.push(l.letter); });
const ctx = { reqs: reqs, extra: unlinked, confs: confs || {} };

const learned = {};  // request id -> tracking learned during the replay
const taken = function (t) { return reqs.some(function (x) { return x.tracking && String(x.tracking).toUpperCase() === t.toUpperCase(); }); };
const rows = inbox.filter(function (m) { return !m.auto; }).sort(function (a, b) { return String(a.received_at).localeCompare(String(b.received_at)) || a.id - b.id; });

function resolve(m) {
  return resolveRequest({ from: m.from_addr, subject: m.subject, filenames: String(m.attachments || '').split('; ').filter(Boolean), text: m.text || '' }, ctx);
}

for (let pass = 0; pass < 2; pass++) {
  for (const m of rows) {
    const res = resolve(m);
    if (res.requestId && res.trk && !byId[res.requestId].tracking && !taken(res.trk)) {
      byId[res.requestId].tracking = res.trk;
      learned[res.requestId] = { tracking: res.trk, inbox_id: m.id };
    }
  }
}

const relink = [], unflag = [], leave = [];
for (const m of rows) {
  const res = resolve(m);
  const now = res.requestId, was = m.request_id || null;
  // Mail from before the Worker started logging (2026-10-12) came from the Yahoo history import, which left it unmatched on
  // purpose; its correspondence-log entries already exist, so re-linking it here would duplicate them.
  if (now !== was && String(m.received_at) >= '2026-10-12') {
    const old = was && byId[was];
    // Only break an existing link when the letter itself names a different case than the one it was filed under.
    const conflict = !!(old && old.tracking && res.trk && String(old.tracking).toUpperCase().replace(/[^A-Z0-9]/g, '') !== String(res.trk).toUpperCase().replace(/[^A-Z0-9]/g, ''));
    if (!was || now || conflict) relink.push({ id: m.id, from: m.from_addr, subject: m.subject, was: was, now: now, why: res.hit && res.hit.sure ? res.hit.why : now ? 'sender address, letter agrees' : 'letter names case ' + res.trk + ', not ' + (old && old.tracking) });
    else leave.push({ id: m.id, from: m.from_addr, subject: m.subject, was: was, note: 'no longer matched, but nothing contradicts the old link' });
  }
  // Mail still waiting on Mark that was flagged as needing a reply: is there a real ask in it?
  if (m.status === 'new' && m.needs_reply && !/^(Denial:|Resubmit required|Expedite appeal)/.test(m.reason || '')) {
    const own = now && byId[now] ? [byId[now].summary].concat(byId[now].letters) : reqs.map(function (r) { return r.summary; }).concat(unlinked);
    const body = String(m.text || '');
    const ask = asksForReply(m.subject + '\n' + body.slice(0, 14000), own);
    if (!ask) unflag.push({ id: m.id, from: m.from_addr, subject: m.subject, reason: m.reason });
  }
}

console.log(JSON.stringify({ learned: learned, relink: relink, unflag: unflag, leave: leave }));
