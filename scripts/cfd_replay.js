#!/usr/bin/env node
/* 差價交易分析網頁（docs/cfd/index.html）的離線回測。

   讀網頁「設定 → 回測 → 匯出 K 線」存下來的檔案，用網頁自己的分析引擎與回測模組重播一次，不需要瀏覽器，也不需要連線。
   同一份 K 線、同樣的取樣間隔時，結果和在網頁上按「開始回測」逐欄相同，所以可以：
   - 在同一份資料上比較兩個版本的網頁（--page 指到不同的 index.html）；
   - 改了分析方式之後馬上知道結果，不必請使用者用手機重跑。

   用法：
     node scripts/cfd_replay.js <K 線檔> [--page docs/cfd/index.html] [--out rows.csv] [--summary summary.json]
                                          [--step N] [--only 代號1,代號2] [--quiet]
   --step  每隔幾根取樣一次（預設用匯出時的設定）
   --only  只跑這些商品

   也可以當模組用：const { loadPage, readKlines, replay } = require('./cfd_replay.js')

   K 線檔的格式（文字）：第一行「#m 」後面是這次匯出的說明（JSON）；每個商品的每個週期先有一行
   「#i,代號,類別,小數位,來源,週期,根數」，之後每根 K 線一行「時間(秒),開,高,低,收,量,主動買進量」。
   時間和上一根剛好差一根時留空；來源沒有主動買進量時最後一欄留空。 */
'use strict';
const fs = require('fs'), vm = require('vm'), path = require('path');

const TF_MS = { '1m': 60e3, '5m': 300e3, '15m': 900e3, '30m': 1800e3, '1h': 3600e3, '4h': 14400e3, '1d': 86400e3 };

/* 把網頁裡不需要瀏覽器的模組（分析引擎、追蹤單、交易時段、回測）載進來。它們排在「行情資料」模組之前 */
function loadPage(file) {
  const html = fs.readFileSync(file, 'utf8'), a = html.indexOf('<script>'), b = html.lastIndexOf('</script>');
  if (a < 0 || b < 0) throw new Error('找不到網頁的程式：' + file);
  const js = html.slice(a + 8, b), cut = js.search(/\/\* =+ 行情資料/);
  if (cut < 0) throw new Error('找不到模組分界（行情資料）：' + file);
  const box = { console, Intl, Date, Math, JSON, Map, Set, Array, Object, Number, String, isFinite, isNaN, parseFloat, parseInt, Promise, Symbol, Error };
  box.globalThis = box; vm.createContext(box); vm.runInContext(js.slice(0, cut), box, { filename: file });
  if (!box.Engine || !box.BT) throw new Error('網頁裡沒有分析引擎或回測模組：' + file);
  return { E: box.Engine, Track: box.Track, Mkt: box.Mkt, BT: box.BT };
}

/* 讀 K 線檔。回傳 { meta, list: [{ id, cls, dp, tf, src, cd, h1, h1src }] }；cd、h1 的每一根是 { t(毫秒), o, h, l, c, v, b? } */
function readKlines(file) {
  const lines = fs.readFileSync(file, 'utf8').split('\n'), byId = new Map(); let meta = null, cur = null, step = 0, pt = 0;
  for (const ln of lines) {
    if (!ln) continue;
    if (ln.startsWith('#m ')) { meta = JSON.parse(ln.slice(3)); continue; }
    if (ln.startsWith('#i,')) {
      const [, id, cls, dp, src, tf, n] = ln.split(',');
      if (!TF_MS[tf]) throw new Error('不認得的週期：' + ln);
      cur = { id, cls, dp: +dp, src, tf, n: +n, k: [] }; step = TF_MS[tf] / 1000; pt = 0;
      if (!byId.has(id)) byId.set(id, []);
      byId.get(id).push(cur); continue;
    }
    if (ln[0] === '#' || !cur) continue;
    const f = ln.split(','), t = f[0] === '' ? pt + step : +f[0], bar = { t: t * 1000, o: +f[1], h: +f[2], l: +f[3], c: +f[4], v: f[5] === '' ? NaN : +f[5] }; // 來源沒給成交量時網頁裡是 NaN，這裡照樣
    if (f[6] !== undefined && f[6] !== '') bar.b = +f[6];
    cur.k.push(bar); pt = t;
  }
  if (!meta) throw new Error('這不是匯出的 K 線檔（沒有 #m 那一行）');
  const list = [];
  for (const [id, ss] of byId) {
    for (const s of ss) if (s.k.length !== s.n) throw new Error(`${id} ${s.tf}：標頭寫 ${s.n} 根，實際 ${s.k.length} 根（檔案可能不完整）`);
    const main = ss.find(s => s.tf === meta.tf), h1 = ss.find(s => s.tf === '1h' && s !== main);
    if (!main) continue;
    list.push({ id, cls: main.cls, dp: main.dp, tf: main.tf, src: main.src, cd: main.k, h1: h1 ? h1.k : null, h1src: h1 ? h1.src : null });
  }
  return { meta, list };
}

/* 重播：和網頁的 runBt 做一樣的事。opt：step（取樣間隔）、only（代號的集合）、onInst（進度） */
function replay(page, data, opt) {
  opt = opt || {}; const { BT } = page, meta = data.meta, step = opt.step || meta.step, rows = [], t0 = Date.now(); let inst = 0;
  for (const s of data.list) {
    if (opt.only && !opt.only.has(s.id)) continue;
    const cost = meta.cost && meta.cost[s.cls] != null ? meta.cost[s.cls] : undefined;
    const c = BT.prep({ id: s.id, cls: s.cls, dp: s.dp, tf: s.tf, cd: s.cd, h1: s.h1 || undefined, cost, step, src: s.src });
    for (const i of c.idx) { const r = BT.sample(c, i); if (r) rows.push(r); }
    inst++; if (opt.onInst) opt.onInst(s, inst);
  }
  const sum = BT.summarize(rows, { tf: meta.tf, size: meta.size, step, at: meta.at, secs: Math.round((Date.now() - t0) / 1000), stopped: false, errs: [], demo: !!meta.demo, ver: 2 });
  return { rows, sum };
}

function main() {
  const argv = process.argv.slice(2), opt = {}, pos = [];
  for (let i = 0; i < argv.length; i++) { const a = argv[i]; if (a === '--quiet') opt.quiet = true; else if (a.startsWith('--')) opt[a.slice(2)] = argv[++i]; else pos.push(a); }
  if (!pos.length) { console.error('用法：node scripts/cfd_replay.js <K 線檔> [--page docs/cfd/index.html] [--out rows.csv] [--summary summary.json] [--step N] [--only 代號1,代號2]'); process.exit(2); }
  const pageFile = opt.page || path.join(__dirname, '..', 'docs', 'cfd', 'index.html'), page = loadPage(pageFile), data = readKlines(pos[0]), m = data.meta;
  if (!opt.quiet) console.error(`K 線檔：${m.tf}，${data.list.length} 個商品，共 ${m.rows} 根，${new Date(m.at).toISOString()} 匯出${m.demo ? '（示範資料）' : ''}；網頁：${pageFile}`);
  const { rows, sum } = replay(page, data, { step: opt.step ? +opt.step : 0, only: opt.only ? new Set(opt.only.split(',')) : null,
    onInst: (s, k) => { if (!opt.quiet) process.stderr.write(`\r${k}／${data.list.length} ${s.id}        `); } });
  if (!opt.quiet) process.stderr.write('\n');
  if (opt.out) fs.writeFileSync(opt.out, page.BT.csv(rows));
  if (opt.summary) fs.writeFileSync(opt.summary, JSON.stringify(sum));
  const pc = v => (v == null ? '—' : (v * 100).toFixed(1) + '%'), r = v => (v == null ? '—' : (v >= 0 ? '+' : '') + v.toFixed(3) + 'R');
  console.log(`判斷 ${sum.meta.n} 筆；有偏向時方向命中 ${pc(sum.all.hit)}（${sum.all.nb}）；全部計畫 ${r(sum.all.pr)}（${sum.all.pn}）；現在可進場 ${sum.go.n} 筆 ${r(sum.go.pr)}`);
  if (sum.sep) console.log(`區別力（看多後上漲 − 看空後上漲）：全部 ${pc(sum.sep.all.sep)}、門檻通過 ${pc(sum.sep.pass.sep)}、門檻未通過 ${pc(sum.sep.fail.sep)}`);
}

module.exports = { loadPage, readKlines, replay, TF_MS };
if (require.main === module) main();
