"""Render the snapshot dicts into docs/index.html."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from ..analysis import review
from ..config import LOCAL_TZ, MODEL, ModelConfig

TEMPLATE_DIR = Path(__file__).parent / "templates"
# The front page shows the reviews of the last few days; reviews.html has all of them.
RECENT_REVIEW_DAYS = 3
RECENT_REVIEW_LIMIT = 12


def _env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(["html", "j2"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["tpe"] = taipei_time
    return env


def taipei_time(value: Any) -> str:
    """Any ISO time with an offset -> '10/14 20:25' in Taipei; '—' when unknown."""
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return "—"
    if dt.tzinfo is None:
        return "—"
    return dt.astimezone(LOCAL_TZ).strftime("%m/%d %H:%M")


def render_index(
    football: dict[str, Any] | None,
    mlb: dict[str, Any] | None,
    ledger_summary: dict[str, Any] | None,
    generated_at: datetime,
    out_path: Path,
    cfg: ModelConfig = MODEL,
    backtest_exists: bool = False,
    backtest_total: dict[str, Any] | None = None,
    tw_summary: dict[str, Any] | None = None,
    football_reviews: list[Mapping[str, Any]] | None = None,
    mlb_reviews: list[Mapping[str, Any]] | None = None,
    odds_budget: Mapping[str, Any] | None = None,
    moves: Mapping[tuple[str, str], Mapping[str, Any]] | None = None,
    tw_moves: Mapping[str, Any] | None = None,
) -> Path:
    """`moves` is moves.track() of the line-move tape; `tw_moves` holds the settled
    運彩-only moves and their summary (moves.tw_divergences / divergence_summary)."""
    template = _env().get_template("index.html.j2")
    moves = moves or {}

    # Matches that have kicked off leave the "upcoming" part of the page; once a result
    # is in they reappear under 賽後檢討 with the pre-match numbers next to the score.
    competitions: list[dict[str, Any]] = []
    signals: list[dict[str, Any]] = []
    started: list[dict[str, Any]] = []
    credits = None
    if football:
        credits = football.get("credits_remaining")
        for comp in football.get("competitions", []):
            upcoming = [
                {**c, "moves": moves.get(("football", str(c.get("match_id"))))}
                for c in comp.get("cards", [])
                if not _started(c, generated_at)
            ]
            started.extend(c for c in comp.get("cards", []) if _started(c, generated_at))
            competitions.append({**comp, "cards": upcoming})
            signals.extend(s for s in comp.get("signals", []) if not _started(s, generated_at))
    # Signals in a snapshot made before a rule change still obey today's rules.
    cards_by_id = {
        c.get("match_id"): c
        for comp in (football or {}).get("competitions", [])
        for c in comp.get("cards", [])
    }
    signals = [s for s in signals if _signal_allowed(s, cards_by_id.get(s.get("match_id")), cfg)]
    signals.sort(key=lambda s: s.get("kickoff_utc", ""))
    if mlb:
        started.extend(g for g in mlb.get("games", []) if _started(g, generated_at))
        mlb = {
            **mlb,
            "games": [
                {**g, "moves": moves.get(("mlb", str(g.get("game_pk"))))}
                for g in mlb.get("games", [])
                if not _started(g, generated_at)
            ],
        }
    reviews = review_views(football_reviews or [], mlb_reviews or [], generated_at)
    reviewed = {r["row"].get("match_id") for r in reviews["football"]} | {
        r["row"].get("game_pk") for r in reviews["mlb"]
    }
    waiting = [c for c in started if str(c.get("match_id") or c.get("game_pk")) not in reviewed]

    # 運彩 value bets across both sports, for the section at the top of the page.
    tw_values: list[dict[str, Any]] = []
    tw_reports: list[dict[str, Any]] = []
    for sport_name, snap, cards in (
        (
            "足球",
            football,
            [c for comp in (football or {}).get("competitions", []) for c in comp.get("cards", [])],
        ),
        ("MLB", mlb, (mlb or {}).get("games", [])),
    ):
        if snap and snap.get("tw_report"):
            tw_reports.append({"sport": sport_name, **snap["tw_report"]})
        for c in cards:
            tw = c.get("tw")
            if not tw:
                continue
            for v in tw.get("values", []):
                tw_values.append(
                    {
                        "sport": sport_name,
                        "competition_name": c.get("competition_name", "MLB"),
                        "kickoff_local": c.get("kickoff_local") or c.get("local"),
                        "kickoff_utc": c.get("kickoff_utc", ""),
                        "home": c["home"],
                        "away": c["away"],
                        "home_zh": tw.get("home_zh"),
                        "away_zh": tw.get("away_zh"),
                        "captured_at": tw.get("captured_at"),
                        **v,
                    }
                )
    tw_values.sort(key=lambda v: (-v["ev"], v["kickoff_utc"]))
    big_moves = big_move_rows(
        [("football", c) for comp in competitions for c in comp["cards"]]
        + [("mlb", g) for g in (mlb or {}).get("games", [])]
    )

    html = template.render(
        tw_values=tw_values,
        tw_reports=tw_reports,
        tw_summary=tw_summary,
        generated_local=generated_at.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M"),
        credits_remaining=credits,
        competitions=competitions,
        signals=signals,
        ledger=ledger_summary,
        mlb=mlb,
        cfg=cfg,
        backtest=backtest_exists,
        backtest_total=backtest_total,
        reviews=reviews,
        waiting=waiting,
        odds_budget=odds_budget,
        big_moves=big_moves,
        tw_events=[
            {
                **e,
                "label": side_label(
                    e["sport"], e["market"], e["side"], e["line"], e["home"], e["away"]
                ),
                "match": f"{e['home']} vs {e['away']}"
                if e["sport"] == "football"
                else f"{e['away']} @ {e['home']}",
            }
            for e in reversed((tw_moves or {}).get("events", []))
        ][:10],
        tw_moves_summary=(tw_moves or {}).get("summary") or {},
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    return out_path


def side_label(sport: str, market: str, side: str, line: str, home: str, away: str) -> str:
    """'主勝 Arsenal FC', '大 2.5', 'Los Angeles Dodgers -1.5' ..."""
    if market == "h2h":
        if sport == "mlb":
            return f"{home if side == 'home' else away} 不讓分"
        return {"home": f"主勝 {home}", "draw": "和局", "away": f"客勝 {away}"}.get(side, side)
    if market == "totals":
        return f"{'大' if side == 'over' else '小'} {line}"
    own = float(line) if side == "home" else -float(line)
    return f"{home if side == 'home' else away} {own:+g}"


def big_move_rows(cards: list[tuple[str, Mapping[str, Any]]]) -> list[dict[str, Any]]:
    """One row per side whose international price moved by the 大單 threshold, with what
    運彩 did over the same time and the EV of its current price for that side."""
    rows = []
    for sport, card in cards:
        mv = card.get("moves") or {}
        for e in mv.get("sides", []):
            if not e.get("big") or e["move"] <= 0:
                continue  # one row per move: the side the money went to
            tw_block = (card.get("tw") or {}).get(e["market"]) or {}
            tw_side = tw_block.get(e["side"]) if isinstance(tw_block, dict) else None
            same_line = e["market"] == "h2h" or str(tw_block.get("line", "")) in (e["line"], "")
            rows.append(
                {
                    **e,
                    "sport": sport,
                    "kickoff_utc": card.get("kickoff_utc", ""),
                    "kickoff_local": card.get("kickoff_local") or card.get("local"),
                    "match": f"{card['home']} vs {card['away']}"
                    if sport == "football"
                    else f"{card['away']} @ {card['home']}",
                    "label": side_label(
                        sport, e["market"], e["side"], e["line"], card["home"], card["away"]
                    ),
                    "tw_ev": tw_side.get("ev") if tw_side and same_line else None,
                    "tw_fresh": (card.get("tw") or {}).get("fair_fresh", True),
                }
            )
    rows.sort(key=lambda r: r["kickoff_utc"])
    return rows


def render_reviews_page(
    football_reviews: list[Mapping[str, Any]],
    mlb_reviews: list[Mapping[str, Any]],
    generated_at: datetime,
    out_path: Path,
) -> Path:
    """docs/reviews.html: every review, newest first, with the cumulative diagnostics."""
    views = review_views(football_reviews, mlb_reviews, generated_at, recent_only=False)
    html = (
        _env()
        .get_template("reviews.html.j2")
        .render(
            generated_local=generated_at.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M"),
            reviews=views,
        )
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    return out_path


def review_views(
    football_rows: list[Mapping[str, Any]],
    mlb_rows: list[Mapping[str, Any]],
    now: datetime,
    recent_only: bool = True,
) -> dict[str, Any]:
    """Explained review cards (newest first) and the cumulative summaries."""
    football = [
        {"row": row, **review.explain_football(row), **_odds_table(row)}
        for row in sorted(football_rows, key=lambda r: str(r.get("kickoff_utc")), reverse=True)
    ]
    mlb = [
        {"row": row, **review.explain_mlb(row)}
        for row in sorted(mlb_rows, key=lambda r: str(r.get("kickoff_utc")), reverse=True)
    ]
    if recent_only:
        football = _recent(football, now)
        mlb = _recent(mlb, now)
        # 模型分歧 matches first: they are the ones the owner was told to look at.
        football.sort(key=lambda v: v["row"].get("lean_source") != "signal")
    return {
        "football": football,
        "mlb": mlb,
        "summary": review.summarize_football(football_rows),
        "mlb_summary": review.summarize_mlb(mlb_rows),
        "total": len(football_rows) + len(mlb_rows),
    }


def _recent(views: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    cutoff = (now - timedelta(days=RECENT_REVIEW_DAYS)).isoformat()
    recent = [v for v in views if str(v["row"].get("kickoff_utc")) >= cutoff]
    return (recent or views[:6])[:RECENT_REVIEW_LIMIT]


def _odds_table(row: Mapping[str, Any]) -> dict[str, Any]:
    """Model / market / 運彩 per 1X2 outcome, with the actual result marked."""

    def pct(key: str) -> str:
        v = row.get(key)
        return f"{float(v) * 100:.0f}%" if v not in (None, "") else "—"

    moved = any(
        row.get(f"open_{s}") not in (None, "")
        and row.get(f"close_{s}") not in (None, "")
        and abs(float(row[f"open_{s}"]) - float(row[f"close_{s}"])) >= 0.01
        for s in "hda"
    )
    table = []
    for s, label, code in (("h", "主勝", "H"), ("d", "和局", "D"), ("a", "客勝", "A")):
        market = pct(f"open_{s}")
        if moved and row.get(f"close_{s}") not in (None, ""):
            market += f" → {pct(f'close_{s}')}"
        tw = row.get(f"tw_{s}")
        table.append(
            {
                "label": label,
                "model": pct(f"model_{s}") if str(row.get("model_ok")) == "1" else "—",
                "market": market,
                "tw": tw if tw not in (None, "") else "—",
                "hit": row.get("outcome") == code,
            }
        )
    has_any = any(t["model"] != "—" or t["market"] != "—" for t in table)
    return {"table": table if has_any else [], "moved": moved}


def _signal_allowed(
    signal: Mapping[str, Any], card: Mapping[str, Any] | None, cfg: ModelConfig
) -> bool:
    """The same rules analysis/football.py applies when it builds a card."""
    if signal.get("competition") == "CL" and not cfg.cup_signals:
        return False
    model = (card or {}).get("model") or {}
    counts = [model.get("home_matches"), model.get("away_matches")]
    return all(n is None or n >= cfg.min_signal_matches for n in counts)


def _started(item: Mapping[str, Any], now: datetime) -> bool:
    try:
        return datetime.fromisoformat(str(item.get("kickoff_utc"))) <= now
    except ValueError:
        return False
