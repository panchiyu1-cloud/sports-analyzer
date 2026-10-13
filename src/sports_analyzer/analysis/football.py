"""Build football cards: fixture + model prediction + market consensus -> dict.

The dict layout is the contract with the HTML template and the ledger, so it is
documented here once:

card = {
  sport, competition, competition_name, kickoff_utc, kickoff_local,
  home, away, home_key, away_key,
  model: {fitted, data_ok, lambda_home, mu_away, p_home, p_draw, p_away,
          over25, most_likely, ranks..., matches...},
  form: {home, away}, table: {home: {...}, away: {...}}, h2h: [...],
  market: {books, h2h: {home|draw|away: {fair, best, book}}, totals: {...}, spread: {...}},
  edges: {home, draw, away, over, under, spread_home, spread_away},
  recommendation: {...} | None,
  notes: [str],
}
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime, timedelta
from typing import Any

from ..config import LOCAL_TZ, Competition, ModelConfig
from ..fetch.football import Match, TableRow
from ..fetch.odds import OddsEvent
from ..history import HistoricalMatch
from ..models import market
from ..models.dixon_coles import FittedModel, Prediction
from ..teams import best_match

log = logging.getLogger(__name__)

WEEKDAYS_ZH = "一二三四五六日"
SELECTION_LABELS = {
    "home": "主勝",
    "draw": "和局",
    "away": "客勝",
    "over": "大分",
    "under": "小分",
    "spread_home": "主讓",
    "spread_away": "客受讓",
}


# ------------------------------------------------------------------ odds matching
def match_odds(fixture: Match, events: list[OddsEvent], tolerance_hours: float = 12) -> OddsEvent | None:
    """Find the odds event for a fixture: same two teams, kickoff within tolerance."""
    window = timedelta(hours=tolerance_hours)
    candidates = [e for e in events if abs(e.commence_time - fixture.utc_date) <= window]
    for e in candidates:
        if e.home_key == fixture.home_key and e.away_key == fixture.away_key:
            return e
    # Fuzzy fallback: canonical keys differ slightly (new team, unusual spelling).
    home_keys = [e.home_key for e in candidates]
    away_keys = [e.away_key for e in candidates]
    h = best_match(fixture.home, home_keys)
    a = best_match(fixture.away, away_keys)
    for e in candidates:
        if e.home_key == h and e.away_key == a:
            log.info("fuzzy-matched odds for %s vs %s -> %s vs %s", fixture.home, fixture.away, e.home, e.away)
            return e
    return None


def market_view(event: OddsEvent, method: str, totals_prefer: float | None = 2.5) -> dict[str, Any]:
    """Consensus for the match-winner market (1X2, or two-way when no draw is quoted),
    the main totals line and the main spread line."""
    view: dict[str, Any] = {"books": len(event.books), "h2h": None, "totals": None, "spread": None}

    three_way = any("draw" in b.h2h for b in event.books)
    if three_way:
        outcomes: tuple[str, ...] = ("home", "draw", "away")
    else:
        outcomes = ("home", "away")
    h2h_quotes = [
        (b.book, [b.h2h[o] for o in outcomes])
        for b in event.books
        if set(outcomes) <= b.h2h.keys()
    ]
    c = market.consensus(outcomes, h2h_quotes, method)
    if c:
        view["h2h"] = {o: _side(c, o) for o in c.outcomes}

    totals_line = _main_line([t.point for b in event.books for t in b.totals], prefer=totals_prefer)
    if totals_line is not None:
        quotes = [
            (b.book, [t.over_price, t.under_price])
            for b in event.books
            for t in b.totals
            if t.point == totals_line
        ]
        c = market.consensus(("over", "under"), quotes, method)
        if c:
            view["totals"] = {"line": totals_line, **{o: _side(c, o) for o in c.outcomes}}

    spread_line = _main_line([s.point for b in event.books for s in b.spreads], prefer=None)
    if spread_line is not None:
        quotes = [
            (b.book, [s.home_price, s.away_price])
            for b in event.books
            for s in b.spreads
            if s.point == spread_line
        ]
        c = market.consensus(("home", "away"), quotes, method)
        if c:
            view["spread"] = {"line": spread_line, **{o: _side(c, o) for o in c.outcomes}}
    return view


def _side(c: market.Consensus, outcome: str) -> dict[str, Any]:
    """Fair probability, the fair decimal odds it implies, and the best price quoted."""
    fair = c.prob(outcome)
    return {
        "fair": round(fair, 4),
        "fair_odds": round(1.0 / fair, 2) if fair > 0 else None,
        "best": c.price(outcome),
        "book": c.book(outcome),
    }


def _main_line(points: list[float], prefer: float | None) -> float | None:
    """The line most books quote; ties broken by closeness to `prefer`."""
    if not points:
        return None
    counts = Counter(points)
    top = max(counts.values())
    tied = [p for p, n in counts.items() if n == top]
    if prefer is None:
        return sorted(tied, key=abs)[0]
    return sorted(tied, key=lambda p: abs(p - prefer))[0]


# ---------------------------------------------------------------------- cards
def build_cards(
    comp: Competition,
    fixtures: list[Match],
    model: FittedModel | None,
    standings: list[TableRow],
    results: list[Match],
    history: list[HistoricalMatch],
    odds_events: list[OddsEvent],
    cfg: ModelConfig,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (cards, signals) for one competition."""
    table = {row.team_key: row for row in standings}
    cards: list[dict[str, Any]] = []
    signals: list[dict[str, Any]] = []
    for fx in sorted(fixtures, key=lambda m: m.utc_date):
        card = _base_card(comp, fx)
        notes: list[str] = []
        if comp.is_cup:
            # The pooled model links leagues only through the few cross-league matches,
            # so club-vs-club comparisons across leagues are much noisier than domestic ones.
            notes.append("跨聯賽合併模型：可靠度低於聯賽內比賽，回測尚未涵蓋")

        pred = _predict(model, fx, cfg, notes)
        card["model"] = _model_block(model, pred, fx, cfg)
        card["form"] = {
            "home": table[fx.home_key].form if fx.home_key in table else "",
            "away": table[fx.away_key].form if fx.away_key in table else "",
        }
        card["table"] = {
            "home": _table_block(table.get(fx.home_key)),
            "away": _table_block(table.get(fx.away_key)),
        }
        card["h2h"] = _head_to_head(fx, results, history)

        event = match_odds(fx, odds_events)
        card["market"] = market_view(event, cfg.devig_method) if event else None
        if event is None:
            notes.append("找不到對應賠率（可能尚未開盤）")

        card["edges"] = _edges(pred, card["market"]) if pred and card["market"] else {}
        card["recommendation"] = _recommend(card, pred, cfg) if card["edges"] else None
        card["notes"] = notes
        cards.append(card)
        if card["recommendation"]:
            signals.append({**_signal_header(card), **card["recommendation"]})
    return cards, signals


def _base_card(comp: Competition, fx: Match) -> dict[str, Any]:
    local = fx.utc_date.astimezone(LOCAL_TZ)
    return {
        "sport": "football",
        "competition": comp.code,
        "competition_name": comp.name,
        "match_id": fx.id,
        "kickoff_utc": fx.utc_date.isoformat(),
        "kickoff_local": f"{local.month}/{local.day} (週{WEEKDAYS_ZH[local.weekday()]}) {local:%H:%M}",
        "matchday": fx.matchday,
        "home": fx.home,
        "away": fx.away,
        "home_key": fx.home_key,
        "away_key": fx.away_key,
    }


def _predict(model: FittedModel | None, fx: Match, cfg: ModelConfig, notes: list[str]) -> Prediction | None:
    if model is None:
        notes.append("本聯賽資料不足，尚未建立模型")
        return None
    try:
        pred = model.predict(fx.home_key, fx.away_key)
    except KeyError as exc:
        notes.append(f"模型沒有這支球隊的資料：{exc}")
        return None
    for key, name in ((fx.home_key, fx.home), (fx.away_key, fx.away)):
        n = model.matches_per_team.get(key, 0)
        if n < cfg.min_team_matches:
            notes.append(f"資料不足：{name} 僅 {n} 場（建議 ≥ {cfg.min_team_matches}）")
    return pred


def _model_block(model: FittedModel | None, pred: Prediction | None, fx: Match, cfg: ModelConfig) -> dict[str, Any]:
    if model is None or pred is None:
        return {"fitted": False, "data_ok": False}
    hm = model.matches_per_team.get(fx.home_key, 0)
    am = model.matches_per_team.get(fx.away_key, 0)
    i, j, p = pred.most_likely_score()
    return {
        "fitted": True,
        "data_ok": hm >= cfg.min_team_matches and am >= cfg.min_team_matches,
        "n_matches": model.n_matches,
        "home_matches": hm,
        "away_matches": am,
        "n_teams": len(model.teams),
        "lambda_home": round(pred.lambda_home, 2),
        "mu_away": round(pred.mu_away, 2),
        "p_home": round(pred.p_home, 4),
        "p_draw": round(pred.p_draw, 4),
        "p_away": round(pred.p_away, 4),
        "over25": round(pred.over_prob(2.5), 4),
        "most_likely": f"{i}-{j}（{p:.0%}）",
        "home_attack_rank": model.attack_rank(fx.home_key),
        "home_defence_rank": model.defence_rank(fx.home_key),
        "away_attack_rank": model.attack_rank(fx.away_key),
        "away_defence_rank": model.defence_rank(fx.away_key),
        "home_advantage": round(model.home_advantage, 3),
    }


def _table_block(row: TableRow | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "position": row.position,
        "played": row.played,
        "won": row.won,
        "drawn": row.drawn,
        "lost": row.lost,
        "gf": row.goals_for,
        "ga": row.goals_against,
        "points": row.points,
    }


def _head_to_head(fx: Match, results: list[Match], history: list[HistoricalMatch], limit: int = 4) -> list[dict[str, Any]]:
    pair = {fx.home_key, fx.away_key}
    rows: list[tuple[datetime, str, str, int, int]] = []
    for r in results:
        if r.is_finished and {r.home_key, r.away_key} == pair:
            rows.append((r.utc_date, r.home, r.away, r.home_goals or 0, r.away_goals or 0))
    seen_days = {r[0].date() for r in rows}
    for h in history:
        if {h.home, h.away} == pair and h.day not in seen_days:
            rows.append((datetime.combine(h.day, datetime.min.time(), tzinfo=fx.utc_date.tzinfo), h.home, h.away, h.home_goals, h.away_goals))
    rows.sort(key=lambda r: r[0], reverse=True)
    return [
        {"date": r[0].strftime("%Y-%m-%d"), "home": r[1], "away": r[2], "score": f"{r[3]}-{r[4]}"}
        for r in rows[:limit]
    ]


def _edges(pred: Prediction, mv: dict[str, Any]) -> dict[str, float]:
    edges: dict[str, float] = {}
    if mv.get("h2h"):
        for o, p in (("home", pred.p_home), ("draw", pred.p_draw), ("away", pred.p_away)):
            edges[o] = round(market.edge(p, mv["h2h"][o]["fair"]), 4)
    if mv.get("totals"):
        line = mv["totals"]["line"]
        over = pred.over_prob(line)
        edges["over"] = round(market.edge(over, mv["totals"]["over"]["fair"]), 4)
        edges["under"] = round(market.edge(1 - over, mv["totals"]["under"]["fair"]), 4)
    if mv.get("spread"):
        line = mv["spread"]["line"]
        try:
            cover = pred.home_cover_prob(line)
        except ValueError:
            return edges
        edges["spread_home"] = round(market.edge(cover, mv["spread"]["home"]["fair"]), 4)
        edges["spread_away"] = round(market.edge(1 - cover, mv["spread"]["away"]["fair"]), 4)
    return edges


def _recommend(card: dict[str, Any], pred: Prediction | None, cfg: ModelConfig) -> dict[str, Any] | None:
    """Pick the single best edge that clears the thresholds; None if nothing does.

    A team the model has seen fewer than `min_team_matches` times (typical for
    Champions League clubs outside the top five leagues) gets no signal at all:
    the first real run showed the model inventing 97% favourites from one match.
    """
    if pred is None or not card["model"].get("data_ok"):
        return None
    mv = card["market"]
    best: dict[str, Any] | None = None
    for key, e in card["edges"].items():
        model_p, market_side, mkt_name, line = _lookup(key, pred, mv)
        if market_side is None or model_p < cfg.min_model_prob:
            continue
        conf = market.confidence(e, cfg.edge_threshold, cfg.edge_mid, cfg.edge_high)
        if conf is None:
            continue
        candidate = {
            "market": mkt_name,
            "selection_key": key,
            "selection": _selection_text(card, key, line),
            "line": line,
            "model_p": round(model_p, 4),
            "market_p": market_side["fair"],
            "price": market_side["best"],
            "book": market_side["book"],
            "edge": e,
            "ev": round(market.expected_value(model_p, market_side["best"]), 4),
            "kelly": round(market.kelly_fraction(model_p, market_side["best"]), 4),
            "confidence": conf,
        }
        if best is None or candidate["edge"] > best["edge"]:
            best = candidate
    return best


def _lookup(key: str, pred: Prediction, mv: dict[str, Any]) -> tuple[float, dict[str, Any] | None, str, float | None]:
    if key in ("home", "draw", "away"):
        p = {"home": pred.p_home, "draw": pred.p_draw, "away": pred.p_away}[key]
        return p, mv["h2h"][key], "1X2", None
    if key in ("over", "under"):
        line = mv["totals"]["line"]
        over = pred.over_prob(line)
        return (over if key == "over" else 1 - over), mv["totals"][key], "大小", line
    if key in ("spread_home", "spread_away"):
        line = mv["spread"]["line"]
        cover = pred.home_cover_prob(line)
        side = "home" if key == "spread_home" else "away"
        return (cover if side == "home" else 1 - cover), mv["spread"][side], "讓球", line
    return 0.0, None, "", None


def _selection_text(card: dict[str, Any], key: str, line: float | None) -> str:
    label = SELECTION_LABELS[key]
    if key == "home":
        return f"{label} {card['home']}"
    if key == "away":
        return f"{label} {card['away']}"
    if key in ("over", "under"):
        return f"{label} {line}"
    if key == "spread_home":
        return f"{card['home']} {line:+g}"
    if key == "spread_away":
        return f"{card['away']} {-line:+g}"
    return label


def _signal_header(card: dict[str, Any]) -> dict[str, Any]:
    return {
        "sport": card["sport"],
        "competition": card["competition"],
        "competition_name": card["competition_name"],
        "match_id": card["match_id"],
        "kickoff_utc": card["kickoff_utc"],
        "kickoff_local": card["kickoff_local"],
        "home": card["home"],
        "away": card["away"],
    }
