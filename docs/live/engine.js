/* 即時精選的運算核心（不碰網路、不碰畫面，所以可以在 Node 裡測試：tests/js/engine_test.js）。
 *
 * 一句話：運彩賠率 × 國際公允機率 − 1 = EV。EV 夠高、價格夠新，才叫「可下」。
 * 這裡的公式和 Python 那邊一樣（models/market.py 的去水、models/margin.py 的常態分佈換線），
 * 改任何一邊都要改另一邊。
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.Engine = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  // ------------------------------------------------------------------ 小工具
  function clamp(x, lo, hi) { return Math.max(lo, Math.min(hi, x)); }
  function isNum(x) { return typeof x === 'number' && isFinite(x); }
  function toMs(t) {
    if (!t) return null;
    var ms = typeof t === 'number' ? t : Date.parse(t);
    return isFinite(ms) ? ms : null;
  }
  function ageMin(t, now) {
    var ms = toMs(t);
    return ms === null ? null : (now - ms) / 60000;
  }
  function median(xs) {
    var s = xs.slice().sort(function (a, b) { return a - b; });
    var n = s.length;
    return n ? (n % 2 ? s[(n - 1) / 2] : (s[n / 2 - 1] + s[n / 2]) / 2) : NaN;
  }

  // ------------------------------------------------------------------ 賠率與去水
  // 美式賠率（ESPN 給的字串："-270"、"+221"、"EVEN"）換成歐式小數賠率。
  function americanToDecimal(v) {
    if (v === null || v === undefined) return null;
    var s = String(v).trim().toUpperCase();
    if (s === 'EVEN' || s === 'EV') return 2;
    var n = parseFloat(s);
    if (!isFinite(n) || n === 0) return null;
    return n > 0 ? 1 + n / 100 : 1 + 100 / -n;
  }

  // 去水（power 法）：每個隱含機率取 k 次方，讓總和剛好是 1。水錢多半壓在冷門上，
  // 這個方法比等比例縮小更接近莊家實際的定價方式。
  function devig(prices) {
    for (var i = 0; i < prices.length; i++) if (!(prices[i] > 1)) return null;
    var raw = prices.map(function (p) { return 1 / p; });
    function sum(k) { return raw.reduce(function (a, r) { return a + Math.pow(r, k); }, 0); }
    var lo = 0.5, hi = 5;
    while (sum(hi) > 1 && hi < 1e6) hi *= 2;
    for (var j = 0; j < 200 && hi - lo > 1e-10; j++) {
      var mid = (lo + hi) / 2;
      if (sum(mid) > 1) lo = mid; else hi = mid;
    }
    var k = (lo + hi) / 2;
    var probs = raw.map(function (r) { return Math.pow(r, k); });
    var total = probs.reduce(function (a, b) { return a + b; }, 0);
    return probs.map(function (p) { return p / total; });
  }

  // 多家莊家的共識：每家各自去水，逐個選項取中位數，再讓總和回到 1。
  function consensus(quotes) {
    var per = null, used = 0;
    quotes.forEach(function (prices) {
      var fair = devig(prices);
      if (!fair) return;
      if (!per) per = fair.map(function () { return []; });
      if (fair.length !== per.length) return;
      fair.forEach(function (p, i) { per[i].push(p); });
      used++;
    });
    if (!used) return null;
    var meds = per.map(median);
    var total = meds.reduce(function (a, b) { return a + b; }, 0);
    return { probs: meds.map(function (m) { return m / total; }), books: used };
  }

  // ------------------------------------------------------------------ 常態分佈換線（NBA）
  function cdf(z) {
    // Abramowitz-Stegun 7.1.26，誤差 < 1.5e-7，換線用綽綽有餘。
    var t = 1 / (1 + 0.3275911 * Math.abs(z) / Math.SQRT2);
    var y = 1 - (((((1.061405429 * t - 1.453152027) * t) + 1.421413741) * t - 0.284496736) * t + 0.254829592) * t * Math.exp(-z * z / 2);
    return z >= 0 ? (1 + y) / 2 : (1 - y) / 2;
  }
  function atLeast(k, mu, sigma) { return 1 - cdf((k - 0.5 - mu) / sigma); }
  // 總分（或勝分差）超過 line 的機率、剛好等於（退款）的機率、低於的機率。
  function overProbs(mu, sigma, line) {
    var over, push = 0;
    if (Math.abs(line - Math.round(line)) < 1e-9) {
      var k = Math.round(line);
      push = cdf((k + 0.5 - mu) / sigma) - cdf((k - 0.5 - mu) / sigma);
      over = atLeast(k + 1, mu, sigma);
    } else {
      over = atLeast(Math.ceil(line), mu, sigma);
    }
    return [over, push, Math.max(0, 1 - over - push)];
  }
  function conditional(win, push) { return 1 - push > 0 ? win / (1 - push) : 0; }
  function meanFromOver(line, pOver, sigma) {
    pOver = clamp(pOver, 1e-6, 1 - 1e-6);
    var lo = line - 8 * sigma, hi = line + 8 * sigma;
    for (var i = 0; i < 100; i++) {
      var mid = (lo + hi) / 2;
      var r = overProbs(mid, sigma, line);
      if (conditional(r[0], r[1]) < pOver) lo = mid; else hi = mid;
    }
    return (lo + hi) / 2;
  }
  // 把國際盤的主線換算到運彩的線。回傳 {a: 機率, b: 機率}（a = 大 / 主隊過盤），不含退款。
  function shiftLine(kind, mainLine, pA, newLine, sigma) {
    var sign = kind === 'spread' ? -1 : 1; // 主隊讓分 -7.5 = 勝分差超過 7.5
    var mu = meanFromOver(sign * mainLine, pA, sigma);
    var r = overProbs(mu, sigma, sign * newLine);
    return { a: conditional(r[0], r[1]), b: conditional(r[2], r[1]) };
  }

  // ------------------------------------------------------------------ 隊名比對
  var DROP = { fc: 1, cf: 1, afc: 1, sc: 1, ac: 1, as: 1, ss: 1, ssc: 1, us: 1, rc: 1, rcd: 1, cd: 1, ud: 1, sd: 1, ca: 1, club: 1, de: 1, the: 1, bc: 1, sv: 1, vfb: 1, vfl: 1, tsg: 1, fsv: 1, bsc: 1, calcio: 1, football: 1, futbol: 1, '1': 1 };
  function norm(name) {
    var s = String(name || '').toLowerCase();
    if (s.normalize) s = s.normalize('NFD').replace(/[̀-ͯ]/g, '');
    s = s.replace(/ø/g, 'o').replace(/ß/g, 'ss').replace(/æ/g, 'ae').replace(/[^a-z0-9]+/g, ' ').trim();
    var kept = s.split(' ').filter(function (t) { return t && !DROP[t]; });
    return kept.length ? kept.join(' ') : s;
  }
  function bigrams(s) {
    var out = {}, t = s.replace(/ /g, '');
    for (var i = 0; i < t.length - 1; i++) { var g = t.substr(i, 2); out[g] = (out[g] || 0) + 1; }
    return out;
  }
  // 0–1：兩個名字有多像（去掉 FC 之類的字、重音符號之後，比字的重疊與字母對的重疊）。
  function sim(a, b) {
    a = norm(a); b = norm(b);
    if (!a || !b) return 0;
    if (a === b) return 1;
    var short = a.length < b.length ? a : b, long = a.length < b.length ? b : a;
    var best = 0;
    if (short.length >= 4 && long.indexOf(short) === 0) best = 0.8;
    var ta = a.split(' '), tb = b.split(' '), common = 0;
    ta.forEach(function (t) { if (tb.indexOf(t) >= 0) common++; });
    best = Math.max(best, common / (ta.length + tb.length - common));
    var ga = bigrams(a), gb = bigrams(b), inter = 0, na = 0, nb = 0, g;
    for (g in ga) { na += ga[g]; if (gb[g]) inter += Math.min(ga[g], gb[g]); }
    for (g in gb) nb += gb[g];
    if (na + nb) best = Math.max(best, 2 * inter / (na + nb));
    return best;
  }
  function teamSim(name, match, side) {
    return Math.max(sim(name, match[side]), sim(name, match[side + '_key']));
  }
  // 把另一個來源的比賽（ESPN、The Odds API）對回我們的比賽：同聯盟、開賽時間差 3 小時內、
  // 主客隊名字合起來最像的那一場；分數不夠（< 0.9，滿分 2）就當作沒有。
  function matchEvents(matches, events, hours) {
    var out = {}, window = (hours || 3) * 3600000;
    matches.forEach(function (m) {
      var k0 = toMs(m.kickoff), best = null, bestScore = 0;
      events.forEach(function (e) {
        if (m.sport === 'nba' && e.id && String(e.id) === String(m.id)) { best = e; bestScore = 9; return; }
        var k1 = toMs(e.kickoff);
        if (k0 === null || k1 === null || Math.abs(k0 - k1) > window) return;
        var score = teamSim(e.home, m, 'home') + teamSim(e.away, m, 'away');
        if (score > bestScore) { best = e; bestScore = score; }
      });
      if (best && bestScore >= 0.9) out[m.key] = best;
    });
    return out;
  }

  // ------------------------------------------------------------------ ESPN（即時參考盤）
  function num(v) { var n = parseFloat(String(v === undefined || v === null ? '' : v).replace(/^[ou]/i, '')); return isFinite(n) ? n : null; }
  function pick(o, path) {
    for (var i = 0; i < path.length && o !== null && o !== undefined; i++) o = o[path[i]];
    return o === undefined ? null : o;
  }
  // ESPN 計分板 -> [{id, kickoff, home, away, state, completed, score, odds, open}]
  // odds / open = {provider, h2h:{home,draw,away}, spread:{line,home,away}, totals:{line,over,under}}（小數賠率）
  function parseEspn(payload) {
    var events = (payload && Array.isArray(payload.events)) ? payload.events : [];
    var out = [];
    events.forEach(function (ev) {
      var comp = pick(ev, ['competitions', 0]);
      if (!comp || !Array.isArray(comp.competitors)) return;
      var home = null, away = null;
      comp.competitors.forEach(function (c) { if (c.homeAway === 'home') home = c; else if (c.homeAway === 'away') away = c; });
      if (!home || !away) return;
      var st = pick(comp, ['status', 'type']) || pick(ev, ['status', 'type']) || {};
      var e = {
        id: String(ev.id || ''), kickoff: ev.date || comp.date || null,
        home: pick(home, ['team', 'displayName']) || '', away: pick(away, ['team', 'displayName']) || '',
        state: st.state || 'pre', completed: !!st.completed, detail: st.shortDetail || st.description || '',
        score: { home: num(home.score), away: num(away.score) }, odds: null, open: null
      };
      var o = pick(comp, ['odds', 0]);
      if (o && typeof o === 'object') {
        e.odds = espnOdds(o, 'close');
        e.open = espnOdds(o, 'open');
        if (e.odds) e.odds.provider = pick(o, ['provider', 'name']) || '';
      }
      out.push(e);
    });
    return out;
  }
  function espnOdds(o, which) {
    function dec(path) { return americanToDecimal(pick(o, path.concat([which, 'odds']))); }
    var out = { h2h: null, spread: null, totals: null };
    var h = dec(['moneyline', 'home']), a = dec(['moneyline', 'away']), d = dec(['moneyline', 'draw']);
    if (which === 'close') {
      if (!h) h = americanToDecimal(pick(o, ['homeTeamOdds', 'moneyLine']));
      if (!a) a = americanToDecimal(pick(o, ['awayTeamOdds', 'moneyLine']));
      if (!d) d = americanToDecimal(pick(o, ['drawOdds', 'moneyLine']));
    }
    if (h && a) { out.h2h = { home: h, away: a }; if (d) out.h2h.draw = d; }
    var sh = dec(['pointSpread', 'home']), sa = dec(['pointSpread', 'away']);
    var line = num(pick(o, ['pointSpread', 'home', which, 'line']));
    if (sh && sa && line !== null) out.spread = { line: line, home: sh, away: sa };
    var ov = dec(['total', 'over']), un = dec(['total', 'under']);
    var tl = num(pick(o, ['total', 'over', which, 'line']));
    if (tl === null && which === 'close') tl = num(o.overUnder);
    if (ov && un && tl !== null) out.totals = { line: tl, over: ov, under: un };
    return (out.h2h || out.spread || out.totals) ? out : null;
  }
  var SIDES = { h2h: ['home', 'draw', 'away'], totals: ['over', 'under'], spread: ['home', 'away'] };
  // 一家莊家的賠率 -> 去水後的機率（和共識同一個形狀，方便比較）。
  function fairFromPrices(odds, at) {
    if (!odds) return null;
    var out = {};
    ['h2h', 'totals', 'spread'].forEach(function (mk) {
      var b = odds[mk];
      out[mk] = null;
      if (!b) return;
      var sides = SIDES[mk].filter(function (s) { return b[s] > 1; });
      if (sides.length < 2) return;
      var fair = devig(sides.map(function (s) { return b[s]; }));
      if (!fair) return;
      var blk = { line: isNum(b.line) ? b.line : null, at: at || null };
      sides.forEach(function (s, i) { blk[s] = fair[i]; });
      out[mk] = blk;
    });
    return out;
  }

  // ------------------------------------------------------------------ The Odds API（共識價）
  function mainLine(points, prefer) {
    var count = {}, best = null;
    points.forEach(function (p) { count[p] = (count[p] || 0) + 1; });
    Object.keys(count).forEach(function (k) {
      var p = parseFloat(k);
      if (best === null || count[k] > count[best] ||
          (count[k] === count[best] && prefer !== null && prefer !== undefined && Math.abs(p - prefer) < Math.abs(best - prefer))) best = p;
    });
    return best;
  }
  // The Odds API 的回應 -> [{id, kickoff, home, away, fair:{h2h,totals,spread}}]，和 pipeline 的共識同一個算法。
  function parseOddsApi(payload, at, totalsPrefer) {
    var out = [];
    (Array.isArray(payload) ? payload : []).forEach(function (ev) {
      if (!ev || !Array.isArray(ev.bookmakers)) return;
      var h2h = [], three = false, totals = [], spreads = [];
      ev.bookmakers.forEach(function (bk) {
        (bk.markets || []).forEach(function (mk) {
          var oc = mk.outcomes || [], by = {};
          oc.forEach(function (x) { by[x.name] = x; });
          if (mk.key === 'h2h' && by[ev.home_team] && by[ev.away_team]) {
            var q = { home: by[ev.home_team].price, away: by[ev.away_team].price };
            if (by.Draw) { q.draw = by.Draw.price; three = true; }
            h2h.push(q);
          } else if (mk.key === 'totals' && by.Over && by.Under && isNum(by.Over.point)) {
            totals.push({ line: by.Over.point, a: by.Over.price, b: by.Under.price });
          } else if (mk.key === 'spreads' && by[ev.home_team] && by[ev.away_team] && isNum(by[ev.home_team].point)) {
            spreads.push({ line: by[ev.home_team].point, a: by[ev.home_team].price, b: by[ev.away_team].price });
          }
        });
      });
      var fair = { books: ev.bookmakers.length, h2h: null, totals: null, spread: null };
      var sides = three ? ['home', 'draw', 'away'] : ['home', 'away'];
      var c = consensus(h2h.filter(function (q) { return sides.every(function (s) { return q[s] > 1; }); })
        .map(function (q) { return sides.map(function (s) { return q[s]; }); }));
      if (c) { fair.h2h = { line: null, at: at }; sides.forEach(function (s, i) { fair.h2h[s] = c.probs[i]; }); }
      [['totals', totals, ['over', 'under'], totalsPrefer], ['spread', spreads, ['home', 'away'], null]].forEach(function (d) {
        var line = mainLine(d[1].map(function (x) { return x.line; }), d[3]);
        if (line === null) return;
        var cc = consensus(d[1].filter(function (x) { return x.line === line; }).map(function (x) { return [x.a, x.b]; }));
        if (!cc) return;
        fair[d[0]] = { line: line, at: at };
        fair[d[0]][d[2][0]] = cc.probs[0];
        fair[d[0]][d[2][1]] = cc.probs[1];
      });
      out.push({ id: ev.id, kickoff: ev.commence_time, home: ev.home_team, away: ev.away_team, fair: fair });
    });
    return out;
  }

  // ------------------------------------------------------------------ 運彩（瀏覽器直接讀到時）
  function twKind(name) {
    if (/角球|半場|上半|下半|局|節|單雙|勝分差/.test(name)) return 'other';
    if (name === '不讓分' || name === '獨贏' || name === '勝負') return 'h2h';
    if (name.indexOf('讓分') === 0) return 'spread';
    if (/^(\[總分\])?大小( [\d.]+)?$/.test(name)) return 'totals';
    return 'other';
  }
  // 運彩的原始檔 -> [{id, no, home_zh, away_zh, kickoff, league_zh, sport_id, markets:[{name, kind, line, legs, prices}]}]
  // 賠率 = 1 + pu/pd；讓分的 hv/mv 是「主隊」的讓分，每隊自己的讓分寫在選項名稱最後。
  function parseTw(payload) {
    var out = [];
    (Array.isArray(payload) ? payload : []).forEach(function (raw) {
      if (!raw || !raw.hn || !raw.an || !raw.kt) return;
      var g = { id: String(raw.id || ''), no: String(raw.no || ''), home_zh: String(raw.hn).trim(), away_zh: String(raw.an).trim(),
        kickoff: raw.kt, league_zh: String(raw.tn || raw.ln || raw.lg || raw.cn || '').trim(), sport_id: String(raw.si || ''), markets: [] };
      (Array.isArray(raw.ms) ? raw.ms : []).forEach(function (m) {
        var name = String(m.name || '').trim(), kind = twKind(name);
        if (kind === 'other') return;
        var prices = {}, line = num(m.mv), homeLine = null, threeWay = name.indexOf(':') >= 0;
        (Array.isArray(m.cs) ? m.cs : []).forEach(function (c) {
          var pd = parseFloat(c.pd), pu = parseFloat(c.pu), nm = String(c.name || '').trim(), v = String(c.v || '').toUpperCase();
          if (!(pd > 0) || !(pu >= 0)) return;
          var price = Math.round((1 + pu / pd) * 1000) / 1000, side = null;
          if (kind === 'totals') side = nm.charAt(0) === '大' ? 'over' : (nm.charAt(0) === '小' ? 'under' : null);
          else if (v === 'H') side = 'home';
          else if (v === 'A') side = 'away';
          else if (v === 'D' || v === 'X' || nm.indexOf('和') >= 0) side = 'draw';
          if (!side) return;
          if (kind === 'spread') {
            if (side === 'draw') threeWay = true;
            var own = /\s([+-]\d+(?:\.\d+)?)$/.exec(nm);
            if (own && side === 'home') homeLine = parseFloat(own[1]);
            if (own && side === 'away' && homeLine === null) homeLine = -parseFloat(own[1]);
          }
          if (line === null && num(c.hv) !== null) line = num(c.hv);
          prices[side] = price;
        });
        if (kind === 'spread' && threeWay) return; // 足球的三向讓分沒有國際價可比
        if (kind === 'spread' && homeLine !== null) line = homeLine;
        var legs = parseInt(m.m, 10);
        if (Object.keys(prices).length >= 2) {
          g.markets.push({ name: name, kind: kind, line: kind === 'h2h' ? null : line, legs: legs >= 1 && legs <= 8 ? legs : null, prices: prices });
        }
      });
      out.push(g);
    });
    return out;
  }
  function zhKey(zh, table) {
    zh = String(zh || '').trim();
    var cands = [zh, zh.replace(/[（(].*?[)）]/g, '').trim(), zh.replace(/^[A-Za-z0-9 .\-]+/, '').trim()];
    for (var i = 0; i < cands.length; i++) if (cands[i] && table[cands[i]]) return table[cands[i]];
    return null;
  }
  // 運彩的比賽對回我們的比賽：隊名對照表（data.json 的 zh）兩隊都對得上、開賽時間差 6 小時內。
  function matchTw(matches, games, table) {
    var out = {};
    games.forEach(function (g) {
      var hk = zhKey(g.home_zh, table), ak = zhKey(g.away_zh, table), k1 = toMs(g.kickoff);
      if (!hk || !ak || k1 === null) return;
      matches.forEach(function (m) {
        var k0 = toMs(m.kickoff);
        if (k0 === null || Math.abs(k0 - k1) > 6 * 3600000) return;
        if (m.home_key === hk && m.away_key === ak) out[m.key] = g;
      });
    });
    return out;
  }
  // 即時讀到的運彩比賽 -> 和 data.json 的 tw 同一個形狀（讓分、大小可以有很多條線）。
  function twFromGame(g, at) {
    var tw = { at: at, src: 'live', id: g.id, no: g.no, legs: {}, h2h: null, totals: [], spread: [], derived: [], names: {} };
    g.markets.forEach(function (m) {
      if (m.legs) tw.legs[m.name] = m.legs;
      tw.names[m.name] = m.prices;
      var b = { line: m.line, legs: m.legs, name: m.name };
      Object.keys(m.prices).forEach(function (s) { b[s] = m.prices[s]; });
      if (m.kind === 'h2h') tw.h2h = b; else tw[m.kind].push(b);
    });
    return tw;
  }

  // ------------------------------------------------------------------ 評估一場比賽
  var DEFAULTS = {
    bankroll: 20000,      // 本金（元）
    kelly: 0.25,          // 用幾分之一的 Kelly（1/4：保守）
    maxPct: 0.02,         // 單注上限：本金的 2%
    unit: 100,            // 運彩最小下注單位
    priceFreshMin: 30,    // 運彩價在這麼多分鐘內才算「現在的價格」
    fairFreshMin: 30,     // 共識價在這麼多分鐘內就直接相信，不再和即時參考盤取保守值
    nearEv: -0.03         // EV 高於這個值算「差一點」
  };

  function newer(a, b) {
    if (!a) return b || null;
    if (!b) return a;
    return (toMs(b.at) || 0) > (toMs(a.at) || 0) ? b : a;
  }
  // 這個市場在某一條線上的公允機率。同一條線直接用；NBA 的線不同就用常態分佈換算。
  function probsAt(sport, market, block, line, cfg) {
    if (!block) return null;
    if (market === 'h2h') return { probs: block, derived: false };
    if (!isNum(line) || !isNum(block.line)) return null;
    if (Math.abs(block.line - line) < 1e-9) return { probs: block, derived: false };
    var s = SIDES[market], a = s[0], b = s[1];
    if (sport !== 'nba' || !isNum(block[a])) return null;
    if (Math.abs(block.line - line) > (market === 'spread' ? 8 : 12)) return null;
    var sigma = market === 'spread' ? cfg.nba_margin_sigma : cfg.nba_total_sigma;
    var r = shiftLine(market, block.line, block[a], line, sigma);
    var probs = {};
    probs[a] = r.a; probs[b] = r.b;
    return { probs: probs, derived: true };
  }
  function needPrice(p, thr) { return p > 0 ? Math.ceil((1 + thr) / p * 100 - 1e-9) / 100 : null; }
  function fmtLine(x) { return (x > 0 ? '+' : '') + String(x); }

  function twBlocks(tw, market) {
    if (!tw || !tw[market]) return [];
    return Array.isArray(tw[market]) ? tw[market] : [tw[market]];
  }

  /* 評估一場比賽的每一種下法。
   * ctx = { cfg, fair: 瀏覽器自己買的共識價（可無）, ref: 即時參考盤去水後的機率（可無）,
   *         refFirst: 這場第一次看到參考盤時的機率, tw: 瀏覽器直接讀到的運彩價（可無）, manual: {下法 id: 手動輸入的賠率} }
   */
  function evaluate(match, ctx, settings, now) {
    var cfg = ctx.cfg, S = Object.assign({}, DEFAULTS, settings || {});
    var sport = match.sport, thrDirect = (cfg.threshold && cfg.threshold[sport]) || 0.03;
    var names = { home: match.home_zh || match.home, away: match.away_zh || match.away };
    var tw = newer(match.tw, ctx.tw), manual = ctx.manual || {};
    var twAge = tw ? ageMin(tw.at, now) : null;
    var bets = [], fairAt = null, notes = [];

    function fairOf(market) {
      var f = newer(match.fair && match.fair[market], ctx.fair && ctx.fair[market]);
      // 還沒有共識價（例如早上還沒買）：先用參考盤那一家的去水價頂著，但最多只到「先確認」。
      if (!f && ctx.ref && ctx.ref[market]) f = Object.assign({ single: true }, ctx.ref[market]);
      return f;
    }
    // src = {src: 'live' | 'upload', age: 分鐘}：這個運彩價是哪裡來的、多久以前的。
    function add(market, side, line, label, price, legs, cons, ref, derived, id, src) {
      var m = manual[id], usePrice = null, priceSrc = null, priceAge = null;
      if (m && m.price > 1) { usePrice = m.price; priceSrc = 'manual'; priceAge = ageMin(m.at, now); }
      else if (price > 1) { usePrice = price; priceSrc = src.src; priceAge = src.age; }
      var pCons = cons.p, fAge = ageMin(cons.at, now);
      // 共識價夠新就直接用；舊了就和即時參考盤取比較低的那個（保守：寧可少算，不要多算）。
      var p = pCons;
      if (isNum(ref) && !(fAge !== null && fAge <= S.fairFreshMin)) p = Math.min(pCons, ref);
      var thr = derived ? Math.max(cfg.derived_threshold || 0.04, thrDirect) : thrDirect;
      var bet = {
        id: id, market: market, side: side, line: line, label: label, legs: legs || null, derived: !!derived,
        price: usePrice, priceSrc: priceSrc, priceAge: priceAge, pCons: pCons, pRef: isNum(ref) ? ref : null, p: p,
        fairAge: fAge, thr: thr, need: needPrice(p, thr), ev: usePrice ? p * usePrice - 1 : null, status: 'noprice',
        priceFresh: priceAge !== null && priceAge <= S.priceFreshMin,
        fairOk: fAge !== null && fAge <= (cfg.gap_minutes || 180), single: !!cons.single
      };
      if (usePrice) {
        if (bet.ev >= thr) bet.status = bet.priceFresh && bet.fairOk && !bet.single ? 'value' : 'check';
        else if (bet.ev >= 0) bet.status = 'even';
        else bet.status = bet.ev >= S.nearEv ? 'near' : 'pass';
      }
      bets.push(bet);
    }
    var twSrc = { src: tw && tw.src === 'live' ? 'live' : 'upload', age: twAge };

    ['h2h', 'totals', 'spread'].forEach(function (market) {
      // 運彩的足球讓分是三向的（讓分 1:0，含和局），國際的兩向亞洲盤沒有東西可以比。
      if (sport === 'football' && market === 'spread') return;
      var fair = fairOf(market);
      if (!fair) return;
      if (market === 'h2h') fairAt = fair.at;
      var ref = fair.single ? null : (ctx.ref && ctx.ref[market]);
      var blocks = twBlocks(tw, market);
      // 運彩沒有價格時，仍然列出國際主線的每個選項，讓使用者知道「看到多少才可以下」。
      if (!blocks.length) blocks = [{ line: fair.line, none: true }];
      blocks.forEach(function (b) {
        var line = market === 'h2h' ? null : (b.line === undefined ? fair.line : b.line);
        var fp = probsAt(sport, market, fair, line, cfg);
        if (!fp) return;
        var rp = ref ? probsAt(sport, market, ref, market === 'h2h' ? null : line, cfg) : null;
        var legs = b.legs || (tw && tw.legs && (tw.legs[b.name] || (!Array.isArray(tw[market]) && tw.legs[market]))) || null;
        SIDES[market].forEach(function (side) {
          if (!isNum(fp.probs[side])) return;
          var label;
          if (market === 'h2h') label = side === 'draw' ? '和局' : names[side] + ' 不讓分';
          else if (market === 'totals') label = (side === 'over' ? '大 ' : '小 ') + line;
          else label = names[side] + ' ' + fmtLine(side === 'home' ? line : -line);
          var id = market + ':' + side + (line === null || line === undefined ? '' : ':' + line);
          add(market, side, line, label, b.none ? null : b[side], legs, { p: fp.probs[side], at: fair.at, single: fair.single },
            rp && isNum(rp.probs[side]) ? rp.probs[side] : null, fp.derived, id, twSrc);
        });
      });
    });

    // pipeline 換算好的其他玩法（足球的其他大小線、兩隊都進球、單雙、上半場…）。
    // 公允機率來自 pipeline；瀏覽器直接讀到運彩時，價格換成現在的。
    var up = match.tw;
    ((up && up.derived) || []).forEach(function (d) {
      if (!isNum(d.p) || !(d.price > 1)) return;
      var price = d.price, src = { src: 'upload', age: ageMin(up.at, now) };
      if (ctx.tw && ctx.tw.names && ctx.tw.names[d.market]) {
        var live = ctx.tw.names[d.market][String(d.key || '').split('_').pop()];
        if (live > 1) { price = live; src = { src: 'live', age: ageMin(ctx.tw.at, now) }; }
      }
      add('derived', d.key, d.line, d.label, price, (up.legs || {})[d.market],
        { p: d.p, at: (match.fair && match.fair.h2h && match.fair.h2h.at) || null }, null, true, 'derived:' + d.market + ':' + d.key, src);
      bets[bets.length - 1].marketName = d.market;
    });

    // 國際盤往哪邊動：共識價（磁帶）的第一筆到最新一筆，加上這次開著網頁期間參考盤的變化。
    var move = {};
    var tape = (match.tape && match.tape.intl) || [];
    if (tape.length >= 2) {
      var first = tape[0][1], last = tape[tape.length - 1][1];
      Object.keys(last).forEach(function (s) { if (isNum(first[s])) move[s] = last[s] - first[s]; });
    }
    if (ctx.ref && ctx.ref.h2h && ctx.refFirst && ctx.refFirst.h2h) {
      ['home', 'draw', 'away'].forEach(function (s) {
        if (isNum(ctx.ref.h2h[s]) && isNum(ctx.refFirst.h2h[s])) move[s] = (move[s] || 0) + ctx.ref.h2h[s] - ctx.refFirst.h2h[s];
      });
    }

    var RANK = { value: 5, check: 4, even: 3, near: 2, pass: 1, noprice: 0 };
    var best = null;
    bets.forEach(function (b) {
      if (!best) { best = b; return; }
      var r = RANK[b.status] - RANK[best.status];
      if (r > 0 || (r === 0 && b.status !== 'noprice' && b.ev > best.ev)) best = b;
    });
    // 沒有運彩價時，挑「國際盤最近往它移動最多」的不讓分選項當作要盯的對象。
    if (best && best.status === 'noprice') {
      var top = null;
      bets.forEach(function (b) {
        if (b.market !== 'h2h') return;
        var mv = move[b.side] || 0;
        if (!top || mv > (move[top.side] || 0) || (mv === (move[top.side] || 0) && b.p > top.p)) top = b;
      });
      if (top) best = top;
    }

    var grade = 'D', raw = 0;
    if (best) {
      var mv2 = best.market === 'h2h' ? (move[best.side] || 0) : 0;
      var mvPts = 10 * clamp(mv2 / 0.04, 0, 1);
      var hours = (toMs(match.kickoff) - now) / 3600000;
      if (best.status === 'noprice') {
        grade = 'W';
        raw = 30 + 5 * mvPts + 20 * clamp(1 - hours / 48, 0, 1);
      } else {
        grade = { value: 'A', check: 'B', even: 'B', near: 'C', pass: 'D' }[best.status];
        var evPts = 55 * clamp((best.ev + 0.15) / 0.23, 0, 1);
        var pricePts = 7.5 * clamp(1 - Math.max(0, (best.priceAge || 0) - S.priceFreshMin) / 690, 0, 1);
        var fairPts = 7.5 * clamp(1 - Math.max(0, (best.fairAge === null ? 999 : best.fairAge) - S.fairFreshMin) / 330, 0, 1);
        var agree = best.pRef === null ? 5 : 10 * clamp(1 - Math.abs(best.pCons - best.pRef) / 0.03, 0, 1);
        var legPts = best.legs === 1 ? 10 : (best.legs === 2 ? 4 : (best.legs ? 0 : 5));
        raw = evPts + pricePts + fairPts + agree + mvPts + legPts;
      }
    }
    // 分數落在等級的區間裡，排序就不會出現「不下」排在「可下」前面。
    var RANGE = { A: [80, 100], B: [60, 79], C: [40, 59], W: [20, 45], D: [0, 35] };
    var rg = RANGE[grade];
    var score = Math.round(rg[0] + clamp(raw, 0, 100) / 100 * (rg[1] - rg[0]));

    if (best && best.pRef !== null && Math.abs(best.pCons - best.pRef) >= 0.03) {
      notes.push('共識價和即時參考盤差 ' + (Math.abs(best.pCons - best.pRef) * 100).toFixed(1) + ' 個百分點：國際盤可能已經動了，先更新共識價');
    }
    return { key: match.key, match: match, bets: bets, best: best, grade: grade, score: score, move: move,
      fairAt: fairAt, twAt: tw ? tw.at : null, twSrc: tw ? (tw.src || 'upload') : null, notes: notes };
  }

  // Kelly 注金：f* = (p × 賠率 − 1) ÷ (賠率 − 1)，只下其中一部分，再壓在單注上限以下，取到 100 元。
  function stake(p, price, settings) {
    var S = Object.assign({}, DEFAULTS, settings || {});
    if (!(price > 1) || !(p > 0)) return 0;
    var f = (p * price - 1) / (price - 1);
    if (f <= 0) return 0;
    var amount = S.bankroll * Math.min(f * S.kelly, S.maxPct);
    var rounded = Math.floor(amount / S.unit) * S.unit;
    return rounded >= S.unit ? rounded : (amount > 0 ? S.unit : 0);
  }

  /* 每場的「投資方式」。evals = evaluate() 的結果（全部比賽），回傳依分數排好的清單，
   * 每一筆多了 plan = { action, text[], stake, parlay } 。
   * action：bet 現在可下 / confirm 先確認運彩現價 / wait 等運彩價 / skip 不下。
   */
  function plan(evals, settings) {
    var S = Object.assign({}, DEFAULTS, settings || {});
    var ranked = evals.filter(function (e) { return e.best; }).sort(function (a, b) {
      return b.score - a.score || (toMs(a.match.kickoff) || 0) - (toMs(b.match.kickoff) || 0);
    });
    // 可以拿來串關的腿：別場比賽裡 EV 最高的下法（至少不賠水錢）。
    var legsPool = ranked.filter(function (e) { return e.best.ev !== null && e.best.ev >= 0; });
    ranked.forEach(function (e) {
      var b = e.best, p = { action: 'skip', stake: 0, parlay: null, text: [] };
      var legs = b.legs || null;
      if (b.status === 'noprice') {
        p.action = 'wait';
        p.text.push('運彩賠率還沒進來。' + (b.need ? '在運彩看到「' + b.label + '」≥ ' + b.need.toFixed(2) + ' 才有 ' + (b.thr * 100).toFixed(0) + '% 以上的價值' : ''));
      } else if (b.status === 'value' || b.status === 'check') {
        p.action = b.status === 'value' ? 'bet' : 'confirm';
        var ev = b.ev, price = b.price, prob = b.p;
        if (legs && legs > 1) {
          var partners = legsPool.filter(function (x) { return x.key !== e.key; }).slice(0, legs - 1);
          if (partners.length < legs - 1) {
            p.action = 'skip';
            p.text.push('這個玩法至少要串 ' + legs + ' 關，目前沒有其他不賠水錢的選項可以搭，單獨這一注下不了');
          } else {
            partners.forEach(function (x) { ev = (1 + ev) * (1 + x.best.ev) - 1; price *= x.best.price; prob *= x.best.p; });
            p.parlay = { legs: legs, ev: ev, price: price, prob: prob,
              with: partners.map(function (x) { return { key: x.key, label: x.best.label, price: x.best.price, ev: x.best.ev }; }) };
            if (ev < b.thr) {
              p.action = 'skip';
              p.text.push('至少要串 ' + legs + ' 關；搭上目前最好的其他選項後整張 EV 只剩 ' + (ev * 100).toFixed(1) + '%，不夠');
            } else {
              p.text.push('至少要串 ' + legs + ' 關：搭 ' + p.parlay.with.map(function (w) { return w.label + ' @' + w.price.toFixed(2); }).join('、') +
                '，整張賠率約 ' + price.toFixed(2) + '、EV ' + (ev * 100).toFixed(1) + '%');
            }
          }
        } else if (legs === 1) {
          p.text.push('單場可投');
        } else {
          p.text.push('串關限制未知：下注前在運彩確認這個玩法能不能單場投');
        }
        if (p.action !== 'skip') {
          p.stake = stake(prob, price, S);
          p.text.unshift((p.action === 'bet' ? '下 ' : '先確認運彩現在的賠率，還有 ≥ ' + b.need.toFixed(2) + ' 再下 ') + b.label + ' @ ' + b.price.toFixed(2));
          if (b.single) p.text.push('公允價目前只有參考盤一家莊家的價格：先買共識價確認');
          else if (!b.fairOk) p.text.push('國際共識價已經超過 3 小時沒更新：先更新共識價再判斷');
          p.text.push('最低可接受賠率 ' + b.need.toFixed(2) + '（低於它就不下）');
          p.text.push('建議注金 ' + p.stake + ' 元（本金 ' + S.bankroll + ' 的 ' + (p.stake / S.bankroll * 100).toFixed(1) + '%，' + (S.kelly === 1 ? '全' : '1/' + Math.round(1 / S.kelly)) + ' Kelly）');
        }
      } else {
        var word = b.status === 'even' ? '不賠水錢但也沒有賺頭' : (b.status === 'near' ? '差一點' : '運彩的價格比公允價差太多');
        p.text.push('不下：' + word + '（' + b.label + ' @ ' + b.price.toFixed(2) + '，EV ' + (b.ev * 100).toFixed(1) + '%）');
        p.text.push('運彩把「' + b.label + '」調到 ≥ ' + b.need.toFixed(2) + ' 才可以下');
      }
      e.plan = p;
      // 串不成的「可下」降一級：它現在不能照著做。
      if (p.action === 'skip' && (e.grade === 'A' || e.grade === 'B') && b.ev >= b.thr) { e.grade = 'C'; e.score = Math.min(e.score, 59); }
    });
    return ranked.sort(function (a, b) { return b.score - a.score || (toMs(a.match.kickoff) || 0) - (toMs(b.match.kickoff) || 0); });
  }

  // ------------------------------------------------------------------ 結算（追蹤用）
  // 回傳 'win' | 'loss' | 'push' | null（這種玩法要自己結算）。
  function settle(bet, home, away) {
    if (!isNum(home) || !isNum(away)) return null;
    if (bet.market === 'h2h') {
      var w = home > away ? 'home' : (home < away ? 'away' : 'draw');
      return bet.side === w ? 'win' : 'loss';
    }
    if (bet.market === 'totals' && isNum(bet.line)) {
      var t = home + away;
      if (t === bet.line) return 'push';
      return (t > bet.line) === (bet.side === 'over') ? 'win' : 'loss';
    }
    if (bet.market === 'spread' && isNum(bet.line)) {
      var d = home - away + bet.line; // line = 主隊的讓分
      if (d === 0) return 'push';
      return (d > 0) === (bet.side === 'home') ? 'win' : 'loss';
    }
    return null;
  }
  function profit(result, stakeAmount, price) {
    return result === 'win' ? stakeAmount * (price - 1) : (result === 'loss' ? -stakeAmount : 0);
  }

  return {
    DEFAULTS: DEFAULTS, SIDES: SIDES,
    americanToDecimal: americanToDecimal, devig: devig, consensus: consensus, median: median,
    cdf: cdf, overProbs: overProbs, meanFromOver: meanFromOver, shiftLine: shiftLine,
    norm: norm, sim: sim, matchEvents: matchEvents,
    parseEspn: parseEspn, fairFromPrices: fairFromPrices, parseOddsApi: parseOddsApi, mainLine: mainLine,
    parseTw: parseTw, matchTw: matchTw, twFromGame: twFromGame, zhKey: zhKey,
    probsAt: probsAt, needPrice: needPrice, evaluate: evaluate, stake: stake, plan: plan,
    settle: settle, profit: profit, ageMin: ageMin, toMs: toMs
  };
});
