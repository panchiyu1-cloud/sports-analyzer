/* 差價交易「模擬戰」回測：重播。
   第一段（rows）：每個商品、每 5 分鐘，把「當時看得到的 K 線」交給網頁的分析引擎，記下它當時的評級、機率、門檻與進場計畫。
   第二段（sim）：照時間順序，讓網頁的模擬戰核心（Sim）自己排單、進場、出場。兩段都不偷看未來。
   用法：node cfd/bt_replay.js --cache <資料夾> --out <資料夾> [--days 60] [--jobs 4] [--only XAUUSD,BTCUSD] */
'use strict';
const fs = require('fs'), path = require('path'), os = require('os'), { spawn } = require('child_process');
const L = require('./bt_lib.js'), { STEP, M15, H1, Agg } = L;

const arg = (k, d) => { const i = process.argv.indexOf('--' + k); return i > 0 ? process.argv[i + 1] : d; };
const TF = '15m', WIN = 300, ROWS_V = 1, GRADES = 'ABCD', PT = { market: 'm', limit: 'l', stop: 's' }, PTN = { m: 'market', l: 'limit', s: 'stop' };

/* 這些商品不列入：網頁上它們只有 Yahoo 的延遲報價（期貨、指數慢 10–15 分鐘），真正的模擬戰碰到報價延遲 3 分鐘以上就不進場，所以回測也不做 */
const DELAYED = new Set(['CORN', 'WHEAT', 'SOYBEAN', 'COFFEE', 'SUGAR', 'COCOA', 'US30', 'US2000', 'GER40', 'TWSE']);

/* 回測的視窗：到最後一個星期五為止的 N 個平日（週一到週五），從第一天的前一晚（外匯開盤）算起 */
function windowOf(days, lastT) {
  let d = new Date(lastT); d = new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate()));
  while (d.getUTCDay() === 0 || d.getUTCDay() === 6) d = new Date(d.getTime() - 86400e3); // 最後一個平日
  const end = d.getTime() + 86400e3 + 5 * H1; let n = 1, s = d;                             // 到隔天 05:00 UTC（紐約收盤之後）
  while (n < days) { s = new Date(s.getTime() - 86400e3); if (s.getUTCDay() !== 0 && s.getUTCDay() !== 6) n++; }
  return { from: s.getTime() - 3 * H1, to: Math.min(end, lastT + STEP), first: s.toISOString().slice(0, 10), last: d.toISOString().slice(0, 10), days };
}

/* 照時間把 5 分 K 餵進各個週期。to(t)：把「t 以前已經收完」的 5 分 K 都放進去；view()：這時候看得到的 K 線（每個週期最後一根可能還沒收完，和交易所即時給的一樣） */
function feeder(data) {
  const m5 = data.m5.map(L.toObj), a15 = new Agg(M15, 420), a1 = new Agg(H1, 900), a4 = new Agg(4 * H1, 260), aD = new Agg(24 * H1, 60); let j = 0;
  if (m5.length) { const h0 = Math.floor(m5[0].t / H1) * H1; for (const r of data.h1 || []) { const b = L.toObj(r); if (b.t < h0) { a1.add(b); a4.add(b); aD.add(b); } } } // 5 分 K 開始以前的 1 小時 K：只拿來暖 1 小時、4 小時與日線
  return { m5, a15, to(t) { while (j < m5.length && m5[j].t < t) { const b = m5[j++]; a15.add(b); a1.add(b); a4.add(b); aD.add(b); } },
    view: () => ({ win: a15.b.slice(-WIN), mtf: { '1h': a1.b.slice(-200), '4h': a4.b.slice(-200) }, daily: aD.b.slice(-40) }) };
}
/* ---------- 第一段：一個商品的逐步分析 ---------- */
function rowsFor(P, inst, data, win) {
  const { E, Mkt } = P, F = feeder(data), m5 = F.m5, a15 = F.a15, n = m5.length, out = { id: inst.id, rows: [], first: null, steps: 0, skipped: { warm: 0, closed: 0, stale: 0, err: 0 } };
  if (!n) return out;
  const cost0 = (P.DEF.cost[inst.cls] || 0) / 100, wkMul = Math.max(1, +P.DEF.wkCost || 5);
  for (let t = Math.ceil((m5[0].t + STEP) / STEP) * STEP; t <= Math.min(win.to, m5[n - 1].t + STEP); t += STEP) {
    F.to(t);
    if (t < win.from) continue;
    const sc = Mkt.sched(inst.cls, t, !!inst.wk); if (!sc.open) { out.skipped.closed++; continue; }
    if (a15.b.length < WIN) { out.skipped.warm++; continue; }
    const wk = !!sc.weekend, last = a15.last();
    if (t - last.t > M15 + Math.max(M15 * 0.5, 20 * 60e3) || (wk && L.dead(a15.b, M15, t))) { out.skipped.stale++; continue; } // 沒有新的報價（盤後、假日）：網頁會標「沒有交易」
    const V = F.view(), A = E.analyze({ cd: V.win, tf: TF, cls: inst.cls, dp: inst.dp, mtf: V.mtf, daily: V.daily, news: null, inter: null, cost: cost0 * (wk ? wkMul : 1), now: t });
    if (!A || A.error) { out.skipped.err++; continue; }
    const R = E.rate(A, { dp: inst.dp, stale: false, closed: false, weekend: wk, thin: !wk && Mkt.thin(inst.cls, t), dawn: E.isDawn(t), eventSoon: false });
    const row = [t, wk ? 1 : 0, A.price, +A.prob.up.toFixed(4), R.score, R.dir, GRADES.indexOf(R.grade)];
    if (R.dir && R.score >= 45) { // 夠格進「觀察中」的才需要完整內容
      const G = E.gate(A, R.dir, inst.cls, t), pl = R.plan;
      row.push(+A.prob.down.toFixed(4), G.pass ? 1 : 0, pl ? PT[pl.type] || pl.type : '', pl ? pl.side : '', pl ? pl.entry : 0, pl ? pl.sl : 0, pl ? pl.tp1 : 0, pl ? pl.tp2 : 0, pl ? pl.name : '', A.regime.label);
    }
    out.rows.push(row); out.steps++; if (out.first == null) out.first = t;
  }
  return out;
}

/* 改停利的試算（使用者 2026-11-23：「停利設定在目標價格的一半就好，不用等到真的抵達目標價格」）。只改兩個停利價，進場價與停損不動。
   half：停利 1、停利 2 都拉近一半（還是先平一半、其餘保本，再等停利 2）；one：走到「最終目標（停利 2）的一半」就全部出場（兩個停利設在同一個價位） */
function planTp(pl, mode) {
  if (!pl || !mode) return pl;
  const e = pl.entry, h1 = e + (pl.tp1 - e) / 2, h2 = e + (pl.tp2 - e) / 2;
  return Object.assign({}, pl, mode === 'half' ? { tp1: h1, tp2: h2 } : { tp1: h2, tp2: h2 });
}
/* ---------- 第二段：讓模擬戰照時間走一遍 ---------- */
function simulate(P, insts, data, rows, win, opt) {
  const { E, Mkt, Sim } = P, cfg = Object.assign({ histFull: 1e9, histMax: 1e9 }, opt.cfg || {}), st = Sim.init(cfg, win.from);
  const wkMul = Math.max(1, +P.DEF.wkCost || 5), holdMs = (+P.DEF.maxHold > 0 ? +P.DEF.maxHold : 72) * H1, byId = {}; for (const i of insts) byId[i.id] = i;
  const S = {}; // 每個商品的進度：5 分 K 讀到哪、分析讀到哪、15 分 K（算相關用）、這個方向的訊號從什麼時候開始
  for (const i of insts) S[i.id] = { m5: (data[i.id] ? data[i.id].m5 : []), j: 0, rows: rows[i.id] || [], k: 0, a15: new Agg(M15, 100), sig: null, lastP: null };
  const ev = [], cancels = {}, whyNot = {}, daily = [], ready = { n: 0 }; let dayKey = '', dayStartEq = cfg.start || 1000, peak = dayStartEq, maxDd = 0, maxDdUsd = 0, steps = 0;
  const same = (a, da, b, db) => { const ra = S[a], rb = S[b], c = ra && rb ? L.corr(ra.a15.b.slice(-81).map(x => [x.t, x.c]), rb.a15.b.slice(-81).map(x => [x.t, x.c]), M15) : null;
    return c != null ? c * da * db >= 0.7 : da === db && byId[a] && byId[b] && byId[a].cls === byId[b].cls && byId[a].cls === 'crypto'; };
  const note = list => { for (const e of list) { ev.push(e); if (e.k === 'cancel') cancels[e.v] = (cancels[e.v] || 0) + 1; if (e.k === 'ready') ready.n++; } };
  for (let t = Math.ceil(win.from / STEP) * STEP; t <= win.to; t += STEP) {
    steps++;
    // 1) 新收完的 5 分 K 進來
    for (const id in S) { const s = S[id], m = s.m5; while (s.j < m.length && m[s.j][0] < t) { const r = m[s.j++]; s.a15.add({ t: r[0], o: r[1], h: r[2], l: r[3], c: r[4], v: r[5] }); s.lastP = r[4]; } }
    // 2) 掛單與持有中的單：用這段時間的 5 分 K 判斷到價、停利、停損、時間到（和網頁的 simMonitor 同一個順序）
    const act = st.items.filter(p => p.stage === 'open' || (p.stage === 'ready' && p.type === 'limit')), groups = {};
    for (const p of act) { const k = p.instId + (p.wk ? '|w' : ''); (groups[k] = groups[k] || []).push(p); }
    for (const k in groups) {
      const ps = groups[k], id = ps[0].instId, wk = !!ps[0].wk, inst = byId[id], s = S[id], sc = Mkt.sched(inst.cls, t, wk), closed = !sc.open, due = ps.some(p => p.stage === 'open' && t >= p.dueAt);
      if (closed && !due) { note(Sim.step(st, id, { closed: true, now: t, wk })); continue; }
      const first = Math.min(...ps.map(p => p.lastCheck)), m = s.m5; let a = s.j; while (a > 0 && m[a - 1][0] >= first) a--; if (a > 0) a--; // 往回多帶一根，接得上上次檢查的時間
      const raw = m.slice(a, s.j); if (!raw.length) continue;
      const cd = raw.map(L.toObj).filter(b => !Mkt.closedAt(inst.cls, b.t, wk)); if (!cd.length) continue;
      note(Sim.step(st, id, { cd, price: cd[cd.length - 1].c, now: t, from: raw[0][0], tfMs: STEP, wk }));
    }
    // 3) 這一步的掃描結果 → 候選
    const info = {}, cands = [];
    for (const id in S) {
      const s = S[id], inst = byId[id]; while (s.k < s.rows.length && s.rows[s.k][0] < t) s.k++;
      const r = s.k < s.rows.length && s.rows[s.k][0] === t ? s.rows[s.k] : null;
      if (!r) { if (st.items.some(p => p.instId === id)) info[id] = { off: true, t, wk: false, price: s.lastP, pUp: null }; continue; } // 這時候沒有交易或沒有報價
      const wk = !!r[1], price = r[2], up = r[3], score = r[4], d = r[5], grade = GRADES[r[6]] || 'D';
      info[id] = { pUp: up, price, t, off: false, wk, grade, score, basis: 'src' };
      const ok = score >= 55, pv = s.sig; s.sig = pv && pv.dir === d && pv.ok === ok ? pv : { dir: d, ok, t: t - (opt.sigLead || 0) }; // 這個方向的「可進場」從什麼時候開始
      if (!d || r.length < 10) continue;
      const g = s.sig, age = t - g.t, pl = r[9] ? planTp({ type: PTN[r[9]] || r[9], side: r[10], name: r[15], entry: r[11], sl: r[12], tp1: r[13], tp2: r[14] }, opt.tp) : null;
      cands.push({ id, name: inst.name, code: inst.code, cls: inst.cls, dp: inst.dp, tf: TF, tfMs: M15, dir: d, score, grade, label: '', action: '', notes: [], prob: d > 0 ? up : r[7], gate: !!r[8], gateWhy: null, plan: pl, price, t,
        sigOk: !!(g.ok && age >= st.cfg.sigMs), sigWait: g.ok ? Math.max(0, st.cfg.sigMs - age) : 0, off: false, lag: 0, eventSoon: false, closeIn: Mkt.closeIn(inst.cls, t, wk), wk,
        cost: opt.noCost ? 0 : (P.DEF.cost[inst.cls] || 0) / 100 * (wk ? wkMul : 1), regime: r[16] || '', src: (data[id] && data[id].src) || '', basis: 'src', unit: null, news: '', inter: '', nextEv: '' });
    }
    cands.sort((a, b) => b.score - a.score);
    note(Sim.scan(st, { now: t, cands, info, same, holdMs }));
    for (const w of st.watch) if (w.score >= st.cfg.minScore) for (const y of w.why) whyNot[y.k] = (whyNot[y.k] || 0) + 1;
    // 4) 每天（臺北時間）記一次帳
    const eq = Sim.equity(st); if (eq > peak) peak = eq; const dd = peak > 0 ? (peak - eq) / peak : 0; if (dd > maxDd) { maxDd = dd; maxDdUsd = peak - eq; }
    const dk = L.tpeDay(t); if (dk !== dayKey) { if (dayKey) daily.push([dayKey, +dayStartEq.toFixed(2)]); dayKey = dk; dayStartEq = eq; }
  }
  const eqEnd = Sim.equity(st); daily.push([dayKey, +dayStartEq.toFixed(2)]); daily.push(['end', +eqEnd.toFixed(2)]);
  return { st, ev, cancels, whyNot, daily, ready: ready.n, peak, maxDd, maxDdUsd, steps, eqEnd };
}

/* ---------- 整理成果 ---------- */
const r2 = v => (typeof v === 'number' && isFinite(v) ? Math.round(v * 100) / 100 : null), r4 = v => (typeof v === 'number' && isFinite(v) ? +v.toFixed(4) : null);
function grp(tr, key) { const m = {}; for (const p of tr) { const k = key(p), g = m[k] = m[k] || { n: 0, w: 0, pnl: 0, r: 0 }; g.n++; if (p.pnl > 0) g.w++; g.pnl += p.pnl; g.r += p.rNet || 0; } for (const k in m) { m[k].pnl = r2(m[k].pnl); m[k].avgR = r4(m[k].r / m[k].n); delete m[k].r; } return m; }
function report(P, res, win, key, name) {
  const { Sim, BT } = P, st = res.st, tr = st.hist.filter(p => !p.cancel).sort((a, b) => a.openedAt - b.openedAt), open = st.items.filter(p => p.stage === 'open'), s = Sim.stats(st), start = st.cfg.start;
  const sumPnl = tr.reduce((a, p) => a + p.pnl, 0), booked = st.items.reduce((a, p) => a + (p.booked || 0), 0), wins = tr.filter(p => p.pnl > 0), loss = tr.filter(p => p.pnl <= 0);
  const days = {}; for (const p of tr) { const d = L.tpeDay(p.closedAt), g = days[d] = days[d] || { n: 0, pnl: 0 }; g.n++; g.pnl += p.pnl; }
  const dl = Object.values(days), half = win.from + (win.to - win.from) / 2;
  return { key, name, start, end: r2(res.eqEnd), balance: r2(st.balance), ret: r4(res.eqEnd / start - 1), n: tr.length, wins: wins.length, winRate: tr.length ? r4(wins.length / tr.length) : null, pnl: r2(sumPnl),
    avgR: r4(s.avgR), avgWin: wins.length ? r2(wins.reduce((a, p) => a + p.pnl, 0) / wins.length) : null, avgLoss: loss.length ? r2(loss.reduce((a, p) => a + p.pnl, 0) / loss.length) : null,
    best: r2(s.best), worst: r2(s.worst), cost: r2(tr.reduce((a, p) => a + (p.costUsd || 0), 0)), fin: r2(tr.reduce((a, p) => a + (p.finUsd || 0), 0)), holdMin: tr.length ? Math.round(tr.reduce((a, p) => a + p.heldMs, 0) / tr.length / 60e3) : null,
    maxDd: r4(res.maxDd), maxDdUsd: r2(res.maxDdUsd), peak: r2(res.peak), ready: res.ready, cancels: res.cancels, whyNot: res.whyNot, openLeft: open.length, openPnl: r2(open.reduce((a, p) => a + Sim.pnlNow(p), 0)),
    books: Math.abs(st.balance - (start + sumPnl + booked)) < 1e-6, // 帳要對得起來：餘額＝起始＋每筆損益（＋還沒出場那幾筆已入帳的一半）
    reached1R: tr.filter(p => p.mfe >= 1).length, tp1: tr.filter(p => p.tp1At).length, daysTraded: dl.length, daysUp: dl.filter(d => d.pnl > 0).length, daysDown: dl.filter(d => d.pnl < 0).length,
    byCls: grp(tr, p => p.cls), bySide: grp(tr, p => p.side), byGate: grp(tr, p => (p.gate ? 'pass' : 'fail')), byReason: grp(tr, p => p.reason), byType: grp(tr, p => p.type), byWk: grp(tr, p => (p.wk ? 'weekend' : 'weekday')),
    bySes: grp(tr, p => BT.session(Math.round(p.openedAt / 1000))), byGrade: grp(tr, p => p.grade), byHalf: grp(tr, p => (p.openedAt < half ? 'first' : 'second')), byInst: grp(tr, p => p.instId),
    byWeek: grp(tr, p => { const d = new Date(p.closedAt + 8 * H1), m = new Date(d.getTime() - ((d.getUTCDay() + 6) % 7) * 86400e3); return m.toISOString().slice(0, 10); }),
    daily: res.daily,
    trades: tr.map(p => [p.instId, p.cls, p.side === 'long' ? 1 : -1, p.type[0], p.wk ? 1 : 0, p.gate ? 1 : 0, p.score, p.grade, r4(p.prob0), Math.round(p.created / 1000), Math.round(p.openedAt / 1000), Math.round(p.closedAt / 1000),
      p.entry, p.exit, p.sl, p.tp1, p.tp2, p.reason, r4(p.r), r4(p.rNet), r2(p.pnl), r2(p.riskUsd), r2(p.notional), r2(p.costUsd), r2(p.finUsd || 0), r4(p.mfe), r4(p.mae), p.tp1At ? 1 : 0, p.gap ? 1 : 0, r2(p.balAfter)]) };
}
const TRADE_COLS = ['id', 'cls', 'dir', 'type', 'wk', 'gate', 'score', 'grade', 'prob', 'ready', 'open', 'close', 'entry', 'exit', 'sl', 'tp1', 'tp2', 'reason', 'r', 'rNet', 'pnl', 'risk', 'notional', 'cost', 'fin', 'mfe', 'mae', 'tp1hit', 'gap', 'balAfter'];
const VARIANTS = [
  { key: 'rule', name: '照模擬戰的規則', sigLead: STEP },                      // 訊號第一次出現就當成已經出現一陣子（下一步、5 分鐘後仍然成立才成交）
  { key: 'slow', name: '進場再晚 5 分鐘', sigLead: 0 },                          // 訊號要連續兩步才排單，第三步才成交
  { key: 'nocost', name: '不算點差（只看訊號本身）', sigLead: STEP, noCost: true },
  // 停利拉近一半的兩種讀法，各配同樣三種算法（照規則、晚 5 分鐘、不算點差），才能和上面三個一對一比較
  { key: 'halfA', name: 'A：兩個停利都拉近一半', sigLead: STEP, tp: 'half' },
  { key: 'halfA_slow', name: 'A＋進場再晚 5 分鐘', sigLead: 0, tp: 'half' },
  { key: 'halfA_nocost', name: 'A＋不算點差', sigLead: STEP, tp: 'half', noCost: true },
  { key: 'halfB', name: 'B：走到最終目標的一半就全部出場', sigLead: STEP, tp: 'one' },
  { key: 'halfB_slow', name: 'B＋進場再晚 5 分鐘', sigLead: 0, tp: 'one' },
  { key: 'halfB_nocost', name: 'B＋不算點差', sigLead: STEP, tp: 'one', noCost: true },
];

/* ---------- 主程式 ---------- */
function loadAll(cache, only) {
  const P = L.loadPage(arg('page')), all = P.Data.INSTRUMENTS.filter(i => !only || only.includes(i.id)), data = {}, missing = [];
  for (const i of all) { const f = path.join(cache, 'bars', i.id + '.json.gz'); if (fs.existsSync(f)) data[i.id] = L.readGz(f); else missing.push(i.id); }
  let lastT = 0; for (const id in data) { const m = data[id].m5; if (m.length && m[m.length - 1][0] > lastT) lastT = m[m.length - 1][0]; }
  return { P, all, data, missing, lastT };
}
async function main() {
  const cache = arg('cache'), out = arg('out', cache), days = Math.max(1, parseInt(arg('days', '60'), 10) || 60), only = arg('only') ? arg('only').split(',') : null;
  if (!cache) throw new Error('--cache <dir> is required');
  const { P, all, data, missing, lastT } = loadAll(cache, only);
  if (!lastT) throw new Error('no bars in ' + cache);
  const endT = arg('end') ? +arg('end') : lastT, win = windowOf(days, endT), insts = all.filter(i => data[i.id] && !DELAYED.has(i.id) && data[i.id].m5.length);
  const worker = arg('worker');
  if (worker != null) { // 子程序：只算分到的那幾個商品
    for (const id of worker.split(',')) { const inst = insts.find(i => i.id === id); if (!inst) continue; const t0 = Date.now(), r = rowsFor(P, inst, data[id], win); r.ms = Date.now() - t0; L.writeGz(path.join(cache, 'rows', id + '.json.gz'), r); console.log(`  ${id}: ${r.steps} steps, ${(r.ms / 1000).toFixed(0)} s`); }
    return;
  }
  console.log(`window ${win.first} .. ${win.last} (${days} weekdays), ${insts.length} instruments, ${missing.length} without data${missing.length ? ': ' + missing.join(' ') : ''}`);
  // 第一段：分給幾個子程序平行算（資料與網頁沒變就沿用上次的結果）
  // 第一段只和這四樣有關：網頁、bt_lib.js、視窗、逐步分析的寫法（feeder / rowsFor 有改就把 ROWS_V 加一）。第二段怎麼改都不用重算第一段。
  const sig = require('crypto').createHash('sha1').update(fs.readFileSync(arg('page') || L.PAGE)).update(fs.readFileSync(require.resolve('./bt_lib.js'))).update('rows-v' + ROWS_V).update(JSON.stringify(win)).digest('hex').slice(0, 12);
  const keep = process.argv.includes('--keep-rows'); // 已經有的第一段結果照用（2026-11-22 那一次是用舊的戳記算的，內容相同）
  const stamp = id => sig + ':' + data[id].m5.length + ':' + data[id].m5[data[id].m5.length - 1][0] + ':' + (data[id].h1 || []).length;
  const todo = insts.filter(i => { const f = path.join(cache, 'rows', i.id + '.json.gz'), s = path.join(cache, 'rows', i.id + '.stamp'); if (keep && fs.existsSync(f)) { fs.writeFileSync(s, stamp(i.id)); return false; } return !(fs.existsSync(f) && fs.existsSync(s) && fs.readFileSync(s, 'utf8') === stamp(i.id)); });
  if (todo.length) {
    const jobs = Math.max(1, Math.min(parseInt(arg('jobs', '0'), 10) || os.cpus().length, todo.length)), bins = Array.from({ length: jobs }, () => ({ w: 0, ids: [] }));
    for (const i of todo.slice().sort((a, b) => data[b.id].m5.length - data[a.id].m5.length)) { const b = bins.reduce((x, y) => (y.w < x.w ? y : x)); b.ids.push(i.id); b.w += data[i.id].m5.length; }
    console.log(`stage 1: ${todo.length} instruments on ${jobs} processes`);
    const t0 = Date.now();
    await Promise.all(bins.filter(b => b.ids.length).map(b => new Promise((res, rej) => {
      const a = [__filename, '--cache', cache, '--days', String(days), '--end', String(endT), '--worker', b.ids.join(',')]; if (arg('page')) a.push('--page', arg('page'));
      const c = spawn(process.execPath, a, { stdio: 'inherit' }); c.on('exit', code => (code ? rej(new Error('worker failed: ' + code)) : res()));
    })));
    for (const i of todo) fs.writeFileSync(path.join(cache, 'rows', i.id + '.stamp'), stamp(i.id));
    console.log(`stage 1 done in ${((Date.now() - t0) / 1000).toFixed(0)} s`);
  } else console.log('stage 1: reused');
  const rows = {}, cover = {};
  for (const i of insts) { const r = L.readGz(path.join(cache, 'rows', i.id + '.json.gz')); rows[i.id] = r.rows; const m = data[i.id].m5;
    cover[i.id] = { cls: i.cls, src: data[i.id].src, bars: m.length, from: L.tpe(m[0][0]).slice(0, 16), to: L.tpe(m[m.length - 1][0]).slice(0, 16), firstStep: r.first ? L.tpe(r.first).slice(0, 16) : null, steps: r.steps, skipped: r.skipped, flat: data[i.id].flat }; }
  // 第二段
  const runs = [];
  for (const v of VARIANTS) { const t0 = Date.now(), res = simulate(P, insts, data, rows, win, v), rep = report(P, res, win, v.key, v.name); runs.push(rep);
    console.log(`${v.key}: ${rep.n} trades, win ${rep.winRate == null ? '-' : (rep.winRate * 100).toFixed(1) + '%'}, ${rep.start} -> ${rep.end} (${(rep.ret * 100).toFixed(1)}%), maxDD ${(rep.maxDd * 100).toFixed(1)}%, books ${rep.books ? 'ok' : 'MISMATCH'} (${((Date.now() - t0) / 1000).toFixed(0)} s)`); }
  if (runs.some(r => !r.books)) throw new Error('the books do not add up');
  const meta = { generated: new Date().toISOString(), window: { first: win.first, last: win.last, days, from: L.tpe(win.from).slice(0, 16), to: L.tpe(win.to).slice(0, 16) }, stepMin: STEP / 60e3, tf: TF,
    rules: P.Sim.DEF, cost: P.DEF.cost, wkCost: P.DEF.wkCost, maxHoldH: P.DEF.maxHold, instruments: insts.length, delayed: [...DELAYED], missing, cover, tradeCols: TRADE_COLS,
    fetch: fs.existsSync(path.join(cache, 'fetch_report.json')) ? JSON.parse(fs.readFileSync(path.join(cache, 'fetch_report.json'), 'utf8')) : null };
  fs.mkdirSync(out, { recursive: true }); fs.writeFileSync(path.join(out, 'sim_backtest.json'), JSON.stringify({ meta, runs }));
  console.log('wrote ' + path.join(out, 'sim_backtest.json'));
}
if (require.main === module) main().catch(e => { console.error(e && e.stack ? e.stack : e); process.exit(1); });
module.exports = { windowOf, feeder, rowsFor, simulate, report, planTp, VARIANTS, DELAYED, TRADE_COLS, WIN };
