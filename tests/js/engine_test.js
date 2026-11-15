/* Tests of docs/live/engine.js. Run by tests/test_live.py (python -m unittest / pytest)
 * when Node is installed, or directly: node tests/js/engine_test.js
 * No network: the ESPN / 運彩 samples are the JSON fixtures in tests/fixtures/. */
'use strict';
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const E = require('../../docs/live/engine.js');

const FIX = path.join(__dirname, '..', 'fixtures');
const read = (name) => JSON.parse(fs.readFileSync(path.join(FIX, name), 'utf8'));
const close = (a, b, tol, msg) => assert.ok(Math.abs(a - b) <= (tol || 1e-4), `${msg || ''} ${a} vs ${b}`);
const NOW = Date.parse('2026-11-15T12:00:00Z');
const CFG = { threshold: { football: 0.02, mlb: 0.03, nba: 0.03 }, derived_threshold: 0.04, gap_minutes: 180,
  nba_margin_sigma: 12.5, nba_total_sigma: 18.0 };
let n = 0;
function test(name, fn) { try { fn(); n++; } catch (e) { console.error('FAIL ' + name); throw e; } }

test('american odds', () => {
  close(E.americanToDecimal('-270'), 1 + 100 / 270);   // 1.3704
  close(E.americanToDecimal('+221'), 3.21);
  assert.strictEqual(E.americanToDecimal('EVEN'), 2);
  assert.strictEqual(E.americanToDecimal(400), 5);
  assert.strictEqual(E.americanToDecimal(null), null);
  assert.strictEqual(E.americanToDecimal('x'), null);
});

test('power de-vig equals models/market.py', () => {
  // python: devig([1.5, 2.8]) -> [0.6565, 0.3435]; devig([2.1, 3.4, 3.6]) -> [0.4602, 0.278, 0.2618]
  const two = E.devig([1.5, 2.8]);
  close(two[0], 0.6565); close(two[1], 0.3435);
  const three = E.devig([2.1, 3.4, 3.6]);
  close(three[0], 0.4602); close(three[1], 0.278); close(three[2], 0.2618);
  assert.strictEqual(E.devig([1.0, 2.0]), null);
});

test('consensus is the median of each book, renormalised', () => {
  // Three books with no margin: 50/50, 60/40, 70/30 -> median 60/40.
  const c = E.consensus([[2, 2], [1 / 0.6, 1 / 0.4], [1 / 0.7, 1 / 0.3]]);
  close(c.probs[0], 0.6); close(c.probs[1], 0.4);
  assert.strictEqual(c.books, 3);
  assert.strictEqual(E.consensus([[1, 2]]), null);
});

test('normal line translation equals models/margin.py', () => {
  close(E.cdf(1.96), 0.975, 1e-4);
  // python: mu = mean_from_cover(-7.5, 0.5, 12.5) = 7.5; cover_probs(mu, 12.5, -5.5) -> home 0.5636
  const a = E.shiftLine('spread', -7.5, 0.5, -5.5, 12.5);
  close(a.a, 0.5636, 2e-4); close(a.b, 0.4364, 2e-4);
  // whole line -9: home 0.4364, push 0.0317, away 0.5319 -> without the push 0.4507 / 0.5493
  const b = E.shiftLine('spread', -7.5, 0.5, -9, 12.5);
  close(b.a, 0.4364 / (1 - 0.0317), 3e-4); close(b.b, 0.5319 / (1 - 0.0317), 3e-4);
  // python: mean_from_over(224.5, 0.52, 18) -> over 220: 0.6205 / 0.3795 without the push
  const t = E.shiftLine('totals', 224.5, 0.52, 220, 18);
  close(t.a, 0.6205, 3e-4); close(t.b, 0.3795, 3e-4);
});

test('team names: same club under another spelling', () => {
  assert.strictEqual(E.norm('1. FC Köln'), 'koln');
  assert.strictEqual(E.sim('Wolverhampton Wanderers FC', 'Wolverhampton Wanderers'), 1);
  assert.ok(E.sim('FC Bayern München', 'Bayern Munich') > 0.6);
  assert.ok(E.sim('Internazionale', 'inter') >= 0.8);
  assert.ok(E.sim('Arsenal', 'Chelsea') < 0.4);
});

const espn = read('espn_odds.json');
test('ESPN scoreboard: prices as decimal odds, home handicap, totals line', () => {
  const ev = E.parseEspn(espn.nba);
  assert.strictEqual(ev.length, 2);
  const e = ev[0];
  assert.deepStrictEqual([e.id, e.home, e.away, e.state], ['401909985', 'Philadelphia 76ers', 'Indiana Pacers', 'pre']);
  close(e.odds.h2h.home, 1.3704); close(e.odds.h2h.away, 3.21);
  assert.strictEqual(e.odds.spread.line, -6.5);
  close(e.odds.spread.home, 1 + 100 / 118); close(e.odds.spread.away, 1 + 100 / 102);
  assert.strictEqual(e.odds.totals.line, 224.5);
  assert.strictEqual(e.odds.provider, 'DraftKings');
  assert.strictEqual(e.open.totals.line, 222.5);
  const s = E.parseEspn(espn.soccer)[0];
  close(s.odds.h2h.home, 1.4); close(s.odds.h2h.draw, 5); close(s.odds.h2h.away, 7);
  assert.deepStrictEqual(E.parseEspn({ events: [{ id: 1 }] }), []);
  assert.deepStrictEqual(E.parseEspn(null), []);
});

test('one book de-vigged into the consensus shape', () => {
  const e = E.parseEspn(espn.soccer)[0];
  const f = E.fairFromPrices(e.odds, 'T');
  close(f.h2h.home + f.h2h.draw + f.h2h.away, 1, 1e-9);
  assert.ok(f.h2h.home > 0.66 && f.h2h.home < 0.70);
  assert.strictEqual(f.totals.line, 3.5);
  assert.strictEqual(f.h2h.at, 'T');
});

test('events are matched by time and names, NBA by id', () => {
  const matches = [
    { key: 'football:1', sport: 'football', id: '1', kickoff: '2026-11-21T12:30:00+00:00', home: 'Manchester City FC', away: 'Fulham FC', home_key: 'manchester city', away_key: 'fulham' },
    { key: 'football:2', sport: 'football', id: '2', kickoff: '2026-11-21T15:00:00+00:00', home: 'AFC Bournemouth', away: 'Nottingham Forest FC', home_key: 'bournemouth', away_key: 'nottingham forest' },
    { key: 'football:3', sport: 'football', id: '3', kickoff: '2026-11-21T15:00:00+00:00', home: 'Arsenal FC', away: 'Chelsea FC', home_key: 'arsenal', away_key: 'chelsea' },
    { key: 'nba:401909986', sport: 'nba', id: '401909986', kickoff: '2026-11-15T21:00:00+00:00', home: 'x', away: 'y' }
  ];
  const got = E.matchEvents(matches, E.parseEspn(espn.soccer).concat(E.parseEspn(espn.nba)));
  assert.strictEqual(got['football:1'].id, '401879233');
  assert.strictEqual(got['football:2'].id, '401879238');
  assert.strictEqual(got['football:3'], undefined);
  assert.strictEqual(got['nba:401909986'].home, 'Phoenix Suns');
});

test('The Odds API response becomes the same consensus as the pipeline', () => {
  const book = (h, d, a, line, o, u) => ({ markets: [
    { key: 'h2h', outcomes: [{ name: 'A', price: h }, { name: 'B', price: a }, { name: 'Draw', price: d }] },
    { key: 'totals', outcomes: [{ name: 'Over', price: o, point: line }, { name: 'Under', price: u, point: line }] }] });
  const payload = [{ id: 'e1', commence_time: '2026-11-21T15:00:00Z', home_team: 'A', away_team: 'B',
    bookmakers: [book(2, 4, 4, 2.5, 2, 2), book(2, 4, 4, 2.5, 2, 2), book(2, 4, 4, 3.5, 3, 1.5)] }];
  const ev = E.parseOddsApi(payload, 'T', 2.5)[0];
  close(ev.fair.h2h.home, 0.5); close(ev.fair.h2h.draw, 0.25); close(ev.fair.h2h.away, 0.25);
  assert.strictEqual(ev.fair.totals.line, 2.5);
  close(ev.fair.totals.over, 0.5);
  assert.strictEqual(ev.fair.spread, null);
  assert.deepStrictEqual(E.parseOddsApi({ message: 'bad key' }, 'T'), []);
});

test('運彩 feed: price = 1 + pu/pd, own handicap from the name, 最少串關數', () => {
  const games = E.parseTw(read('tw_basketball.json'));
  assert.ok(games.length >= 1);
  const g = games[0];
  const h2h = g.markets.find((m) => m.kind === 'h2h');
  assert.ok(h2h.prices.home > 1 && h2h.prices.away > 1);
  const sp = g.markets.find((m) => m.kind === 'spread');
  assert.ok(sp && typeof sp.line === 'number');
  const raw = [{ id: 'g1', no: '7', hn: '主隊', an: '客隊', kt: '2026-11-21T15:00:00Z', si: '9', ms: [
    { name: '不讓分', m: '2', cs: [{ name: '主隊', v: 'H', pd: '100', pu: '90' }, { name: '客隊', v: 'A', pd: '100', pu: '75' }] },
    { name: '讓分 -3.5', m: '1', mv: '-3.5', cs: [{ name: '主隊 -3.5', v: 'H', pd: '4', pu: '3', hv: '-3.5' }, { name: '客隊 +3.5', v: 'A', pd: '4', pu: '3', hv: '-3.5' }] },
    { name: '讓分 1:0', m: '1', cs: [{ name: '主隊', v: 'H', pd: '1', pu: '1' }, { name: '和局', v: 'D', pd: '1', pu: '2' }, { name: '客隊', v: 'A', pd: '1', pu: '3' }] },
    { name: '[總分]大小 200.5', m: '3', mv: '200.5', cs: [{ name: '大 200.5', v: 'H', pd: '4', pu: '3' }, { name: '小 200.5', v: 'A', pd: '4', pu: '3' }] },
    { name: '角球大小 9.5', m: '1', cs: [{ name: '大', v: 'H', pd: '1', pu: '1' }, { name: '小', v: 'A', pd: '1', pu: '1' }] }] }];
  const one = E.parseTw(raw)[0];
  assert.deepStrictEqual(one.markets.map((m) => [m.kind, m.line, m.legs]), [['h2h', null, 2], ['spread', -3.5, 1], ['totals', 200.5, 3]]);
  close(one.markets[0].prices.home, 1.9); close(one.markets[1].prices.away, 1.75);
  const tw = E.twFromGame(one, 'T');
  assert.strictEqual(tw.src, 'live');
  assert.strictEqual(tw.spread[0].line, -3.5);
  assert.strictEqual(tw.legs['不讓分'], 2);
  const table = { '主隊': 'home team', '客隊': 'away team' };
  const m = [{ key: 'nba:1', kickoff: '2026-11-21T16:00:00Z', home_key: 'home team', away_key: 'away team' }];
  assert.strictEqual(E.matchTw(m, [one], table)['nba:1'].id, 'g1');
  assert.deepStrictEqual(E.matchTw(m, [one], {}), {});
});

// ---------------------------------------------------------------- evaluate / plan
function match(over) {
  return Object.assign({
    key: 'football:1', sport: 'football', id: '1', kickoff: '2026-11-16T12:00:00Z', home: 'Arsenal FC', away: 'Chelsea FC',
    home_zh: '兵工廠', away_zh: '切爾西',
    fair: { h2h: { home: 0.5, draw: 0.26, away: 0.24, line: null, at: '2026-11-15T11:50:00Z' }, totals: null, spread: null },
    tw: { at: '2026-11-15T11:55:00Z', legs: { h2h: 1 }, h2h: { home: 2.1, draw: 3.3, away: 3.6, line: null }, totals: null, spread: null, derived: [] },
    tape: { intl: [], tw: [] }
  }, over || {});
}

test('EV = fair probability x 運彩 price - 1; fresh on both sides is 可下', () => {
  const r = E.evaluate(match(), { cfg: CFG }, {}, NOW);
  const home = r.bets.find((b) => b.id === 'h2h:home');
  close(home.ev, 0.5 * 2.1 - 1);                 // +5%
  assert.strictEqual(home.status, 'value');
  assert.strictEqual(home.need, 2.04);            // 1.02 / 0.5
  assert.strictEqual(home.label, '兵工廠 不讓分');
  assert.strictEqual(home.legs, 1);
  assert.strictEqual(r.best.id, 'h2h:home');
  assert.strictEqual(r.grade, 'A');
  assert.ok(r.score >= 80);
  const draw = r.bets.find((b) => b.id === 'h2h:draw');
  close(draw.ev, 0.26 * 3.3 - 1);
  assert.strictEqual(draw.status, 'pass');
});

test('an old 運彩 price is only "confirm first"', () => {
  const m = match(); m.tw.at = '2026-11-15T08:00:00Z';
  const r = E.evaluate(m, { cfg: CFG }, {}, NOW);
  assert.strictEqual(r.best.status, 'check');
  assert.strictEqual(r.grade, 'B');
  const p = E.plan([r], {})[0].plan;
  assert.strictEqual(p.action, 'confirm');
  assert.ok(p.text[0].indexOf('2.04') > 0);
});

test('an old consensus takes the lower of consensus and the live reference', () => {
  const m = match(); m.fair.h2h.at = '2026-11-15T10:00:00Z';
  const ref = { h2h: { home: 0.47, draw: 0.27, away: 0.26 } };
  const r = E.evaluate(m, { cfg: CFG, ref: ref }, {}, NOW);
  const home = r.bets.find((b) => b.id === 'h2h:home');
  close(home.p, 0.47); close(home.ev, 0.47 * 2.1 - 1);     // -1.3%: no longer a bet
  assert.strictEqual(home.status, 'near');
  assert.ok(r.notes.length === 1);
  // A fresh consensus is trusted as it is.
  const fresh = E.evaluate(match(), { cfg: CFG, ref: ref }, {}, NOW).bets.find((b) => b.id === 'h2h:home');
  close(fresh.p, 0.5);
});

test('no 運彩 price: the price to look for, and never a bet', () => {
  const r = E.evaluate(match({ tw: null }), { cfg: CFG }, {}, NOW);
  assert.strictEqual(r.grade, 'W');
  assert.ok(r.bets.every((b) => b.status === 'noprice' && b.ev === null));
  assert.strictEqual(r.bets.find((b) => b.id === 'h2h:away').need, 4.25);   // 1.02 / 0.24
  const p = E.plan([r], {})[0].plan;
  assert.strictEqual(p.action, 'wait');
  assert.strictEqual(p.stake, 0);
  // A price typed by hand is judged like any other.
  const typed = E.evaluate(match({ tw: null }), { cfg: CFG, manual: { 'h2h:away': { price: 4.5, at: NOW } } }, {}, NOW);
  assert.strictEqual(typed.best.id, 'h2h:away');
  close(typed.best.ev, 0.08);
  assert.strictEqual(typed.grade, 'A');
});

test('NBA: a 運彩 line away from the international line is translated (4% needed)', () => {
  const m = match({ key: 'nba:9', sport: 'nba', home_zh: '', away_zh: '', home: 'Home', away: 'Away',
    fair: { h2h: { home: 0.729, away: 0.271, at: '2026-11-15T11:50:00Z' }, totals: null,
      spread: { line: -7.5, home: 0.5, away: 0.5, at: '2026-11-15T11:50:00Z' } },
    tw: null });
  const live = { at: '2026-11-15T11:58:00Z', src: 'live', legs: {}, h2h: null, totals: [], names: {},
    spread: [{ line: -5.5, legs: 1, name: '讓分 -5.5', home: 1.85, away: 1.75 }] };
  const r = E.evaluate(m, { cfg: CFG, tw: live }, {}, NOW);
  const home = r.bets.find((b) => b.id === 'spread:home:-5.5');
  close(home.p, 0.5636, 2e-4);
  close(home.ev, 0.5636 * 1.85 - 1, 5e-4);              // +4.3%
  assert.ok(home.derived);
  assert.strictEqual(home.thr, 0.04);
  assert.strictEqual(home.label, 'Home -5.5');
  assert.strictEqual(r.bets.find((b) => b.id === 'spread:away:-5.5').label, 'Away +5.5');
  assert.strictEqual(home.status, 'value');
  assert.strictEqual(home.priceSrc, 'live');
});

test('translated football markets keep the pipeline probability', () => {
  const m = match();
  m.tw.legs['[總分]大小 3.5'] = 2;
  m.tw.derived = [{ market: '[總分]大小 3.5', label: '小 3.5', key: 'under', line: 3.5, price: 1.5, p: 0.7 }];
  const d = E.evaluate(m, { cfg: CFG }, {}, NOW).bets.find((b) => b.market === 'derived');
  close(d.ev, 0.05); assert.strictEqual(d.legs, 2); assert.strictEqual(d.thr, 0.04);
});

test('stake: a quarter of Kelly, capped at 2% of the bankroll, in 100s', () => {
  // p 0.5 @ 2.10: Kelly (1.05 - 1) / 1.1 = 4.55%; a quarter = 1.14% of 20,000 = 227 -> 200.
  assert.strictEqual(E.stake(0.5, 2.1, {}), 200);
  // p 0.6 @ 2.10: Kelly 23.6%, a quarter 5.9% -> capped at 2% = 400.
  assert.strictEqual(E.stake(0.6, 2.1, {}), 400);
  assert.strictEqual(E.stake(0.45, 2.1, {}), 0);
  assert.strictEqual(E.stake(0.5, 2.02, { bankroll: 5000 }), 100);   // a small edge still bets the minimum
});

test('plan: single bet, parlay of two, and a parlay that cannot be built', () => {
  const a = E.evaluate(match(), { cfg: CFG }, {}, NOW);
  const single = E.plan([a], {})[0];
  assert.strictEqual(single.plan.action, 'bet');
  assert.strictEqual(single.plan.stake, 200);
  assert.ok(single.plan.text.some((t) => t === '單場可投'));

  const m2 = match({ key: 'football:2' }); m2.tw.legs.h2h = 2;
  const alone = E.plan([E.evaluate(m2, { cfg: CFG }, {}, NOW)], {})[0];
  assert.strictEqual(alone.plan.action, 'skip');
  assert.strictEqual(alone.grade, 'C');

  const both = E.plan([E.evaluate(m2, { cfg: CFG }, {}, NOW), E.evaluate(match(), { cfg: CFG }, {}, NOW)], {});
  const leg = both.find((e) => e.key === 'football:2');
  assert.strictEqual(leg.plan.action, 'bet');
  close(leg.plan.parlay.ev, 1.05 * 1.05 - 1);          // +10.25%
  close(leg.plan.parlay.price, 2.1 * 2.1);
  assert.strictEqual(leg.plan.parlay.with[0].key, 'football:1');
});

test('ranking: 可下 before 待確認 before 差一點 before 不下', () => {
  const stale = match({ key: 'football:b' }); stale.tw.at = '2026-11-15T06:00:00Z';
  const near = match({ key: 'football:c' }); near.tw.h2h.home = 1.96;
  const bad = match({ key: 'football:d' }); bad.tw.h2h = { home: 1.7, draw: 3.0, away: 3.2, line: null };
  const wait = match({ key: 'football:w', tw: null });
  const evals = [bad, wait, near, stale, match()].map((m) => E.evaluate(m, { cfg: CFG }, {}, NOW));
  const order = E.plan(evals, {}).map((e) => e.key + e.grade);
  assert.deepStrictEqual(order.slice(0, 3), ['football:1A', 'football:bB', 'football:cC']);
  assert.ok(order.indexOf('football:dD') > 2);
});

test('market move raises a waiting match', () => {
  const quiet = E.evaluate(match({ tw: null }), { cfg: CFG }, {}, NOW);
  const moved = E.evaluate(match({ tw: null, tape: { intl: [['t1', { home: 0.44, away: 0.3 }], ['t2', { home: 0.5, away: 0.24 }]], tw: [] } }), { cfg: CFG }, {}, NOW);
  close(moved.move.home, 0.06);
  assert.strictEqual(moved.best.id, 'h2h:home');
  assert.ok(moved.score > quiet.score);
});

test('no consensus yet: one book stands in, never more than "confirm first"', () => {
  const m = match({ fair: null });
  const ref = { h2h: { home: 0.5, draw: 0.26, away: 0.24, at: NOW } };
  const r = E.evaluate(m, { cfg: CFG, ref: ref }, {}, NOW);
  const home = r.bets.find((b) => b.id === 'h2h:home');
  close(home.ev, 0.05);
  assert.ok(home.single);
  assert.strictEqual(home.status, 'check');
  assert.strictEqual(r.grade, 'B');
  assert.deepStrictEqual(E.evaluate(m, { cfg: CFG }, {}, NOW).bets, []);
});

test('football: the two-way handicap is never listed (運彩 only has the three-way one)', () => {
  const m = match();
  m.fair.spread = { line: -0.5, home: 0.5, away: 0.5, at: '2026-11-15T11:50:00Z' };
  const ref = { h2h: { home: 0.5, draw: 0.26, away: 0.24 }, spread: { line: -0.5, home: 0.5, away: 0.5 } };
  const r = E.evaluate(m, { cfg: CFG, ref: ref }, {}, NOW);
  assert.ok(r.bets.every((b) => b.market !== 'spread'));
});

test('settlement', () => {
  assert.strictEqual(E.settle({ market: 'h2h', side: 'draw' }, 1, 1), 'win');
  assert.strictEqual(E.settle({ market: 'h2h', side: 'home' }, 1, 2), 'loss');
  assert.strictEqual(E.settle({ market: 'totals', side: 'over', line: 2.5 }, 2, 1), 'win');
  assert.strictEqual(E.settle({ market: 'totals', side: 'under', line: 3 }, 2, 1), 'push');
  assert.strictEqual(E.settle({ market: 'spread', side: 'home', line: -7.5 }, 110, 100), 'win');
  assert.strictEqual(E.settle({ market: 'spread', side: 'away', line: -7.5 }, 105, 100), 'win');
  assert.strictEqual(E.settle({ market: 'spread', side: 'home', line: -5 }, 105, 100), 'push');
  assert.strictEqual(E.settle({ market: 'derived', side: 'btts_yes' }, 1, 1), null);
  close(E.profit('win', 200, 2.1), 220);
  assert.strictEqual(E.profit('loss', 200, 2.1), -200);
  assert.strictEqual(E.profit('push', 200, 2.1), 0);
});

console.log('engine.js: ' + n + ' tests passed');
