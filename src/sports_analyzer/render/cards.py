"""Render the snapshot dicts into docs/index.html."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from ..analysis import fade, review
from ..analysis.mlb import G_SHAPES
from ..config import LOCAL_TZ, MODEL, ModelConfig
from ..models.market import t10_signal
from ..moves import FOLLOW_MIN, MARKET_SIDES

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
    bighand_matches: int = 0,
    signal_rows: list[Mapping[str, Any]] | None = None,
    value_rows: list[Mapping[str, Any]] | None = None,
    radar: Mapping[str, Any] | None = None,
) -> Path:
    """`moves` is moves.track() of the line-move tape; `tw_moves` holds the recorded
    運彩-only moves (oldest first, settled) and their summary; `bighand_matches` is how
    many matches the 大戶紀錄 page (docs/bighand.html) lists; `radar` is moves.radar()
    with 運彩's names and pregame.upload_plan() (延遲雷達)."""
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
    reviews = review_views(
        football_reviews or [], mlb_reviews or [], generated_at, True, signal_rows, value_rows
    )
    reviewed = {r["row"].get("match_id") for r in reviews["football"]} | {
        r["row"].get("game_pk") for r in reviews["mlb"]
    }
    recommended = match_picks(signal_rows or [], value_rows or [])
    waiting = [
        {
            **c,
            "recommended": (
                "mlb" if c.get("game_pk") else "football",
                str(c.get("match_id") or c.get("game_pk")),
            )
            in recommended,
        }
        for c in started
        if str(c.get("match_id") or c.get("game_pk")) not in reviewed
    ]

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
                "label": side_label(e["sport"], e["market"], e["side"], e["line"], *_names(e)),
                "match": match_title(e["sport"], *_names(e)),
            }
            for e in reversed((tw_moves or {}).get("events", []))
        ][:10],
        tw_moves_summary=(tw_moves or {}).get("summary") or {},
        bighand_matches=bighand_matches,
        radar=radar_view(radar, generated_at) if radar is not None else None,
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    return out_path


SPORT_LABELS = {"football": "足球", "mlb": "MLB"}
# The radar runs of radar.yml, in Taipei time, for the page's explanation.
RADAR_TIMES = "23:40、02:40、05:40"
WATCH_ROWS = 8


def _until(start: datetime | None, now: datetime) -> str:
    """'2 小時 10 分' until kickoff."""
    if start is None:
        return ""
    minutes = max(0, int((start - now).total_seconds() // 60))
    hours, rest = divmod(minutes, 60)
    return f"{hours} 小時 {rest} 分" if hours else f"{rest} 分"


def radar_view(radar: Mapping[str, Any], now: datetime) -> dict[str, Any]:
    """延遲雷達 rows ready for the template: 運彩's names, Taipei times, the price that
    still pays; the upload plan and when the radar last saw each price source."""
    found = radar.get("found") or {}
    names = radar.get("names") or {}

    def row(e: Mapping[str, Any]) -> dict[str, Any]:
        sport = e["sport"]
        known = names.get((sport, str(e["match_id"]))) or {}
        home = known.get("home_zh") or e["home"]
        away = known.get("away_zh") or e["away"]
        start = _parse(e["kickoff_utc"])
        return {
            **e,
            "sport_label": SPORT_LABELS.get(sport, sport),
            "competition": known.get("competition") or SPORT_LABELS.get(sport, ""),
            "match": match_title(sport, home, away),
            "label": side_label(sport, e["market"], e["side"], e["line"], home, away),
            "kickoff_local": taipei_time(e["kickoff_utc"]),
            "until": _until(start, now),
        }

    last = found.get("last") or {}
    return {
        "candidates": [row(e) for e in found.get("candidates", [])],
        "watch": [row(e) for e in found.get("watch", [])][:WATCH_ROWS],
        "watch_total": len(found.get("watch", [])),
        "last": [
            {"sport": SPORT_LABELS[s], "tw": last[s].get("tw"), "intl": last[s].get("intl")}
            for s in ("football", "mlb")
            if s in last
        ],
        "plan": [
            {
                **p,
                "sport_label": SPORT_LABELS.get(p["sport"], p["sport"]),
                "at_local": taipei_time(p["at"].isoformat()),
                "first_local": taipei_time(p["first"].isoformat())[6:],
                "last_local": taipei_time(p["last"].isoformat())[6:],
            }
            for p in radar.get("plan", [])
        ][:6],
        "times": RADAR_TIMES,
    }


def _parse(value: Any) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else None


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


def match_title(sport: str, home: str, away: str) -> str:
    """Football 'home vs away'; baseball 'away @ home' (the way 運彩 and US books list it)."""
    return f"{home} vs {away}" if sport == "football" else f"{away} @ {home}"


def _names(item: Mapping[str, Any]) -> tuple[str, str]:
    """運彩's Chinese names when known, else the international ones."""
    return (
        str(item.get("home_zh") or item.get("home", "")),
        str(item.get("away_zh") or item.get("away", "")),
    )


EVENT_TYPES = {"tw_only": "運彩大戶", "intl_big": "國際大單", "radar": "延遲雷達"}
SIDE_SHORT = {"home": "主", "draw": "和", "away": "客", "over": "大", "under": "小"}
BET_STATUS = {
    "pending": "待結算",
    "win": "贏",
    "loss": "輸",
    "push": "走盤",
    "half_win": "贏一半",
    "half_loss": "輸一半",
    "void": "取消",
}
RESULT_LABELS = {"win": "贏", "loss": "輸", "push": "走盤"}


FADE_RESULTS = {
    "win": "贏",
    "loss": "輸",
    "push": "走盤",
    "half_win": "贏一半",
    "half_loss": "輸一半",
}
# The chart's scale (points); the 運彩 break-even lines of football fades sit near 14.
FADE_CHART_MAX = 15.0


def render_fade_page(
    summary: Mapping[str, Any] | None,
    live: list[Mapping[str, Any]],
    record: list[Mapping[str, Any]],
    generated_at: datetime,
    out_path: Path,
    cfg: ModelConfig = MODEL,
) -> Path:
    """docs/fade.html (反者道之動): the backtest of the worst strategies and of betting
    against them (data/fade_summary.json), today's games where one of them points one
    way, and the forward record of the opposite bets at real 運彩 prices."""
    html = (
        _env()
        .get_template("fade.html.j2")
        .render(
            generated_local=generated_at.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M"),
            s=fade_summary_view(summary) if summary else None,
            live=[
                {**r, "names": [fade.STRATEGIES[k]["name"] for k in r["strategies"]]} for r in live
            ],
            record=[
                {
                    **r,
                    "strategy_name": fade.STRATEGIES[r["strategy"]]["name"],
                    "result_label": FADE_RESULTS.get(r["result"], r["result"]),
                }
                for r in record
            ][:60],
            record_n=len(record),
            record_summary=[
                {"key": k, **fade.STRATEGIES[k], **v}
                for k, v in fade.record_summary(record).items()
            ],
            strategies=fade.STRATEGIES,
            cfg=cfg,
        )
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    return out_path


def fade_summary_view(summary: Mapping[str, Any]) -> dict[str, Any]:
    """The backtest summary arranged for the page: the consistently bad strategies, the
    owner's and the system's own, the rest, and the chart rows."""
    rows = [{"key": k, **v} for k, v in summary.get("strategies", {}).items()]
    for r in rows:
        f = r["fade"]
        r["impossible"] = (f.get("fair") or 0) + (f.get("need_tw") or 0) >= 100
    by_z = sorted(rows, key=lambda r: r["self"].get("z") if r["self"].get("z") is not None else 0)
    consistent = [r for r in by_z if r["verdict"] == "consistent"]
    mine = [r for r in by_z if r["group"] == "mine"]
    shown = {r["key"] for r in consistent} | {r["key"] for r in mine}
    rest = [r for r in by_z if r["key"] not in shown]
    chart_keys = [r["key"] for r in consistent] + [
        k for k in ("model4", "model10", "mlb_steam_fav") if k not in {r["key"] for r in consistent}
    ]
    chart = []
    for k in chart_keys:
        r = next((x for x in rows if x["key"] == k), None)
        if r is None or not r["fade"].get("n"):
            continue
        have = r["fade"]["gap_pp"]
        chart.append(
            {
                "sport": r["sport"],
                "name": r["name"],
                "have": have,
                "need_intl": r["fade"].get("need_intl"),
                "need_tw": r["fade"].get("need_tw"),
                "impossible": r["impossible"],
                "w_have": round(max(0.0, min(have, FADE_CHART_MAX)) / FADE_CHART_MAX * 100, 1),
                "x_intl": round(
                    min(r["fade"].get("need_intl") or 0, FADE_CHART_MAX) / FADE_CHART_MAX * 100, 1
                ),
                "x_tw": round(
                    min(r["fade"].get("need_tw") or 0, FADE_CHART_MAX) / FADE_CHART_MAX * 100, 1
                ),
            }
        )
    chart.sort(key=lambda c: -c["have"])
    tw_rois = [(r["fade"].get("roi_tw"), r) for r in rows if r["fade"].get("roi_tw") is not None]
    best_tw = max(tw_rois, key=lambda x: x[0]) if tw_rois else (None, None)
    return {
        **summary,
        "tested": len(rows),
        "consistent": consistent,
        "mine": mine,
        "rest": rest,
        "chart": chart,
        "chart_max": FADE_CHART_MAX,
        "profitable_tw": sum(1 for roi, _ in tw_rois if roi > 0),
        "best_tw": {"roi": best_tw[0], "name": best_tw[1]["name"] if best_tw[1] else ""},
        "worst": by_z[0] if by_z else None,
    }


def render_bighand_page(
    history: list[Mapping[str, Any]],
    summary: Mapping[str, Mapping[str, Any]],
    generated_at: datetime,
    out_path: Path,
    cfg: ModelConfig = MODEL,
) -> Path:
    """docs/bighand.html: every match where a 大戶 move was caught (moves.event_history),
    newest kickoff first, whether or not it ever became a bet."""
    html = (
        _env()
        .get_template("bighand.html.j2")
        .render(
            generated_local=generated_at.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M"),
            matches=[bighand_view(m, cfg) for m in history],
            summary=[
                {"type": kind, "label": label, **summary[kind]}
                for kind, label in EVENT_TYPES.items()
                if kind in summary
            ],
            cfg=cfg,
        )
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    return out_path


def bighand_view(m: Mapping[str, Any], cfg: ModelConfig = MODEL) -> dict[str, Any]:
    """One match of the 大戶紀錄 page: words and numbers ready for the template."""
    sport = m["sport"]
    home, away = _names(m)
    events = []
    for e in m["events"]:
        after = e.get("after")
        trend = None
        if after is not None:
            trend = "續漲" if after >= FOLLOW_MIN else ("回落" if after <= -FOLLOW_MIN else "持平")
        events.append(
            {
                **e,
                "type_label": EVENT_TYPES.get(e["type"], e["type"]),
                "label": side_label(sport, e["market"], e["side"], e["line"], home, away),
                "trend": trend,
                "result_label": RESULT_LABELS.get(e.get("result", ""))
                or ("待結果" if m.get("closed") else "待賽"),
            }
        )
    g_last, g_peak = m.get("g_last"), m.get("g_peak")
    return {
        **m,
        "title": match_title(sport, home, away),
        "sport_label": "足球" if sport == "football" else "MLB",
        "kickoff_local": taipei_time(m.get("kickoff_utc")),
        "events": events,
        "types": sorted({e["type"] for e in events}),
        "bets": [
            {
                **b,
                "ev_pct": _float(b.get("ev")),
                "status_label": BET_STATUS.get(b.get("status", ""), b.get("status", "")),
            }
            for b in m.get("value_bets", [])
        ],
        "g_shape": G_SHAPES[t10_signal(g_last, cfg.g_threshold)] if g_last is not None else None,
        "g_zone": g_peak is not None and abs(g_peak) >= cfg.g_threshold,
        "tables": [_trajectory_table(sport, t, m["events"]) for t in m.get("markets", [])],
    }


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _trajectory_table(
    sport: str, t: Mapping[str, Any], events: list[Mapping[str, Any]]
) -> dict[str, Any]:
    """Time | international % per side | 運彩 price per side (| G for the MLB moneyline),
    one row per time we saw the market; the times a move was caught are marked."""
    market, line = t["market"], t["line"]
    sides = [s for s in MARKET_SIDES[market] if not (sport == "mlb" and s == "draw")]
    if market == "h2h":
        title = "不讓分" if sport == "mlb" else "勝平負"
    elif market == "totals":
        title = f"大小 {line}"
    else:
        title = f"讓分（主 {float(line):+g}）" if line else "讓分"
    caught = {e["to_at"] for e in events if e["market"] == market and e["line"] == line}
    with_g = any(p.get("g") is not None for p in t["points"])
    rows = []
    for p in t["points"]:
        rows.append(
            {
                "at": taipei_time(p["at"]),
                "intl": [f"{p['intl'][s] * 100:.1f}" if s in p["intl"] else "" for s in sides],
                "tw": [f"{p['tw'][s]:.2f}" if s in p["tw"] else "" for s in sides],
                "g": f"{p['g'] * 100:+.1f}" if p.get("g") is not None else "",
                "caught": p["at"] in caught,
            }
        )
    return {
        "title": title,
        "sides": [SIDE_SHORT[s] for s in sides],
        "rows": rows,
        "with_g": with_g,
    }


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
    signal_rows: list[Mapping[str, Any]] | None = None,
    value_rows: list[Mapping[str, Any]] | None = None,
) -> Path:
    """docs/reviews.html: every review, newest first, with the cumulative diagnostics."""
    views = review_views(
        football_reviews, mlb_reviews, generated_at, False, signal_rows, value_rows
    )
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
    signal_rows: list[Mapping[str, Any]] | None = None,
    value_rows: list[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Explained review cards (newest first) and the cumulative summaries. Every
    recommendation the page made for a match (模型分歧 ledger rows, 運彩 value bets) is
    reviewed on that match's card."""
    picks = match_picks(signal_rows or [], value_rows or [])
    football = [
        {
            "row": row,
            **review.explain_football(row, picks.get(("football", str(row.get("match_id"))), [])),
            **_odds_table(row),
        }
        for row in sorted(football_rows, key=lambda r: str(r.get("kickoff_utc")), reverse=True)
    ]
    mlb = [
        {"row": row, **review.explain_mlb(row, picks.get(("mlb", str(row.get("game_pk"))), []))}
        for row in sorted(mlb_rows, key=lambda r: str(r.get("kickoff_utc")), reverse=True)
    ]
    if recent_only:
        football = _recent(football, now)
        mlb = _recent(mlb, now)
    return {
        "football": football,
        "mlb": mlb,
        "summary": review.summarize_football(football_rows),
        "mlb_summary": review.summarize_mlb(mlb_rows),
        "total": len(football_rows) + len(mlb_rows),
        "picks": _picks_summary([p for v in football + mlb for p in v["picks"]]),
    }


def match_picks(
    signal_rows: list[Mapping[str, Any]], value_rows: list[Mapping[str, Any]]
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """(sport, match id) -> the match's recommendations, oldest first: 運彩 value bets
    and 模型分歧 signals (football only: MLB has no model signals)."""
    out: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for r in value_rows:
        out.setdefault((str(r.get("sport")), str(r.get("match_id"))), []).append(
            review.pick_from_value(r)
        )
    for r in signal_rows:
        if r.get("sport") == "football":
            out.setdefault(("football", str(r.get("match_id"))), []).append(
                review.pick_from_signal(r)
            )
    for v in out.values():
        v.sort(key=lambda p: (p["source"] != "value", str(p["at"])))
    return out


def _picks_summary(picks: list[Mapping[str, Any]]) -> dict[str, Any]:
    done = [p for p in picks if p["result"] not in ("", "pending")]
    return {
        "n": len(picks),
        "settled": len(done),
        "wins": sum(1 for p in done if p["result"] in ("win", "half_win")),
    }


def _recent(views: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    """The last few days' reviews, recommended matches first (the ones the owner was
    told to look at), so a busy weekend never pushes them off the front page."""
    cutoff = (now - timedelta(days=RECENT_REVIEW_DAYS)).isoformat()
    recent = [v for v in views if str(v["row"].get("kickoff_utc")) >= cutoff] or views[:6]
    recent.sort(key=lambda v: (not v["picks"], v["row"].get("lean_source") != "signal"))
    return recent[:RECENT_REVIEW_LIMIT]


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
