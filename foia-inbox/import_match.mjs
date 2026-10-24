// Helper for import_history.py: reads {requests, confs, messages} on stdin, prints the matcher's verdict for each message.
import { matchMessage, newsworthy } from './src/match.js';

let raw = '';
for await (const c of process.stdin) raw += c;
const { requests, confs, messages } = JSON.parse(raw);
const out = messages.map(function (m) {
  const match = matchMessage({ from: m.from, subject: m.subject, filenames: m.filenames, text: m.text }, requests, confs);
  // Our own request text, quoted back by the agency, shouldn't trigger the alert.
  const own = match && requests.filter(function (r) { return r.id === match.id; }).map(function (r) { return r.summary; })[0];
  return { key: m.key, match: match, news: newsworthy(m.text, own) };
});
console.log(JSON.stringify(out));
