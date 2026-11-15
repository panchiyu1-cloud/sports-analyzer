/* 即時精選：抓資料、排名、畫畫面。運算都在 engine.js（有測試），這裡只負責「去哪裡拿價格」和「怎麼顯示」。
 *
 * 價格來源（全部在使用者自己的瀏覽器裡讀，不經過任何伺服器）：
 *   data.json      pipeline 每次 render 寫的：國際共識價、最後一次上傳的運彩價、門檻
 *   ESPN 計分板     一家莊家的現價，約 30 秒看一次：只用來看「盤有沒有動」，開賽後不再讀
 *   運彩            瀏覽器讀得到就直接讀（要在台灣、而且運彩允許）；讀不到就用手機上傳的價格或手動輸入
 *   The Odds API   使用者自己的 key（只存在這個瀏覽器的 localStorage），手動按才買，有每日上限
 */
(function () {
  'use strict';
  var E = window.Engine;
  var ESPN = 'https://site.api.espn.com/apis/site/v2/sports/';
  var TW = 'https://blob3rd.sportslottery.com.tw/apidata/Pre/';
  var ODDS = 'https://api.the-odds-api.com/v4/sports/';
  var RESERVE = 25;                       // The Odds API 留給 pipeline 的點數，這一頁不會買到低於它
  var LS = { settings: 'live.settings', hist: 'live.hist', track: 'live.track', manual: 'live.manual', key: 'live.oddskey', fair: 'live.fair', spend: 'live.spend' };

  function load(k, d) { try { var v = localStorage.getItem(k); return v ? JSON.parse(v) : d; } catch (e) { return d; } }
  function save(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) { /* 私密模式存不了也照常運作 */ } }

  var S = {
    data: null, dataAt: 0, dataErr: null,
    settings: Object.assign({}, E.DEFAULTS, { poll: 30, buyCap: 6, buyMarkets: 'all', twIds: {} }, load(LS.settings, {})),
    espn: {}, hist: load(LS.hist, {}), fair: load(LS.fair, {}), tw: {}, manual: load(LS.manual, {}), track: load(LS.track, []),
    key: load(LS.key, ''), spend: load(LS.spend, { day: '', used: 0 }),
    st: { espn: { ok: null, at: 0, provider: '' }, tw: {}, odds: { remaining: null, msg: '' } },
    due: {}, ranked: [], flash: {}, ui: { tab: 'top', open: {}, filter: 'all', armed: null, msg: '' }
  };

  // ------------------------------------------------------------------ 顯示用的小工具
  function esc(s) {
    return String(s === null || s === undefined ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  var FMT = new Intl.DateTimeFormat('zh-TW', { timeZone: 'Asia/Taipei', month: 'numeric', day: 'numeric', weekday: 'short', hour: '2-digit', minute: '2-digit', hour12: false });
  var CLK = new Intl.DateTimeFormat('zh-TW', { timeZone: 'Asia/Taipei', hour: '2-digit', minute: '2-digit', hour12: false });
  function when(t) { var ms = E.toMs(t); return ms === null ? '—' : FMT.format(new Date(ms)); }
  function clock(t) { var ms = E.toMs(t); return ms === null ? '—' : CLK.format(new Date(ms)); }
  function ago(min) {
    if (min === null || min === undefined) return '—';
    if (min < 1.5) return '剛剛';
    if (min < 90) return Math.round(min) + ' 分前';
    return (min / 60).toFixed(min < 600 ? 1 : 0) + ' 小時前';
  }
  function left(t, now) {
    var min = Math.max(0, Math.round((E.toMs(t) - now) / 60000));
    if (min < 60) return '還有 ' + min + ' 分';
    if (min < 2880) return '還有 ' + Math.floor(min / 60) + ' 小時 ' + (min % 60) + ' 分';
    return '還有 ' + Math.floor(min / 1440) + ' 天';
  }
  function pct(x, d) { return x === null || x === undefined ? '—' : (x >= 0 ? '+' : '') + (x * 100).toFixed(d === undefined ? 1 : d) + '%'; }
  function px(x) { return x > 1 ? x.toFixed(2) : '—'; }
  function ymd(ms) { var d = new Date(ms); return '' + d.getUTCFullYear() + ('0' + (d.getUTCMonth() + 1)).slice(-2) + ('0' + d.getUTCDate()).slice(-2); }
  function today() { return ymd(Date.now() + 8 * 3600000); }
  var GRADE = { A: '✓ 可下', B: '◐ 先確認', C: '△ 差一點', W: '… 等運彩價', D: '✕ 不下' };
  var US = { mlb: 1, nba: 1 };

  // ------------------------------------------------------------------ 讀資料
  function getJson(url, opts) {
    var ctl = new AbortController(), timer = setTimeout(function () { ctl.abort(); }, 12000);
    return fetch(url, Object.assign({ signal: ctl.signal, cache: 'no-store' }, opts || {})).then(function (r) {
      clearTimeout(timer);
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json().then(function (j) { return { json: j, headers: r.headers }; });
    }, function (e) { clearTimeout(timer); throw e; });
  }
  function upcoming() {
    var now = Date.now();
    return (S.data ? S.data.matches : []).filter(function (m) { return E.toMs(m.kickoff) > now; });
  }
  function league(code) {
    return (S.data ? S.data.leagues : []).filter(function (l) { return l.code === code; })[0] || null;
  }

  function loadData() {
    return getJson('data.json?ts=' + Date.now()).then(function (r) {
      if (!r.json || !Array.isArray(r.json.matches)) throw new Error('data.json 格式不對');
      S.data = r.json; S.dataAt = Date.now(); S.dataErr = null;
      prune();
    }).catch(function (e) { S.dataErr = e.name === 'AbortError' ? '逾時' : (e.message || '讀取失敗'); });
  }
  // 已經開賽或不在名單上的比賽：把瀏覽器裡留著的價格紀錄清掉。
  function prune() {
    var keep = {};
    upcoming().forEach(function (m) { keep[m.key] = 1; });
    [S.hist, S.fair, S.manual, S.espn, S.tw].forEach(function (o) { Object.keys(o).forEach(function (k) { if (!keep[k]) delete o[k]; }); });
    save(LS.hist, S.hist); save(LS.fair, S.fair); save(LS.manual, S.manual);
  }

  // ESPN 的日期是美東日期；足球多查一個 UTC 日期，免得跨日的比賽漏掉。
  function espnDates(m) {
    var k = E.toMs(m.kickoff), a = ymd(k - 5 * 3600000), b = ymd(k);
    return (US[m.sport] || a === b) ? [a] : [a, b];
  }
  function pollEspn(force) {
    var now = Date.now(), groups = {};
    upcoming().forEach(function (m) {
      var lg = league(m.league);
      if (!lg || !lg.espn || E.toMs(m.kickoff) - now > 7 * 86400000) return;
      espnDates(m).forEach(function (d) {
        var g = groups[lg.espn + '|' + d] = groups[lg.espn + '|' + d] || { path: lg.espn, date: d, matches: [], soon: false };
        g.matches.push(m);
        if (E.toMs(m.kickoff) - now < 86400000) g.soon = true;
      });
    });
    var jobs = Object.keys(groups).filter(function (id) {
      var every = groups[id].soon ? Math.max(15, S.settings.poll) * 1000 : 300000;
      if (!force && S.due[id] && now - S.due[id] < every) return false;
      S.due[id] = now;
      return true;
    }).map(function (id) {
      var g = groups[id];
      return getJson(ESPN + g.path + '/scoreboard?dates=' + g.date).then(function (r) {
        var found = E.matchEvents(g.matches, E.parseEspn(r.json));
        Object.keys(found).forEach(function (key) { takeEspn(key, found[key], g); });
        S.st.espn.ok = true; S.st.espn.at = Date.now();
      }).catch(function () { S.st.espn.ok = false; });
    });
    return Promise.all(jobs);
  }
  function takeEspn(key, ev, g) {
    var now = Date.now();
    // 開賽後的價格是滾球盤，不能拿來比賽前的運彩價：只留賽前的。
    if (ev.state !== 'pre' || !ev.odds) { if (S.espn[key]) S.espn[key].state = ev.state; return; }
    S.espn[key] = { at: now, id: ev.id, path: g.path, date: g.date, state: ev.state, odds: ev.odds, open: E.fairFromPrices(ev.open, null), fair: E.fairFromPrices(ev.odds, now) };
    if (ev.odds.provider) S.st.espn.provider = ev.odds.provider;
    var f = S.espn[key].fair && S.espn[key].fair.h2h;
    if (!f) return;
    var p = {}, o = {};
    ['home', 'draw', 'away'].forEach(function (s) { if (f[s] !== undefined) { p[s] = Math.round(f[s] * 10000) / 10000; o[s] = Math.round(ev.odds.h2h[s] * 1000) / 1000; } });
    var h = S.hist[key] = S.hist[key] || [], last = h[h.length - 1];
    if (!last || JSON.stringify(last.o) !== JSON.stringify(o)) {
      h.push({ t: now, p: p, o: o });
      if (h.length > 60) h.splice(1, h.length - 60);   // 第一筆留著：它是「第一次看到」的基準
      if (last) S.flash[key] = now;
      save(LS.hist, S.hist);
    }
  }

  // 運彩：讀得到就直接讀。讀不到（瀏覽器被擋、不在台灣）就 10 分鐘後再試，畫面照常用上傳的價格。
  function pollTw(force) {
    if (!S.data) return Promise.resolve();
    var now = Date.now(), sports = {};
    upcoming().forEach(function (m) { sports[m.sport] = 1; });
    // 有運動還不知道運彩代號、又沒有任何一種讀得到：用足球的檔案試一次瀏覽器能不能讀（找代號要先知道這件事）。
    var feeds = S.data.tw_feeds || {};
    var noId = Object.keys(sports).some(function (s) { return !(S.settings.twIds[s] || feeds[s]); });
    if (noId && feeds.football) sports.football = 1;
    return Promise.all(Object.keys(sports).map(function (sport) {
      var st = S.st.tw[sport] = S.st.tw[sport] || { state: 'unknown', at: 0, n: 0 };
      var id = S.settings.twIds[sport] || (S.data.tw_feeds || {})[sport];
      if (!id) { st.state = 'noid'; return findTwId(sport); }
      var every = st.state === 'ok' ? 60000 : 600000;
      if (!force && st.tried && now - st.tried < every) return null;
      st.tried = now;
      return getJson(TW + encodeURIComponent(id) + '-Games.zh.json').then(function (r) {
        takeTw(sport, r.json, 'live');
      }).catch(function () { st.state = 'blocked'; });
    }));
  }
  function takeTw(sport, payload, how) {
    var games = E.parseTw(payload), now = Date.now();
    var ms = upcoming().filter(function (m) { return !sport || m.sport === sport; });
    var found = E.matchTw(ms, games, S.data.zh || {});
    Object.keys(found).forEach(function (key) { S.tw[key] = E.twFromGame(found[key], now); });
    if (sport) S.st.tw[sport] = { state: 'ok', at: now, tried: now, n: Object.keys(found).length, games: games.length, how: how };
    return Object.keys(found).length;
  }
  // 籃球的運彩代號還不知道：足球或棒球讀得到（代表瀏覽器沒被擋）時，一天試一次附近的代號。
  function findTwId(sport) {
    var ok = Object.keys(S.st.tw).some(function (s) { return S.st.tw[s].state === 'ok'; });
    var st = S.st.tw[sport];
    if (!ok || st.looked === today()) return null;
    st.looked = today();
    var known = Object.keys(S.data.tw_feeds || {}).map(function (s) { return S.data.tw_feeds[s]; });
    var ids = [];
    for (var i = 34725; i <= 34760; i++) if (known.indexOf(i + '.1') < 0) ids.push(i + '.1');
    var ms = upcoming().filter(function (m) { return m.sport === sport; });
    function next() {
      var id = ids.shift();
      if (!id) return null;
      return getJson(TW + id + '-Games.zh.json').then(function (r) {
        if (Object.keys(E.matchTw(ms, E.parseTw(r.json), S.data.zh || {})).length) {
          S.settings.twIds[sport] = id; save(LS.settings, S.settings);
          takeTw(sport, r.json, 'live');
          return null;
        }
        return next();
      }).catch(function () { return next(); });
    }
    return next();
  }

  // The Odds API：只在使用者按下去的時候買，花的是和 pipeline 同一個帳戶的點數。
  function buyCost(lg) {
    if (S.settings.buyMarkets === 'h2h') return { markets: 'h2h', cost: 1 };
    if (lg.sport === 'football') return { markets: 'h2h,totals', cost: 2 };
    if (lg.sport === 'nba') return { markets: 'h2h,spreads,totals', cost: 3 };
    return { markets: 'h2h,spreads', cost: 2 };
  }
  function spentToday() { if (S.spend.day !== today()) S.spend = { day: today(), used: 0 }; return S.spend.used; }
  function buy(code) {
    var lg = league(code), c = lg && buyCost(lg);
    if (!lg || !S.key) return Promise.resolve();
    var remaining = S.st.odds.remaining !== null ? S.st.odds.remaining : ((S.data.budget || {}).remaining || 0);
    if (spentToday() + c.cost > S.settings.buyCap) { S.ui.msg = '今天在這一頁已經買了 ' + spentToday() + ' 點，到上限了（設定裡可以調）'; return Promise.resolve(); }
    if (remaining - c.cost < RESERVE) { S.ui.msg = '這個月只剩 ' + remaining + ' 點，留給每天的自動更新，不買'; return Promise.resolve(); }
    var ms = upcoming().filter(function (m) { return m.league === code; });
    var url = ODDS + lg.odds_key + '/odds?regions=eu&oddsFormat=decimal&dateFormat=iso&markets=' + c.markets + '&apiKey=' + encodeURIComponent(S.key);
    S.ui.msg = '買 ' + lg.name + ' 的共識價中…';
    return getJson(url).then(function (r) {
      var at = new Date().toISOString();
      var found = E.matchEvents(ms, E.parseOddsApi(r.json, at, lg.sport === 'football' ? 2.5 : null), US[lg.sport] ? 6 : 3);
      Object.keys(found).forEach(function (key) { S.fair[key] = found[key].fair; });
      save(LS.fair, S.fair);
      S.spend.used = spentToday() + c.cost; save(LS.spend, S.spend);
      var rem = parseInt(r.headers.get('x-requests-remaining'), 10);
      if (isFinite(rem)) S.st.odds.remaining = rem;
      S.ui.msg = lg.name + '：更新了 ' + Object.keys(found).length + ' 場的共識價（花 ' + c.cost + ' 點' + (isFinite(rem) ? '，本月剩 ' + rem : '') + '）';
    }).catch(function (e) {
      // 錯誤訊息可能帶著含 key 的網址，只顯示種類。
      S.ui.msg = '買共識價失敗（' + (e.name === 'AbortError' ? '逾時' : (/^HTTP \d+$/.test(e.message) ? e.message : '連線問題')) + '）';
    });
  }
  function checkKey() {
    if (!S.key) return Promise.resolve();
    return getJson('https://api.the-odds-api.com/v4/sports?apiKey=' + encodeURIComponent(S.key)).then(function (r) {
      var rem = parseInt(r.headers.get('x-requests-remaining'), 10);
      S.st.odds.remaining = isFinite(rem) ? rem : null;
      S.st.odds.msg = 'key 可以用' + (isFinite(rem) ? '，本月剩 ' + rem + ' 點' : '');
    }).catch(function (e) { S.st.odds.msg = /401|403/.test(e.message) ? 'key 不對或已停用' : '查不到（連線問題）'; });
  }

  // ------------------------------------------------------------------ 評估、追蹤
  function evaluateAll() {
    if (!S.data) return;
    var now = Date.now();
    var evals = upcoming().map(function (m) {
      var e = S.espn[m.key], h = S.hist[m.key];
      var ref = e && now - e.at < 15 * 60000 ? e.fair : null;   // 參考盤太久沒更新（例如分頁放在背景）就不用
      return E.evaluate(m, { cfg: S.data.cfg, fair: S.fair[m.key], ref: ref, refFirst: h && h.length ? { h2h: h[0].p } : null, tw: S.tw[m.key], manual: S.manual[m.key] }, S.settings, now);
    });
    S.ranked = E.plan(evals, S.settings);
    // 追蹤中的注：記下開賽前最後看到的公允機率（收盤價值 CLV 用）。
    var by = {};
    S.ranked.forEach(function (e) { by[e.key] = e; });
    var changed = false;
    S.track.forEach(function (t) {
      var e = by[t.key];
      if (!e || t.result) return;
      var b = e.bets.filter(function (x) { return x.id === t.bet.id; })[0];
      if (b && b.pCons !== t.closeP) { t.closeP = b.pCons; changed = true; }
    });
    if (changed) save(LS.track, S.track);
  }
  function addTrack(key) {
    var e = S.ranked.filter(function (x) { return x.key === key; })[0];
    if (!e || !e.best || !(e.best.price > 1)) return;
    var b = e.best, m = e.match, lg = league(m.league);
    S.track.unshift({
      id: key + '|' + b.id + '|' + Date.now(), key: key, at: Date.now(), sport: m.sport, league: m.league_name, kickoff: m.kickoff,
      home: m.home, away: m.away, home_key: m.home_key, away_key: m.away_key, title: title(m), espn: lg ? lg.espn : null,
      bet: { id: b.id, market: b.market, side: b.side, line: b.line, label: b.label }, price: b.price, p: b.p, ev: b.ev,
      stake: (e.plan && e.plan.stake) || S.settings.unit, grade: e.grade, closeP: b.pCons, result: null, score: null
    });
    save(LS.track, S.track);
  }
  // 比賽打完：用 ESPN 的比分結算不讓分、讓分、大小；其他玩法自己按。
  function settleTracked() {
    var now = Date.now(), groups = {};
    S.track.forEach(function (t) {
      if (t.result || !t.espn || E.toMs(t.kickoff) + 100 * 60000 > now || E.toMs(t.kickoff) + 3 * 86400000 < now) return;
      espnDates(t).forEach(function (d) { (groups[t.espn + '|' + d] = groups[t.espn + '|' + d] || { path: t.espn, date: d, items: [] }).items.push(t); });
    });
    return Promise.all(Object.keys(groups).map(function (id) {
      var g = groups[id];
      return getJson(ESPN + g.path + '/scoreboard?dates=' + g.date).then(function (r) {
        var events = E.parseEspn(r.json).filter(function (e) { return e.completed; });
        g.items.forEach(function (t) {
          var ev = E.matchEvents([Object.assign({}, t, { key: t.id, id: (t.key.split(':')[1] || '') })], events)[t.id];
          if (!ev || t.result) return;
          var res = E.settle(t.bet, ev.score.home, ev.score.away);
          t.score = ev.score.home + '–' + ev.score.away;
          if (res) { t.result = res; t.profit = E.profit(res, t.stake, t.price); }
        });
        save(LS.track, S.track);
      }).catch(function () { /* 下次再試 */ });
    }));
  }

  // ------------------------------------------------------------------ 畫面
  function title(m) {
    var h = m.home_zh || m.home, a = m.away_zh || m.away;
    return US[m.sport] ? a + ' @ ' + h : h + ' vs ' + a;
  }
  function sideName(m, s) { return s === 'draw' ? '和局' : (m[s + '_zh'] || m[s]); }

  function oddsCells(e) {
    var m = e.match, sp = S.espn[m.key], h = S.hist[m.key] || [], first = h.length ? h[0].p : null;
    var sides = US[m.sport] ? ['away', 'home'] : ['home', 'draw', 'away'];
    var flash = S.flash[m.key] && Date.now() - S.flash[m.key] < 4000;
    return sides.map(function (s) {
      var bet = e.bets.filter(function (b) { return b.market === 'h2h' && b.side === s; })[0];
      if (!bet && !(sp && sp.odds && sp.odds.h2h && sp.odds.h2h[s])) return '';
      var now = sp && sp.odds && sp.odds.h2h ? sp.odds.h2h[s] : null;
      var d = sp && sp.fair && sp.fair.h2h && first && first[s] !== undefined ? sp.fair.h2h[s] - first[s] : null;
      var delta = d === null || Math.abs(d) < 0.0005 ? '' :
        '<span class="delta ' + (d > 0 ? 'pos' : 'neg') + '" title="參考盤去水後的機率，和第一次看到時比">' + (d > 0 ? '▲' : '▼') + Math.abs(d * 100).toFixed(1) + '</span>';
      return '<div class="cell' + (flash ? ' flash' : '') + '"><div class="side">' + esc(sideName(m, s)) + '</div>' +
        '<div class="now">' + px(now) + delta + '</div><div class="cap">即時參考盤</div>' +
        '<div class="row"><span>公允</span><b class="fair">' + (bet ? px(1 / bet.pCons) : '—') + '</b></div>' +
        '<div class="row"><span>運彩</span><b class="tw">' + (bet && bet.price ? px(bet.price) : '—') + '</b></div></div>';
    }).join('');
  }

  function spark(e) {
    var m = e.match, side = e.best && e.best.market === 'h2h' ? e.best.side : 'home', series = [];
    function pts(list, get) {
      return list.map(function (x) { var v = get(x); return v ? [E.toMs(v[0]), v[1]] : null; })
        .filter(function (x) { return x && x[0] !== null && typeof x[1] === 'number'; });
    }
    var intl = pts((m.tape && m.tape.intl) || [], function (x) { return [x[0], x[1][side]]; });
    var bought = S.fair[m.key] && S.fair[m.key].h2h;
    if (bought && typeof bought[side] === 'number') intl.push([E.toMs(bought.at), bought[side]]);
    series.push({ c: 'var(--intl)', w: 2, p: intl });
    series.push({ c: 'var(--tw)', w: 2, p: pts((m.tape && m.tape.tw) || [], function (x) { return [x[0], x[1][side]]; }) });
    series.push({ c: 'var(--ref)', w: 1.3, dash: '3 3', p: pts(S.hist[m.key] || [], function (x) { return [x.t, x.p[side]]; }) });
    var all = [].concat.apply([], series.map(function (s) { return s.p; }));
    if (all.length < 2) return '<p class="sub">這場還沒有足夠的價格紀錄可以畫線（至少要兩筆）。</p>';
    var x0 = Math.min.apply(null, all.map(function (p) { return p[0]; })), x1 = Math.max.apply(null, all.map(function (p) { return p[0]; }));
    var y0 = Math.min.apply(null, all.map(function (p) { return p[1]; })) - 0.01, y1 = Math.max.apply(null, all.map(function (p) { return p[1]; })) + 0.01;
    if (x1 === x0) x1 = x0 + 1;
    var W = 600, H = 86;
    function X(t) { return (6 + (t - x0) / (x1 - x0) * (W - 12)).toFixed(1); }
    function Y(v) { return (H - 6 - (v - y0) / (y1 - y0) * (H - 12)).toFixed(1); }
    var body = series.map(function (s) {
      if (!s.p.length) return '';
      var dots = s.p.length === 1 ? '<circle cx="' + X(s.p[0][0]) + '" cy="' + Y(s.p[0][1]) + '" r="3" fill="' + s.c + '"/>' : '';
      return dots + '<polyline fill="none" stroke="' + s.c + '" stroke-width="' + s.w + '"' + (s.dash ? ' stroke-dasharray="' + s.dash + '"' : '') +
        ' points="' + s.p.map(function (p) { return X(p[0]) + ',' + Y(p[1]); }).join(' ') + '"/>';
    }).join('');
    return '<svg class="spark" viewBox="0 0 ' + W + ' ' + H + '" preserveAspectRatio="none" role="img" aria-label="隱含機率變化">' + body + '</svg>' +
      '<div class="legend"><span><i style="background:var(--intl)"></i>國際共識</span><span><i style="background:var(--tw)"></i>運彩（去水後）</span>' +
      '<span><i style="background:var(--ref)"></i>即時參考盤</span><span>' + esc(sideName(m, side)) + ' 的隱含機率 ' + (y0 * 100 + 1).toFixed(0) + '%–' + (y1 * 100 - 1).toFixed(0) + '%・' + clock(x0) + ' → ' + clock(x1) + '</span></div>';
  }

  function detail(e) {
    var m = e.match, now = Date.now(), lg = league(m.league), man = S.manual[m.key] || {};
    var rows = e.bets.map(function (b) {
      var typed = man[b.id] ? man[b.id].price : '';
      var cls = b.status === 'value' ? 'pos' : (b.ev !== null && b.ev < 0 ? 'neg' : '');
      return '<tr' + (e.best && b.id === e.best.id ? ' class="on"' : '') + '><td>' + esc(b.label) + (b.derived ? '<span class="tag">換算</span>' : '') + '</td>' +
        '<td>' + (b.legs ? (b.legs === 1 ? '單場' : '串 ' + b.legs + ' 關') : '<span class="muted">—</span>') + '</td>' +
        '<td class="r"><input inputmode="decimal" aria-label="' + esc(b.label) + ' 的運彩賠率" data-key="' + esc(m.key) + '" data-bet="' + esc(b.id) + '" value="' + esc(typed) + '" placeholder="' + (b.price && b.priceSrc !== 'manual' ? b.price.toFixed(2) : '輸入') + '"></td>' +
        '<td class="n">' + px(1 / b.pCons) + '</td><td class="n">' + (b.need ? b.need.toFixed(2) : '—') + '</td>' +
        '<td class="n ' + cls + '">' + pct(b.ev) + '</td></tr>';
    }).join('');
    var sp = S.espn[m.key], c = lg && buyCost(lg);
    var times = ['共識價 ' + (e.fairAt ? clock(e.fairAt) + '（' + ago(E.ageMin(e.fairAt, now)) + '）' : '還沒有'),
      '運彩 ' + (e.twAt ? clock(e.twAt) + '（' + (e.twSrc === 'live' ? '即時讀取' : '手機上傳') + '，' + ago(E.ageMin(e.twAt, now)) + '）' : '還沒有'),
      '參考盤 ' + (sp ? esc(S.st.espn.provider || 'ESPN') + ' ' + clock(sp.at) : '還沒有')];
    var buyBtn = !lg ? '' : (S.key
      ? '<button class="btn" data-act="buy" data-code="' + esc(m.league) + '">' + (S.ui.armed === 'buy:' + m.league ? '再按一次確認：花 ' + c.cost + ' 點' : '買' + esc(lg.name) + '最新共識價（' + c.cost + ' 點）') + '</button>'
      : '<button class="btn" data-act="tab" data-tab="set">設定 The Odds API key 就能自己更新共識價</button>');
    return '<div class="detail"><div class="scroll"><table><thead><tr><th>下法</th><th>串關</th><th class="r">運彩賠率</th><th class="r">公允</th><th class="r">至少要</th><th class="r">EV</th></tr></thead><tbody>' +
      rows + '</tbody></table></div><p class="sub">在「運彩賠率」那一格打你在運彩看到的價格，EV 和建議會立刻重算（只存在這個瀏覽器）。「至少要」＝這個價格以上才有價值。</p>' +
      spark(e) + '<p class="sub">' + times.join('・') + '</p><div class="acts">' + buyBtn +
      '<a class="btn" href="../' + esc(m.page) + '">完整分析卡</a></div></div>';
  }

  function card(e, rank) {
    var m = e.match, b = e.best, now = Date.now(), open = !!S.ui.open[m.key];
    var en = (m.home_zh || m.away_zh) ? '<small>' + esc(US[m.sport] ? m.away + ' @ ' + m.home : m.home + ' vs ' + m.away) + '</small>' : '';
    var pick = '';
    if (b) {
      pick = '<div class="pickline"><span class="what">' + esc(b.label) + (b.derived ? '<span class="tag">換算</span>' : '') + '</span>' +
        (b.price ? '<span><span class="lbl">運彩' + (b.priceSrc === 'manual' ? '（手動）' : '') + '</span><span class="big amber">' + b.price.toFixed(2) + '</span></span>' +
          '<span><span class="lbl">EV</span><span class="big ' + (b.ev >= b.thr ? 'pos' : (b.ev < 0 ? 'neg' : '')) + '">' + pct(b.ev) + '</span></span>' : '') +
        '<span><span class="lbl">至少要</span><span class="big">' + (b.need ? b.need.toFixed(2) : '—') + '</span></span>' +
        '<span><span class="lbl">公允</span><span class="big" style="color:#8CCBF5">' + px(1 / b.pCons) + '</span></span></div>';
    }
    var plan = e.plan ? '<ul class="plan">' + e.plan.text.map(function (t) { return '<li>' + esc(t) + '</li>'; }).join('') + '</ul>' : '';
    var notes = e.notes.map(function (n) { return '<p class="note">⚠ ' + esc(n) + '</p>'; }).join('');
    var tracked = S.track.some(function (t) { return t.key === m.key && !t.result && b && t.bet.id === b.id; });
    return '<article class="card" data-grade="' + e.grade + '" id="c-' + esc(m.key) + '"><div class="head">' +
      (rank ? '<div class="rank">' + rank + '</div>' : '') +
      '<div><div class="meta"><span class="lg">' + esc(m.league_name) + '</span><span>' + when(m.kickoff) + '</span><span class="muted">' + left(m.kickoff, now) + '</span></div>' +
      '<div class="teams">' + esc(title(m)) + en + '</div></div>' +
      '<div class="grade"><span class="chip ' + e.grade + '">' + GRADE[e.grade] + '</span><span class="score">' + e.score + ' 分</span></div></div>' +
      pick + '<div class="odds">' + oddsCells(e) + '</div>' + plan + notes +
      '<div class="acts"><button class="btn pri" data-act="open" data-key="' + esc(m.key) + '" aria-expanded="' + open + '">' + (open ? '收起分析 ▴' : '分析・輸入賠率 ▾') + '</button>' +
      (b && b.price ? '<button class="btn' + (tracked ? ' ok' : '') + '" data-act="track" data-key="' + esc(m.key) + '"' + (tracked ? ' disabled' : '') + '>' + (tracked ? '已加入追蹤' : '加入追蹤') + '</button>' : '') +
      '</div>' + (open ? detail(e) : '') + '</article>';
  }

  function sources() {
    var now = Date.now(), d = S.data, out = [];
    var newest = null, twAt = null, live = 0;
    S.ranked.forEach(function (e) {
      if (e.fairAt && (!newest || E.toMs(e.fairAt) > E.toMs(newest))) newest = e.fairAt;
      if (e.twAt && (!twAt || E.toMs(e.twAt) > E.toMs(twAt))) twAt = e.twAt;
      if (e.twSrc === 'live') live++;
    });
    out.push(['國際共識', newest ? ago(E.ageMin(newest, now)) : '還沒有', newest && E.ageMin(newest, now) <= 180]);
    out.push(['即時參考盤', S.st.espn.ok ? (S.st.espn.provider || 'ESPN') + ' ' + clock(S.st.espn.at) : (S.st.espn.ok === false ? '連不上' : '等待中'), !!S.st.espn.ok]);
    out.push(['運彩', live ? '即時讀取 ' + live + ' 場' : (twAt ? '手機上傳 ' + ago(E.ageMin(twAt, now)) : '沒有價格'), live > 0 || (twAt && E.ageMin(twAt, now) <= 30)]);
    out.push(['資料', d ? clock(d.generated_at) + ' 產生' : (S.dataErr || '讀取中'), !!d && !S.dataErr]);
    return '<div class="srcs">' + out.map(function (s) {
      return '<span class="src"><i class="dot ' + (s[2] ? 'on' : '') + '"></i>' + esc(s[0]) + ' <b>' + esc(s[1]) + '</b></span>';
    }).join('') + '</div>';
  }

  function viewTop() {
    var top = S.ranked.slice(0, 10), nA = S.ranked.filter(function (e) { return e.grade === 'A'; }).length;
    var nB = S.ranked.filter(function (e) { return e.grade === 'B' && e.plan && e.plan.action === 'confirm'; }).length;
    var head, sub;
    if (!S.ranked.length) {
      head = '現在沒有可以分析的比賽';
      sub = '接下來幾天沒有英超、西甲、德甲、義甲、法甲、歐冠、MLB 或 NBA 的比賽（或是資料還沒更新）。';
    } else if (nA) {
      head = '現在可下：' + nA + ' 場';
      sub = '運彩的價格高於國際公允價、而且兩邊的價格都是剛剛的。下注前看一眼運彩現價沒有低於「至少要」。';
    } else if (nB) {
      head = '有 ' + nB + ' 場要先確認運彩現價';
      sub = '用最後一次拿到的運彩價算起來有價值，但那個價格不是現在的。到運彩看現價，還在「至少要」以上才下。';
    } else {
      head = '現在沒有達到門檻的比賽';
      sub = '下面是最接近的 10 場：每場寫了運彩要開到多少才值得下。沒有達到就不下，這也是一種決定。';
    }
    var body = top.length ? '<div class="cards">' + top.map(function (e, i) { return card(e, i + 1); }).join('') + '</div>'
      : '<div class="empty"><b>沒有比賽</b>有比賽時，這裡會依分數排出前 10 場。</div>';
    // 運彩讀不到、畫面上又沒有任何運彩價：告訴使用者怎麼讓價格進來。
    var blocked = Object.keys(S.st.tw).some(function (s) { return S.st.tw[s].state === 'blocked'; });
    var noTw = S.ranked.length && S.ranked.every(function (e) { return e.grade === 'W'; });
    var hint = noTw ? '<p class="note">目前沒有任何一場有運彩賠率' + (blocked ? '（這個瀏覽器讀不到運彩）' : '') +
      '。用 iPhone 捷徑上傳一次，或在比賽卡按「分析・輸入賠率」打上你在運彩看到的價格，就會算出 EV 和建議。</p>' : '';
    return '<section class="hero"><h1>' + esc(head) + '</h1><p>' + esc(sub) + '</p>' + sources() + hint + msg() + '</section>' + body;
  }
  function msg() { return S.ui.msg ? '<p class="note">' + esc(S.ui.msg) + '</p>' : ''; }

  function viewAll() {
    var codes = {}, list = S.ranked.slice().sort(function (a, b) { return E.toMs(a.match.kickoff) - E.toMs(b.match.kickoff); });
    list.forEach(function (e) { codes[e.match.league] = e.match.league_name; });
    var f = S.ui.filter;
    var filters = '<div class="filters"><button data-act="filter" data-f="all" aria-pressed="' + (f === 'all') + '">全部 ' + list.length + '</button>' +
      Object.keys(codes).map(function (c) { return '<button data-act="filter" data-f="' + esc(c) + '" aria-pressed="' + (f === c) + '">' + esc(codes[c]) + '</button>'; }).join('') + '</div>';
    var shown = list.filter(function (e) { return f === 'all' || e.match.league === f; });
    return '<section class="hero"><h1>全部比賽</h1><p>依開賽時間排列。每一格上面的大數字是即時參考盤的現價（▲▼ 是和第一次看到時比，去水後機率變了幾個百分點），下面是公允賠率和運彩賠率。</p>' + sources() + msg() + '</section>' +
      filters + (shown.length ? '<div class="cards">' + shown.map(function (e) { return card(e, 0); }).join('') + '</div>' : '<div class="empty"><b>沒有比賽</b></div>');
  }

  function viewTrack() {
    var done = S.track.filter(function (t) { return t.result; }), wins = done.filter(function (t) { return t.result === 'win'; }).length;
    var staked = done.reduce(function (a, t) { return a + t.stake; }, 0), pnl = done.reduce(function (a, t) { return a + (t.profit || 0); }, 0);
    var clvs = S.track.filter(function (t) { return t.closeP; }).map(function (t) { return t.closeP * t.price - 1; });
    var clv = clvs.length ? clvs.reduce(function (a, b) { return a + b; }, 0) / clvs.length : null;
    var kpis = '<div class="kpis"><div class="kpi"><b>' + S.track.length + '</b><span>追蹤的注</span></div>' +
      '<div class="kpi"><b>' + wins + ' / ' + done.length + '</b><span>贏 / 已結算</span></div>' +
      '<div class="kpi"><b class="' + (pnl > 0 ? 'pos' : (pnl < 0 ? 'neg' : '')) + '">' + (pnl >= 0 ? '+' : '') + Math.round(pnl) + '</b><span>損益（元）' + (staked ? '・報酬率 ' + pct(pnl / staked) : '') + '</span></div>' +
      '<div class="kpi"><b class="' + (clv > 0 ? 'pos' : (clv < 0 ? 'neg' : '')) + '">' + pct(clv) + '</b><span>平均收盤價值 CLV</span></div></div>';
    var RES = { win: '贏', loss: '輸', push: '退款' };
    var rows = S.track.map(function (t) {
      var state = t.result ? '<span class="chip ' + (t.result === 'win' ? 'A' : (t.result === 'loss' ? 'D' : 'W')) + '">' + RES[t.result] + (t.score ? ' ' + esc(t.score) : '') + '</span>'
        : '<span class="chip W">' + (E.toMs(t.kickoff) > Date.now() ? '未開賽' : '等賽果') + '</span>';
      var c = t.closeP ? t.closeP * t.price - 1 : null;
      return '<article class="card"><div class="head"><div><div class="meta"><span class="lg">' + esc(t.league) + '</span><span>' + when(t.kickoff) + '</span><span class="muted">' + when(t.at) + ' 加入</span></div>' +
        '<div class="teams">' + esc(t.title) + '</div></div><div class="grade">' + state + '</div></div>' +
        '<div class="pickline"><span class="what">' + esc(t.bet.label) + '</span><span><span class="lbl">賠率</span><span class="big amber">' + t.price.toFixed(2) + '</span></span>' +
        '<span><span class="lbl">注金</span><span class="big">' + t.stake + '</span></span><span><span class="lbl">當時 EV</span><span class="big">' + pct(t.ev) + '</span></span>' +
        '<span><span class="lbl">CLV</span><span class="big ' + (c > 0 ? 'pos' : (c < 0 ? 'neg' : '')) + '">' + pct(c) + '</span></span>' +
        (t.result ? '<span><span class="lbl">損益</span><span class="big ' + (t.profit > 0 ? 'pos' : (t.profit < 0 ? 'neg' : '')) + '">' + (t.profit > 0 ? '+' : '') + Math.round(t.profit) + '</span></span>' : '') + '</div>' +
        '<div class="acts">' + ['win', 'loss', 'push'].map(function (r) { return '<button class="btn" data-act="settle" data-id="' + esc(t.id) + '" data-r="' + r + '">' + RES[r] + '</button>'; }).join('') +
        '<button class="btn" data-act="untrack" data-id="' + esc(t.id) + '">刪除</button></div></article>';
    }).join('');
    return '<section class="hero"><h1>追蹤與成績單</h1><p>在比賽卡按「加入追蹤」，就把當時的下法、賠率、建議注金記在這個瀏覽器裡（紙上紀錄，不會真的下注）。不讓分、讓分、大小在比賽打完後自動結算，其他玩法自己按。CLV＝開賽前最後的公允機率 × 你拿到的賠率 − 1：長期是正的，才代表拿到的價格真的比市場好。</p></section>' +
      kpis + (rows ? '<div class="cards">' + rows + '</div>' : '<div class="empty"><b>還沒有追蹤任何一注</b>有運彩價格的比賽卡上會出現「加入追蹤」。</div>');
  }

  function viewSet() {
    var s = S.settings, d = S.data || {}, cfg = d.cfg || { threshold: {} };
    function inp(label, name, value, attrs) { return '<label>' + label + '<input data-set="' + name + '" value="' + esc(value) + '" ' + (attrs || 'inputmode="decimal"') + '></label>'; }
    var tw = Object.keys(S.st.tw).map(function (sp) {
      var t = S.st.tw[sp], name = { football: '足球', mlb: '棒球', nba: '籃球' }[sp] || sp;
      var word = t.state === 'ok' ? '讀得到：' + t.games + ' 場，對上 ' + t.n + ' 場（' + clock(t.at) + '）'
        : (t.state === 'blocked' ? '這個瀏覽器讀不到（不在台灣，或運彩不讓其他網站直接讀）' : (t.state === 'noid' ? '還不知道運彩的代號' + (t.looked ? '，今天找過了沒找到' : '') : '還沒試'));
      return '<li>' + name + '：' + esc(word) + '</li>';
    }).join('');
    var b = d.budget || {};
    return '<section class="hero"><h1>設定</h1><p>所有設定、追蹤紀錄和 API key 都只存在這個瀏覽器（localStorage），不會上傳到任何地方；換一台裝置要重新設定。</p></section>' +
      '<div class="panel"><h2>資金與注金</h2><div class="form">' +
      inp('本金（元）', 'bankroll', s.bankroll) +
      '<label>Kelly 比例<select data-set="kelly">' + [[0.125, '1/8（很保守）'], [0.25, '1/4（建議）'], [0.5, '1/2'], [1, '全 Kelly（波動很大）']].map(function (o) { return '<option value="' + o[0] + '"' + (s.kelly === o[0] ? ' selected' : '') + '>' + o[1] + '</option>'; }).join('') + '</select></label>' +
      inp('單注上限（本金的 %）', 'maxPct', s.maxPct * 100) + inp('最小下注單位（元）', 'unit', s.unit) +
      '</div><p class="sub">注金 = 本金 × min(Kelly × 比例, 單注上限)，取到最小單位。只有 EV 達到門檻的注才有建議注金。</p></div>' +
      '<div class="panel"><h2>判斷標準</h2><p class="sub">可下門檻：足球 EV ≥ ' + pct(cfg.threshold.football, 0) + '、MLB ' + pct(cfg.threshold.mlb, 0) + '、NBA ' + pct(cfg.threshold.nba, 0) + '，換算的玩法 ' + pct(cfg.derived_threshold, 0) + '（和其他頁同一套，由回測決定，這裡不能改）。</p><div class="form">' +
      inp('運彩價幾分鐘內算「現在的價格」', 'priceFreshMin', s.priceFreshMin) + inp('參考盤幾秒看一次', 'poll', s.poll) + '</div></div>' +
      '<div class="panel"><h2>運彩賠率</h2><ul class="plan">' + (tw || '<li>還沒試</li>') + '</ul>' +
      '<div class="acts"><button class="btn" data-act="twretry">現在再試一次</button></div>' +
      '<p class="sub">讀不到時有三個辦法：① 照舊用 iPhone 捷徑上傳，2–3 分鐘後這一頁會自動帶入；② 在比賽卡的「分析」裡手動輸入你看到的賠率；③ 把運彩的原始檔內容貼在下面。</p>' +
      '<textarea id="twpaste" placeholder="貼上 …-Games.zh.json 的內容（JSON）"></textarea><div class="acts"><button class="btn" data-act="twpaste">套用貼上的運彩資料</button></div></div>' +
      '<div class="panel"><h2>The Odds API（自己更新共識價）</h2><p class="sub">填了 key，比賽卡的「分析」裡會多一個「買最新共識價」的按鈕。花的是和每天自動更新同一個帳戶的點數（每月 500 點）' +
      (b.remaining !== undefined ? '；自動更新最後回報本月剩 ' + esc(b.remaining) + ' 點' : '') + '。這一頁每天最多買到下面的上限，而且不會買到剩不到 ' + RESERVE + ' 點。</p><div class="form">' +
      '<label>API key<input id="oddskey" type="password" autocomplete="off" value="' + esc(S.key) + '" placeholder="貼上你的 key"></label>' +
      inp('這一頁每天最多花幾點', 'buyCap', s.buyCap) +
      '<label>每次買哪些市場<select data-set="buyMarkets"><option value="all"' + (s.buyMarkets === 'all' ? ' selected' : '') + '>該運動的主要市場（2–3 點）</option><option value="h2h"' + (s.buyMarkets === 'h2h' ? ' selected' : '') + '>只買不讓分（1 點）</option></select></label></div>' +
      '<div class="acts"><button class="btn pri" data-act="savekey">儲存並測試</button><button class="btn" data-act="clearkey">清除 key</button></div>' +
      (S.st.odds.msg ? '<p class="note">' + esc(S.st.odds.msg) + '</p>' : '') + '<p class="sub">今天這一頁已經花了 ' + spentToday() + ' 點。</p></div>' +
      '<div class="panel"><h2>清除資料</h2><div class="acts"><button class="btn" data-act="reset">' + (S.ui.armed === 'reset' ? '再按一次：清除這個瀏覽器裡的全部設定與紀錄' : '清除這個瀏覽器裡的設定與紀錄') + '</button></div></div>';
  }

  var TABS = [['top', '精選 10'], ['all', '全部比賽'], ['track', '追蹤'], ['set', '設定']];
  function render() {
    var app = document.getElementById('app'), active = document.activeElement;
    // 正在打字就不要重畫（輸入到一半的賠率會不見）。
    if (active && app.contains(active) && /INPUT|TEXTAREA|SELECT/.test(active.tagName)) return;
    var counts = { top: Math.min(10, S.ranked.length), all: S.ranked.length, track: S.track.filter(function (t) { return !t.result; }).length };
    document.getElementById('tabs').innerHTML = TABS.map(function (t) {
      return '<button role="tab" data-act="tab" data-tab="' + t[0] + '" aria-selected="' + (S.ui.tab === t[0]) + '">' + t[1] +
        (counts[t[0]] ? '<span class="count">' + counts[t[0]] + '</span>' : '') + '</button>';
    }).join('');
    var ok = S.st.espn.ok && S.data && !S.dataErr;
    document.getElementById('pulse').innerHTML = '<i class="dot ' + (ok ? 'on' : (S.st.espn.ok === false || S.dataErr ? 'off' : '')) + '"></i><span>' +
      (ok ? '即時 ' + clock(Date.now()) : (S.dataErr ? '資料讀取失敗：' + esc(S.dataErr) : '連線中…')) + '</span>';
    var html = !S.data ? '<p class="sub" style="padding:30px 0">' + (S.dataErr ? '讀不到資料（' + esc(S.dataErr) + '），稍後會自動再試。' : '載入中…') + '</p>'
      : ({ top: viewTop, all: viewAll, track: viewTrack, set: viewSet }[S.ui.tab])();
    if (html === lastHtml) return;          // 內容沒變就不重畫（每 5 秒檢查一次，大部分時候沒有變）
    lastHtml = html;
    var y = window.scrollY;
    app.innerHTML = html;
    window.scrollTo(0, y);
  }
  var lastHtml = '';
  function refresh() { evaluateAll(); render(); }

  // ------------------------------------------------------------------ 操作
  document.addEventListener('click', function (ev) {
    var el = ev.target.closest('[data-act]');
    if (!el) return;
    var act = el.getAttribute('data-act'), key = el.getAttribute('data-key');
    var armed = S.ui.armed; S.ui.armed = null;
    if (act === 'tab') { S.ui.tab = el.getAttribute('data-tab'); S.ui.msg = ''; window.scrollTo(0, 0); }
    else if (act === 'open') { if (S.ui.open[key]) delete S.ui.open[key]; else S.ui.open[key] = 1; }
    else if (act === 'filter') S.ui.filter = el.getAttribute('data-f');
    else if (act === 'track') addTrack(key);
    else if (act === 'untrack') { S.track = S.track.filter(function (t) { return t.id !== el.getAttribute('data-id'); }); save(LS.track, S.track); }
    else if (act === 'settle') {
      S.track.forEach(function (t) { if (t.id === el.getAttribute('data-id')) { t.result = el.getAttribute('data-r'); t.profit = E.profit(t.result, t.stake, t.price); } });
      save(LS.track, S.track);
    } else if (act === 'buy') {
      var code = el.getAttribute('data-code');
      if (armed === 'buy:' + code) { buy(code).then(refresh); } else S.ui.armed = 'buy:' + code;
    } else if (act === 'savekey') {
      S.key = document.getElementById('oddskey').value.trim(); save(LS.key, S.key);
      S.st.odds.msg = S.key ? '測試中…' : '沒有填 key';
      checkKey().then(render);
    } else if (act === 'clearkey') { S.key = ''; save(LS.key, ''); S.st.odds = { remaining: null, msg: 'key 已清除' }; }
    else if (act === 'twretry') { Object.keys(S.st.tw).forEach(function (s) { S.st.tw[s].tried = 0; S.st.tw[s].looked = ''; }); pollTw(true).then(refresh); }
    else if (act === 'twpaste') {
      try {
        var n = takeTw(null, JSON.parse(document.getElementById('twpaste').value), 'paste');
        S.ui.msg = '貼上的運彩資料對上 ' + n + ' 場';
      } catch (e) { S.ui.msg = '貼上的內容不是 JSON，沒有套用'; }
      S.ui.tab = 'top';
    } else if (act === 'reset') {
      if (armed === 'reset') { Object.keys(LS).forEach(function (k) { try { localStorage.removeItem(LS[k]); } catch (e) { /* 忽略 */ } }); location.reload(); return; }
      S.ui.armed = 'reset';
    }
    if (document.activeElement && document.activeElement.blur) document.activeElement.blur();
    refresh();
  });
  document.addEventListener('change', function (ev) {
    var el = ev.target;
    if (el.hasAttribute('data-bet')) {
      var key = el.getAttribute('data-key'), id = el.getAttribute('data-bet'), v = parseFloat(el.value);
      S.manual[key] = S.manual[key] || {};
      if (v > 1 && v < 1000) S.manual[key][id] = { price: v, at: Date.now() }; else delete S.manual[key][id];
      save(LS.manual, S.manual);
    } else if (el.hasAttribute('data-set')) {
      var name = el.getAttribute('data-set'), x = parseFloat(el.value);
      if (name === 'buyMarkets') S.settings.buyMarkets = el.value;
      else if (isFinite(x) && x > 0) S.settings[name] = name === 'maxPct' ? Math.min(x, 100) / 100 : x;
      save(LS.settings, S.settings);
    } else return;
    el.blur();
    refresh();
  });

  // ------------------------------------------------------------------ 排程
  var busy = false, lastData = 0, lastSettle = 0;
  function tick(force) {
    if (busy || (document.hidden && !force)) return;
    busy = true;
    var now = Date.now(), jobs = [];
    var first = !S.data;
    var p = (first || now - lastData > 60000) ? (lastData = now, loadData()) : Promise.resolve();
    p.then(function () {
      if (!S.data) return null;
      jobs.push(pollEspn(force), pollTw(force));
      if (now - lastSettle > 300000) { lastSettle = now; jobs.push(settleTracked()); }
      return Promise.all(jobs);
    }).catch(function () { /* 各來源自己記錄狀態 */ }).then(function () { busy = false; refresh(); });
  }
  document.addEventListener('visibilitychange', function () { if (!document.hidden) tick(true); });
  tick(true);
  setInterval(tick, 5000);
  window.LiveApp = { state: S, tick: tick };   // 測試與除錯用
})();
