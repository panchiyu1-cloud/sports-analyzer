/* 差價交易「模擬戰」回測的自我檢查（不連網，用假行情）。在真的跑回測之前先跑一次：node cfd/bt_selftest.js
   檢查：(1) 重播時交給分析引擎的 K 線，和網頁自己的回測模組（BT.views）算出來的一模一樣；
        (2) 不偷看未來：把之後的資料整段拿掉，之前每一步的分析結果一個數字都不變；
        (3) 模擬戰走完之後帳對得起來，而且每一筆的成交價當時真的存在、沒有在休市時進場、沒有超過同時持有的上限；
        (4) 讀資料檔的小工具（zip、K 線整理、各來源的格式）。 */
'use strict';
const fs = require('fs'), os = require('os'), path = require('path'), zlib = require('zlib'), { execFileSync } = require('child_process');
const L = require('./bt_lib.js'), R = require('./bt_replay.js'), F = require('./bt_fetch.js'), { STEP, M15, H1 } = L;
let pass = 0, fail = 0; const ok = (c, name, extra) => { if (c) pass++; else { fail++; console.log('FAIL', name, extra === undefined ? '' : JSON.stringify(extra).slice(0, 600)); } };
const P = L.loadPage(), { E, Mkt, Sim, BT } = P;

/* 假行情：5 分 K 的隨機漫步，帶一點一段一段的趨勢（才會有訊號）。24 小時都有，休市由 Mkt 決定 */
function walk(seed, p0, vol, t0, n) {
  let s = seed >>> 0; const rnd = () => (s = (Math.imul(s, 1664525) + 1013904223) >>> 0) / 4294967296, out = []; let p = p0, mu = 0;
  for (let i = 0; i < n; i++) { if (i % 180 === 0) mu = (Math.floor(rnd() * 3) - 1) * vol * 0.25; const o = p, c = o * (1 + mu + (rnd() - 0.5) * 2 * vol), h = Math.max(o, c) * (1 + rnd() * vol * 0.5), l = Math.min(o, c) * (1 - rnd() * vol * 0.5); out.push([t0 + i * STEP, o, h, l, c, Math.round(50 + rnd() * 300)]); p = c; }
  return out;
}
const agg = (rows, ms) => { const a = new L.Agg(ms, 1e9); for (const r of rows) a.add(L.toObj(r)); return a.b; };
const END = Date.UTC(2026, 10, 21, 5, 0, 0), DAYS = 20, T0 = END - DAYS * 86400e3; // 到 11/21（週六）05:00 UTC 為止的 20 天
const IDS = { BTCUSD: [91000, 0.0012], ETHUSD: [3100, 0.0014], XAUUSD: [4100, 0.0006], EURUSD: [1.16, 0.00025], AAPL: [270, 0.0009], SPY: [680, 0.0005], US500: [6800, 0.0005], WTI: [60, 0.001] };
const insts = P.Data.INSTRUMENTS.filter(i => IDS[i.id]), data = {};
let sd = 11; for (const i of insts) { const m5 = walk(sd++ * 7919, IDS[i.id][0], IDS[i.id][1], T0, DAYS * 288), h1 = agg(walk(sd * 31, IDS[i.id][0], IDS[i.id][1] * 3, T0 - 40 * 86400e3, 40 * 24).map(r => [T0 - 40 * 86400e3 + (r[0] - (T0 - 40 * 86400e3)) / STEP * H1, r[1], r[2], r[3], r[4], r[5]]), H1).map(L.toRow);
  data[i.id] = { id: i.id, src: 'test', m5, h1 }; }
ok(insts.length === Object.keys(IDS).length, 'test instruments exist on the page', insts.map(i => i.id));

{ // 視窗
  const w = R.windowOf(60, Date.UTC(2026, 10, 22, 14, 0, 0));
  ok(w.first === '2026-08-31' && w.last === '2026-11-20' && w.from === Date.UTC(2026, 7, 30, 21) && w.to === Date.UTC(2026, 10, 21, 5), 'window: 60 weekdays ending on the last Friday', w);
  const w2 = R.windowOf(5, Date.UTC(2026, 10, 18, 10, 0, 0)); ok(w2.first === '2026-11-12' && w2.last === '2026-11-18' && w2.to === Date.UTC(2026, 10, 18, 10, 5), 'window: ends at the last bar when the day is not over', w2);
}
{ // (1) 交給分析引擎的 K 線：和網頁的回測模組一樣
  const id = 'XAUUSD', inst = insts.find(i => i.id === id), d = data[id], m15 = agg(d.m5, M15), h1 = agg(d.m5, H1);
  const c = BT.prep({ id, cls: inst.cls, dp: inst.dp, tf: '15m', cd: m15, h1, cost: 0.00012, step: 1 }), fd = R.feeder({ m5: d.m5, h1: [] });
  let same = 0, diff = 0, firstDiff = null; const eq = (a, b) => a.length === b.length && a.every((x, k) => x.t === b[k].t && x.o === b[k].o && x.h === b[k].h && x.l === b[k].l && x.c === b[k].c && Math.abs(x.v - b[k].v) < 1e-6);
  for (let i = R.WIN - 1; i < m15.length - 1; i += 37) {
    const V = BT.views(c, i); fd.to(V.tC); const M = fd.view();
    if (eq(M.win, V.win) && eq(M.mtf['1h'], V.mtf['1h']) && eq(M.mtf['4h'], V.mtf['4h']) && eq(M.daily, V.daily)) same++; else { diff++; firstDiff = firstDiff || { i, a: [M.win.length, M.mtf['1h'].length, M.mtf['4h'].length, M.daily.length], b: [V.win.length, V.mtf['1h'].length, V.mtf['4h'].length, V.daily.length] }; }
  }
  ok(same > 40 && diff === 0, 'the bars given to the engine equal the page\'s own backtest views', { same, diff, firstDiff });
  // 15 分 K 還沒收完的時候：最後一根只含已經收完的 5 分 K
  const fd2 = R.feeder({ m5: d.m5, h1: [] }), t = d.m5[4000][0] - (d.m5[4000][0] % M15) + 2 * STEP; fd2.to(t); const v = fd2.view(), lastBar = v.win[v.win.length - 1], parts = d.m5.filter(r => r[0] >= lastBar.t && r[0] < t);
  ok(parts.length === 2 && lastBar.o === parts[0][1] && lastBar.c === parts[1][4] && lastBar.h === Math.max(parts[0][2], parts[1][2]) && v.win.every(b => b.t < t) && v.mtf['1h'].every(b => b.t < t) && v.daily.every(b => b.t < t), 'the forming bar holds only finished 5-minute bars');
}
const win = R.windowOf(9, END - STEP), rows = {};
{ // (2) 不偷看未來
  const id = 'BTCUSD', inst = insts.find(i => i.id === id), full = R.rowsFor(P, inst, data[id], win), cutT = win.from + 5 * 86400e3 + 7 * STEP;
  const part = R.rowsFor(P, inst, { m5: data[id].m5.filter(r => r[0] < cutT), h1: data[id].h1 }, win), a = full.rows.filter(r => r[0] <= cutT), b = part.rows;
  ok(b.length > 1000 && a.length === b.length && JSON.stringify(a) === JSON.stringify(b), 'no look-ahead: removing later data changes nothing before it', [a.length, b.length]);
  ok(full.rows.every(r => r[0] % STEP === 0 && r[0] >= win.from && r[0] <= win.to) && full.first === full.rows[0][0], 'rows sit on the 5-minute grid inside the window');
  const m = new Map(data[id].m5.map(r => [r[0], r])); ok(full.rows.every(r => m.get(r[0] - STEP) && m.get(r[0] - STEP)[4] === r[2]), 'the price of a step is the close of the 5-minute bar that just finished');
  for (const i of insts) rows[i.id] = i.id === id ? full.rows : R.rowsFor(P, i, data[i.id], win).rows;
  // 休市時不分析；個股在週末、外匯在週末都沒有列
  const wkend = t => { const d = new Date(t).getUTCDay(), h = new Date(t).getUTCHours(); return (d === 6 && h >= 6) || (d === 0 && h < 18); };
  const pause = t => { const d = new Date(t).getUTCDay(), h = new Date(t).getUTCHours(); return (d === 5 && h >= 23) || (d === 6 && h < 7); }; // 加密貨幣每週固定暫停的那一段
  ok(rows.US500.length > 500 && !rows.US500.some(r => wkend(r[0])) && !rows.AAPL.some(r => wkend(r[0])) && !rows.SPY.some(r => wkend(r[0])) && rows.BTCUSD.some(r => wkend(r[0])) && !rows.BTCUSD.some(r => pause(r[0])), 'closed markets are not analysed');
  ok(rows.EURUSD.filter(r => wkend(r[0])).every(r => r[1] === 1) && rows.EURUSD.some(r => r[1] === 0), 'a weekend row of an instrument with a weekend session is always marked');
  ok(rows.XAUUSD.some(r => r[1] === 1 && wkend(r[0])) && rows.XAUUSD.filter(r => r[1] === 1).every(r => Mkt.weekend('metal', r[0], true)) && !rows.SPY.some(r => r[1] === 1), 'IG weekend sessions are marked for the instruments that have one');
  const cand = Object.values(rows).flat().filter(r => r.length > 7); ok(cand.length > 200 && cand.every(r => r[5] !== 0 && r[4] >= 45 && (r[9] === '' || ['m', 'l', 's'].includes(r[9]))), 'candidate rows carry the plan', cand.length);
}
{ // (3) 模擬戰
  for (const v of R.VARIANTS) {
    const res = R.simulate(P, insts, data, rows, win, v), rep = R.report(P, res, win, v.key, v.name), st = res.st, tr = st.hist.filter(p => !p.cancel);
    ok(rep.books && rep.n === tr.length && rep.n >= 8, v.key + ': trades happen and the books add up', { n: rep.n, end: rep.end });
    const bars = id => data[id].m5, at = (id, t) => bars(id).find(r => r[0] === t - STEP);
    ok(tr.every(p => p.type !== 'market' || (at(p.instId, p.openedAt) && at(p.instId, p.openedAt)[4] === p.entry)), v.key + ': a market order fills at the price of that moment');
    ok(tr.every(p => { const b = bars(p.instId).filter(r => r[0] >= p.openedAt - STEP && r[0] <= p.closedAt); return b.length && p.exit <= Math.max(...b.map(r => r[2])) + 1e-9 && p.exit >= Math.min(...b.map(r => r[3])) - 1e-9; }), v.key + ': every exit price was really traded in that span');
    ok(tr.every(p => p.type !== 'limit' || bars(p.instId).some(r => r[0] >= p.created && r[0] <= p.openedAt && r[3] <= p.entry && r[2] >= p.entry)), v.key + ': a limit order fills only when the price was touched');
    ok(tr.every(p => !Mkt.closedAt(p.cls, p.openedAt, !!p.wk) && p.closedAt >= p.openedAt && p.openedAt >= p.created + (p.type === 'market' ? 60e3 : 0)), v.key + ': no entry while the market is closed, none before the confirmation');
    const evs = []; for (const p of tr) { evs.push([p.openedAt, 1, p]); evs.push([p.closedAt, -1, p]); } evs.sort((a, b) => a[0] - b[0] || a[1] - b[1]);
    let n = 0, mx = 0, clsMax = 0; const cls = {}, on = {}; let dupInst = false;
    for (const [, d, p] of evs) { n += d; cls[p.cls] = (cls[p.cls] || 0) + d; on[p.instId] = (on[p.instId] || 0) + d; if (on[p.instId] > 1) dupInst = true; mx = Math.max(mx, n); clsMax = Math.max(clsMax, cls[p.cls]); }
    ok(mx <= 3 && clsMax <= 2 && !dupInst, v.key + ': never more than 3 positions, 2 per class, 1 per instrument', { mx, clsMax });
    ok(tr.every(p => p.heldMs <= 6 * H1 + STEP || p.reason === 'lost' || Mkt.closedAt(p.cls, p.openedAt + 6 * H1, !!p.wk) || bars(p.instId).filter(r => r[0] >= p.openedAt + 6 * H1 && r[0] < p.closedAt).length === 0), v.key + ': nothing is held past 6 hours of trading');
    ok(tr.every(p => p.riskUsd <= p.eq0 * (p.gate ? 0.02 : 0.01) + 1e-9), v.key + ': risk per trade follows the rules (2% of the equity when queued, 1% when the gate fails)');
    { // 停利：沒改的照原計畫；改過的兩個停利都在原計畫的一半，停損不動
      const rowAt = p => rows[p.instId].find(r => r[0] === p.created), eq = (a, b) => Math.abs(a - b) < 1e-9;
      ok(tr.every(p => { const r = rowAt(p); if (!r || !r[9]) return false; const e = r[11], h1 = e + (r[13] - e) / 2, h2 = e + (r[14] - e) / 2;
        return p.sl === r[12] && (!v.tp ? p.tp1 === r[13] && p.tp2 === r[14] : eq(p.tp2, h2) && eq(p.tp1, v.tp === 'half' ? h1 : h2)); }), v.key + ': targets are ' + (v.tp ? 'at half the plan\'s distance' : 'the plan\'s own') + ', the stop is the plan\'s');
      if (v.tp) ok(tr.some(p => p.reason === 'tp2') && tr.filter(p => p.reason === 'tp2').every(p => p.exit === p.tp2 || p.gap), v.key + ': target exits fill at the new target');
      if (v.tp === 'one') ok(tr.every(p => p.reason !== 'be') && tr.filter(p => p.tp1At).every(p => p.reason === 'tp2' && p.tp1At === p.closedAt && eq(p.r, Math.abs(p.tp2 - p.entry) / p.risk)), v.key + ': the whole position leaves at one price', tr.filter(p => p.tp1At && p.reason !== 'tp2').map(p => [p.reason, p.tp1At, p.closedAt]).slice(0, 3));
    }
    if (v.noCost) ok(tr.every(p => p.costUsd === 0), v.key + ': no spread charged'); else ok(tr.every(p => p.costUsd > 0), v.key + ': every trade pays the spread');
    if (v.key === 'rule') { const wk = tr.filter(p => p.wk); ok(tr.some(p => p.reason === 'sl') && tr.some(p => p.pnl > 0), 'rule: both winners and stops occur', rep.byReason); ok(wk.every(p => Math.abs(p.costRate - (P.DEF.cost[p.cls] / 100) * 5) < 1e-12), 'rule: weekend trades pay the weekend spread', wk.length);
      ok(rep.daily.length >= 9 && rep.trades.every(t => t.length === R.TRADE_COLS.length) && JSON.stringify(rep).length < 3e6, 'report is complete and compact'); }
    if (v.key === 'slow') { const base = R.simulate(P, insts, data, rows, win, R.VARIANTS[0]).st.hist.filter(p => !p.cancel && p.type === 'market'); ok(Math.min(...tr.filter(p => p.type === 'market').map(p => p.openedAt - p.created)) >= STEP && base.length > 0, 'slow: entries wait one more step'); }
  }
  // 同樣的資料跑兩次，結果一樣
  const a = R.report(P, R.simulate(P, insts, data, rows, win, R.VARIANTS[0]), win, 'a', 'a'), b = R.report(P, R.simulate(P, insts, data, rows, win, R.VARIANTS[0]), win, 'a', 'a');
  ok(JSON.stringify(a) === JSON.stringify(b), 'deterministic');
}
{ // 停利的改法：只動兩個停利價
  const pl = { type: 'market', side: 'long', name: 'x', entry: 100, sl: 98, tp1: 103, tp2: 106 }, sh = { type: 'limit', side: 'short', name: 'y', entry: 50, sl: 51, tp1: 48.5, tp2: 47 };
  ok(JSON.stringify(R.planTp(pl, 'half')) === JSON.stringify({ type: 'market', side: 'long', name: 'x', entry: 100, sl: 98, tp1: 101.5, tp2: 103 }) && R.planTp(sh, 'half').tp1 === 49.25 && R.planTp(sh, 'half').tp2 === 48.5, 'half: both targets at half the distance');
  ok(R.planTp(pl, 'one').tp1 === 103 && R.planTp(pl, 'one').tp2 === 103 && R.planTp(sh, 'one').tp1 === 48.5 && R.planTp(sh, 'one').sl === 51, 'one: both targets at half of the final target');
  ok(R.planTp(pl) === pl && R.planTp(null, 'half') === null && pl.tp1 === 103, 'no mode: the plan is left alone (and never changed in place)');
  ok(R.VARIANTS.slice(0, 3).map(v => v.key).join() === 'rule,slow,nocost' && R.VARIANTS.slice(0, 3).every(v => !v.tp), 'the first three runs are the unchanged rules');
}
{ // (4) 資料檔的小工具
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'cfdbt-')), csv = 'open_time,open,high,low,close,volume,close_time,q,n,tb,tq,i\n1790000100000,2,3,1,2.5,10,1790000399999,0,0,0,0,0\n1790000400000,2.5,3,2,2.2,5,1790000699999,0,0,0,0,0\n';
  fs.writeFileSync(path.join(dir, 'X-5m-2026-09-21.csv'), csv); let zipped = false;
  try { execFileSync('python3', ['-c', 'import zipfile,sys; z=zipfile.ZipFile(sys.argv[1],"w",zipfile.ZIP_DEFLATED); z.write(sys.argv[2],"X-5m-2026-09-21.csv"); z.close()', path.join(dir, 'a.zip'), path.join(dir, 'X-5m-2026-09-21.csv')]); zipped = true; } catch (e) { console.log('  (python3 not available: zip check skipped)'); }
  if (zipped) { const txt = L.unzipOne(fs.readFileSync(path.join(dir, 'a.zip'))).toString('utf8'); ok(txt === csv, 'unzip reads a one-file archive'); ok(JSON.stringify(F.parseBinanceCsv(txt)) === JSON.stringify([[1790000100000, 2, 3, 1, 2.5, 10], [1790000400000, 2.5, 3, 2, 2.2, 5]]), 'Binance data file: header skipped, numbers read'); }
  ok(JSON.stringify(F.parseBinanceCsv('1790000100000000,2,3,1,2.5,10,x\n')) === JSON.stringify([[1790000100000, 2, 3, 1, 2.5, 10]]), 'Binance data file: microsecond times are converted');
  ok(JSON.stringify(L.tidy([[600e3, 1, 2, 0.5, 1.5, 1], [300e3, 1, 2, 0.5, 1.5, 1], [300e3, 9, 9, 9, 9, 9], [301e3, 1, 1, 1, 1, 1], [900e3, 1, 0.5, 2, 1, 1], [1200e3, NaN, 1, 1, 1, 1], [1500e3, 0, 1, 1, 1, 1]], STEP)) === JSON.stringify([[300e3, 9, 9, 9, 9, 9], [600e3, 1, 2, 0.5, 1.5, 1]]), 'tidy: grid, order, duplicates, bad rows');
  const y = { chart: { result: [{ meta: { regularMarketTime: 1 }, timestamp: [1790000100, 1790000400, 1790000700, 1790000950], indicators: { quote: [{ open: [1, null, 3, 4], high: [2, 2, 4, 5], low: [0.5, 1, 2, 3], close: [1.5, 1, 3.5, 4.5], volume: [10, 0, null, 7] }] } }] } };
  ok(JSON.stringify(F.parseYahoo(y)) === JSON.stringify([[1790000100000, 1, 2, 0.5, 1.5, 10], [1790000700000, 3, 4, 2, 3.5, 0], [1790000950000, 4, 5, 3, 4.5, 7]]) && F.parseYahoo({ chart: { result: null, error: { code: 'Not Found' } } }).length === 0, 'Yahoo answer: empty bars dropped, an error gives nothing');
  ok(JSON.stringify(F.parseBinanceApi([[1790000100000, '2', '3', '1', '2.5', '10', 1790000399999, '0', 0, '0', '0', '0']])) === JSON.stringify([[1790000100000, 2, 3, 1, 2.5, 10]]), 'Binance API answer');
  ok(F.dayList(Date.UTC(2026, 10, 19, 12), Date.UTC(2026, 10, 21, 3)).join() === '2026-11-19,2026-11-20,2026-11-21' && F.monthList(Date.UTC(2026, 7, 30), Date.UTC(2026, 10, 2)).join() === '2026-08,2026-09,2026-10,2026-11', 'date lists for the data files');
  const plan = F.planOf(P.Data.INSTRUMENTS);
  ok(plan.find(x => x.id === 'EURUSD').chain[0].join(':') === 'yahoo:EURUSD=X' && plan.find(x => x.id === 'XAUUSD').chain.map(c => c.join(':')).join(' ') === 'binancef:XAUUSDT yahoo:XAUUSD=X binance:PAXGUSDT yahoo:GC=F' && plan.find(x => x.id === 'BTCUSD').chain[0].join(':') === 'binance:BTCUSDT' && plan.find(x => x.id === 'US500').chain.join() === 'yahoo,ES=F' && !plan.some(x => R.DELAYED.has(x.id)),
    'source plan follows the page (FX: Yahoo first), sources this run cannot use are left out', plan.find(x => x.id === 'XAUUSD').chain);
  ok(F.quality([[0, 1, 1, 1, 1, 0], [M15, 1, 2, 1, 1, 0]].concat(Array.from({ length: 300 }, (x, i) => [(i + 2) * M15, 1, 1.1, 0.9, 1, 1])), 100).ok && !F.quality(Array.from({ length: 300 }, (x, i) => [i * M15, 1, i % 5 ? 1 : 1.1, 1, 1, 1]), 100).ok, 'quality: a feed whose bars mostly do not move is refused');
  ok(F.sane({ ref: 100 }, [[0, 1, 1, 1, 99, 0]]) && !F.sane({ ref: 100 }, [[0, 1, 1, 1, 0.5, 0]]) && F.sane({}, [[0, 1, 1, 1, 0.5, 0]]), 'a same-named instrument at a very different price is refused');
  fs.rmSync(dir, { recursive: true, force: true });
}
console.log(`selftest: ${pass} passed, ${fail} failed`); process.exit(fail ? 1 : 0);
