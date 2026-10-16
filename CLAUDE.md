# CLAUDE.md — sports-analyzer

Guidance for Claude Code (and any other agent) working in this repository.
Rules below are distilled from the ECC (`affaan-m/ECC`) `rules/common` and
`rules/python` sets, trimmed to what matters for this project.

## What this project is

A daily pipeline that
1. fetches fixtures/results (football-data.org, MLB Stats API) and odds (The Odds API),
2. fits a model per competition (football: Dixon-Coles; MLB: log5 + starting pitcher),
3. compares model probabilities with de-vigged market probabilities,
4. writes a JSON snapshot + appends signals to `data/ledger.csv`,
5. renders the site in `docs/`: a front page and one page per analysis method (GitHub Pages).

It runs on GitHub Actions (`.github/workflows/daily.yml`). The owner is not a
professional developer: keep code plain, well-named, and commented where a
formula is not obvious.

## Non-negotiable rules

### Security
- NEVER hardcode secrets. Tokens come only from environment variables
  (`FOOTBALL_DATA_TOKEN`, `ODDS_API_KEY`, optional `ODDS_API_IO_KEY`, `NTFY_TOPIC`) via
  `sports_analyzer.config.Settings`. An exception's text can carry a request URL with
  its key: log `type(exc).__name__`, not the exception.
- Never print or log a token, even partially. Never write tokens into
  snapshots, ledgers, HTML, or test fixtures.
- Fail fast at startup if a required token is missing (unless `--offline`).
- Treat API responses as untrusted data: validate shape before use, never
  `eval`, never render API strings into HTML without autoescaping.

### API quotas (hard limits of the free tiers)
- football-data.org: 10 requests/minute. Always go through
  `sports_analyzer.http.Session`, which reads `X-Requests-Available-Minute`
  and `X-RequestCounter-Reset` and sleeps instead of hammering.
- The Odds API: 500 credits/month, cost = markets × regions per request.
  Only request odds for competitions that have fixtures inside the window.
  Log `x-requests-remaining` after every call.
- Every odds purchase goes through `budget.plan_markets()` (data/odds_budget.json):
  an even daily share of the credits left (x1.6 burst, 25 kept in reserve) over the days
  to the next 1st of the month (The Odds API resets every plan's credits on the 1st, UTC;
  nothing carries over); short of it a run buys h2h only, or nothing (prices carried
  over, marked stale). Football buys h2h,totals only (運彩 football handicaps are
  three-way, so Asian handicap prices compare with nothing); MLB keeps the run line.
- Pre-game refreshes (`pipeline tw` after a recent upload, see "T-10 價值版") and the
  延遲雷達 purchases (`pipeline radar`, essential markets only; triggered by the 盯盤
  sensor) may spend `budget.PREGAME_EXTRA` (6) credits a day past that allowance, never
  the reserve. The evening run buys match-winner prices only (football h2h, MLB h2h +
  run line); the morning's totals stay, marked stale. It covers the morning's two days
  (`--days 2`): the pages and the sensor read the newest snapshot, and a one-day window
  dropped the second day's matches every evening (credits go per competition, not match).
- odds-api.io (the owner's free key, `ODDS_API_IO_KEY`): 100 requests/hour, 500/day
  (when its day starts is not documented: `sensor.py` counts any 24 hours), two
  bookmakers. Free keys are "for development and testing" and its data may not be
  redistributed: it never goes into the repo, the pages or the logs (see "盯盤").
  One free account per person: never suggest a second one.

### Code style (ECC python rules)
- PEP 8, type annotations on every function signature, `from __future__ import annotations`.
- Prefer immutable data: `@dataclass(frozen=True)` / `NamedTuple` for records.
- Small pure functions for maths (`models/`), I/O only in `fetch/` and `pipeline.py`.
- No emojis in code. Comments explain *why*, not *what*.
- Format with `ruff format`, lint with `ruff check` before committing.

### Testing (ECC testing rules)
- Tests live in `tests/`, named `test_*.py`, runnable with plain
  `python -m unittest discover -s tests` AND with `pytest`.
- No network in tests. API parsing is tested against the JSON fixtures in
  `tests/fixtures/`. Models are tested on synthetic data with known answers.
- Every bug fix gets a regression test. New maths gets a test with a
  hand-checked number.

### Git workflow
- Conventional commits: `feat:`, `fix:`, `docs:`, `test:`, `chore:`.
- Generated files that ARE committed: `docs/*.html` (every task renders every page),
  `data/snapshots/**`, `data/ledger.csv`,
  `data/tw_ledger.csv`, `data/reviews/*.csv`, `data/history/*.csv`, `data/moves/*.csv`,
  `data/fade_summary.json`, `data/combo_summary.json`. Raw dumps under `data/raw/` are not.
- Workflows commit through `scripts/commit_generated.sh` (handles two runs racing).

## Commands

```bash
python -m unittest discover -s tests -v          # tests (no extra deps)
pytest --cov=src                                 # same, with coverage
ruff check . && ruff format .                    # lint + format
python -m sports_analyzer.pipeline football      # daily football run (needs .env)
python -m sports_analyzer.pipeline football --offline   # render from last snapshot
python -m sports_analyzer.pipeline review         # post-match review (football-data token only)
python -m sports_analyzer.pipeline sensor         # one 盯盤 round (ODDS_API_IO_KEY; state in .sensor/)
python -m sports_analyzer.pipeline radar --only PL,MLB  # buy the consensus for these competitions
python scripts/backtest_football.py data/history/*.csv  # backtest + docs/backtest.html
python scripts/validate_implied.py                      # validate + recalibrate the implied model
python scripts/backtest_fade.py --model-records records.json  # 反者道之動 (needs data/raw/)
python scripts/backtest_combos.py                       # 組合篩選 (needs data/raw/)
```

## Layout

```
src/sports_analyzer/
  config.py        competitions, sport keys, thresholds, Settings (env)
  http.py          shared requests session: retries, header-based throttling
  teams.py         team-name normalisation + alias table across data sources
  fetch/           football.py (football-data.org), odds.py (The Odds API), mlb.py,
                   oddsio.py (odds-api.io: the 盯盤 sensor's two bookmakers)
  models/          dixon_coles.py, market.py (de-vig, edge, T-10 G value),
                   implied.py (market-implied goal model + per-market calibration)
  analysis/        football.py — fixtures + model + odds -> card dicts + signals
                   review.py — finished match: pre-match card vs result, verdict, wording
  review_store.py  data/reviews/{football,mlb}.csv (one row per finished match)
  ledger.py        append / settle signals in data/ledger.csv
  moves.py         line-move tape data/moves/YYYY-MM.csv + 大戶 record data/moves/events.csv
                   + 延遲雷達 (radar, radar_events)
  render/          site.py — Site (everything a render needs) -> one view per page -> docs/*.html
                   cards.py — shared helpers (names, times, labels, 大戶 record, review cards)
                   templates/ base + _style (look), _ui (macros), one .html.j2 per page
  sensor.py        盯盤: what to read when, the quota, when a move asks for a purchase
  pipeline.py      CLI entry point
scripts/backtest_football.py   rolling backtest on football-data.co.uk CSVs
scripts/backtest_fade.py       65 strategies vs the market (反者道之動) -> data/fade_summary.json
scripts/backtest_combos.py     combinations of them (組合篩選) -> data/combo_summary.json
```

## Model notes (so nobody "simplifies" them away)
- Dixon-Coles: Poisson goals with attack/defence per team, home advantage,
  rho correction for 0-0 / 1-0 / 0-1 / 1-1, exponential time decay (xi per day).
- De-vig: default "power" method; "multiplicative" kept for comparison.
- Blend (`models/blend.py`): multinomial logit of log(model) and log(market)
  with ridge towards "market only". Fitted walk-forward in the backtest.
- T-10 G value (owner's baseball method): G = p + c - 1 with p = de-vigged
  favourite ML prob and c = de-vigged favourite -1.5 run-line prob;
  thresholds +/-6 percentage points. Lives in `models/market.py`. Since 2026-10-14 it
  only names the game's shape; the bet decision is EV (see "T-10 價值版").

## Backtest verdict (2026-10-13) — read before touching thresholds
- The backtest is two-stage walk-forward with closing-line value (CLV). CLV is
  the acceptance test, not ROI: a strategy passes only if CLV - 2se > 0.
- Result: raw model CLV -4.7%; blend learns a NEGATIVE model weight (a ~ -0.19);
  blend log-loss == pre-match market, worse than closing; CLV negative at every
  threshold and league. `signals_validated` stays False. Do not re-enable
  recommendations because a ROI number looks good — check `data/backtest_summary.json["passed"]`.
- What the page is for now: fair odds (1 / de-vigged consensus prob) so the
  owner can compare against the price he can actually get (台灣運彩).
- 模型分歧 is never emitted for cups (`cup_signals=False`: the pooled CL model cannot
  compare leagues) or when a team has fewer than `min_signal_matches` (20) fitted
  matches (promoted teams); the card says what was held back and why.

## 運彩 feed (Phase 4)
- `blob3rd.sportslottery.com.tw/apidata/Pre/<sport>-Games.zh.json` is geo-blocked
  outside Taiwan (CloudFront 403 from GitHub runners). The owner's iPhone
  Shortcut uploads the raw file to `data/tw/<football|baseball>/<YYYY-MM-DD-HHmm>.json`;
  never try to fetch it from Actions. Sport ids: football 34740.1, baseball 34731.1.
- `fetch/taiwan.py` parses (price = 1 + pu/pd), `analysis/taiwan.py` compares
  with the consensus (EV = fair_prob x price - 1), `tw_ledger.py` records value
  bets, CLV (from a later consensus; stale carried-over blocks never count) and
  settlement. Unknown Chinese names land in the snapshot's `tw_report.unknown_teams` (the
  value page lists them: "那些比賽暫時不能比價"): add them to `TW_ALIASES`, do not
  fuzzy-match Chinese. 運彩 spells some teams its own way (紐卡索聯, 文達不來梅, 沙爾克04,
  佛倫提那, 昂傑 ...); check the latest upload's report after each matchday.
- Workflows: `tw.yml` on push to data/tw/** (has ODDS_API_KEY for the pre-game
  refresh); `evening.yml` 12:25 UTC refresh for CLV; `daily.yml` runs the tw step too.
- A value bet needs a fair price bought within `max_price_gap_minutes` (180) of the
  運彩 capture (`analysis/taiwan.is_fresh`; consensus blocks carry `odds_at`, older
  snapshots are backfilled from `generated_at`, carried-over "stale" blocks never
  count). Otherwise EVs are shown with a "不判斷" note and nothing is recorded.
- 運彩 run lines: `hv`/`mv` are the HOME handicap on both selections; each team's own
  handicap is read from its name ("道奇 -1.5" / "釀酒人 +1.5").

## 賽後檢討 (post-match review)
- `pipeline review` (daily + evening + manual `review.yml`, and `--new-only` from sensor.yml's
  act job as soon as a match should have finished: `review_due`, football 2 h / MLB 2.5 h after
  kickoff, asked each round until the result is in, at most 6 h / 9 h after kickoff; owner,
  2026-10-16: "比賽結束就立刻檢討"; a `--new-only` run that finds nothing final renders
  nothing, `review_changed`, so a round waiting for a result leaves nothing to commit) joins the last pre-kickoff
  card from the snapshots of the last 14 days with the result (football-data.org /
  MLB Stats API box score) and football-data.co.uk statistics (HxG/AxG, shots, cards;
  domestic leagues only, 2-3 days late; `scripts/refresh_history.sh` refreshes them daily).
- Keep outcome and process apart: per-match log-loss says who was closer; the verdict
  ("判斷正確 / 不走運 / 判斷錯誤 ...") uses only evidence independent of the score:
  pre-kickoff market move on the lean's side and the xG / shots-on-target picture.
- The lean is the 模型分歧 ledger row if any, else the 1X2 side where the FIRST card's
  model sat >= 5pp above the opening market (the last card would hide leans the market
  later moved toward).
- CSV rows hold facts; `explain_*` derives the wording at render time, so wording changes
  re-explain old matches. Thresholds are constants at the top of `analysis/review.py`.
- Quarter-line handicaps settle half win / half loss (`ledger.grade`).
- Every recommendation is reviewed on its match's card ("這場的推薦"): each 模型分歧 ledger
  row, each 運彩 value bet (tw_ledger) and the MLB T-10 pick (`cards.match_picks` joins
  them at render time; `review.explain_pick` words them). Result apart from process:
  value bets by CLV (closing_fair_prob x 運彩 price - 1, `CLV_MIN` 1%), signals by the
  market move from market_p to close_p (the card's own lean signal reuses the card's
  move, so the two verdicts never disagree), football also by xG / shots on target.
- `run_review` also settles both ledgers for the matches it reviews and fills ledger
  `close_p`/`clv` (`_close_signals`; `review.closing_prob`: the fresh direct block, else
  the implied model for handicap/totals lines). `tw.yml` runs `review --new-only` after
  every upload (only matches without a review row: no request when nothing finished),
  so pressing the phone shortcut is also "review now".
- `tw_ledger.update_clv` never uses prices bought at or after kickoff (the evening run
  prices the day from 00:00 UTC, while early kick-offs are being played): the h2h
  block's `odds_at`, else the run's time, must be before kickoff.
- Snapshots must keep a 運彩 block once captured: a later upload no longer lists a match
  that has kicked off (`analysis/taiwan.annotate`).

## Market-implied pricing of 運彩's other markets
- `models/implied.fit_implied` finds (lambda, mu, rho) whose Dixon-Coles score grid has
  exactly the market's fair 1X2 and main totals line; it then prices totals at other
  lines, team totals, both teams to score, odd/even, 讓分 a:b (three-way handicap)
  and first-half 1X2/totals. It translates the market; it never replaces it.
- Validated on 3,689 matches (2024-2027, closing Pinnacle/Betfair): Asian handicap MAE
  0.74pp vs the closing price; out-of-sample mean bias <=0.5pp after the per-market
  logistic calibration in data/implied_calibration.json (kept only where it beat the
  raw price out of sample). First-half share of goals 0.44. Rerun weekly by backtest.yml.
- One-goal 三向讓分 sides that are plain 1X2 outcomes stay exactly at the market price.
- Derived value bets need EV >= 4% (first half 5%) vs 2% for direct markets.

## T-10 backtest (2026-10-14) — read before changing the baseball signal
- scripts/backtest_t10.py on 10,522 MLB games 2021-04 .. 2025-08 (SportsBookReview closing
  odds of six US books via ArnavSaraogi/mlb-odds-scraper; flattened CSV on the `t10-data`
  branch, built by the manual "T-10 backtest data" workflow; raw data never on main).
- The dataset's "closing" lines include in-play prices (~3% of book rows; closes >10pp
  from the opening consensus moved toward the eventual winner 70% of the time).
  `analysis/t10_backtest.trusted_books` keeps a book only within 4pp of the cross-book
  median close and 10pp of the opening median; unverifiable games (444) are dropped.
  Any new study on this data must use it, or "best price" and hit rates leak the result.
- G >= +6pp favourite ML: win rate 1.8pp BELOW the closing market (z -2.3), ROI -5.5% at
  the book's close (every favourite: -5.2%), -3.9% at the best of six books. G <= -6pp
  underdog +1.5: +0.65pp vs market (z +0.8; +2.0 with multiplicative de-vig), ROI -2.1%
  (best price -0.6%), negative in 4 of 5 seasons, CLV negative when bet at the open. At
  運彩's ~15% margin both lose 12-15%. No threshold from 2 to 10pp and no G decile beats
  the market.
- Walk-forward logistic tests (out-of-sample log-loss, consensus prices): the run line adds
  nothing to the moneyline for the winner, the moneyline and total add nothing to the run
  line for the cover, and the open->close move adds nothing to the close. Price-only
  overlays have nothing left to find; an edge needs a price 運彩 sets worse than the
  international fair price (EV), not a pattern in one book's prices.
- Verdict: not passed (data/t10_backtest_summary.json["passed"] is false). The prompt's
  Switch zones / price guard / overlays were not tested (rules not written down).

## T-10 價值版 (2026-10-14) — the MLB decision rule since the backtest
- `analysis/mlb.t10_value_block` (card["t10v"]): the four 運彩 bets (favourite / underdog x
  moneyline / run line) against the international consensus, EV = fair x 運彩 price - 1.
  "可下" (status value) needs EV >= `mlb_value_threshold` (3%) on a fresh comparison;
  "不下" (pass) otherwise; "不判斷" (stale) when the fair price is > 3h from the 運彩 price.
  MLB 運彩 values are recorded in tw_ledger at the same 3% (run_tw passes a cfg copy).
- G (international) only describes the game (`G_SHAPES`: 強隊大勝型 / 一分差型 / 中性).
  G from 運彩's own prices (same de-vig) minus it, split into dp (moneyline) and dc
  (run line), becomes a hint: the side 運彩 underrates is the cheaper one. Hints are
  pointers; the EVs decide.
- Pre-game refresh (`analysis/pregame.py`, `pipeline._pregame_refresh`): when `run_tw` sees
  an upload at most 90 minutes old, cards starting within `pregame_window_hours` (3) whose
  h2h is older than `pregame_min_age_minutes` (20), carried over or missing get fresh
  odds (one request per sport key) before the comparison; pending 運彩 bets get their
  CLV from them first, so no bet is its own close.
- The newest MLB snapshot holds the next US date: `run_tw` and the page (`_mlb_for_page`)
  also use the unstarted games of the snapshot before it, and write both back.
- MLB review rows record the last pre-game t10v; only a value bet is graded (its own
  team + handicap, units at 運彩's price); "不下" is never judged on the score.

## 大手追蹤 (2026-10-14) — the line-move tape
- Study (scratch, 22 leagues 2019-2027 from the `odds-history` branch built by the manual
  "Football odds history" workflow; MLB from `t10-data`): early->close moves carry real
  information (closing log-loss better in every tier; 2.4x stronger in the smaller
  countries' top flights), sides steamed >= 8pp opened at 43%, closed at 53%, won 55%. At
  the close almost nothing is left (pre-registered signals did not hold on the 2024/25+
  hold-out; MLB nothing). Only a price that has not moved yet pays: moves >= 8pp backed
  at a stale 運彩-like price returned about +7%, 5-8pp did not beat 運彩's margin.
- `moves.py` + data/moves/YYYY-MM.csv (generated, committed): every international fair
  price (run_football, run_mlb, pre-game refresh; stale blocks skipped) and every 運彩
  price with its own de-vigged probability (run_tw), deduplicated on time + key.
  `scripts/backfill_moves.py` rebuilds it from the snapshot history.
- `moves.track`: first-seen vs latest international probability per side (大單 at
  `big_move_threshold`, 8pp) and 運彩's first/last price with `follow` = 運彩's move over
  the international move across 運彩's own span (prices within the freshness window).
- `moves.tw_divergences`: between two 運彩 uploads, 運彩 moved a side >= 2pp more than the
  market did (international prices needed near both uploads): 運彩's own big bettors.
  Uploads with no international price within the window are skipped, not compared (a
  mid-day manual upload must not hide the 08:00 -> 20:00 comparison). Settled at render
  time from the review CSVs; the page compares hits with the international probability
  at the time (z > 2 before believing anything).
- 大戶紀錄 (owner's request: keep every match where a move was caught, even if it never
  became 可下): `pipeline._record_bighand` runs in every render (every task ends with
  one) and appends new `tw_divergences` + `moves.intl_big_moves` (first time a side stood
  >= 8pp above our first price, before kickoff) to data/moves/events.csv, with the
  competition and 運彩's Chinese names from the snapshots (lost later). One row per
  detection (`event_id`: a 運彩 move per match/market/upload whatever side tops later; a
  大單 per side); never edited or pruned, a header change rewrites the file whole.
  `moves.event_history` joins it with the whole tape (prices up to kickoff, MLB G from
  ML + ±1.5 bought together), tw_ledger value bets and results -> 盤口移動's 歷史數據
  (moves.html#history; radar events on radar.html; docs/bighand.html redirects there).
  "鎖定區" is shown both ways: 可下 (a tw_ledger row) and MLB G reaching ±6 (the old T-10
  trigger).
- Freshness (`analysis/taiwan.is_fresh`) goes by a block's own `odds_at`, also for
  carried-over "stale" blocks; only an unstamped carried block is never fresh.
- `notify.py`: new 運彩 value bets go to the phone through ntfy.sh when the NTFY_TOPIC
  secret is set (JSON API; the topic is a secret: never log it). One message per bet id.
  Nothing else is pushed (owner, 2026-10-16: no push for the radar).

## 反者道之動 (2026-10-15) — betting against the worst strategies
- Owner's idea: find the strategy that fails most and do the opposite. A high failure rate
  means nothing (long odds pay for it); "bad" = picks win less often than the closing
  market's own fair probability. `scripts/backtest_fade.py` (data/raw/ from the `t10-data`
  and `odds-history` branches, plus the football model's walk-forward stage 1) scores 65
  strategies and their exact opposite (two-way: the other side; 1X2: the other two
  outcomes dutched) -> data/fade_summary.json. Halves by season (MLB 2021-23 / 24-25,
  football 19/20-22/23 / 23/24-26/27): "consistent" = z <= -1.8 overall and negative in both.
- Result: 10 consistent (worst: football longshots >= 8 odds z -3.1, home underdogs, 3-win
  streak teams; MLB T-10 G >= +6 z -2.3, favourites, overs, home favourites >= 55%). Fading
  them gains 0.4-5% at fair prices, loses 2-7% at international prices and 6-23% at 運彩;
  no strategy's opposite makes money at 運彩. The worst are only 1-3pp off the market.
- 運彩's price model (`analysis/fade.tw_prices`): football spreads its margin by a power law
  (13% on favourites, 40% on the longest outcome; within 4% on 191 real 1X2 markets, an
  even spread was 21% off); MLB fits an even 16%. A dutch of two 運彩 prices can be < 1.
- docs/fade.html (`site.fade_page`, every render): the summary, today's games where a
  consistently bad strategy points one way (`fade.live_candidates`: T-10 G >= +6, MLB home
  favourite >= 55%, football longest outcome at fair odds >= 8, home underdog, 模型分歧)
  with the opposite bet's 運彩 price and EV, and `fade.fade_record` - the same opposite
  bets on finished games at the 運彩 price captured before kickoff (reviews + ledger).
- Fixed 2026-10-15: MLB form (streaks, series, last game) is keyed by (start, home, away)
  and built from every final game (`mlb_results`: 11,163, not only the 10,522 priced); keyed
  by start time alone, ~2,400 games sharing a start read another game's teams. League
  tables break ties on goal difference, then name (a set's order changed run to run). The 9
  MLB form strategies moved by < 1.5pp, all still noise; the 10 consistent ones are unchanged.
- Data gap: every row for Cleveland 2021 and the Athletics 2025 has game_type "Unknown"
  (298 final games); every MLB study here leaves them out, also as opponents' history.

## 組合篩選 (2026-10-15) — combining strategies does not beat 運彩
- Owner's question: can T-10, the 反者道之動 fades and the 65 strategies, combined, pick bets
  with a higher win rate? `scripts/backtest_combos.py` -> data/combo_summary.json. A filter
  = one bet type + 1-3 conditions ("strategy k fired, pointing at X"; always-on conditions
  dropped, identical ones merged). Filters with >= 200 early bets are scored on the early
  seasons; fixed rules (best early z; best early 運彩 t-stat; agreement filters using T-10 /
  steam; Bonferroni) take 10 each, and only those are looked up on the late seasons.
- Result: 5,926 filters (MLB 2,478, football 3,448); early z >= 2: 48 / 39, no more than
  chance (56 / 78); none survives Bonferroni. 20 made money at 運彩 early, 0 of them late
  (mean -19% / -12%). The early top decile, +4.3 / +3.5pp over the market, was -0.3 /
  +0.6pp late (corr -0.07 / +0.05) where 運彩 needs +7.5 / +9.7pp. Vote counts (conditions
  that backed a bet and beat the market early, k agreeing): late -1.6 to +1.5pp, 運彩 -12
  to -26%. Ridge logistic on top of the market's log-odds (all conditions): late log-loss
  0.67963 vs 0.67968 (MLB), 0.60488 vs 0.60490; its best late 運彩 EV was -7.0% / -1.2%
  (never a bet). A gradient-boosted correction (scratch, sklearn) did worse than the
  market. The owner's T-10 + fade combos: +0.4 to +3.2pp, 運彩 -5% to -32%.
- High win rates are cheap: home steamed >= 4pp with an away longshot >= 8 -> home or draw
  won 96.5% (market 92.9%, ahead in all 8 seasons), but 運彩's dutch of two prices pays
  0.94: -9.3%. One late survivor to watch, not a rule: MLB favourite with G >= +6 that beat
  the same opponent by 5+ the game before -> underdog +1.5, +7.6pp over the market, ahead in
  all 5 seasons (late z 2.55: 1 of 30 late lookups, ~15% by luck), still -1.2% at 運彩
  (needs +7.8pp). Patterns in prices are priced in; only a 運彩 price above the
  international fair price (positive EV) pays.

## 延遲雷達 (2026-10-16) — 運彩 behind the market between uploads
- 運彩 reprices later than the international market; the only edge found so far is a 運彩
  price that has not followed a move yet (大手追蹤). The phone uploads at 08:00 / 20:00
  Taipei and the routine runs bought international prices within the hour, so the tape had
  no international price hours after an upload: nothing could see 運彩 lagging.
- Purchases (`pipeline radar`): since 2026-10-16 triggered by the 盯盤 sensor (sensor.yml's
  buy job, `--only` the competitions that moved: all their matches with a 運彩 price within
  48 h, one request each), no longer at fixed times; radar.yml is manual only (without
  `--only`: `pregame.radar_due`, matches with a 運彩 price starting within
  `radar_window_hours` (4) whose h2h is older than `radar_min_age_minutes` (120)).
  `_buy_fresh` (shared with the pre-game refresh) buys the essential markets only (h2h;
  MLB also spreads) within the allowance plus PREGAME_EXTRA; pending 運彩 bets get CLV from
  them; then `run_tw` re-judges the last upload (stale: "不判斷") and the render follows.
  Nothing bought: no run_tw, no render, no commit (step output `refreshed`).
- `moves.radar` (radar.html): for each side of a match not started, the
  last 運彩 price against the newest international price bought more than
  `max_price_gap_minutes` after it (a nearer one is the regular comparison, 可下).
  Candidates: that price, unchanged, has the value-bet EV (`value_threshold`: football 2%,
  MLB 3%); `need` (`price_needed`) is the lowest 運彩 price that still pays, for the owner
  to check in the 運彩 app or confirm by uploading. Watch: the market moved toward the side
  by `radar_watch_move` (4pp) since the upload, not enough yet.
- `moves.radar_events` -> events.csv type "radar", one per side and 運彩 upload (event_id uses
  from_at = the upload; to_at = the first international price that showed it); radar.html's
  歷史數據 shows it with ev_at = the stale EV and close_tw_price = whether 運彩 followed.
  Findings are not pushed to the phone (owner's choice).
- `pregame.upload_plan` (建議上傳時間): an upload `UPLOAD_LEAD` (60 min) before the first
  game not yet covered, on the half hour, covers the games of the next
  `pregame_window_hours`; marked done when the sport's last upload falls in the slot.

## 盯盤 (2026-10-16) — two bookmakers watched, the consensus bought when they move
- Owner's request: use odds-api.io's free quota to the full, buy the evening's match-winner
  prices only, drop the three fixed radar runs. The Odds API's many-book consensus stays
  the fair price; 500 credits a month buy about one full look a day. odds-api.io's two
  books (FanDuel + Bet365) can look every ten minutes but are not a consensus (their
  de-vigged average sat 0.6pp median, 1.6pp worst from it on 36 matches): they say WHEN,
  the consensus says HOW MUCH. A move is measured on the two books' own readings, never
  against the consensus (that would read the gap between sources as a move).
- sensor.yml, cron every five minutes (1-59/5), concurrency group sensor. GitHub started the
  scheduled runs 25-47 minutes apart whether asked for every ten or every five minutes, so a
  run does rounds: after each, `pipeline sensor` outputs `next` (minutes to the next reading,
  `sensor.next_due`, or review time, `pipeline.review_next`) and the job sleeps that long and
  goes again, up to about 40 minutes (`timeout-minutes` 55); it stops at once when a round
  has `buy` or `review`, or when nothing is due within 15 minutes. Each round first takes
  data/snapshots, data/reviews and data/odds_budget.json from the newest main (a long run
  must not judge moves against an old purchase time). Readings still go by `TIERS` and the
  quota is planned on `RUN_MINUTES` (10). Job `watch`:
  restores .sensor/ from the Actions cache, runs the rounds (`--hold` when a
  daily-group run is queued, waiting or running: a purchase queued then could cancel a
  waiting upload's run), saves the cache, pushes a counts-only status.json to the orphan
  branch sensor-status. Job `act` (job concurrency group daily), when `buy` or `review` is
  not empty: `pipeline radar --only <competitions>`, `pipeline review --new-only`, commit.
- `sensor.watched`: matches with a 運彩 price (`tw`) within `WINDOW_HOURS` (48) in a
  competition of `fetch/oddsio.LEAGUES` (MLB: regular-season and playoff slugs). Matched to
  odds-api.io events by competition, kickoff within 2 h and both names (`teams.canonical`,
  else difflib; home and away must agree). Event lists are asked for the window only
  (`from`/`to`: the answer is not in date order) every 6 h, or after 1 h while a watched
  match is missing.
- Tiers (`TIERS`): within 24 h every run, 24-48 h every 30 min (1 min of slack); due
  matches go ten to a request (`/odds/multi`), soonest kickoff first. Free places of a
  request and spare requests read the other matches (`reads`): the spare is an even share
  of what the forecast of the next 24 hours' tiers (`forecast`, 72 h of cards with or
  without a 運彩 price) leaves of DAILY_CAP, never more than leaves every 24-hour window
  ahead room for the forecast (`headroom`) and SPARE_MARGIN (20). Caps: HOURLY_CAP 90 and
  DAILY_CAP 480 over ANY hour / 24 hours (the API's own day is undocumented; a UTC-day count
  reached 552 in some 24 h in simulation), BURST 10 a run, stop when the API says 10 or
  fewer are left this hour. No retries (a 429 can ask for most of an hour's wait).
  Simulated typical week (scratch, real code): football only 152-451 requests a day, every
  window <= 480, matches 24-48 h out read every 10 min (median); with 15 MLB games a day
  the cap binds and far matches fall back to 30 min.
- Reading: each book's match-winner prices de-vigged (power), averaged over the books
  that quote every side. Trigger (`moved`): a side moved `MOVE` (3pp) or more since the
  reading nearest the later of the h2h block's `odds_at` and the last purchase asked for
  that competition (`triggers`: a purchase that never came must not be asked for every ten
  minutes); not within `MIN_GAP` (20 min) of either; the latest reading <= 30 min old.
  Purchases are trimmed to what The Odds API's allowance (+ PREGAME_EXTRA) can pay
  (`purchase_cost`: 1, MLB 2); a held one (busy queue, budget) is not recorded and comes
  back next round.
- Privacy: odds-api.io's terms forbid redistribution. Readings live only in the Actions
  cache (.sensor/state.json, de-vigged probabilities); status.json has counts and our own
  team names of unmatched matches; logs print counts and exception type names only.
- MLB in season (March on) will press on the cap: revisit `TIERS` with real numbers then.

## 網站 (2026-10-16) — one page per analysis method
- Owner's request: the old single page was too busy. `render/site.py` builds every page from
  one `Site` (pipeline.render_latest gathers snapshots, the whole tape, CSVs, the 大戶 record,
  radar, fade data, budget, backtest summaries and 運彩's names from 14 days of snapshots).
  Pages (`PAGES`): index 總覽, value 運彩價值, t10 T-10, radar 延遲雷達, moves 盤口移動,
  model 模型分歧, fade 反者道之動, reviews 全部檢討; backtest.html and t10_backtest.html are
  re-rendered from data/*_summary.json (`REPORTS`, `render_report`, also used by the scripts).
- Every method page: hero (what the method does, the numbers that matter), tabs 即將開賽 /
  歷史數據 (fade: 今日反指標 / 反向紀錄 / 回測). 即將開賽 = an analysis card per match not
  started; each card carries the tape for its market (`observations`): a chart (`spark`,
  blue = international de-vigged probability, amber = 運彩's own de-vigged probability, one
  axis, crosshair tooltip) and a table, newest first, where each time is read the method's
  way (`ev_reader`, G, move since first seen, model gap). 歷史數據 = finished matches only
  (the ones still to play are cards), each with what went right or wrong: value bets by
  CLV, passed matches by the closing EV (`_passed`), T-10 by G's shape (`shape_check`),
  caught moves by what came after (`_event_lessons`).
- 模型分歧 cards chart the signal's own market (1X2 / 大小 / 讓球) with the model as a line;
  `signal_allowed` applies today's signal rules to older snapshots.
- Every card carries a curve (owner, 2026-10-16: 「每種分析的卡片下面都有數值變化的曲線」).
  反者道之動 cards chart the opposite bet (`fade_series`): its sides' international fair
  probability added up (blue) vs 1 / 運彩's dutched price, margin included (amber), so EV > 0
  only where blue is above amber; `fade_reader` reads each upload. History cards (value
  bets, T-10, caught moves, every review) chart the prices up to kickoff (`past_curve`):
  the selection's own market (`tape_focus`; a derived bet has no price history, its team's
  1X2 stands in and the caption says so), reviews the lean (`review_focus`) with the model
  as a line, MLB G (`mlb_past_curve`, else the home moneyline). With fewer than two prices
  the card says so (`.nochart`) instead of drawing nothing.
- Front page: headline (可下 / 雷達候選 / none), next upload (`upload_plan`), method tiles,
  48-hour board with flags linking to the cards (`anchor`: m-<sport>-<id>); 反指標 only when
  its price pays, 模型分歧 as a quiet chip (most games carry one).
- Every page's cards follow their matches' kickoff (owner, 2026-10-16: easier to read;
  `by_kickoff`): soonest first for the matches to come, newest first for the history,
  whatever their status (可下 / 大單 / 分歧 used to jump the queue; the hero counts and the
  front page flags still point them out). Two cards of one match keep one anchor
  (`unique_anchors`).
- Look (`_style.html.j2`, "夜場轉播"): floodlit night sky, pitch and diamond line art, turf
  stripes; Barlow Condensed for numbers. Status chips always carry an icon and a word.
- Every date and time on the site is Taipei time (owner, 2026-10-16; the masthead says so):
  `when`, `|tpe`, `|stamp` (report stamps: summaries store `generated` as a UTC ISO time;
  a bare date from an older summary is only reformatted), `|month` for data spans (a game
  date is the venue's own, a month needs no zone). The day is Taipei's: Friday-evening
  European matches are Saturday 02:30-03:00; the newest MLB snapshot holds the next US date.
- The front board keeps today's matches (since 00:00 Taipei) after kickoff (`Site.started`
  from the last two days of snapshots, `started_today`): the score and a link to the review
  once reviewed, else "已開賽・等賽果"; the reviews page's waiting list uses them too. Review
  cards carry the match anchor (`m-<sport>-<id>`); no page may repeat an anchor.

