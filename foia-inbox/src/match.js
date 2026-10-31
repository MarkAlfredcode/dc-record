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

const ENTITIES = { amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", nbsp: ' ', sect: '§', para: '¶', ldquo: '“', rdquo: '”', lsquo: '‘', rsquo: '’', ndash: '–', mdash: '—', hellip: '…', copy: '©', reg: '®' };

// Agency letters arrive with HTML entities still in them (&sect; &ldquo; &#39;). Decode them so a quoted request lines up with ours.
export function decode(s) {
  return String(s || '').replace(/&(#x[0-9a-f]+|#\d+|[a-z]+);/gi, function (m, e) {
    if (e[0] === '#') { const n = e[1] === 'x' || e[1] === 'X' ? parseInt(e.slice(2), 16) : parseInt(e.slice(1), 10); return n > 0 && n < 0x110000 ? String.fromCodePoint(n) : m; }
    const v = ENTITIES[e.toLowerCase()];
    return v === undefined ? m : v;
  });
}

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
// "Not part of a longer token" is checked with lookarounds, not \b, because State writes F-2027-01282_U.S._Department...
export function extractTracking(s) {
  const t = String(s || '');
  const L = '(?<![A-Za-z0-9-])', R = '(?![A-Za-z0-9-])';
  const pats = [new RegExp(L + '(\\d{4}-[A-Z]{2,6}-\\d{3,6})' + R), new RegExp(L + '(F-\\d{4}-\\d{4,6})' + R), new RegExp(L + '([A-Z]{2}-\\d{4}-\\d{4,6}-F)' + R),
    new RegExp(L + '(\\d{2}-\\d{4,6}-F(?:-[A-Z]+)?)' + R), new RegExp(L + '([A-Z]{2,6}(?:-[A-Z]{2,6})?-\\d{4}-\\d{3,6})' + R),  // DOC-IOS-2027-000119, OST-2027-0027
    new RegExp(L + '(\\d{4}-\\d{4,6})' + R)];
  for (const p of pats) { const m = p.exec(t); if (m) return m[1]; }
  return null;
}

// Subject and attachment names first, then a labelled number in the letter ("Reference No: F-2027-01282",
// "Your request number is F-2027-01277"). Only labelled numbers count in the body: a bare 2027-0123 could be a ZIP+4.
export function extractTrackingDeep(subject, filenames, body) {
  const head = extractTracking(decode(subject) + ' ' + (Array.isArray(filenames) ? filenames.join(' ') : String(filenames || '')));
  if (head) return head;
  const m = /(?:reference|tracking|request|case|control)\s*(?:no\.?|number|#|id)\s*(?:is|:|#)?\s*([A-Z0-9][A-Z0-9-]*\d[A-Z0-9-]*)/i.exec(decode(body).slice(0, 8000));
  return m && m[1].indexOf('-') > 0 ? m[1].replace(/[-]+$/, '') : null;
}

// Every text a request is known by: the tracker's summary plus the full letter(s) that were sent (reqs[].letters).
// Agencies quote the full letter, and the summary is often a one-line paraphrase.
function requestTexts(r) { return [r.summary].concat(r.letters || []).filter(Boolean); }

// Runs of SHINGLE consecutive words from every request's text, keyed by a hash and mapped to the one request that
// contains them (0 when several do). Runs that several texts share (our boilerplate paragraphs, a template sent to 60
// universities) decide nothing, so they end up as 0. Word runs, not character slices, so the same paragraph lines up
// in every letter no matter where it falls.  extraTexts: other letters (dismissed or unsent suggestions) that only sharpen that comparison.
const SHINGLE = 8;
const SHARED_MAX = 100;  // a run shared by more requests than this is our boilerplate, not a template letter sent to a set of agencies

function hash(str) {  // cyrb53: a 53-bit hash is a safe Map key and makes accidental collisions negligible
  let h1 = 0xdeadbeef, h2 = 0x41c6ce57;
  for (let i = 0; i < str.length; i++) { const c = str.charCodeAt(i); h1 = Math.imul(h1 ^ c, 2654435761); h2 = Math.imul(h2 ^ c, 1597334677); }
  h1 = Math.imul(h1 ^ (h1 >>> 16), 2246822507) ^ Math.imul(h2 ^ (h2 >>> 13), 3266489909);
  h2 = Math.imul(h2 ^ (h2 >>> 16), 2246822507) ^ Math.imul(h1 ^ (h1 >>> 13), 3266489909);
  return 4294967296 * (2097151 & h2) + (h1 >>> 0);
}

function shingles(text, limit) {
  const w = norm(decode(text)).split(' ').slice(0, limit || 1500);
  const out = [];
  for (let i = 0; i + SHINGLE <= w.length; i++) out.push(hash(w.slice(i, i + SHINGLE).join(' ')));
  return out;
}

export function sliceIndex(reqs, extraTexts) {
  const idx = new Map();
  idx.words = {};  // id -> word count of that request's longest text, to judge how much of it a message quotes
  idx.shared = new Map();  // for runs set to 0: the ids that share them (null once more than SHARED_MAX do)
  const add = function (id, txt) {
    if (id > 0) idx.words[id] = Math.max(idx.words[id] || 0, norm(decode(txt)).split(' ').length);
    new Set(shingles(txt)).forEach(function (k) {
      const cur = idx.get(k);
      if (cur === undefined || cur === id) { idx.set(k, id); return; }
      idx.set(k, 0);
      let s = idx.shared.get(k);
      if (s === null) return;
      if (!s) { s = new Set([cur]); idx.shared.set(k, s); }
      s.add(id);
      if (s.size > SHARED_MAX) idx.shared.set(k, null);
    });
  };
  reqs.forEach(function (r) { requestTexts(r).forEach(function (t) { add(r.id, t); }); });
  (extraTexts || []).forEach(function (t, i) { add(-1 - i, t); });  // never a real id, so anything it shares goes to 0
  return idx;
}

// msg: { from, subject, filenames: [], text }
// reqs: [{ id, agency, email, tracking, summary, summary_short, letters? }]
// confirmations: { '3254546': requestId } from foia.gov filings
// slices: optional sliceIndex(reqs, otherLetters), or a function returning one (built only if needed), to match many messages against the same requests
// -> { id, sure, why, score } or null. `sure` means a number, the exact title or the quoted request text matched; otherwise it's a "likely" guess.
export function matchMessage(msg, reqs, confirmations, slices) {
  const raw = decode([msg.subject, (msg.filenames || []).join(' '), msg.text].join('\n'));
  const hay = raw.toLowerCase();

  const conf = {};
  (raw.match(/\b\d{6,8}\b/g) || []).forEach(function (n) { if (confirmations && confirmations[n]) conf[confirmations[n]] = n; });
  const confIds = Object.keys(conf);
  if (confIds.length === 1) return { id: +confIds[0], sure: true, why: 'foia.gov confirmation #' + conf[confIds[0]] };

  const tracked = reqs.filter(function (r) {
    const t = String(r.tracking || '').trim();
    return t.length >= 4 && new RegExp('(?<![A-Za-z0-9-])' + escapeRe(t.toLowerCase()) + '(?![A-Za-z0-9-])').test(hay);
  });
  if (tracked.length === 1) return { id: tracked[0].id, sure: true, why: 'tracking number ' + tracked[0].tracking };

  const nh = ' ' + norm(raw) + ' ';
  const titled = reqs.filter(function (r) { const t = norm(r.summary_short); return t.length >= 20 && nh.indexOf(' ' + t + ' ') >= 0; });
  if (titled.length === 1) return { id: titled[0].id, sure: true, why: 'the request title appears in the message' };

  // The letter quotes the request itself (acknowledgments, referrals and State's "Request Description" all do).
  // Slide a window over the message and count how many distinct slices unique to one request it contains.
  const idx = typeof slices === 'function' ? slices() : slices || sliceIndex(reqs);  // a function defers the (costly) index until the cheap checks above have failed
  const counts = {};
  const ks = shingles(raw, 20000);
  for (let i = 0; i < ks.length; i++) {
    const id = idx.get(ks[i]);
    if (id > 0) { counts[id] = (counts[id] || 0) + 1; i += SHINGLE - 1; }  // count separate stretches of text, not every overlapping run
  }
  const quoted = Object.keys(counts).map(function (id) { return { id: +id, n: counts[id] }; }).sort(function (a, b) { return b.n - a.n; });
  // Sure only when a real stretch of the request is quoted (5+ separate runs, or over half of a short one) and no other
  // request comes close. Three runs of legal boilerplate that two of our letters share is not a quote.
  if (quoted[0] && quoted[0].n >= 2 && (!quoted[1] || quoted[0].n >= quoted[1].n * 2)) {
    const covered = quoted[0].n * SHINGLE / Math.max(idx.words[quoted[0].id] || 1, 1);
    if (quoted[0].n >= 5 || covered >= 0.5) return { id: quoted[0].id, sure: true, why: 'the message quotes the request text', n: quoted[0].n };
  }

  // A letter we sent to several agencies at once (the congressional-correspondence-log request went to 12 departments) is
  // shared text, so the check above can't use it. Among the requests that went to the sender's own agency, though, the
  // quote can belong to only one: Commerce quoting that letter means the Commerce copy.
  const dom = rootDomain(msg.from);
  const peers = dom && idx.shared ? new Set(reqs.filter(function (r) { return rootDomain(r.email) === dom; }).map(function (r) { return r.id; })) : null;
  if (peers && peers.size) {
    const pc = {};
    for (let i = 0; i < ks.length; i++) {
      const own = idx.get(ks[i]), group = own === 0 ? idx.shared.get(ks[i]) : null;
      const ids = (own > 0 ? [own] : group ? Array.from(group) : []).filter(function (id) { return peers.has(id); });
      if (ids.length === 1) { pc[ids[0]] = (pc[ids[0]] || 0) + 1; i += SHINGLE - 1; }
    }
    const pq = Object.keys(pc).map(function (id) { return { id: +id, n: pc[id] }; }).sort(function (a, b) { return b.n - a.n; });
    if (pq[0] && pq[0].n >= 2 && (!pq[1] || pq[0].n >= pq[1].n * 2)) {
      const covered = pq[0].n * SHINGLE / Math.max(idx.words[pq[0].id] || 1, 1);
      if (pq[0].n >= 5 || covered >= 0.5) return { id: pq[0].id, sure: true, why: 'the message quotes the request text (a letter sent to several agencies; this is the one that went to ' + dom + ')', n: pq[0].n };
    }
  }

  // Weighted word overlap: rare words (FAA, Zipline) count, common ones (department) barely.
  const docs = reqs.map(function (r) {
    const base = [r.agency, r.summary_short, r.summary].join(' ');
    return { r: r, set: new Set(words(base).concat(acronyms(r.agency), acronyms(r.summary))), agency: new Set(words(r.agency).concat(acronyms(r.agency))) };
  });
  const df = {};
  docs.forEach(function (d) { d.set.forEach(function (t) { df[t] = (df[t] || 0) + 1; }); });
  const mset = new Set(words(msg.subject + ' ' + msg.text).concat(acronyms(msg.subject + '\n' + msg.text)));
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

// What a flagged reply does to the request's status. Denying *expedited processing* leaves the request open;
// only a refusal of the request itself moves it. -> 'Denied' | 'No records' | 'Closed' | null
export function outcomeOf(reason) {
  const r = String(reason || '').replace(/^Denial:\s*/i, '');
  if (!r || /expedit/i.test(r)) return null;
  if (/no (?:responsive )?records|did not locate/i.test(r)) return 'No records';
  if (/(?:is|has been) (?:hereby )?closed/i.test(r) && !/den(?:y|ied|ial)/i.test(r)) return 'Closed';
  if (/administratively closed/i.test(r)) return null;
  if (/neither confirm nor deny|withh|den(?:y|ying|ied|ial)|adverse|unable to grant|(?:is|has been) (?:hereby )?(?:denied|closed)/i.test(r)) return 'Denied';
  return null;
}

// ---- Does this letter actually ask Mark for something? -------------------------------------------------------
//
// A phrase like "respond within" or "please provide" shows up all the time in letters that want nothing from us:
//   - the agency talking about itself ("we will not be able to respond within the 20 days", "ICE's goal is to respond within")
//   - our own request quoted back (an acknowledgment, State's "Request Description:", a records request that says "Please provide all records")
//   - portal boilerplate ("--- Please respond above this line ---", "this mailbox is not monitored")
//   - an offer, not a demand ("you have the option to narrow the scope")
// So: drop the boilerplate and our own sentences first, then judge each trigger sentence by who the verb belongs to.

const BOILERPLATE = /-{2,}\s*please respond above this line\s*-{2,}|this (?:email )?(?:box|mailbox|address) is not monitored|please do not reply to this (?:email|message)/gi;

// Email clients wrap at about 72 columns, so a sentence arrives as "...a quick call\nregarding the requests." A line of 55+
// characters that stops without sentence punctuation is a wrapped line: join it to the next one (blank lines and short
// headings stay). Used only to show a sentence whole; deciding what a letter says still works line by line.
function unwrap(text) {
  return text.replace(/^([^\n]{55,})\n(?=[ \t]*\S)/gm, function (m, line) { return /[.!?:;]\s*$/.test(line) ? m : line + ' '; });
}

// s: one sentence as sentencesOf() cut it. -> the full sentence from the unwrapped letter when s was cut at a wrapped line, else s
function wholeSentence(text, s) {
  const flat = unwrap(decode(text).replace(/\r/g, '')).replace(/\s+/g, ' ');
  const i = flat.indexOf(s);
  if (i < 0) return s;
  const m = /^[\s\S]*?[.!?](?=\s|$)/.exec(flat.slice(i, i + 600));
  return m && m[0].length > s.length ? m[0] : s;
}

// Sentences, one per element. Splits on sentence punctuation, colons and line breaks.
function sentencesOf(text) {
  return decode(text).replace(/\r/g, '').replace(BOILERPLATE, ' ').split(/(?<=[.!?:;])(?<!\b(?:No|Nos|Mr|Ms|Mrs|Dr|St|Inc|Co|Corp|Jr|Sr|vs?|U\.S|Jan|Feb|Aug|Sept?|Oct|Nov|Dec)\.)\s+|\s*\n\s*/).map(function (x) { return x.replace(/\s+/g, ' ').trim(); }).filter(Boolean);
}

// ownTexts: our own request text(s) (the matched request's, or all of them when the message isn't matched yet).
// -> sentences that are not ours, joined with newlines
export function stripOwn(text, ownTexts) {
  const corpus = ' ' + norm(decode((ownTexts || []).join(' '))) + ' ';
  return sentencesOf(text).filter(function (s) {
    const n = norm(s);
    return n.length < 25 || corpus.indexOf(' ' + n + ' ') < 0;
  }).join('\n');
}

// "If you have questions, please reply", "if you need additional information": an offer to help, not a request for something.
const OPTIONAL = /\b(?:if|should) you (?:have|had|need|disagree|believe|received|are not the intended|would like to (?:request|appeal|file|receive))\b|\bfor (?:any )?(?:questions|additional information|further assistance)\b/i;

const RESPONDS = /respond\s+(?:by|within|no later)|reply\s+(?:by|within)/i;

// Is this "respond within ..." the agency describing its own deadline (no), or asking us to answer (yes)?
// Whichever subject is closest before the verb owns it: "We will review your request and respond within" is the agency's.
function respondAsk(sent, at) {
  const near = sent.slice(Math.max(0, at - 80), at);
  const last = function (re) { let i = -1, m; const g = new RegExp(re.source, 'gi'); while ((m = g.exec(near))) i = m.index + m[0].length; return i; };
  const agency = last(/\b(?:we|us|our|ours|it|this (?:office|agency|department|component)|the (?:agency|department|office|component|statute|law)|ice|fbi|goal|able|will|would|shall|statut\w*|requires? the agency|regulations?)\b/);
  const requester = last(/\b(?:please|you(?:\s+(?:must|should|need to|are (?:required|requested|asked) to|do not|don't|fail to))?|requester|failure to|fail to|unless you)\b/);
  if (requester >= 0 || agency >= 0) return requester > agency;
  return /\b(?:closed|close|withdrawn|abandon\w*|administratively|will not be processed|without further)\b/i.test(sent);  // bare "Respond within 10 days to avoid closure."
}

// Asking us for time on the phone: a courtesy word plus a call/meeting word in the same sentence.
export const CALL_ASK = /\b(?:let (?:me|us) know|please|would you|could you|are you|your availability|propose|suggest)\b[^.?!]{0,120}\b(?:available|availability|free|times?|schedule|set up|arrange|discuss|talk|speak)\b[^.?!]{0,100}\b(?:calls?|phone|meeting|conference|teleconference|zoom|teams)\b|\b(?:schedule|set up|arrange)\b[^.?!]{0,40}\b(?:calls?|meeting|teleconference)\b/i;

const ASKS = [
  { re: /still interested|wish to (?:proceed|continue|pursue)|would you like (?:us )?to (?:proceed|continue)|how you would like (?:us )?to proceed/i },
  { re: /\bplease\s+(?:confirm|clarify|narrow|specify|identify|provide|let us know|advise|respond)\b/i,
    // "Please provide all records..." is a records request (ours, echoed back); "Please provide your mailing address" is a question.
    veto: function (s) { return /please provide (?:me |us )?(?:with )?(?:all|any|copies|a copy|the following|records|documents|communications|emails?|text|calendar)\b/i.test(s) && !/\byour\b/i.test(s); } },
  { re: /(?:need|require)s?\s+(?:additional |more |some |further )?(?:clarification|information|clarifying)|clarif(?:y|ication)\s+(?:of|regarding|on)\s+(?:your|the)\s+(?:request|scope|search|parameters)|clarify\s+your|(?:requesting|asking for|seeking|seek)\s+(?:further |additional )?clarification/i,
    // "...sought clarification of your request on July 27" reports what happened; it isn't asking again.
    veto: function (s) { return /\b(?:sought|requested|asked(?: for)?|received|provided|responded)\b[^.]{0,20}clarif/i.test(s); } },
  { re: /narrow\s+(?:your|the)\s+(?:request|search|scope)/i,
    // Only when it's required of us; "you have the option / may / wish to narrow" is an offer.
    check: function (s, at) { const n = s.slice(Math.max(0, at - 70), at); return /\b(?:please|must|need to|needs to|required to|request(?:s|ed)? that you|ask(?:s|ed)? that you|asks? you to|should)\b/i.test(n) && !/\b(?:option|opportunity|may|can|could|wish|choose|if you)\b/i.test(n); } },
  { re: /certif(?:y|ication)/i,
    // Asked of us (missing, required, provide) and not our own "I certify" or certified mail.
    check: function (s) { return !/\bI (?:hereby )?certif|certif\w* (?:that )?(?:the|this) (?:above|information|statement)|certified (?:mail|copy|copies|letter)|certifying official/i.test(s) && (/(?:please|must|need|require\w*|provide|submit|send|include|attach|complete|sign)\b[^.]{0,80}certif|certif\w+[^.]{0,60}\b(?:is|are) (?:required|needed|missing|necessary)|(?:did not|does not|failed to|fail to|lack\w*|without)\b[^.]{0,50}certif/i.test(s)); } },
  { re: RESPONDS, check: respondAsk },
  { re: /will be (?:closed|considered withdrawn|administratively closed)|abandon/i,
    // A closure notice isn't a question; a closure that depends on our answer is.
    check: function (s) { return /(?:unless|if (?:we|you|this office|the agency)|absent|without|failure|fail|until)\b[^.]{0,120}(?:respon|hear|receive|repl|contact|clarif|confirm|advis)/i.test(s); } },
  { re: /deposit|cost estimate|advance payment|fee estimate/i,
    // A fee question ("agree to pay", "willing to pay up to"), not "independent government cost estimate" in a records request.
    check: function (s) { return !/government cost estimate|program cost|independent government/i.test(s) && (/\b(?:fee|fees|cost|payment|deposit)\b[^.]{0,120}\b(?:agree|authoriz|commit|willing|confirm|advance|exceed|pay|prepay)/i.test(s) || /\b(?:agree|authoriz|willing|confirm|pay|prepay)\w*[^.]{0,120}\b(?:fee|fees|cost estimate|deposit|payment)/i.test(s)); } },
  { re: /are there any (?:policy areas|keywords|custodians)|which (?:custodians|offices|employees|departments)/i },
  // "Please let me know when you might be available next week for a quick call": a person at the agency wants to talk.
  { re: CALL_ASK, veto: function (s) { return /public liaison|any aspect|\bif you\b|should you|feel free/i.test(s); } }
];

// text: subject + what the agency wrote. ownTexts: our request text(s), to ignore what they quoted back.
// -> { phrase, excerpt } for the first real ask, or null. `phrase` is what the tracker shows as the reason.
export function asksForReply(text, ownTexts, source) {  // source: the unsplit letter, so a wrapped sentence can be shown whole
  const own = ownTexts && ownTexts.length ? ' ' + norm(decode(ownTexts.join(' '))) + ' ' : '';
  const sents = sentencesOf(text);
  for (const s of sents) {
    for (const a of ASKS) {
      const m = a.re.exec(s);
      if (!m) continue;
      if (/^\W{0,3}[\u2610\u25a1]/.test(s) || /(?:^|\s)o\s[\u2610\u25a1]/.test(s)) continue;  // an unchecked box on a form is an option, not a request
      if (a !== ASKS[0] && OPTIONAL.test(s)) continue;
      if (a.veto && a.veto(s)) continue;
      if (a.check && !a.check(s, m.index)) continue;
      const n = norm(s);
      if (own && n.length >= 25 && own.indexOf(' ' + n + ' ') >= 0) continue;  // our own sentence, quoted back
      return { phrase: m[0], excerpt: wholeSentence(source || text, s).slice(0, 300) };
    }
  }
  return null;
}

// "DLAF-2027-0736 through DLAF-2027-0740": one letter about several case numbers. -> { nums: [...every number in the range] } or null
const RANGE = /(?<![A-Za-z0-9-])((?:[A-Z]{2,6}-){1,2}\d{4}-)(\d{3,6})\s*(?:through|thru|to|-|\u2013|\u2014)\s*(?:(?:[A-Z]{2,6}-){1,2}\d{4}-)?(\d{3,6})(?![A-Za-z0-9-])/;
export function caseRange(text) {
  const m = RANGE.exec(decode(text));
  if (!m) return null;
  const lo = parseInt(m[2], 10), hi = parseInt(m[3], 10);
  if (!(hi > lo) || hi - lo > 40) return null;
  const nums = [];
  for (let n = lo; n <= hi; n++) nums.push(m[1] + String(n).padStart(m[2].length, '0'));
  return { nums: nums };
}

// A message that names a range of case numbers, from an agency we have exactly that many open requests with, is about
// all of them. reqs: tracker requests (with .email, .status, .tracking). -> { ids, sure, why } or null. Never decides alone
// unless the count lines up; the tracker offers it as one click.
const OPEN = /^(?:Awaiting reply|Acknowledged|In progress|Extended|Needs clarification|Resubmit required)$/;
export function matchRange(msg, reqs) {
  const range = caseRange(msg.subject + '\n' + String(msg.text || '').slice(0, 6000));
  if (!range) return null;
  const dom = rootDomain(msg.from);
  if (!dom || GENERIC_DOMAINS.includes(dom)) return null;
  // Open requests with that agency, minus any we already know carry a different case number than the range names.
  const tail = function (t) { return String(t || '').toUpperCase().replace(/^.*?(\d{4}-\d{3,6})$/, '$1'); };
  const ids = reqs.filter(function (r) {
    return rootDomain(r.email) === dom && OPEN.test(r.status || '') &&
      (!r.tracking || range.nums.some(function (n) { return n.toUpperCase().endsWith(tail(r.tracking)); }));
  }).map(function (r) { return r.id; });
  if (!ids.length) return null;
  return { ids: ids, sure: ids.length === range.nums.length, count: range.nums.length, nums: range.nums,
    why: 'its case numbers (' + range.nums[0] + ' through ' + range.nums[range.nums.length - 1] + ': ' + range.nums.length + ' cases)' + (ids.length === range.nums.length ? ' and the ' + ids.length + ' open requests we have with that agency' : '') };
}

// ---- Matching by who sent it -----------------------------------------------------------------------------------
// The fallback when the letter names nothing (a bare "Ref: F-2027-01273"). The address is weak evidence when it can't
// tell requests apart, so senderTrusted() throws it out in the cases that went wrong before: foiastatus@state.gov
// belonged to exactly one request and so swallowed the acknowledgments of thirty others.

const GENERIC_DOMAINS = ['gov', 'com', 'org', 'net', 'edu', 'us', 'mil', 'k12.ga.us', 'state.ma.us', 'state.co.us'];

function bareSubject(s) {
  return String(s || '').toLowerCase().replace(/\[[^\]]*\]/g, ' ').replace(/^(\s*(re|fw|fwd)\s*:\s*)+/, ' ').replace(/\s+/g, ' ').trim();
}

// Several requests often go to one FOIA address. Pick the one whose title is in the reply's subject, then one
// whose tracking number appears; with no unique hit, return null for Mark.
function pick(rows, subject, hay) {
  if (rows.length === 1) return rows[0].id;
  const subj = bareSubject(subject);
  const titled = rows.filter(function (r) { return r.summary_short && r.summary_short.length >= 12 && subj.includes(r.summary_short.toLowerCase()); });
  if (titled.length) return titled.sort(function (a, b) { return b.summary_short.length - a.summary_short.length; })[0].id;
  const tracked = rows.filter(function (r) { return r.tracking && String(r.tracking).length >= 4 && hay.includes(String(r.tracking).toLowerCase()); });
  return tracked.length === 1 ? tracked[0].id : null;
}

// reqs newest first. hay: lowercase subject + start of the letter + attachment names. -> request id or null
export function matchSender(from, subject, hay, reqs) {
  const f = String(from || '').toLowerCase();
  const byAddr = reqs.filter(function (r) { return String(r.email || '').toLowerCase() === f; });
  if (byAddr.length) return pick(byAddr, subject, hay);
  for (const r of reqs) if (r.tracking && String(r.tracking).length >= 4 && hay.includes(String(r.tracking).toLowerCase())) return r.id;
  const parts = (f.split('@')[1] || '').split('.');
  for (let i = 0; i < parts.length - 1; i++) {
    const d = parts.slice(i).join('.');
    if (GENERIC_DOMAINS.includes(d)) break;  // or every .gov sender would match some request
    const rows = reqs.filter(function (r) { const e = String(r.email || '').toLowerCase(); return e.endsWith('@' + d) || e.endsWith('.' + d); });
    if (rows.length) return pick(rows, subject, hay);  // null if ambiguous: leave for Mark to assign
  }
  return null;
}

function sameTracking(a, b) { return String(a || '').toUpperCase().replace(/[^A-Z0-9]/g, '') === String(b || '').toUpperCase().replace(/[^A-Z0-9]/g, ''); }

// id: what matchSender found. trk: the tracking number the message itself carries (extractTrackingDeep).
// hit: matchMessage's verdict, if any. -> true to believe the address.
export function senderTrusted(id, from, trk, hit, reqs) {
  const r = reqs.find(function (x) { return x.id === id; });
  if (!r) return false;
  if (hit && hit.id === id) return true;  // the letter agrees with the address
  if (trk && r.tracking) return sameTracking(r.tracking, trk);  // the letter is about this case number, or about a different one
  // An agency we've filed 3+ requests with: its address says which agency wrote, not which request. Unless the letter ties it, leave it for Mark.
  const dom = rootDomain(from);
  const sharing = dom ? reqs.filter(function (x) { return rootDomain(x.email) === dom; }).length : 0;
  return sharing < 3;
}

// The whole decision, shared by the Email Worker and by the replay/rematch scripts so they can't drift apart.
// msg: { from, subject, filenames, text }.  ctx: { reqs (newest first, with .letters), extra (other letters), confs }
// -> { requestId|null, hit, trk }. The letter decides when it can; the sender address only when it can be trusted.
export function resolveRequest(msg, ctx) {
  const trk = extractTrackingDeep(msg.subject, msg.filenames, msg.text);
  let hit = null;
  try { hit = matchMessage(msg, ctx.reqs, ctx.confs, function () { return ctx.slices || (ctx.slices = sliceIndex(ctx.reqs, ctx.extra)); }); } catch (e) { hit = null; }
  let requestId = hit && hit.sure ? hit.id : null;
  if (!requestId) {
    const hay = (msg.subject + ' ' + String(msg.text || '').slice(0, 4000) + ' ' + (msg.filenames || []).join(' ')).toLowerCase();
    const sid = matchSender(msg.from, msg.subject, hay, ctx.reqs);
    if (sid && senderTrusted(sid, msg.from, trk, hit, ctx.reqs)) requestId = sid;
  }
  return { requestId: requestId, hit: hit, trk: trk };
}

// ---- What the agency said, in plain English ---------------------------------------------------------------------
//
// explainReply() reads an agency's own words (our request quoted back is stripped first) and says what it decided, in
// everyday language: got it, case number, which processing line and how long that line takes, fees, fast-track,
// records released or withheld (each exemption spelled out), passed to another office, what they need from Mark, how
// long he has to appeal. Rules, not a model, so it never says anything the letter doesn't. The tracker shows it on
// every reply; kind 'released' (records produced) also rings Mark's phone and fills the tracker's Records tab.

const EXEMPTIONS = {
  '1': 'classified national-security information', '2': 'internal personnel rules', '3': 'another law bars release',
  '4': 'company trade secrets or confidential business information', '5': 'internal deliberations or legal advice',
  '6': 'personal privacy', '7': 'law-enforcement records', '7A': 'it could interfere with an investigation or case',
  '7B': 'it could deny someone a fair trial', '7C': 'personal privacy in law-enforcement files', '7D': 'it could reveal a confidential source',
  '7E': 'law-enforcement techniques', '7F': "it could endanger someone's safety", '8': 'bank-regulation reports', '9': 'oil and gas well data'
};
const AP_MONTHS = ['Jan.', 'Feb.', 'March', 'April', 'May', 'June', 'July', 'Aug.', 'Sept.', 'Oct.', 'Nov.', 'Dec.'];
const DATE_RE = /\b((?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|Sept?(?:ember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.? \d{1,2},? \d{4}|\d{1,2}\/\d{1,2}\/\d{4})\b/;
function apDay(d) { return AP_MONTHS[d.getUTCMonth()] + ' ' + d.getUTCDate() + ', ' + d.getUTCFullYear(); }
function parseDay(s) {
  const d = new Date(String(s).replace(/\./g, '').replace(/^Sept/, 'Sep') + ' 12:00 UTC');
  return isNaN(d) ? null : d;
}
const NUM = { thirty: 30, sixty: 60, ninety: 90, ten: 10, twenty: 20, forty: 40, 'forty-five': 45 };
function count(s) { return NUM[String(s).toLowerCase()] || parseInt(s, 10); }
function clip(s, n) { s = String(s).replace(/\s+/g, ' ').trim(); return s.length > n ? s.slice(0, n - 1).replace(/\s+\S*$/, '') + '…' : s; }

// What an appeal decision is about, from how often the letter and the decision attached to it talk about each issue.
const APPEAL_ABOUT = [
  ['expedite', 'Expedited-processing', /expedit/gi],
  ['fee', 'Fee-waiver', /fee waiver|waive[rd]? (?:of )?fees?/gi],
  ['search', 'Search', /adequa\w+ of (?:the |its )?search|reasonable search|no responsive records/gi],
  ['withheld', 'Withheld-records', /exemption|withh[eo]ld|redact/gi]
];
function appealAbout(all) {
  let best = null, top = 1;
  APPEAL_ABOUT.forEach(function (a) { const n = (all.match(a[2]) || []).length; if (n > top) { top = n; best = a; } });
  return best ? { kind: best[0], label: best[1] } : null;
}

// [key, pattern, optional check(sentence)]. Every pattern is the agency speaking ("has been denied", "we located"),
// so the same words in our own request ("I request expedited processing") never count.
const SAYS = [
  ['glomar', /neither confirm nor deny|cannot confirm or deny/i],
  ['withheld', /withh(?:eld|old(?:ing)?) (?:the (?:responsive )?records |them |these records |the documents |all (?:responsive )?records )?in (?:their )?(?:full|entirety)|denied in (?:full|its entirety)|den(?:y|ying) your request in (?:full|its entirety)/i],
  ['norecords', /no (?:responsive )?records (?:were|was) (?:found|located|identified)|(?:did not|could not|were unable to|was unable to) (?:locate|find|identify) any (?:responsive )?records|does not (?:maintain|have|possess) any (?:responsive )?(?:government )?records|(?:located|found) no (?:responsive )?records|no records responsive|there (?:are|were) no (?:responsive )?records|did not identify any (?:responsive )?records/i],
  ['partial', /(?:released?|releasing|granted?|withheld|withholding|denied) in part|partial(?:ly)? (?:release|grant|denial|denied|granted)|(?:information|portions?|material|names?|pages?) (?:has|have|was|were|is|are) (?:been )?(?:redacted|withheld)|(?:with|contains?|containing) redactions/i],
  ['released', /(?:enclosed|attached|provided|available|releas\w*|produc\w*)\b[^.]{0,80}\b\d[\d,]* (?:pages?|records|documents)|(?:we are|we have|I am|this office is|the department is) (?:enclos|releas|produc|provid)\w*[^.]{0,60}\b(?:records|documents|pages)|(?:interim|final) (?:release|production)|documents? released|(?:records?|documents?) (?:have|has) been (?:released|produced)|(?:responsive|requested) (?:records|documents) (?:are|is) (?:attached|enclosed|available)|records? responsive to your request (?:are|is) (?:attached|enclosed)|please (?:find|see) (?:the )?(?:attached|enclosed)[^.]{0,40}\b(?:responsive )?(?:records|documents|production)\b|(?:attached|enclosed) (?:are|is|please find) (?:the |copies of the )?(?:responsive )?(?:records|documents)\b/i,
    function (s) { return !/copy of your (?:request|letter|appeal)|fee schedule|narrowing options|request (?:is )?(?:enclosed|attached)|(?:attached|enclosed) (?:copy of )?(?:your |my |the )?(?:original )?(?:public )?(?:records? |foia |information |open records )?request\b/i.test(s); }],
  ['appealDecided', /(?:affirm|uphold|upheld|sustain)\w* (?:the |our |its |this office's )?(?:initial |original )?(?:determination|decision|response|denial)|appeals? (?:is|are|has been|have been) (?:hereby )?(?:denied|granted|dismissed|remanded|closed)|(?:determined|decided|concluded|ordered) that (?:the |your )?(?:FOIA )?appeals?\b[^\n]{0,200}?\b(?:denied|granted|dismissed|remanded)|we (?:hereby )?(?:deny|grant|dismiss)\w* (?:the |your )?(?:FOIA )?appeals?\b|remand\w* (?:your request|the request|this matter)/i],
  ['appealAck', /(?:received|receipt of|acknowledg\w*)[^.]{0,80}\bappeal|appeal has been (?:assigned|logged|docketed|received)/i,
    function (s) { return !/(?:may|can|right to|if you)[^.]{0,40}appeal/i.test(s); }],
  ['transfer', /\b(?:transferr\w*|referr\w*|forward\w*|transfer|referral)\b(?: of)?[^.]{0,60}?\b(?:request|records|it|documents|case)\b[^.]{0,80}?\bto (?:the )?((?:[A-Z][\w&.'-]*)(?:\s+(?:[A-Z][\w&.'-]*|of|and|for|the|on))*)/,
    function (s) { return !/referred to as|(?:may|might|will|could) (?:be )?(?:need to )?(?:transfer|refer)/i.test(s); }],
  ['rejected', /not accept\w* (?:foia )?requests? (?:via|by|through) e-?mail|requests? (?:submitted|sent|received) (?:via|by) e-?mail (?:will|are|is) not|you must submit a (?:new )?(?:freedom of information act )?(?:\(foia\) |foia )?request|submit (?:your|the|a) (?:new )?(?:foia )?requests? online|requests? can be submitted online|to file a (?:foia )?request[^.]{0,80}\buses? (?:the |its )?[\w-]+ (?:request )?(?:processing )?system|resubmit (?:your|the) request|(?:need|have) to (?:visit|use) (?:our|the) (?:online )?portal|must be (?:sent|submitted) via a method/i],
  ['redirect', /(?:[Pp]lease|[Yy]ou (?:should|must|may|will need to|would need to)|[Ww]e (?:suggest|recommend)(?: that)? you|[Kk]indly|[Ii]t should be|should be) (?:re-?)?(?:submit\w*|send|sent|direct\w*|forward\w*|file|filed|address\w*) (?:(?:your|the|this|a|that) (?:records |foia |public records |open records )?requests? )?(?:directly )?to (?:the )?((?:[A-Z][\w&.'-]*)(?:\s+(?:[A-Z][\w&.'-]*|of|and|for|the|on))*)|(?:is|are) not the (?:appropriate|correct|right|proper) (?:office|agency|component|custodian|public body)/,
    function (s) { return !/(?:to (?:the |our )?)(?:online|web|portal|website|link|form|e-?mail)\b|\bif you\b|appeal/i.test(s); }],
  ['spam', /quarantined|missing one of your|caught in (?:our|a|the) (?:spam|junk)/i],
  ['closed', /(?:your |the |this )?request(?: #?[\w-]*\d[\w-]*)? (?:is|has been|was) (?:hereby )?(?:administratively )?closed|(?:we are|we have|I am) (?:now )?clos(?:ed|ing) (?:your|this|the) (?:request|case|file)/i,
    function (s) { return !/\b(?:unless|if|absent|without a response|fail\w*)\b/i.test(s); }],
  ['expediteDenied', /expedit\w*(?: processing| treatment)?[^.]{0,80}\b(?:is|has been|are|was|were) (?:hereby )?(?:denied|not granted|not warranted)|den(?:y|ied|ying|ial of) (?:your )?(?:request for )?expedit|does not (?:meet|qualify for|warrant) expedit|(?:not|in)eligible (?:for )?expedit/i],
  ['expediteGranted', /expedit\w*(?: processing| treatment)?[^.]{0,80}\b(?:is|has been|was) (?:hereby )?granted|grant(?:ed|ing) (?:your )?(?:request for )?expedit/i],
  ['feeDenied', /fee waiver[^.]{0,80}\b(?:is|has been|was) (?:hereby )?(?:denied|not granted)|den(?:y|ied|ying) your (?:request for a )?fee waiver/i],
  ['feeWaived', /fee waiver[^.]{0,80}\b(?:is|has been|was) (?:hereby )?granted|grant(?:ed|ing) your (?:request for a )?fee waiver|no fees? (?:will|shall) be (?:assessed|charged)|(?:fees?|costs?) (?:will be|shall be|are|is|have been|has been) waived|determined that no fees/i,
    function (s) { return !/waived (?:where|if|when|unless)|\$\s?25(?:\.00)? or less/i.test(s); }],
  ['feePending', /fee waiver[^.]{0,60}\b(?:is |remains )?(?:under (?:consideration|review)|pending)|(?:will|shall) (?:decide|address|rule on|make a determination (?:on|regarding)) (?:your )?(?:request for a )?fee waiver/i],
  ['media', /(?:categori[sz]ed|placed|classified|treated|qualif\w*) (?:you )?(?:as|in) (?:an? |the )?[“"]?(?:representative of the )?news[- ]media/i],
  ['feeEstimate', /(?:estimat\w*|anticipat\w*|expect\w*)[^.]{0,80}\$\s?[\d,]+(?:\.\d\d)?|\$\s?[\d,]+(?:\.\d\d)?[^.]{0,60}\b(?:estimat\w*|in fees)/i,
    function (s) { return !/\$\s?(?:25|50)(?:\.00)?\b[^.]{0,30}(?:or less|threshold|minimum)|government cost estimate|waive (?:the first )?\$|^if\b|\bif the (?:cost|fee)/i.test(s); }],
  ['track', /\b(complex|simple|expedited|fast|standard|normal) (?:processing )?(?:track|queue)\b/i],
  ['extension', /unusual circumstances|needs? additional time|(?:need|require)s? more time|(?:extend|extension of|invok\w*)[^.]{0,60}\b(?:time limit|deadline|response time|(?:10|ten) (?:additional )?(?:business |working )?days?)/i,
    function (s) { return !/\b(?:may|might|if)\b[^.]{0,60}(?:extend|invok|unusual)/i.test(s); }],
  ['eta', /estimated (?:date of )?completion(?: date)?|expect\w* (?:to )?(?:complete|respond|provide|issue|finish)\w*|anticipat\w* (?:complet|respon|provid)\w*/i,
    function (s) { return DATE_RE.test(s) && !/visiting|check the status/i.test(s); }],
  ['ack', /acknowledg\w*(?: the)? receipt|receipt of your|(?:have|has) received your|(?:we|this office|the department|the agency|OIA|our office) received your|request (?:has been|was|is) (?:logged|received|assigned)|assigned (?:it |your request )?(?:the )?(?:above|following|tracking|case|request|control|reference)[- ](?:referenced )?(?:number|no\.?)|(?:tracking|case|request|reference|control) (?:number|no\.?) (?:is|has been assigned)|this (?:email|letter) (?:is to )?(?:acknowledg|confirm)\w*|has been assigned (?:to|the)|^\W*acknowledge?ment\b|\backnowledge?ment (?:letter|of (?:your )?(?:foia )?request)|(?:request|it)\b[^.]{0,60}\b(?:was|has been|have been) received|received (?:a|your)\b[^.]{0,80}\brequest|is being processed|(?:writ\w+|this is) to acknowledge/i],
  ['duplicate', /\bduplicate\b/i]
];
// Portal account mail (sign-in links, codes, surveys), judged by the subject: nothing to read, nothing to alert.
const ACCOUNT = /recovery link|account is ready|(?:reset|create|set) (?:a |your )?(?:new )?password|verification code|confirmation code|authentication method|confirm your email|welcome to the|tell us how we did|survey/i;
// Agency case numbers extractTracking() leaves alone (DOC-IOS-2027-000122, OST-2027-0027, FOIA-2027-00191) or that only a label names ("the OMB FOIA number is 2027-054").
const CASE_NO = /\b([A-Z]{2,5}(?:-[A-Z]{2,5})?-\d{4}-\d{3,6}(?:-[A-Z]{1,2})?|\d{4}-[A-Z]{2,6}-\d{3,6}-[A-Z]{1,2})\b/;
const CASE_LABEL = /(?:foia|tracking|case|request|reference|control)\s*(?:number|no\.?|#)\s*(?:is|:)?\s*([A-Z0-9]*\d[A-Z0-9]*-[A-Z0-9-]*\d)/i;

// subject: the mail's subject. text: what the agency wrote (its fresh reply plus attachment text, not the thread below).
// ownTexts: our request text(s). received: ISO date the mail arrived (for appeal deadlines).
// -> { kind, headline, tone: bad|action|good|info, priority: 2-4 for the phone alert, points: [...], quote } or null
// The agency's own opening words: paragraphs (blank-line separated, wrapped lines rejoined), skipping greetings, our own
// quoted text, signatures and contact blocks, stopping at the sign-off. Up to two paragraphs. -> string or null
function firstWords(text, subject, ownTexts) {
  const corpus = ' ' + norm(decode((ownTexts || []).join(' '))) + ' ';
  const out = [];
  for (const raw of decode(text).replace(/\r/g, '').split(/\n[ \t\u00a0]*\n/)) {
    const para = raw.split('\n').filter(function (l) {
      const ln = norm(l);
      return !/^\s*(?:re|subject)\s*:/i.test(l) && !(ln.length >= 12 && (norm(subject).indexOf(ln) >= 0 || corpus.indexOf(' ' + ln + ' ') >= 0));
    }).join(' ').replace(/\s+/g, ' ').trim();
    if (para.length <= 30 && /^(?:v\/r|vr|very respectfully|respectfully|sincerely|regards|best|thank you|thanks),?$/i.test(para)) break;
    if (para.split(' ').length < 8 || !/[.?!:]$/.test(para) || /@|https?:|[*_=#<>{}]{3,}|\(\d{3}\)\s?\d{3}|\d{3}-\d{3}-\d{4}/.test(para)) continue;
    if (/^(?:dear|hello|hi|good (?:morning|afternoon|evening)|re:|subject:)/i.test(para) && para.length < 60) continue;
    const n = norm(para);
    if (String(subject).indexOf(para) >= 0 || (n.length >= 25 && corpus.indexOf(' ' + n + ' ') >= 0)) continue;
    out.push(para);
    if (out.length === 2 || /:$/.test(para)) break;  // a paragraph ending in a colon introduces a quote (usually our own request): stop there
  }
  return out.length ? out.join(' ') : null;
}

export function explainReply(subject, text, ownTexts, received) {
  if (ACCOUNT.test(subject || '')) return { kind: 'account', headline: 'Portal account email: nothing to do', tone: 'info', priority: 1, points: [], quote: null };
  const body = stripOwn(String(subject || '') + '\n' + String(text || ''), ownTexts || []);
  const sents = body.split('\n').filter(function (s) { return s.length > 3; });
  if (!sents.length) return null;
  const hit = {};
  for (const s of sents) {
    if (/^\W{0,3}[\u2610\u25a1]|(?:^|\s)o\s[\u2610\u25a1]|[\u2610\u25a1]\s*(?:is|are|was|the|request)\b/i.test(s)) continue;  // an unchecked box on a form (Hawaii's notice to requester) is an option, not a decision
    for (const d of SAYS) {
      if (hit[d[0]]) continue;
      const m = d[1].exec(s);
      if (m && (!d[2] || d[2](s))) hit[d[0]] = { s: s, m: m };
    }
  }
  const all = sents.join(' ');
  // Portals (NextRequest) announce every file as "a document has been released", including their own extension and
  // acknowledgment letters: that's not records. A partial release ("released in part") is.
  if (hit.released && /documents? (?:has been |have been )?released/i.test(hit.released.m[0]) && /\b(?:extension|acknowledg\w*|clarification|fee estimate|invoice|cost estimate)(?: letter| notice|\.pdf)/i.test(all)) {
    if (/extension/i.test(all) && !hit.extension) hit.extension = { s: hit.released.s, m: hit.released.m };
    delete hit.released;
  }
  if (!hit.released && hit.partial && /releas|grant|redact/i.test(hit.partial.m[0])) hit.released = hit.partial;
  const src = String(subject || '') + '\n' + String(text || '');
  const ask = asksForReply(sents.join('\n'), ownTexts || [], src);
  const pts = [];
  const add = function (t) { if (t && pts.indexOf(t) < 0) pts.push(t); };

  // The case numbers: theirs for the request, and an appeal's own number when this is about an appeal.
  const trk = extractTrackingDeep(subject, [], text) || (CASE_NO.exec(String(subject || '')) || CASE_LABEL.exec(String(text || '').slice(0, 6000)) || CASE_NO.exec(String(text || '').slice(0, 3000)) || [])[1] || null;
  const appealNo = (/appeal[^.]{0,80}?\bassigned (?:the )?(?:appeal |tracking |reference )*(?:number|no\.?)\s*([A-Z0-9]+-[A-Z0-9-]*\d)/i.exec(all) || /\b((?:A|FIA|APP|AP)-\d{2,4}-\d{3,6})\b/.exec(all) || [])[1];

  if (hit.glomar) add('They won’t say whether any records exist (a “Glomar” response). Agencies use it to protect secrets like an investigation or a source; it can be appealed.');
  if (hit.withheld) add('They’re keeping all of the records from you.');
  if (hit.norecords) add('They say they searched and found nothing.');
  if (hit.released) {
    const pages = /(\d[\d,]*) (?:pages?|records|documents)/i.exec(hit.released.s);
    add('They sent records' + (pages ? ': ' + pages[1] + ' ' + (/page/i.test(pages[0]) ? 'pages' : 'records') : '') + '.');
  }
  if (hit.partial) add('Some of it is blacked out or held back.');
  const ex = [];
  (all.match(/\(b\)\s?\((\d)\)(?:\s?\(([A-F])\))?|\bExemptions? (\d)(?:\s?\(([A-F])\))?/gi) || []).forEach(function (x) {
    const m = /(\d)\)?(?:\s?\(([A-F])\))?$/i.exec(x.replace(/\s+/g, ''));
    const k = m ? m[1] + (m[2] ? m[2].toUpperCase() : '') : null;
    if (k && EXEMPTIONS[k] && ex.indexOf(k) < 0) ex.push(k);
  });
  if (ex.length && (hit.withheld || hit.partial || hit.released || hit.glomar || hit.appealDecided))
    add('Their reasons: ' + ex.map(function (k) { return '(b)(' + k[0] + ')' + (k[1] ? '(' + k[1] + ')' : '') + ' ' + EXEMPTIONS[k]; }).join('; ') + '.');
  const aAbout = hit.appealDecided ? appealAbout(all) : null;
  if (hit.appealDecided) {
    const lost = /den|dismiss|affirm|uphe|sustain/i.test(hit.appealDecided.m[0]);
    const nums = (all.match(/\b(?:FIA|OHA|APP|AP|A)-\d{2,4}-\d{3,6}\b/g) || []).filter(function (x, i, a) { return a.indexOf(x) === i; });
    const reqs = (all.match(/\b[A-Z]{1,4}-\d{4}-\d{3,6}(?:-[A-Z])?\b/g) || []).filter(function (x, i, a) { return a.indexOf(x) === i && nums.indexOf(x) < 0; });
    const what = { expedite: 'their refusal to fast-track (expedite) your ' + (reqs.length > 1 ? 'requests' : 'request'), fee: 'their fee-waiver decision', search: 'their search for records', withheld: 'the records they withheld' }[aAbout && aAbout.kind];
    if (what) add('This was your appeal of ' + what + (nums.length ? ' (appeal' + (nums.length > 1 ? 's ' : ' ') + nums.slice(0, 3).join(', ') + ')' : '') + (reqs.length ? ', covering ' + reqs.slice(0, 4).join(', ') : '') + '. ' + (lost ? 'They denied it.' : 'They ruled in your favor.'));
    else add('They ruled on your appeal: “' + clip(hit.appealDecided.s, 200) + '”');
    // Their reason: the decision's own conclusion ("Therefore, we find that ..."), last one before the order.
    const concl = (all.match(/(?:Therefore|Accordingly|Consequently|Thus|Because of this|For these reasons),? (?:we|OHA|this office)\s+(?:find|found|conclude|determine)\w*[^.]{15,320}\./gi) || []).pop();
    if (lost && aAbout && aAbout.kind === 'expedite' && /current exigency|breaking news|compelling need/i.test(all))
      add('Their reason: they said you didn’t prove a “compelling need.” They look for a breaking story or evidence that the public or news media are already focused on the subject, and they found none in your appeal.');
    else if (lost && concl) add('Their reason: “' + clip(concl, 260) + '”');
    if (/final (?:order|decision|agency action)/i.test(all) && /judicial review/i.test(all))
      add('This is their final answer. Your options now are a lawsuit in federal court (judicial review)' + (/OGIS|Office of Government Information Services/i.test(all) ? ', or free mediation through OGIS (the federal FOIA ombudsman)' : '') + '.');
  }
  if (hit.appealAck) add('They got your appeal' + (appealNo ? ' (appeal number ' + appealNo + ')' : '') + '.');
  if (ask) {  // every distinct thing they ask for, up to three (ICE's clarification letters ask for dates, places and offices at once)
    const asks = [];
    sents.forEach(function (s) { const a = asks.length < 3 && asksForReply(s, ownTexts || [], src); if (a && asks.indexOf(a.excerpt) < 0) asks.push(a.excerpt); });
    if (!asks.length) asks.push(ask.excerpt);
    add('They need something from you: ' + asks.map(function (x) { return '“' + clip(x, 220) + '”'; }).join(' '));
  }
  if (ask) {
    const close = /(?:clos\w*|withdrawn|administratively)[^.]{0,120}?\b(\d+|ten|twenty|thirty) (?:business |working |calendar )?days|\b(\d+|ten|twenty|thirty) (?:business |working |calendar )?days[^.]{0,120}?(?:clos\w*|withdrawn)/i.exec(all);
    if (close) add('If you don’t answer within ' + count(close[1] || close[2]) + ' days, they’ll close the request.');
  }
  if (hit.rejected) add('They don’t take requests by email. It has to be filed again on their website.');
  if (hit.redirect) add(hit.redirect.m[1] ? 'They say this belongs with ' + hit.redirect.m[1].replace(/\s+(?:of|and|for|the|on)$/i, '') + ': it has to be sent there instead.' : 'They say they’re the wrong office for this request.');
  if (hit.spam) add('They think one of your emails may have been caught in their spam filter and want to make sure nothing was lost.');
  if (hit.transfer) {
    const tm = SAYS.find(function (d) { return d[0] === 'transfer'; })[1].exec(all.slice(Math.max(0, all.indexOf(hit.transfer.s.slice(0, 40)))));
    const dest = ((tm || hit.transfer.m)[1]).replace(/\s+(?:of|and|for|the|on)$/i, '');
    add('They passed ' + (/portion|part of/i.test(hit.transfer.s) ? 'part of it' : 'it') + ' to ' + dest + ', which will answer you directly.');
  }
  if (hit.duplicate) add('They say this repeats another request of yours, so they’re handling it under that one.');
  if (hit.closed && !hit.norecords && !hit.withheld && !hit.duplicate) add('They closed the request.');
  if (hit.feeEstimate) add('They estimate the fees at ' + /\$\s?[\d,]+(?:\.\d\d)?/.exec(hit.feeEstimate.s)[0].replace(/\s/g, '') + '.');
  if (hit.feeDenied) add('Fee waiver denied: you may be charged.');
  else if (hit.feeWaived) add('No fees.');
  else if (hit.feePending) add('They haven’t decided on your fee waiver yet.');
  if (hit.media && !hit.feeWaived) add('They put you in the news-media fee group, the cheapest one.');
  if (hit.expediteDenied && !(hit.appealDecided && aAbout && aAbout.kind === 'expedite')) add('Your request to jump the line (expedited processing) was denied.');
  if (hit.expediteGranted) add('Your request to jump the line (expedited processing) was granted.');
  if (hit.track) {
    const line = hit.track.m[1].toLowerCase();
    const avg = /average processing time[^.]{0,60}?\b(\d+) (?:business |working )?days for simple[^.]{0,60}?\b(\d+) (?:business |working )?days for complex/i.exec(all);
    if (line === 'complex') add('It’s in the slow line for big requests' + (avg ? '; that line averaged ' + avg[2] + ' days last year' : '') + '.');
    else if (line === 'simple') add('It’s in the faster line for small requests' + (avg ? '; that line averaged ' + avg[1] + ' days last year' : '') + '.');
  }
  if (hit.extension) add('They’re taking extra time, which the law allows for big or multi-office searches.');
  if (hit.eta) { const d = parseDay(DATE_RE.exec(hit.eta.s)[1]); if (d) add('They expect to finish by ' + apDay(d) + '.'); }
  if (!ask && (hit.track || hit.extension) && /narrow(?:ing)? (?:options|the scope|your request)/i.test(all)) add('They’d move it faster if you narrowed it.');

  // How long to appeal, when there's something to appeal.
  const adverse = hit.glomar || hit.withheld || hit.norecords || hit.partial || hit.expediteDenied || hit.feeDenied || hit.closed ||
    (hit.appealDecided && /den|dismiss|affirm|uphe|sustain/i.test(hit.appealDecided.m[0]));
  if (adverse) {
    const ap = /appeal[^]{0,260}?\b(?:within|no later than) (\d+|thirty|sixty|ninety|forty-five) (calendar[- ]|business |working )?days/i.exec(all);
    const to = /(?<![\w.+-])([\w.+-]*appeal[\w.+-]*@[\w-]+(?:\.[\w-]+)+)/i.exec(all) || /appeal[^.]{0,160}?(?<![\w.+-])([\w.+-]+@[\w-]+(?:\.[\w-]+)+)/i.exec(all);
    if (ap) {
      const n = count(ap[1]), biz = /business|working/i.test(ap[2] || '');
      const start = received ? new Date(String(received).slice(0, 10) + 'T12:00:00Z') : null;
      const by = start && !isNaN(start) ? new Date(start.getTime() + (biz ? Math.ceil(n * 7 / 5) : n) * 86400000) : null;
      add('You have ' + n + (biz ? ' business' : '') + ' days to appeal' + (by ? ' (by ' + (biz ? 'about ' : '') + apDay(by) + ')' : '') + (to ? ', by email to ' + to[1].replace(/\.$/, '') : '') + '.');
    }
  }
  const range = caseRange(String(subject || '') + '\n' + String(text || '').slice(0, 6000));
  if (range) add('Their case numbers: ' + range.nums[0] + ' through ' + range.nums[range.nums.length - 1] + ' (' + range.nums.length + ' requests).');
  else if (trk && !hit.appealAck && !hit.appealDecided) add('Their case number: ' + trk + '.');

  // The headline and how loudly to ring the phone: the most consequential thing the letter says.
  let kind, headline, tone, priority = 3;
  if (hit.glomar) { kind = 'glomar'; headline = 'Won’t confirm or deny the records exist'; tone = 'bad'; priority = 4; }
  else if (hit.withheld) { kind = 'withheld'; headline = 'Records withheld in full'; tone = 'bad'; priority = 4; }
  else if (hit.norecords) { kind = 'norecords'; headline = 'No records found'; tone = 'bad'; priority = 4; }
  else if (hit.released) { kind = 'released'; headline = hit.partial ? 'Records released, some withheld' : 'Records released'; tone = 'good'; priority = 4; }
  else if (hit.appealDecided) { const den = /den|dismiss|affirm|uphe|sustain/i.test(hit.appealDecided.m[0]); kind = 'appealDecided'; headline = den ? (aAbout ? aAbout.label + ' appeal denied' : 'Appeal denied') : /remand/i.test(hit.appealDecided.m[0]) ? 'Appeal won: sent back for a new search' : 'Appeal decided'; tone = den ? 'bad' : 'good'; priority = 4; }
  else if (ask) { kind = 'ask'; headline = CALL_ASK.test(ask.excerpt) ? 'They want to set up a call with you' : 'They need an answer from you'; tone = 'action'; priority = 4; }
  else if (hit.redirect && !hit.transfer) { kind = 'redirect'; headline = 'Wrong office' + (hit.redirect.m[1] ? ': send it to ' + hit.redirect.m[1].replace(/\s+(?:of|and|for|the|on)$/i, '') : ''); tone = 'action'; priority = 4; }
  else if (hit.rejected) { kind = 'rejected'; headline = 'Won’t take it by email: refile online'; tone = 'action'; priority = 4; }
  else if (hit.feeEstimate) { kind = 'fees'; headline = 'Fee estimate'; tone = 'action'; priority = 4; }
  else if (hit.spam) { kind = 'spam'; headline = 'An email of yours may be lost'; tone = 'action'; priority = 4; }
  else if (hit.duplicate) { kind = 'duplicate'; headline = 'Closed as a duplicate'; tone = 'info'; }
  else if (hit.closed) { kind = 'closed'; headline = 'Request closed'; tone = 'bad'; priority = 4; }
  else if (hit.transfer) { kind = 'transfer'; headline = 'Passed to another office'; tone = 'info'; }
  else if (hit.appealAck) { kind = 'appealAck'; headline = 'Appeal received'; tone = 'info'; }
  else if (hit.ack) {
    kind = 'ack'; tone = 'info'; priority = 2;
    headline = 'Request received' + (hit.expediteGranted ? '; fast-track granted' : hit.expediteDenied ? '; fast-track denied' : '') + (hit.feeDenied ? '; fee waiver denied' : '');
    if (hit.expediteGranted || hit.feeDenied) priority = 3;
  }
  else if (hit.expediteDenied || hit.expediteGranted || hit.feeDenied || hit.feeWaived || hit.extension || hit.eta || hit.track) { kind = 'update'; headline = 'Status update'; tone = hit.expediteDenied || hit.feeDenied ? 'bad' : 'info'; priority = 2; }
  else { kind = 'other'; headline = 'Message from the agency'; tone = 'info'; priority = 3; }

  // In their own words: the sentence behind the headline, for anything beyond a routine acknowledgment.
  const lead = { glomar: hit.glomar, withheld: hit.withheld, norecords: hit.norecords, released: hit.released, appealDecided: hit.appealDecided,
    rejected: hit.rejected, redirect: hit.redirect, fees: hit.feeEstimate, spam: hit.spam, closed: hit.closed, transfer: hit.transfer, duplicate: hit.duplicate }[kind];
  // The whole sentence from the running text, so one broken across lines in a PDF reads whole.
  const whole = function (s) { const i = all.indexOf(s); if (i < 0) return s; const m = /^[\s\S]*?[.!?](?=\s|$)/.exec(all.slice(i, i + 400)); return m && m[0].length >= s.length ? m[0] : s; };
  let quote = lead ? whole(lead.s) : null;
  // Portal notices (NextRequest) wrap the agency's words in a banner: "...along with the following message: <their words>".
  const wrapped = /along with the following message:\s*([^*]{20,600}?)(?=\s*\*\s|\s*Document links|$)/i.exec(all);
  if (wrapped && (!quote || /\*{4,}|^\[/.test(quote))) quote = wrapped[1];
  // Nothing in the letter matched a known pattern: say what it does say. The first real paragraphs, rejoined where the
  // email client wrapped a line mid-sentence, so the card never ends on "...through".
  if (kind === 'other') quote = firstWords(String(text || ''), String(subject || ''), ownTexts || []) ||
    sents.find(function (s) { return s.length >= 40 && /[a-z]{3,}.*[a-z]{3,}/i.test(s) && !/[*_=]{4}/.test(s) && String(subject || '').indexOf(s) < 0 && !/[<>{}#]|https?:|^(?:dear|hello|hi|good (?:morning|afternoon|evening)|re:|subject:)/i.test(s); }) || null;
  return { kind: kind, headline: headline, tone: tone, priority: priority, points: pts.slice(0, 8), quote: quote ? clip(quote, kind === 'other' ? 420 : 300) : null };
}


// ---- Where requests should go, learned from replies ----------------------------------------------------------------
//
// learnRoute() turns an agency's "we don't take email, use our portal", "this belongs with X" or "we passed it to X"
// into a routing rule for the address we wrote to. kind 'portal' (file at portal), 'office' (send to another office:
// office name, plus office_email when the letter gives one), 'hint' (a transfer: the request was passed on, so
// another request on the same subject may belong there too; shown, never applied on its own).
// plain: explainReply()'s reading. fromAddr: the sender. -> { kind, portal, office, office_email, quote, requires } or null

const NOT_PORTAL = /aka\.ms|safelinks|sam\.gov|efile\.fara\.gov|usaspending|fpds|federalregister|congress\.gov|twitter|facebook|linkedin|instagram|youtube|civicplus\.help|\/help\b|kb_article|unsubscribe|settings\?/i;
export function regDomain(addr) {
  const host = String(addr || '').toLowerCase().replace(/^.*@/, '').replace(/^https?:\/\//, '').split(/[/?#:]/)[0];
  const parts = host.split('.').filter(Boolean);
  return parts.slice(parts[parts.length - 1] === 'us' ? -3 : -2).join('.');
}
function bestPortal(text, fromAddr) {
  const dom = regDomain(fromAddr);
  const urls = (String(text || '').match(/https?:\/\/[^\s<>()\]"'|]+/g) || []).map(function (u) { return u.replace(/[.,;:)\]]+$/, ''); });
  let best = null, top = 0;
  urls.forEach(function (u) {
    if (NOT_PORTAL.test(u)) return;
    let sc = 0;
    if (/submit|request|portal|nextrequest|govqa|justfoia|foiaxpress|\bpal\.|mycusthelp|securerelease|foiaonline|records-?request|make-?request|open-?records|\/foia/i.test(u)) sc += 3;
    if (dom && regDomain(u) === dom) sc += 2;
    if (/(?:^|\/\/)(?:www\.)?foia\.gov\/?$/i.test(u)) sc += 1;
    if (/policy|\/aps\/|overview|faq|privacy/i.test(u)) sc -= 2;
    if (sc > top || (sc === top && best && u.length > best.length && sc > 0)) { top = sc; best = u; }
  });
  return top > 0 ? best : null;
}
// What an office says every request must carry ("In your request, you must include a statement that you are willing to pay
// for professional fees for search and review"), from the same letter that sends us to its portal or another office.
const REQUIRES = /\b(?:you must|requests? must|you will need to|you need to|(?:please )?be sure to) include (?:a |an |the |your )?[^.]{5,220}\./i;
function requirementOf(text) { const m = REQUIRES.exec(String(text || '').replace(/\s+/g, ' ')); return m ? clip(m[0].replace(/^./, function (c) { return c.toUpperCase(); }), 300) : null; }

// The line a letter gets for a learned requirement, or null when there's no standard wording for it (the tracker still shows
// the requirement). A fee statement agrees to the fees of our category, so it doesn't give up news-media status.
const FEE_LINE = 'I am willing to pay fees for search, review and duplication to the extent they apply to my fee category. As a representative of the news media, I understand that only duplication beyond the first 100 pages is chargeable.';
export function requirementLine(requires) { return /\b(?:pay|fees?)\b/i.test(requires || '') ? FEE_LINE : null; }

// letter with the requirement's line added after its fee paragraph (else before the signature), once.
export function withRequirement(letter, requires) {
  const line = requirementLine(requires), l = String(letter || '');
  if (!line || /willing to pay fees for search/i.test(l)) return l;
  const fee = /(please contact me before incurring any (?:fees )?above \$\d+\.)/i;
  if (fee.test(l)) return l.replace(fee, '$1 ' + line);
  const sig = /\n\s*(?:Sincerely|Thank you|Respectfully|Best),?\s*\n/i.exec(l);
  return sig ? l.slice(0, sig.index) + '\n\n' + line + l.slice(sig.index) : l + '\n\n' + line;
}

export function learnRoute(plain, text, fromAddr) {
  if (!plain) return null;
  const t = String(text || '');
  if (plain.kind === 'rejected') return { kind: 'portal', portal: bestPortal(t, fromAddr), office: null, office_email: null, quote: plain.quote, requires: requirementOf(t) };
  if (plain.kind === 'redirect') {
    const m = /^Wrong office: send it to (.+)$/.exec(plain.headline) ||
      /(?:[Pp]lease|[Yy]ou (?:should|must|may|will need to)|[Kk]indly) (?:re-?)?(?:submit|send|direct|forward|file|address) (?:your |the |this )?(?:records |foia )?request (?:directly )?to (?:the )?((?:[A-Z][\w&.'-]*)(?:\s+(?:[A-Z][\w&.'-]*|of|and|for|the|on))*)/.exec(t);
    const own = /@markalfred\.news$|@yahoo\.com$|^image\d+|\.(?:png|jpe?g|gif)@/i;
    const near = plain.quote ? t.slice(Math.max(0, t.indexOf(plain.quote.slice(0, 40))), Math.max(0, t.indexOf(plain.quote.slice(0, 40))) + 600) : '';
    const em = (near.match(/[\w.+-]+@[\w-]+(?:\.[\w-]+)+/g) || []).map(function (x) { return x.replace(/\.$/, ''); })
      .filter(function (x) { return !own.test(x) && x.toLowerCase() !== String(fromAddr || '').toLowerCase(); })[0] || null;
    return { kind: 'office', portal: bestPortal(near, fromAddr), office: m ? m[1].replace(/\s+(?:of|and|for|the|on)$/i, '') : null, office_email: em, quote: plain.quote, requires: requirementOf(t) };
  }
  if (plain.kind === 'transfer') {
    const p = (plain.points || []).map(function (x) { return /^They passed (?:part of it|it) to (.+), which will answer you directly\.$/.exec(x); }).filter(Boolean)[0];
    return p ? { kind: 'hint', portal: null, office: p[1], office_email: null, quote: plain.quote } : null;
  }
  return null;
}
