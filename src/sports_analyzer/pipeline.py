"""Daily pipeline entry point.

    python -m sports_analyzer.pipeline football [--days 2] [--date YYYY-MM-DD]
    python -m sports_analyzer.pipeline mlb
    python -m sports_analyzer.pipeline nba
    python -m sports_analyzer.pipeline review           # post-match review of finished games
    python -m sports_analyzer.pipeline radar            # 延遲雷達: fresh prices near kickoff
    python -m sports_analyzer.pipeline render            # re-render from the latest snapshots
    python -m sports_analyzer.pipeline football --offline  # same as render, kept for habit

Each run writes data/snapshots/<date>/<sport>.json, appends signals to
data/ledger.csv, settles old signals, and renders docs/index.html.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import requests

from . import budget as budget_mod
from . import ledger as ledger_mod
from . import moves as moves_mod
from . import notify, review_store, tw_ledger
from . import sensor as sensor_mod
from .analysis import fade as fade_mod
from .analysis import pregame, review
from .analysis.football import build_cards
from .analysis.mlb import build_cards as build_mlb_cards
from .analysis.mlb import g_value_block, t10_value_block
from .analysis.nba import build_cards as build_nba_cards
from .analysis.nba import shape_block
from .analysis.taiwan import annotate as annotate_tw
from .config import (
    COMPETITION_BY_CODE,
    DATA_DIR,
    DOCS_DIR,
    FOOTBALL_COMPETITIONS,
    FOOTBALL_REVIEWS_PATH,
    HISTORY_DIR,
    IMPLIED_CALIBRATION_PATH,
    LEDGER_PATH,
    LOCAL_TZ,
    MLB_ODDS_KEY,
    MLB_REVIEWS_PATH,
    MODEL,
    MOVES_DIR,
    NBA_ODDS_KEY,
    NBA_REVIEWS_PATH,
    ODDS_BUDGET_PATH,
    SENSOR_DIR,
    SNAPSHOT_DIR,
    TW_DIR,
    TW_LEDGER_PATH,
    Competition,
    ModelConfig,
    Settings,
)
from .fetch import taiwan
from .fetch.football import FootballDataClient, Match
from .fetch.mlb import MlbClient
from .fetch.nba import NbaClient
from .fetch.odds import FOOTBALL_MARKETS, MLB_MARKETS, NBA_MARKETS, OddsClient, OddsEvent
from .fetch.oddsio import LEAGUES as IO_LEAGUES
from .fetch.oddsio import MAX_IDS, OddsIoClient
from .history import find_stats, load_history, load_stats
from .models import dixon_coles as dc
from .models.implied import IDENTITY, Calibration
from .render.cards import taipei_time
from .render.site import REPORTS, Site, render_site

log = logging.getLogger("sports_analyzer")


# ------------------------------------------------------------------- football
def run_football(
    settings: Settings,
    today: date,
    cfg: ModelConfig = MODEL,
    markets: tuple[str, ...] = FOOTBALL_MARKETS,
) -> dict[str, Any]:
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
            comp.code,
            len(fixtures[comp.code]),
            len(results[comp.code]),
        )

    # 2. Fit one model per domestic league; a pooled model for the Champions League.
    history = {
        c.code: (load_history(HISTORY_DIR, c.fdcouk_code) if c.fdcouk_code else [])
        for c in FOOTBALL_COMPETITIONS
    }
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
            models[comp.code] = (
                _fit_or_none(comp, pooled, today, cfg) if fixtures[comp.code] else None
            )

    # 3. Odds only for competitions with fixtures (each call costs credits), and only
    #    as many markets as today's share of the monthly credits allows.
    window_start = datetime.combine(today, datetime.min.time(), tzinfo=UTC)
    window_end = window_start + timedelta(days=settings.fixture_window_days + 1)
    budget = budget_mod.load(ODDS_BUDGET_PATH, today)
    calls = sum(1 for comp in FOOTBALL_COMPETITIONS if fixtures[comp.code])
    wanted = markets
    markets = budget_mod.plan_markets(
        markets, calls, budget_mod.allowance_today(budget, today), essential=("h2h",)
    )
    odds_events: dict[str, list[OddsEvent]] = {comp.code: [] for comp in FOOTBALL_COMPETITIONS}
    for comp in FOOTBALL_COMPETITIONS:
        if not fixtures[comp.code] or not markets:
            continue
        try:
            odds_events[comp.code] = odds.odds(
                comp.odds_key, commence_from=window_start, commence_to=window_end, markets=markets
            )
        except requests.RequestException as exc:
            # e.g. the quota ran out (HTTP 401): keep the last prices rather than fail the run.
            log.warning("football odds unavailable (%s); keeping earlier prices", exc)
            odds_events = {code: [] for code in odds_events}
            markets = ()
            break

    # 4. Cards + signals.
    budget = budget_mod.record(budget, odds.credits_used(), odds.credits_remaining(), today)
    budget_mod.save(ODDS_BUDGET_PATH, budget)
    snapshot: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "date": today.isoformat(),
        "credits_remaining": budget.remaining,
        "odds_markets": list(markets),
        "competitions": [],
    }
    all_signals: list[dict[str, Any]] = []
    for comp in FOOTBALL_COMPETITIONS:
        model = models.get(comp.code)
        cards, signals = build_cards(
            comp,
            fixtures[comp.code],
            model,
            standings[comp.code],
            results[comp.code],
            history[comp.code],
            odds_events[comp.code],
            cfg,
        )
        snapshot["competitions"].append(
            {
                "code": comp.code,
                "name": comp.name,
                "model": _model_summary(model),
                "cards": cards,
                "signals": signals,
                "name_check": _name_check(
                    results[comp.code], history[comp.code], odds_events[comp.code]
                ),
            }
        )
        all_signals.extend(signals)

    # 4b. A second run on the same date keeps what the first one had and this one lacks
    #     (totals when only h2h was bought, a 運彩 block), marked stale and never used
    #     for CLV. With no odds bought at all (budget), the last prices of any date.
    _merge_previous_markets(snapshot, _latest_snapshot("football"), any_date=not markets)
    if markets != wanted:
        log.warning("football odds: wanted %s, bought %s (budget)", wanted, markets or "none")
    taped = moves_mod.record(
        MOVES_DIR,
        moves_mod.intl_rows(
            [c for comp in snapshot["competitions"] for c in comp["cards"]],
            "football",
            snapshot["generated_at"],
        ),
    )
    log.info("line-move tape: %d international prices recorded", taped)

    # 5. Ledgers: add today's signals, settle anything that has finished (both ledgers).
    finished = [m for rs in results.values() for m in rs if m.is_finished]
    added = ledger_mod.append_signals(LEDGER_PATH, all_signals)
    settled = ledger_mod.settle_football(LEDGER_PATH, finished)
    tw_settled = tw_ledger.settle(
        TW_LEDGER_PATH,
        {str(m.id): (m.home_goals or 0, m.away_goals or 0, m.home_ht, m.away_ht) for m in finished},
        "football",
    )
    # A later consensus for the same matches is the closing proxy for 運彩 bets taken earlier.
    all_cards = [c for comp in snapshot["competitions"] for c in comp["cards"]]
    clv_updated = tw_ledger.update_clv(
        TW_LEDGER_PATH, all_cards, "football", _implied_calibration()
    )
    log.info(
        "ledger: %d new signals, %d settled | tw ledger: %d settled, %d CLV filled",
        added,
        settled,
        tw_settled,
        clv_updated,
    )

    _write_snapshot(today, "football", snapshot)
    return snapshot


def _training_sample(results: list[Match], history: list) -> list[dc.MatchResult]:
    sample = [h.result for h in history]
    sample.extend(
        dc.MatchResult(
            m.utc_date.date(), m.home_key, m.away_key, m.home_goals or 0, m.away_goals or 0
        )
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


def _fit_or_none(
    comp: Competition, sample: list[dc.MatchResult], today: date, cfg: ModelConfig
) -> dc.FittedModel | None:
    if len(sample) < 10:
        log.warning("%s: only %d results, skipping model", comp.code, len(sample))
        return None
    model = dc.fit(sample, as_of=today, xi=cfg.dc_xi, max_goals=cfg.dc_max_goals)
    log.info(
        "%s: fitted on %d matches, %d teams, home adv %.3f, rho %.3f",
        comp.code,
        model.n_matches,
        len(model.teams),
        model.home_advantage,
        model.rho,
    )
    return model


def _merge_previous_markets(
    snapshot: dict[str, Any], previous: dict[str, Any] | None, any_date: bool = False
) -> None:
    """Carry consensus blocks from the previous snapshot into cards that lack them.

    Normally only within the same date (the evening h2h-only refresh keeps the
    morning's totals). With `any_date` (no odds were bought) every block is carried,
    h2h included, from whichever snapshot is newest.
    """
    if not previous or (previous.get("date") != snapshot.get("date") and not any_date):
        return
    keys = ("h2h", "totals", "spread") if any_date else ("totals", "spread")
    old_cards = {
        c["match_id"]: c for comp in previous.get("competitions", []) for c in comp.get("cards", [])
    }
    for comp in snapshot["competitions"]:
        for card in comp["cards"]:
            old = old_cards.get(card["match_id"])
            if not old or not old.get("market"):
                continue
            if card.get("market") is None:
                card["market"] = {
                    "books": old["market"].get("books", 0),
                    "h2h": None,
                    "totals": None,
                    "spread": None,
                }
            for key in keys:
                if card["market"].get(key) is None and old["market"].get(key):
                    card["market"][key] = {**old["market"][key], "stale": True}
            if old.get("tw") and not card.get("tw"):
                card["tw"] = old["tw"]


def _name_check(
    results: list[Match], history: list, odds_events: list[OddsEvent]
) -> dict[str, Any]:
    """Diagnostic: team keys that appear in only one source are alias-table gaps.

    Written into the snapshot so a mismatch is visible the morning after, instead
    of silently splitting one club into two model entries.
    """
    api_keys = {m.home_key for m in results} | {m.away_key for m in results}
    api_names = {m.home_key: m.home for m in results} | {m.away_key: m.away for m in results}
    csv_keys = {h.home for h in history} | {h.away for h in history}
    odds_keys = {e.home_key for e in odds_events} | {e.away_key for e in odds_events}
    odds_names = {e.home_key: e.home for e in odds_events} | {
        e.away_key: e.away for e in odds_events
    }
    out: dict[str, Any] = {}
    if api_keys and csv_keys:
        out["api_only"] = sorted(api_names[k] for k in api_keys - csv_keys)
        out["csv_only"] = sorted(csv_keys - api_keys)
    if odds_keys:
        out["odds_unmatched"] = sorted(odds_names[k] for k in odds_keys - api_keys - csv_keys)
    return out


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
def run_mlb(
    today_local: date,
    settings: Settings | None = None,
    cfg: ModelConfig = MODEL,
    markets: tuple[str, ...] = MLB_MARKETS,
) -> dict[str, Any]:
    """Today's MLB games (Taipei date) with market consensus, fair odds and the T-10 G value.
    `markets`: the evening run keeps the run line (half of G) and drops the totals."""
    client = MlbClient()
    games = client.schedule(today_local)
    # Yesterday's finals settle the 運彩 ledger (the Taipei "day" spans two US dates).
    finals = {
        str(g.game_pk): (g.home_score or 0, g.away_score or 0)
        for d in (today_local - timedelta(days=1), today_local - timedelta(days=2))
        for g in client.schedule(d)
        if g.is_final
    }
    tw_settled = tw_ledger.settle(TW_LEDGER_PATH, finals, "mlb")
    log.info("mlb: %d games today, %d 運彩 rows settled", len(games), tw_settled)
    events: list[OddsEvent] = []
    credits = None
    budget = budget_mod.load(ODDS_BUDGET_PATH, today_local)
    markets = budget_mod.plan_markets(
        markets,
        1 if games else 0,
        budget_mod.allowance_today(budget, datetime.now(UTC).date()),
        essential=("h2h", "spreads"),  # the two halves of the T-10 G value
    )
    if games and settings and settings.odds_api_key and markets:
        odds = OddsClient(settings.odds_api_key, regions=settings.odds_regions)
        # The MLB "date" is a US date; its games start the next Taipei morning, so the
        # window has to follow the actual first pitches, not the Taipei calendar day.
        first = min(g.game_date for g in games) - timedelta(hours=1)
        last = max(g.game_date for g in games) + timedelta(hours=1)
        try:
            events = odds.odds(MLB_ODDS_KEY, commence_from=first, commence_to=last, markets=markets)
        except requests.RequestException as exc:
            log.warning("MLB odds unavailable (%s); keeping earlier prices", exc)
        credits = odds.credits_remaining()
        budget_mod.save(
            ODDS_BUDGET_PATH,
            budget_mod.record(budget, odds.credits_used(), credits, datetime.now(UTC).date()),
        )
    cards = build_mlb_cards(games, events, cfg)
    snapshot = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "date": today_local.isoformat(),
        "credits_remaining": credits,
        "games": cards,
        "odds_key": MLB_ODDS_KEY,
    }
    # A second run on the same date (the evening refresh) must not wipe what the
    # morning found: games that have since started lose their odds, and the 運彩
    # comparison lives on the card.
    _merge_previous_mlb(snapshot, _latest_snapshot("mlb"))
    for card in cards:
        # A carried-over 運彩 block is re-judged against the new prices (and shown as
        # not judged when the two are hours apart: analysis/taiwan.is_fresh).
        card["t10v"] = t10_value_block(card, cfg)
    moves_mod.record(MOVES_DIR, moves_mod.intl_rows(cards, "mlb", snapshot["generated_at"]))
    clv_updated = tw_ledger.update_clv(TW_LEDGER_PATH, cards, "mlb")
    log.info(
        "mlb: %d cards with odds, %d 運彩 CLV filled",
        sum(1 for c in cards if c["market"]),
        clv_updated,
    )
    _write_snapshot(today_local, "mlb", snapshot)
    return snapshot


def _merge_previous_mlb(snapshot: dict[str, Any], previous: dict[str, Any] | None) -> None:
    """MLB: carry blocks from an earlier same-date snapshot, the T-10 G block with them."""
    _merge_previous_games(snapshot, previous, "game_pk", ("t10",))


def _merge_previous_games(
    snapshot: dict[str, Any],
    previous: dict[str, Any] | None,
    key: str,
    derived: tuple[str, ...] = (),
) -> None:
    """Carry market blocks and 運彩 comparisons from an earlier same-date snapshot of a
    sport whose snapshot is a list of games (MLB by game_pk, NBA by game_id). `derived`
    names the card blocks computed from the market (MLB's t10, NBA's shape), carried
    with it."""
    if not previous or previous.get("date") != snapshot.get("date"):
        return
    old_cards = {str(c.get(key)): c for c in previous.get("games", [])}
    for card in snapshot["games"]:
        old = old_cards.get(str(card.get(key)))
        if not old:
            continue
        if card.get("market") is None and old.get("market"):
            # Per-block "stale" flags: update_clv() must not use a carried-over
            # price as the closing price (CLV would then just equal EV).
            card["market"] = {
                k: ({**v, "stale": True} if isinstance(v, dict) else v)
                for k, v in old["market"].items()
            }
            for name in derived:
                card[name] = old.get(name)
            bought = (old["market"].get("h2h") or {}).get("odds_at")
            card["notes"] = [
                f"國際價沿用 {taipei_time(bought)} 的報價（這次沒有更新：預算不足或比賽已開打）"
                if bought
                else "國際價為稍早快照（這次沒有更新：預算不足或比賽已開打）"
            ]
        elif card.get("market") and old.get("market"):
            # The evening run buys the match-winner and run line only: the morning's
            # totals stay, marked stale like any carried-over block.
            for key in ("totals", "spread"):
                if card["market"].get(key) is None and old["market"].get(key):
                    card["market"][key] = {**old["market"][key], "stale": True}
        if old.get("tw") and not card.get("tw"):
            card["tw"] = old["tw"]


# ------------------------------------------------------------------------ NBA
def run_nba(
    today_local: date,
    settings: Settings | None = None,
    cfg: ModelConfig = MODEL,
    markets: tuple[str, ...] = NBA_MARKETS,
) -> dict[str, Any]:
    """The NBA games of one US date (the Taipei date's: they tip off the next Taipei
    morning, like MLB) with the market consensus, the fair prices and what the market
    expects. `markets`: the evening run keeps the handicap and drops the totals."""
    client = NbaClient()
    games = client.scoreboard(today_local)
    # The last two US dates' finals settle the 運彩 ledger (a Taipei day spans two).
    finals = {
        g.game_id: (g.home_score or 0, g.away_score or 0)
        for d in (today_local - timedelta(days=1), today_local - timedelta(days=2))
        for g in client.scoreboard(d)
        if g.is_final
    }
    tw_settled = tw_ledger.settle(TW_LEDGER_PATH, finals, "nba")
    log.info("nba: %d games on %s, %d 運彩 rows settled", len(games), today_local, tw_settled)
    events: list[OddsEvent] = []
    credits = None
    budget = budget_mod.load(ODDS_BUDGET_PATH, today_local)
    markets = budget_mod.plan_markets(
        markets,
        1 if games else 0,
        budget_mod.allowance_today(budget, datetime.now(UTC).date()),
        essential=("h2h", "spreads"),  # 運彩's two main basketball markets
    )
    if games and settings and settings.odds_api_key and markets:
        odds = OddsClient(settings.odds_api_key, regions=settings.odds_regions)
        first = min(g.tipoff for g in games) - timedelta(hours=1)
        last = max(g.tipoff for g in games) + timedelta(hours=1)
        try:
            events = odds.odds(NBA_ODDS_KEY, commence_from=first, commence_to=last, markets=markets)
        except requests.RequestException as exc:
            # The type only: a connection error's text carries the URL, key included.
            log.warning("NBA odds unavailable (%s); keeping earlier prices", type(exc).__name__)
        credits = odds.credits_remaining()
        budget_mod.save(
            ODDS_BUDGET_PATH,
            budget_mod.record(budget, odds.credits_used(), credits, datetime.now(UTC).date()),
        )
    cards = build_nba_cards(games, events, cfg)
    snapshot = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "date": today_local.isoformat(),
        "credits_remaining": credits,
        "games": cards,
        "odds_key": NBA_ODDS_KEY,
    }
    # The evening refresh must not wipe what the morning found (started games lose
    # their odds; the 運彩 comparison lives on the card).
    _merge_previous_games(snapshot, _latest_snapshot("nba"), "game_id", ("shape",))
    moves_mod.record(MOVES_DIR, moves_mod.intl_rows(cards, "nba", snapshot["generated_at"]))
    clv_updated = tw_ledger.update_clv(TW_LEDGER_PATH, cards, "nba")
    log.info(
        "nba: %d cards with odds, %d 運彩 CLV filled",
        sum(1 for c in cards if c["market"]),
        clv_updated,
    )
    _write_snapshot(today_local, "nba", snapshot)
    return snapshot


# ------------------------------------------------------------------ 運彩
def run_tw(
    cfg: ModelConfig = MODEL, settings: Settings | None = None, now: datetime | None = None
) -> dict[str, Any]:
    """Compare the newest 運彩 upload with the newest snapshots; record value bets.

    Games starting within the pre-game window first get freshly bought international
    prices (analysis/pregame.py), when an Odds API key is set, the upload is recent
    and the budget allows: the T-10 idea of deciding just before first pitch.
    """
    now = now or datetime.now(UTC)
    reports: dict[str, Any] = {}
    new_values: list[dict[str, Any]] = []
    for src, dst in taiwan.normalize_uploads(TW_DIR):
        log.info("tw: renamed upload %s -> %s", src.relative_to(TW_DIR), dst.relative_to(TW_DIR))
    calib = _implied_calibration()

    football = _latest_snapshot("football")
    games, path, parse_report = taiwan.load_latest(TW_DIR, "football")
    if football and path:
        _backfill_odds_at(football)
        cards = [c for comp in football["competitions"] for c in comp["cards"]]
        captured = taiwan.captured_at(path)
        pre, refreshed = _pregame_refresh("football", cards, captured, settings, now, cfg)
        if refreshed:
            # The fresh consensus is the closing proxy for 運彩 bets recorded earlier;
            # filled before this run records new ones, so no bet is its own close.
            tw_ledger.update_clv(TW_LEDGER_PATH, cards, "football", calib, now=now)
        rep = annotate_tw(cards, games, captured, cfg, calib=calib)
        rep.update(
            {
                "file": path.name,
                "unknown_teams": sorted(parse_report.unknown_teams),
                "unknown_markets": parse_report.unknown_markets,
                "pregame": pre,
            }
        )
        football["tw_report"] = rep
        _tape(cards, "football")
        rep["values_added"] = tw_ledger.append_values(
            TW_LEDGER_PATH, cards, "football", added_rows=new_values
        )
        _write_snapshot(date.fromisoformat(football["date"]), "football", football)
        reports["football"] = rep
        log.info("tw football: %s", {k: v for k, v in rep.items() if k not in ("unmatched_games",)})
    elif path is None:
        log.info("tw football: no upload found under %s", TW_DIR / "football")

    # The newest MLB snapshot holds the next US date; today's later first pitches sit
    # in the one before it, and those are the games a pre-game upload is about.
    mlb_snaps = _latest_snapshots("mlb", 2)
    games, path, parse_report = taiwan.load_latest(TW_DIR, "baseball")
    if mlb_snaps and path:
        latest = mlb_snaps[-1]
        cards = list(latest.get("games", []))
        seen = {str(c.get("game_pk")) for c in cards}
        for older in mlb_snaps[:-1]:
            cards += [c for c in older.get("games", []) if str(c.get("game_pk")) not in seen]
        for snap in mlb_snaps:
            _backfill_odds_at(snap)
        captured = taiwan.captured_at(path)
        pre, refreshed = _pregame_refresh("mlb", cards, captured, settings, now, cfg)
        for card in refreshed:
            card["t10"] = g_value_block(card["market"], cfg)
            card["notes"] = [n for n in card.get("notes", []) if "賠率" not in n]
        if refreshed:
            tw_ledger.update_clv(TW_LEDGER_PATH, cards, "mlb", now=now)
        mlb_cfg = replace(cfg, value_threshold=cfg.mlb_value_threshold)
        rep = annotate_tw(cards, games, captured, mlb_cfg)
        for card in cards:
            card["t10v"] = t10_value_block(card, cfg)
        rep.update(
            {
                "file": path.name,
                "unknown_teams": sorted(parse_report.unknown_teams),
                "unknown_markets": parse_report.unknown_markets,
                "pregame": pre,
            }
        )
        latest["tw_report"] = rep
        _tape(cards, "mlb")
        rep["values_added"] = tw_ledger.append_values(
            TW_LEDGER_PATH, cards, "mlb", added_rows=new_values
        )
        for snap in mlb_snaps:
            _write_snapshot(date.fromisoformat(snap["date"]), "mlb", snap)
        reports["mlb"] = rep
        log.info("tw mlb: %s", {k: v for k, v in rep.items() if k not in ("unmatched_games",)})

    # NBA, the same way as MLB: the newest snapshot holds the next US date, the one
    # before it the games tipping off later today (Taipei).
    nba_snaps = _latest_snapshots("nba", 2)
    games, path, parse_report = taiwan.load_latest(TW_DIR, "basketball")
    if nba_snaps and path:
        latest = nba_snaps[-1]
        cards = _games_of(nba_snaps, "game_id")
        for snap in nba_snaps:
            _backfill_odds_at(snap)
        captured = taiwan.captured_at(path)
        pre, refreshed = _pregame_refresh("nba", cards, captured, settings, now, cfg)
        for card in refreshed:
            card["shape"] = shape_block(card["market"], cfg)
            card["notes"] = [n for n in card.get("notes", []) if "賠率" not in n]
        if refreshed:
            tw_ledger.update_clv(TW_LEDGER_PATH, cards, "nba", now=now)
        nba_cfg = replace(cfg, value_threshold=cfg.nba_value_threshold)
        rep = annotate_tw(cards, games, captured, nba_cfg)
        rep.update(
            {
                "file": path.name,
                "unknown_teams": sorted(parse_report.unknown_teams),
                "unknown_markets": parse_report.unknown_markets,
                "pregame": pre,
            }
        )
        latest["tw_report"] = rep
        _tape(cards, "nba")
        rep["values_added"] = tw_ledger.append_values(
            TW_LEDGER_PATH, cards, "nba", competition_of=lambda c: "NBA", added_rows=new_values
        )
        for snap in nba_snaps:
            _write_snapshot(date.fromisoformat(snap["date"]), "nba", snap)
        reports["nba"] = rep
        log.info("tw nba: %s", {k: v for k, v in rep.items() if k not in ("unmatched_games",)})
    elif path is None:
        log.info("tw nba: no upload found under %s", TW_DIR / "basketball")
    if new_values and settings and settings.ntfy_topic:
        tracked = moves_mod.track(moves_mod.read(MOVES_DIR), cfg)
        notify.notify_value_bets(settings.ntfy_topic, new_values, tracked)
    return reports


def _games_of(snapshots: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    """The games of the newest snapshot plus those of the older ones it does not list
    (the newest copy of a game wins). `snapshots` are oldest first."""
    cards: list[dict[str, Any]] = list(snapshots[-1].get("games", [])) if snapshots else []
    seen = {str(c.get(key)) for c in cards}
    for older in snapshots[:-1][::-1]:
        for c in older.get("games", []):
            if str(c.get(key)) not in seen:
                cards.append(c)
                seen.add(str(c.get(key)))
    return cards


def _tape(cards: list[dict[str, Any]], sport: str) -> None:
    """Both price sets on the cards go on the line-move tape (duplicates are dropped)."""
    n_intl = moves_mod.record(MOVES_DIR, moves_mod.intl_rows(cards, sport))
    n_tw = moves_mod.record(MOVES_DIR, moves_mod.tw_rows(cards, sport))
    log.info("line-move tape %s: %d international, %d 運彩 prices recorded", sport, n_intl, n_tw)


def _pregame_refresh(
    sport: str,
    cards: list[dict[str, Any]],
    captured: datetime | None,
    settings: Settings | None,
    now: datetime,
    cfg: ModelConfig,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Buy fresh international prices for the cards about to start (see analysis/pregame),
    when the 運彩 upload is recent. Returns a small report and the refreshed cards."""
    due = pregame.due_cards(cards, now, cfg)
    report: dict[str, Any] = {"due": len(due), "refreshed": 0}
    if not due:
        return report, []
    if not pregame.upload_is_recent(captured, now):
        report["skipped"] = "upload not recent"
        return report, []
    return _buy_fresh(sport, due, settings, now, cfg, report)


def _buy_fresh(
    sport: str,
    due: list[dict[str, Any]],
    settings: Settings | None,
    now: datetime,
    cfg: ModelConfig,
    report: dict[str, Any],
    essential_only: bool = False,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """One request per sport key (MLB, NBA, or each football competition with a due
    match): h2h + spreads (+ totals) for MLB and NBA and h2h (+ totals) for football, or
    only the essential ones (h2h; MLB also the run line, the other half of G; NBA also
    the handicap), within the
    budget's allowance plus the pre-game extra. Prices close to kickoff are worth more
    than the routine runs' ones; the reserve is never touched."""
    if not settings or not settings.odds_api_key:
        report["skipped"] = "no API key"
        return report, []
    groups: dict[str, list[dict[str, Any]]] = {}
    if sport == "mlb":
        groups[MLB_ODDS_KEY] = due
        wanted, essential, prefer = MLB_MARKETS, ("h2h", "spreads"), None
    elif sport == "nba":
        groups[NBA_ODDS_KEY] = due
        wanted, essential, prefer = NBA_MARKETS, ("h2h", "spreads"), None
    else:
        for card in due:
            comp = COMPETITION_BY_CODE.get(str(card.get("competition")))
            if comp:
                groups.setdefault(comp.odds_key, []).append(card)
        wanted, essential, prefer = FOOTBALL_MARKETS, ("h2h",), 2.5
    if essential_only:
        wanted = essential
    today = now.date()
    state = budget_mod.load(ODDS_BUDGET_PATH, today)
    allowance = budget_mod.allowance_today(state, today, extra=budget_mod.PREGAME_EXTRA)
    markets = budget_mod.plan_markets(wanted, len(groups), allowance, essential)
    if not markets or not groups:
        report["skipped"] = "budget"
        return report, []
    odds = OddsClient(settings.odds_api_key, regions=settings.odds_regions)
    events: list[OddsEvent] = []
    for key, group in groups.items():
        starts = [s for s in (pregame.kickoff(c) for c in group) if s is not None]
        try:
            events += odds.odds(
                key,
                commence_from=min(starts) - timedelta(hours=1),
                commence_to=max(starts) + timedelta(hours=1),
                markets=markets,
            )
        except requests.RequestException as exc:
            # The type only: a connection error's text carries the URL, key included.
            log.warning(
                "pre-game odds unavailable (%s); keeping earlier prices", type(exc).__name__
            )
            break
    budget_mod.save(
        ODDS_BUDGET_PATH,
        budget_mod.record(state, odds.credits_used(), odds.credits_remaining(), today),
    )
    refreshed = pregame.refresh_cards(due, events, cfg, totals_prefer=prefer)
    report.update({"refreshed": len(refreshed), "markets": list(markets)})
    log.info("pre-game %s: %s", sport, report)
    return report, refreshed


# ---------------------------------------------------------------- 盯盤
def run_sensor(
    settings: Settings | None,
    now: datetime | None = None,
    client: OddsIoClient | None = None,
    hold: bool = False,
) -> dict[str, Any]:
    """One 盯盤 round (sensor.py; sensor.yml every ten minutes): read the two odds-api.io
    bookmakers for the watched matches, within the free key's quota, and return the
    competitions whose matches moved (`buy`) for `pipeline radar --only`. `hold`: a
    daily-group task is running or waiting, so nothing is asked for (the move is still
    there next round). Nothing is bought here and nothing goes into the repo: the state
    stays in .sensor/ (the Actions cache); status.json there holds counts, no prices."""
    now = now or datetime.now(UTC)
    report: dict[str, Any] = {"at": now.isoformat(timespec="seconds"), "buy": []}
    if not settings or not settings.odds_api_io_key:
        report["skipped"] = "no ODDS_API_IO_KEY"
        return report
    path = SENSOR_DIR / "state.json"
    state = sensor_mod.load_state(path)
    cards: list[tuple[str, dict[str, Any]]] = []
    football = _latest_snapshot("football")
    for comp in (football or {}).get("competitions", []):
        cards += [("football", c) for c in comp.get("cards", [])]
    for snap in _latest_snapshots("mlb", 2)[::-1]:  # newest first: it wins a duplicate
        cards += [("mlb", g) for g in snap.get("games", [])]
    for snap in _latest_snapshots("nba", 2)[::-1]:
        cards += [("nba", g) for g in snap.get("games", [])]
    watch = sensor_mod.watched(cards, now)
    client = client or OddsIoClient(settings.odds_api_io_key)
    budget = sensor_mod.allowance(state["usage"], now)
    mapped: dict[str, str] = {}
    readable: list[sensor_mod.Watch] = []
    readings = extra = 0
    try:
        if watch and budget and sensor_mod.books_due(state, now):
            state["books"] = client.selected_books()
            state["books_at"] = report["at"]
        mapped = sensor_mod.match_events(watch, sensor_mod.cached_events(state))
        # Lists cover the window for as long as they are kept (EVENTS_TTL).
        since = now - sensor_mod.MATCH_TOLERANCE
        until = now + timedelta(hours=sensor_mod.WINDOW_HOURS) + sensor_mod.EVENTS_TTL
        for league in sensor_mod.stale_leagues(watch, state, mapped, now):
            sport, slugs = IO_LEAGUES[league]
            if client.requests + len(slugs) > budget:
                break
            try:
                events = [e for slug in slugs for e in client.events(sport, slug, since, until)]
            except requests.HTTPError as exc:
                # One league the API does not know (a wrong slug, an off-season playoff
                # slug) must not stop the other leagues' readings: it is asked again
                # when its list is stale, like a league with no events.
                log.warning("盯盤: %s events unavailable (%s)", league, type(exc).__name__)
                events = []
            sensor_mod.store_events(state, league, events, now)
        mapped = sensor_mod.match_events(watch, sensor_mod.cached_events(state))
        readable = [w for w in watch if w.key in mapped]
        last = sensor_mod.last_readings(state)
        population = sensor_mod.watched(cards, now, hours=sensor_mod.FORECAST_HOURS, need_tw=False)
        due_keys = {w.key for w in sensor_mod.due(readable, last, now)}
        must = min(-(-len(due_keys) // MAX_IDS), budget - client.requests)
        plan = sensor_mod.forecast(population, now)
        spare = sensor_mod.spare_requests(state["usage"], now, plan, client.requests, must)
        report["spare"] = spare
        for chunk in sensor_mod.reads(readable, last, now, budget - client.requests, spare):
            left = client.remaining_this_hour
            if not state["books"] or (left is not None and left <= sensor_mod.HOUR_MARGIN):
                break
            found = {
                p.event_id: p for p in client.odds([mapped[w.key] for w in chunk], state["books"])
            }
            for w in chunk:
                prices = found.get(mapped[w.key])
                probs = sensor_mod.two_book_probs(prices, w.sides) if prices else None
                if probs:
                    state["readings"].append({"at": report["at"], "key": w.key, "p": probs})
                    readings += 1
                    extra += w.key not in due_keys
    except requests.RequestException as exc:
        # The type only: the request URL carries the key.
        log.warning("盯盤: odds-api.io unavailable (%s); stopping this round", type(exc).__name__)
        report["error"] = type(exc).__name__
    state["usage"] = sensor_mod.record_usage(state["usage"], client.requests, now)
    sensor_mod.prune(state, now)
    upcoming_read = sensor_mod.next_due(readable, sensor_mod.last_readings(state), now)
    report["next_due_at"] = upcoming_read.isoformat(timespec="seconds") if upcoming_read else None
    moving = sensor_mod.moved(watch, state["readings"], now, state["triggers"])
    buy = sensor_mod.purchases(moving)
    if buy and hold:
        report["held_busy"] = buy
        buy = []
    if buy:
        # Only what The Odds API's budget can pay for today (the pre-game extra included).
        odds_budget = budget_mod.load(ODDS_BUDGET_PATH, now.date())
        credits = budget_mod.allowance_today(
            odds_budget, now.date(), extra=budget_mod.PREGAME_EXTRA
        )
        affordable: list[str] = []
        for league in buy:
            cost = sum(sensor_mod.purchase_cost(x) for x in [*affordable, league])
            if credits is None or cost <= credits:
                affordable.append(league)
        if len(affordable) < len(buy):
            report["held_budget"] = [x for x in buy if x not in affordable]
        buy = affordable
    sensor_mod.record_triggers(state, buy, now)
    report.update(
        {
            "buy": buy,
            "moving": len(moving),
            "watched": len(watch),
            "mapped": len(mapped),
            "unmapped": [f"{w.league} {w.home} vs {w.away}" for w in watch if w.key not in mapped],
            "readings": readings,
            "extra_readings": extra,
            "requests": client.requests,
            "requests_24h": sensor_mod.used(state["usage"], now),
            "remaining_this_hour": client.remaining_this_hour,
        }
    )
    sensor_mod.save_state(path, state)
    (SENSOR_DIR / "status.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    log.info("盯盤: %s", {k: v for k, v in report.items() if k != "unmapped"})
    return report


# ---------------------------------------------------------------- 延遲雷達
def run_radar(
    cfg: ModelConfig = MODEL,
    settings: Settings | None = None,
    now: datetime | None = None,
    only: set[str] | None = None,
) -> dict[str, Any]:
    """延遲雷達's own purchase, rather than on an upload: fresh international prices,
    essential markets only, within the budget plus the pre-game extra. `only` (from the
    盯盤 sensor): the competitions (PL ... CL, MLB, NBA) whose matches moved, all their
    matches with a 運彩 price within the sensor's window; without it (a manual run),
    the matches starting within radar_window_hours whose price is old
    (pregame.radar_due). The render that follows sets the new prices against the last
    uploaded 運彩 prices (moves.radar). Pending 運彩 bets get their closing-line value
    from them first, as after a pre-game refresh."""
    now = now or datetime.now(UTC)
    reports: dict[str, Any] = {}

    def pick(cards: list[dict[str, Any]], sport: str) -> list[dict[str, Any]]:
        if only is None:
            return pregame.radar_due(cards, now, cfg)
        end = now + timedelta(hours=sensor_mod.WINDOW_HOURS)
        out = []
        for c in cards:
            start = pregame.kickoff(c)
            league = {"mlb": "MLB", "nba": "NBA"}.get(sport) or str(c.get("competition", ""))
            if c.get("tw") and start is not None and now < start <= end and league in only:
                out.append(c)
        return out

    football = _latest_snapshot("football")
    if football:
        _backfill_odds_at(football)
        cards = [c for comp in football["competitions"] for c in comp["cards"]]
        due = pick(cards, "football")
        rep: dict[str, Any] = {"due": len(due), "refreshed": 0}
        refreshed: list[dict[str, Any]] = []
        if due:
            rep, refreshed = _buy_fresh("football", due, settings, now, cfg, rep, True)
        if refreshed:
            tw_ledger.update_clv(TW_LEDGER_PATH, cards, "football", _implied_calibration(), now=now)
            moves_mod.record(MOVES_DIR, moves_mod.intl_rows(refreshed, "football"))
            _write_snapshot(date.fromisoformat(football["date"]), "football", football)
        reports["football"] = rep

    # As in run_tw: the next US date's snapshot, plus the unstarted games of the one before.
    mlb_snaps = _latest_snapshots("mlb", 2)
    if mlb_snaps:
        cards = list(mlb_snaps[-1].get("games", []))
        seen = {str(c.get("game_pk")) for c in cards}
        for older in mlb_snaps[:-1]:
            cards += [c for c in older.get("games", []) if str(c.get("game_pk")) not in seen]
        for snap in mlb_snaps:
            _backfill_odds_at(snap)
        due = pick(cards, "mlb")
        rep, refreshed = {"due": len(due), "refreshed": 0}, []
        if due:
            rep, refreshed = _buy_fresh("mlb", due, settings, now, cfg, rep, True)
        if refreshed:
            for card in refreshed:
                card["t10"] = g_value_block(card["market"], cfg)
                card["notes"] = [n for n in card.get("notes", []) if "賠率" not in n]
                # Judged against the last upload: "不判斷" when that is hours away.
                card["t10v"] = t10_value_block(card, cfg)
            tw_ledger.update_clv(TW_LEDGER_PATH, cards, "mlb", now=now)
            moves_mod.record(MOVES_DIR, moves_mod.intl_rows(refreshed, "mlb"))
            for snap in mlb_snaps:
                _write_snapshot(date.fromisoformat(snap["date"]), "mlb", snap)
        reports["mlb"] = rep

    nba_snaps = _latest_snapshots("nba", 2)
    if nba_snaps:
        cards = _games_of(nba_snaps, "game_id")
        for snap in nba_snaps:
            _backfill_odds_at(snap)
        due = pick(cards, "nba")
        rep, refreshed = {"due": len(due), "refreshed": 0}, []
        if due:
            rep, refreshed = _buy_fresh("nba", due, settings, now, cfg, rep, True)
        if refreshed:
            for card in refreshed:
                card["shape"] = shape_block(card["market"], cfg)
                card["notes"] = [n for n in card.get("notes", []) if "賠率" not in n]
            tw_ledger.update_clv(TW_LEDGER_PATH, cards, "nba", now=now)
            moves_mod.record(MOVES_DIR, moves_mod.intl_rows(refreshed, "nba"))
            for snap in nba_snaps:
                _write_snapshot(date.fromisoformat(snap["date"]), "nba", snap)
        reports["nba"] = rep
    log.info("radar: %s", reports)
    return reports


def _github_output(name: str, value: Any) -> None:
    """A step output for the workflow (sensor.yml: `buy` starts the purchase job, whose
    commit follows only when something was bought, `refreshed`)."""
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{name}={value}\n")


def _backfill_odds_at(snapshot: dict[str, Any]) -> None:
    """Snapshots made before consensus blocks carried their own time: the snapshot's
    time is when its fresh blocks were bought. Carried-over (stale) ones stay unstamped."""
    made = snapshot.get("generated_at")
    if not made:
        return
    cards = [c for comp in snapshot.get("competitions", []) for c in comp.get("cards", [])]
    cards += list(snapshot.get("games", []))
    for card in cards:
        for block in (card.get("market") or {}).values():
            if isinstance(block, dict) and not block.get("stale") and "odds_at" not in block:
                block["odds_at"] = made


# --------------------------------------------------------------------- review
# A match is looked up for its review this long after kickoff: a football match ends
# about 1 h 55 min after kickoff, an MLB game 2 h 40 min to 3 h 15 min after first
# pitch (the result services mark it finished minutes later; a game not final yet is
# simply asked again next time).
FOOTBALL_REVIEW_AFTER_HOURS = 2.0
MLB_REVIEW_AFTER_HOURS = 2.5
# An NBA game runs 2 h 15 min to 2 h 40 min from tip-off (overtime included).
NBA_REVIEW_AFTER_HOURS = 2.5
# The 盯盤 rounds ask for a review only this long after kickoff: a postponed or abandoned
# match must not start a run every few minutes. Later ones wait for the routine reviews.
QUICK_REVIEW_HOURS = {"football": 6.0, "mlb": 9.0, "nba": 7.0}
# The result services key a game by its US date; every first pitch / tip-off is
# 16:00-05:00 UTC, so six hours back always lands on it.
US_DATE_SHIFT = timedelta(hours=6)


def _review_times(now: datetime) -> list[tuple[str, datetime, datetime]]:
    """Matches without a review that kicked off at most QUICK_REVIEW_HOURS ago or are
    still to come: (key, when to look the result up, when to stop asking). The same
    pre-kickoff cards run_review reviews, or a match it can never review (first seen
    after kickoff) would be asked for every round. Read from the checkout only."""
    done = {
        "football": {r["match_id"] for r in review_store.read(FOOTBALL_REVIEWS_PATH)},
        "mlb": {r["game_pk"] for r in review_store.read(MLB_REVIEWS_PATH)},
        "nba": {r["game_id"] for r in review_store.read(NBA_REVIEWS_PATH)},
    }
    after = {
        "football": FOOTBALL_REVIEW_AFTER_HOURS,
        "mlb": MLB_REVIEW_AFTER_HOURS,
        "nba": NBA_REVIEW_AFTER_HOURS,
    }
    cards_of = {
        "football": lambda s: [c for comp in s.get("competitions", []) for c in comp["cards"]],
        "mlb": lambda s: list(s.get("games", [])),
        "nba": lambda s: list(s.get("games", [])),
    }
    out = []
    for sport, key in (("football", "match_id"), ("mlb", "game_pk"), ("nba", "game_id")):
        pre = review.collect_prematch(_recent_snapshots(sport, now.date(), 2), cards_of[sport], key)
        for mid, entry in pre.items():
            try:
                kickoff = _dt_utc(entry["card"]["kickoff_utc"])
            except (KeyError, ValueError):
                continue
            stop = kickoff + timedelta(hours=QUICK_REVIEW_HOURS[sport])
            if mid in done[sport] or stop <= now:
                continue
            out.append((f"{sport}:{mid}", kickoff + timedelta(hours=after[sport]), stop))
    return out


def review_due(now: datetime) -> list[str]:
    """Matches that should have finished and have no review yet, kicked off at most
    QUICK_REVIEW_HOURS ago: what a 盯盤 round hands to `review --new-only` (sensor.yml),
    so a review follows a match within minutes, not at the next routine run. Read from
    the checkout only (snapshots and review CSVs): no request."""
    return sorted(key for key, at, _ in _review_times(now) if at <= now)


def review_next(now: datetime) -> datetime | None:
    """When the next match reaches its review time, if one is still to come."""
    return min((at for _, at, _ in _review_times(now) if at > now), default=None)


def run_review(
    settings: Settings | None,
    now: datetime | None = None,
    days: int = 14,
    new_only: bool = False,
) -> dict[str, Any]:
    """Post-match review: finished matches from the last `days` days of snapshots.

    Football rows are rebuilt every run (football-data.co.uk statistics arrive a few
    days after the match); MLB rows are written once, when the game is final. With
    `new_only` (the run after a phone upload) only matches without a review row are
    looked up, so an upload costs no football-data.org request unless one has finished.
    The finished matches also settle both ledgers here and each 模型分歧 gets its
    closing line, so every recommendation on the page is reviewed with its match.
    Costs no Odds API credits: football-data.org results + the free MLB Stats API.
    """
    now = now or datetime.now(UTC)
    report: dict[str, Any] = {}

    snaps = _recent_snapshots("football", now.date(), days)
    pre = review.collect_prematch(
        snaps,
        lambda s: [c for comp in s.get("competitions", []) for c in comp.get("cards", [])],
        "match_id",
    )
    due = {k: e for k, e in pre.items() if _kicked_off(e["card"], now, FOOTBALL_REVIEW_AFTER_HOURS)}
    if new_only:
        have = {r["match_id"] for r in review_store.read(FOOTBALL_REVIEWS_PATH)}
        due = {k: e for k, e in due.items() if k not in have}
    rows: list[dict[str, Any]] = []
    if due and settings and settings.football_data_token:
        fd = FootballDataClient(settings.football_data_token)
        results: dict[str, Match] = {}
        for code in sorted({str(e["card"].get("competition")) for e in due.values()}):
            for m in fd.results(code):
                results[str(m.id)] = m
        stats = load_stats(
            HISTORY_DIR, [c.fdcouk_code for c in FOOTBALL_COMPETITIONS if c.fdcouk_code]
        )
        signals: dict[str, dict[str, str]] = {}
        for r in sorted(ledger_mod.read(LEDGER_PATH), key=lambda r: r["created_at"]):
            if r["sport"] == "football":
                signals.setdefault(r["match_id"], r)
        for k, e in due.items():
            m = results.get(k)
            if m is None or not m.is_finished:
                continue
            card = e["card"]
            st = find_stats(
                stats, card.get("home_key", ""), card.get("away_key", ""), m.utc_date.date()
            )
            rows.append(review.football_row(e, m, st, signals.get(k), now))
        added, updated = review_store.upsert(
            FOOTBALL_REVIEWS_PATH, rows, review_store.FOOTBALL_COLUMNS, "match_id"
        )
        finished = [results[k] for k in due if k in results and results[k].is_finished]
        settled = ledger_mod.settle_football(LEDGER_PATH, finished)
        settled += tw_ledger.settle(
            TW_LEDGER_PATH,
            {
                str(m.id): (m.home_goals or 0, m.away_goals or 0, m.home_ht, m.away_ht)
                for m in finished
            },
            "football",
        )
        closed = _close_signals({str(m.id): due[str(m.id)] for m in finished})
        report["football"] = {
            "due": len(due),
            "added": added,
            "updated": updated,
            "settled": settled,
            "signals_closed": closed,
        }
    log.info("review football: %s", report.get("football", {"due": len(due)}))

    msnaps = _recent_snapshots("mlb", now.date(), days)
    mpre = review.collect_prematch(msnaps, lambda s: s.get("games", []), "game_pk")
    done = {r["game_pk"] for r in review_store.read(MLB_REVIEWS_PATH)}
    mdue = {
        k: e
        for k, e in mpre.items()
        if k not in done and _kicked_off(e["card"], now, MLB_REVIEW_AFTER_HOURS)
    }
    mrows: list[dict[str, Any]] = []
    if mdue:
        client = MlbClient()
        finals = {}
        for day in sorted(
            {_dt_utc(e["card"]["kickoff_utc"]) - US_DATE_SHIFT for e in mdue.values()}
        ):
            for g in client.schedule(day.date()):
                finals[str(g.game_pk)] = g
        for k, e in mdue.items():
            g = finals.get(k)
            if g is None or not g.is_final:
                continue
            mrows.append(review.mlb_row(e, g, client.box(g.game_pk), now))
        added, _ = review_store.upsert(MLB_REVIEWS_PATH, mrows, review_store.MLB_COLUMNS, "game_pk")
        settled = tw_ledger.settle(
            TW_LEDGER_PATH,
            {k: (g.home_score or 0, g.away_score or 0) for k, g in finals.items() if g.is_final},
            "mlb",
        )
        report["mlb"] = {"due": len(mdue), "added": added, "settled": settled}
    log.info("review mlb: %s", report.get("mlb", {"due": len(mdue)}))

    nsnaps = _recent_snapshots("nba", now.date(), days)
    npre = review.collect_prematch(nsnaps, lambda s: s.get("games", []), "game_id")
    ndone = {r["game_id"] for r in review_store.read(NBA_REVIEWS_PATH)}
    ndue = {
        k: e
        for k, e in npre.items()
        if k not in ndone and _kicked_off(e["card"], now, NBA_REVIEW_AFTER_HOURS)
    }
    nrows: list[dict[str, Any]] = []
    if ndue:
        client = NbaClient()
        nfinals = {}
        for day in sorted(
            {_dt_utc(e["card"]["kickoff_utc"]) - US_DATE_SHIFT for e in ndue.values()}
        ):
            try:
                for g in client.scoreboard(day.date()):
                    nfinals[str(g.game_id)] = g
            except requests.RequestException as exc:
                # ESPN's feed is public, not promised: the games are asked again next run.
                log.warning(
                    "nba: scoreboard for %s unavailable (%s)", day.date(), type(exc).__name__
                )
        for k, e in ndue.items():
            g = nfinals.get(k)
            if g is None or not g.is_final:
                continue
            nrows.append(review.nba_row(e, g, now))
        added, _ = review_store.upsert(NBA_REVIEWS_PATH, nrows, review_store.NBA_COLUMNS, "game_id")
        settled = tw_ledger.settle(
            TW_LEDGER_PATH,
            {k: (g.home_score or 0, g.away_score or 0) for k, g in nfinals.items() if g.is_final},
            "nba",
        )
        report["nba"] = {"due": len(ndue), "added": added, "settled": settled}
    log.info("review nba: %s", report.get("nba", {"due": len(ndue)}))
    return report


def review_changed(report: Mapping[str, Any]) -> bool:
    """Did a review run write anything: a review row, a settled bet, a closed signal?"""
    return any(
        (report.get(sport) or {}).get(key)
        for sport in ("football", "mlb", "nba")
        for key in ("added", "updated", "settled", "signals_closed")
    )


def _close_signals(entries: dict[str, dict[str, Any]]) -> int:
    """Give each 模型分歧 of these finished matches its closing line: the selection's fair
    probability in the last consensus before kickoff and close_p x price - 1 (CLV)."""
    if not entries:
        return 0
    calib = _implied_calibration()
    rows = ledger_mod.read(LEDGER_PATH)
    filled = 0
    for r in rows:
        entry = entries.get(str(r["match_id"]))
        if r["sport"] != "football" or entry is None or r.get("close_p"):
            continue
        line = float(r["line"]) if r.get("line") else None
        p = review.closing_prob(entry.get("close"), r["selection_key"], line, calib)
        if p is None:
            continue
        r["close_p"] = f"{p:.4f}"
        price = float(r["price"]) if r.get("price") else None
        r["clv"] = f"{p * price - 1:.4f}" if price else ""
        filled += 1
    if filled:
        ledger_mod.write(LEDGER_PATH, rows)
    return filled


def _implied_calibration() -> Calibration:
    """Per-market corrections for the market-implied model (identity if never fitted)."""
    try:
        return Calibration.from_json(IMPLIED_CALIBRATION_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return IDENTITY


def _recent_snapshots(sport: str, today: date, days: int) -> list[dict[str, Any]]:
    out = []
    for path in sorted(SNAPSHOT_DIR.glob(f"*/{sport}.json")):
        try:
            day = date.fromisoformat(path.parent.name)
        except ValueError:
            continue
        if today - timedelta(days=days) <= day <= today + timedelta(days=1):
            out.append(json.loads(path.read_text(encoding="utf-8")))
    return out


def _parse_utc(value: Any) -> datetime | None:
    try:
        return _dt_utc(str(value))
    except ValueError:
        return None


def _dt_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _kicked_off(card: dict[str, Any], now: datetime, hours: float) -> bool:
    """True once a match has surely finished (kickoff + `hours`)."""
    try:
        return _dt_utc(card["kickoff_utc"]) + timedelta(hours=hours) <= now
    except (KeyError, ValueError):
        return False


# -------------------------------------------------------------------- render
def render_latest() -> Path:
    """Every page of the site (render/site.py) from the latest snapshots, the tape and the
    CSVs; returns docs/index.html. The 大戶 record is brought up to date first."""
    now = datetime.now(UTC)
    football = _latest_snapshot("football")
    mlb = _games_for_page("mlb", "game_pk", now)
    nba = _games_for_page("nba", "game_id", now)
    football_reviews = review_store.read(FOOTBALL_REVIEWS_PATH)
    mlb_reviews = review_store.read(MLB_REVIEWS_PATH)
    nba_reviews = review_store.read(NBA_REVIEWS_PATH)
    signal_rows = ledger_mod.read(LEDGER_PATH)
    value_rows = tw_ledger.read(TW_LEDGER_PATH)
    # The whole tape: every page's price history and the 大戶 record draw from it.
    tape = moves_mod.read(MOVES_DIR, months=None)
    scores = {("football", str(r["match_id"])): r for r in football_reviews}
    scores.update({("mlb", str(r["game_pk"])): r for r in mlb_reviews})
    scores.update({("nba", str(r["game_id"])): r for r in nba_reviews})
    # 運彩's Chinese names and the competition, as the snapshots of the last two weeks knew
    # them: kept in the 大戶 record and shown on the history of every page.
    names = _match_names(
        [s for s in (football, mlb, nba) if s]
        + _recent_snapshots("football", now.date(), 14)[::-1]
        + _recent_snapshots("mlb", now.date(), 14)[::-1]
        + _recent_snapshots("nba", now.date(), 14)[::-1]
    )
    history = moves_mod.event_history(
        _record_bighand(tape, names),
        tape,
        _final_scores(scores),
        value_rows,
        now,
    )
    fade_summary, fade_live, fade_record = _fade_data(
        football, mlb, football_reviews, mlb_reviews, signal_rows, now
    )
    site = Site(
        now=now,
        football=football,
        mlb=mlb,
        nba=nba,
        tape=tape,
        football_reviews=football_reviews,
        mlb_reviews=mlb_reviews,
        nba_reviews=nba_reviews,
        signal_rows=signal_rows,
        value_rows=value_rows,
        history=history,
        history_summary=moves_mod.history_summary(history),
        radar=_radar(tape, now, football, mlb, names, nba),
        tracked=moves_mod.track(tape, MODEL),
        fade_summary=fade_summary,
        fade_live=fade_live,
        fade_record=fade_record,
        ledger_summary=ledger_mod.summary(signal_rows),
        tw_summary=tw_ledger.summary(value_rows),
        budget=_budget_view(now.date()),
        backtest_total=_backtest_total(),
        backtest_exists=(DOCS_DIR / "backtest.html").exists(),
        names=names,
        reports=_reports(),
        started=_started_today(now),
    )
    return render_site(site, DOCS_DIR)["index"]


def _started_today(now: datetime) -> list[dict[str, Any]]:
    """Cards of the matches that kicked off since midnight in Taipei, from the snapshots
    of the last two days (the newest copy wins). A started MLB game of the previous US
    date is in no list the page otherwise reads, and its review comes hours later: the
    front page and the reviews page keep the day's schedule whole with these."""
    since = now.astimezone(LOCAL_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for sport in ("football", "mlb", "nba"):
        for snap in _recent_snapshots(sport, now.date(), 2):  # oldest first
            if sport == "football":
                cards = [c for comp in snap.get("competitions", []) for c in comp["cards"]]
            else:
                cards = list(snap.get("games", []))
            for card in cards:
                start = pregame.kickoff(card)
                if start is not None and since <= start <= now:
                    out[(sport, _card_id(card))] = card
    return sorted(out.values(), key=lambda c: str(c.get("kickoff_utc")))


def _reports() -> dict[str, dict[str, Any]]:
    """The backtest summaries whose pages the site re-renders (render.site.REPORTS)."""
    out = {}
    for name, (file, _, _) in REPORTS.items():
        try:
            out[name] = json.loads((DATA_DIR / file).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
    return out


def render_safely() -> Path | None:
    """render_latest for the CLI. Every task ends with a render after its data is written
    (odds bought, an upload compared, a review made); a page that fails to render must not
    fail the task, or the workflow never commits that data. The error goes to the log and,
    as an annotation, to the run's summary in GitHub Actions; the pages keep their last
    version until the next render."""
    try:
        out = render_latest()
    except Exception:  # any rendering bug: the data step already succeeded
        log.exception("render failed; the pages keep their previous version")
        print("::error title=render failed::the pages were not updated; the data was saved")
        return None
    log.info("rendered %s", out)
    return out


def _backtest_total() -> dict[str, Any] | None:
    """The football model's backtest headline (data/backtest_summary.json), if any."""
    path = DATA_DIR / "backtest_summary.json"
    if not path.exists():
        return None
    try:
        bt = json.loads(path.read_text(encoding="utf-8"))
        key = bt.get("strategies", {}).get("blend_pinnacle", {}).get("0.03", {})
        return {
            "verdict": bt.get("verdict"),
            "passed": bt.get("passed", False),
            "n": key.get("n"),
            "roi": key.get("roi"),
            "clv": key.get("clv"),
            "generated": bt.get("generated"),
        }
    except (OSError, ValueError, AttributeError):
        return None


def _radar(
    tape: list[dict[str, str]],
    now: datetime,
    football: dict[str, Any] | None,
    mlb: dict[str, Any] | None,
    names: dict[tuple[str, str], dict[str, str]] | None = None,
    nba: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """延遲雷達 for the front page: what the tape says now (moves.radar), the names 運彩
    uses and each match's league (`names`: the last two weeks of snapshots, so a match a
    newer snapshot left out still says which league it is), and when to upload so that
    the coming games get judged with fresh prices."""
    found = moves_mod.radar(tape, now, MODEL)
    starts = {
        "football": [
            pregame.kickoff(c)
            for comp in (football or {}).get("competitions", [])
            for c in comp.get("cards", [])
        ],
        "mlb": [pregame.kickoff(g) for g in (mlb or {}).get("games", [])],
        "nba": [pregame.kickoff(g) for g in (nba or {}).get("games", [])],
    }
    last_upload = {
        sport: moves_mod.parse_time(found["last"].get(sport, {}).get("tw")) for sport in starts
    }
    plan = pregame.upload_plan(
        {sport: [s for s in ks if s is not None] for sport, ks in starts.items()},
        last_upload,
        now,
        MODEL,
    )
    if names is None:
        names = _match_names([s for s in (football, mlb, nba) if s])
    return {"found": found, "names": names, "plan": plan}


def _fade_data(
    football: dict[str, Any] | None,
    mlb: dict[str, Any] | None,
    football_reviews: list[dict[str, str]],
    mlb_reviews: list[dict[str, str]],
    signal_rows: list[dict[str, str]],
    now: datetime,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], list[dict[str, Any]]]:
    """反者道之動: the backtest summary (scripts/backtest_fade.py), today's games where a
    consistently bad strategy points one way, and the forward record of the opposites."""
    summary = None
    try:
        summary = json.loads((DATA_DIR / "fade_summary.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        log.info("fade: no backtest summary yet (scripts/backtest_fade.py)")

    def upcoming(card: dict[str, Any]) -> bool:
        start = pregame.kickoff(card)
        return start is not None and start > now

    comps = (football or {}).get("competitions", [])
    live = fade_mod.live_candidates(
        [c for comp in comps for c in comp.get("cards", []) if upcoming(c)],
        [s for comp in comps for s in comp.get("signals", [])],
        [g for g in (mlb or {}).get("games", []) if upcoming(g)],
        MODEL,
    )
    record = fade_mod.fade_record(football_reviews, mlb_reviews, signal_rows)
    return summary, live, record


def _record_bighand(
    tape: list[dict[str, str]],
    names: dict[tuple[str, str], dict[str, str]],
) -> list[dict[str, str]]:
    """Every 大戶 move the tape shows (運彩-only moves, international 大單, 延遲雷達
    findings) goes into data/moves/events.csv once, with the competition and 運彩's
    Chinese team names known now, whether or not it ever became a bet; returns the whole
    record. Done at render time because every task ends with a render, whatever it added
    to the tape."""
    detected = (
        moves_mod.tw_divergences(tape, MODEL)
        + moves_mod.intl_big_moves(tape, MODEL)
        + moves_mod.radar_events(tape, MODEL)
    )
    path = MOVES_DIR / moves_mod.EVENTS_FILE
    n = moves_mod.record_events(path, detected, names)
    if n:
        log.info("大戶紀錄: %d new moves recorded", n)
    return moves_mod.read_events(path)


def _match_names(snapshots: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, str]]:
    """(sport, match id) -> competition and 運彩's Chinese team names, from snapshots
    given newest first (the first value found wins)."""
    out: dict[tuple[str, str], dict[str, str]] = {}
    for snap in snapshots:
        cards = [c for comp in snap.get("competitions", []) for c in comp.get("cards", [])]
        cards += list(snap.get("games", []))
        for c in cards:
            sport = _card_sport(c)
            tw = c.get("tw") or {}
            have = out.setdefault(
                (sport, _card_id(c)), {"competition": "", "home_zh": "", "away_zh": ""}
            )
            found = {
                "competition": c.get("competition_name") or GAME_LEAGUES.get(sport, ""),
                "home_zh": tw.get("home_zh") or "",
                "away_zh": tw.get("away_zh") or "",
            }
            for k, v in found.items():
                have[k] = have[k] or v
    return out


# Sports whose snapshot is a list of games rather than competitions, and the league
# name their cards carry on the site.
GAME_LEAGUES = {"mlb": "MLB", "nba": "NBA"}


def _card_sport(card: Mapping[str, Any]) -> str:
    """The sport a card belongs to (older MLB snapshots predate the `sport` field)."""
    sport = str(card.get("sport") or "")
    if sport:
        return sport
    return "mlb" if card.get("game_pk") else "football"


def _card_id(card: Mapping[str, Any]) -> str:
    """A card's id in its sport: football match id, MLB game_pk, NBA game_id."""
    return str(card.get("match_id") or card.get("game_pk") or card.get("game_id") or "")


def _final_scores(
    rows: dict[tuple[str, str], dict[str, str]],
) -> dict[tuple[str, str], tuple[int, int]]:
    """Final scores from the post-match review rows (football full time, MLB final)."""
    out = {}
    for key, r in rows.items():
        h, a = (
            (r.get("ft_h"), r.get("ft_a"))
            if key[0] == "football"
            else (r.get("final_h"), r.get("final_a"))  # MLB and NBA alike
        )
        try:
            out[key] = (int(float(h)), int(float(a)))
        except (TypeError, ValueError):
            continue
    return out


def _games_for_page(sport: str, key: str, now: datetime) -> dict[str, Any] | None:
    """The newest MLB / NBA snapshot plus the games of the one before that have not
    started.

    The morning run already holds the next US date, so without this today's later
    first pitches / tip-offs (and their pre-game comparisons) would drop off the page
    at 08:15.
    """
    snaps = _latest_snapshots(sport, 2)
    if not snaps:
        return None
    latest = snaps[-1]
    games = list(latest.get("games", []))
    seen = {str(g.get(key)) for g in games}
    for older in snaps[:-1]:
        for g in older.get("games", []):
            start = pregame.kickoff(g)
            if str(g.get(key)) not in seen and start is not None and start > now:
                games.append(g)
    games.sort(key=lambda g: str(g.get("kickoff_utc", "")))
    return {**latest, "games": games}


def _budget_view(today: date) -> dict[str, Any] | None:
    state = budget_mod.load(ODDS_BUDGET_PATH, today)
    if state.remaining is None:
        return None
    return {"remaining": state.remaining, "daily": round(budget_mod.daily_share(state, today))}


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


def _latest_snapshots(sport: str, n: int) -> list[dict[str, Any]]:
    """The newest `n` snapshots of a sport, oldest first."""
    paths = sorted(SNAPSHOT_DIR.glob(f"*/{sport}.json"))[-n:]
    return [json.loads(p.read_text(encoding="utf-8")) for p in paths]


# ----------------------------------------------------------------------- CLI
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sports_analyzer.pipeline")
    parser.add_argument(
        "task",
        choices=[
            "football",
            "mlb",
            "nba",
            "tw",
            "review",
            "radar",
            "sensor",
            "render",
            "test-notify",
        ],
    )
    parser.add_argument(
        "--days", type=int, default=None, help="fixture window in days (default from env or 2)"
    )
    parser.add_argument(
        "--date", type=str, default=None, help="run as if today were YYYY-MM-DD (UTC)"
    )
    parser.add_argument(
        "--markets",
        type=str,
        default=None,
        help="comma-separated odds markets, e.g. h2h (football default h2h,totals; "
        "mlb and nba default h2h,spreads,totals)",
    )
    parser.add_argument(
        "--only",
        type=str,
        default=None,
        help="radar: only these competitions, e.g. PL,SA,MLB,NBA (what the 盯盤 sensor saw move)",
    )
    parser.add_argument(
        "--hold",
        action="store_true",
        help="sensor: read, but ask for no purchase (another task is running)",
    )
    parser.add_argument(
        "--offline", action="store_true", help="skip the network and just re-render"
    )
    parser.add_argument(
        "--new-only",
        action="store_true",
        help="review: only matches without a review yet (the quick run after an upload)",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )

    today = date.fromisoformat(args.date) if args.date else datetime.now(UTC).date()
    if args.task == "render" or args.offline:
        render_safely()
        return 0
    if args.task == "sensor":
        # Every few minutes (sensor.yml): two bookmakers read; the competitions that
        # moved, and whether a match has just finished, go to the workflow, whose next
        # job buys them (radar --only) and reviews (review --new-only).
        report = run_sensor(Settings.from_env(offline=True), hold=args.hold)
        _github_output("buy", ",".join(report.get("buy", [])))
        now = datetime.now(UTC)
        finished = [] if args.hold else review_due(now)
        log.info("盯盤: %d finished match(es) waiting for a review", len(finished))
        _github_output("review", len(finished) or "")
        # Minutes to the next useful round (a reading or a review falling due): the job
        # waits for it rather than for GitHub's next scheduled start.
        soon = [t for t in (_parse_utc(report.get("next_due_at")), review_next(now)) if t]
        wait = math.ceil((min(soon) - now).total_seconds() / 60) if soon else None
        _github_output("next", max(1, wait) if wait is not None else "")
        return 0
    if args.task == "radar":
        # After the sensor saw a move (sensor.yml, --only) or by hand: fresh prices, then
        # the last upload is re-judged and the page shows what the new prices say.
        settings = Settings.from_env(offline=True)
        only = {x.strip() for x in args.only.split(",") if x.strip()} if args.only else None
        reports = run_radar(settings=settings, only=only)
        bought = sum(r.get("refreshed", 0) for r in reports.values())
        _github_output("refreshed", bought)
        if not bought:
            log.info("radar: nothing bought, nothing to compare")
            return 0
        run_tw(settings=settings)
        render_safely()
        return 0
    if args.task == "test-notify":
        # One test message, so the owner can check the phone side of NTFY_TOPIC.
        topic = Settings.from_env(offline=True).ntfy_topic
        if not topic:
            raise SystemExit("NTFY_TOPIC is not set (GitHub: Settings > Secrets > Actions)")
        ok = notify.send(
            topic, "比賽分析：推播測試", "設定成功！有新的運彩價值注時，會像這樣通知你。"
        )
        log.info("test notification %s", "sent" if ok else "NOT sent")
        return 0 if ok else 1
    if args.task == "tw":
        # The phone already uploaded the feed. The Odds API key is optional: with it,
        # games about to start get fresh international prices first (budget allowing).
        run_tw(settings=Settings.from_env(offline=True))
        render_safely()
        return 0

    if args.task == "review":
        # Needs only the football-data.org token (results); the MLB Stats API has no key.
        settings = Settings.from_env(offline=True)
        if not settings.football_data_token:
            raise SystemExit("Missing required environment variable: FOOTBALL_DATA_TOKEN")
        report = run_review(settings, new_only=args.new_only)
        # The 盯盤 rounds ask for a quick review every ten minutes until a result is in (an
        # MLB game often runs past its review time): one that found nothing final writes
        # nothing, so no render and nothing for the workflow to commit.
        if args.new_only and not review_changed(report):
            log.info("review: nothing new is final yet, nothing to render")
            return 0
        render_safely()
        return 0

    settings = Settings.from_env()
    if args.days is not None:
        settings = replace(settings, fixture_window_days=args.days)
    chosen = tuple(m.strip() for m in args.markets.split(",")) if args.markets else None
    if args.task == "football":
        run_football(settings, today, markets=chosen or FOOTBALL_MARKETS)
    elif args.task == "mlb":
        local_today = datetime.now(LOCAL_TZ).date() if not args.date else today
        run_mlb(local_today, settings, markets=chosen or MLB_MARKETS)
    elif args.task == "nba":
        local_today = datetime.now(LOCAL_TZ).date() if not args.date else today
        run_nba(local_today, settings, markets=chosen or NBA_MARKETS)
    render_safely()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
