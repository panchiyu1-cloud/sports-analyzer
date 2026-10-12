"""Daily pipeline entry point.

    python -m sports_analyzer.pipeline football [--days 2] [--date YYYY-MM-DD]
    python -m sports_analyzer.pipeline mlb
    python -m sports_analyzer.pipeline render            # re-render from the latest snapshots
    python -m sports_analyzer.pipeline football --offline  # same as render, kept for habit

Each run writes data/snapshots/<date>/<sport>.json, appends signals to
data/ledger.csv, settles old signals, and renders docs/index.html.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import ledger as ledger_mod
from .analysis.football import build_cards
from .config import (
    DOCS_DIR,
    FOOTBALL_COMPETITIONS,
    HISTORY_DIR,
    LEDGER_PATH,
    LOCAL_TZ,
    MLB_ODDS_KEY,
    MODEL,
    SNAPSHOT_DIR,
    Competition,
    ModelConfig,
    Settings,
)
from .fetch.football import FootballDataClient, Match
from .fetch.mlb import MlbClient
from .fetch.odds import OddsClient, OddsEvent
from .history import load_history
from .models import dixon_coles as dc
from .render.cards import render_index

log = logging.getLogger("sports_analyzer")


# ------------------------------------------------------------------- football
def run_football(settings: Settings, today: date, cfg: ModelConfig = MODEL) -> dict[str, Any]:
    fd = FootballDataClient(settings.football_data_token or "")
    odds = OddsClient(settings.odds_api_key or "", regions=settings.odds_regions)

    # 1. Pull fixtures / results / standings for every competition (18 calls, throttled).
    fixtures: dict[str, list[Match]] = {}
    results: dict[str, list[Match]] = {}
    standings: dict[str, list] = {}
    for comp in FOOTBALL_COMPETITIONS:
        fixtures[comp.code] = fd.upcoming(comp.code, today, settings.fixture_window_days)
        results[comp.code] = fd.results(comp.code)
        standings[comp.code] = fd.standings(comp.code)
        log.info(
            "%s: %d fixtures in window, %d results this season",
            comp.code, len(fixtures[comp.code]), len(results[comp.code]),
        )

    # 2. Fit one model per domestic league; a pooled model for the Champions League.
    history = {c.code: (load_history(HISTORY_DIR, c.fdcouk_code) if c.fdcouk_code else []) for c in FOOTBALL_COMPETITIONS}
    models: dict[str, dc.FittedModel | None] = {}
    domestic_results: list[dc.MatchResult] = []
    for comp in FOOTBALL_COMPETITIONS:
        if comp.is_cup:
            continue
        sample = _training_sample(results[comp.code], history[comp.code])
        domestic_results.extend(sample)
        models[comp.code] = _fit_or_none(comp, sample, today, cfg)
    for comp in FOOTBALL_COMPETITIONS:
        if comp.is_cup:
            pooled = _dedupe(domestic_results + _training_sample(results[comp.code], []))
            models[comp.code] = _fit_or_none(comp, pooled, today, cfg) if fixtures[comp.code] else None

    # 3. Odds only for competitions with fixtures (each call costs credits).
    window_start = datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc)
    window_end = window_start + timedelta(days=settings.fixture_window_days + 1)
    odds_events: dict[str, list[OddsEvent]] = {}
    for comp in FOOTBALL_COMPETITIONS:
        if not fixtures[comp.code]:
            odds_events[comp.code] = []
            continue
        odds_events[comp.code] = odds.odds(comp.odds_key, commence_from=window_start, commence_to=window_end)

    # 4. Cards + signals.
    snapshot: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "date": today.isoformat(),
        "credits_remaining": odds.credits_remaining(),
        "competitions": [],
    }
    all_signals: list[dict[str, Any]] = []
    for comp in FOOTBALL_COMPETITIONS:
        model = models.get(comp.code)
        cards, signals = build_cards(
            comp, fixtures[comp.code], model, standings[comp.code], results[comp.code],
            history[comp.code], odds_events[comp.code], cfg,
        )
        snapshot["competitions"].append(
            {
                "code": comp.code,
                "name": comp.name,
                "model": _model_summary(model),
                "cards": cards,
                "signals": signals,
            }
        )
        all_signals.extend(signals)

    # 5. Ledger: add today's signals, settle anything that has finished.
    added = ledger_mod.append_signals(LEDGER_PATH, all_signals)
    settled = ledger_mod.settle_football(LEDGER_PATH, [m for rs in results.values() for m in rs])
    log.info("ledger: %d new signals, %d settled", added, settled)

    _write_snapshot(today, "football", snapshot)
    return snapshot


def _training_sample(results: list[Match], history: list) -> list[dc.MatchResult]:
    sample = [h.result for h in history]
    sample.extend(
        dc.MatchResult(m.utc_date.date(), m.home_key, m.away_key, m.home_goals or 0, m.away_goals or 0)
        for m in results
        if m.is_finished
    )
    return _dedupe(sample)


def _dedupe(sample: list[dc.MatchResult]) -> list[dc.MatchResult]:
    seen: set[tuple[date, str, str]] = set()
    out: list[dc.MatchResult] = []
    for r in sample:
        key = (r.day, r.home, r.away)
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def _fit_or_none(comp: Competition, sample: list[dc.MatchResult], today: date, cfg: ModelConfig) -> dc.FittedModel | None:
    if len(sample) < 10:
        log.warning("%s: only %d results, skipping model", comp.code, len(sample))
        return None
    model = dc.fit(sample, as_of=today, xi=cfg.dc_xi, max_goals=cfg.dc_max_goals)
    log.info("%s: fitted on %d matches, %d teams, home adv %.3f, rho %.3f",
             comp.code, model.n_matches, len(model.teams), model.home_advantage, model.rho)
    return model


def _model_summary(model: dc.FittedModel | None) -> dict[str, Any] | None:
    if model is None:
        return None
    return {
        "n_matches": model.n_matches,
        "n_teams": len(model.teams),
        "home_advantage": round(model.home_advantage, 3),
        "rho": round(model.rho, 3),
        "as_of": model.as_of.isoformat(),
    }


# ------------------------------------------------------------------------ MLB
def run_mlb(today_local: date) -> dict[str, Any]:
    games = MlbClient().schedule(today_local)
    snapshot = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "date": today_local.isoformat(),
        "games": [
            {
                "game_pk": g.game_pk,
                "local": g.game_date.astimezone(LOCAL_TZ).strftime("%m/%d %H:%M"),
                "status": g.status,
                "home": g.home,
                "away": g.away,
                "home_probable": g.home_probable,
                "away_probable": g.away_probable,
                "venue": g.venue,
            }
            for g in games
        ],
        "odds_key": MLB_ODDS_KEY,
    }
    _write_snapshot(today_local, "mlb", snapshot)
    return snapshot


# -------------------------------------------------------------------- render
def render_latest() -> Path:
    football = _latest_snapshot("football")
    mlb = _latest_snapshot("mlb")
    summary = ledger_mod.summary(ledger_mod.read(LEDGER_PATH))
    return render_index(
        football,
        mlb,
        summary,
        datetime.now(timezone.utc),
        DOCS_DIR / "index.html",
        backtest_exists=(DOCS_DIR / "backtest.html").exists(),
    )


def _write_snapshot(day: date, sport: str, payload: dict[str, Any]) -> Path:
    folder = SNAPSHOT_DIR / day.isoformat()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{sport}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    log.info("snapshot written: %s", path.relative_to(SNAPSHOT_DIR.parent.parent))
    return path


def _latest_snapshot(sport: str) -> dict[str, Any] | None:
    candidates = sorted(SNAPSHOT_DIR.glob(f"*/{sport}.json"))
    if not candidates:
        return None
    return json.loads(candidates[-1].read_text(encoding="utf-8"))


# ----------------------------------------------------------------------- CLI
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sports_analyzer.pipeline")
    parser.add_argument("task", choices=["football", "mlb", "render"])
    parser.add_argument("--days", type=int, default=None, help="fixture window in days (default from env or 2)")
    parser.add_argument("--date", type=str, default=None, help="run as if today were YYYY-MM-DD (UTC)")
    parser.add_argument("--offline", action="store_true", help="skip the network and just re-render")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )

    today = date.fromisoformat(args.date) if args.date else datetime.now(timezone.utc).date()
    if args.task == "render" or args.offline:
        out = render_latest()
        log.info("rendered %s", out)
        return 0

    settings = Settings.from_env()
    if args.days is not None:
        settings = Settings(
            settings.football_data_token, settings.odds_api_key, settings.odds_regions, args.days
        )
    if args.task == "football":
        run_football(settings, today)
    elif args.task == "mlb":
        run_mlb(datetime.now(LOCAL_TZ).date() if not args.date else today)
    out = render_latest()
    log.info("rendered %s", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
