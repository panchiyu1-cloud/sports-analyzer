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
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from . import ledger as ledger_mod
from . import review_store, tw_ledger
from .analysis import review
from .analysis.football import build_cards
from .analysis.mlb import build_cards as build_mlb_cards
from .analysis.taiwan import annotate as annotate_tw
from .config import (
    DATA_DIR,
    DOCS_DIR,
    FOOTBALL_COMPETITIONS,
    FOOTBALL_REVIEWS_PATH,
    HISTORY_DIR,
    LEDGER_PATH,
    LOCAL_TZ,
    MLB_ODDS_KEY,
    MLB_REVIEWS_PATH,
    MODEL,
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
from .fetch.odds import DEFAULT_MARKETS, OddsClient, OddsEvent
from .history import find_stats, load_history, load_stats
from .models import dixon_coles as dc
from .render.cards import render_index, render_reviews_page

log = logging.getLogger("sports_analyzer")


# ------------------------------------------------------------------- football
def run_football(
    settings: Settings,
    today: date,
    cfg: ModelConfig = MODEL,
    markets: tuple[str, ...] = DEFAULT_MARKETS,
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

    # 3. Odds only for competitions with fixtures (each call costs credits).
    window_start = datetime.combine(today, datetime.min.time(), tzinfo=UTC)
    window_end = window_start + timedelta(days=settings.fixture_window_days + 1)
    odds_events: dict[str, list[OddsEvent]] = {}
    for comp in FOOTBALL_COMPETITIONS:
        if not fixtures[comp.code]:
            odds_events[comp.code] = []
            continue
        odds_events[comp.code] = odds.odds(
            comp.odds_key, commence_from=window_start, commence_to=window_end, markets=markets
        )

    # 4. Cards + signals.
    snapshot: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "date": today.isoformat(),
        "credits_remaining": odds.credits_remaining(),
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

    # 4b. A cheap evening run (h2h only) keeps the morning's totals/spreads so the page
    #     does not lose them; those blocks are marked stale and never used for CLV.
    if set(markets) != set(DEFAULT_MARKETS):
        _merge_previous_markets(snapshot, _latest_snapshot("football"))

    # 5. Ledgers: add today's signals, settle anything that has finished (both ledgers).
    finished = [m for rs in results.values() for m in rs if m.is_finished]
    added = ledger_mod.append_signals(LEDGER_PATH, all_signals)
    settled = ledger_mod.settle_football(LEDGER_PATH, finished)
    tw_settled = tw_ledger.settle(
        TW_LEDGER_PATH,
        {str(m.id): (m.home_goals or 0, m.away_goals or 0) for m in finished},
        "football",
    )
    # A later consensus for the same matches is the closing proxy for 運彩 bets taken earlier.
    all_cards = [c for comp in snapshot["competitions"] for c in comp["cards"]]
    clv_updated = tw_ledger.update_clv(TW_LEDGER_PATH, all_cards, "football")
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


def _merge_previous_markets(snapshot: dict[str, Any], previous: dict[str, Any] | None) -> None:
    """Carry totals/spread consensus from the previous snapshot into cards that lack them."""
    if not previous or previous.get("date") != snapshot.get("date"):
        return
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
            for key in ("totals", "spread"):
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
    if games and settings and settings.odds_api_key:
        odds = OddsClient(settings.odds_api_key, regions=settings.odds_regions)
        # The MLB "date" is a US date; its games start the next Taipei morning, so the
        # window has to follow the actual first pitches, not the Taipei calendar day.
        first = min(g.game_date for g in games) - timedelta(hours=1)
        last = max(g.game_date for g in games) + timedelta(hours=1)
        events = odds.odds(MLB_ODDS_KEY, commence_from=first, commence_to=last)
        credits = odds.credits_remaining()
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
            card["notes"] = ["賠率為稍早快照（比賽可能已開打）"]
        if old.get("tw") and not card.get("tw"):
            card["tw"] = old["tw"]


# ------------------------------------------------------------------ 運彩
def run_tw(cfg: ModelConfig = MODEL) -> dict[str, Any]:
    """Compare the newest 運彩 upload with the newest snapshots; record value bets."""
    reports: dict[str, Any] = {}
    for src, dst in taiwan.normalize_uploads(TW_DIR):
        log.info("tw: renamed upload %s -> %s", src.relative_to(TW_DIR), dst.relative_to(TW_DIR))
    football = _latest_snapshot("football")
    games, path, parse_report = taiwan.load_latest(TW_DIR, "football")
    if football and path:
        cards = [c for comp in football["competitions"] for c in comp["cards"]]
        rep = annotate_tw(cards, games, taiwan.captured_at(path), cfg)
        rep.update(
            {
                "file": path.name,
                "unknown_teams": sorted(parse_report.unknown_teams),
                "unknown_markets": parse_report.unknown_markets,
            }
        )
        football["tw_report"] = rep
        rep["values_added"] = tw_ledger.append_values(TW_LEDGER_PATH, cards, "football")
        _write_snapshot(date.fromisoformat(football["date"]), "football", football)
        reports["football"] = rep
        log.info("tw football: %s", {k: v for k, v in rep.items() if k not in ("unmatched_games",)})
    elif path is None:
        log.info("tw football: no upload found under %s", TW_DIR / "football")

    mlb = _latest_snapshot("mlb")
    games, path, parse_report = taiwan.load_latest(TW_DIR, "baseball")
    if mlb and path:
        cards = mlb["games"]
        rep = annotate_tw(cards, games, taiwan.captured_at(path), cfg)
        rep.update(
            {
                "file": path.name,
                "unknown_teams": sorted(parse_report.unknown_teams),
                "unknown_markets": parse_report.unknown_markets,
            }
        )
        mlb["tw_report"] = rep
        rep["values_added"] = tw_ledger.append_values(TW_LEDGER_PATH, cards, "mlb")
        _write_snapshot(date.fromisoformat(mlb["date"]), "mlb", mlb)
        reports["mlb"] = rep
        log.info("tw mlb: %s", {k: v for k, v in rep.items() if k not in ("unmatched_games",)})
    return reports


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
    football = _latest_snapshot("football")
    mlb = _latest_snapshot("mlb")
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
    now = datetime.now(UTC)
    football_reviews = review_store.read(FOOTBALL_REVIEWS_PATH)
    mlb_reviews = review_store.read(MLB_REVIEWS_PATH)
    render_reviews_page(football_reviews, mlb_reviews, now, DOCS_DIR / "reviews.html")
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
    parser.add_argument("task", choices=["football", "mlb", "tw", "review", "render"])
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
        help="comma-separated odds markets, e.g. h2h (default h2h,spreads,totals)",
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
    if args.task == "tw":
        # Needs no network: the phone already uploaded the feed.
        run_tw()
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
        settings = Settings(
            settings.football_data_token, settings.odds_api_key, settings.odds_regions, args.days
        )
    markets = tuple(m.strip() for m in args.markets.split(",")) if args.markets else DEFAULT_MARKETS
    if args.task == "football":
        run_football(settings, today, markets=markets)
    elif args.task == "mlb":
        run_mlb(datetime.now(LOCAL_TZ).date() if not args.date else today, settings)
    out = render_latest()
    log.info("rendered %s", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
