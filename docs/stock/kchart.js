/* 輕量 K 線圖（Canvas，無外部套件）：K 棒、均線、布林、成交量、MACD/KD/RSI、訊號標記、十字線提示 */
(function () {
  const cssv = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
  const palette = () => ({
    up: cssv('--up'), down: cssv('--down'), grid: cssv('--grid'), text: cssv('--text'), text2: cssv('--text-2'),
    muted: cssv('--muted'), ma5: cssv('--ma5'), ma20: cssv('--ma20'), ma60: cssv('--ma60'), accent: cssv('--accent'),
    warn: cssv('--warn'), surface: cssv('--surface'),
  });

  function fmtP(p) {
    if (p == null || !isFinite(p)) return '—';
    const a = Math.abs(p);
    const d = a >= 1000 ? 0 : a >= 100 ? 1 : 2;
    return p.toLocaleString('zh-TW', { maximumFractionDigits: d });
  }
  function fmtVolLots(shares) {
    const lots = shares / 1000;
    if (lots >= 1e4) return (lots / 1e4).toLocaleString('zh-TW', { maximumFractionDigits: 1 }) + ' 萬張';
    return Math.round(lots).toLocaleString('zh-TW') + ' 張';
  }
  function niceStep(range, n) {
    const raw = range / Math.max(n, 1);
    const p = Math.pow(10, Math.floor(Math.log10(raw)));
    const f = raw / p;
    return (f < 1.5 ? 1 : f < 3 ? 2 : f < 7 ? 5 : 10) * p;
  }

  class KChart {
    constructor(wrap, opts) {
      this.wrap = wrap;
      this.opts = Object.assign({ height: 300, sub: null, bb: false, compact: false, range: null, levels: [], intraday: false }, opts || {});
      this.canvas = document.createElement('canvas');
      this.canvas.setAttribute('role', 'img');
      wrap.appendChild(this.canvas);
      this.ctx = this.canvas.getContext('2d');
      this.hover = null;
      this.tip = document.getElementById('tooltip');
      const move = (e) => {
        const r = this.canvas.getBoundingClientRect();
        this.hover = { x: e.clientX - r.left, y: e.clientY - r.top, cx: e.clientX, cy: e.clientY };
        this.draw();
      };
      this.canvas.addEventListener('pointermove', move);
      this.canvas.addEventListener('pointerdown', move);
      this.canvas.addEventListener('pointerleave', (e) => {
        const hide = () => { this.hover = null; this.tip.hidden = true; this.draw(); };
        if (e.pointerType === 'touch') { clearTimeout(this._t); this._t = setTimeout(hide, 2500); } else hide();
      });
      this.ro = new ResizeObserver(() => this.draw());
      this.ro.observe(wrap);
    }
    destroy() { this.ro.disconnect(); this.tip.hidden = true; this.canvas.remove(); }
    setData(d) { this.data = d; this.draw(); }
    setOpts(o) { Object.assign(this.opts, o); this.draw(); }

    draw() {
      const d = this.data;
      if (!d || !d.bars || !d.bars.length) return;
      const o = this.opts, P = palette(), ctx = this.ctx;
      const W = Math.max(200, this.wrap.clientWidth), H = o.height, dpr = window.devicePixelRatio || 1;
      if (this.canvas.width !== Math.round(W * dpr) || this.canvas.height !== Math.round(H * dpr)) {
        this.canvas.width = Math.round(W * dpr); this.canvas.height = Math.round(H * dpr);
      }
      this.canvas.style.height = H + 'px';
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, W, H);
      ctx.font = '11px ' + cssv('--font');

      const bars = d.bars, N = bars.length, ind = d.ind || {};
      const s0 = o.range ? Math.max(0, N - o.range) : 0;
      const nView = N - s0;
      const padL = 4, padR = o.compact ? 46 : 58, padT = 20, padB = 18, gap = 8;
      const plotW = W - padL - padR, inner = H - padT - padB;
      let hPrice, hVol, hSub = 0;
      if (o.sub && !o.compact) { hPrice = inner * 0.58; hVol = inner * 0.14; hSub = inner * 0.28 - gap * 2; }
      else { hPrice = inner * 0.78; hVol = inner * 0.22 - gap; }
      const yP0 = padT, yV0 = padT + hPrice + gap, yS0 = yV0 + hVol + gap;
      const step = plotW / nView;
      const bw = Math.max(1, Math.min(13, step * 0.68));
      const xOf = (i) => padL + (i - s0 + 0.5) * step;

      // 價格區間
      let lo = Infinity, hi = -Infinity;
      for (let i = s0; i < N; i++) { lo = Math.min(lo, bars[i][3]); hi = Math.max(hi, bars[i][2]); }
      const overl = ['ma5', 'ma20', 'ma60'].concat(o.bb ? ['bb_up', 'bb_lo'] : []);
      for (const k of overl) { const a = ind[k]; if (!a) continue; for (let i = s0; i < N; i++) if (a[i] != null) { lo = Math.min(lo, a[i]); hi = Math.max(hi, a[i]); } }
      const span0 = hi - lo || hi * 0.02 || 1;
      const levels = (o.levels || []).filter((L) => L.price && L.price > lo - span0 * 0.25 && L.price < hi + span0 * 0.25);
      for (const L of levels) { lo = Math.min(lo, L.price); hi = Math.max(hi, L.price); }
      const pad = (hi - lo) * 0.06 || hi * 0.01;
      lo -= pad; hi += pad;
      const yOf = (p) => yP0 + (hi - p) / (hi - lo) * hPrice;

      // 格線與價格刻度
      ctx.strokeStyle = P.grid; ctx.lineWidth = 1; ctx.fillStyle = P.muted; ctx.textAlign = 'left'; ctx.textBaseline = 'middle';
      const st = niceStep(hi - lo, o.compact ? 3 : 5);
      for (let p = Math.ceil(lo / st) * st; p <= hi; p += st) {
        const y = Math.round(yOf(p)) + 0.5;
        ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(padL + plotW, y); ctx.stroke();
        ctx.fillText(fmtP(p), padL + plotW + 6, y);
      }
      // 日期刻度（日 K：換月時標示；分時 K：換日時畫分隔線並標日期）
      ctx.textAlign = 'center'; ctx.textBaseline = 'alphabetic';
      let lastX = -999;
      const keyLen = o.intraday ? 10 : 7, minGap = o.intraday ? 46 : 54;
      for (let i = s0 + 1; i < N; i++) {
        if (bars[i][0].slice(0, keyLen) !== bars[i - 1][0].slice(0, keyLen)) {
          const x = xOf(i) - (o.intraday ? step / 2 : 0);
          const fits = x - lastX > minGap && x > padL + 16 && x < padL + plotW - 16;
          if (o.intraday || fits) {
            ctx.strokeStyle = P.grid; ctx.beginPath(); ctx.moveTo(Math.round(x) + 0.5, padT); ctx.lineTo(Math.round(x) + 0.5, H - padB); ctx.stroke();
          }
          if (fits) {
            const m = parseInt(bars[i][0].slice(5, 7), 10);
            const label = o.intraday ? m + '/' + parseInt(bars[i][0].slice(8, 10), 10) : (m === 1 ? bars[i][0].slice(0, 4) : m + '月');
            ctx.fillText(label, o.intraday ? x + Math.min(22, step * 2) : x, H - 4);
            lastX = x;
          }
        }
      }

      // 布林通道
      if (o.bb && ind.bb_up) {
        ctx.setLineDash([3, 3]); ctx.strokeStyle = P.muted; ctx.lineWidth = 1;
        for (const k of ['bb_up', 'bb_lo']) this._line(ind[k], s0, N, xOf, yOf);
        ctx.setLineDash([]);
      }
      // K 棒
      for (let i = s0; i < N; i++) {
        const [, op, h, l, c] = bars[i];
        const up = c >= op, col = up ? P.up : P.down, x = xOf(i);
        ctx.strokeStyle = col; ctx.fillStyle = col; ctx.lineWidth = 1;
        ctx.beginPath(); ctx.moveTo(Math.round(x) + 0.5, yOf(h)); ctx.lineTo(Math.round(x) + 0.5, yOf(l)); ctx.stroke();
        const y1 = yOf(Math.max(op, c)), y2 = yOf(Math.min(op, c));
        ctx.fillRect(x - bw / 2, y1, bw, Math.max(1, y2 - y1));
      }
      // 均線
      ctx.lineWidth = o.compact ? 1.4 : 1.6;
      for (const [k, col] of [['ma60', P.ma60], ['ma20', P.ma20], ['ma5', P.ma5]]) {
        if (!ind[k]) continue; ctx.strokeStyle = col; this._line(ind[k], s0, N, xOf, yOf);
      }
      // 關鍵價位
      const labs = [];
      for (const L of levels) {
        const y = Math.round(yOf(L.price)) + 0.5;
        ctx.setLineDash([5, 4]); ctx.strokeStyle = L.color || P.text2; ctx.lineWidth = 1;
        ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(padL + plotW, y); ctx.stroke(); ctx.setLineDash([]);
        labs.push({ L, ty: L.price >= bars[N - 1][4] ? y - 9 : y + 9 });
      }
      // 價位很接近時把標籤往下錯開，避免疊在一起
      labs.sort((a, b) => a.ty - b.ty);
      for (let k = 1; k < labs.length; k++) if (labs[k].ty - labs[k - 1].ty < 15) labs[k].ty = labs[k - 1].ty + 15;
      const over = labs.length ? labs[labs.length - 1].ty - (yP0 + hPrice - 8) : 0;
      for (const { L, ty: ty0 } of labs) {
        const ty = over > 0 ? Math.max(yP0 + 8, ty0 - over) : ty0;
        const tw = ctx.measureText(L.label).width;
        ctx.globalAlpha = 0.85; ctx.fillStyle = P.surface; ctx.fillRect(padL + plotW - tw - 10, ty - 7, tw + 8, 14); ctx.globalAlpha = 1;
        ctx.fillStyle = L.color || P.text2; ctx.textAlign = 'right'; ctx.textBaseline = 'middle';
        ctx.fillText(L.label, padL + plotW - 6, ty);
      }
      // 訊號標記
      for (const m of d.marks || []) {
        if (m.i < s0 || m.i >= N) continue;
        const x = xOf(m.i), b = bars[m.i];
        const bull = m.kind !== 'bear';
        ctx.fillStyle = bull ? P.up : P.down;
        ctx.beginPath();
        if (bull) { const y = yOf(b[3]) + 6; ctx.moveTo(x, y); ctx.lineTo(x - 4.5, y + 7); ctx.lineTo(x + 4.5, y + 7); }
        else { const y = yOf(b[2]) - 6; ctx.moveTo(x, y); ctx.lineTo(x - 4.5, y - 7); ctx.lineTo(x + 4.5, y - 7); }
        ctx.closePath(); ctx.fill();
      }
      // 成交量
      let vmax = 0;
      for (let i = s0; i < N; i++) vmax = Math.max(vmax, bars[i][5]);
      ctx.globalAlpha = 0.6;
      for (let i = s0; i < N; i++) {
        const b = bars[i], h = vmax ? b[5] / vmax * (hVol - 2) : 0;
        ctx.fillStyle = b[4] >= b[1] ? P.up : P.down;
        ctx.fillRect(xOf(i) - bw / 2, yV0 + hVol - h, bw, h);
      }
      ctx.globalAlpha = 1;
      ctx.fillStyle = P.muted; ctx.textAlign = 'left'; ctx.textBaseline = 'top';
      if (vmax) ctx.fillText(fmtVolLots(vmax).replace(' ', ''), padL + plotW + 6, yV0);
      if (!o.compact && hVol > 34) ctx.fillText('成交量', padL + plotW + 6, yV0 + 15);

      // 最新價標籤（右側刻度）
      {
        const last = bars[N - 1], prevC = N > 1 ? bars[N - 2][4] : last[1];
        const y = Math.round(yOf(last[4]));
        ctx.fillStyle = last[4] >= prevC ? P.up : P.down;
        ctx.fillRect(padL + plotW + 1, y - 8, padR - 2, 16);
        ctx.fillStyle = '#fff'; ctx.textBaseline = 'middle'; ctx.fillText(fmtP(last[4]), padL + plotW + 4, y);
      }

      // 副圖
      let subY = null;
      if (o.sub && !o.compact) subY = this._sub(o.sub, ind, s0, N, xOf, yS0, hSub, padL, plotW, bw, P);

      // 圖例（滑鼠位置或最後一天）
      let hi_ = N - 1;
      if (this.hover && this.hover.x >= padL && this.hover.x <= padL + plotW) {
        hi_ = Math.min(N - 1, Math.max(s0, s0 + Math.floor((this.hover.x - padL) / step)));
      }
      this._legend(ind, hi_, padL, P);

      // 十字線與提示框
      if (this.hover && this.hover.x >= padL && this.hover.x <= padL + plotW) {
        const x = Math.round(xOf(hi_)) + 0.5;
        ctx.strokeStyle = P.text2; ctx.globalAlpha = 0.5; ctx.lineWidth = 1; ctx.setLineDash([3, 3]);
        ctx.beginPath(); ctx.moveTo(x, padT); ctx.lineTo(x, H - padB); ctx.stroke();
        if (this.hover.y >= yP0 && this.hover.y <= yP0 + hPrice) {
          const y = Math.round(this.hover.y) + 0.5;
          ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(padL + plotW, y); ctx.stroke();
          ctx.setLineDash([]); ctx.globalAlpha = 1;
          const p = hi - (this.hover.y - yP0) / hPrice * (hi - lo);
          ctx.fillStyle = P.text2; ctx.fillRect(padL + plotW + 1, y - 8, padR - 2, 16);
          ctx.fillStyle = '#111'; ctx.textAlign = 'left'; ctx.textBaseline = 'middle'; ctx.fillText(fmtP(p), padL + plotW + 4, y);
        }
        ctx.setLineDash([]); ctx.globalAlpha = 1;
        this._tooltip(hi_);
      }
    }

    _line(arr, s0, N, xOf, yOf) {
      const ctx = this.ctx; let started = false;
      ctx.beginPath();
      for (let i = s0; i < N; i++) {
        const v = arr[i];
        if (v == null) { started = false; continue; }
        if (!started) { ctx.moveTo(xOf(i), yOf(v)); started = true; } else ctx.lineTo(xOf(i), yOf(v));
      }
      ctx.stroke();
    }

    _sub(kind, ind, s0, N, xOf, y0, h, padL, plotW, bw, P) {
      const ctx = this.ctx;
      let series = [], lo = Infinity, hi = -Infinity, guides = [], label = '';
      if (kind === 'macd') {
        label = 'MACD';
        series = [['dif', P.ma5, 'DIF'], ['dea', P.ma20, 'DEA']];
        for (const k of ['dif', 'dea', 'osc']) for (let i = s0; i < N; i++) if (ind[k] && ind[k][i] != null) { lo = Math.min(lo, ind[k][i]); hi = Math.max(hi, ind[k][i]); }
        const m = Math.max(Math.abs(lo), Math.abs(hi)) || 1; lo = -m; hi = m; guides = [0];
      } else if (kind === 'kd') {
        label = 'KD(9)'; series = [['k', P.ma5, 'K'], ['kd_d', P.ma20, 'D']]; lo = 0; hi = 100; guides = [20, 80];
      } else if (kind === 'rsi') {
        label = 'RSI(14)'; series = [['rsi', P.ma5, 'RSI']]; lo = 0; hi = 100; guides = [30, 50, 70];
      }
      const yOf = (v) => y0 + (hi - v) / (hi - lo) * h;
      ctx.strokeStyle = P.grid; ctx.lineWidth = 1; ctx.fillStyle = P.muted; ctx.textAlign = 'left'; ctx.textBaseline = 'middle';
      ctx.setLineDash([3, 3]);
      for (const g of guides) { const y = Math.round(yOf(g)) + 0.5; ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(padL + plotW, y); ctx.stroke(); ctx.fillText(String(g), padL + plotW + 6, y); }
      ctx.setLineDash([]);
      if (kind === 'macd' && ind.osc) {
        for (let i = s0; i < N; i++) {
          const v = ind.osc[i]; if (v == null) continue;
          ctx.fillStyle = v >= 0 ? P.up : P.down; ctx.globalAlpha = 0.7;
          const y = yOf(Math.max(v, 0)), yb = yOf(Math.min(v, 0));
          ctx.fillRect(xOf(i) - bw / 2, y, bw, Math.max(1, yb - y));
        }
        ctx.globalAlpha = 1;
      }
      ctx.lineWidth = 1.5;
      for (const [k, col] of series) { if (!ind[k]) continue; ctx.strokeStyle = col; this._line(ind[k], s0, N, xOf, yOf); }
      ctx.fillStyle = P.muted; ctx.textBaseline = 'top'; ctx.fillText(label, padL + 2, y0);
      this._subSeries = series;
      return y0;
    }

    _legend(ind, i, padL, P) {
      const ctx = this.ctx; let x = padL + 2; const y = 10;
      ctx.textBaseline = 'middle'; ctx.textAlign = 'left';
      for (const [k, col, nm] of [['ma5', P.ma5, 'MA5'], ['ma20', P.ma20, 'MA20'], ['ma60', P.ma60, 'MA60']]) {
        if (!ind[k]) continue;
        const v = ind[k][i];
        ctx.fillStyle = col; ctx.fillRect(x, y - 1, 10, 2.5); x += 14;
        ctx.fillStyle = P.text2; const t = `${nm} ${fmtP(v)}`; ctx.fillText(t, x, y); x += ctx.measureText(t).width + 12;
      }
    }

    _tooltip(i) {
      const b = this.data.bars[i], prev = i > 0 ? this.data.bars[i - 1][4] : null, tip = this.tip;
      const chg = prev ? (b[4] / prev - 1) * 100 : null;
      const cls = chg == null ? '' : chg > 0 ? 'up' : chg < 0 ? 'down' : 'flat';
      const marks = (this.data.marks || []).filter((m) => m.i === i).map((m) => `<div>${m.kind === 'bear' ? '▼' : '▲'} ${m.label}</div>`).join('');
      tip.innerHTML = `<div class="tt-d">${b[0]}</div><div class="tt-g">
        <span>開</span><span class="num">${fmtP(b[1])}</span><span>高</span><span class="num">${fmtP(b[2])}</span>
        <span>低</span><span class="num">${fmtP(b[3])}</span><span>收</span><span class="num ${cls}">${fmtP(b[4])}</span>
        <span>漲跌</span><span class="num ${cls}">${chg == null ? '—' : (chg > 0 ? '+' : '') + chg.toFixed(2) + '%'}</span>
        <span>成交量</span><span class="num">${fmtVolLots(b[5])}</span></div>${marks ? `<div class="tt-m">${marks}</div>` : ''}`;
      tip.hidden = false;
      const r = tip.getBoundingClientRect();
      let x = this.hover.cx + 14, y = this.hover.cy + 14;
      if (x + r.width > window.innerWidth - 8) x = this.hover.cx - r.width - 14;
      if (y + r.height > window.innerHeight - 8) y = this.hover.cy - r.height - 14;
      tip.style.left = Math.max(4, x) + 'px'; tip.style.top = Math.max(4, y) + 'px';
    }
  }

  function sparkline(canvas, values, color) {
    if (!values || values.length < 2) return;
    const dpr = window.devicePixelRatio || 1, W = canvas.clientWidth || 96, H = canvas.clientHeight || 44;
    canvas.width = W * dpr; canvas.height = H * dpr;
    const ctx = canvas.getContext('2d'); ctx.setTransform(dpr, 0, 0, dpr, 0, 0); ctx.clearRect(0, 0, W, H);
    const lo = Math.min(...values), hi = Math.max(...values), n = values.length;
    const x = (i) => 2 + i / (n - 1) * (W - 4), y = (v) => 3 + (hi - v) / ((hi - lo) || 1) * (H - 6);
    ctx.strokeStyle = color; ctx.lineWidth = 1.6; ctx.beginPath();
    values.forEach((v, i) => (i ? ctx.lineTo(x(i), y(v)) : ctx.moveTo(x(i), y(v))));
    ctx.stroke();
    ctx.fillStyle = color; ctx.beginPath(); ctx.arc(x(n - 1), y(values[n - 1]), 2.5, 0, Math.PI * 2); ctx.fill();
  }

  window.KChart = KChart;
  window.sparkline = sparkline;
  window.fmtP = fmtP;
  window.fmtVolLots = fmtVolLots;
})();
