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
    new RegExp(L + '(\\d{2}-\\d{4,6}-F(?:-[A-Z]+)?)' + R), new RegExp(L + '(\\d{4}-\\d{4,6})' + R)];
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
  const add = function (id, txt) {
    if (id > 0) idx.words[id] = Math.max(idx.words[id] || 0, norm(decode(txt)).split(' ').length);
    new Set(shingles(txt)).forEach(function (k) {
      const cur = idx.get(k);
      idx.set(k, cur === undefined || cur === id ? id : 0);
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

// Sentences, one per element. Splits on sentence punctuation, colons and line breaks.
function sentencesOf(text) {
  return decode(text).replace(/\r/g, '').replace(BOILERPLATE, ' ').split(/(?<=[.!?:;])\s+|\s*\n\s*/).map(function (x) { return x.replace(/\s+/g, ' ').trim(); }).filter(Boolean);
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
  { re: /are there any (?:policy areas|keywords|custodians)|which (?:custodians|offices|employees|departments)/i }
];

// text: subject + what the agency wrote. ownTexts: our request text(s), to ignore what they quoted back.
// -> { phrase, excerpt } for the first real ask, or null. `phrase` is what the tracker shows as the reason.
export function asksForReply(text, ownTexts) {
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
      return { phrase: m[0], excerpt: s.slice(0, 300) };
    }
  }
  return null;
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
