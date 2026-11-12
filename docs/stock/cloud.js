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

  const notHere = (msg) => ({ error: msg });

  async function api(path, opts) {
    const method = ((opts && opts.method) || 'GET').toUpperCase();
    if (method !== 'GET') return notHere('雲端版只能看，不能在網頁上修改設定或重新掃描。');
    const u = new URL(path, location.href), p = u.pathname;
    const code = (u.searchParams.get('code') || '').trim().toUpperCase();
    switch (p) {
      case '/api/status': {
        const [st, m] = await Promise.all([file('status.json'), getMeta()]);
        if (!st) throw new Error('雲端還沒有資料');
        st.cloud = m ? { published: m.published, ts: m.ts, every: m.every, final: m.final } : null;
        return st;
      }
      case '/api/top': return (await file('top.json')) || { pending: true, mode: 'close' };
      case '/api/market': return (await file('market.json')) || { rows: [], pending: true };
      case '/api/review': return (await file('review.json')) || { pending: true };
      case '/api/daytrade': return (await file('daytrade.json')) || { live: false, cands: [], positions: [], alerts: [], trades: [], settings: {}, rules: {}, market: {}, snaps: {} };
      case '/api/daytrade/review': return file('daytrade_review.json');
      case '/api/diag': return (await file('diag.json')) || { sources: {} };
      case '/api/stock': {
        const m = await getMeta();
        if (m && m.stocks && !m.stocks.includes(code)) {
          return notHere('雲端版沒有這檔股票的分析：只準備有足夠歷史資料與成交量的股票。');
        }
        return (await file(`stock/${code}.json`)) || notHere('這檔股票的資料還沒產生，請稍後再試。');
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

  return { api, file, meta: getMeta };
})();
