'use strict';
/* 臺股強勢掃描 —— 雲端版的資料讀取。
   網頁放在 GitHub Pages；資料由 GitHub Actions 每分鐘推到存放庫的 stock-data 分支（JSON 檔）。
   這裡把電腦版的 /api/... 對應到那些檔案，app.js 其餘程式不用改。
   測試：網址加上 ?data=http://localhost:8766/ 就改讀那個資料夾。 */
window.TWSCAN_CLOUD = (() => {
  const REPO = 'panchiyu1-cloud/sports-analyzer', BRANCH = 'stock-data';
  const RAW = `https://raw.githubusercontent.com/${REPO}/`;
  const HEAD_URL = `https://api.github.com/repos/${REPO}/git/ref/heads/${BRANCH}`;
  const HEAD_TTL = 75000;   // GitHub API 未登入每小時 60 次：最多 75 秒問一次最新版本
  const local = new URLSearchParams(location.search).get('data');
  let head = null, headAt = 0, meta = null, metaSha = null;

  // 分支最新一版的代號（SHA）。用 SHA 讀檔一定是最新、完整的同一版；問不到時改用分支名稱（可能慢幾分鐘）。
  async function sha() {
    if (local) return null;
    if (relayFresh() && relayHead) return relayHead;
    if (Date.now() - headAt < HEAD_TTL) return head;
    headAt = Date.now();
    try {
      const r = await fetch(HEAD_URL, { headers: { Accept: 'application/vnd.github+json' } });
      if (r.ok) {
        const j = await r.json();
        head = (j && j.object && j.object.sha) || null;
      } else head = null;
    } catch (e) { head = null; }
    return head;
  }

  async function file(name) {
    const s = await sha();
    const url = local ? `${local.replace(/\/?$/, '/')}${name}?t=${Date.now()}`
      : s ? `${RAW}${s}/${name}` : `${RAW}${BRANCH}/${name}?t=${Math.floor(Date.now() / 20000)}`;
    const r = await fetch(url, s ? {} : { cache: 'no-store' });
    if (r.status === 404) return null;
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return r.json();
  }

  async function getMeta() {
    const s = await sha();
    if (meta && s && s === metaSha) return meta;
    meta = (await file('meta.json')) || meta;
    metaSha = s;
    return meta;
  }

  // ---------------------------------------------------------------- 10 秒報價（使用者自己的 Google Apps Script 轉播站）
  // GitHub 一分鐘只能更新一次；設定了轉播站網址，價格改成每 10 秒更新（分析、名單仍是每分鐘）。
  const RKEY = 'twscan.cloud.relay', RELAY_EVERY = 10000;
  let relayUrl = '', ticks = null, relayAt = 0, relayErr = 0, relayHead = null, watchSent = '', watchAt = 0;
  try { relayUrl = localStorage.getItem(RKEY) || ''; } catch (e) { /* 無痕模式 */ }
  const hms = (t) => typeof t === 'string' && /^\d\d:\d\d:\d\d$/.test(t);
  function relayFresh() { return !!(ticks && Date.now() / 1000 - ticks.ts < 90); }
  async function relayPoll() {
    if (!relayUrl || document.hidden) return;
    try {
      const r = await fetch(relayUrl + (relayUrl.includes('?') ? '&' : '?') + 't=' + Date.now());
      const d = await r.json();
      relayErr = 0;
      if (d && d.q && d.ts && (!ticks || d.ts > ticks.ts)) {
        ticks = d;
        relayAt = Date.now();
        if (d.sha) relayHead = d.sha;
        window.dispatchEvent(new CustomEvent('twscan:tick'));
      }
    } catch (e) { relayErr++; }
    sendWatch();
  }
  // 持倉和正在看的股票：請雲端優先更新（每幾秒讀一次證交所）
  function sendWatch() {
    if (!relayUrl) return;
    const m = loadMine();
    const codes = m.positions.map((p) => p.code);
    const hs = /^#stock-([0-9A-Z]+)/i.exec(location.hash);
    if (hs) codes.push(hs[1].toUpperCase());
    const key = [...new Set(codes)].slice(0, 10).join(',');
    if (key === watchSent && Date.now() - watchAt < 20 * 60000) return;
    watchSent = key;
    watchAt = Date.now();
    fetch(relayUrl, { method: 'POST', headers: { 'Content-Type': 'text/plain;charset=utf-8' },
      body: JSON.stringify({ watch: key ? key.split(',') : [] }) }).catch(() => { watchAt = 0; });
  }
  setInterval(relayPoll, RELAY_EVERY);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) relayPoll(); });
  setTimeout(relayPoll, 300);

  // 某檔股票的 10 秒報價：[價, 昨收, 推估?, 時間, 高, 低]；不是今天的、太舊的不用
  function tickFor(code) {
    if (!relayFresh() || ticks.date !== tpe().date) return null;
    const t = ticks.q[code];
    return t && t[0] && hms(t[3]) ? t : null;
  }
  // 物件裡的價格比 10 秒報價舊，就換成新的（名單、當沖候選、個股頁、追蹤表都是這種物件）
  function patchObj(o) {
    const t = tickFor(o.code);
    if (!t || o.time === '收盤' || (hms(o.time) && o.time >= t[3])) return;
    const prev = t[1] || o.prev;
    o.price = t[0];
    if (prev) { o.chgp = r2((t[0] / prev - 1) * 100); if ('chg' in o) o.chg = r2(t[0] - prev); }
    o.est = !!t[2];
    o.time = t[3];
    if (t[4] != null && o.high != null) o.high = Math.max(o.high, t[4]);
    if (t[5] != null && o.low != null) o.low = Math.min(o.low, t[5]);
  }
  function patch(x, depth) {
    if (!ticks || !x || typeof x !== 'object' || depth > 6) return x;
    if (Array.isArray(x)) { x.forEach((y) => patch(y, depth + 1)); return x; }
    if (typeof x.code === 'string' && typeof x.price === 'number') patchObj(x);
    for (const k in x) {
      if (k === 'chart' || k === 'spark' || k === 'news') continue;
      const v = x[k];
      if (v && typeof v === 'object') patch(v, depth + 1);
    }
    return x;
  }
  function patchMarket(m) {
    if (!ticks || !m || !Array.isArray(m.rows) || !Array.isArray(m.fields)) return m;
    const f = {};
    m.fields.forEach((k, i) => { f[k] = i; });
    m.rows.forEach((r) => {
      const t = tickFor(r[f.code]);
      const qt = r[f.qtime];
      if (!t || qt === '收盤' || (hms(qt) && qt >= t[3])) return;
      const prev = t[1] || r[f.prev];
      r[f.price] = t[0];
      if (prev) { r[f.chg] = r2(t[0] - prev); r[f.chgp] = r2((t[0] / prev - 1) * 100); }
      r[f.est] = t[2] ? 1 : 0;
      r[f.qtime] = t[3];
      if (t[4] != null && r[f.high] != null) r[f.high] = Math.max(r[f.high], t[4]);
      if (t[5] != null && r[f.low] != null) r[f.low] = Math.min(r[f.low], t[5]);
    });
    return m;
  }
  const relay = {
    url: () => relayUrl,
    set(url) {
      relayUrl = (url || '').trim();
      try { if (relayUrl) localStorage.setItem(RKEY, relayUrl); else localStorage.removeItem(RKEY); } catch (e) { /* ignore */ }
      ticks = null; relayHead = null; watchSent = '';
      relayPoll();
    },
    status: () => ({ on: !!relayUrl, fresh: relayFresh(), at: relayAt, now: ticks && ticks.now, err: relayErr, n: ticks ? Object.keys(ticks.q).length : 0 }),
  };

  const notHere = (msg) => ({ error: msg });

  // ---------------------------------------------------------------- 我的持倉與交易紀錄（只存在這支手機的瀏覽器）
  // 雲端主機不知道你的持倉，所以買賣紀錄、損益、停損／目標提醒都在網頁上算（規則和電腦版 daytrade.py 相同）。
  const MKEY = 'twscan.cloud.mine';
  const FEE_RATE = 0.001425, MIN_FEE = 20, TAX_DT = 0.0015, TAX_STOCK = 0.003, TAX_ETF = 0.001;
  const STOP_MIN = 0.008, STOP_MAX = 0.025, RR = 2;
  const r2 = (x) => Math.round(x * 100) / 100;
  const tickSize = (p, etf) => (etf ? (p < 50 ? 0.01 : 0.05) : p < 10 ? 0.01 : p < 50 ? 0.05 : p < 100 ? 0.1 : p < 500 ? 0.5 : p < 1000 ? 1 : 5);
  const floorTick = (p, etf) => { const t = tickSize(p, etf); return r2(Math.floor(p / t + 1e-6) * t); };
  const ceilTick = (p, etf) => { const t = tickSize(p, etf); return r2(Math.ceil(p / t - 1e-6) * t); };
  const fmtPx = (p) => (p == null ? '—' : p < 100 ? p.toFixed(2) : p < 1000 ? p.toFixed(1) : p.toFixed(0));

  function tpe() {
    const s = new Date().toLocaleString('sv-SE', { timeZone: 'Asia/Taipei', hour12: false });
    return { date: s.slice(0, 10), hm: s.slice(11, 16), min: +s.slice(11, 13) * 60 + +s.slice(14, 16) };
  }
  function tradePnl(entry, exit, lots, disc, etf, daytrade) {
    const buy = entry * 1000 * lots, sell = exit * 1000 * lots;
    const fb = Math.max(MIN_FEE, Math.floor(buy * FEE_RATE * disc));
    const fs = Math.max(MIN_FEE, Math.floor(sell * FEE_RATE * disc));
    const tax = Math.floor(sell * (etf ? TAX_ETF : daytrade ? TAX_DT : TAX_STOCK));
    return sell - buy - fb - fs - tax;
  }
  function levels(entry, op, vw, low, etf, limUp) {
    const t = tickSize(entry, etf);
    const refs = [op, vw].filter((x) => x && x <= entry);
    const base = refs.length ? Math.max(...refs) : (low || entry);
    let stop = floorTick(base - t * 0.999, etf);
    const dist = (entry - stop) / entry;
    if (dist < STOP_MIN) stop = floorTick(entry * (1 - STOP_MIN), etf);
    else if (dist > STOP_MAX) stop = ceilTick(entry * (1 - STOP_MAX), etf);
    if (stop >= entry) stop = floorTick(entry - t * 0.999, etf);
    let target = floorTick(entry + RR * (entry - stop), etf);
    if (limUp && target >= limUp) target = floorTick(limUp - tickSize(limUp, etf) * 0.999, etf);
    return [stop, Math.max(target, entry)];
  }

  let mine = null;
  function loadMine() {
    if (mine) return mine;
    try { mine = JSON.parse(localStorage.getItem(MKEY) || '{}'); } catch (e) { mine = {}; }
    mine = Object.assign({ positions: [], trades: [], alerts: [], akeys: [], day: null, seq: 0 }, mine || {});
    return mine;
  }
  function saveMine() { try { localStorage.setItem(MKEY, JSON.stringify(mine)); } catch (e) { /* 無痕模式存不了 */ } }
  function newDay(today) {
    if (mine.day !== today) { mine.day = today; mine.alerts = []; mine.akeys = []; mine.seq = 0; }
  }

  let disc = 0.28, lastCands = [];
  async function quotes() {
    try { return (await file('quotes.json')) || { q: {} }; } catch (e) { return { q: {} }; }
  }
  // 10 秒報價比每分鐘的 quotes.json 新，就用 10 秒的
  function quoteOf(q, code) {
    const x = q.q[code], t = tickFor(code);
    if (t && (!x || !hms(x[3]) || t[3] > x[3])) return [t[0], t[1], t[2], t[3], x ? x[4] : null, t[5], t[4], x ? x[7] : null, x ? x[8] : 0, x ? x[9] : code];
    return x;
  }
  function posView(p, q, today) {
    const x = quoteOf(q, p.code), v = Object.assign({}, p);
    if (x && x[0]) {
      const price = x[0], pnl = tradePnl(p.entry, price, p.lots, disc, p.etf, p.date === today);
      Object.assign(v, {
        price, est: !!x[2], pnl: Math.round(pnl), ret: r2(pnl / (p.entry * 1000 * p.lots) * 100),
        chgp: x[1] ? r2((price / x[1] - 1) * 100) : null, quote_time: x[3],
        state: p.stop && price <= p.stop ? 'stop' : p.target && price >= p.target ? 'target' : 'hold',
      });
    }
    return v;
  }
  function summary(trades) {
    const n = trades.length;
    if (!n) return { n: 0 };
    const pnl = trades.reduce((a, t) => a + t.pnl, 0);
    return { n, win: trades.filter((t) => t.pnl > 0).length, pnl: Math.round(pnl), avg_ret: r2(trades.reduce((a, t) => a + t.ret, 0) / n) };
  }
  function alert(kind, key, title, body, code) {
    if (mine.akeys.includes(key)) return;
    mine.akeys.push(key);
    mine.seq += 1;
    const now = tpe();
    mine.alerts.push({ id: `L${now.date}-${mine.seq}`, ts: Date.now() / 1000, time: now.hm, kind, code, title, body });
    mine.alerts = mine.alerts.slice(-30);
    saveMine();
  }
  // 持倉碰到停損／目標價、收盤前還有部位：網頁開著時提醒（和電腦版相同的文字）
  function checkAlerts(q) {
    const now = tpe();
    newDay(now.date);
    if (q.date !== now.date) return;
    const todays = [];
    for (const p of mine.positions) {
      if (p.date === now.date) todays.push(p.name);
      const x = quoteOf(q, p.code), price = x && x[0];
      if (!price) continue;
      if (p.stop && price <= p.stop) {
        alert('stop', `stop:${p.id}`, `停損：${p.name} ${p.code} 跌到 ${fmtPx(price)}`,
          `已碰到停損價 ${fmtPx(p.stop)}（進場 ${fmtPx(p.entry)} × ${p.lots} 張），建議出場。`, p.code);
      } else if (p.target && price >= p.target) {
        alert('target', `target:${p.id}`, `到目標價：${p.name} ${p.code} ${fmtPx(price)}`,
          `已到目標價 ${fmtPx(p.target)}（進場 ${fmtPx(p.entry)} × ${p.lots} 張），可以獲利了結。`, p.code);
      }
    }
    if (todays.length) {
      [['close:1310', 13 * 60 + 10, '13:25 前'], ['close:1320', 13 * 60 + 20, '馬上']].forEach(([key, t0, txt]) => {
        if (now.min >= t0 && now.min < 14 * 60) {
          alert('close', key, '收盤前提醒：還有當沖部位沒出場', `${todays.join('、')} 還沒賣出，請${txt}出場（13:25 後只剩最後一盤集合競價）。`);
        }
      });
    }
  }
  function record(code, name, etf, lots, buy, sell, date, buyTime, sellTime, daytrade, extra) {
    const pnl = tradePnl(buy, sell, lots, disc, etf, daytrade);
    const t = Object.assign({ id: 't' + Date.now(), date, code, name, etf, lots, buy, sell, buy_time: buyTime, sell_time: sellTime,
      daytrade, pnl: Math.round(pnl), ret: r2(pnl / (buy * 1000 * lots) * 100) }, extra || {});
    mine.trades.push(t);
    mine.trades.sort((a, b) => (a.date + (a.sell_time || '')).localeCompare(b.date + (b.sell_time || '')));
    saveMine();
    return t;
  }
  async function action(d) {
    loadMine();
    const now = tpe(), num = (v) => parseFloat(v), find = (id) => mine.positions.find((p) => p.id === id);
    newDay(now.date);
    switch (d.action) {
      case 'buy': case 'log': {
        const code = String(d.code || '').trim().toUpperCase();
        const q = await quotes(), x = q.q[code];
        if (!x) return { error: `找不到股票代號 ${code}` };
        const etf = !!x[8], name = x[9] || code;
        if (d.action === 'log') {
          const buy = num(d.buy), sell = num(d.sell), lots = parseInt(d.lots, 10), date = String(d.date || now.date);
          if (!(buy > 0 && sell > 0 && lots >= 1 && lots <= 9999)) return { error: '買價、賣價與張數需大於 0' };
          if (!/^\d{4}-\d{2}-\d{2}$/.test(date) || date > now.date) return { error: '日期格式不正確' };
          return { ok: true, trade: record(code, name, etf, lots, buy, sell, date, null, null, d.daytrade !== false, { manual: true }) };
        }
        const entry = num(d.price), lots = parseInt(d.lots, 10);
        if (!(entry > 0) || !(lots >= 1 && lots <= 9999)) return { error: '買進價與張數需大於 0' };
        const c = lastCands.find((y) => y.code === code) || {};
        let stop = num(d.stop), target = num(d.target);
        if (!stop || !target) {
          const [st, tg] = levels(entry, c.open || x[4], c.vwap, c.low || x[5], etf, x[7]);
          stop = stop || st; target = target || tg;
        }
        if (!(stop < entry && entry < target)) return { error: '停損價要低於買進價，目標價要高於買進價' };
        const p = { id: 'p' + Date.now(), code, name, etf, date: now.date, time: now.hm, entry, lots, stop, target,
          score: c.score ?? null, rank: c.rank ?? null };
        mine.positions.push(p);
        saveMine();
        return { ok: true, position: p };
      }
      case 'edit': {
        const p = find(d.id);
        if (!p) return { error: '找不到這筆持倉' };
        const stop = num(d.stop), target = num(d.target);
        if (!(stop > 0 && stop < target)) return { error: '停損價要低於目標價' };
        Object.assign(p, { stop, target });
        mine.akeys = mine.akeys.filter((k) => k !== `stop:${p.id}` && k !== `target:${p.id}`);   // 改了價位就允許重新提醒
        saveMine();
        return { ok: true };
      }
      case 'cancel': {
        const p = find(d.id);
        if (!p) return { error: '找不到這筆持倉' };
        mine.positions = mine.positions.filter((y) => y !== p);
        saveMine();
        return { ok: true };
      }
      case 'sell': {
        const p = find(d.id), price = num(d.price);
        if (!p) return { error: '找不到這筆持倉' };
        if (!(price > 0)) return { error: '賣出價需大於 0' };
        const t = record(p.code, p.name, p.etf, p.lots, p.entry, price, now.date, p.time, now.hm, p.date === now.date,
          { buy_date: p.date, score: p.score, rank: p.rank });
        mine.positions = mine.positions.filter((y) => y !== p);
        saveMine();
        return { ok: true, trade: t };
      }
      case 'del_trade': {
        const n = mine.trades.length;
        mine.trades = mine.trades.filter((t) => t.id !== d.id);
        if (mine.trades.length === n) return { error: '找不到這筆交易' };
        saveMine();
        return { ok: true };
      }
      default:
        return notHere('雲端版的當沖額度、停損金額與推播設定在雲端統一設定，要修改請跟 Claude 說。');
    }
  }
  async function withMine(d, q) {
    loadMine();
    const today = tpe().date;
    newDay(today);
    if (mine.positions.length) checkAlerts(q || await quotes());
    return today;
  }

  async function api(path, opts) {
    const method = ((opts && opts.method) || 'GET').toUpperCase();
    const u = new URL(path, location.href), p = u.pathname;
    if (method !== 'GET') {
      if (p === '/api/daytrade') {
        let body = {};
        try { body = JSON.parse((opts && opts.body) || '{}'); } catch (e) { /* ignore */ }
        return action(body);
      }
      return notHere('雲端版不能在網頁上修改設定或重新掃描。');
    }
    const code = (u.searchParams.get('code') || '').trim().toUpperCase();
    switch (p) {
      case '/api/status': {
        const [st, m] = await Promise.all([file('status.json'), getMeta()]);
        if (!st) throw new Error('雲端還沒有資料');
        st.cloud = m ? { published: m.published, ts: m.ts, every: m.every, final: m.final } : null;
        await withMine(st);
        st.alerts = (st.alerts || []).concat(mine.alerts.slice(-6));
        return st;
      }
      case '/api/top': return patch((await file('top.json')) || { pending: true, mode: 'close' }, 0);
      case '/api/market': return patchMarket((await file('market.json')) || { rows: [], pending: true });
      case '/api/review': return (await file('review.json')) || { pending: true };
      case '/api/daytrade': {
        const d = (await file('daytrade.json')) || { live: false, cands: [], positions: [], alerts: [], trades: [], settings: {}, rules: {}, market: {}, snaps: {} };
        disc = (d.settings && d.settings.fee_discount) || disc;
        patch(d.cands, 0);
        lastCands = d.cands || [];
        loadMine();
        const q = mine.positions.length ? await quotes() : { q: {} };
        const today = await withMine(d, q);
        d.positions = mine.positions.map((x) => posView(x, q, today));
        const todays = mine.trades.filter((t) => t.date === today);
        d.trades = todays.slice().reverse();
        d.trades_summary = summary(todays);
        d.alerts = (d.alerts || []).concat(mine.alerts).sort((a, b) => b.ts - a.ts).slice(0, 30);
        return d;
      }
      case '/api/daytrade/review': {
        const d = await file('daytrade_review.json');
        if (!d) return null;
        loadMine();
        disc = (d.settings && d.settings.fee_discount) || disc;
        d.trades = mine.trades.slice().reverse();
        d.trades_summary = summary(mine.trades);
        d.settings = Object.assign({ min_fee: MIN_FEE }, d.settings || {});
        return d;
      }
      case '/api/diag': return (await file('diag.json')) || { sources: {} };
      case '/api/stock': {
        const m = await getMeta();
        if (m && m.stocks && !m.stocks.includes(code)) {
          return notHere('雲端版沒有這檔股票的分析：只準備有足夠歷史資料與成交量的股票。');
        }
        return patch(await file(`stock/${code}.json`), 0) || notHere('這檔股票的資料還沒產生，請稍後再試。');
      }
      case '/api/stock/tf': {
        const tf = u.searchParams.get('tf') || 'd';
        const m = await getMeta();
        const has = m && m.tf && m.tf.includes(code);
        const d = has ? await file(`tf/${code}-${tf === '60m' ? '60m' : '30m'}.json`) : null;
        if (d) return d;
        return { tf, mtf: [], error: '雲端版的 30 分／60 分 K 只提供名單上的股票' };
      }
      case '/api/pricecheck': return notHere('雲端版不提供核對報價。');
      default: throw new Error('not found');
    }
  }

  return { api, file, meta: getMeta, relay };
})();
