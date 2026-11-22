/* 差價交易「模擬戰」回測：抓歷史 K 線（5 分 K 與 1 小時 K）。只在 GitHub Actions 上跑（寫程式的沙箱連不到行情來源）。
   來源照網頁對每個商品的順序，只用這裡拿得到長歷史的三種：Binance 現貨、Binance 合約、Yahoo。
   Binance 的 API 從美國的機器可能被擋，被擋就改抓它公開的資料檔（data.binance.vision）。
   用法：node cfd/bt_fetch.js --cache <資料夾> [--days 60] [--only XAUUSD,BTCUSD] */
'use strict';
const fs = require('fs'), path = require('path');
const L = require('./bt_lib.js'), { STEP, M15, H1 } = L;

const arg = (k, d) => { const i = process.argv.indexOf('--' + k); return i > 0 ? process.argv[i + 1] : d; };
const UA = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36';
const sleep = ms => new Promise(r => setTimeout(r, ms));
const ymd = t => new Date(t).toISOString().slice(0, 10);

/* ---------- 各來源的格式（純函式，bt_selftest.js 會檢查） ---------- */
function parseYahoo(d) {
  const r = d && d.chart && d.chart.result && d.chart.result[0]; if (!r || !Array.isArray(r.timestamp)) return [];
  const q = r.indicators && r.indicators.quote && r.indicators.quote[0]; if (!q) return [];
  const out = []; for (let i = 0; i < r.timestamp.length; i++) if (q.open[i] != null && q.close[i] != null && q.high[i] != null && q.low[i] != null) out.push([r.timestamp[i] * 1000, q.open[i], q.high[i], q.low[i], q.close[i], q.volume[i] || 0]);
  return out;
}
const parseBinanceApi = d => (Array.isArray(d) ? d.map(r => [+r[0], +r[1], +r[2], +r[3], +r[4], +r[5]]) : []);
function parseBinanceCsv(txt) {
  const out = [];
  for (const line of txt.split('\n')) { const c = line.split(','); let t = +c[0]; if (!line || !isFinite(t)) continue; if (t > 1e14) t = Math.floor(t / 1000); out.push([t, +c[1], +c[2], +c[3], +c[4], +c[5]]); } // 2025 年起現貨的資料檔時間改成微秒
  return out;
}
function dayList(from, to) { const out = []; for (let t = Date.UTC(new Date(from).getUTCFullYear(), new Date(from).getUTCMonth(), new Date(from).getUTCDate()); t <= to; t += 86400e3) out.push(ymd(t)); return out; }
function monthList(from, to) { const out = [], a = new Date(from), b = new Date(to); for (let y = a.getUTCFullYear(), m = a.getUTCMonth(); y < b.getUTCFullYear() || (y === b.getUTCFullYear() && m <= b.getUTCMonth()); m === 11 ? (y++, m = 0) : m++) out.push(y + '-' + String(m + 1).padStart(2, '0')); return out; }
/* 每個商品要試哪些來源、什麼順序：照網頁的清單，外匯先用 Yahoo（網頁抓長歷史時也是這樣：交易所的外匯合約太冷清），這裡拿不到長歷史的來源略過 */
function planOf(instruments, delayed) {
  const skip = delayed || require('./bt_replay.js').DELAYED, can = { binance: 1, binancef: 1, yahoo: 1 };
  return instruments.filter(i => !skip.has(i.id)).map(i => {
    let chain = i.src.filter(s => can[s[0]]).map(s => [s[0], s[1]]);
    if (i.cls === 'fx') chain = chain.filter(s => s[0] === 'yahoo').concat(chain.filter(s => s[0] !== 'yahoo'));
    return { id: i.id, cls: i.cls, ref: i.ref, chain };
  });
}
/* 這份 K 線（15 分）能不能用：太多根「高低同價」（沒有成交、價格不動）的不用——和網頁抓長歷史時同一條規則 */
function quality(rows15) { const n = rows15.length, flat = n ? rows15.filter(r => r[2] === r[3]).length / n : 1; return { n, flat: +flat.toFixed(4), ok: n >= 30 && !(n >= 200 && flat > 0.08) }; }
/* 價位離網頁記的大概價位太遠：是同代號的別的商品（例如同名的加密貨幣），不用 */
function sane(inst, rows) { if (!rows.length) return false; const p = rows[rows.length - 1][4]; return !(inst.ref && (p < inst.ref / 6 || p > inst.ref * 6)); }

/* ---------- 連線 ---------- */
const stat = { req: 0, fail: {}, fapi: null, spotApi: null };
async function get(url, o) {
  o = o || {}; let last = null;
  for (let a = 0; a <= (o.retries == null ? 2 : o.retries); a++) {
    const ac = new AbortController(), tm = setTimeout(() => ac.abort(), o.timeout || 25000); stat.req++;
    try {
      const r = await fetch(url, { headers: { 'User-Agent': UA, Accept: '*/*' }, signal: ac.signal });
      if (r.status === 404) { const e = new Error('404'); e.http = 404; throw e; }
      if (r.status === 451 || r.status === 403) { const e = new Error('blocked ' + r.status); e.http = r.status; throw e; }
      if (!r.ok) { const e = new Error('http ' + r.status); e.http = r.status; e.retry = r.status === 429 || r.status >= 500; throw e; }
      return o.bin ? Buffer.from(await r.arrayBuffer()) : await r.json();
    } catch (e) { last = e; if (e.http && !e.retry) break; await sleep(800 * (a + 1) + (e.http === 429 ? 4000 : 0)); }
    finally { clearTimeout(tm); }
  }
  const k = last && last.http ? 'http' + last.http : (last && last.name) || 'Error'; stat.fail[k] = (stat.fail[k] || 0) + 1; throw last;
}
async function pool(items, n, fn) { const out = new Array(items.length); let i = 0; await Promise.all(Array.from({ length: Math.min(n, items.length) }, async () => { while (i < items.length) { const k = i++; out[k] = await fn(items[k], k); } })); return out; }

async function yahoo(sym, iv, range, prepost) {
  let last = null;
  for (const host of ['query1.finance.yahoo.com', 'query2.finance.yahoo.com']) {
    try { return parseYahoo(await get(`https://${host}/v8/finance/chart/${encodeURIComponent(sym)}?range=${range}&interval=${iv}&includePrePost=${prepost ? 'true' : 'false'}`, { retries: 1 })); } catch (e) { last = e; }
  }
  throw last;
}
/* Binance 的 API：從最新的往回一頁一頁抓到 from 為止。全部的請求排隊、每 0.3 秒一個（它按 IP 算用量，幾個商品同時抓也不能超過） */
let gateAt = 0;
async function gate(ms) { const now = Date.now(), at = Math.max(now, gateAt); gateAt = at + ms; if (at > now) await sleep(at - now); }
async function binanceApi(base, pathq, sym, iv, from, to, lim) {
  const ms = iv === '5m' ? STEP : H1; let end = to, out = [];
  for (let k = 0; k < 120; k++) {
    await gate(300);
    const part = parseBinanceApi(await get(`${base}${pathq}?symbol=${sym}&interval=${iv}&limit=${lim}&endTime=${end}`)); if (!part.length) break;
    out = part.concat(out); if (part[0][0] <= from || part.length < lim) break; end = part[0][0] - 1;
  }
  return out.filter(r => r[0] >= from - ms);
}
/* Binance 的公開資料檔：過去的整個月抓月檔，這個月抓日檔 */
async function binanceFiles(kind, sym, iv, from, to) {
  const root = `https://data.binance.vision/data/${kind}`, thisMonth = ymd(Date.now()).slice(0, 7), file = async (per, key) => {
    try { return parseBinanceCsv(L.unzipOne(await get(`${root}/${per}/klines/${sym}/${iv}/${sym}-${iv}-${key}.zip`, { bin: true, retries: 1 })).toString('utf8')); } catch (e) { if (e.http === 404) return null; throw e; } };
  let out = [], any = false;
  for (const m of monthList(from, to)) {
    let rows = m < thisMonth ? await file('monthly', m) : null;
    if (!rows) { const days = dayList(Math.max(from, Date.parse(m + '-01T00:00:00Z')), Math.min(to, Date.now())).filter(d => d.slice(0, 7) === m), got = await pool(days, 8, d => file('daily', d)); rows = [].concat(...got.filter(Boolean)); }
    if (rows.length) any = true; out = out.concat(rows);
  }
  if (!any) { const e = new Error('404'); e.http = 404; throw e; }
  return out.filter(r => r[0] >= from && r[0] <= to);
}
async function binance(kind, sym, iv, from, to) {
  const spot = kind === 'binance', key = spot ? 'spotApi' : 'fapi';
  if (stat[key] !== 'blocked') {
    try { const r = await binanceApi(spot ? 'https://data-api.binance.vision' : 'https://fapi.binance.com', spot ? '/api/v3/klines' : '/fapi/v1/klines', sym, iv, from, to, 1000); if (stat[key] == null) stat[key] = 'ok'; return r; }
    catch (e) { if (e.http === 400 || e.http === 404) { const x = new Error('no such symbol'); x.http = 404; throw x; } if (stat[key] == null) { stat[key] = 'blocked'; console.log(`  ${spot ? 'Binance spot' : 'Binance futures'} API not reachable from here (${e.http || e.name}): using the public data files`); } else if (stat[key] === 'ok') throw e; }
  }
  return binanceFiles(spot ? 'spot' : 'futures/um', sym, iv, from, to);
}

/* ---------- 主程式 ---------- */
async function main() {
  const cache = arg('cache'), days = Math.max(1, parseInt(arg('days', '60'), 10) || 60), only = arg('only') ? arg('only').split(',') : null; if (!cache) throw new Error('--cache <dir> is required');
  const P = L.loadPage(arg('page')), R = require('./bt_replay.js'), now = Date.now(), win = R.windowOf(days, now), byId = {}; for (const i of P.Data.INSTRUMENTS) byId[i.id] = i;
  const from5 = win.from - 7 * 86400e3, from1 = win.from - 130 * 86400e3, plan = planOf(P.Data.INSTRUMENTS).filter(x => !only || only.includes(x.id)), report = { at: new Date(now).toISOString(), window: win, inst: {} };
  console.log(`fetching ${plan.length} instruments: 5-minute bars from ${ymd(from5)}, 1-hour bars from ${ymd(from1)}`);
  const open15 = (inst, rows5) => { const a = new L.Agg(M15, 1e9); for (const r of rows5) if (!P.Mkt.closedAt(inst.cls, r[0], false)) a.add(L.toObj(r)); return a.b.map(L.toRow); };
  await pool(plan, 4, async x => {
    const inst = byId[x.id], tried = []; let best = null;
    for (const [src, sym] of x.chain) {
      const rec = { src: src + ':' + sym };
      try {
        const m5 = L.tidy(src === 'yahoo' ? await yahoo(sym, '5m', '60d', inst.cls === 'stock' || inst.cls === 'etf') : await binance(src, sym, '5m', from5, now), STEP).filter(r => r[0] >= from5);
        if (!sane(inst, m5)) throw Object.assign(new Error('price far from the page\'s reference'), { why: 'price' });
        const q = quality(open15(inst, m5.filter(r => r[0] >= win.from))); rec.n5 = m5.length; rec.from = m5.length ? L.tpe(m5[0][0]).slice(0, 16) : null; rec.to = m5.length ? L.tpe(m5[m5.length - 1][0]).slice(0, 16) : null; rec.flat = q.flat; rec.ok = q.ok;
        rec.cover = m5.length ? +Math.min(1, (m5[m5.length - 1][0] - Math.max(m5[0][0], win.from)) / (Math.min(win.to, now) - win.from)).toFixed(3) : 0;
        let h1 = []; try { h1 = L.tidy(src === 'yahoo' ? (await yahoo(sym, '60m', '730d', false)).map(r => [Math.floor(r[0] / H1) * H1, r[1], r[2], r[3], r[4], r[5]]) : await binance(src, sym, '1h', from1, now), H1).filter(r => r[0] >= from1); } catch (e) { rec.h1err = e.http ? 'http' + e.http : e.name; }
        rec.n1 = h1.length;
        const cand = { rec, m5, h1, src: rec.src, score: (q.ok ? 1 : 0) * 10 + rec.cover };
        if (!best || cand.score > best.score) best = cand;
        tried.push(rec); if (q.ok && rec.cover >= 0.6) break;
      } catch (e) { rec.err = e.why || (e.http ? 'http' + e.http : e.name || 'Error'); tried.push(rec); } // 只記錯誤的種類
    }
    report.inst[x.id] = { cls: x.cls, tried, used: best ? best.src : null };
    if (best) L.writeGz(path.join(cache, 'bars', x.id + '.json.gz'), { id: x.id, src: best.src, flat: best.rec.flat, m5: best.m5, h1: best.h1 });
    console.log(`  ${x.id.padEnd(8)} ${best ? best.src.padEnd(22) + ' 5m ' + String(best.m5.length).padStart(6) + '  ' + best.rec.from + ' .. ' + best.rec.to + '  cover ' + best.rec.cover + '  flat ' + best.rec.flat + '  1h ' + best.h1.length : 'NO DATA'}${tried.length > 1 || !best ? '   tried: ' + tried.map(t => t.src + (t.err ? ' ✗' + t.err : t.ok ? ' ok' : ' thin')).join(', ') : ''}`);
  });
  report.net = stat; const used = {}; for (const k in report.inst) { const u = report.inst[k].used; used[u ? u.split(':')[0] : 'none'] = (used[u ? u.split(':')[0] : 'none'] || 0) + 1; } report.used = used;
  fs.mkdirSync(cache, { recursive: true }); fs.writeFileSync(path.join(cache, 'fetch_report.json'), JSON.stringify(report));
  console.log(`done: ${JSON.stringify(used)}; requests ${stat.req}; failures ${JSON.stringify(stat.fail)}; Binance futures API ${stat.fapi}, spot API ${stat.spotApi}`);
  if (!Object.values(report.inst).some(v => v.used)) throw new Error('no instrument has data');
}
if (require.main === module) main().catch(e => { console.error(e && e.stack ? e.stack : e); process.exit(1); });
module.exports = { parseYahoo, parseBinanceApi, parseBinanceCsv, dayList, monthList, planOf, quality, sane, main };
