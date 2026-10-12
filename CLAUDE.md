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
5. renders `docs/index.html` (served by GitHub Pages).

It runs on GitHub Actions (`.github/workflows/daily.yml`). The owner is not a
professional developer: keep code plain, well-named, and commented where a
formula is not obvious.

## Non-negotiable rules

### Security
- NEVER hardcode secrets. Tokens come only from environment variables
  (`FOOTBALL_DATA_TOKEN`, `ODDS_API_KEY`) via `sports_analyzer.config.Settings`.
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
- Generated files that ARE committed: `docs/index.html`, `data/snapshots/**`,
  `data/ledger.csv`, `data/history/*.csv`. Raw dumps under `data/raw/` are not.

## Commands

```bash
python -m unittest discover -s tests -v          # tests (no extra deps)
pytest --cov=src                                 # same, with coverage
ruff check . && ruff format .                    # lint + format
python -m sports_analyzer.pipeline football      # daily football run (needs .env)
python -m sports_analyzer.pipeline football --offline   # render from last snapshot
python scripts/backtest_football.py data/history/*.csv  # backtest + docs/backtest.html
```

## Layout

```
src/sports_analyzer/
  config.py        competitions, sport keys, thresholds, Settings (env)
  http.py          shared requests session: retries, header-based throttling
  teams.py         team-name normalisation + alias table across data sources
  fetch/           football.py (football-data.org), odds.py (The Odds API), mlb.py
  models/          dixon_coles.py, market.py (de-vig, edge, T-10 G value)
  analysis/        football.py — fixtures + model + odds -> card dicts + signals
  ledger.py        append / settle signals in data/ledger.csv
  render/          cards.py + templates/index.html.j2 -> docs/index.html
  pipeline.py      CLI entry point
scripts/backtest_football.py   rolling backtest on football-data.co.uk CSVs
```

## Model notes (so nobody "simplifies" them away)
- Dixon-Coles: Poisson goals with attack/defence per team, home advantage,
  rho correction for 0-0 / 1-0 / 0-1 / 1-1, exponential time decay (xi per day).
- De-vig: default "power" method; "multiplicative" kept for comparison.
- T-10 G value (owner's baseball method): G = p + c - 1 with p = de-vigged
  favourite ML prob and c = de-vigged favourite -1.5 run-line prob;
  thresholds +/-6 percentage points. Lives in `models/market.py`.
