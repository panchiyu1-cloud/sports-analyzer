"""Daily pipeline entry point.

    python -m sports_analyzer.pipeline football [--days 2] [--date YYYY-MM-DD]
    python -m sports_analyzer.pipeline mlb
    python -m sports_analyzer.pipeline review           # post-match review of finished games
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
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import requests

from . import budget as budget_mod
from . import ledger as ledger_mod
from . import moves as moves_mod
from . import notify, review_store, tw_ledger
from .analysis import pregame, review
from .analysis.football import build_cards
from .analysis.mlb import build_cards as build_mlb_cards
from .analysis.mlb import g_value_block, t10_value_block
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
    ODDS_BUDGET_PATH,
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
from .fetch.odds import FOOTBALL_MARKETS, MLB_MARKETS, OddsClient, OddsEvent
from .history import find_stats, load_history, load_stats
from .models import dixon_coles as dc
from .models.implied import IDENTITY, Calibration
from .render.cards import render_index, render_reviews_page, taipei_time

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
    today_local: date, settings: Settings | None = None, cfg: ModelConfig = MODEL
) -> dict[str, Any]:
    """Today's MLB games (Taipei date) with market consensus, fair odds and the T-10 G value."""
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
        MLB_MARKETS,
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
    """Carry market blocks and 運彩 comparisons from an earlier same-date MLB snapshot."""
    if not previous or previous.get("date") != snapshot.get("date"):
        return
    old_cards = {str(c.get("game_pk")): c for c in previous.get("games", [])}
    for card in snapshot["games"]:
        old = old_cards.get(str(card.get("game_pk")))
        if not old:
            continue
        if card.get("market") is None and old.get("market"):
            # Per-block "stale" flags: update_clv() must not use a carried-over
            # price as the closing price (CLV would then just equal EV).
            card["market"] = {
                k: ({**v, "stale": True} if isinstance(v, dict) else v)
                for k, v in old["market"].items()
            }
            card["t10"] = old.get("t10")
            bought = (old["market"].get("h2h") or {}).get("odds_at")
            card["notes"] = [
                f"國際價沿用 {taipei_time(bought)} 的報價（這次沒有更新：預算不足或比賽已開打）"
                if bought
                else "國際價為稍早快照（這次沒有更新：預算不足或比賽已開打）"
            ]
        if old.get("tw") and not card.get("tw"):
            card["tw"] = old["tw"]


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
            tw_ledger.update_clv(TW_LEDGER_PATH, cards, "football", calib)
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
            tw_ledger.update_clv(TW_LEDGER_PATH, cards, "mlb")
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
    if new_values and settings and settings.ntfy_topic:
        tracked = moves_mod.track(moves_mod.read(MOVES_DIR), cfg)
        notify.notify_value_bets(settings.ntfy_topic, new_values, tracked)
    return reports


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
    """Buy fresh international prices for the cards about to start (see analysis/pregame).

    One request per sport key (MLB, or each football competition with a due match),
    h2h + spreads (+ totals) for MLB and h2h (+ totals) for football, within the
    budget's allowance plus the pre-game extra. Returns a small report and the
    refreshed cards.
    """
    due = pregame.due_cards(cards, now, cfg)
    report: dict[str, Any] = {"due": len(due), "refreshed": 0}
    if not due:
        return report, []
    if not pregame.upload_is_recent(captured, now):
        report["skipped"] = "upload not recent"
        return report, []
    if not settings or not settings.odds_api_key:
        report["skipped"] = "no API key"
        return report, []
    groups: dict[str, list[dict[str, Any]]] = {}
    if sport == "mlb":
        groups[MLB_ODDS_KEY] = due
        wanted, essential, prefer = MLB_MARKETS, ("h2h", "spreads"), None
    else:
        for card in due:
            comp = COMPETITION_BY_CODE.get(str(card.get("competition")))
            if comp:
                groups.setdefault(comp.odds_key, []).append(card)
        wanted, essential, prefer = FOOTBALL_MARKETS, ("h2h",), 2.5
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
            log.warning("pre-game odds unavailable (%s); keeping earlier prices", exc)
            break
    budget_mod.save(
        ODDS_BUDGET_PATH,
        budget_mod.record(state, odds.credits_used(), odds.credits_remaining(), today),
    )
    refreshed = pregame.refresh_cards(due, events, cfg, totals_prefer=prefer)
    report.update({"refreshed": len(refreshed), "markets": list(markets)})
    log.info("pre-game %s: %s", sport, report)
    return report, refreshed


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
def run_review(
    settings: Settings | None, now: datetime | None = None, days: int = 14
) -> dict[str, Any]:
    """Post-match review: finished matches from the last `days` days of snapshots.

    Football rows are rebuilt every run (football-data.co.uk statistics arrive a few
    days after the match); MLB rows are written once, when the game is final.
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
    due = {k: e for k, e in pre.items() if _kicked_off(e["card"], now, hours=2)}
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
        report["football"] = {"due": len(due), "added": added, "updated": updated}
    log.info("review football: %s", report.get("football", {"due": len(due)}))

    msnaps = _recent_snapshots("mlb", now.date(), days)
    mpre = review.collect_prematch(msnaps, lambda s: s.get("games", []), "game_pk")
    done = {r["game_pk"] for r in review_store.read(MLB_REVIEWS_PATH)}
    mdue = {k: e for k, e in mpre.items() if k not in done and _kicked_off(e["card"], now, hours=4)}
    mrows: list[dict[str, Any]] = []
    if mdue:
        client = MlbClient()
        finals = {}
        # The schedule is keyed by the US date; every first pitch is 16:00-05:00 UTC, so
        # six hours back always lands on it.
        for day in sorted(
            {_dt_utc(e["card"]["kickoff_utc"]) - timedelta(hours=6) for e in mdue.values()}
        ):
            for g in client.schedule(day.date()):
                finals[str(g.game_pk)] = g
        for k, e in mdue.items():
            g = finals.get(k)
            if g is None or not g.is_final:
                continue
            mrows.append(review.mlb_row(e, g, client.box(g.game_pk), now))
        added, _ = review_store.upsert(MLB_REVIEWS_PATH, mrows, review_store.MLB_COLUMNS, "game_pk")
        report["mlb"] = {"due": len(mdue), "added": added}
    log.info("review mlb: %s", report.get("mlb", {"due": len(mdue)}))
    return report


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
    now = datetime.now(UTC)
    football = _latest_snapshot("football")
    mlb = _mlb_for_page(now)
    summary = ledger_mod.summary(ledger_mod.read(LEDGER_PATH))
    backtest_path = DATA_DIR / "backtest_summary.json"
    backtest_total = None
    if backtest_path.exists():
        try:
            bt = json.loads(backtest_path.read_text(encoding="utf-8"))
            key = bt.get("strategies", {}).get("blend_pinnacle", {}).get("0.03", {})
            backtest_total = {
                "verdict": bt.get("verdict"),
                "passed": bt.get("passed", False),
                "n": key.get("n"),
                "roi": key.get("roi"),
                "clv": key.get("clv"),
                "generated": bt.get("generated"),
            }
        except (OSError, ValueError, AttributeError):
            backtest_total = None
    football_reviews = review_store.read(FOOTBALL_REVIEWS_PATH)
    mlb_reviews = review_store.read(MLB_REVIEWS_PATH)
    render_reviews_page(football_reviews, mlb_reviews, now, DOCS_DIR / "reviews.html")
    tape = moves_mod.read(MOVES_DIR)
    scores = {("football", str(r["match_id"])): r for r in football_reviews}
    scores.update({("mlb", str(r["game_pk"])): r for r in mlb_reviews})
    events = moves_mod.settle(moves_mod.tw_divergences(tape, MODEL), _final_scores(scores))
    return render_index(
        football,
        mlb,
        summary,
        now,
        DOCS_DIR / "index.html",
        backtest_exists=(DOCS_DIR / "backtest.html").exists(),
        backtest_total=backtest_total,
        tw_summary=tw_ledger.summary(tw_ledger.read(TW_LEDGER_PATH)),
        football_reviews=football_reviews,
        mlb_reviews=mlb_reviews,
        odds_budget=_budget_view(now.date()),
        moves=moves_mod.track(tape, MODEL),
        tw_moves={"events": events, "summary": moves_mod.divergence_summary(events)},
    )


def _final_scores(
    rows: dict[tuple[str, str], dict[str, str]],
) -> dict[tuple[str, str], tuple[int, int]]:
    """Final scores from the post-match review rows (football full time, MLB final)."""
    out = {}
    for key, r in rows.items():
        h, a = (
            (r.get("ft_h"), r.get("ft_a"))
            if key[0] == "football"
            else (r.get("final_h"), r.get("final_a"))
        )
        try:
            out[key] = (int(float(h)), int(float(a)))
        except (TypeError, ValueError):
            continue
    return out


def _mlb_for_page(now: datetime) -> dict[str, Any] | None:
    """The newest MLB snapshot plus the games of the one before that have not started.

    The morning run already holds the next US date, so without this today's later
    first pitches (and their pre-game comparisons) would drop off the page at 08:15.
    """
    snaps = _latest_snapshots("mlb", 2)
    if not snaps:
        return None
    latest = snaps[-1]
    games = list(latest.get("games", []))
    seen = {str(g.get("game_pk")) for g in games}
    for older in snaps[:-1]:
        for g in older.get("games", []):
            start = pregame.kickoff(g)
            if str(g.get("game_pk")) not in seen and start is not None and start > now:
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
        "task", choices=["football", "mlb", "tw", "review", "render", "test-notify"]
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
        help="comma-separated football odds markets, e.g. h2h (default h2h,totals)",
    )
    parser.add_argument(
        "--offline", action="store_true", help="skip the network and just re-render"
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
        out = render_latest()
        log.info("rendered %s", out)
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
        out = render_latest()
        log.info("rendered %s", out)
        return 0

    if args.task == "review":
        # Needs only the football-data.org token (results); the MLB Stats API has no key.
        settings = Settings.from_env(offline=True)
        if not settings.football_data_token:
            raise SystemExit("Missing required environment variable: FOOTBALL_DATA_TOKEN")
        run_review(settings)
        out = render_latest()
        log.info("rendered %s", out)
        return 0

    settings = Settings.from_env()
    if args.days is not None:
        settings = replace(settings, fixture_window_days=args.days)
    markets = (
        tuple(m.strip() for m in args.markets.split(",")) if args.markets else FOOTBALL_MARKETS
    )
    if args.task == "football":
        run_football(settings, today, markets=markets)
    elif args.task == "mlb":
        run_mlb(datetime.now(LOCAL_TZ).date() if not args.date else today, settings)
    out = render_latest()
    log.info("rendered %s", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
