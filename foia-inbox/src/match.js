// Matching agency mail to tracker requests by what the message says, not just who sent it.
//
// Pure functions, no I/O. Two copies must stay identical:
//   dc-record  foia-inbox/src/match.js          (the Email Worker, auto-matches only when `sure`)
//   markalfredsite  functions/api/foia-match.js (the tracker API, offers a "likely match" for mail the Worker left alone)
//
// A message is the subject, the attachment filenames and the body plus attachment text. Agencies repeat the
// request title, their own tracking number or the foia.gov confirmation number in the attached letter, so
// reading the attachment is what ties a sender like judy.a.shipp@faa.gov to the request it answers.

const STOP = new Set(('the and for that with from this have has had are was were will would should could their there which about into your you our not ' +
  'but all any can may per its also than then them these those been being such other more most some when where what who whom how why out off over under ' +
  'again further once only own same too very just dear sincerely thank thanks regards please attached attach enclosed email mail letter message sent ' +
  'received receipt acknowledging acknowledgment acknowledgement acknowledge freedom information act foia request requests requested records record ' +
  'regarding relating related copy mark alfred washington sun reporter image png jpg pdf').split(' '));

function words(s) {
  return (String(s || '').toLowerCase().match(/[a-z0-9]{3,}/g) || []).filter(function (w) { return !STOP.has(w) && !/^\d+$/.test(w); });
}

// "Federal Aviation Administration" -> faa, "Department of Transportation" -> dot
function acronyms(s) {
  const out = [];
  const re = /\b[A-Z][a-z]+(?:\s+(?:(?:of|for|the|and)\s+)?[A-Z][a-z]+){1,5}/g;
  let m;
  while ((m = re.exec(String(s || '')))) {
    const a = m[0].split(/\s+/).filter(function (w) { return /^[A-Z]/.test(w); }).map(function (w) { return w[0]; }).join('').toLowerCase();
    if (a.length >= 3) out.push(a);
  }
  return out;
}

function norm(s) { return String(s || '').toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim(); }

function rootDomain(email) {
  const d = String(email || '').toLowerCase().split('@')[1] || '';
  return d.split('.').slice(-2).join('.');
}

function escapeRe(s) { return String(s).replace(/[.*+?^${}()|[\]\\]/g, '\\$&'); }

// Agency tracking numbers in the shapes we see: 2027-ICFO-01652, F-2027-01276, HQ-2027-00163-F, 27-00084-F-HQ, 2027-00451.
export function extractTracking(s) {
  const t = String(s || '');
  const pats = [/\b(\d{4}-[A-Z]{2,6}-\d{3,6})\b/, /\b(F-\d{4}-\d{4,6})\b/, /\b([A-Z]{2}-\d{4}-\d{4,6}-F)\b/, /\b(\d{2}-\d{4,6}-F(?:-[A-Z]+)?)\b/, /\b(\d{4}-\d{4,6})\b/];
  for (const p of pats) { const m = p.exec(t); if (m) return m[1]; }
  return null;
}

// msg: { from, subject, filenames: [], text }
// reqs: [{ id, agency, email, tracking, summary, summary_short }]
// confirmations: { '3254546': requestId } from foia.gov filings
// -> { id, sure, why, score } or null. `sure` means a number or the exact title matched; otherwise it's a "likely" guess.
export function matchMessage(msg, reqs, confirmations) {
  const raw = [msg.subject, (msg.filenames || []).join(' '), msg.text].join('\n');
  const hay = raw.toLowerCase();

  const conf = {};
  (raw.match(/\b\d{6,8}\b/g) || []).forEach(function (n) { if (confirmations && confirmations[n]) conf[confirmations[n]] = n; });
  const confIds = Object.keys(conf);
  if (confIds.length === 1) return { id: +confIds[0], sure: true, why: 'foia.gov confirmation #' + conf[confIds[0]] };

  const tracked = reqs.filter(function (r) {
    const t = String(r.tracking || '').trim();
    return t.length >= 4 && new RegExp('(?<![\\w-])' + escapeRe(t.toLowerCase()) + '(?![\\w-])').test(hay);
  });
  if (tracked.length === 1) return { id: tracked[0].id, sure: true, why: 'tracking number ' + tracked[0].tracking };

  const nh = ' ' + norm(raw) + ' ';
  const titled = reqs.filter(function (r) { const t = norm(r.summary_short); return t.length >= 20 && nh.indexOf(' ' + t + ' ') >= 0; });
  if (titled.length === 1) return { id: titled[0].id, sure: true, why: 'the request title appears in the message' };

  // The letter quotes the request itself (most acknowledgments and referrals do): count 80-character slices of
  // each request's text found in the message. Boilerplate every request shares hits many requests and decides nothing.
  const quoted = reqs.map(function (r) {
    const t = norm(r.summary);
    let n = 0;
    for (let i = 0; i + 80 <= Math.min(t.length, 700); i += 50) if (nh.indexOf(t.slice(i, i + 80)) >= 0) n++;
    return { id: r.id, n: n };
  }).sort(function (a, b) { return b.n - a.n; });
  if (quoted[0] && quoted[0].n >= 2 && (!quoted[1] || quoted[0].n > quoted[1].n)) return { id: quoted[0].id, sure: true, why: 'the message quotes the request text' };

  // Weighted word overlap: rare words (FAA, Zipline) count, common ones (department) barely.
  const docs = reqs.map(function (r) {
    const base = [r.agency, r.summary_short, r.summary].join(' ');
    return { r: r, set: new Set(words(base).concat(acronyms(r.agency), acronyms(r.summary))), agency: new Set(words(r.agency).concat(acronyms(r.agency))) };
  });
  const df = {};
  docs.forEach(function (d) { d.set.forEach(function (t) { df[t] = (df[t] || 0) + 1; }); });
  const mset = new Set(words(msg.subject + ' ' + msg.text).concat(acronyms(msg.subject + '\n' + msg.text)));
  const dom = rootDomain(msg.from);
  const scored = docs.map(function (d) {
    let s = 0, hits = [], agencyHit = false;
    d.set.forEach(function (t) { if (mset.has(t)) { const w = Math.log((reqs.length + 1) / (df[t] + 1)); if (d.agency.has(t) && w >= 2.5) agencyHit = true; /* generic words (office, department) don't name an agency */ s += d.agency.has(t) ? w * 2 : w; hits.push(t); } });
    if (dom && rootDomain(d.r.email) === dom) { s += 2; agencyHit = true; }
    return { id: d.r.id, score: agencyHit ? s : 0, hits: hits };  // a guess must at least name the agency
  }).sort(function (a, b) { return b.score - a.score; });
  const top = scored[0], next = scored[1];
  if (top && top.score >= 10 && top.hits.length >= 3 && (!next || top.score >= next.score * 1.25)) {
    return { id: top.id, sure: false, score: top.score, why: 'shares ' + top.hits.slice(0, 5).join(', ') };
  }
  return null;
}

// Replies that are a story in themselves ("an ongoing criminal law enforcement investigation", a Glomar
// response, records that were deleted). The Worker pushes an alert for these and the tracker pins a banner.
const NEWSWORTHY = [
  ['an investigation or law-enforcement hold-up', /(?<!\bnot\s)(?<!\bno\s)(?:ongoing|active|pending|open|continuing)\s+(?:criminal\s+|civil\s+)?(?:law[- ]enforcement\s+)?(?:investigation|proceeding|prosecution|inquiry)|\(b\)\(7\)\(a\)|exemption 7\s*\(?a\)?/i],
  ['a Glomar response (neither confirm nor deny)', /neither confirm nor deny|cannot confirm or deny/i],
  ['a grand jury or sealed matter', /grand jury|under seal|sealed by (?:a |the )?court/i],
  ['classified or national-security grounds', /classified (?:information|national security)|national security (?:information|interests?)/i],
  ['an executive or presidential privilege claim', /executive privilege|presidential communications privilege/i],
  ['a referral to DOJ, the FBI or an inspector general', /referred (?:your request |the matter |this matter |these records )?to (?:the )?(?:department of justice|doj|fbi|inspector general|office of inspector general|special counsel)/i],
  ['records destroyed, deleted or never kept', /(?:records|messages|texts?|emails?)\s+(?:were|was|have been|has been)\s+(?:destroyed|deleted|lost|purged|not (?:retained|preserved|saved))|no longer (?:exist|available|retained)|auto-?delet\w+|disappearing messages/i]
];

// text: what the agency wrote (body + attachments). ownText: our own request, so a phrase we wrote
// and they quoted back doesn't count. -> { label, phrase, excerpt } or null
export function newsworthy(text, ownText) {
  const t = String(text || ''), own = norm(ownText);
  for (const n of NEWSWORTHY) {
    const g = new RegExp(n[1].source, 'gi');
    let m;
    while ((m = g.exec(t))) {
      if (own && own.indexOf(norm(m[0])) >= 0) continue;
      return { label: n[0], phrase: m[0], excerpt: t.slice(Math.max(0, m.index - 160), m.index + m[0].length + 160).replace(/\s+/g, ' ').trim() };
    }
  }
  return null;
}
