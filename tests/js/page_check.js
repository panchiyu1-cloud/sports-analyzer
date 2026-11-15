/* A browser check of docs/live/ with every outside source mocked (no network).
 * Not part of CI (needs Playwright + Chromium): node tests/js/page_check.js [screenshot-dir]
 * It loads the real index.html / engine.js / app.js against a made-up data.json, ESPN
 * scoreboard, 運彩 feed and The Odds API answer, and checks what the owner would see. */
'use strict';
const assert = require('assert');
const fs = require('fs');
const http = require('http');
const path = require('path');
const { chromium } = require('playwright');

const DOCS = path.join(__dirname, '..', '..', 'docs');
const SHOTS = process.argv[2] || null;
const now = Date.now();
const iso = (ms) => new Date(ms).toISOString();
const K1 = now + 5 * 3600e3, K2 = now + 6 * 3600e3, K3 = now + 20 * 3600e3;
const SECRET = 'test-key-not-real-0123456789';

const data = {
  generated_at: iso(now - 5 * 60e3),
  cfg: { threshold: { football: 0.02, mlb: 0.03, nba: 0.03 }, derived_threshold: 0.04, gap_minutes: 180, pregame_hours: 3, nba_margin_sigma: 12.5, nba_total_sigma: 18 },
  budget: { remaining: 288, used: 212, period_start: '2026-11-01' },
  leagues: [
    { code: 'PL', name: '英超', sport: 'football', espn: 'soccer/eng.1', odds_key: 'soccer_epl' },
    { code: 'NBA', name: 'NBA', sport: 'nba', espn: 'basketball/nba', odds_key: 'basketball_nba' }],
  tw_feeds: { football: '34740.1', mlb: '34731.1', nba: null },
  zh: { '曼城': 'manchester city', '富勒姆': 'fulham', '伯恩茅斯': 'bournemouth', '諾丁漢森林': 'nottingham forest', '費城76人': 'philadelphia 76ers', '印第安納溜馬': 'indiana pacers' },
  matches: [
    { key: 'football:1', sport: 'football', id: '1', league: 'PL', league_name: '英超', kickoff: iso(K1), home: 'Manchester City FC', away: 'Fulham FC',
      home_key: 'manchester city', away_key: 'fulham', home_zh: '曼城', away_zh: '富勒姆', page: 'value.html#m-football-1',
      fair: { books: 20, h2h: { home: 0.70, draw: 0.18, away: 0.12, line: null, at: iso(now - 10 * 60e3), stale: false },
        totals: { over: 0.45, under: 0.55, line: 3.5, at: iso(now - 10 * 60e3), stale: false }, spread: null },
      tw: { at: iso(now - 4 * 3600e3), id: 'g1', no: '101', legs: { h2h: 1, totals: 2 }, h2h: { home: 1.35, draw: 4.6, away: 7.5, line: null },
        totals: { over: 2.0, under: 1.65, line: 3.5 }, spread: null, derived: [] },
      tape: { intl: [[iso(now - 9 * 3600e3), { home: 0.66, draw: 0.2, away: 0.14 }], [iso(now - 10 * 60e3), { home: 0.70, draw: 0.18, away: 0.12 }]], tw: [[iso(now - 4 * 3600e3), { home: 0.69, draw: 0.19, away: 0.12 }]] } },
    { key: 'football:2', sport: 'football', id: '2', league: 'PL', league_name: '英超', kickoff: iso(K2), home: 'AFC Bournemouth', away: 'Nottingham Forest FC',
      home_key: 'bournemouth', away_key: 'nottingham forest', home_zh: '', away_zh: '', page: 'model.html#m-football-2',
      fair: { books: 20, h2h: { home: 0.45, draw: 0.27, away: 0.28, line: null, at: iso(now - 10 * 60e3), stale: false }, totals: null, spread: null },
      tw: null, tape: { intl: [], tw: [] } },
    { key: 'nba:401909985', sport: 'nba', id: '401909985', league: 'NBA', league_name: 'NBA', kickoff: iso(K3), home: 'Philadelphia 76ers', away: 'Indiana Pacers',
      home_key: 'philadelphia 76ers', away_key: 'indiana pacers', home_zh: '', away_zh: '', page: 'nba.html#m-nba-401909985',
      fair: { books: 24, h2h: { home: 0.729, away: 0.271, line: null, at: iso(now - 10 * 60e3), stale: false },
        totals: { over: 0.5, under: 0.5, line: 224.5, at: iso(now - 10 * 60e3), stale: false }, spread: { home: 0.5, away: 0.5, line: -7.5, at: iso(now - 10 * 60e3), stale: false } },
      tw: null, tape: { intl: [], tw: [] } }]
};

const espnFix = JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'fixtures', 'espn_odds.json'), 'utf8'));
espnFix.soccer.events[0].date = iso(K1); espnFix.soccer.events[1].date = iso(K2); espnFix.nba.events[0].date = iso(K3);
// 運彩 live: Fulham now pays 9.5 (fair 0.12 -> EV +14%), single bet allowed.
const twFeed = [{ id: 'g1', no: '101', hn: '曼城', an: '富勒姆', kt: iso(K1), si: '34740.1', tn: '英格蘭超級聯賽', ms: [
  { name: '不讓分', m: '1', cs: [{ name: '曼城', v: 'H', pd: '100', pu: '35' }, { name: '和局', v: 'D', pd: '10', pu: '36' }, { name: '富勒姆', v: 'A', pd: '10', pu: '85' }] }] }];
const oddsApi = [{ id: 'o2', commence_time: iso(K2), home_team: 'Bournemouth', away_team: 'Nottingham Forest', bookmakers: [1, 2, 3].map(() => ({ markets: [
  { key: 'h2h', outcomes: [{ name: 'Bournemouth', price: 2.0 }, { name: 'Nottingham Forest', price: 4.0 }, { name: 'Draw', price: 4.0 }] }] })) }];

const TYPES = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.json': 'application/json' };
const server = http.createServer((req, res) => {
  const file = path.join(DOCS, decodeURIComponent(req.url.split('?')[0]).replace(/\/$/, '/index.html'));
  if (!file.startsWith(DOCS) || !fs.existsSync(file)) { res.writeHead(404); res.end(); return; }
  res.writeHead(200, { 'content-type': TYPES[path.extname(file)] || 'text/plain' });
  res.end(fs.readFileSync(file));
});

(async () => {
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  const base = 'http://127.0.0.1:' + server.address().port + '/live/';
  const browser = await chromium.launch();
  const seen = { tw: 0, odds: [], other: [] };
  let twOpen = false;
  async function open(viewport) {
    const ctx = await browser.newContext({ viewport, deviceScaleFactor: 1 });
    const page = await ctx.newPage();
    const errors = [];
    page.on('pageerror', (e) => errors.push(String(e)));
    page.on('console', (m) => { if (m.type() === 'error' && !/Failed to load resource|net::ERR/.test(m.text())) errors.push(m.text()); });
    await page.route('**/*', (route) => {
      const url = route.request().url();
      const json = (body, headers) => route.fulfill({ status: 200, contentType: 'application/json', headers: Object.assign({ 'access-control-allow-origin': '*', 'access-control-expose-headers': '*' }, headers || {}), body: JSON.stringify(body) });
      if (url.startsWith(base)) return /data\.json/.test(url) ? json(data) : route.continue();
      if (/fonts\.g/.test(url)) return route.abort();
      if (/site\.api\.espn\.com.*soccer\/eng\.1/.test(url)) return json(espnFix.soccer);
      if (/site\.api\.espn\.com.*basketball\/nba/.test(url)) return json(espnFix.nba);
      if (/sportslottery/.test(url)) { seen.tw++; return twOpen && /34740\.1/.test(url) ? json(twFeed) : route.abort(); }
      if (/the-odds-api\.com\/v4\/sports\?/.test(url)) { seen.odds.push(url); return json([], { 'x-requests-remaining': '288' }); }
      if (/the-odds-api\.com/.test(url)) { seen.odds.push(url); return json(oddsApi, { 'x-requests-remaining': '287' }); }
      seen.other.push(url); return route.abort();
    });
    await page.goto(base);
    await page.waitForSelector('.card');
    return { page, ctx, errors };
  }

  // ---- 1. 運彩 blocked: the upload's old price is "confirm first", never 可下
  let { page, ctx, errors } = await open({ width: 390, height: 844 });
  await page.waitForFunction(() => document.querySelectorAll('.cell .now').length >= 6 && /即時/.test(document.getElementById('pulse').textContent));
  let cards = await page.$$eval('.card', (els) => els.map((e) => ({ grade: e.dataset.grade, text: e.innerText })));
  assert.strictEqual(cards.length, 3);
  assert.ok(cards.every((c) => c.grade !== 'A'), 'no 可下 from a four-hour-old 運彩 price');
  assert.ok(/現在沒有達到門檻的比賽/.test(await page.textContent('.hero h1')));
  assert.ok(cards.some((c) => /等運彩價/.test(c.text) && /≥/.test(c.text)), 'a waiting card names the price to look for');
  assert.ok(/1\.40/.test(cards.find((c) => /曼城/.test(c.text)).text), 'live reference price (DraftKings -250 = 1.40) is shown');
  if (SHOTS) await page.screenshot({ path: path.join(SHOTS, 'live-mobile-blocked.png'), fullPage: true });

  // ---- 2. a price typed by hand is judged at once
  const bour = page.locator('.card', { hasText: 'Bournemouth' });
  await bour.locator('[data-act="open"]').click();
  const input = bour.locator('input[data-bet="h2h:home"]');
  await input.fill('2.40');
  await input.press('Tab');
  await page.waitForFunction(() => [...document.querySelectorAll('.card')].some((c) => c.dataset.grade === 'A'));
  const typed = await page.locator('.card[data-grade="A"]').innerText();
  assert.ok(/AFC Bournemouth 不讓分/.test(typed) && /\+8\.0%/.test(typed) && /手動/.test(typed), typed);   // 0.45 x 2.40 - 1
  assert.ok(/建議注金/.test(typed));
  assert.ok(/現在可下：1 場/.test(await page.textContent('.hero h1')));
  await page.locator('.card[data-grade="A"] [data-act="track"]').click();
  await page.click('[data-tab="track"]');
  assert.ok(/AFC Bournemouth 不讓分/.test(await page.textContent('#app')));
  if (SHOTS) await page.screenshot({ path: path.join(SHOTS, 'live-mobile-track.png'), fullPage: true });

  // ---- 3. The Odds API key stays in the browser; buying needs two presses
  await page.click('[data-tab="set"]');
  await page.fill('#oddskey', SECRET);
  await page.click('[data-act="savekey"]');
  await page.waitForFunction(() => /本月剩 288 點/.test(document.getElementById('app').textContent));
  await page.click('[data-tab="all"]');
  const b2 = page.locator('.card', { hasText: 'Bournemouth' });
  await b2.locator('[data-act="buy"]').click();
  assert.strictEqual(seen.odds.length, 1, 'one press buys nothing');
  await page.locator('.card', { hasText: 'Bournemouth' }).locator('[data-act="buy"]').click();
  await page.waitForFunction(() => /更新了 1 場的共識價/.test(document.getElementById('app').textContent));
  assert.ok(/soccer_epl\/odds\?.*markets=h2h,totals/.test(seen.odds[1]));
  const html = await page.content();
  assert.ok(html.indexOf(SECRET) < 0 || (await page.$('#oddskey')) !== null, 'the key is only ever in its own field');
  assert.ok(!/test-key/.test(await page.innerText('body')), 'the key is never printed on the page');
  // bought consensus: home 0.5 -> the typed 2.40 is now +20%
  assert.ok(/\+20\.0%/.test(await page.locator('.card', { hasText: 'Bournemouth' }).innerText()));
  assert.deepStrictEqual(errors, []);
  if (SHOTS) await page.screenshot({ path: path.join(SHOTS, 'live-mobile-all.png'), fullPage: true });
  await ctx.close();

  // ---- 4. 運彩 readable from the browser: a live price above the fair price is 可下
  twOpen = true;
  ({ page, ctx, errors } = await open({ width: 1200, height: 900 }));
  await page.waitForFunction(() => [...document.querySelectorAll('.card')].some((c) => c.dataset.grade === 'A'), null, { timeout: 20000 });
  const top = await page.locator('.card').first().innerText();
  assert.ok(/富勒姆 不讓分/.test(top) && /9\.50/.test(top) && /\+14\.0%/.test(top) && /單場可投/.test(top), top);   // 0.12 x 9.5 - 1
  assert.ok(/運彩 即時讀取 1 場/.test(await page.textContent('.srcs')));
  assert.deepStrictEqual(errors, []);
  if (SHOTS) await page.screenshot({ path: path.join(SHOTS, 'live-desktop-value.png'), fullPage: true });
  await ctx.close();

  assert.deepStrictEqual(seen.other, [], 'no request to anything unexpected');
  await browser.close();
  server.close();
  console.log('live page: all checks passed');
})().catch((e) => { console.error(e); process.exit(1); });
