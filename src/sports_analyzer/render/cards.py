"""Shared view helpers for the site (render/site.py): names, times, labels, the 大戶 record
and the post-match review cards."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from ..analysis import review
from ..analysis.mlb import G_SHAPES
from ..config import LOCAL_TZ, MODEL, ModelConfig
from ..models.market import t10_signal
from ..moves import FOLLOW_MIN, MARKET_SIDES
from ..sensor import MOVE, RUN_MINUTES

TEMPLATE_DIR = Path(__file__).parent / "templates"


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


SPORT_LABELS = {"football": "足球", "mlb": "MLB"}
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
        # 盯盤 (sensor.py), for the page's explanation: how often it looks, what move buys.
        "sensor_minutes": RUN_MINUTES,
        "sensor_move": round(MOVE * 100),
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


def review_views(
    football_rows: list[Mapping[str, Any]],
    mlb_rows: list[Mapping[str, Any]],
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
