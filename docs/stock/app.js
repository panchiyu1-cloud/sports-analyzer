'use strict';
/* 臺股強勢掃描 —— 前端（原生 JavaScript，無外部套件） */
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => Array.from(el.querySelectorAll(s));
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

// 雲端版（GitHub Pages）：資料改從 cloud.js 讀；電腦版：問本機程式
const CLOUD = !!window.TWSCAN_CLOUD;
const NO_LINK = CLOUD ? '無法讀取雲端資料' : '無法連線到本機程式';
async function api(path, opts) {
  if (CLOUD) return window.TWSCAN_CLOUD.api(path, opts);
  const r = await fetch(path, Object.assign({ cache: 'no-store' }, opts || {}));
  if (!r.ok && r.status !== 500) throw new Error('HTTP ' + r.status);
  return r.json();
}

const S = {
  status: null, view: null, charts: [], timers: [], topGen: null, topLoaded: 0,
  market: null, mf: { q: '', mkt: '', type: 'stock', ind: '', sort: 'value', dir: -1, page: 0, scored: false },
  stock: { code: null, sub: 'macd', bb: false, data: null, chart: null, tf: null, tfd: null, tfLoaded: 0, ranges: { d: 120, '30m': 45, '60m': 50 } },
};
try { Object.assign(S.mf, JSON.parse(localStorage.getItem('twscan.mf') || '{}'), { page: 0, q: '' }); } catch (e) { /* 無痕模式等情況 */ }
const saveMf = () => { try { localStorage.setItem('twscan.mf', JSON.stringify(S.mf)); } catch (e) { /* ignore */ } };

// ---------------------------------------------------------------- 格式
const clsOf = (v) => (v == null ? '' : v > 0 ? 'up' : v < 0 ? 'down' : 'flat');
const arrow = (v) => (v == null ? '' : v > 0 ? '▲' : v < 0 ? '▼' : '－');
const pct = (v, d = 2) => (v == null ? '—' : (v > 0 ? '+' : '') + Number(v).toFixed(d) + '%');
const fmtInt = (n) => (n == null || !isFinite(n) ? '—' : Math.round(n).toLocaleString('zh-TW'));
const signed = (n) => (n == null ? '—' : (n > 0 ? '+' : '') + fmtInt(n));
const fastMode = () => S.status && ['open', 'preopen'].includes(S.status.market_phase);
function ago(ts) {
  if (!ts) return '';
  const s = Date.now() / 1000 - ts;
  if (s < 3600) return Math.max(1, Math.round(s / 60)) + ' 分鐘前';
  if (s < 86400) return Math.round(s / 3600) + ' 小時前';
  return Math.round(s / 86400) + ' 天前';
}
const gradeCls = (s) => (s >= 70 ? 'g-strong' : s >= 50 ? 'g-bull' : s >= 30 ? 'g-mid' : 'g-weak');
const scCls = (s) => (s == null ? '' : s >= 70 ? 's70' : s >= 50 ? 's50' : s >= 30 ? 's30' : '');
function ring(score) {
  const r = 22, c = 2 * Math.PI * r, v = Math.max(0, Math.min(100, score || 0));
  const col = score >= 50 ? 'var(--up)' : score >= 30 ? 'var(--warn)' : 'var(--muted)';
  return `<div class="ring" role="img" aria-label="評分 ${score}"><svg width="52" height="52"><circle cx="26" cy="26" r="${r}" fill="none" stroke="var(--surface-3)" stroke-width="5"/><circle cx="26" cy="26" r="${r}" fill="none" stroke="${col}" stroke-width="5" stroke-linecap="round" stroke-dasharray="${(c * v) / 100} ${c}"/></svg><b>${score ?? '—'}</b></div>`;
}
function tagCls(s) { return s.key === 'wash' ? 'wash' : s.key === 'dist' ? 'dist' : s.kind; }
function sentiLabel(s) { return s > 0.15 ? ['p', '偏多'] : s < -0.15 ? ['n', '偏空'] : ['z', '中性']; }
function newsItem(it, withCodes) {
  const [c, l] = it.s > 0 ? ['p', '利多'] : it.s < 0 ? ['n', '利空'] : ['z', '中性'];
  const codes = withCodes && it.codes && it.codes.length ? ' · ' + it.codes.map((x) => `<a href="#stock-${x}">${x}</a>`).join(' ') : '';
  const title = it.link ? `<a href="${esc(it.link)}" target="_blank" rel="noopener">${esc(it.title)}</a>` : esc(it.title);
  return `<li><span class="senti ${c}">${l}</span>${title}<div class="nmeta">${esc(it.source || '')} · ${ago(it.ts)}${codes}</div></li>`;
}
function levelsOf(c) {
  const L = c.levels || {}, out = [];
  if (L.resistance) out.push({ price: L.resistance, label: '壓力 ' + fmtP(L.resistance), color: 'rgba(255,91,87,.75)' });
  if (L.support) out.push({ price: L.support, label: '支撐 ' + fmtP(L.support), color: 'rgba(47,191,113,.8)' });
  return out;
}
function chipsSum(chips) {
  if (!chips || !chips.length) return null;
  return chips.reduce((a, x) => a + (x.total || 0), 0) / 1000;
}

// ---------------------------------------------------------------- 狀態列
const PHASE = { open: ['盤中即時', 'live'], preopen: ['開盤前試撮', 'live'], post: ['盤後', 'closed'], closed: ['已收盤', 'closed'] };
async function pollStatus() {
  try {
    const st = await api('/api/status');
    S.status = st;
    dtIngest(st.alerts);
    $('#demoBadge').hidden = !st.demo;
    const [txt, cls] = PHASE[st.market_phase] || ['—', ''];
    $('#phaseText').textContent = txt;
    $('#phaseDot').className = 'dot ' + cls;
    $('#clock').textContent = st.now.slice(11, 16);
    if (CLOUD) cloudAge(st);
    const busy = st.scanning && st.phase !== 'idle';
    $('#scanbar').hidden = !busy;
    if (busy) {
      $('#scanText').textContent = st.msg + (st.first_scan_done ? '' : '（首次啟動需下載歷史資料，約 3–8 分鐘，完成後會自動顯示）');
      const f = $('#scanFill');
      if (st.total > 0) { f.classList.remove('indet'); f.style.width = Math.round((st.done / st.total) * 100) + '%'; }
      else f.classList.add('indet');
    }
    if (S.view === 'top') { renderIndexRow(st); renderModel(st); renderMarketNews(st); }
  } catch (e) {
    $('#phaseText').textContent = NO_LINK;
    $('#phaseDot').className = 'dot';
  }
}

// ---------------------------------------------------------------- 雲端版：資料新鮮度
function cloudAge(st) {
  const c = st.cloud, box = $('#statusBox');
  let el = $('#cloudAge');
  if (!el) { box.insertAdjacentHTML('beforeend', '<span id="cloudAge" class="cloud-age"></span>'); el = $('#cloudAge'); }
  const min = c && c.ts ? Math.floor((Date.now() / 1000 - c.ts) / 60) : null;
  const liveish = ['open', 'preopen'].includes(st.market_phase);
  if (liveish && min != null && min >= 4) {
    el.textContent = `・資料延遲 ${min} 分鐘`;
    el.className = 'cloud-age late';
    if (min >= 15) { $('#phaseText').textContent = '雲端更新中斷'; $('#phaseDot').className = 'dot'; }
  } else {
    el.textContent = '';
    el.className = 'cloud-age';
  }
  box.title = c && c.published ? `雲端資料更新時間：${c.published.slice(0, 16).replace('T', ' ')}` : '資料狀態';
}
function cloudInfoHTML() {
  const c = (S.status && S.status.cloud) || {};
  return `<h3 style="margin-top:18px">雲端版</h3>
    <div class="muted small" style="margin-top:0">這個網頁不需要開電腦：程式在 GitHub 的雲端主機上執行，交易日 08:05～14:10 每分鐘把最新結果推上來（盤中報價每幾秒讀一次證交所即時資料），16:45、19:15 再更新三大法人、收盤確認名單與復盤。
      最後更新：<b class="num">${c.published ? esc(c.published.slice(0, 16).replace('T', ' ')) : '—'}</b>。GitHub 排程偶爾會晚幾分鐘啟動；盤中若頂端顯示「資料延遲」，代表雲端這一輪還沒接上。
      雲端版只能看，持倉與交易紀錄請記在券商 App；當沖「可進場」提醒會推播到手機（ntfy）。</div>`;
}

// ---------------------------------------------------------------- 路由
function clearView() {
  S.charts.forEach((c) => c.destroy());
  S.charts = [];
  S.timers.forEach(clearInterval);
  S.timers = [];
  if (S.stock.chart) { S.stock.chart.destroy(); S.stock.chart = null; }
  if (S.rvChart) { S.rvChart.destroy(); S.rvChart = null; }
  $('#tooltip').hidden = true;
}
function setTab(t) { $$('.tabs a').forEach((a) => a.classList.toggle('active', a.dataset.tab === t)); }
function route() {
  const h = decodeURIComponent(location.hash.slice(1)) || 'top';
  clearView();
  window.scrollTo(0, 0);
  if (h.startsWith('stock-')) { setTab(''); showStock(h.slice(6).toUpperCase()); }
  else if (h === 'market') { setTab('market'); showMarket(); }
  else if (h === 'review') { setTab('review'); showReview(); }
  else if (h === 'about') { setTab('about'); showAbout(); }
  else { setTab('top'); showTop(); }
}
window.addEventListener('hashchange', route);

// ================================================================ 首頁：強勢前十
const md = (iso) => (iso ? iso.slice(5).replace('-', '/') : '');
// 報價來源：成交價／推估價／官方收盤
const EST_TIP = '推估價：證交所這一刻沒有揭示成交價，依最佳一檔買賣價推估，誤差通常在一檔以內';
const estMark = (est) => (est ? `<span class="est-tag" title="${EST_TIP}">估</span>` : '');
function quoteSrc(c) {
  const hms = (t) => (t || '').slice(0, 8);
  if (c.src === 'daily') return ['ok', `${md(c.date)} 收盤價（證交所／櫃買中心官方收盤資料）`];
  if (c.src === 'close') return ['ok', `${md(c.date)} 收盤價`];
  if (c.no_trade) return ['na', '今天尚未成交，顯示的是平盤參考價'];
  if (c.src === 'mis' && c.est) return ['est', `推估價（${hms(c.time)}）：這一刻證交所沒有揭示成交價，依最佳一檔買賣價推估` + (c.lt ? `；最後確認的成交價 ${fmtP(c.lt)}（${hms(c.lt_t)}）` : '')];
  if (c.src === 'mis') return ['ok', `成交價（證交所即時，${hms(c.lt_t || c.time)}）`];
  return ['na', ''];
}
const stIcon = (k) => ({ bull: '✓', warn: '⚠', bear: '✕' }[k] || '·');

function showTop() {
  S.view = 'top';
  S.cardsKey = null;
  $('#view').innerHTML = `
    <section class="section"><div class="index-row" id="idxRow"></div></section>
    <section class="section">
      <div class="section-head"><h2 id="listTitle">最有可能上漲的 10 檔</h2><span class="sub" id="topSub"></span></div>
      <div class="listswitch" id="listSwitch"></div>
      <div id="modeNote" class="mode-note" hidden></div>
      <div id="trackBox"></div>
      <div id="topCards" class="cards"></div>
    </section>
    <section class="section" id="alertSec"></section>
    <section class="section grid2">
      <div class="panel" id="modelPanel"></div>
      <div class="panel" id="newsPanel"></div>
    </section>`;
  if (S.status) { renderIndexRow(S.status); renderModel(S.status); renderMarketNews(S.status); }
  loadTop();
  S.timers.push(setInterval(() => { if (fastMode() || Date.now() - S.topLoaded > 120000) loadTop(); }, 20000));
  S.timers.push(setInterval(() => { if (S.listMode === 'dt' && (fastMode() || Date.now() - (DT.loaded || 0) > 60000)) dtLoad(); }, 10000));
}

async function loadTop() {
  let d;
  try { d = await api('/api/top'); } catch (e) { return; }
  S.topLoaded = Date.now();
  if (S.view !== 'top') return;
  S.top = d;
  renderTop();
}

function renderIndexRow(st) {
  const row = $('#idxRow');
  if (!row) return;
  const card = (k) => {
    const x = st.index[k] || {};
    const c = clsOf(x.chgp);
    return `<div class="idx"><div><div class="lbl">${x.name || k}</div><div class="val num ${c}">${x.price ? x.price.toLocaleString('zh-TW', { maximumFractionDigits: 2 }) : '—'}</div>
      <div class="chg num ${c}">${arrow(x.chgp)} ${pct(x.chgp)}${x.time ? ` <span class="muted">${x.time.slice(0, 5)}</span>` : ''}</div></div><canvas data-spark="${k}" aria-hidden="true"></canvas></div>`;
  };
  const rg = { bull: ['多頭格局', '加權指數站上月線且月線高於季線', 'up'], bear: ['偏弱格局', '指數跌破下彎的月線，訊號可靠度降低', 'down'], neutral: ['盤整格局', '指數在均線間整理', 'flat'] }[st.regime] || ['資料不足', '等待大盤歷史資料', 'flat'];
  const b = st.breadth || { up: 0, down: 0, flat: 0 }, tot = b.up + b.down + b.flat || 1;
  row.innerHTML = card('TAIEX') + card('TPEX') +
    `<div class="idx"><div><div class="lbl">大盤狀態</div><div class="val ${rg[2]}">${rg[0]}</div><div class="chg muted">${rg[1]}</div></div></div>
     <div class="idx" style="flex-direction:column;gap:0"><div class="lbl">上市櫃個股漲跌家數</div>
      <div class="breadth"><span style="width:${(b.up / tot) * 100}%;background:var(--up)"></span><span style="width:${(b.flat / tot) * 100}%;background:var(--flat)"></span><span style="width:${(b.down / tot) * 100}%;background:var(--down)"></span></div>
      <div class="chg num"><span class="up">▲ 漲 ${b.up}</span>　<span class="flat">平 ${b.flat}</span>　<span class="down">▼ 跌 ${b.down}</span></div></div>`;
  $$('canvas[data-spark]', row).forEach((cv) => {
    const x = st.index[cv.dataset.spark] || {};
    sparkline(cv, x.spark, getComputedStyle(document.documentElement).getPropertyValue(x.chgp < 0 ? '--down' : '--up').trim());
  });
}

function cardHTML(c, mode, closeRank) {
  const chg = c.price && c.prev ? c.price - c.prev : null;
  const cl = clsOf(c.chgp);
  const extra = [];
  if (mode === 'live') {
    extra.push(closeRank[c.code] ? `<span class="tag keep">昨日收盤名單第 ${closeRank[c.code]} 名</span>`
      : '<span class="tag new">新進榜・待收盤確認</span>');
  }
  const tags = extra.join('') + c.signals.filter((s) => s.pts !== 0).slice(0, 7)
    .map((s) => `<span class="tag ${tagCls(s)}">${s.pts > 0 ? '▲' : '▼'} ${esc(s.label)}</span>`).join('');
  const pos = c.signals.filter((s) => s.pts > 0).reduce((a, s) => a + s.pts, 0);
  const neg = c.signals.filter((s) => s.pts < 0).reduce((a, s) => a + s.pts, 0);
  const facts = [];
  if (c.flow && c.flow.total > 0) facts.push(`外盤比 <b>${Math.round((c.flow.outer / c.flow.total) * 100)}%</b>`);
  if (c.vol_est) facts.push(`預估全日量 <b>${fmtInt(c.vol_est.est)} 張</b>（目前 ${fmtInt(c.vol_est.actual)}）`);
  const cs = chipsSum(c.chips);
  if (cs != null) facts.push(`法人近 ${c.chips.length} 日 <b class="${clsOf(cs)}">${signed(cs)} 張</b>`);
  if (c.news && c.news.n) { const [, l] = sentiLabel(c.news.score); facts.push(`新聞 <b>${c.news.n} 則・${l}</b>`); }
  if (c.levels && c.levels.atr_pct) facts.push(`日均波動 <b>${c.levels.atr_pct}%</b>`);
  const L = c.levels || {};
  const lv = [L.support && `支撐 <b class="num">${fmtP(L.support)}</b>`, L.resistance && `壓力 <b class="num">${fmtP(L.resistance)}</b>`,
    L.invalid && `跌破 <b class="num">${fmtP(L.invalid)}</b> 訊號失效（參考）`].filter(Boolean).join('');
  const bt = c.backtest;
  const btTxt = bt && bt.n >= 5
    ? `歷史驗證：此股過去一年評分 ≥ ${bt.threshold} 共 <b>${bt.n}</b> 次，10 個交易日後上漲 <b>${bt.win10}%</b>（平均 ${pct(bt.avg10, 1)}）；同期任意一天買進的上漲機率為 ${bt.base_win10}%。`
    : bt ? `歷史驗證：此股過去一年高分訊號僅 ${bt.n} 次，樣本不足，參考價值有限。` : '';
  const news = c.news && c.news.items && c.news.items.length
    ? `<div class="news-mini">${c.news.items.slice(0, 3).map((it) => `<div><span class="senti ${it.s > 0 ? 'p' : it.s < 0 ? 'n' : 'z'}">${it.s > 0 ? '利多' : it.s < 0 ? '利空' : '中性'}</span>${it.link ? `<a href="${esc(it.link)}" target="_blank" rel="noopener">${esc(it.title)}</a>` : esc(it.title)} <span class="muted">${ago(it.ts)}</span></div>`).join('')}</div>` : '';
  const tm = mode === 'close' ? `${md(c.date)} 收盤` : `${(c.time || '').slice(0, 5)} ${c.est ? '推估' : c.no_trade ? '未成交' : '即時'}`;
  const badge = mode === 'close' ? `<span class="mbadge confirm">收盤確認・${md(c.date)}</span>` : `<span class="mbadge live">盤中暫定・${(c.time || '').slice(0, 5)}</span>`;
  return `<article class="card ${mode}" data-code="${c.code}">
    <div class="card-head">
      <div class="rank r${c.rank}">${c.rank}</div>
      <div class="title"><a href="#stock-${c.code}"><span class="nm">${esc(c.name)}</span><span class="cd">${c.code}</span></a>
        <div class="meta">${esc(c.industry || '—')} · ${c.market === 'TPEx' ? '上櫃' : '上市'}</div>${badge}</div>
      <div class="quote"><div class="price num ${cl}">${mode === 'close' ? '' : estMark(c.est)}${fmtP(c.price)}</div>
        <div class="chg num ${cl}">${arrow(c.chgp)} ${chg == null ? '' : fmtP(Math.abs(chg))} (${pct(c.chgp)})</div><div class="tm">${tm}</div></div>
    </div>
    ${mode === 'close' ? `<div class="today" data-today="${c.code}" hidden></div>` : ''}
    <div class="score-row">${ring(c.score)}<div><div class="grade ${gradeCls(c.score)}">${c.grade || ''}${mode === 'live' ? '<span class="muted" style="font-weight:400;font-size:12px">（暫定）</span>' : ''}</div>
      <div class="facts">${facts.join('')}</div></div></div>
    <div class="chart-wrap" data-chart="${c.code}" aria-label="${esc(c.name)} 近 6 個月 K 線圖"></div>
    <div class="tags">${tags}</div>
    <details data-code="${c.code}"><summary>評分拆解：多方 +${pos}　空方 ${neg}　＝ ${c.score} 分</summary>
      <ul class="sig-list">${c.signals.slice().sort((a, b) => b.pts - a.pts).map((s) => `<li><span>${esc(s.label)}</span><span class="pts num ${clsOf(s.pts)}">${s.pts > 0 ? '+' + s.pts : s.pts === 0 ? '—' : s.pts}</span></li>`).join('')}</ul>
    </details>
    ${lv ? `<div class="levels">${lv}</div>` : ''}
    ${news}
    ${btTxt ? `<div class="bt">${btTxt}</div>` : ''}
    <div class="card-foot"><span class="muted" style="font-size:12px">▲ 紅三角＝多方訊號日　虛線＝支撐／壓力</span><a href="#stock-${c.code}">完整分析 →</a></div>
  </article>`;
}

function renderEmptyTop(box) {
  const st = S.status || {};
  box.innerHTML = `<div class="panel empty" style="grid-column:1/-1">${st.first_scan_done && !st.universe
    ? '<div class="big">無法取得行情資料</div>請確認電腦有連上網路；到 <a href="#about" style="color:var(--accent)">模型與資料</a> 頁可以看到是哪個資料來源失敗，程式會定時自動重試。'
    : st.first_scan_done
    ? '<div class="big">目前沒有符合條件的股票</div>可到「模型與資料」頁檢查資料來源狀態。'
    : '<div class="big">正在進行第一次全市場掃描…</div>首次啟動需要下載約 700 檔股票一年的日 K 線（約 3–8 分鐘），完成後這裡會自動出現結果。<br>之後再開啟只需更新當天資料，幾秒內就會完成。'}</div>`;
}

function renderTop() {
  const d = S.top, box = $('#topCards');
  if (!d || !box) return;
  if (d.pending || (!d.close && !d.live)) {
    $('#listSwitch').innerHTML = ''; $('#modeNote').hidden = true; $('#trackBox').innerHTML = '';
    S.charts.forEach((c) => c.destroy()); S.charts = []; S.cardsKey = null;
    renderEmptyTop(box);
    return;
  }
  const c = d.close, l = d.live;
  if (!S.listMode) { try { S.listMode = localStorage.getItem('twscan.listMode') || 'close'; } catch (e) { S.listMode = 'close'; } }
  let mode = S.listMode;
  if (mode === 'live' && !l) mode = 'close';
  if (mode === 'close' && !c) mode = 'live';
  const P = mode === 'close' ? c : l;

  $('#listSwitch').innerHTML = `
    <button class="ls ${mode === 'close' ? 'on' : ''}" data-m="close" ${c ? '' : 'disabled'}>
      <span class="ls-badge confirm">收盤確認</span><b>${c ? md(c.date) + ' 收盤後評估' : '尚未產生'}</b>
      <small>${d.mode === 'live' ? '今天盤中固定不變・回測驗證過的用法' : '完整日資料・回測驗證過的用法'}</small></button>
    <button class="ls live ${mode === 'live' ? 'on' : ''}" data-m="live" ${l ? '' : 'disabled'}>
      <span class="ls-badge live">盤中暫定</span><b>${l ? '即時評估 ' + l.generated.slice(11, 16) : '盤中即時名單'}</b>
      <small>${l ? '收盤前會一直變動・未經回測' : '交易時間（09:00–13:30）才會出現'}</small></button>
    <button class="ls dt ${mode === 'dt' ? 'on' : ''}" data-m="dt">
      <span class="ls-badge dt">當沖模式</span><b>${d.mode === 'live' ? '現在能不能進場' : '進場建議與提醒'}</b>
      <small>進場價・停損・目標・張數，符合條件會提醒</small></button>`;
  $$('#listSwitch .ls').forEach((b) => (b.onclick = () => {
    S.listMode = b.dataset.m; S.cardsKey = null;
    try { localStorage.setItem('twscan.listMode', S.listMode); } catch (e) { /* ignore */ }
    renderTop();
  }));

  if (mode === 'dt') {
    $('#listTitle').textContent = '當沖模式：現在能不能進場';
    $('#topSub').textContent = '依你的額度與停損金額，算好進場價、停損價、目標價與張數';
    $('#modeNote').hidden = true;
    renderTrack(null);
    $('#alertSec').innerHTML = '';
    if (S.cardsKey !== 'dt') {
      S.cardsKey = 'dt';
      S.charts.forEach((ch) => ch.destroy());
      S.charts = [];
      box.className = 'dtwrap';
      box.innerHTML = '<div class="panel empty">載入中…</div>';
      if (DT.data) dtRender(true);
      dtLoad(true);
    }
    return;
  }
  box.className = 'cards';

  $('#listTitle').textContent = mode === 'close' ? '收盤確認名單：最有可能上漲的 10 檔' : '盤中暫定名單：目前訊號最強的 10 檔';
  $('#topSub').textContent = `從 ${P.eligible ?? '—'} 檔流動性足夠的股票中評分排序`;
  const note = $('#modeNote');
  note.hidden = false;
  note.className = 'mode-note ' + mode;
  if (mode === 'close') {
    note.innerHTML = d.mode === 'live'
      ? `這份名單是用 <b>${md(c.date)} 收盤後</b>的完整資料（含三大法人、新聞）評估的，今天盤中<b>不會變動</b>。下方「今日盤中追蹤」會即時顯示每檔今天的走勢，協助你判斷進場時機。`
      : `這份名單是用 <b>${md(c.date)} 收盤後</b>的完整資料（含三大法人、新聞）評估的，供<b>下一個交易日</b>參考。下個交易日開盤後它會固定不變，並自動出現盤中追蹤。`;
  } else {
    const done = l.est_frac ? Math.round(l.est_frac * 100) : null;
    note.innerHTML = `依 <b>${l.generated.slice(11, 16)}</b> 的即時價格計算${done ? `，成交量以「預估全日量」換算（目前約完成全日的 ${done}%）` : ''}。<b>收盤前排名會一直變動</b>，而且盤中用法沒有經過回測；新進榜的股票建議等收盤確認後再考慮。`;
  }

  renderTrack(mode === 'close' ? d.track : null);

  const key = mode + '|' + P.generated;
  if (S.cardsKey !== key) {
    S.cardsKey = key;
    const open = new Set($$('.card details[open]').map((x) => x.dataset.code));
    S.charts.forEach((ch) => ch.destroy());
    S.charts = [];
    const closeRank = {};
    (c ? c.top : []).forEach((x) => (closeRank[x.code] = x.rank));
    if (!P.top.length) renderEmptyTop(box);
    else {
      box.innerHTML = P.top.map((x) => cardHTML(x, mode, closeRank)).join('');
      P.top.forEach((x) => {
        const ch = new KChart(box.querySelector(`[data-chart="${x.code}"]`), { height: 230, compact: true, levels: levelsOf(x) });
        ch.setData(x.chart);
        S.charts.push(ch);
      });
      $$('.card details', box).forEach((x) => { if (open.has(x.dataset.code)) x.open = true; });
    }
    renderAlerts(P, mode);
  }
  fillTodayStrips(mode === 'close' && d.track && d.track.mode === 'live' ? d.track : null);
}

function renderTrack(tr) {
  const el = $('#trackBox');
  if (!el) return;
  if (!tr || !tr.rows || !tr.rows.length) { el.innerHTML = ''; return; }
  const sum = tr.summary || {};
  if (tr.mode === 'live') {
    const cnt = sum.counts || {};
    const chips = [['ok', '條件符合', 'bull'], ['chase', '跳空過大', 'warn'], ['fade', '開高走低', 'warn'], ['broken', '跌破失效價', 'bear'],
      ['weak', '走勢偏弱', 'info'], ['watch', '觀察中', 'info'], ['wait', '尚未成交', 'info']]
      .filter(([k]) => cnt[k]).map(([k, t, cl]) => `<span class="st ${cl}">${stIcon(cl)} ${t} ${cnt[k]}</span>`).join('');
    el.innerHTML = `<div class="panel track live-track">
      <div class="track-head"><h3>昨日收盤名單・今日盤中追蹤 <span class="muted">${md(tr.list_date)} 名單 → ${md(tr.trade_date)} ${esc(S.top.now || '')}</span></h3><div class="st-row">${chips}</div></div>
      <div class="table-scroll"><table class="tbl"><thead><tr><th>股票</th><th class="n">開盤跳空</th><th class="n">目前漲跌</th>
        <th class="n hide-sm">外盤比</th><th class="n hide-sm">評分 昨收→盤中</th><th>狀態</th></tr></thead><tbody>
      ${tr.rows.map((r) => `<tr onclick="location.hash='stock-${r.code}'"><td><span class="cd">${r.rank}.</span><span class="nm">${esc(r.name)}</span> <span class="cd hide-sm">${r.code}</span></td>
        <td class="n num ${clsOf(r.gap)}">${pct(r.gap)}</td><td class="n num ${clsOf(r.chgp)}">${r.est ? `<span class="est-approx" title="${EST_TIP}">≈</span>` : ''}${pct(r.chgp)}</td>
        <td class="n num hide-sm">${r.outer != null ? r.outer + '%' : '—'}</td>
        <td class="n num hide-sm">${r.list_score ?? '—'} → ${r.now_score ?? '—'}</td>
        <td><span class="st ${r.kind}">${stIcon(r.kind)} ${esc(r.label)}</span></td></tr>`).join('')}</tbody></table></div>
      <div class="muted small">判斷規則：開盤跳空 ≥ ${S.status?.chase_gap ?? 3}% 且仍在高檔＝不宜追價；開高後跌回開盤價下方＝開高走低；跌破卡片上的「訊號失效價」＝跌破失效價；
      上漲、外盤比 ≥ 50%、盤中評分 ≥ 45＝條件符合。這些只是協助觀察的規則，不是買賣建議。</div></div>`;
  } else {
    el.innerHTML = `<details class="panel track" id="reviewBox" ${S.reviewOpen ? 'open' : ''}>
      <summary><span><b>上一份名單的實際表現</b> <span class="muted">${md(tr.list_date)} 收盤名單 → ${md(tr.trade_date)} 收盤</span></span>
        <span class="st ${sum.avg > 0 ? 'bull' : 'info'}">平均 ${pct(sum.avg)}・上漲 ${sum.up ?? 0}/${sum.n ?? 0}</span></summary>
      <div class="table-scroll"><table class="tbl"><thead><tr><th>股票</th><th class="n">開盤跳空</th><th class="n">收盤漲跌</th><th class="n hide-sm">盤中最高</th><th>結果</th></tr></thead><tbody>
      ${tr.rows.map((r) => `<tr onclick="location.hash='stock-${r.code}'"><td><span class="cd">${r.rank}.</span><span class="nm">${esc(r.name)}</span> <span class="cd hide-sm">${r.code}</span></td>
        <td class="n num ${clsOf(r.gap)}">${pct(r.gap)}</td><td class="n num ${clsOf(r.chgp)}">${pct(r.chgp)}</td>
        <td class="n num hide-sm ${clsOf(r.high_chgp)}">${pct(r.high_chgp)}</td>
        <td><span class="st ${r.kind}">${stIcon(r.kind)} ${esc(r.label)}</span></td></tr>`).join('')}</tbody></table></div>
      <div class="muted small">${tr.reconstructed ? '這份名單是程式事後用歷史資料重建的（當時程式沒有在執行）。' : ''}這裡只看隔一個交易日的漲跌；評分模型評估的是 10 個交易日的表現，單日結果波動大，僅供參考。</div>
    </details>`;
    $('#reviewBox').addEventListener('toggle', (e) => { S.reviewOpen = e.target.open; });
  }
}

function fillTodayStrips(tr) {
  const rows = {};
  ((tr && tr.rows) || []).forEach((r) => (rows[r.code] = r));
  $$('[data-today]').forEach((el) => {
    const r = rows[el.dataset.today];
    if (!r) { el.hidden = true; return; }
    el.hidden = false;
    el.innerHTML = r.status === 'wait'
      ? `<span class="lbl">今日盤中</span><span class="muted">尚未成交</span>`
      : `<span class="lbl">今日盤中 ${esc((r.time || '').slice(0, 5))}</span>
        <span>開盤 <b class="num ${clsOf(r.gap)}">${pct(r.gap)}</b></span><span>目前 <b class="num ${clsOf(r.chgp)}">${pct(r.chgp)}</b></span>
        ${r.outer != null ? `<span>外盤 <b class="num">${r.outer}%</b></span>` : ''}${r.now_score != null ? `<span>盤中評分 <b class="num">${r.now_score}</b></span>` : ''}
        <span class="st ${r.kind}">${stIcon(r.kind)} ${esc(r.label)}</span>`;
  });
}

function renderAlerts(P, mode) {
  const a = $('#alertSec');
  if (!a) return;
  if (P.alerts && P.alerts.length) {
    a.innerHTML = `<div class="section-head"><h2>高檔出貨警示 <span class="muted" style="font-size:14px;font-weight:500">${mode === 'close' ? md(P.date) + ' 收盤評估' : '盤中暫定'}</span></h2><span class="sub">位於高檔且出現多項出貨跡象的股票（已排除在前十名之外）</span></div>
      <div class="panel table-scroll"><table class="tbl"><thead><tr><th>股票</th><th class="n">成交</th><th class="n">漲跌幅</th><th class="n">評分</th><th>出貨跡象</th></tr></thead><tbody>
      ${P.alerts.map((x) => `<tr onclick="location.hash='stock-${x.code}'"><td><span class="cd">${x.code}</span><span class="nm">${esc(x.name)}</span></td>
        <td class="n num">${fmtP(x.price)}</td><td class="n num ${clsOf(x.chgp)}">${pct(x.chgp)}</td><td class="n"><span class="sc ${scCls(x.score)}">${x.score}</span></td>
        <td style="white-space:normal;min-width:260px">${esc(x.reason.replace('疑似高檔出貨：', ''))}</td></tr>`).join('')}</tbody></table></div>`;
  } else a.innerHTML = '';
}

function renderModel(st) {
  const p = $('#modelPanel');
  if (!p) return;
  const m = st.model;
  if (!m || !m.buckets) { p.innerHTML = '<h3>評分模型回測</h3><div class="muted">掃描完成後會用過去一年的資料驗證評分模型。</div>'; return; }
  const base = m.baseline || {};
  const max = Math.max(80, ...m.buckets.map((b) => b.win10 || 0));
  p.innerHTML = `<h3>評分模型回測 <span class="muted" style="font-weight:400;font-size:12.5px">（${m.stocks} 檔、過去一年每 2 個交易日）</span></h3>
    ${m.buckets.map((b) => `<div class="bucket"><span>${b.range} 分</span>
      <div class="bar" title="10 日後上漲機率">${b.n ? `<i style="width:${(b.win10 / max) * 100}%"></i>` : ''}<u style="left:${((base.win10 || 0) / max) * 100}%"></u></div>
      <span class="num">${b.n ? `上漲 ${b.win10}%・均 ${pct(b.avg10, 1)}` : '無樣本'}<br><span class="muted">樣本 ${fmtInt(b.n)} 次</span></span></div>`).join('')}
    <div class="muted" style="font-size:12px;margin-top:8px">藍色長條＝該分數區間出現後 10 個交易日股價上漲的比例；白線＝所有日子平均 ${base.win10 ?? '—'}%。
    只用技術面（不含法人、新聞），且以目前仍熱門的股票回測，結果會偏樂觀，僅供參考。</div>`;
}

function renderMarketNews(st) {
  const p = $('#newsPanel');
  if (!p) return;
  const items = st.market_news || [];
  p.innerHTML = `<h3>即時財經新聞</h3>${items.length ? `<ul class="news">${items.slice(0, 10).map((it) => newsItem(it, true)).join('')}</ul>` : '<div class="muted">尚未取得新聞。</div>'}`;
}

// ================================================================ 全市場行情
function showMarket() {
  S.view = 'market';
  $('#view').innerHTML = `
    <section class="section">
      <div class="section-head"><h2>全市場行情</h2><span class="sub" id="mkSub"></span></div>
      <div class="mk-top">
        <div class="panel" id="breadthPanel"></div>
        <div class="panel"><h3>產業漲跌熱力圖 <span class="muted" style="font-weight:400;font-size:12.5px">方塊越大＝成交值越高；點一下可篩選下表</span></h3><div class="heat" id="heat"></div></div>
      </div>
    </section>
    <section class="section"><div class="panel">
      <div class="filters">
        <input type="search" id="fq" placeholder="搜尋代號或名稱（例：2330、台積電）" value="${esc(S.mf.q)}" aria-label="搜尋股票">
        <select id="fmkt" aria-label="市場"><option value="">上市＋上櫃</option><option value="TWSE">上市</option><option value="TPEx">上櫃</option></select>
        <select id="ftype" aria-label="類型"><option value="stock">股票</option><option value="etf">ETF</option><option value="">全部</option></select>
        <select id="find" aria-label="產業"><option value="">全部產業</option></select>
        <label><input type="checkbox" id="fscored"> 只看有評分</label>
        <span class="muted" id="fcount" style="font-size:13px;margin-left:auto"></span>
      </div>
      <div class="table-scroll"><table class="tbl" id="mkTable"></table></div>
      <div class="pager" id="pager"></div>
    </div></section>`;
  $('#fmkt').value = S.mf.mkt; $('#ftype').value = S.mf.type; $('#fscored').checked = S.mf.scored;
  const upd = () => { S.mf.page = 0; saveMf(); renderMarketTable(); };
  $('#fq').addEventListener('input', (e) => { S.mf.q = e.target.value.trim(); upd(); });
  $('#fmkt').addEventListener('change', (e) => { S.mf.mkt = e.target.value; upd(); });
  $('#ftype').addEventListener('change', (e) => { S.mf.type = e.target.value; upd(); });
  $('#find').addEventListener('change', (e) => { S.mf.ind = e.target.value; upd(); renderHeat(); });
  $('#fscored').addEventListener('change', (e) => { S.mf.scored = e.target.checked; upd(); });
  loadMarket();
  S.timers.push(setInterval(() => { if (fastMode()) loadMarket(); }, 30000));
  S.timers.push(setInterval(() => { if (!fastMode()) loadMarket(); }, 300000));
}

async function loadMarket() {
  let d;
  try { d = await api('/api/market'); } catch (e) { return; }
  if (S.view !== 'market') return;
  const F = {}; d.fields.forEach((f, i) => (F[f] = i));
  S.market = { ...d, F };
  const b = d.breadth, tot = b.up + b.down + b.flat || 1;
  $('#mkSub').textContent = `共 ${d.rows.length} 檔 · 更新於 ${d.generated.slice(11, 16)}` + (d.live ? '（盤中每 1–2 分鐘輪詢報價，「≈」是依買賣價推估的價格；評分為盤中暫定）' : d.close_date ? `（評分為 ${md(d.close_date)} 收盤評估）` : '');
  $('#breadthPanel').innerHTML = `<h3>市場廣度</h3>
    <div class="breadth" style="height:12px"><span style="width:${(b.up / tot) * 100}%;background:var(--up)"></span><span style="width:${(b.flat / tot) * 100}%;background:var(--flat)"></span><span style="width:${(b.down / tot) * 100}%;background:var(--down)"></span></div>
    <dl class="kv" style="margin-top:10px"><dt>上漲</dt><dd class="up num">▲ ${b.up} 檔</dd><dt>平盤</dt><dd class="flat num">${b.flat} 檔</dd><dt>下跌</dt><dd class="down num">▼ ${b.down} 檔</dd>
    <dt>個股成交值</dt><dd class="num">約 ${fmtInt(b.value)} 億元</dd></dl>`;
  const sel = $('#find');
  const cur = S.mf.ind;
  sel.innerHTML = '<option value="">全部產業</option>' + d.industries.map((g) => `<option>${esc(g.name)}</option>`).join('');
  sel.value = cur;
  renderHeat();
  renderMarketTable();
}

function heatColor(chgp) {
  const a = Math.min(Math.abs(chgp || 0) / 3, 1);
  if (Math.abs(chgp || 0) < 0.05) return 'rgba(163,162,155,.22)';
  return chgp > 0 ? `rgba(255,91,87,${0.16 + a * 0.6})` : `rgba(47,191,113,${0.16 + a * 0.55})`;
}
function renderHeat() {
  const d = S.market;
  if (!d) return;
  const total = d.industries.reduce((a, g) => a + (g.value || 0), 0) || 1;
  $('#heat').innerHTML = d.industries.slice(0, 32).map((g) => {
    const grow = Math.max(1, Math.sqrt(g.value / total) * 12);
    return `<div class="tile ${S.mf.ind === g.name ? 'sel' : ''}" style="flex-grow:${grow.toFixed(2)};background:${heatColor(g.chgp)}" data-ind="${esc(g.name)}" title="${esc(g.name)}：${g.n} 檔，漲 ${g.up}／跌 ${g.down}${g.lead ? '，成交值最大：' + esc(g.lead[1]) : ''}">
      <div class="tn">${esc(g.name)}</div><div class="tv num">${arrow(g.chgp)} ${pct(g.chgp)}</div><div class="ts num">${fmtInt(g.value)} 億 · ${g.up}↑ ${g.down}↓</div></div>`;
  }).join('');
  $$('#heat .tile').forEach((t) => t.addEventListener('click', () => {
    S.mf.ind = S.mf.ind === t.dataset.ind ? '' : t.dataset.ind;
    $('#find').value = S.mf.ind; S.mf.page = 0; saveMf(); renderHeat(); renderMarketTable();
    $('#mkTable').scrollIntoView({ behavior: 'smooth', block: 'start' });
  }));
}

function rangeBar(o, h, l, c, prev) {
  if (!h || !l || !c) return '';
  const lo = Math.min(l, prev || l), hi = Math.max(h, prev || h), W = 90, span = hi - lo || 1;
  const x = (p) => 3 + ((p - lo) / span) * (W - 6);
  const op = o || c, col = c >= op ? 'var(--up)' : 'var(--down)';
  return `<svg class="rangebar" width="${W}" height="14" aria-hidden="true"><line x1="${x(l)}" x2="${x(h)}" y1="7" y2="7" stroke="var(--muted)" stroke-width="1.5"/>
    <rect x="${Math.min(x(op), x(c))}" y="3" width="${Math.max(2, Math.abs(x(c) - x(op)))}" height="8" rx="1.5" fill="${col}"/>
    ${prev ? `<line x1="${x(prev)}" x2="${x(prev)}" y1="1" y2="13" stroke="var(--text)" stroke-width="1.5"/>` : ''}</svg>`;
}

const COLS = [
  ['code', '股票', ''], ['price', '成交', 'n'], ['chgp', '漲跌幅', 'n'], ['range', '日內區間', 'hide-sm" title="灰線＝最低到最高，色塊＝開盤到成交，白線＝昨收'],
  ['vol', '成交量(張)', 'n hide-sm'], ['value', '成交值(億)', 'n hide-sm'], ['outer', '外盤比', 'n hide-sm'], ['score', 'SCORE', 'n'],
];
function renderMarketTable() {
  const d = S.market;
  if (!d) return;
  const F = d.F, f = S.mf, q = f.q.toLowerCase();
  let rows = d.rows.filter((r) => (!f.mkt || r[F.market] === f.mkt) && (!f.type || r[F.type] === f.type)
    && (!f.ind || r[F.industry] === f.ind) && (!f.scored || r[F.score] != null)
    && (!q || r[F.code].toLowerCase().includes(q) || r[F.name].toLowerCase().includes(q)));
  const key = f.sort === 'code' ? F.code : F[f.sort];
  rows.sort((a, b) => {
    const x = a[key], y = b[key];
    if (x == null && y == null) return 0;
    if (x == null) return 1;
    if (y == null) return -1;
    return (x < y ? -1 : x > y ? 1 : 0) * f.dir;
  });
  const per = 50, pages = Math.max(1, Math.ceil(rows.length / per));
  f.page = Math.min(f.page, pages - 1);
  const pageRows = rows.slice(f.page * per, f.page * per + per);
  $('#fcount').textContent = `符合 ${rows.length} 檔`;
  const head = COLS.map(([k, t, c]) => {
    const sortable = k !== 'range';
    const on = f.sort === k;
    const label = t === 'SCORE' ? (d.live ? '盤中評分' : '評分') : t;
    return `<th class="${c} ${sortable ? 'th-sort' : ''} ${on ? 'on' : ''}" ${sortable ? `data-k="${k}"` : ''}>${label}${on ? (f.dir > 0 ? ' ↑' : ' ↓') : ''}</th>`;
  }).join('');
  const body = pageRows.map((r) => {
    const cl = clsOf(r[F.chgp]);
    return `<tr data-code="${r[F.code]}">
      <td><span class="cd">${r[F.code]}</span><span class="nm">${esc(r[F.name])}</span> <span class="ind hide-sm">${esc(r[F.industry])}</span></td>
      <td class="n num ${cl}">${r[F.est] ? `<span class="est-approx" title="${EST_TIP}">≈</span>` : ''}${fmtP(r[F.price])}</td>
      <td class="n num ${cl}">${arrow(r[F.chgp])} ${pct(r[F.chgp])}</td>
      <td class="hide-sm">${rangeBar(r[F.open], r[F.high], r[F.low], r[F.price], r[F.prev])}</td>
      <td class="n num hide-sm">${fmtInt(r[F.vol])}</td>
      <td class="n num hide-sm">${r[F.value] != null ? r[F.value].toFixed(2) : '—'}</td>
      <td class="n num hide-sm">${r[F.outer] != null ? r[F.outer] + '%' : '<span class="muted">—</span>'}</td>
      <td class="n">${r[F.score] != null ? `<span class="sc ${scCls(r[F.score])}">${r[F.score]}</span>` : '<span class="muted">—</span>'}</td></tr>`;
  }).join('');
  $('#mkTable').innerHTML = `<thead><tr>${head}</tr></thead><tbody>${body || '<tr><td colspan="8" class="muted" style="text-align:center;padding:30px">沒有符合條件的股票</td></tr>'}</tbody>`;
  $$('#mkTable th[data-k]').forEach((th) => th.addEventListener('click', () => {
    const k = th.dataset.k;
    if (f.sort === k) f.dir = -f.dir; else { f.sort = k; f.dir = k === 'code' ? 1 : -1; }
    saveMf(); renderMarketTable();
  }));
  $$('#mkTable tbody tr[data-code]').forEach((tr) => tr.addEventListener('click', () => { location.hash = 'stock-' + tr.dataset.code; }));
  $('#pager').innerHTML = pages > 1 ? `<button class="btn" id="pPrev" ${f.page === 0 ? 'disabled' : ''}>上一頁</button>
    <span class="num">${f.page + 1} / ${pages}</span><button class="btn" id="pNext" ${f.page >= pages - 1 ? 'disabled' : ''}>下一頁</button>` : '';
  if (pages > 1) {
    $('#pPrev').onclick = () => { f.page--; renderMarketTable(); };
    $('#pNext').onclick = () => { f.page++; renderMarketTable(); };
  }
}

// ================================================================ 個股完整分析
const TF_NAME = { '30m': '30 分', '60m': '60 分', d: '日' };
const TF_RANGES = {
  d: [[60, '3 個月'], [120, '6 個月'], [250, '1 年']],
  '30m': [[18, '2 日'], [45, '5 日'], [90, '10 日'], [0, '1 個月']],
  '60m': [[25, '5 日'], [50, '10 日'], [100, '20 日'], [0, '3 個月']],
};

function showStock(code) {
  S.view = 'stock';
  const st = S.stock;
  st.code = code;
  st.data = null;
  st.tfd = null;
  st.tfLoaded = 0;
  if (!st.tf) { try { st.tf = localStorage.getItem('twscan.tf') || 'd'; } catch (e) { st.tf = 'd'; } }
  if (!TF_RANGES[st.tf]) st.tf = 'd';
  $('#view').innerHTML = `<div class="panel empty"><div class="big">載入 ${esc(code)} 的分析…</div>如果是第一次查看這檔股票，需要幾秒鐘下載歷史資料。</div>`;
  loadStock(code);
  S.timers.push(setInterval(() => { if (fastMode()) loadStock(code); }, 15000));
  S.timers.push(setInterval(() => { if (fastMode() && st.data && Date.now() - st.tfLoaded > 30000) loadTf(code); }, 10000));
}

async function loadStock(code) {
  let c;
  try { c = await api('/api/stock?code=' + encodeURIComponent(code)); } catch (e) {
    $('#view').innerHTML = `<div class="panel empty"><div class="big">${NO_LINK}</div>${CLOUD ? '請檢查網路連線，稍後再試。' : '請確認程式視窗仍在執行。'}</div>`;
    return;
  }
  if (S.view !== 'stock' || S.stock.code !== code) return;
  const first = !S.stock.data;
  S.stock.data = c;
  if (!c.chart) {
    $('#view').innerHTML = `<p><a href="#" onclick="history.back();return false" class="muted">← 返回</a></p><div class="panel empty"><div class="big">${esc(c.name || code)} ${esc(c.code || '')}</div>${esc(c.error || '資料不足')}</div>`;
    return;
  }
  if (first) { renderStock(c); loadTf(code); } else updateStock(c);
}

document.addEventListener('click', (e) => {
  if (e.target && e.target.id === 'btnPriceCheck' && S.stock.code) showPriceCheck(S.stock.code);
});

// ---- 多週期：30 分／60 分／日
async function loadTf(code) {
  const st = S.stock, tf = st.tf;
  st.tfLoaded = Date.now();
  let r;
  try { r = await api(`/api/stock/tf?code=${encodeURIComponent(code)}&tf=${tf}`); } catch (e) { return; }
  if (S.view !== 'stock' || st.code !== code || st.tf !== tf || !r.mtf) return;
  st.tfd = r;
  renderMtf();
  applyStockChart();
}

function setTf(tf) {
  const st = S.stock;
  if (st.tf === tf || !TF_RANGES[tf]) return;
  st.tf = tf;
  try { localStorage.setItem('twscan.tf', tf); } catch (e) { /* ignore */ }
  renderMtf();
  renderRangeSeg();
  applyStockChart();
  if (tf !== 'd' && !(st.tfd && st.tfd.tf === tf && st.tfd.chart)) loadTf(st.code);
}

function renderMtf() {
  const st = S.stock, t = st.tfd, box = $('#mtfRow');
  if (!box) return;
  const arr = { bull: ' ▲', bear: ' ▼', flat: '' };
  const chips = ['30m', '60m', 'd'].map((tf) => {
    const m = t && t.mtf ? t.mtf.find((x) => x.tf === tf) : null;
    return `<button class="mtf-chip ${(m && m.state) || 'na'} ${st.tf === tf ? 'on' : ''}" data-tf="${tf}" aria-pressed="${st.tf === tf}">
      <span class="nm">${TF_NAME[tf]} K</span><b>${m ? esc(m.label) + (arr[m.state] || '') : '…'}</b></button>`;
  }).join('');
  const m = t && t.mtf ? t.mtf.find((x) => x.tf === st.tf) : null;
  const pts = m && m.points && m.points.length ? `<div class="tf-points"><b>${TF_NAME[st.tf]} K 目前${esc(m.label)}：</b>${m.points.map((p) => `<span class="${p.ok ? 'ok' : 'no'}">${p.ok ? '✓' : '✕'} ${esc(p.text)}</span>`).join('')}${m.rsi != null ? `<span class="na">RSI ${m.rsi}</span>` : ''}</div>` : '';
  box.innerHTML = `<div class="mtf-chips">${chips}</div>${t && t.mtf_text ? `<div class="mtf-text">${esc(t.mtf_text)}</div>` : ''}${pts}`;
  $$('#mtfRow .mtf-chip').forEach((b) => (b.onclick = () => setTf(b.dataset.tf)));
}

function renderRangeSeg() {
  const st = S.stock, box = $('#segRange');
  if (!box) return;
  box.innerHTML = TF_RANGES[st.tf].map(([v, lbl]) => `<button data-v="${v}" class="${(st.ranges[st.tf] || 0) === v ? 'on' : ''}">${lbl}</button>`).join('');
  $$('#segRange button').forEach((b) => (b.onclick = () => {
    st.ranges[st.tf] = +b.dataset.v || 0;
    renderRangeSeg();
    st.chart.setOpts({ range: st.ranges[st.tf] || null });
  }));
}

function tfLevels(c) {
  const st = S.stock, t = st.tfd, out = levelsOf(c);
  if (!t) return out;
  if (st.tf !== 'd' && t.day) {
    if (t.day.open) out.push({ price: t.day.open, label: '今開 ' + fmtP(t.day.open), color: 'rgba(170,170,170,.9)' });
    if (t.day.vwap) out.push({ price: t.day.vwap, label: '均價 ' + fmtP(t.day.vwap), color: 'rgba(240,173,78,.95)' });
  }
  const p = t.position;
  if (p) {
    out.push({ price: p.entry, label: '我的進場 ' + fmtP(p.entry), color: 'rgba(57,135,229,.95)' });
    out.push({ price: p.stop, label: '停損 ' + fmtP(p.stop), color: 'rgba(47,191,113,.95)' });
    out.push({ price: p.target, label: '目標 ' + fmtP(p.target), color: 'rgba(255,91,87,.95)' });
  }
  return out;
}

function applyStockChart() {
  const st = S.stock, c = st.data, note = $('#chartNote');
  if (!c || !st.chart || !note) return;
  const range = st.ranges[st.tf] || null, t = st.tfd, nm = TF_NAME[st.tf];
  if (st.tf === 'd') {
    st.chart.setOpts({ intraday: false, range, levels: tfLevels(c) });
    st.chart.setData(c.chart);
    note.textContent = '▲ 紅色三角形＝多方訊號發生日、▼ 綠色＝空方訊號；虛線為支撐／壓力參考價。滑鼠移到圖上（手機用手指按住）可看每日數值。';
  } else if (t && t.tf === st.tf && t.chart) {
    st.chart.setOpts({ intraday: true, range, levels: tfLevels(c) });
    st.chart.setData(t.chart);
    note.textContent = `${nm} K 線：每根代表 ${nm}鐘。分時資料來自 Yahoo，可能延遲約 20 分鐘，最後一根已用即時報價更新${t.live ? `（${t.updated}）` : ''}。虛線：今開、當日均價${t.position ? '、你的進場／停損／目標價' : ''}，以及日線的支撐／壓力。上方的偏多／偏空只看均線與動能，不看量（盤中開盤、收盤的量本來就比較大）。`;
  } else if (t && t.tf === st.tf && t.error) {
    st.chart.setOpts({ intraday: false, range: st.ranges.d, levels: tfLevels(c) });
    st.chart.setData(c.chart);
    note.textContent = t.error + '（目前顯示的是日 K）';
  } else {
    note.textContent = `載入 ${nm} K 線中…`;
  }
}


function stockHeadHTML(c) {
  const chg = c.price && c.prev ? c.price - c.prev : null, cl = clsOf(c.chgp);
  const [qc, qt] = quoteSrc(c);
  return `<div><div><span class="nm">${esc(c.name)}</span><span class="cd">${c.code}</span>
      <span class="badge">${esc(c.industry || '—')}</span> <span class="badge">${c.market === 'TPEx' ? '上櫃' : '上市'}${c.type === 'etf' ? ' ETF' : ''}</span></div>
      <div class="ohlc num"><span>開 <b>${fmtP(c.open)}</b></span><span>高 <b>${fmtP(c.high)}</b></span><span>低 <b>${fmtP(c.low)}</b></span>
      <span>昨收 <b>${fmtP(c.prev)}</b></span><span>量 <b>${fmtInt(c.vol)}</b> 張</span></div>
      <div class="qsrc ${qc}">${esc(qt)} ${CLOUD ? '' : '<button class="linkbtn" id="btnPriceCheck" type="button">核對報價</button>'}</div></div>
    <div style="text-align:right"><div class="price num ${cl}">${c.est ? '<span class="est-approx" title="' + EST_TIP + '">≈</span>' : ''}${fmtP(c.price)}</div><div class="num ${cl}" style="font-size:15px">${arrow(c.chgp)} ${chg == null ? '' : fmtP(Math.abs(chg))} (${pct(c.chgp)})</div></div>`;
}

// ---- 核對報價：重新抓一次即時資料，並和官方收盤、歷史 K 線比對
async function showPriceCheck(code) {
  const box = $('#priceCheck');
  if (!box) return;
  if (!box.hidden && box.dataset.code === code) { box.hidden = true; return; }
  box.hidden = false;
  box.dataset.code = code;
  box.innerHTML = '<div class="muted">正在向證交所重新抓取即時報價…</div>';
  let d;
  try { d = await api('/api/pricecheck?code=' + encodeURIComponent(code)); } catch (e) { box.innerHTML = `<div class="muted">${NO_LINK}。</div>`; return; }
  if (d.error) { box.innerHTML = `<div class="muted">${esc(d.error)}</div>`; return; }
  const sh = d.shown || {}, lv = d.live, f = (v) => (v == null ? '—' : fmtP(v));
  const srcName = sh.src === 'daily' ? '官方收盤資料' : sh.est ? '推估價' : sh.no_trade ? '平盤參考價（尚未成交）' : sh.src === 'mis' ? '即時成交價' : '歷史日 K';
  const liveRow = lv ? `<tr><td>證交所即時<span class="hide-sm">（剛剛重新抓取）</span></td><td class="n num">${lv.price != null ? `<b>${f(lv.price)}</b>` : '<span class="muted">沒有揭示成交價</span>'}</td>
      <td class="n num hide-sm">${f(lv.bid)} / ${f(lv.ask)}</td><td class="n num hide-sm">${f(lv.open)} / ${f(lv.high)} / ${f(lv.low)}</td><td class="n num hide-sm">${f(lv.prev)}</td><td class="n num hide-sm">${fmtInt(lv.vol)}</td><td class="muted">${esc((lv.time || '').slice(0, 8))}</td></tr>`
    : `<tr><td>證交所即時</td><td colspan="2" class="muted">${esc(d.live_error || '沒有資料')}</td></tr>`;
  const offRows = (d.official || []).map((o) => `<tr><td>官方收盤資料</td><td class="n num"><b>${f(o.close)}</b></td><td class="hide-sm"></td><td class="n num hide-sm">${f(o.open)} / ${f(o.high)} / ${f(o.low)}</td><td class="hide-sm"></td><td class="n num hide-sm">${fmtInt(o.vol / 1000)}</td><td class="muted">${md(o.date)}</td></tr>`).join('');
  const barRows = (d.bars || []).map((b) => `<tr><td>程式的日 K</td><td class="n num">${f(b.close)}</td><td class="hide-sm"></td><td class="n num hide-sm">${f(b.open)} / ${f(b.high)} / ${f(b.low)}</td><td class="hide-sm"></td><td class="n num hide-sm">${fmtInt(b.vol / 1000)}</td><td class="muted">${md(b.date)}</td></tr>`).join('');
  box.innerHTML = `<h3>核對報價 <span class="muted" style="font-weight:400;font-size:12.5px">${esc(d.now)}</span></h3>
    <div class="table-scroll"><table class="tbl pc-tbl"><thead><tr><th>來源</th><th class="n">價格</th><th class="n hide-sm">買一 / 賣一</th><th class="n hide-sm">開 / 高 / 低</th><th class="n hide-sm">昨收</th><th class="n hide-sm">量(張)</th><th>時間</th></tr></thead>
    <tbody><tr class="cur"><td>畫面顯示（${srcName}）</td><td class="n num"><b>${f(sh.price)}</b></td><td class="hide-sm"></td><td class="n num hide-sm">${f(sh.open)} / ${f(sh.high)} / ${f(sh.low)}</td><td class="n num hide-sm">${f(sh.prev)}</td><td class="n num hide-sm">${fmtInt(sh.vol)}</td><td class="muted">${sh.src === 'daily' || sh.time === '收盤' ? md(sh.date) + ' 收盤' : esc(((sh.est ? sh.time : sh.lt_t || sh.time) || '').slice(0, 8))}</td></tr>
    ${liveRow}${offRows}${barRows}</tbody></table></div>
    <ul class="pc-checks">${(d.checks || []).map((k) => `<li class="${k.ok === true ? 'ok' : k.ok === false ? 'no' : 'na'}">${k.ok === true ? '✓' : k.ok === false ? '✕' : 'ⓘ'} ${esc(k.text)}</li>`).join('')}</ul>
    <div class="muted small">證交所即時資料每 5 秒揭示一次，只有揭示那一刻剛好有成交才會給成交價，所以盤中有時會看到「推估價」。收盤後一律以證交所／櫃買中心公布的官方收盤資料為準（約 13:50 後公布）。如果和券商軟體仍有差異，請把這個畫面截圖回報。</div>`;
}

function renderStock(c) {
  const st = S.stock;
  $('#view').innerHTML = `
    <p style="margin:0 0 8px"><a href="#" onclick="history.length>1?history.back():location.hash='top';return false" class="muted">← 返回</a></p>
    <div class="stock-head" id="stkHead">${stockHeadHTML(c)}</div>
    <div class="panel pricecheck" id="priceCheck" hidden></div>
    <div class="panel">
      <div class="mtf" id="mtfRow"></div>
      <div class="chart-tools">
        <div class="seg" id="segRange"></div>
        <div style="display:flex;gap:8px;flex-wrap:wrap">
          <div class="seg" id="segSub"><button data-v="macd">MACD</button><button data-v="kd">KD</button><button data-v="rsi">RSI</button><button data-v="">只看量</button></div>
          <div class="seg" id="segBB"><button data-v="1">布林通道</button></div>
        </div>
      </div>
      <div class="chart-wrap" id="bigChart" aria-label="${esc(c.name)} K 線圖"></div>
      <div class="muted" id="chartNote" style="font-size:12px;margin-top:6px"></div>
    </div>
    <div class="grid3">
      <div class="panel" id="pScore"></div>
      <div class="panel" id="pDepth"></div>
      <div class="panel" id="pChips"></div>
    </div>
    <div class="panel" style="margin-top:14px" id="pNews"></div>`;
  const segs = () => {
    $$('#segSub button').forEach((b) => b.classList.toggle('on', b.dataset.v === (st.sub || '')));
    $$('#segBB button').forEach((b) => b.classList.toggle('on', st.bb));
  };
  segs();
  renderMtf();
  renderRangeSeg();
  $$('#segSub button').forEach((b) => b.onclick = () => { st.sub = b.dataset.v || null; segs(); st.chart.setOpts({ sub: st.sub, height: st.sub ? 470 : 380 }); });
  $$('#segBB button').forEach((b) => b.onclick = () => { st.bb = !st.bb; segs(); st.chart.setOpts({ bb: st.bb }); });
  st.chart = new KChart($('#bigChart'), { height: st.sub ? 470 : 380, sub: st.sub, bb: st.bb, range: st.ranges.d, levels: levelsOf(c) });
  st.chart.setData(c.chart);   // 先畫日 K，分時資料載入後再換
  applyStockChart();
  fillStockPanels(c);
}

function updateStock(c) {
  $('#stkHead').innerHTML = stockHeadHTML(c);
  if (S.stock.tf === 'd') applyStockChart();
  fillStockPanels(c);
}

function fillStockPanels(c) {
  const pos = c.signals.filter((s) => s.pts > 0).reduce((a, s) => a + s.pts, 0);
  const neg = c.signals.filter((s) => s.pts < 0).reduce((a, s) => a + s.pts, 0);
  const fl = c.flags || {};
  const flagTxt = fl.dist ? '<div class="tag dist" style="margin-top:8px">⚠ 疑似高檔出貨，已排除在前十名之外</div>'
    : fl.wash ? '<div class="tag wash" style="margin-top:8px">疑似主力洗盤（回檔量縮、守住支撐）</div>' : '';
  const scoreTxt = c.score == null ? '<div class="muted">歷史資料不足 4 個月，暫不評分。</div>' : `
    <div class="score-row">${ring(c.score)}<div><div class="grade ${gradeCls(c.score)}">${c.grade}</div>
    <div class="muted" style="font-size:12.5px">多方 +${pos}　空方 ${neg}</div></div></div>${flagTxt}
    <ul class="sig-list">${c.signals.slice().sort((a, b) => b.pts - a.pts).map((s) => `<li><span>${esc(s.label)}</span><span class="pts num ${clsOf(s.pts)}">${s.pts > 0 ? '+' : ''}${s.pts}</span></li>`).join('')}</ul>`;
  const ce = c.close_eval;
  const head = c.live
    ? `<h3>盤中即時評分 <span class="mbadge live">暫定・${esc((c.time || '').slice(0, 5))}</span></h3>
       ${ce ? `<div class="mode-note close" style="margin:0 0 10px">昨日（${md(ce.date)}）收盤評分：<b>${ce.score}</b>（${esc(ce.grade)}）。收盤評分是回測驗證過的依據，盤中分數會隨即時價格變動。</div>` : ''}
       ${c.vol_est ? `<div class="muted small" style="margin-bottom:8px">量能以預估全日量 ${fmtInt(c.vol_est.est)} 張計算（目前實際 ${fmtInt(c.vol_est.actual)} 張）</div>` : ''}`
    : `<h3>收盤評分 <span class="mbadge confirm">${md(c.date)} 收盤</span></h3>`;
  $('#pScore').innerHTML = head + scoreTxt;

  // 五檔與內外盤
  const dp = c.depth || {};
  let depth = '<div class="muted" style="font-size:13px">盤中才有五檔委買委賣資料。</div>';
  if (dp.bids && dp.bids.length && dp.asks && dp.asks.length) {
    const mx = Math.max(...(dp.bid_vols || [0]), ...(dp.ask_vols || [0]), 1);
    depth = `<table class="depth num"><tr class="muted"><td>委買量</td><td style="text-align:right">買價</td><td>賣價</td><td style="text-align:right">委賣量</td></tr>
      ${[0, 1, 2, 3, 4].map((i) => `<tr><td class="vb">${dp.bid_vols[i] != null ? `<i style="left:0;width:${(dp.bid_vols[i] / mx) * 100}%;background:var(--up)"></i>${fmtInt(dp.bid_vols[i])}` : ''}</td>
        <td style="text-align:right" class="up">${fmtP(dp.bids[i])}</td><td class="down">${fmtP(dp.asks[i])}</td>
        <td class="vb" style="text-align:right">${dp.ask_vols[i] != null ? `<i style="right:0;width:${(dp.ask_vols[i] / mx) * 100}%;background:var(--down)"></i>${fmtInt(dp.ask_vols[i])}` : ''}</td></tr>`).join('')}</table>`;
  }
  let flow = '<div class="muted" style="font-size:13px;margin-top:10px">內外盤需在盤中開著程式才會開始累計；目前評分改用 K 線收盤位置估算買賣力道。</div>';
  if (c.flow && c.flow.total > 0) {
    const o = Math.round((c.flow.outer / c.flow.total) * 100);
    flow = `<div style="margin-top:12px;font-size:13.5px">外盤 <b class="up">${o}%</b>　內盤 <b class="down">${100 - o}%</b></div>
      <div class="flowbar"><span style="width:${o}%;background:var(--up)"></span><span style="width:${100 - o}%;background:var(--down)"></span></div>
      <div class="muted" style="font-size:12px">外盤 ${fmtInt(c.flow.outer)} 張／內盤 ${fmtInt(c.flow.inner)} 張（自 ${esc((c.flow.since || '').slice(0, 5))} 起追蹤）。外盤＝以賣價成交（買方主動），內盤＝以買價成交（賣方主動）。</div>`;
  }
  $('#pDepth').innerHTML = `<h3>五檔報價與內外盤</h3>${depth}${flow}`;

  // 法人、價位、驗證
  const chips = c.chips || [];
  const chipsTbl = chips.length ? `<table class="tbl" style="font-size:12.5px"><thead><tr><th>日期</th><th class="n">外資</th><th class="n">投信</th><th class="n">自營商</th><th class="n">合計</th></tr></thead><tbody>
    ${chips.slice().reverse().map((x) => `<tr style="cursor:default"><td>${x.date.slice(5)}</td>${['f', 't', 'd', 'total'].map((k) => `<td class="n num ${clsOf(x[k])}">${signed(x[k] / 1000)}</td>`).join('')}</tr>`).join('')}</tbody></table>
    <div class="muted" style="font-size:12px;margin-top:4px">單位：張。上櫃股票的法人資料從程式開始執行後逐日累積。</div>` : '<div class="muted" style="font-size:13px">尚無三大法人資料。</div>';
  const L = c.levels || {}, bt = c.backtest;
  $('#pChips').innerHTML = `<h3>三大法人買賣超</h3>${chipsTbl}
    <h3 style="margin-top:16px">關鍵價位（參考）</h3>
    <dl class="kv num"><dt>壓力</dt><dd>${fmtP(L.resistance)}</dd><dt>支撐</dt><dd>${fmtP(L.support)}</dd>
    <dt>訊號失效</dt><dd>${fmtP(L.invalid)} <span class="muted">（收盤跌破代表多方訊號失效）</span></dd><dt>日均波動</dt><dd>${L.atr_pct ?? '—'}%</dd>
    <dt>20 日均成交值</dt><dd>${c.avg_value ? (c.avg_value / 1e8).toFixed(2) + ' 億' : '—'}</dd></dl>
    <h3 style="margin-top:16px">此股歷史驗證</h3>
    <div style="font-size:13px" class="t2">${bt ? `過去一年評分 ≥ ${bt.threshold} 共 ${bt.n} 次，10 個交易日後上漲 ${bt.win10}%、平均 ${pct(bt.avg10, 1)}；任意一天買進上漲 ${bt.base_win10}%。${bt.n < 5 ? '（樣本不足）' : ''}` : '資料不足以回測。'}</div>`;

  const items = (c.news && c.news.items) || [];
  const [k, l] = c.news ? sentiLabel(c.news.score) : ['z', '—'];
  $('#pNews').innerHTML = `<h3>相關新聞 <span class="senti ${k}" style="margin-left:6px">整體${l}</span></h3>
    ${items.length ? `<ul class="news">${items.map((it) => newsItem(it, false)).join('')}</ul>` : '<div class="muted">近 7 日沒有找到標題提及此股的新聞。</div>'}
    <div class="muted" style="font-size:12px;margin-top:6px">新聞情緒以財經詞庫比對標題（例如「營收創新高」為利多、「下修」為利空），越新的新聞權重越高，僅供參考。</div>`;
}


// ================================================================ 復盤
const WD = ['日', '一', '二', '三', '四', '五', '六'];
const wd = (iso) => WD[new Date(iso + 'T00:00:00').getDay()];

function showReview() {
  S.view = 'review';
  S.rv = S.rv || { h: 10, show: 20, open: new Set() };
  $('#view').innerHTML = `
    <section class="section">
      <div class="section-head"><h2>復盤</h2>
        <div class="seg" id="rvH"><button data-h="1">當沖</button><button data-h="2">2 日</button><button data-h="5">5 日</button><button data-h="10">10 日</button></div></div>
      <div id="rvStatus" class="mode-note" hidden></div>
      <div class="rv-metrics" id="rvMetrics"></div>
      <div class="panel" style="margin-top:12px">
        <div class="rv-legend"><span><i style="background:var(--accent)"></i>名單累積報酬（扣成本）</span><span><i style="background:var(--flat)"></i>加權指數</span><span><i style="background:var(--warn)"></i>櫃買指數</span></div>
        <div id="rvCurve" class="chart-wrap" style="height:200px"></div>
        <div class="muted small" id="rvNote"></div>
      </div>
      <details class="panel rv-adv" id="rvWhy" style="margin-top:12px"><summary>深入分析：持有幾天最好？表現為什麼會變？</summary><div id="rvWhyBody"></div></details>
      <details class="panel rv-adv" id="rvIntra" style="margin-top:12px"><summary>盤中名單驗證：當沖模式的建議有沒有用？</summary><div id="rvIntraBody"></div></details>
      <div class="panel" style="margin-top:12px">
        <h3>每日名單 <span class="muted" style="font-weight:400;font-size:12.5px">點一下展開這天的 10 檔</span></h3>
        <div id="rvRows"></div>
      </div>
      <details class="panel rv-adv" id="rvAdv" style="margin-top:12px"><summary>進階：策略比較與成本設定</summary><div id="rvAdvBody"></div></details>
      <details class="panel rv-adv" id="rvMine" style="margin-top:12px"${CLOUD ? ' hidden' : ''}><summary>我的交易紀錄</summary><div id="rvMineBody"></div></details>
    </section>`;
  $$('#rvH button').forEach((b) => (b.onclick = () => { S.rv.h = +b.dataset.h; renderReview(); }));
  loadReview();
  dtReviewLoad();
  S.timers.push(setInterval(() => {
    const st = S.review && S.review.status;
    if ((st && st.building) || Date.now() - (S.reviewLoaded || 0) > 120000) loadReview();
  }, 4000));
}

async function loadReview() {
  let d;
  try { d = await api('/api/review'); } catch (e) { return; }
  S.reviewLoaded = Date.now();
  if (S.view !== 'review') return;
  const prevGen = S.review && S.review.generated;
  S.review = d;
  if (prevGen !== d.generated || !$('#rvRows').children.length) renderReview();
  else renderReviewStatus();
}

function renderReviewStatus() {
  const d = S.review, st = d.status || {}, el = $('#rvStatus');
  if (!el) return;
  if (st.building) {
    el.hidden = false; el.className = 'mode-note live';
    el.innerHTML = `${esc(st.msg || '建立復盤資料中…')}${st.total ? `<div class="scanbar-track" style="margin-top:8px"><div class="scanbar-fill" style="width:${Math.round(st.done / st.total * 100)}%"></div></div>` : ''}`;
  } else if (d.error) {
    el.hidden = false; el.className = 'mode-note live'; el.textContent = '復盤計算發生錯誤：' + d.error;
  } else el.hidden = true;
}

function renderReview() {
  const d = S.review, h = S.rv.h;
  if (!d || !$('#rvMetrics')) return;
  $$('#rvH button').forEach((b) => b.classList.toggle('on', +b.dataset.h === h));
  renderReviewStatus();
  const sm = (d.summary || {})[h] || {};
  const set = d.settings || {};
  if (!d.rows || !d.rows.length) {
    $('#rvMetrics').innerHTML = '';
    $('#rvRows').innerHTML = `<div class="empty"><div class="big">復盤資料建立中</div>第一次需要補抓過去 5 個月的三大法人資料並重建每日名單，約需幾分鐘，完成後會自動顯示。</div>`;
    return;
  }
  const metric = (label, val, cls, sub) => `<div class="rv-m"><div class="l">${label}</div><div class="v num ${cls || ''}">${val}</div>${sub ? `<div class="s">${sub}</div>` : ''}</div>`;
  $('#rvMetrics').innerHTML =
    metric(`名單平均報酬<span class="hide-sm">（每筆，扣成本）</span>`, pct(sm.avg), clsOf(sm.avg), h === 1 ? '當沖：開盤買、收盤賣' : `持有 ${h} 個交易日`) +
    metric('同期大盤', pct(sm.mkt), clsOf(sm.mkt), `加權指數・櫃買 <span class="${clsOf(sm.otc)}">${pct(sm.otc)}</span><span class="hide-sm">・同條件個股 <span class="${clsOf(sm.pool)}">${pct(sm.pool)}</span></span>`) +
    metric('上漲機率', sm.win != null ? sm.win + '%' : '—', '', '扣成本後賺錢的比例') +
    metric('已追蹤', `${sm.days ?? 0} 天`, '', `${fmtInt(sm.n)} 筆・${d.range ? md(d.range[0]) + '起' : ''}`);
  const cv = (d.curves || {})[h];
  const wrap = $('#rvCurve');
  if (S.rvChart) S.rvChart.destroy();
  S.rvChart = cv && cv.dates.length ? new CurveChart(wrap, cv, [['A', 'var(--accent)', '名單'], ['mkt', 'var(--flat)', '加權'], ['otc', 'var(--warn)', '櫃買']]) : null;
  if (!S.rvChart) wrap.innerHTML = '<div class="muted small">資料累積中，至少要有一筆完成的持有紀錄才會畫出曲線。</div>';
  $('#rvNote').innerHTML = `假設：${h === 1 ? '名單公布後下一個交易日開盤買進、當天收盤賣出（現股當沖，需先向券商申請當沖資格；實際盤中進出點會不同）' : `名單公布後下一個交易日開盤買進，持有 ${h} 個交易日後收盤賣出`}；每天把資金平均分給所有持有中的股票。已扣手續費（${(set.fee_discount * 10).toFixed(1)} 折）與證交稅 ${h === 1 ? '0.15%（當沖減半）' : '0.3%'}，未計最低手續費與滑價；股價已還原除權息。指數是價格指數（不含股利），長期會略為低估大盤。紅漲綠跌。`;
  renderReviewWhy();
  renderReviewRows();
  renderReviewAdv();
}

// ---------------------------------------------------------------- 深入分析
const SIG_NAME = {
  ma_bull: '均線多頭排列', above_ma: '站上月線與季線', ma20_up: '月線上揚', ma60_up: '季線上揚', adx: 'ADX 多方趨勢',
  macd_gc: 'MACD 黃金交叉', macd_up: 'MACD 動能增強', kd_low_gc: 'KD 低檔黃金交叉', kd_gc: 'KD 黃金交叉', kd_dull: 'KD 高檔鈍化',
  rsi_ok: 'RSI 強勢區', rsi_50: 'RSI 穿越 50', rs: '相對強勢', vol_red: '帶量長紅', pv_up: '量價齊揚', obv_high: 'OBV 創新高',
  obv_acc: '價穩量增吸籌', flow_out: '外盤比高', depth_bid: '委買較多', flow_est: '估算外盤偏多', bo60: '突破 60 日新高',
  bo_box: '箱型突破', bo20: '帶量突破 20 日高', bo20_weak: '突破 20 日高（量不足）', bo_hold: '突破後守穩', bb_break: '布林壓縮後突破',
  bb_squeeze: '布林通道壓縮', ma_expand: '均線糾結後發散', gap_up: '跳空缺口未補', w_bottom: 'W 底突破', w_forming: 'W 底成形中',
  wash: '洗盤', trust_buy: '投信連買', foreign_buy: '外資連買', insti_buy: '法人買超', news_pos: '新聞偏多', mkt_bull: '大盤多頭',
};
const REGIME = { bull: '大盤多頭', neutral: '大盤盤整', bear: '大盤偏空' };
const avgOf = (a) => { const v = a.filter((x) => x != null); return v.length ? v.reduce((s, x) => s + x, 0) / v.length : null; };
const dPct = (v) => (v == null ? '—' : `<span class="${clsOf(v)}">${pct(v)}</span>`);

function rvStats(rows, h) {
  const L = avgOf(rows.map((r) => r.h[h].avg)), P = avgOf(rows.map((r) => r.pool && r.pool[h]));
  return { n: rows.length, L, P, M: avgOf(rows.map((r) => r.mk && r.mk[h])), O: avgOf(rows.map((r) => r.otc && r.otc[h])),
    ex: L != null && P != null ? L - P : null };
}

const HOLD_NAME = (h) => (h === 1 ? '當沖' : `持有 ${h} 日`);
const f2 = (v) => (v == null ? '—' : (v > 0 ? '+' : '') + v.toFixed(2) + '%');
const rtCost = (set, h) => (2 * set.fee_rate * set.fee_discount + (h === 1 ? (set.tax_dt ?? 0.0015) : set.tax)) * 100;
function rvSeg(rows, sp) {
  const s = S.rv.seg || 'all';
  return s === 'a' ? rows.filter((r) => r.date < sp) : s === 'b' ? rows.filter((r) => r.date >= sp) : rows;
}

function rvHoldPath(d, sp) {
  const set = d.settings || {}, holds = d.holds || [5, 10], N = d.path_days || 10;
  // 持有天數比較
  const hs = holds.map((h) => {
    const rs = rvSeg(d.rows.filter((r) => r.h[h] && r.h[h].state === 'done'), sp);
    const vals = rs.flatMap((r) => r.picks.map((p) => p['r' + h]).filter((v) => v != null));
    return { h, st: rvStats(rs, h), win: vals.length ? Math.round(vals.filter((v) => v > 0).length / vals.length * 1000) / 10 : null };
  }).filter((x) => x.st.n);
  const best = hs.reduce((m, x) => (x.st.L != null && (!m || x.st.L > m.st.L) ? x : m), null);
  const holdHTML = hs.length ? `<div class="table-scroll"><table class="tbl rv-tbl"><thead><tr><th></th><th class="n hide-sm">成本</th><th class="n">名單</th><th class="n">上漲<span class="hide-sm">機率</span></th><th class="n">同條件</th><th class="n hide-sm">加權</th><th class="n">選股<span class="hide-sm">貢獻</span></th></tr></thead><tbody>${hs.map((x) =>
    `<tr style="cursor:default" class="${x.h === S.rv.h ? 'cur' : ''}"><td>${x.h === 1 ? '當沖' : x.h + ' 日'}<span class="muted show-sm">成本 ${rtCost(set, x.h).toFixed(2)}%</span></td><td class="n num hide-sm muted">${rtCost(set, x.h).toFixed(2)}%</td><td class="n num"><b>${dPct(x.st.L)}</b></td><td class="n num">${x.win != null ? x.win + '%' : '—'}</td><td class="n num">${dPct(x.st.P)}</td><td class="n num hide-sm">${dPct(x.st.M)}</td><td class="n num">${dPct(x.st.ex)}</td></tr>`).join('')}</tbody></table></div>
    <div class="muted small">名單、同條件個股都已扣成本（一次買賣：手續費＋證交稅；當沖稅減半為 0.15%）。加權指數未扣成本。${best ? `這段期間平均報酬最高的是「${HOLD_NAME(best.h)}」。` : ''}差距在 0.3 個百分點以內多半只是雜訊。</div>` : '<div class="muted small">資料不足。</div>';

  // 報酬發生在哪一天（未扣成本）
  const pr = rvSeg(d.rows.filter((r) => r.path), sp);
  let pathHTML = '<div class="muted small">要有完整 10 個交易日的紀錄才能拆解，資料累積中。</div>';
  if (pr.length >= 3) {
    const L = (k) => avgOf(pr.map((r) => r.path.d[k])), P = (k) => avgOf(pr.map((r) => r.ppath && r.ppath.d[k]));
    const LC = (k) => avgOf(pr.map((r) => r.path.c[k])), PC = (k) => avgOf(pr.map((r) => r.ppath && r.ppath.c[k]));
    const tr = (name, l, p, lc, pc, cls) => `<tr style="cursor:default" class="${cls || ''}"><td>${name}</td><td class="n num">${dPct(l)}</td><td class="n num">${dPct(p)}</td><td class="n num"><b>${dPct(lc)}</b></td><td class="n num">${dPct(pc)}</td></tr>`;
    let body = tr('隔夜跳空<span class="muted show-sm">開盤買不到</span><span class="muted hide-sm">（開盤買不到）</span>', L(0), P(0), null, null, 'gap');
    let pk = 1;
    for (let k = 1; k <= N; k++) {
      if (LC(k - 1) != null && LC(k - 1) > LC(pk - 1)) pk = k;
      body += tr(k === 1 ? '第 1 天<span class="muted show-sm">開盤→收盤</span><span class="muted hide-sm">（開盤→收盤）</span>' : `第 ${k} 天`, L(k), P(k), LC(k - 1), PC(k - 1), k === pk ? '' : '');
    }
    const gap = L(0), d1 = L(1), c1 = rtCost(set, 1), peak = LC(pk - 1), lastC = LC(N - 1);
    const notes = [];
    notes.push(`名單股隔天平均跳空 <b class="${clsOf(gap)}">${f2(gap)}</b>${gap > 0.1 ? '，這段漲幅開盤買進是拿不到的' : ''}；開盤到收盤平均 <b class="${clsOf(d1)}">${f2(d1)}</b>` +
      (gap > 0.3 && d1 < 0 ? '，常見「開高走低」，開盤追進容易買在當天高點。' : '。'));
    notes.push(`<b>當沖</b>：第 1 天平均 ${f2(d1)}，扣掉約 ${c1.toFixed(2)}% 的當沖成本後約 <b class="${clsOf(d1 - c1)}">${f2(d1 - c1)}</b>（同條件個股 ${f2(P(1) != null ? P(1) - c1 : null)}）。` +
      (d1 - c1 > 0 ? '平均來說有賺，但只用收盤價估算，實際當沖要看盤中進出點。' : '平均來說當沖賺不到成本。'));
    notes.push(`從開盤買進算，名單累積報酬在<b>第 ${pk} 天最高</b>（${f2(peak)}，未扣成本），` +
      (pk < N ? `到第 ${N} 天剩 ${f2(lastC)}，之後的天數是在回吐。` : `到第 ${N} 天仍在上升。`) +
      `扣一次買賣成本約 ${rtCost(set, 2).toFixed(2)}%。`);
    pathHTML = `<div class="table-scroll"><table class="tbl rv-tbl rv-path"><thead><tr><th></th><th class="n">名單<span class="hide-sm">當天</span></th><th class="n">同條件<span class="hide-sm">當天</span></th><th class="n">名單累積</th><th class="n">同條件累積</th></tr></thead><tbody>${body}</tbody></table></div>
      <div class="rv-why">${notes.map((m) => `<p>${m}</p>`).join('')}</div>
      <div class="muted small">「累積」是從隔天開盤買進算到該天收盤，未扣成本；共 ${pr.length} 天的名單（每天先算 10 檔平均，再平均各天）。</div>`;
  }
  return `<h3 style="margin-top:12px">持有天數比較</h3>${holdHTML}
    <h3 style="margin-top:16px">報酬發生在哪一天</h3>${pathHTML}`;
}

function renderReviewWhy() {
  const d = S.review, h = S.rv.h, body = $('#rvWhyBody');
  if (!body) return;
  const done = d.rows.filter((r) => r.h[h] && r.h[h].state === 'done').slice().reverse();
  if (done.length < 6) { body.innerHTML = '<div class="muted small" style="margin-top:8px">完成的紀錄還太少，至少需要 6 天。</div>'; return; }
  if (!S.rv.split || S.rv.split <= done[0].date || S.rv.split > done[done.length - 1].date) {
    let saved = null; try { saved = localStorage.getItem('rvSplit'); } catch (e) { /* 無痕模式 */ }
    S.rv.split = saved && saved > done[0].date && saved <= done[done.length - 1].date ? saved : done[Math.floor(done.length / 2)].date;
  }
  const sp = S.rv.split, A = done.filter((r) => r.date < sp), B = done.filter((r) => r.date >= sp);
  const a = rvStats(A, h), b = rvStats(B, h);
  const head = '<thead><tr><th></th><th class="n hide-sm">天數</th><th class="n">名單</th><th class="n">同條件<span class="hide-sm">個股</span></th><th class="n">加權</th><th class="n hide-sm">櫃買</th><th class="n">選股<span class="hide-sm">貢獻</span></th></tr></thead>';
  const line = (name, x, sub) => `<tr style="cursor:default"><td>${name}${sub ? `<span class="hide-sm">（${sub}）</span>` : ''}<span class="muted show-sm">${sub ? sub + '・' : ''}${x.n} 天</span></td><td class="n num hide-sm">${x.n}</td><td class="n num">${dPct(x.L)}</td><td class="n num">${dPct(x.P)}</td><td class="n num">${dPct(x.M)}</td><td class="n num hide-sm">${dPct(x.O)}</td><td class="n num"><b>${dPct(x.ex)}</b></td></tr>`;

  // 1. 前後段比較＋自動解讀
  const thr = { 1: 0.3, 2: 0.5, 5: 0.8, 10: 1.2 }[h] || 1, f1 = (v) => (v == null ? '—' : (v > 0 ? '+' : '') + v.toFixed(1) + '%');
  const envA = a.P != null && a.M != null ? a.P - a.M : null, envB = b.P != null && b.M != null ? b.P - b.M : null;
  const largeShare = (rs) => { const k = rs.filter((r) => r.style); return k.length ? Math.round(k.filter((r) => r.style === 'large').length / k.length * 100) : null; };
  const msgs = [];
  if (envA != null && envB != null) {
    msgs.push(`<b>大環境</b>：同條件個股（也就是不靠選股、平均買進所有符合門檻的股票）相對加權指數，前段 ${f1(envA)}，後段 ${f1(envB)}。` +
      (largeShare(A) != null ? `權值股比中小型股強的日子，前段占 ${largeShare(A)}%，後段占 ${largeShare(B)}%。` : ''));
  }
  if (a.ex != null && b.ex != null) msgs.push(`<b>選股貢獻</b>：名單相對同條件個股，前段 ${f1(a.ex)}，後段 ${f1(b.ex)}。`);
  let concl = '';
  if (envA != null && envB != null && a.ex != null && b.ex != null) {
    const envDrop = envA - envB > thr, pickDrop = a.ex - b.ex > thr;
    if (envDrop && !pickDrop) concl = `後段落後大盤，主要是整體個股（尤其中小型股）落後由權值股撐起的加權指數，${b.ex > 0 ? '名單本身仍勝過同條件個股' : '名單和同條件個股差不多'}，比較像「風格輪動」而不是選股失靈。這種時期持有大盤型 ETF（例如 0050）確實較有利，但通常只能事後確認；可以看下方「按當時大盤狀態」表中「權值股較強」那一列，判斷這種狀態下名單是否還有優勢。`;
    else if (pickDrop && !envDrop) concl = '大環境變化不大，但名單相對同條件個股的優勢明顯縮小，代表選股訊號本身在後段變差，可以看下方「各訊號表現」找出失靈的訊號。';
    else if (envDrop && pickDrop) concl = '兩個原因都有：整體個股落後權值股，名單的選股優勢也同時縮小。可對照下方「各訊號表現」與「按當時大盤狀態」。';
    else concl = `前後兩段在大環境與選股貢獻上的差異都不大（差距小於 ${thr} 個百分點），可能只是短期波動。`;
    if (A.length < 15 || B.length < 15) concl += ' 注意：其中一段不到 15 天，樣本偏少，結論僅供參考。';
  }

  // 2. 按月份
  const months = {};
  done.forEach((r) => (months[r.date.slice(0, 7)] = months[r.date.slice(0, 7)] || []).push(r));
  const monthRows = Object.keys(months).sort().map((m) => line(`${+m.slice(5)} 月`, rvStats(months[m], h))).join('');

  // 3. 按當時大盤狀態
  const grp = (pred) => done.filter(pred);
  const stateRows = ['bull', 'neutral', 'bear'].map((k) => [REGIME[k], grp((r) => r.regime === k)])
    .concat([['中小型股較強', grp((r) => r.style === 'small')], ['權值股較強', grp((r) => r.style === 'large')]])
    .filter(([, rs]) => rs.length).map(([nm, rs]) => line(nm, rvStats(rs, h))).join('');

  // 4. 各訊號表現（入選股票報酬 − 同一天同條件個股平均）
  const sig = {};
  let haveKeys = 0;
  done.forEach((r) => {
    const pv = r.pool && r.pool[h];
    if (pv == null) return;
    r.picks.forEach((p) => {
      const v = p['r' + h];
      if (v == null || !p.keys) return;
      haveKeys++;
      const side = r.date < sp ? 0 : 1;
      p.keys.forEach((k) => { const o = (sig[k] = sig[k] || [[0, 0], [0, 0]]); o[side][0] += v - pv; o[side][1]++; });
    });
  });
  const sigRows = Object.entries(sig).filter(([, o]) => o[0][1] >= 5 && o[1][1] >= 5)
    .map(([k, o]) => ({ k, a: o[0][0] / o[0][1], na: o[0][1], b: o[1][0] / o[1][1], nb: o[1][1] }))
    .sort((x, y) => (x.b - x.a) - (y.b - y.a));
  const sigShow = S.rv.sigAll || sigRows.length <= 10 ? sigRows : sigRows.slice(0, 5).concat([null], sigRows.slice(-5));
  const sigHTML = sigRows.length ? `<div class="table-scroll"><table class="tbl rv-tbl"><thead><tr><th>訊號</th><th class="n">前段</th><th class="n">後段</th><th class="n">變化</th></tr></thead><tbody>${sigShow.map((x) => x == null
    ? `<tr style="cursor:default"><td colspan="4" class="muted" style="text-align:center">⋯ 中間 ${sigRows.length - 10} 個訊號變化較小 ⋯</td></tr>`
    : `<tr style="cursor:default"><td>${esc(SIG_NAME[x.k] || x.k)}</td><td class="n num">${dPct(x.a)}<span class="muted hide-sm"> ${x.na}</span></td><td class="n num">${dPct(x.b)}<span class="muted hide-sm"> ${x.nb}</span></td><td class="n num"><b>${dPct(x.b - x.a)}</b></td></tr>`).join('')}</tbody></table></div>
    ${sigRows.length > 10 ? `<div style="margin:6px 0"><button class="btn" id="rvSigAll">${S.rv.sigAll ? '只看變化最大的 10 個' : `顯示全部 ${sigRows.length} 個訊號`}</button></div>` : ''}
    <div class="muted small">數字是帶有該訊號的入選股票，平均報酬減去同一天「同條件個股」的平均<span class="hide-sm">（灰色小字是筆數）</span>。最上面是變差最多的，最下面是變好最多的；兩段都至少 5 筆的訊號才列出。</div>`
    : `<div class="muted small">${haveKeys ? '各訊號在兩段的筆數都不足 5 筆。' : '舊的紀錄沒有存訊號代碼，等復盤資料重建完成後就會出現。'}</div>`;

  body.innerHTML = `
    <div class="filters rv-filter" style="margin:10px 0 6px"><label>分段日期 <input type="date" id="rvSplit" value="${sp}" min="${done[1].date}" max="${done[done.length - 1].date}" style="background:var(--surface-2);border:1px solid var(--border);border-radius:8px;padding:5px 8px"></label>
      <span class="muted small-inline">這天（含）以後算「後段」</span></div>
    <div class="filters rv-filter" style="margin:0 0 4px"><span class="muted small-inline">下面兩張表看：</span><div class="seg" id="rvSeg"><button data-s="all">全部</button><button data-s="a">前段</button><button data-s="b">後段</button></div></div>
    ${rvHoldPath(d, sp)}
    <h3 style="margin-top:16px">前段 vs 後段（${HOLD_NAME(h)}）</h3>
    <div class="table-scroll"><table class="tbl rv-tbl">${head}<tbody>${line('前段', a, `～${md(A.length ? A[A.length - 1].date : sp)}`)}${line('後段', b, `${md(sp)}～`)}</tbody></table></div>
    ${msgs.length ? `<div class="rv-why">${msgs.map((m) => `<p>${m}</p>`).join('')}${concl ? `<p class="concl">${concl}</p>` : ''}</div>` : ''}
    <h3 style="margin-top:16px">按月份（${HOLD_NAME(h)}）</h3>
    <div class="table-scroll"><table class="tbl rv-tbl">${head}<tbody>${monthRows}</tbody></table></div>
    <h3 style="margin-top:16px">按當時大盤狀態（${HOLD_NAME(h)}）</h3>
    <div class="table-scroll"><table class="tbl rv-tbl">${head}<tbody>${stateRows}</tbody></table></div>
    <div class="muted small">「大盤多頭／盤整／偏空」以名單當天加權指數與月線、季線的位置判斷；「中小型股較強／權值股較強」比較櫃買指數與加權指數近 20 日的漲幅。</div>
    <h3 style="margin-top:16px">各訊號表現（前段 vs 後段，${HOLD_NAME(h)}）</h3>
    ${sigHTML}
    <div class="muted small" style="margin-top:10px">「前段 vs 後段」以下的表格都是${HOLD_NAME(h)}、每天名單的平均（上方可切換）。「同條件個股」是名單當天所有符合前十名門檻（股價 ≥ ${set0(d).min_price} 元、20 日均成交值 ≥ ${fmtInt((set0(d).min_value || 0) / 1e4)} 萬元）的股票，同樣隔天開盤買進、扣成本，代表「不靠選股」的平均水準；「選股貢獻」＝名單減同條件個股。</div>`;
  $$('#rvSeg button').forEach((bt) => {
    bt.classList.toggle('on', bt.dataset.s === (S.rv.seg || 'all'));
    bt.onclick = () => { S.rv.seg = bt.dataset.s; renderReviewWhy(); };
  });
  const sa = $('#rvSigAll');
  if (sa) sa.onclick = () => { S.rv.sigAll = !S.rv.sigAll; renderReviewWhy(); };
  $('#rvSplit').onchange = (e) => {
    const v = e.target.value;
    if (!v) return;
    S.rv.split = v;
    try { localStorage.setItem('rvSplit', v); } catch (err) { /* 無痕模式 */ }
    renderReviewWhy();
  };
}
const set0 = (d) => d.settings || {};

function renderReviewRows() {
  const d = S.review, h = S.rv.h, rows = d.rows.slice(0, S.rv.show);
  const html = rows.map((r) => {
    const x = r.h[h] || {};
    const pill = x.state === 'done' ? '<span class="pill done">已完成</span>'
      : x.state === 'track' ? `<span class="pill prog">追蹤中 ${x.day}/${h} 天</span>` : '<span class="pill wait">待進場</span>';
    const right = x.state === 'wait' ? '<span class="muted">下個交易日開盤</span>'
      : `<b class="num ${clsOf(x.avg)}">${pct(x.avg)}</b> <span class="muted small-inline">↑${x.up}/${x.n}</span>`;
    const open = S.rv.open.has(r.date);
    return `<div class="rv-row ${open ? 'open' : ''}" data-d="${r.date}"><span class="rv-date"><b>${md(r.date)}</b> <span class="muted">（${wd(r.date)}）</span> ${pill}${r.reconstructed ? '<span class="pill rec" title="當天程式沒有執行，這份名單是事後用歷史資料重建的（不含新聞）">重建</span>' : ''}</span><span>${right}</span></div>
      ${open ? reviewDetail(r, h) : ''}`;
  }).join('');
  $('#rvRows').innerHTML = html + (d.rows.length > S.rv.show ? `<div style="text-align:center;margin-top:10px"><button class="btn" id="rvMore">顯示更多（還有 ${d.rows.length - S.rv.show} 天）</button></div>` : '');
  $$('#rvRows .rv-row').forEach((el) => (el.onclick = () => {
    const k = el.dataset.d;
    if (S.rv.open.has(k)) S.rv.open.delete(k); else S.rv.open.add(k);
    renderReviewRows();
  }));
  const more = $('#rvMore');
  if (more) more.onclick = () => { S.rv.show += 20; renderReviewRows(); };
}

function reviewDetail(r, h) {
  const rows = r.picks.map((p) => {
    let ret, note = '';
    if (p.status === 'wait') { ret = '—'; note = '待進場'; }
    else if (p.status !== 'ok') { ret = '—'; note = '無資料'; }
    else {
      const v = p['r' + h];
      ret = v != null ? `<span class="${clsOf(v)}">${pct(v)}</span>` : `<span class="${clsOf(p.cur)}">${pct(p.cur)}</span> <span class="muted hide-sm">（第 ${Math.min(p.held, h)} 天）</span>`;
      if (p.skipB) note = '跳空不追';
      else if (p['stop' + h]) note = `${md(p['stop' + h])} 跌破失效價`;
    }
    return `<tr onclick="event.stopPropagation();location.hash='stock-${p.code}'"><td><span class="cd">${p.rank}.</span><span class="nm">${esc(p.name)}</span> <span class="cd">${p.code}</span>
        <div class="muted rv-tag">${esc((p.tags || [])[0] || '')}</div></td>
      <td class="n num ${clsOf(p.gap)}">${pct(p.gap)}</td><td class="n num">${ret}</td><td class="hide-sm muted">${note}</td></tr>`;
  }).join('');
  return `<div class="rv-detail table-scroll"><table class="tbl"><thead><tr><th>股票</th><th class="n">開盤跳空</th><th class="n">${h === 1 ? '當沖' : h + ' 日'}報酬<span class="hide-sm">（扣成本）</span></th><th class="hide-sm">策略 B 備註</th></tr></thead><tbody>${rows}</tbody></table></div>`;
}

function renderReviewAdv() {
  const d = S.review, h = S.rv.h, sg = (d.strategies || {})[h], set = d.settings || {};
  if (!sg) return;
  const row = (name, x, extra) => `<tr style="cursor:default"><td>${name}</td><td class="n num">${fmtInt(x.n)}</td><td class="n num ${clsOf(x.avg)}">${pct(x.avg)}</td>
    <td class="n num">${x.win != null ? x.win + '%' : '—'}</td><td class="n num ${clsOf(x.total)}">${pct(x.total)}</td><td class="muted hide-sm">${extra || ''}</td></tr>`;
  $('#rvAdvBody').innerHTML = `
    <h3 style="margin-top:10px">策略比較（${HOLD_NAME(h)}）</h3>
    <div class="table-scroll"><table class="tbl"><thead><tr><th>策略</th><th class="n">筆數</th><th class="n">平均報酬</th><th class="n">上漲機率</th><th class="n">累積報酬</th><th class="hide-sm">說明</th></tr></thead><tbody>
      ${row('A：隔天開盤全部買進', sg.A, '')}
      ${row(`B：跳空 ≥ ${set.chase_gap}% 不追＋跌破失效價停損`, sg.B, `不追 ${sg.B.skipped} 筆・停損 ${sg.B.stops} 筆`)}
      ${sg.C ? row('C：隔天收盤前才買（避開開盤追高）', sg.C, `收盤已跌破失效價不買 ${sg.C.skipped} 筆`) : ''}
    </tbody></table></div>
    <div class="muted small">B 的停損用當天最低價判斷是否跌破失效價，以失效價（或跳空開更低時的開盤價）出場；日 K 無法知道盤中先後順序，結果是近似值。C 是在名單公布後的下一個交易日 13:25～13:30 收盤前買進（收盤已跌破失效價就不買），之後同樣持有 ${h} 個交易日，${h === 1 ? '' : '比 A 晚一天出場，'}可以看出開盤追進是否吃虧。${h === 1 ? '當沖不適用策略 C。' : ''}</div>
    <h3 style="margin-top:16px">成本設定</h3>
    ${CLOUD ? `<div class="muted small">雲端版以手續費 ${(set.fee_discount * 10).toFixed(1)} 折計算（要改請在電腦版設定，或請 Claude 修改雲端設定）。</div>` : `<div class="filters" style="margin:6px 0"><label>手續費折扣 <input type="number" id="feeIn" step="0.1" min="0.5" max="10" value="${(set.fee_discount * 10).toFixed(1)}" style="width:80px;background:var(--surface-2);border:1px solid var(--border);border-radius:8px;padding:5px 8px"> 折</label>
      <button class="btn primary" id="feeSave">儲存並重新計算</button><span class="muted small-inline" id="feeMsg"></span></div>`}
    <div class="muted small">國泰證券電子下單一般為 2.8 折，實際以你的帳戶為準。手續費率 0.1425% 買賣各收一次，證交稅 0.3%（ETF 0.1%）在賣出時收；現股當沖證交稅減半為 0.15%（適用至 2027 年底）。</div>
    <h3 style="margin-top:16px">資料說明</h3>
    <div class="muted small" style="margin-top:0">標示「重建」的日期，是程式用歷史日 K 與三大法人資料事後算出的名單（不含新聞，上櫃股沒有歷史法人資料），可能和當天實際會列出的名單略有不同。之後每天收盤後，程式會自動把當天的名單存進來，並在之後 1～10 個交易日自動補上結果。</div>`;
  if ($('#feeSave')) $('#feeSave').onclick = async () => {
    const v = parseFloat($('#feeIn').value);
    if (!(v > 0 && v <= 10)) { $('#feeMsg').textContent = '請輸入 0.5～10 之間的折數'; return; }
    const r = await api('/api/settings', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ fee_discount: v }) });
    if (r.error) { $('#feeMsg').textContent = r.error; return; }
    $('#feeMsg').textContent = '已儲存';
    S.review = null; await loadReview(); $('#rvAdv').open = true;
  };
}

class CurveChart {
  constructor(wrap, cv, series) {
    this.wrap = wrap; this.cv = cv; this.series = series;
    wrap.innerHTML = '';
    this.canvas = document.createElement('canvas');
    this.canvas.style.width = '100%'; this.canvas.style.height = '100%'; this.canvas.style.touchAction = 'pan-y';
    wrap.appendChild(this.canvas);
    this.tip = $('#tooltip'); this.hover = null;
    const mv = (e) => { const r = this.canvas.getBoundingClientRect(); this.hover = { x: e.clientX - r.left, cx: e.clientX, cy: e.clientY }; this.draw(); };
    this.canvas.addEventListener('pointermove', mv); this.canvas.addEventListener('pointerdown', mv);
    this.canvas.addEventListener('pointerleave', () => { this.hover = null; this.tip.hidden = true; this.draw(); });
    this.ro = new ResizeObserver(() => this.draw()); this.ro.observe(wrap);
  }
  destroy() { this.ro.disconnect(); this.tip.hidden = true; }
  draw() {
    const cs = getComputedStyle(document.documentElement), col = (v) => (v.startsWith('var(') ? cs.getPropertyValue(v.slice(4, -1)).trim() : v);
    const W = this.wrap.clientWidth, H = this.wrap.clientHeight || 200, dpr = window.devicePixelRatio || 1;
    this.canvas.width = W * dpr; this.canvas.height = H * dpr;
    const ctx = this.canvas.getContext('2d'); ctx.setTransform(dpr, 0, 0, dpr, 0, 0); ctx.clearRect(0, 0, W, H);
    ctx.font = '11px ' + cs.getPropertyValue('--font');
    const n = this.cv.dates.length, padL = 6, padR = 46, padT = 10, padB = 18, pw = W - padL - padR, ph = H - padT - padB;
    let lo = 0, hi = 0;
    for (const [k] of this.series) for (const v of this.cv[k]) if (v != null) { lo = Math.min(lo, v); hi = Math.max(hi, v); }
    const pad = (hi - lo) * 0.08 || 1; lo -= pad; hi += pad;
    const x = (i) => padL + (n === 1 ? pw / 2 : (i / (n - 1)) * pw), y = (v) => padT + ((hi - v) / (hi - lo)) * ph;
    ctx.strokeStyle = col('var(--grid)'); ctx.fillStyle = col('var(--muted)'); ctx.textBaseline = 'middle'; ctx.lineWidth = 1;
    const step = Math.pow(10, Math.floor(Math.log10((hi - lo) / 3 || 1))) * (((hi - lo) / 3) / Math.pow(10, Math.floor(Math.log10((hi - lo) / 3 || 1))) > 5 ? 5 : ((hi - lo) / 3) / Math.pow(10, Math.floor(Math.log10((hi - lo) / 3 || 1))) > 2 ? 2 : 1);
    for (let v = Math.ceil(lo / step) * step; v <= hi; v += step) {
      const yy = Math.round(y(v)) + 0.5;
      ctx.strokeStyle = Math.abs(v) < step / 2 ? col('var(--muted)') : col('var(--grid)');
      ctx.beginPath(); ctx.moveTo(padL, yy); ctx.lineTo(padL + pw, yy); ctx.stroke();
      ctx.fillText((v > 0 ? '+' : '') + Math.round(v * 10) / 10 + '%', padL + pw + 6, yy);
    }
    ctx.textAlign = 'center'; ctx.textBaseline = 'alphabetic';
    let lastX = -99;
    for (let i = 1; i < n; i++) if (this.cv.dates[i].slice(5, 7) !== this.cv.dates[i - 1].slice(5, 7) && x(i) - lastX > 40) {
      ctx.fillText(+this.cv.dates[i].slice(5, 7) + '月', x(i), H - 4); lastX = x(i);
    }
    for (const [k, c] of this.series) {
      ctx.strokeStyle = col(c); ctx.lineWidth = k === 'A' ? 2 : 1.6; ctx.beginPath(); let st = false;
      this.cv[k].forEach((v, i) => { if (v == null) { st = false; return; } if (!st) { ctx.moveTo(x(i), y(v)); st = true; } else ctx.lineTo(x(i), y(v)); });
      ctx.stroke();
    }
    if (this.hover && n) {
      const i = Math.max(0, Math.min(n - 1, Math.round(((this.hover.x - padL) / pw) * (n - 1))));
      ctx.strokeStyle = col('var(--text-2)'); ctx.setLineDash([3, 3]); ctx.beginPath(); ctx.moveTo(Math.round(x(i)) + 0.5, padT); ctx.lineTo(Math.round(x(i)) + 0.5, padT + ph); ctx.stroke(); ctx.setLineDash([]);
      this.tip.innerHTML = `<div class="tt-d">${this.cv.dates[i]}</div><div class="tt-g">${this.series.map(([k, , nm]) => `<span>${nm}</span><span class="num ${clsOf(this.cv[k][i])}">${pct(this.cv[k][i])}</span>`).join('')}</div>`;
      this.tip.hidden = false;
      const r = this.tip.getBoundingClientRect();
      let tx = this.hover.cx + 14; if (tx + r.width > window.innerWidth - 8) tx = this.hover.cx - r.width - 14;
      this.tip.style.left = Math.max(4, tx) + 'px'; this.tip.style.top = Math.max(4, this.hover.cy - r.height - 10) + 'px';
    }
  }
}

// ================================================================ 模型與資料
async function showAbout() {
  S.view = 'about';
  $('#view').innerHTML = `<section class="section grid2" style="margin-top:0">
    <div class="panel prose" id="aboutModel"></div>
    <div><div class="panel" id="aboutSrc"><h3>資料來源狀態</h3><div class="muted">載入中…</div></div>
      <div class="panel prose" style="margin-top:14px" id="aboutCfg"></div></div></section>`;
  $('#aboutModel').innerHTML = `<h3>評分模型怎麼運作</h3>
    <p>每次掃描會對有基本成交量的上市櫃股票（當日成交值 ≥ 300 萬元）計算 40 多項訊號，每項訊號有固定的加減分，加總後截在 0–100 分。前十名另外要求 20 日平均成交值 ≥ 5,000 萬元、股價 ≥ 10 元，並排除「疑似高檔出貨」的股票。</p>
    <ul>
      <li><b>趨勢</b>：均線多頭排列、月線／季線方向、ADX 趨勢強度、乖離過大扣分</li>
      <li><b>動能</b>：MACD、KD 黃金／死亡交叉、RSI 強弱區與過熱、相對全市場的 20 日強弱</li>
      <li><b>型態</b>：帶量突破 20／60 日高、箱型整理突破、W 底突破頸線、M 頭跌破、布林通道壓縮後突破、均線糾結後發散、跳空缺口</li>
      <li><b>量價</b>：帶量長紅、爆量長黑、量價齊揚、OBV 創新高、價穩量增吸籌</li>
      <li><b>內外盤</b>：盤中逐筆比對成交價與最佳買賣價累計外盤／內盤；收盤後以 K 線收盤位置估算</li>
      <li><b>洗盤／出貨</b>：多頭回檔量縮守支撐、出現止跌訊號視為洗盤加分；高檔爆量不漲、量價或 RSI 背離、長上影、帶量跌破、法人賣超視為出貨扣分</li>
      <li><b>籌碼</b>：外資、投信連買連賣與近 5 日買賣超占成交量比例</li>
      <li><b>新聞</b>：Google 新聞與鉅亨網標題情緒，越新的新聞權重越高；出現「處置」「全額交割」等字眼另外扣分</li>
    </ul>
    <h3>收盤確認名單 vs 盤中暫定名單</h3>
    <ul>
      <li><b>收盤確認</b>：用某個交易日收盤後的完整資料評估，下個交易日盤中固定不變。回測驗證的就是這種用法，建議當作主要的觀察名單。</li>
      <li><b>盤中暫定</b>：交易時間內每分鐘用即時價重新評分，成交量依開盤經過的時間換算成「預估全日量」。適合發現新機會，但收盤前會一直變動，也沒有經過回測。</li>
      <li><b>報價</b>：盤中來自證交所即時資料。證交所只在揭示那一刻剛好有成交時才給成交價，其餘時間程式沿用最後確認的成交價；確定有新成交但不知道價格時，依最佳一檔買賣價推估，畫面上標「≈」或「估」。收盤後一律改用證交所／櫃買中心的官方收盤資料，並自動修正歷史日 K。個股頁的「核對報價」可以直接比對各個資料來源。</li>
      <li><b>今日盤中追蹤</b>：盤中會即時檢查收盤確認名單的每一檔，標示開盤跳空、目前漲跌、外盤比與狀態，協助判斷進場時機。</li>
      <li><b>個股頁的 30 分／60 分／日 K</b>：點任何一檔股票，圖表上方可以切換三種週期，並顯示每個週期目前偏多、偏空還是整理（依均線與 MACD、KD 五項條件，不看量），以及三個週期放在一起的一句話。分時資料來自 Yahoo，可能延遲約 20 分鐘，最後一根會用即時報價更新；有持倉的股票會在圖上畫出你的進場、停損、目標價。</li>
      <li><b>當沖模式</b>：從盤中暫定名單挑出額度內買得起的前 5 名，檢查五項條件（站上開盤價與當日均價、沒有追高、預估量、外盤比、排名穩定度），標示「可進場／等一下／不要追」，並依你的當沖額度與每筆停損金額算出進場價、停損價、目標價與張數。符合條件、持倉碰到停損或目標價、收盤前還有部位時會發提醒。當日均價是用即時成交估算的；這套規則尚未經過驗證，請搭配復盤頁的「盤中名單驗證」。</li>
      <li><b>復盤</b>：每天的收盤確認名單都會存檔，並自動算出扣除成本後的實際報酬（股價已還原除權息），和加權指數、櫃買指數、同條件個股比較，可切換當沖、持有 2／5／10 日；「深入分析」可比較持有天數、看報酬發生在哪一天，並拆解落後是大環境還是選股造成。</li>
    </ul>
    <p>首頁的「評分模型回測」用過去一年的資料檢驗：分數越高的區間，10 日後上漲的比例是否真的越高。技術分析是機率而不是保證，建議搭配基本面與自己的風險控管。</p>`;
  try {
    const d = await api('/api/diag');
    const rows = Object.entries(d.sources || {}).sort();
    $('#aboutSrc').innerHTML = `<h3>資料來源狀態</h3>
      ${rows.length ? `<div class="table-scroll"><table class="tbl" style="font-size:12.5px"><tbody>${rows.map(([k, v]) => `<tr style="cursor:default"><td>${v.ok ? '<span class="src-ok">● 正常</span>' : '<span class="src-fail">● 失敗</span>'}</td><td>${esc(k)}</td><td class="muted" style="white-space:normal">${esc(v.at)} ${esc(v.msg)}</td></tr>`).join('')}</tbody></table></div>`
        : '<div class="muted">尚未開始抓取（示範模式不連網）。</div>'}
      ${CLOUD ? cloudInfoHTML() : '<div style="margin-top:12px;display:flex;gap:8px;align-items:center"><button class="btn primary" id="btnRescan">立即重新掃描</button><span class="muted" id="rescanMsg" style="font-size:12.5px"></span></div>'}`;
    if ($('#btnRescan')) $('#btnRescan').onclick = async () => { await api('/api/rescan', { method: 'POST' }); $('#rescanMsg').textContent = '已排入重新掃描，進度會顯示在頁面上方。'; };
    const qd = d.quality;
    if (qd) {
      const c = qd.counts || {}, tot = (c.exact || 0) + (c.est || 0) + (c.no_trade || 0) + (c.official || 0);
      const og = qd.official_got || {}, rp = qd.repair || {};
      const last = (qd.official || []).slice().reverse();
      $('#aboutSrc').insertAdjacentHTML('beforeend', `<h3 style="margin-top:18px">報價品質（${md(qd.date)}）</h3>
        <dl class="kv num"><dt>官方收盤價</dt><dd>${fmtInt(c.official || 0)} 檔</dd><dt>即時成交價</dt><dd>${fmtInt(c.exact || 0)} 檔</dd>
        <dt>推估價（≈）</dt><dd>${fmtInt(c.est || 0)} 檔${tot ? `（${Math.round((c.est || 0) / tot * 100)}%）` : ''}</dd><dt>今天尚未成交</dt><dd>${fmtInt(c.no_trade || 0)} 檔</dd>
        <dt>官方收盤資料</dt><dd>上市 ${og.TWSE ? md(og.TWSE) : '—'}・上櫃 ${og.TPEx ? md(og.TPEx) : '—'}（已存 ${qd.archive_days || 0} 天）</dd>
        ${rp.total ? `<dt>舊版 K 線修正</dt><dd>${rp.left ? `進行中，剩 ${fmtInt(rp.left)}／${fmtInt(rp.total)} 檔` : `已完成（${esc(rp.done || '')}）`}</dd>` : ''}</dl>
        ${last.length ? `<div class="table-scroll"><table class="tbl" style="font-size:12.5px"><thead><tr><th>日 K 核對</th><th class="n">核對</th><th class="n">修正價格</th><th class="n">補上</th></tr></thead><tbody>
          ${last.map((x) => `<tr style="cursor:default"><td>${md(x.date)}</td><td class="n">${fmtInt(x.checked)}</td><td class="n">${fmtInt(x.fixed)}</td><td class="n">${fmtInt(x.appended)}</td></tr>`).join('')}</tbody></table></div>
          ${last[0].examples && last[0].examples.length ? `<div class="muted small">例：${last[0].examples.slice(0, 4).map((x) => `${esc(x.name)} ${md(x.date)} ${fmtP(x.before)} → ${fmtP(x.after)}`).join('；')}</div>` : ''}` : ''}
        <div class="muted small">盤中證交所只在剛好有成交的那一刻揭示成交價，其餘時間程式依最佳一檔買賣價推估（畫面上標「≈」或「估」）。收盤後約 13:50 起，程式會改用證交所／櫃買中心公布的官方收盤資料，並自動修正歷史日 K。</div>`);
    }
    const c = d.config || {};
    $('#aboutCfg').innerHTML = `<h3>目前設定</h3>
      <dl class="kv num"><dt>抓歷史資料門檻</dt><dd>當日成交值 ≥ ${fmtInt(c.hist_min_trade_value / 1e4)} 萬元</dd>
      <dt>前十名流動性門檻</dt><dd>20 日均成交值 ≥ ${fmtInt(c.top_min_avg_value / 1e4)} 萬元</dd>
      <dt>前十名包含 ETF</dt><dd>${c.include_etf_in_top ? '是' : '否'}</dd>
      <dt>盤中重新評分</dt><dd>每 ${c.reanalyze_seconds_open} 秒</dd><dt>盤中完整重掃</dt><dd>每 ${c.rescan_minutes_open} 分鐘</dd>
      <dt>三大法人資料天數</dt><dd>${(d.chips_days || []).length} 天</dd></dl>
      <p style="font-size:13px">要調整門檻，用記事本打開程式資料夾裡的 <code>config.json</code>，修改後重新啟動程式即可。</p>`;
  } catch (e) { $('#aboutSrc').innerHTML = '<h3>資料來源狀態</h3><div class="muted">無法讀取。</div>'; }
}

// ---------------------------------------------------------------- 啟動
pollStatus().then(route);
setInterval(pollStatus, CLOUD ? 15000 : 5000);
