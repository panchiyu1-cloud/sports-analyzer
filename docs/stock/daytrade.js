'use strict';
/* 當沖模式：進場候選、持倉、提醒通知、盤中名單驗證。共用的小工具（$、api、esc、pct…）定義在 app.js。 */
const DT = { data: null, open: null, form: {}, setOpen: false, msg: '', lastTs: 0, ac: null, rv: null, rvCp: '09:30' };
try { DT.lastTs = +(localStorage.getItem('twscan.alertTs') || 0); } catch (e) { /* 無痕模式 */ }

const wan = (n) => (n == null ? '—' : (Math.round(n / 1000) / 10).toLocaleString('zh-TW') + ' 萬');
const dtPost = (body) => api('/api/daytrade', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
const DT_ICON = { go: '✓', wait: '⏳', avoid: '✕', late: '✕' };
const ALERT_ICON = { entry: '✓', stop: '⚠', target: '◎', close: '⏰' };

// ---------------------------------------------------------------- 載入與畫面
async function dtLoad(force) {
  let d;
  try { d = await api('/api/daytrade'); } catch (e) { return; }
  DT.data = d;
  DT.loaded = Date.now();
  if (S.view === 'top' && S.listMode === 'dt') dtRender(force);
}

function dtSpark(c) {
  const px = c.spark || [];
  if (px.length < 3) return '';
  const W = 300, H = 62, pad = 3;
  const vals = px.map((p) => p[1]).concat([c.open, c.vwap, c.stop].filter((v) => v));
  if (c.target && c.target <= Math.max(...vals) * 1.012) vals.push(c.target);
  const lo = Math.min(...vals), hi = Math.max(...vals), span = hi - lo || 1;
  const t0 = 9 * 60, t1 = 13 * 60 + 30;
  const x = (t) => pad + ((+t.slice(0, 2) * 60 + +t.slice(3, 5) - t0) / (t1 - t0)) * (W - 2 * pad);
  const y = (v) => pad + ((hi - v) / span) * (H - 2 * pad);
  const line = (v, cls) => (v && v >= lo && v <= hi ? `<line x1="0" x2="${W}" y1="${y(v).toFixed(1)}" y2="${y(v).toFixed(1)}" class="${cls}"/>` : '');
  const pts = px.map((p) => `${x(p[0]).toFixed(1)},${y(p[1]).toFixed(1)}`).join(' ');
  const last = px[px.length - 1];
  return `<div class="dtc-chart"><svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="今日走勢">
      ${line(c.target, 'l-target')}${line(c.stop, 'l-stop')}${line(c.open, 'l-open')}${line(c.vwap, 'l-vwap')}
      <polyline points="${pts}" class="l-px ${clsOf(c.chgp)}"/><circle cx="${x(last[0]).toFixed(1)}" cy="${y(last[1]).toFixed(1)}" r="2.6" class="l-dot ${clsOf(c.chgp)}"/></svg>
    <div class="dtc-legend"><span><i class="k-px"></i>今日走勢</span><span><i class="k-vwap"></i>均價 ${fmtP(c.vwap)}</span><span><i class="k-open"></i>開盤 ${fmtP(c.open)}</span><span><i class="k-stop"></i>停損</span></div></div>`;
}

function dtBuyForm(c) {
  const f = DT.form;
  return `<div class="dt-form"><label>買進價 <input type="number" inputmode="decimal" step="0.01" data-f="price" value="${esc(f.price ?? c.entry)}"></label>
    <label>張數 <input type="number" inputmode="numeric" step="1" min="1" data-f="lots" value="${esc(f.lots ?? 1)}"></label>
    <button class="btn primary" data-act="buy-ok" data-code="${c.code}">確認</button><button class="btn" data-act="form-cancel">取消</button></div>`;
}

function dtCard(c) {
  const cl = clsOf(c.chgp);
  const checks = (c.checks || []).map((k) => `<li class="${k.ok === true ? 'ok' : k.ok === false ? 'no' : 'na'}">${k.ok === true ? '✓' : k.ok === false ? '✕' : '·'} ${esc(k.text)}</li>`).join('');
  const opened = DT.open && DT.open.t === 'buy' && DT.open.code === c.code;
  return `<article class="dtc ${c.status}" data-code="${c.code}">
    <div class="card-head">
      <div class="rank">${c.rank}</div>
      <div class="title"><a href="#stock-${c.code}"><span class="nm">${esc(c.name)}</span><span class="cd">${c.code}</span></a>
        <div class="meta">即時名單第 ${c.rank} 名・盤中評分 ${c.score ?? '—'}${c.tags && c.tags.length ? '・' + esc(c.tags.slice(0, 2).join('、')) : ''}</div></div>
      <div class="quote"><div class="price num ${cl}">${estMark(c.est)}${fmtP(c.price)}</div><div class="chg num ${cl}">${arrow(c.chgp)} ${pct(c.chgp)}</div></div>
    </div>
    <div class="dtc-st"><span class="dts ${c.status}">${DT_ICON[c.status] || '·'} ${esc(c.label)}</span><span class="why">${esc(c.reason)}</span></div>
    <div class="dtc-px">
      <div><span>進場價</span><b class="num">${fmtP(c.entry)}</b><small>現在的賣一價</small></div>
      <div><span>停損價</span><b class="num down">${fmtP(c.stop)}</b><small class="num">${pct(c.stop_pct, 1)}</small></div>
      <div><span>目標價</span><b class="num up">${fmtP(c.target)}</b><small class="num">${pct(c.target_pct, 1)}</small></div>
      <div><span>最多可買</span><b class="num">${c.lots} 張</b><small class="num">約 ${wan(c.amount)}</small></div>
    </div>
    <div class="dtc-risk">買滿 ${c.lots} 張：碰到停損約 <b class="down num">${signed(c.loss)} 元</b>，到目標約 <b class="up num">${signed(c.gain)} 元</b>（已扣成本）</div>
    ${c.status === 'avoid' || c.status === 'late' ? '' : dtSpark(c)}
    <ul class="dtc-checks">${checks}</ul>
    <div class="card-foot">${CLOUD ? '' : opened ? dtBuyForm(c) : `<button class="btn ${c.status === 'go' ? 'primary' : ''}" data-act="buy-open" data-code="${c.code}">${c.status === 'go' ? '我買了，幫我盯' : '我還是買了，幫我盯'}</button>`}
      <a href="#stock-${c.code}" data-tf="30m">30 分／60 分／日 K 線 →</a></div>
  </article>`;
}

function dtPositions(d) {
  const ps = d.positions || [];
  if (!ps.length) return '';
  const row = (p) => {
    const st = p.state === 'stop' ? '<span class="st warn">⚠ 已到停損</span>' : p.state === 'target' ? '<span class="st bull">◎ 已到目標</span>' : '';
    const o = DT.open || {}, f = DT.form;
    let form = '';
    if (o.id === p.id && o.t === 'sell') {
      form = `<div class="dt-form pos-form"><label>賣出價 <input type="number" inputmode="decimal" step="0.01" data-f="price" value="${esc(f.price ?? p.price ?? p.entry)}"></label>
        <button class="btn primary" data-act="sell-ok" data-id="${p.id}">確認賣出</button><button class="btn" data-act="form-cancel">取消</button>
        <button class="btn ghost" data-act="pos-cancel" data-id="${p.id}">刪除這筆（記錯了）</button></div>`;
    } else if (o.id === p.id && o.t === 'edit') {
      form = `<div class="dt-form pos-form"><label>停損價 <input type="number" inputmode="decimal" step="0.01" data-f="stop" value="${esc(f.stop ?? p.stop)}"></label>
        <label>目標價 <input type="number" inputmode="decimal" step="0.01" data-f="target" value="${esc(f.target ?? p.target)}"></label>
        <button class="btn primary" data-act="edit-ok" data-id="${p.id}">儲存</button><button class="btn" data-act="form-cancel">取消</button></div>`;
    }
    return `<div class="pos ${p.state || ''}">
      <div class="pos-name"><a href="#stock-${p.code}" data-tf="30m"><span class="nm">${esc(p.name)}</span> <span class="cd">${p.code}</span></a> ${st}
        <small>${esc(p.date === d.date ? p.time : md(p.date))} 買・停損 <b class="num">${fmtP(p.stop)}</b>・目標 <b class="num">${fmtP(p.target)}</b></small></div>
      <div class="pos-num"><span>買進</span><b class="num">${fmtP(p.entry)} × ${p.lots}</b></div>
      <div class="pos-num"><span>現價</span><b class="num ${clsOf(p.price != null ? p.price - p.entry : null)}">${p.price ? estMark(p.est) + fmtP(p.price) : '—'}</b></div>
      <div class="pos-num"><span>損益（元）</span><b class="num ${clsOf(p.pnl)}">${p.pnl != null ? signed(p.pnl) : '—'}</b><small class="num ${clsOf(p.ret)}">${pct(p.ret)}</small></div>
      <div class="pos-act"><button class="btn primary" data-act="sell-open" data-id="${p.id}">賣出</button><button class="btn" data-act="edit-open" data-id="${p.id}">改價位</button></div>
      ${form}</div>`;
  };
  return `<div class="panel dt-pos"><h3>我的持倉 <span class="muted" style="font-weight:400;font-size:12.5px">碰到停損或目標價會提醒；實際賣出後按「賣出」記錄</span></h3>${ps.map(row).join('')}</div>`;
}

function dtNotifState() {
  if (!('Notification' in window) || !window.isSecureContext) return ['no', '這台裝置的瀏覽器不支援系統通知（用區網網址或手機開啟時常見）。頁面開著時仍會跳出提示與聲音；要在手機收到通知，請設定下面的手機推播。'];
  if (Notification.permission === 'granted') return ['ok', '這台電腦已允許通知。'];
  if (Notification.permission === 'denied') return ['no', '瀏覽器已封鎖這個網頁的通知，請到瀏覽器的網站設定改成允許。'];
  return ['ask', '還沒允許這台電腦顯示通知。'];
}

function dtSettings(d) {
  const s = d.settings, f = DT.form, [ns, ntxt] = dtNotifState();
  return `<div class="panel dt-setbox"><h3>當沖設定</h3>
    <div class="dt-form wrap">
      <label>當沖額度 <input type="number" inputmode="decimal" step="1" min="1" data-f="budget" value="${esc(f.budget ?? s.budget / 1e4)}"> 萬元</label>
      <label>每筆停損金額 <input type="number" inputmode="numeric" step="500" min="100" data-f="risk" value="${esc(f.risk ?? s.risk)}"> 元</label>
      <label class="chk"><input type="checkbox" data-f="notify" ${(f.notify ?? s.notify) ? 'checked' : ''}> 開啟提醒</label>
      <button class="btn primary" data-act="set-save">儲存設定</button><button class="btn" data-act="set-toggle">關閉</button>
    </div>
    <div class="muted small">「最多可買」是依額度與停損金額算出的<b>上限</b>，不是建議買滿；剛開始建議先用 1～2 張測試。</div>
    <h3 style="margin-top:14px">這台裝置的通知</h3>
    <div class="dt-form wrap"><span class="st ${ns === 'ok' ? 'bull' : 'info'}">${ns === 'ok' ? '已允許' : ns === 'ask' ? '尚未允許' : '無法使用'}</span><span class="t2" style="font-size:13px">${ntxt}</span>
      <button class="btn" data-act="notif-enable">${ns === 'ask' ? '允許通知並試播提示音' : '試播提示音'}</button></div>
    <h3 style="margin-top:14px">手機推播（選用）</h3>
    <div class="dt-form wrap"><label>推播主題 <input type="text" data-f="ntfy_topic" style="width:230px" placeholder="例如 twscan-a1b2c3d4e5" value="${esc(f.ntfy_topic ?? s.ntfy_topic ?? '')}"></label>
      <button class="btn" data-act="topic-gen">產生一組</button><button class="btn" data-act="push-test">儲存並發送測試</button></div>
    <div class="muted small">人不在電腦前也能收到提醒：① 手機安裝免費的 <b>ntfy</b> App（App Store／Google Play 搜尋 ntfy）；② 按「產生一組」，在 App 裡按 + 訂閱同一個主題名稱；③ 按「儲存並發送測試」，手機收到就完成。
      主題名稱等於密碼，不要告訴別人；推播內容只有股票代號與價位，會經過 ntfy.sh 的伺服器轉送。不想用就留空。</div>
    <div class="dt-msg" id="dtSetMsg">${esc(DT.msg)}</div></div>`;
}

function dtAlertsList(d) {
  const al = d.alerts || [];
  return `<div class="panel"><h3>今日提醒</h3>${al.length ? `<ul class="dt-alerts">${al.map((a) => `<li class="${a.kind}"><span class="num muted">${esc(a.time)}</span><span class="ic">${ALERT_ICON[a.kind] || '·'}</span><div><b>${esc(a.title)}</b><div class="muted">${esc(a.body)}</div></div></li>`).join('')}</ul>`
    : '<div class="muted" style="font-size:13.5px">今天還沒有提醒。候選股符合進場條件、持倉碰到停損或目標價、收盤前還有部位沒出場時會通知你。</div>'}</div>`;
}

function dtTradeRows(list, withDate) {
  return list.map((t) => `<tr style="cursor:default"><td>${withDate ? `<span class="cd">${md(t.date)}</span>` : ''}<span class="nm">${esc(t.name)}</span> <span class="cd hide-sm">${t.code}</span></td>
    <td class="n num">${fmtP(t.buy)} → ${fmtP(t.sell)}<span class="muted show-sm">${t.lots} 張</span></td><td class="n num hide-sm">${t.lots}</td>
    <td class="n num ${clsOf(t.pnl)}"><b>${signed(t.pnl)}</b></td><td class="n num ${clsOf(t.ret)}">${pct(t.ret)}</td>
    <td class="n"><button class="btn ghost" data-act="trade-del" data-id="${t.id}" title="刪除這筆紀錄">✕</button></td></tr>`).join('');
}

function dtTrades(d) {
  const sm = d.trades_summary || {}, f = DT.form, opened = DT.open && DT.open.t === 'log';
  const form = opened ? `<div class="dt-form wrap" style="margin-top:8px"><label>代號 <input type="text" data-f="code" style="width:76px" value="${esc(f.code ?? '')}"></label>
      <label>買價 <input type="number" inputmode="decimal" step="0.01" data-f="buy" value="${esc(f.buy ?? '')}"></label>
      <label>賣價 <input type="number" inputmode="decimal" step="0.01" data-f="sell" value="${esc(f.sell ?? '')}"></label>
      <label>張數 <input type="number" inputmode="numeric" step="1" min="1" data-f="lots" style="width:64px" value="${esc(f.lots ?? 1)}"></label>
      <label>日期 <input type="date" data-f="date" max="${d.date}" value="${esc(f.date ?? d.date)}"></label>
      <button class="btn primary" data-act="log-ok">加入紀錄</button><button class="btn" data-act="form-cancel">取消</button></div><div class="dt-msg">${esc(DT.msg)}</div>` : '';
  return `<div class="panel"><h3>今日交易 ${sm.n ? `<span class="st ${sm.pnl > 0 ? 'bull' : sm.pnl < 0 ? 'bear' : 'info'}">已實現 ${signed(sm.pnl)} 元・${sm.win}/${sm.n} 筆賺</span>` : ''}</h3>
    ${(d.trades || []).length ? `<div class="table-scroll"><table class="tbl rv-tbl"><thead><tr><th>股票</th><th class="n">買 → 賣</th><th class="n hide-sm">張數</th><th class="n">損益（元）</th><th class="n">報酬</th><th></th></tr></thead><tbody>${dtTradeRows(d.trades)}</tbody></table></div>`
    : '<div class="muted" style="font-size:13.5px">今天還沒有交易紀錄。全部的紀錄與統計在「復盤」頁最下方。</div>'}
    ${opened ? form : '<div style="margin-top:8px"><button class="btn" data-act="log-open">補登一筆交易</button></div>'}</div>`;
}

function dtRender(force) {
  const box = $('#topCards'), d = DT.data;
  if (!box || !d) return;
  const ae = document.activeElement;
  if (!force && ae && ae.closest && ae.closest('#dtRoot') && /INPUT/.test(ae.tagName)) return;   // 正在輸入時不要重畫
  const mk = d.market || {}, s = d.settings;
  const idx = (lbl, v) => `${lbl} <b class="num ${clsOf(v)}">${pct(v)}</b>`;
  let body;
  if (d.live) {
    body = `<div class="dt-head"><h3>進場候選</h3><span class="muted">即時名單中、額度內買得起的前 ${s.top_n} 名・${esc(d.now.slice(0, 5))} 更新</span></div>
      ${d.cands.length ? `<div class="dt-cards">${d.cands.map(dtCard).join('')}</div>` : '<div class="panel empty">目前沒有符合額度的候選股，稍後會自動更新。</div>'}
      ${d.excluded ? `<div class="muted small">另有 ${d.excluded} 檔排名更前面，但一張的金額超過額度、或一張的停損就超過設定金額，已略過。</div>` : ''}`;
  } else {
    body = `<div class="panel empty"><div class="big">盤中才會出現進場候選</div>交易時間 09:00–13:30，程式會從即時名單挑出額度內買得起的前 ${s.top_n} 名，${esc(s.entry_from)} 起開始判斷「可進場／等一下／不要追」，並算好進場價、停損價、目標價與張數。<br>符合條件時會發提醒，不需要一直盯著畫面。</div>`;
  }
  box.className = 'dtwrap';
  box.innerHTML = `<div id="dtRoot" class="dt">
    <div class="panel dt-bar"><div>${d.live ? `<span class="st ${mk.light || 'info'}">${esc(mk.text || '')}</span> <span class="t2">${idx('櫃買', mk.tpex)}・${idx('加權', mk.taiex)}</span>` : '<span class="st info">目前不是交易時間</span>'}</div>
      <div class="t2">額度 <b>${wan(s.budget)}</b>・每筆停損 <b class="num">${fmtInt(s.risk)} 元</b>・提醒 <b>${s.notify ? '開' : '關'}</b>${s.notify && s.ntfy_topic ? '（含手機）' : ''}
        ${CLOUD ? '' : '<button class="btn" data-act="set-toggle">設定</button>'}</div></div>
    ${DT.setOpen ? dtSettings(d) : ''}
    ${CLOUD ? '' : dtPositions(d)}
    ${body}
    ${CLOUD ? dtAlertsList(d) : `<div class="grid2">${dtAlertsList(d)}${dtTrades(d)}</div>`}
    <div class="muted small" style="margin-top:12px">判斷規則：① 站上開盤價與當日均價；② 沒有追高（開盤跳空 &lt; ${d.rules.gap}%、離均價 &lt; ${d.rules.ext}%、漲幅 &lt; ${d.rules.chg}%）；③ 預估量 ≥ 均量 ${d.rules.vol} 倍；④ 外盤比 ≥ 50%；⑤ 連續在榜 ≥ ${d.rules.stable} 分鐘。五項都符合才是「可進場」。
      停損價＝開盤價與當日均價較高者再往下一檔（距離限制在 ${d.rules.stop_min}%～${d.rules.stop_max}%），目標價＝停損距離的 ${d.rules.rr} 倍；當日均價是程式用即時成交估算的。
      這套規則還沒有經過驗證，程式每天會在 09:05、09:30、10:00、11:00 自動存下名單並對答案，結果在「復盤」頁的「盤中名單驗證」。處置股、注意股等可能不能現股當沖，下單前請在券商 App 確認。<b>以上不是投資建議。</b></div>
  </div>`;
}

// 從當沖模式點進個股頁時，直接打開 30 分 K
document.addEventListener('click', (e) => {
  const a = e.target.closest && e.target.closest('a[data-tf]');
  if (a) { S.stock.tf = a.dataset.tf; try { localStorage.setItem('twscan.tf', a.dataset.tf); } catch (err) { /* ignore */ } }
}, true);

// ---------------------------------------------------------------- 操作
function dtVal(root, name) { const el = root.querySelector(`[data-f="${name}"]`); return el ? (el.type === 'checkbox' ? el.checked : el.value.trim()) : undefined; }
function dtOpen(o) { DT.open = o; DT.form = {}; DT.msg = ''; dtRender(true); }

document.addEventListener('input', (e) => {
  const el = e.target;
  if (el.dataset && el.dataset.f && el.closest('#dtRoot, #rvMine')) DT.form[el.dataset.f] = el.type === 'checkbox' ? el.checked : el.value;
});

document.addEventListener('click', async (e) => {
  const b = e.target.closest('[data-act]');
  if (!b || !b.closest('#dtRoot, #rvMine')) return;
  const act = b.dataset.act, root = b.closest('.dt-form') || b.closest('.panel') || document;
  const done = async (r, keepOpen) => {
    if (r && r.error) { DT.msg = r.error; alertBar(r.error); return false; }
    if (!keepOpen) { DT.open = null; DT.form = {}; }
    DT.msg = '';
    await dtLoad(true);
    if (S.view === 'review') dtReviewLoad();
    return true;
  };
  if (act === 'set-toggle') { DT.setOpen = !DT.setOpen; DT.form = {}; DT.msg = ''; dtRender(true); }
  else if (act === 'buy-open') dtOpen({ t: 'buy', code: b.dataset.code });
  else if (act === 'sell-open') dtOpen({ t: 'sell', id: b.dataset.id });
  else if (act === 'edit-open') dtOpen({ t: 'edit', id: b.dataset.id });
  else if (act === 'log-open') dtOpen({ t: 'log' });
  else if (act === 'form-cancel') dtOpen(null);
  else if (act === 'buy-ok') await done(await dtPost({ action: 'buy', code: b.dataset.code, price: dtVal(root, 'price'), lots: dtVal(root, 'lots') }));
  else if (act === 'sell-ok') await done(await dtPost({ action: 'sell', id: b.dataset.id, price: dtVal(root, 'price') }));
  else if (act === 'edit-ok') await done(await dtPost({ action: 'edit', id: b.dataset.id, stop: dtVal(root, 'stop'), target: dtVal(root, 'target') }));
  else if (act === 'pos-cancel') await done(await dtPost({ action: 'cancel', id: b.dataset.id }));
  else if (act === 'trade-del') { if (b.dataset.sure) await done(await dtPost({ action: 'del_trade', id: b.dataset.id })); else { b.dataset.sure = '1'; b.textContent = '確定刪除？'; } }
  else if (act === 'log-ok') await done(await dtPost({ action: 'log', code: dtVal(root, 'code'), buy: dtVal(root, 'buy'), sell: dtVal(root, 'sell'), lots: dtVal(root, 'lots'), date: dtVal(root, 'date') }));
  else if (act === 'set-save') {
    const box = b.closest('.dt-setbox');
    const r = await dtPost({ action: 'settings', budget: parseFloat(dtVal(box, 'budget')) * 1e4, risk: dtVal(box, 'risk'), notify: dtVal(box, 'notify'), ntfy_topic: dtVal(box, 'ntfy_topic') });
    if (await done(r, true)) { DT.form = {}; DT.msg = '已儲存'; dtRender(true); }
  } else if (act === 'topic-gen') {
    const a = new Uint8Array(8); crypto.getRandomValues(a);
    DT.form.ntfy_topic = 'twscan-' + Array.from(a, (x) => x.toString(36).padStart(2, '0')).join('').slice(0, 14);
    dtRender(true);
  } else if (act === 'push-test') {
    const box = b.closest('.dt-setbox'), topic = dtVal(box, 'ntfy_topic');
    let r = await dtPost({ action: 'settings', ntfy_topic: topic });
    if (!r.error) r = await dtPost({ action: 'test_push', ntfy_topic: topic });
    DT.msg = r.error || '已送出測試推播，手機應該在幾秒內收到。';
    await dtLoad(true);
  } else if (act === 'notif-enable') {
    dtAudio();
    if ('Notification' in window && window.isSecureContext && Notification.permission === 'default') await Notification.requestPermission();
    dtBeep('entry');
    dtToast({ kind: 'entry', title: '提醒測試', body: '有新提醒時會像這樣跳出來，並播放提示音。', time: '' });
    dtRender(true);
  }
});

function alertBar(text) {
  dtToast({ kind: 'stop', title: '無法完成', body: text, time: '' }, 6000);
}

// ---------------------------------------------------------------- 通知
function dtAudio() {
  try { DT.ac = DT.ac || new (window.AudioContext || window.webkitAudioContext)(); if (DT.ac.state === 'suspended') DT.ac.resume(); } catch (e) { /* 不支援就算了 */ }
  return DT.ac;
}
document.addEventListener('pointerdown', dtAudio, { passive: true });

function dtBeep(kind) {
  const ac = DT.ac;
  if (!ac || ac.state !== 'running') return;
  const notes = kind === 'stop' || kind === 'close' ? [440, 330, 440, 330] : kind === 'target' ? [660, 880, 1100] : [660, 880];
  notes.forEach((f, i) => {
    const o = ac.createOscillator(), g = ac.createGain(), t = ac.currentTime + i * 0.2;
    o.type = 'sine'; o.frequency.value = f;
    g.gain.setValueAtTime(0.0001, t); g.gain.exponentialRampToValueAtTime(0.25, t + 0.02); g.gain.exponentialRampToValueAtTime(0.0001, t + 0.18);
    o.connect(g).connect(ac.destination); o.start(t); o.stop(t + 0.2);
  });
}

function dtToast(a, ms) {
  let box = $('#toasts');
  if (!box) { box = document.createElement('div'); box.id = 'toasts'; box.setAttribute('aria-live', 'polite'); document.body.appendChild(box); }
  const el = document.createElement('div');
  el.className = 'toast ' + (a.kind || '');
  el.innerHTML = `<div class="ic">${ALERT_ICON[a.kind] || '·'}</div><div><b>${esc(a.title)}</b><div>${esc(a.body)}</div></div><span class="x" aria-label="關閉">✕</span>`;
  el.onclick = (ev) => { el.remove(); if (!ev.target.classList.contains('x') && a.id) { S.listMode = 'dt'; if (location.hash.slice(1) && location.hash !== '#top') location.hash = 'top'; else route(); } };
  box.appendChild(el);
  while (box.children.length > 2) box.firstChild.remove();   // 最多同時顯示兩則，其餘到「今日提醒」看
  setTimeout(() => el.remove(), ms || (a.kind === 'stop' || a.kind === 'close' ? 60000 : 25000));
}

function dtIngest(list) {
  const fresh = (list || []).filter((a) => a.ts > DT.lastTs).sort((x, y) => x.ts - y.ts);
  if (!fresh.length) return;
  DT.lastTs = fresh[fresh.length - 1].ts;
  try { localStorage.setItem('twscan.alertTs', String(DT.lastTs)); } catch (e) { /* ignore */ }
  const nowS = Date.now() / 1000;
  fresh.filter((a) => nowS - a.ts < 300).forEach((a) => {
    dtToast(a);
    dtBeep(a.kind);
    try {
      if ('Notification' in window && window.isSecureContext && Notification.permission === 'granted') {
        const n = new Notification(a.title, { body: a.body, tag: a.id });
        n.onclick = () => { window.focus(); S.listMode = 'dt'; location.hash = 'top'; n.close(); };
      }
    } catch (e) { /* 有些瀏覽器不允許直接建立通知 */ }
    if (document.hidden) document.title = '🔔 ' + a.title;
  });
  if (S.view === 'top' && S.listMode === 'dt') dtLoad();
}
document.addEventListener('visibilitychange', () => { if (!document.hidden) document.title = '臺股強勢掃描'; });

// ---------------------------------------------------------------- 復盤頁：盤中名單驗證＋我的交易
async function dtReviewLoad() {
  try { DT.rv = await api('/api/daytrade/review'); } catch (e) { return; }
  if (S.view === 'review') dtReviewRender();
}

function dtReviewRender() {
  const d = DT.rv, a = $('#rvIntraBody'), m = $('#rvMineBody');
  if (!d || !a || !m) return;
  const cp = d.checkpoints.includes(DT.rvCp) ? DT.rvCp : d.checkpoints[0];
  const g = d.cps[cp] || {};
  const names = [['all', '全部前十名'], ['top3', '前 3 名'], ['top1', '第 1 名'], ['go', '當時判定「可進場」']];
  const row = ([k, nm]) => {
    const x = g[k] || { n: 0 };
    if (!x.n) return `<tr style="cursor:default"><td>${nm}</td><td class="n muted" colspan="6">尚無資料</td></tr>`;
    return `<tr style="cursor:default"><td>${nm}<span class="muted show-sm">${x.n} 筆</span></td><td class="n num hide-sm">${fmtInt(x.n)}</td>
      <td class="n num"><b>${dPct(x.avg)}</b><span class="muted show-sm">賺 ${x.win}%</span></td><td class="n num hide-sm">${x.win}%</td>
      <td class="n num"><b>${dPct(x.rule_avg)}</b><span class="muted show-sm">賺 ${x.rule_win}%</span></td><td class="n num hide-sm">${x.rule_win}%</td>
      <td class="n num hide-sm">${x.target}%／${x.stop}%</td></tr>`;
  };
  const warn = d.days < 20 ? `<div class="rv-why"><p>${d.days ? `目前只累積 <b>${d.days} 天</b>，樣本還很少，至少 20 天再下結論。` : '還沒有資料。'}程式會在每個交易日 ${d.checkpoints.join('、')} 自動存下當時的即時前十名與價格，收盤後對答案；只要那個時間程式有開著就會累積${d.pending ? `（今天的名單收盤後會加進來）` : ''}。</p></div>` : '';
  a.innerHTML = `${warn}
    <div class="filters rv-filter" style="margin:10px 0 6px"><span class="muted small-inline">名單時間</span><div class="seg" id="rvCp">${d.checkpoints.map((c) => `<button data-cp="${c}" class="${c === cp ? 'on' : ''}">${c}</button>`).join('')}</div>
      <span class="muted small-inline">${d.days ? `${md(d.since)} 起・${d.days} 天` : ''}</span></div>
    <div class="table-scroll"><table class="tbl rv-tbl"><thead><tr><th>${cp} 買進</th><th class="n hide-sm">筆數</th><th class="n">抱到收盤</th><th class="n hide-sm">賺錢比例</th><th class="n">照停損停利</th><th class="n hide-sm">賺錢比例</th><th class="n hide-sm">先到目標／先停損</th></tr></thead>
      <tbody>${names.map(row).join('')}</tbody></table></div>
    <div class="muted small">「抱到收盤」＝在那個時間用當時價格買進、當天收盤賣出；「照停損停利」＝依當沖模式算出的停損價與目標價，先碰到哪個就出場，都沒碰到就收盤賣。報酬是每筆平均，已扣當沖成本（手續費 ${(d.settings.fee_discount * 10).toFixed(1)} 折＋證交稅 0.15%）。
      比較「第 1 名」「可進場」和「全部前十名」，就知道排名與進場判斷有沒有用；比較四個時間，就知道幾點的名單最可靠。09:05 還在觀察期（09:10 起才判斷），所以不會有「可進場」。</div>
    ${d.recent.length ? `<h3 style="margin-top:14px">最近幾天（前十名抱到收盤的平均）</h3><div class="table-scroll"><table class="tbl rv-tbl"><thead><tr><th>日期</th>${d.checkpoints.map((c) => `<th class="n">${c}</th>`).join('')}</tr></thead>
      <tbody>${d.recent.map((r) => `<tr style="cursor:default"><td>${md(r.date)}（${wd(r.date)}）</td>${d.checkpoints.map((c) => `<td class="n num">${r.cps[c] ? dPct(r.cps[c].avg) : '<span class="muted">—</span>'}</td>`).join('')}</tr>`).join('')}</tbody></table></div>` : ''}`;
  $$('#rvCp button').forEach((b) => (b.onclick = () => { DT.rvCp = b.dataset.cp; dtReviewRender(); }));

  const sm = d.trades_summary || {};
  $('#rvMine summary').textContent = sm.n ? `我的交易紀錄（${sm.n} 筆・${signed(sm.pnl)} 元）` : '我的交易紀錄';
  m.innerHTML = sm.n ? `
    <div class="rv-metrics" style="margin:10px 0">
      <div class="rv-m"><div class="l">總損益</div><div class="v num ${clsOf(sm.pnl)}">${signed(sm.pnl)}</div><div class="s">元・已扣手續費與證交稅</div></div>
      <div class="rv-m"><div class="l">賺錢筆數</div><div class="v num">${sm.win}/${sm.n}</div><div class="s">${Math.round(sm.win / sm.n * 100)}%</div></div>
      <div class="rv-m"><div class="l">平均每筆報酬</div><div class="v num ${clsOf(sm.avg_ret)}">${pct(sm.avg_ret)}</div><div class="s">扣成本後</div></div>
      <div class="rv-m"><div class="l">平均每筆損益</div><div class="v num ${clsOf(sm.pnl)}">${signed(sm.pnl / sm.n)}</div><div class="s">元</div></div>
    </div>
    <div class="table-scroll"><table class="tbl rv-tbl"><thead><tr><th>股票</th><th class="n">買 → 賣</th><th class="n hide-sm">張數</th><th class="n">損益（元）</th><th class="n">報酬</th><th></th></tr></thead><tbody>${dtTradeRows(d.trades, true)}</tbody></table></div>
    <div class="muted small">手續費以 ${(d.settings.fee_discount * 10).toFixed(1)} 折、每筆最低 ${d.settings.min_fee} 元估算，當沖證交稅 0.15%，實際金額以券商對帳單為準。要新增紀錄，到首頁「當沖模式」按「我買了」或「補登一筆交易」。</div>`
    : '<div class="muted small" style="margin-top:10px">還沒有交易紀錄。到首頁「當沖模式」按「我買了」記錄持倉，賣出後按「賣出」；或用「補登一筆交易」直接輸入買賣價，這裡就會累計你的實際損益。</div>';
}
