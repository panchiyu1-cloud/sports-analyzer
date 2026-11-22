'use strict';
/* 模擬戰（當沖）：假想帳戶照「當沖模式」的建議自動買賣，看績效。
   規則與成交都在掃描程式那一邊算（twscan/sim.py）；這裡只負責顯示。共用的小工具（$、api、esc、pct…）定義在 app.js。 */
const SIM = { data: null, hist: null, tab: null, open: {}, loaded: 0, histN: -1, msg: '', sure: 0, mode: 'live', bt: null, btVar: 'main', btTried: false, study: null };
try { SIM.tab = localStorage.getItem('twscan.simTab'); } catch (e) { /* 無痕模式 */ }

const simMoney = (n) => (n == null ? '—' : fmtInt(n) + ' 元');
const simR = (r) => (r == null ? '' : `${r > 0 ? '+' : ''}${Number(r).toFixed(2)}R`);
const SIM_ST = { run: 'bull', wait: 'warn', daystop: 'warn', pause: 'warn', halt: 'bear', closed: 'info' };
const SIM_REASON = { target: ['bull', '到目標價'], stop: ['bear', '停損'], time: ['info', '收盤前出場'], close: ['info', '收盤價結算'] };

async function simLoad() {
  let d;
  try { d = await api('/api/sim'); } catch (e) { return; }
  if (!d) return;
  SIM.data = d;
  SIM.loaded = Date.now();
  if (d.on && d.hist_n !== SIM.histN && (SIM.tab === 'hist' || SIM.hist)) simHistLoad();
  if (S.view === 'sim') simRender();
}

async function simHistLoad() {
  let h;
  try { h = await api('/api/sim/hist'); } catch (e) { return; }
  if (!h) return;
  SIM.hist = h;
  SIM.histN = h.n;
  if (S.view === 'sim' && SIM.tab === 'hist') simRender();
}

function showSim() {
  S.view = 'sim';
  $('#view').innerHTML = `<section class="section"><div class="section-head"><h2>當沖模擬戰</h2><span class="sub">假想的帳戶，照「當沖模式」的建議自動買賣；不會真的下單</span></div>
    <div id="simMode"></div><div id="simRoot"><div class="panel empty">載入中…</div></div></section>`;
  if (SIM.data) simRender();
  simLoad();
  simBtLoad();
  S.timers.push(setInterval(() => { if (fastMode() || Date.now() - SIM.loaded > 120000) simLoad(); }, 20000));
}

// ---------------------------------------------------------------- 帳戶與績效
function simCurve(d) {
  const pts = d.curve || [];
  if (pts.length < 3) return `<div class="muted small sim-nocurve">權益曲線：累積兩個交易日後顯示。</div>`;
  const W = 320, H = 70, pad = 4;
  const vals = pts.map((p) => p[1]).concat([d.start]);
  const lo = Math.min(...vals), hi = Math.max(...vals), span = hi - lo || 1;
  const x = (i) => pad + (i / (pts.length - 1)) * (W - 2 * pad);
  const y = (v) => pad + ((hi - v) / span) * (H - 2 * pad);
  const last = pts[pts.length - 1][1], cl = clsOf(last - d.start) || 'flat';
  return `<div class="sim-curve"><svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="每天收盤後的帳戶餘額">
      <line x1="0" x2="${W}" y1="${y(d.start).toFixed(1)}" y2="${y(d.start).toFixed(1)}" class="l-base"/>
      <polyline points="${pts.map((p, i) => `${x(i).toFixed(1)},${y(p[1]).toFixed(1)}`).join(' ')}" class="l-eq ${cl}"/>
      <circle cx="${x(pts.length - 1).toFixed(1)}" cy="${y(last).toFixed(1)}" r="2.8" class="l-eqdot ${cl}"/></svg>
    <div class="sim-curve-ax"><span>${md(pts[0][0])} 起始 ${fmtInt(d.start)}</span><span>虛線＝起始資金</span><span>${md(pts[pts.length - 1][0])} <b class="num ${cl}">${fmtInt(last)}</b></span></div></div>`;
}

function simAccount(d, bt) {
  const a = d.account, t = d.today, st = d.stats || { n: 0 };
  const upd = d.now ? d.now.slice(5, 16).replace('-', '/') : '';
  const total = a.equity - d.start;
  const top = bt ? `<span class="st info">回測結果</span><span class="muted">${md(bt.from)}–${md(bt.to)}・${bt.n_days} 個交易日${bt.demo ? '・示範資料' : ''}</span>`
    : `<span class="st ${SIM_ST[a.state] || 'info'}">${a.state === 'run' ? '● ' : ''}${esc(a.state_text)}</span>
      <span class="muted">${esc(upd)} 更新${d.demo ? '・示範資料' : ''}</span>`;
  const third = bt ? `<div class="rv-m"><div class="l">最大回落</div><div class="v num">${fmtInt(a.max_dd)}</div><div class="s">${a.max_dd ? '從高點最多少掉 ' + a.max_dd_pct + '%' : '—'}</div></div>`
    : `<div class="rv-m"><div class="l">今天</div><div class="v num ${clsOf(t.pnl)}">${t.ran || t.n ? signed(t.pnl) : '—'}</div><div class="s">${t.n ? `已結束 ${t.n} 筆・賺 ${t.win} 筆` : t.ran ? '還沒有結束的交易' : '今天還沒開始'}</div></div>`;
  const sub = bt ? `<b class="num">${d.days_run}</b> 個交易日裡 <b class="num">${d.days_traded}</b> 天有進場（賺 ${d.days_up} 天、賠 ${d.days_down} 天）`
    : `可用現金 <b class="num">${fmtInt(a.avail)}</b>・已投入 <b class="num">${fmtInt(a.used)}</b>${a.float ? `・浮動損益 <b class="num ${clsOf(a.float)}">${signed(a.float)}</b>` : ''}
      ・最大回落 <b class="num">${fmtInt(a.max_dd)}</b>${a.max_dd ? `（${a.max_dd_pct}%）` : ''}・執行 <b class="num">${d.days_run}</b> 天${d.days_traded ? `（賺 ${d.days_up} 天、賠 ${d.days_down} 天）` : ''}`;
  return `<div class="panel sim-acct">
    <div class="sim-top">${top}</div>
    <div class="rv-metrics sim-metrics">
      <div class="rv-m"><div class="l">帳戶權益</div><div class="v num">${fmtInt(a.equity)}</div><div class="s">起始 ${fmtInt(d.start)} 元</div></div>
      <div class="rv-m"><div class="l">總損益</div><div class="v num ${clsOf(total)}">${signed(total)}</div><div class="s num ${clsOf(a.ret_pct)}">${pct(a.ret_pct)}</div></div>
      ${third}
      <div class="rv-m"><div class="l">勝率</div><div class="v num">${st.n ? st.win_rate + '%' : '—'}</div><div class="s">${st.n ? `${st.n} 筆裡賺 ${st.win} 筆` : '還沒有交易'}</div></div>
    </div>
    <div class="sim-sub t2">${sub}</div>
    ${simCurve(d)}</div>`;
}

function simStats(d) {
  const s = d.stats || { n: 0 };
  if (!s.n) return `<div class="panel"><h3>績效</h3><div class="muted" style="font-size:13.5px">還沒有結束的交易。有了第一筆之後，這裡會列出勝率、平均每筆賺賠、賺賠比、成本與各種出場原因的結果。</div></div>`;
  const by = s.by || {};
  const reason = Object.keys(SIM_REASON).filter((k) => by[k]).map((k) => `<tr style="cursor:default"><td><span class="st ${SIM_REASON[k][0]}">${SIM_REASON[k][1]}</span></td><td class="n num">${by[k].n} 筆</td><td class="n num ${clsOf(by[k].pnl)}">${signed(by[k].pnl)}</td></tr>`).join('');
  const kv = (l, v) => `<div><span>${l}</span><b class="num">${v}</b></div>`;
  return `<div class="panel"><h3>績效 <span class="muted" style="font-weight:400;font-size:12.5px">${esc(d.created.slice(0, 10))} 起・已扣手續費與證交稅</span></h3>
    ${s.n < 30 ? `<div class="rv-why"><p>目前只有 <b>${s.n} 筆</b>，樣本太少，賺賠多半是運氣。至少累積 30 筆再看這套做法行不行。</p></div>` : ''}
    <div class="sim-kv">
      ${kv('平均每筆', `<span class="${clsOf(s.avg)}">${signed(s.avg)}</span> 元`)}
      ${kv('平均賺／平均賠', `${s.avg_win != null ? '<span class="up">' + signed(s.avg_win) + '</span>' : '—'}／${s.avg_loss != null ? '<span class="down">' + signed(s.avg_loss) + '</span>' : '—'}`)}
      ${kv('賺賠比（總賺 ÷ 總賠）', s.pf != null ? s.pf.toFixed(2) : '—')}
      ${kv('平均 R（賠滿一次停損＝−1）', `<span class="${clsOf(s.avg_r)}">${simR(s.avg_r) || '—'}</span>`)}
      ${kv('平均持有', s.avg_hold + ' 分鐘')}
      ${kv('手續費與證交稅合計', fmtInt(s.cost) + ' 元')}
      ${kv('最好的一筆', `<span class="up">${signed(s.best.pnl)}</span> <small>${esc(s.best.name)} ${md(s.best.date)}</small>`)}
      ${kv('最差的一筆', `<span class="down">${signed(s.worst.pnl)}</span> <small>${esc(s.worst.name)} ${md(s.worst.date)}</small>`)}
    </div>
    <table class="tbl sim-by"><tbody>${reason}</tbody></table></div>`;
}

function simDays(d, n) {
  const rows = (d.days || []).slice(0, n || 20);
  if (!rows.length) return `<div class="panel"><h3>每天的結果</h3><div class="muted" style="font-size:13.5px">程式在交易時間執行過的日子會列在這裡（沒有進場的日子也算一天）。</div></div>`;
  return `<div class="panel"><h3>每天的結果</h3><div class="table-scroll"><table class="tbl rv-tbl"><thead><tr><th>日期</th><th class="n">筆數</th><th class="n">當天損益</th><th class="n">收盤後餘額</th></tr></thead><tbody>
    ${rows.map((r) => `<tr style="cursor:default"><td>${md(r.date)}（${wd(r.date)}）</td><td class="n num">${r.n ? `${r.n}<span class="muted"> 賺${r.win}</span>` : '<span class="muted">沒進場</span>'}</td>
      <td class="n num ${clsOf(r.pnl)}">${r.n ? signed(r.pnl) : '—'}</td><td class="n num">${fmtInt(r.end)}</td></tr>`).join('')}</tbody></table></div></div>`;
}

// ---------------------------------------------------------------- 四個分頁
function simSpark(o) {
  const px = o.path || [];
  if (px.length < 3) return '';
  const W = 300, H = 62, pad = 3;
  const vals = px.map((p) => p[1]).concat([o.entry, o.stop]);
  if (o.target <= Math.max(...vals) * 1.015) vals.push(o.target);
  const lo = Math.min(...vals), hi = Math.max(...vals), span = hi - lo || 1;
  const m = (t) => +t.slice(0, 2) * 60 + +t.slice(3, 5);
  const t0 = m(px[0][0]), t1 = Math.max(m(px[px.length - 1][0]), t0 + 20);
  const x = (t) => pad + ((m(t) - t0) / (t1 - t0)) * (W - 2 * pad);
  const y = (v) => pad + ((hi - v) / span) * (H - 2 * pad);
  const line = (v, cls) => (v >= lo && v <= hi ? `<line x1="0" x2="${W}" y1="${y(v).toFixed(1)}" y2="${y(v).toFixed(1)}" class="${cls}"/>` : '');
  const last = px[px.length - 1], cl = clsOf(last[1] - o.entry);
  return `<div class="dtc-chart"><svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="買進後的走勢">
      ${line(o.target, 'l-target')}${line(o.stop, 'l-stop')}${line(o.entry, 'l-open')}
      <polyline points="${px.map((p) => `${x(p[0]).toFixed(1)},${y(p[1]).toFixed(1)}`).join(' ')}" class="l-px ${cl}"/><circle cx="${x(last[0]).toFixed(1)}" cy="${y(last[1]).toFixed(1)}" r="2.6" class="l-dot ${cl}"/></svg>
    <div class="dtc-legend"><span><i class="k-px"></i>買進後走勢</span><span><i class="k-open"></i>買進價</span><span><i class="k-stop"></i>停損</span><span><i style="border-top-style:dashed;border-color:var(--up)"></i>目標</span></div></div>`;
}

function simPlan(c, entryNote) {
  return `<div class="dtc-px">
      <div><span>買進價</span><b class="num">${fmtP(c.entry)}</b><small>${entryNote}</small></div>
      <div><span>停損價</span><b class="num down">${fmtP(c.stop)}</b><small class="num">${pct(c.stop_pct, 1)}</small></div>
      <div><span>目標價</span><b class="num up">${fmtP(c.target)}</b><small class="num">${pct(c.target_pct, 1)}</small></div>
      <div><span>張數</span><b class="num">${c.lots} 張</b><small class="num">${fmtInt(c.amount)} 元</small></div>
    </div>`;
}

function simHead(c, sub) {
  const cl = clsOf(c.chgp != null ? c.chgp : c.price != null && c.entry ? c.price - c.entry : null);
  return `<div class="card-head">${c.rank ? `<div class="rank">${c.rank}</div>` : ''}
      <div class="title"><a href="#stock-${c.code}" data-tf="30m"><span class="nm">${esc(c.name)}</span><span class="cd">${c.code}</span></a><div class="meta">${sub}</div></div>
      ${c.price != null ? `<div class="quote"><div class="price num ${cl}">${estMark(c.est)}${fmtP(c.price)}</div>${c.chgp != null ? `<div class="chg num ${cl}">${arrow(c.chgp)} ${pct(c.chgp)}</div>` : ''}</div>` : ''}</div>`;
}

function simWatch(d) {
  if (!d.live) return `<div class="panel empty"><div class="big">交易時間才有觀察名單</div>交易日 ${esc(d.rules.entry_from)} 起，程式會從即時名單挑出帳戶買得起的前 ${d.rules.cands} 名放在這裡，並說明每一檔還缺什麼條件。條件齊了會自動移到「準備中」。</div>`;
  if (!d.watch.length) return `<div class="panel empty">目前沒有觀察中的股票。${d.excluded ? `有 ${d.excluded} 檔排名在前面，但帳戶買不起一張。` : ''}</div>`;
  const card = (c) => {
    const checks = (c.checks || []).map((k) => `<li class="${k.ok === true ? 'ok' : k.ok === false ? 'no' : 'na'}">${k.ok === true ? '✓' : k.ok === false ? '✕' : '·'} ${esc(k.text)}</li>`).join('');
    const st = c.blocked ? 'wait' : c.status;
    return `<article class="dtc simc ${st}">
      ${simHead(c, `即時名單第 ${c.rank} 名・盤中評分 ${c.score ?? '—'}`)}
      <div class="dtc-st"><span class="dts ${st}">${c.blocked ? '條件齊了，但不買' : esc(c.label)}</span><span class="why">${esc(c.why || '')}</span></div>
      <details class="sim-more"><summary>如果現在買的計畫與各項條件</summary>
        ${simPlan(c, '現在的賣一價')}
        <div class="dtc-risk">碰到停損約 <b class="down num">${signed(c.loss)} 元</b>，到目標約 <b class="up num">${signed(c.gain)} 元</b>（已扣成本）</div>
        <ul class="dtc-checks">${checks}</ul></details></article>`;
  };
  return `<div class="dt-cards">${d.watch.map(card).join('')}</div>
    ${d.excluded ? `<div class="muted small">另有 ${d.excluded} 檔排名更前面，但一張的金額超過單筆上限（帳戶的 ${d.rules.pos_pct}%），或一張碰停損就超過每筆風險上限，已略過。</div>` : ''}`;
}

function simReady(d) {
  if (!d.ready.length) return `<div class="panel empty"><div class="big">沒有準備中的單</div>觀察中的股票五項條件都符合（當沖模式的「可進場」）、帳戶也還有空位時，會先到這裡等 ${d.rules.confirm_sec} 秒；條件還在才用當時的賣一價買進，避免一閃而過的訊號。</div>`;
  const card = (c) => `<article class="dtc simc wait">
      ${simHead(c, `${esc(c.sig_time.slice(0, 5))} 出現「可進場」・即時名單第 ${c.rank ?? '—'} 名`)}
      <div class="dtc-st"><span class="dts wait">⏳ 確認中</span><span class="why">已等 ${c.waited} 秒（至少 ${d.rules.confirm_sec} 秒；${Math.round(d.rules.confirm_max_sec / 60)} 分鐘內條件沒回來就取消）</span></div>
      ${simPlan(c, '預計，以成交時為準')}
      <div class="dtc-risk">買進後碰到停損約 <b class="down num">${signed(-c.risk)} 元</b>，到目標約 <b class="up num">${signed(c.gain)} 元</b>（已扣成本）</div></article>`;
  return `<div class="dt-cards">${d.ready.map(card).join('')}</div>`;
}

function simTradeRow(t, withDate) {
  const [cls, txt] = SIM_REASON[t.reason] || ['info', t.reason_text || t.reason];
  const opened = SIM.open[t.id];
  return `<div class="sim-row ${opened ? 'on' : ''}" data-sim-row="${esc(t.id)}">
      <div class="sim-row-main"><div class="sim-row-name"><span class="nm">${esc(t.name)}</span> <span class="cd">${t.code}</span>
          <small class="num">${withDate ? md(t.date) + ' ' : ''}${esc(t.time.slice(0, 5))} → ${esc(t.exit_time.slice(0, 5))}・${fmtP(t.entry)} → ${fmtP(t.exit)} × ${t.lots} 張</small></div>
        <div class="sim-row-pnl"><b class="num ${clsOf(t.pnl)}">${signed(t.pnl)}</b><small><span class="st ${cls}">${txt}</span></small></div></div>
      ${opened ? simTradeDetail(t) : ''}</div>`;
}

function simTradeDetail(t) {
  const c = t.ctx || {};
  const kv = (l, v) => `<div><span>${l}</span><b class="num">${v}</b></div>`;
  const idx = [c.taiex != null ? `加權 ${pct(c.taiex)}` : '', c.tpex != null ? `櫃買 ${pct(c.tpex)}` : ''].filter(Boolean).join('・');
  return `<div class="sim-detail">
      ${simSpark(t)}
      <div class="sim-kv">
        ${kv('買進', `${esc(t.time)}　${fmtP(t.entry)} × ${t.lots} 張`)}
        ${kv('賣出', `${esc(t.exit_time)}　${fmtP(t.exit)}（${esc(t.reason_text || '')}）`)}
        ${kv('買進金額', simMoney(t.amount))}
        ${kv('停損價／目標價', `${fmtP(t.stop)}／${fmtP(t.target)}`)}
        ${kv('損益（已扣成本）', `<span class="${clsOf(t.pnl)}">${signed(t.pnl)} 元</span>`)}
        ${kv('報酬率／占帳戶', `<span class="${clsOf(t.ret)}">${pct(t.ret)}</span>／<span class="${clsOf(t.eq_pct)}">${pct(t.eq_pct)}</span>`)}
        ${kv('R 值（賠滿停損＝−1）', `<span class="${clsOf(t.r)}">${simR(t.r) || '—'}</span>`)}
        ${kv('原本最多賠／到目標賺', `${fmtInt(-t.risk)}／${signed(t.gain)}`)}
        ${kv('手續費＋證交稅', `${fmtInt(t.fee)}＋${fmtInt(t.tax)} 元`)}
        ${kv('持有時間', (t.hold_min ?? '—') + ' 分鐘')}
        ${kv('途中最多曾賺／曾賠', `${simR(t.mfe_r) || '—'}／${simR(t.mae_r) || '—'}`)}
        ${kv('出場後餘額', simMoney(t.bal_after))}
        ${kv('進場時的排名／評分', `第 ${c.rank ?? t.rank ?? '—'} 名／${c.score ?? t.score ?? '—'}`)}
        ${kv('進場時大盤', idx || '—')}
      </div>
      ${t.checks && t.checks.length ? `<div class="muted small">進場理由：${t.checks.map(esc).join('；')}</div>` : ''}
      ${t.events && t.events.length ? `<ul class="sim-events">${t.events.map((e) => `<li><span class="num muted">${esc(e[0].slice(0, 8))}</span> ${esc(e[1])}</li>`).join('')}</ul>` : ''}
      ${t.approx ? '<div class="muted small">這一筆是程式中斷後用當天的日線補算的，出場價是估計。</div>' : ''}
      <div class="small"><a href="#stock-${t.code}" data-tf="30m" style="color:var(--accent);font-weight:600">看 ${esc(t.name)} 的 K 線 →</a></div></div>`;
}

function simOpen(d) {
  const card = (o) => {
    const held = d.now && o.time ? Math.max(0, (+d.now.slice(11, 13) * 60 + +d.now.slice(14, 16)) - (+o.time.slice(0, 2) * 60 + +o.time.slice(3, 5))) : null;
    return `<article class="dtc simc ${o.pnl > 0 ? 'go' : o.pnl < 0 ? 'bear' : ''}">
      ${simHead(o, `${esc(o.time.slice(0, 5))} 買進 ${fmtP(o.entry)} × ${o.lots} 張・投入 ${fmtInt(o.amount)} 元`)}
      <div class="dtc-px">
        <div><span>浮動損益</span><b class="num ${clsOf(o.pnl)}">${o.pnl != null ? signed(o.pnl) : '—'}</b><small class="num ${clsOf(o.r)}">${pct(o.ret)}・${simR(o.r)}</small></div>
        <div><span>買進價</span><b class="num">${fmtP(o.entry)}</b><small class="num">現價 ${fmtP(o.price)}</small></div>
        <div><span>停損價</span><b class="num down">${fmtP(o.stop)}</b><small class="num">最多賠 ${fmtInt(o.risk)}</small></div>
        <div><span>目標價</span><b class="num up">${fmtP(o.target)}</b><small class="num">約賺 ${fmtInt(o.gain)}</small></div>
      </div>
      ${simSpark(o)}
      <div class="dtc-risk">已持有 ${held ?? '—'} 分鐘・最晚 ${esc(d.rules.exit_at)} 出場${o.locked ? '・<b class="down">跌停鎖住，暫時賣不掉</b>' : ''}</div></article>`;
  };
  const done = d.trades_today || [];
  return `${d.open.length ? `<div class="dt-cards">${d.open.map(card).join('')}</div>` : `<div class="panel empty"><div class="big">目前沒有持有中的部位</div>買進之後會在這裡顯示買進時間與價格、金額、停損與目標價、浮動損益；碰到停損價、目標價或 ${esc(d.rules.exit_at)} 到了就自動賣出。</div>`}
    ${done.length ? `<div class="panel" style="margin-top:14px"><h3>今天已經結束的交易</h3>${done.map((t) => simTradeRow(t, false)).join('')}</div>` : ''}`;
}

function simHist(d, hh) {
  const h = hh || SIM.hist;
  if (!h) { simHistLoad(); return '<div class="panel empty">載入交易紀錄…</div>'; }
  if (!h.trades.length) return `<div class="panel empty"><div class="big">還沒有交易紀錄</div>每一筆結束的模擬交易都會記在這裡：進出場的時間與價格、張數與金額、停損停利、出場原因、損益與成本、途中最多賺賠多少、當時的排名與大盤。點一筆可以展開。</div>`;
  let last = null;
  const rows = h.trades.map((t) => {
    const head = t.date !== last ? `<div class="sim-date">${md(t.date)}（${wd(t.date)}）</div>` : '';
    last = t.date;
    return head + simTradeRow(t, false);
  }).join('');
  return `<div class="panel"><div class="sim-hist-head"><h3>模擬交易歷史 <span class="muted" style="font-weight:400;font-size:12.5px">共 ${h.n} 筆・點一筆看全部資料</span></h3>
      <button class="btn" data-sim-act="csv">匯出 CSV</button></div>${rows}
      ${h.n > h.trades.length ? `<div class="muted small">只列出最近 ${h.trades.length} 筆。</div>` : ''}</div>`;
}

function simRules(d) {
  const r = d.rules;
  return `<details class="panel sim-rules"><summary>完整規則（這個帳戶怎麼買、怎麼賣、怎麼算錢）</summary>
    <ul>
      <li><b>資金</b>：起始 ${fmtInt(d.start)} 元。只用帳戶裡的錢，買進金額合計不超過餘額（不借錢、不放大）。</li>
      <li><b>買什麼</b>：只做「當沖模式」判定為「可進場」的股票（即時名單裡帳戶買得起的前 ${r.cands} 名；五項條件都符合），只做多（先買後賣）。進場時間 ${esc(r.entry_from)}–${esc(r.entry_to)}。</li>
      <li><b>買多少</b>：單筆不超過餘額的 ${r.pos_pct}%，而且碰到停損（含成本）最多賠餘額的 ${r.risk_pct}%；只買整張。同時最多 ${r.max_pos} 檔，同一檔一天只做一次。</li>
      <li><b>怎麼買</b>：出現「可進場」先進「準備中」，至少等 ${r.confirm_sec} 秒再確認一次，條件還在就用當時的賣一價買進；${Math.round(r.confirm_max_sec / 60)} 分鐘內條件沒回來就取消。</li>
      <li><b>怎麼賣</b>：停損價、目標價用當沖模式算出來的那一組（目標＝停損距離的 2 倍）。碰到目標價以目標價成交；碰到停損價就賣，發現時已經跌破的話用當時的買一價（比停損價差）。兩次報價之間同時碰到兩邊算停損。${esc(r.exit_at)} 還沒出場的，用買一價全部賣出。</li>
      <li><b>停手</b>：當天已經賠到當天開始時餘額的 ${r.day_stop_pct}%，今天不再進場。</li>
      <li><b>成本</b>：手續費 0.1425% × ${(r.fee_discount * 10).toFixed(1)} 折（買賣各一次，每次最低 ${r.min_fee} 元），當沖證交稅 ${r.tax}%（賣出時）。每一筆損益都已經扣掉。</li>
    </ul>
    <div class="muted small">和實際下單的差別：雲端大約每幾秒讀一次這些股票的報價，成交價是用讀到的價格算的，沒有算你的單排在別人後面、或大單把價格推開的情況，所以實際會比模擬差一點。處置股、注意股可能不能現股當沖，程式不會判斷。跌停鎖住賣不掉時，以收盤價結算。
      這個帳戶的目的是替「當沖模式」的建議累積一份誠實的成績單，不是保證會賺；規則的數字都還沒有經過驗證。<b>以上不是投資建議。</b></div></details>`;
}

function simLog(d) {
  const lg = d.log || [];
  if (!lg.length) return '';
  const ic = { buy: '＋', sell: '－', signal: '⏳', cancel: '✕', warn: '⚠', info: '·' };
  return `<details class="panel sim-log" ${SIM.logOpen ? 'open' : ''}><summary>最近動態（${lg.length} 則）</summary><ul class="dt-alerts">${lg.map((x) => `<li class="${x.kind === 'warn' ? 'stop' : ''}"><span class="num muted">${md(x.date)} ${esc(x.time.slice(0, 5))}</span><span class="ic">${ic[x.kind] || '·'}</span><div>${esc(x.text)}</div></li>`).join('')}</ul></details>`;
}

function simControls(d) {
  if (CLOUD) return '<div class="muted small">這個帳戶在雲端自動執行，網頁關著也會照常模擬；網頁上不能暫停或重設。想重新開始或調整規則，請告訴 Claude。</div>';
  return `<div class="filters" style="margin-top:12px"><button class="btn" data-sim-act="${d.account.paused ? 'resume' : 'pause'}">${d.account.paused ? '繼續開新倉' : '暫停開新倉'}</button>
    <button class="btn ghost" data-sim-act="reset">${SIM.sure && Date.now() - SIM.sure < 6000 ? '確定重設？再按一次' : '重設帳戶'}</button><span class="muted small-inline">${esc(SIM.msg)}</span></div>`;
}

// ---------------------------------------------------------------- 回測（stock/backtest_sim.py 的結果，一個固定的檔案）
async function simBtLoad() {
  if (SIM.bt || SIM.btTried) return;
  SIM.btTried = true;
  try {
    const r = await fetch((CLOUD ? '' : '/static/') + 'sim_backtest.json?d=' + new Date().toISOString().slice(0, 10), { cache: 'no-cache' });
    if (!r.ok) return;
    const j = await r.json();
    if (j && j.v === 1 && Array.isArray(j.variants) && j.variants.length) SIM.bt = j;
    const r2 = await fetch((CLOUD ? '' : '/static/') + 'sim_study.json?d=' + new Date().toISOString().slice(0, 10), { cache: 'no-cache' });
    if (r2.ok) { const k = await r2.json(); if (k && k.v === 1 && Array.isArray(k.variants)) SIM.study = k; }
  } catch (e) { if (!SIM.bt) return; }
  if (S.view === 'sim') simRender();
}

function simBtBreak(x) {
  if (!x || !x.n) return '';
  const tbl = (title, rows) => `<h4 class="sim-h4">${title}</h4><table class="tbl rv-tbl"><tbody>${rows.map((r) => `<tr style="cursor:default"><td>${esc(r.k.replace(/^\d /, ''))}</td>
    <td class="n num">${r.n} 筆<span class="muted"> 賺${r.win}</span></td><td class="n num ${clsOf(r.pnl)}">${signed(r.pnl)}</td><td class="n num ${clsOf(r.avg_r)}">${simR(r.avg_r)}</td></tr>`).join('')}</tbody></table>`;
  return `<div class="panel"><h3>拆開來看 <span class="muted" style="font-weight:400;font-size:12.5px">筆數・損益・平均 R</span></h3>
    ${tbl('進場時間', x.hour)}${tbl('股價', x.price)}${tbl('進場時在即時名單的排名', x.rank)}${tbl('進場時的大盤', x.market)}
    <div class="muted small">${x.n} 筆裡有 ${x.reached_1r} 筆途中曾經賺到 1R 以上（1R＝原本最多賠的金額）；平均最多曾賺 ${simR(x.avg_mfe)}、最多曾賠 ${simR(x.avg_mae)}。分得越細每一格的筆數越少，只能當線索。</div></div>`;
}

const SIM_VAR = { main: ['照模擬戰的規則', '買進算賣一價（成交價往上一檔）'], opt: ['樂觀的算法', '買在成交價、停損剛好成交'], nocap: ['單筆上限放寬', '一筆可以用到全部資金'] };

const SIM_STUDY = { base: '現在的規則', regime: '大盤走空不做多', pullback: '不追強，等拉回再買', tick: '避開價差大的價位', all: '三項一起' };

function simStudy() {
  const k = SIM.study;
  if (!k) return '';
  const pass = k.variants.filter((v) => v.verdict === 'pass').length;
  const half = (h) => `<td class="n num ${clsOf(h.pnl)}">${signed(h.pnl)}<small>${h.n} 筆</small></td>`;
  const rows = k.variants.map((v) => `<tr style="cursor:default"><td>${esc(SIM_STUDY[v.key] || v.label)}<small>${v.verdict === 'pass' ? '過關' : v.verdict === 'thin' ? '筆數太少' : '沒過關'}</small></td>
    ${half(v.h1)}${half(v.h2)}<td class="n num ${clsOf(v.all.pnl)}">${signed(v.all.pnl)}<small>${v.all.n} 筆・勝率 ${Math.round(v.all.win_rate || 0)}%</small></td></tr>`).join('');
  const base = k.variants.find((v) => v.key === 'base');
  return `<div class="panel" style="margin-top:14px"><h3>改法研究：${pass ? `有 ${pass} 種過關` : '幾種改法都沒有過關'}</h3>
    <div class="t2" style="font-size:13.5px;line-height:1.75">把回測拉長到 ${k.n_days} 個交易日（${md(k.from)}–${md(k.to)}），分成前後兩半：前半加權指數 <b class="num ${clsOf(k.bench_halves[0])}">${pct(k.bench_halves[0])}</b>，後半 <b class="num ${clsOf(k.bench_halves[1])}">${pct(k.bench_halves[1])}</b>。
      事先講定只測這幾種，而且${esc(k.rule)}。</div>
    <div class="table-scroll" style="margin-top:8px"><table class="tbl rv-tbl sim-var sim-study"><thead><tr><th>改法</th><th class="n">前半</th><th class="n">後半</th><th class="n">全期</th></tr></thead><tbody>${rows}</tbody></table></div>
    <div class="muted small">${base ? `現在的規則在大盤上漲的前半段也賠（${base.h1.n} 筆，${signed(base.h1.pnl)} 元），所以問題不只是後半段的大跌。` : ''}改法主要是讓進場次數變少、賠得少一點；沒有一種把訊號變成賺錢的。
      做法：大盤走空＝加權在月線之下、或進場當時不是上漲；等拉回＝限價單掛在當日均價、30 分鐘有效、價格穿過才算成交；價差大＝一檔跳動超過股價的 0.25%。</div></div>`;
}

function simBtView() {
  const b = SIM.bt;
  const v = b.variants.find((x) => x.key === SIM.btVar) || b.variants[0];
  const d = v.api;
  const rows = b.variants.map((x) => {
    const s = x.api.stats || { n: 0 }, a = x.api.account;
    const nm = SIM_VAR[x.key] || [x.label, ''];
    return `<tr data-sim-var="${esc(x.key)}" class="${x.key === v.key ? 'on' : ''}"><td>${x.key === v.key ? '● ' : ''}${esc(nm[0])}<small>${esc(nm[1])}</small></td><td class="n num">${s.n || 0}</td><td class="n num">${s.n ? Math.round(s.win_rate) + '%' : '—'}</td>
      <td class="n num ${clsOf(s.pnl)}">${s.n ? signed(s.pnl) : '—'}</td><td class="n num">${fmtInt(a.max_dd)}</td></tr>`;
  }).join('');
  const bench = b.bench ? `同一段時間加權指數 <b class="num ${clsOf(b.bench.pct)}">${pct(b.bench.pct)}</b>。` : '';
  return `<div class="panel sim-bt-intro"><h3>回測：把過去 ${b.n_days} 個交易日重播一遍</h3>
      <div class="t2" style="font-size:13.5px;line-height:1.75">${md(b.from)}–${md(b.to)}。用和模擬戰同一套程式（即時排名 → 當沖模式的「可進場」→ 模擬戰的買賣規則），餵它當時的 5 分 K。${bench}</div>
      <details class="sim-more" style="margin-top:8px"><summary>和實際執行不一樣的地方</summary>
        <ul class="sim-ul"><li>實際每幾秒看一次報價；回測每 ${b.step_min} 分鐘一次，「等 60 秒再確認」變成等下一根 5 分 K。</li>
        <li>沒有過去的內外盤資料：「外盤比 ≥ 50%」這個條件沒有檢查，所以回測的進場次數會比實際多一些。</li>
        <li>沒有當時的新聞與三大法人資料：排名只用技術面。${b.tpex_proxy ? '櫃買指數沒有 5 分鐘資料，上櫃股票的「大盤偏弱」改看加權指數。' : ''}</li>
        <li>買進算「最後成交價往上一檔」，停損算「停損價往下一檔」（跳空跌破用開盤價），同一根 K 棒同時碰到停損與目標算停損。</li>
        <li>處置股、不能現股當沖的股票分不出來；${b.n_days} 天只是一小段行情，不代表以後。</li>
        ${b.quality && b.quality.stock_days ? `<li>資料核對：5 分 K 合起來的每日最高、最低價，${(100 - b.quality.hilo_off_pct).toFixed(1)}% 和日 K 一致（${fmtInt(b.quality.stock_days)} 筆）。Yahoo 的 5 分 K 多半沒有 13:30 最後一盤，但模擬戰 13:15 就出場，不受影響。</li>` : ''}</ul></details>
      <div class="table-scroll" style="margin-top:10px"><table class="tbl rv-tbl sim-var"><thead><tr><th>三種算法（點一列看）</th><th class="n">筆數</th><th class="n">勝率</th><th class="n">總損益</th><th class="n">回落</th></tr></thead><tbody>${rows}</tbody></table></div>
      <div class="muted small">回落＝帳戶從最高點最多少掉多少元。出現「可進場」${v.signals} 次，其中 ${v.cancels} 次等到下一根 K 棒條件已經不在，沒有買。回測時間 ${esc(b.generated)}。</div></div>
    ${simStudy()}
    <div style="margin-top:14px">${simAccount(d, b)}</div>
    <div class="grid2" style="margin-top:14px">${simStats(d)}${simDays(d, 60)}</div>
    ${simBtBreak(v.extra)}
    <div style="margin-top:14px">${v.hist ? simHist(d, v.hist) : '<div class="panel empty">交易明細只列在第一種算法；其他兩種只比較總結果。</div>'}</div>`;
}

function simRender() {
  const root = $('#simRoot'), d = SIM.data;
  const modeBox = $('#simMode');
  if (modeBox) {
    modeBox.innerHTML = SIM.bt ? `<div class="seg sim-mode"><button data-sim-mode="live" class="${SIM.mode === 'live' ? 'on' : ''}">實際紀錄</button><button data-sim-mode="bt" class="${SIM.mode === 'bt' ? 'on' : ''}">回測 ${SIM.bt.n_days} 天</button></div>` : '';
  }
  if (root && SIM.mode === 'bt' && SIM.bt) { root.innerHTML = simBtView(); return; }
  if (!root || !d) return;
  if (!d.on) {
    root.innerHTML = `<div class="panel empty"><div class="big">模擬戰的資料還沒有產生</div>${CLOUD ? '雲端主機下一次執行（交易日早上，或傍晚的掃描）之後就會出現。' : '請稍候，程式啟動後就會出現。'}</div>`;
    return;
  }
  const more = root.querySelector('.sim-rules'), lg = root.querySelector('.sim-log');
  SIM.rulesOpen = more ? more.open : SIM.rulesOpen;
  SIM.logOpen = lg ? lg.open : SIM.logOpen;
  const openMore = new Set($$('.sim-more[open]', root).map((el) => el.closest('article').querySelector('.cd').textContent));
  const n = { watch: d.watch.length, ready: d.ready.length, open: d.open.length, hist: d.hist_n };
  if (!['watch', 'ready', 'open', 'hist'].includes(SIM.tab)) SIM.tab = n.open ? 'open' : n.ready ? 'ready' : d.live ? 'watch' : n.hist ? 'hist' : 'watch';
  const tabs = [['watch', '觀察中'], ['ready', '準備中'], ['open', '交易中'], ['hist', '歷史']];
  const body = SIM.tab === 'watch' ? simWatch(d) : SIM.tab === 'ready' ? simReady(d) : SIM.tab === 'open' ? simOpen(d) : simHist(d);
  root.innerHTML = `${simAccount(d)}
    <div class="seg sim-tabs" role="tablist">${tabs.map(([k, l]) => `<button role="tab" data-sim-tab="${k}" class="${SIM.tab === k ? 'on' : ''}">${l}<i class="num ${n[k] && k !== 'hist' && k !== 'watch' ? 'hot' : ''}">${n[k]}</i></button>`).join('')}</div>
    <div id="simBody">${body}</div>
    <div class="grid2">${simStats(d)}${simDays(d)}</div>
    ${simLog(d)}
    ${simRules(d)}
    ${simControls(d)}`;
  if (SIM.rulesOpen) root.querySelector('.sim-rules').open = true;
  $$('.sim-more', root).forEach((el) => { if (openMore.has(el.closest('article').querySelector('.cd').textContent)) el.open = true; });
}

// ---------------------------------------------------------------- 匯出
function simCsv() {
  const h = SIM.mode === 'bt' && SIM.bt ? (SIM.bt.variants.find((x) => x.hist) || {}).hist : SIM.hist;
  if (!h || !h.trades.length) return;
  const cols = [['日期', 'date'], ['代號', 'code'], ['名稱', 'name'], ['訊號時間', 'sig_time'], ['買進時間', 'time'], ['買進價', 'entry'], ['張數', 'lots'], ['買進金額', 'amount'],
    ['停損價', 'stop'], ['目標價', 'target'], ['賣出時間', 'exit_time'], ['賣出價', 'exit'], ['出場原因', 'reason_text'], ['損益', 'pnl'], ['報酬率%', 'ret'], ['占帳戶%', 'eq_pct'], ['R', 'r'],
    ['原本最多賠', 'risk'], ['手續費', 'fee'], ['證交稅', 'tax'], ['持有分鐘', 'hold_min'], ['途中最多賺R', 'mfe_r'], ['途中最多賠R', 'mae_r'], ['出場後餘額', 'bal_after'],
    ['排名', (t) => (t.ctx || {}).rank ?? t.rank], ['評分', (t) => (t.ctx || {}).score ?? t.score], ['加權%', (t) => (t.ctx || {}).taiex], ['櫃買%', (t) => (t.ctx || {}).tpex],
    ['離均價%', (t) => (t.ctx || {}).ext], ['量比', (t) => (t.ctx || {}).vol_ratio], ['外盤比%', (t) => (t.ctx || {}).outer], ['連續在榜分鐘', (t) => (t.ctx || {}).stable], ['補算', (t) => (t.approx ? '是' : '')]];
  const cell = (v) => { const s = v == null ? '' : String(v); return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s; };
  const lines = [cols.map((c) => c[0]).join(',')].concat(h.trades.slice().reverse().map((t) => cols.map((c) => cell(typeof c[1] === 'function' ? c[1](t) : t[c[1]])).join(',')));
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob(['﻿' + lines.join('\r\n')], { type: 'text/csv;charset=utf-8' }));
  a.download = SIM.mode === 'bt' ? `當沖模擬戰-回測-${SIM.bt.from}_${SIM.bt.to}.csv` : `當沖模擬戰-${(SIM.data && SIM.data.date) || ''}.csv`;
  document.body.appendChild(a);
  a.click();
  setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
}

// ---------------------------------------------------------------- 操作
document.addEventListener('click', async (e) => {
  if (S.view !== 'sim') return;
  const mode = e.target.closest('[data-sim-mode]');
  if (mode) { SIM.mode = mode.dataset.simMode; simRender(); window.scrollTo(0, 0); return; }
  const vr = e.target.closest('[data-sim-var]');
  if (vr) { SIM.btVar = vr.dataset.simVar; simRender(); return; }
  const tab = e.target.closest('[data-sim-tab]');
  if (tab) {
    SIM.tab = tab.dataset.simTab;
    try { localStorage.setItem('twscan.simTab', SIM.tab); } catch (err) { /* ignore */ }
    simRender();
    return;
  }
  const act = e.target.closest('[data-sim-act]');
  if (act) {
    const a = act.dataset.simAct;
    if (a === 'csv') { simCsv(); return; }
    if (a === 'reset' && !(SIM.sure && Date.now() - SIM.sure < 6000)) { SIM.sure = Date.now(); simRender(); return; }
    SIM.sure = 0;
    let r;
    try { r = await api('/api/sim', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(a === 'reset' ? { action: 'reset', confirm: 'reset' } : { action: a }) }); } catch (err) { r = { error: NO_LINK }; }
    SIM.msg = r && r.error ? r.error : a === 'reset' ? '已重設' : '';
    if (a === 'reset') { SIM.hist = null; SIM.histN = -1; }
    simLoad();
    return;
  }
  const row = e.target.closest('[data-sim-row]');
  if (row && !e.target.closest('a')) {
    const id = row.dataset.simRow;
    if (SIM.open[id]) delete SIM.open[id]; else SIM.open[id] = true;
    simRender();
  }
});
