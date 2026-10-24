// Reads email attachments so the FOIA tracker and the drafting pass can see what an agency sent.
// PDFs (text layer), Word .docx, plain text/HTML/CSV and ZIP files (one level of nesting, e.g. the
// SecureRelease response bundles) are turned into text. Anything else, and PDFs with no text layer
// (scans), is recorded with a note so nobody assumes it was read.

import { unzipSync, strFromU8 } from 'fflate';
import { extractText, getDocumentProxy } from 'unpdf';

const MAX_TEXT = 60000;       // characters kept per attachment
const MAX_BYTES = 15 * 1024 * 1024;
const KEEP_BYTES = 1000 * 1000;           // originals up to this size are kept (base64 in D1) so an emailed appeal can re-attach them
const KEEP_EXT = ['pdf', 'docx', 'txt', 'htm', 'html', 'csv'];
const MAX_ZIP_MEMBERS = 40;
const MAX_MEMBER_BYTES = 25 * 1024 * 1024;   // uncompressed, per ZIP member (zip-bomb guard)
const MAX_TOTAL_BYTES = 60 * 1024 * 1024;    // uncompressed, per archive

function ext(name) {
  const m = /\.([a-z0-9]+)$/i.exec(name || '');
  return m ? m[1].toLowerCase() : '';
}

function cleanText(t) {
  return String(t || '').replace(/\r/g, '').replace(/[ \t]+\n/g, '\n').replace(/\n{3,}/g, '\n\n').trim();
}

function docxText(bytes) {
  const files = unzipSync(bytes, { filter: function (f) { return f.name === 'word/document.xml' && f.originalSize <= MAX_MEMBER_BYTES; } });
  const xml = files['word/document.xml'];
  if (!xml) return '';
  return cleanText(strFromU8(xml)
    .replace(/<w:tab\/>/g, '\t').replace(/<\/w:p>/g, '\n').replace(/<w:br\/>/g, '\n')
    .replace(/<[^>]+>/g, '')
    .replace(/&amp;/g, '&').replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"').replace(/&apos;/g, "'"));
}

async function pdfText(bytes) {
  const pdf = await getDocumentProxy(new Uint8Array(bytes));
  const out = await extractText(pdf, { mergePages: true });
  return cleanText(Array.isArray(out.text) ? out.text.join('\n') : out.text);
}

// Returns [{ filename, mime, size, status: 'read' | 'no_text' | 'skipped' | 'error', note, text }]
export async function readAttachment(filename, mime, content, depth) {
  const name = filename || 'unnamed';
  const bytes = content instanceof Uint8Array ? content : new Uint8Array(content || []);
  const base = { filename: name, mime: mime || null, size: bytes.length, text: '' };
  if (bytes.length <= KEEP_BYTES && KEEP_EXT.indexOf(ext(name)) >= 0) base.bytes = bytes;
  if (!bytes.length) return [Object.assign(base, { status: 'skipped', note: 'empty' })];
  if (bytes.length > MAX_BYTES) return [Object.assign(base, { status: 'skipped', note: 'over 15 MB' })];
  const e = ext(name);
  try {
    if (e === 'pdf' || /pdf/i.test(mime || '')) {
      const t = await pdfText(bytes);
      return [t.length >= 40
        ? Object.assign(base, { status: 'read', text: t.slice(0, MAX_TEXT) })
        : Object.assign(base, { status: 'no_text', note: 'PDF has no text layer (probably a scan). Needs OCR.' })];
    }
    if (e === 'docx') {
      const t = docxText(bytes);
      return [t ? Object.assign(base, { status: 'read', text: t.slice(0, MAX_TEXT) }) : Object.assign(base, { status: 'no_text', note: 'empty document' })];
    }
    if (e === 'txt' || e === 'csv' || e === 'htm' || e === 'html' || e === 'eml' || /^text\//i.test(mime || '')) {
      let t = new TextDecoder('utf-8', { fatal: false }).decode(bytes);
      if (e === 'htm' || e === 'html') t = t.replace(/<style[\s\S]*?<\/style>|<script[\s\S]*?<\/script>/gi, ' ').replace(/<br\s*\/?>|<\/p>|<\/div>/gi, '\n').replace(/<[^>]+>/g, ' ');
      return [Object.assign(base, { status: 'read', text: cleanText(t).slice(0, MAX_TEXT) })];
    }
    if (e === 'zip' || /zip/i.test(mime || '')) {
      if (depth >= 1) return [Object.assign(base, { status: 'skipped', note: 'nested zip not opened' })];
      let total = 0;
      const entries = unzipSync(bytes, {
        filter: function (f) {
          if (f.originalSize > MAX_MEMBER_BYTES || total + f.originalSize > MAX_TOTAL_BYTES) return false;
          total += f.originalSize;
          return true;
        }
      });
      const names = Object.keys(entries).filter(function (n) { return !n.endsWith('/') && !/^__MACOSX\//.test(n); }).slice(0, MAX_ZIP_MEMBERS);
      const out = [Object.assign(base, { status: 'read', text: 'ZIP containing: ' + names.join('; ') })];
      for (const n of names) {
        const inner = await readAttachment(name + ' > ' + n.split('/').pop(), null, entries[n], depth + 1);
        for (const r of inner) out.push(r);
      }
      return out;
    }
    if (e === 'p7s' || /pkcs7-signature/i.test(mime || '')) return [Object.assign(base, { status: 'skipped', note: 'email signature file' })];
    if (['png', 'jpg', 'jpeg', 'gif', 'tif', 'tiff'].indexOf(e) >= 0) return [Object.assign(base, { status: 'skipped', note: 'image (logo or scan); not read' })];
    return [Object.assign(base, { status: 'skipped', note: 'file type not read: ' + (e || mime || 'unknown') })];
  } catch (err) {
    return [Object.assign(base, { status: 'error', note: String((err && err.message) || err).slice(0, 200) })];
  }
}

export async function readAll(attachments) {
  const out = [];
  for (const a of attachments || []) {
    const rs = await readAttachment(a.filename, a.mimeType, a.content, 0);
    for (const r of rs) out.push(r);
  }
  return out;
}

// Base64 for D1 (btoa on chunks; spreading a large array into fromCharCode overflows the stack).
export function toBase64(bytes) {
  let out = '';
  for (let i = 0; i < bytes.length; i += 0x8000) out += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  return btoa(out);
}
