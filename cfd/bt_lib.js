/* 差價交易「模擬戰」回測：共用的部分。
   不另外寫一份分析或規則：把 docs/cfd/index.html 裡的分析引擎（Engine）、追蹤單（Track）、模擬戰核心（Sim）、
   交易時段（Mkt）原封不動載進來用，所以網頁改了規則，回測跟著變。 */
'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm'), zlib = require('zlib');

const PAGE = path.join(__dirname, '..', 'docs', 'cfd', 'index.html');
const STEP = 300e3, M15 = 900e3, H1 = 3600e3;

/* 把網頁的程式載進一個獨立的環境（只到「行情資料」模組為止；介面與 IG 模組不需要） */
function loadPage(file) {
  const html = fs.readFileSync(file || PAGE, 'utf8'), a = html.indexOf('<script>'), b = html.lastIndexOf('</script>');
  if (a < 0 || b < 0) throw new Error('page layout changed: no script block');
  const js = html.slice(a + 8, b), cut = js.search(/\/\* =+ IG 報價/);
  if (cut < 0) throw new Error('page layout changed: module marker not found');
  const store = {};
  const box = { console, Intl, Math, JSON, Map, Set, Array, Object, Number, String, isFinite, isNaN, parseFloat, parseInt, Promise, Symbol, Error, Date, URL, AbortController,
    encodeURIComponent, decodeURIComponent, setTimeout, clearTimeout,
    localStorage: { getItem: k => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); }, removeItem: k => { delete store[k]; } },
    performance: { now: () => Date.now() }, fetch: () => Promise.reject(new Error('the replay never uses the network')) };
  box.globalThis = box; vm.createContext(box); vm.runInContext(js.slice(0, cut), box, { filename: 'docs/cfd/index.html' });
  // 設定的預設值寫在介面那一段（沒有載入），直接讀那一行
  const m = js.match(/\nconst DEF = (\{ inst: [\s\S]*?\});\n/); if (!m) throw new Error('page layout changed: DEF not found');
  const DEF = vm.runInNewContext('(' + m[1] + ')');
  for (const k of ['Engine', 'Track', 'Sim', 'Mkt', 'BT', 'Data']) if (!box[k]) throw new Error('page layout changed: ' + k + ' missing');
  return { E: box.Engine, Track: box.Track, Sim: box.Sim, Mkt: box.Mkt, BT: box.BT, Data: box.Data, DEF };
}

/* 把小週期的 K 線併成大週期（和交易所給的一樣：最後一根可能還沒收完） */
class Agg {
  constructor(ms, keep) { this.ms = ms; this.keep = keep; this.b = []; }
  add(x) {
    const k = Math.floor(x.t / this.ms) * this.ms, n = this.b.length, g = n ? this.b[n - 1] : null;
    if (g && g.t === k) { if (x.h > g.h) g.h = x.h; if (x.l < g.l) g.l = x.l; g.c = x.c; g.v += x.v || 0; }
    else if (!g || k > g.t) { this.b.push({ t: k, o: x.o, h: x.h, l: x.l, c: x.c, v: x.v || 0 }); if (n + 1 > this.keep * 2) this.b.splice(0, n + 1 - this.keep); }
  }
  last() { return this.b.length ? this.b[this.b.length - 1] : null; }
}

/* 這份 K 線是不是「沒有在動」（和網頁的 Data.dead 同一條規則） */
function dead(cd, ms, now) {
  const n = cd.length; if (!n || now - cd[n - 1].t > ms + Math.max(ms * 0.5, 20 * 60e3)) return true;
  return cd.slice(-8).filter(b => b.h === b.l).length >= 6;
}

/* 兩個商品最近 80 根 K 線的漲跌有多同步（和網頁的 corrOf 同一條算法）。a、b：[[時間, 收盤]...] */
function retMap(rc, tfMs) {
  const m = new Map(), w = 1.5 * tfMs;
  for (let i = 1; i < rc.length; i++) if (rc[i][1] > 0 && rc[i - 1][1] > 0 && rc[i][0] - rc[i - 1][0] <= w) m.set(rc[i][0], Math.log(rc[i][1] / rc[i - 1][1]));
  return m;
}
function corr(a, b, tfMs) {
  const x = retMap(a, tfMs), y = retMap(b, tfMs); let n = 0, sx = 0, sy = 0, sxx = 0, syy = 0, sxy = 0;
  for (const [t, u] of x) { const v = y.get(t); if (v == null) continue; n++; sx += u; sy += v; sxx += u * u; syy += v * v; sxy += u * v; }
  if (n < 30) return null; const vx = sxx - sx * sx / n, vy = syy - sy * sy / n; return vx > 0 && vy > 0 ? (sxy - sx * sy / n) / Math.sqrt(vx * vy) : null;
}

/* ---------- 檔案 ---------- */
const toObj = r => ({ t: r[0], o: r[1], h: r[2], l: r[3], c: r[4], v: r[5] || 0 });
const toRow = b => [b.t, b.o, b.h, b.l, b.c, b.v || 0];
function readGz(file) { return JSON.parse(zlib.gunzipSync(fs.readFileSync(file)).toString('utf8')); }
function writeGz(file, obj) { fs.mkdirSync(path.dirname(file), { recursive: true }); fs.writeFileSync(file, zlib.gzipSync(Buffer.from(JSON.stringify(obj)))); }
/* K 線整理：時間要落在格線上、由舊到新、同一時間只留一根、數字要合理 */
function tidy(rows, ms) {
  const m = new Map();
  for (const r of rows) { const t = r[0]; if (!isFinite(t) || t % ms !== 0) continue; if (![r[1], r[2], r[3], r[4]].every(v => isFinite(v) && v > 0) || r[2] < r[3]) continue; m.set(t, r); }
  return [...m.values()].sort((a, b) => a[0] - b[0]);
}

/* 只有一個檔案的 zip（Binance 的公開資料檔）：讀出那個檔案的內容 */
function unzipOne(buf) {
  let e = buf.length - 22; while (e >= 0 && buf.readUInt32LE(e) !== 0x06054b50) e--;
  if (e < 0) throw new Error('zip: no end record');
  const cd = buf.readUInt32LE(e + 16); if (buf.readUInt32LE(cd) !== 0x02014b50) throw new Error('zip: bad directory');
  const method = buf.readUInt16LE(cd + 10), csize = buf.readUInt32LE(cd + 20), lh = buf.readUInt32LE(cd + 42);
  if (buf.readUInt32LE(lh) !== 0x04034b50) throw new Error('zip: bad local header');
  const start = lh + 30 + buf.readUInt16LE(lh + 26) + buf.readUInt16LE(lh + 28), body = buf.subarray(start, start + csize);
  return method === 0 ? Buffer.from(body) : method === 8 ? zlib.inflateRawSync(body) : (() => { throw new Error('zip: method ' + method); })();
}

const tpe = t => new Date(t + 8 * H1).toISOString(); // 臺北時間（字串）
const tpeDay = t => tpe(t).slice(0, 10);

module.exports = { PAGE, STEP, M15, H1, loadPage, Agg, dead, corr, toObj, toRow, readGz, writeGz, tidy, unzipOne, tpe, tpeDay };
