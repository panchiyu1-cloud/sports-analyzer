"""The site: a front page and one page per analysis method (docs/*.html).

Every method page has two tabs. 即將開賽: an analysis card per match that has not started,
with the prices captured over time (the line-move tape) read the way the method reads
them. 歷史數據: every finished match the method looked at - what it said, what happened,
and what went right or wrong, process apart from luck.

Pure view builders: they turn the snapshots, the tape and the CSV records into dicts for
the templates; `render_site` writes the pages.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ..analysis import fade, review
from ..analysis.mlb import G_SHAPES
from ..config import COMPETITION_BY_CODE, LOCAL_TZ, MODEL, ModelConfig
from ..models.market import t10_signal
from ..moves import parse_time, value_threshold
from .cards import (
    FADE_RESULTS,
    SPORT_LABELS,
    _env,
    _until,
    bighand_view,
    fade_summary_view,
    match_picks,
    match_title,
    radar_view,
    review_views,
    side_label,
    taipei_time,
)

PAGES = (
    ("index", "總覽", "index.html"),
    ("value", "運彩價值", "value.html"),
    ("t10", "T-10", "t10.html"),
    ("radar", "延遲雷達", "radar.html"),
    ("moves", "盤口移動", "moves.html"),
    ("model", "模型分歧", "model.html"),
    ("fade", "反者道之動", "fade.html"),
    ("reviews", "全部檢討", "reviews.html"),
)
# Backtest reports: page -> (summary file in data/, title, the method page it belongs to).
REPORTS = {
    "backtest": ("backtest_summary.json", "足球模型回測", "model"),
    "t10_backtest": ("t10_backtest_summary.json", "T-10 回測", "t10"),
}
WEEKDAYS = "一二三四五六日"
SIDE_ZH = {"home": "主", "draw": "和", "away": "客", "over": "大", "under": "小"}
# How far ahead the front page's fixture board looks.
BOARD_HOURS = 48
HISTORY_LIMIT = 80


@dataclass
class Site:
    """Everything the pages are made from, gathered once per render."""

    now: datetime
    football: dict[str, Any] | None
    mlb: dict[str, Any] | None
    tape: list[dict[str, str]]
    football_reviews: list[dict[str, str]] = field(default_factory=list)
    mlb_reviews: list[dict[str, str]] = field(default_factory=list)
    signal_rows: list[dict[str, str]] = field(default_factory=list)
    value_rows: list[dict[str, str]] = field(default_factory=list)
    history: list[dict[str, Any]] = field(default_factory=list)  # moves.event_history
    history_summary: dict[str, Any] = field(default_factory=dict)
    radar: dict[str, Any] = field(default_factory=dict)  # moves.radar + names + plan
    tracked: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)  # moves.track
    fade_summary: dict[str, Any] | None = None
    fade_live: list[dict[str, Any]] = field(default_factory=list)
    fade_record: list[dict[str, Any]] = field(default_factory=list)
    ledger_summary: dict[str, Any] | None = None
    tw_summary: dict[str, Any] | None = None
    budget: dict[str, Any] | None = None
    backtest_total: dict[str, Any] | None = None
    backtest_exists: bool = False
    # (sport, match id) -> competition and 運彩's Chinese names from recent snapshots, so
    # finished matches keep the names the owner saw on 運彩 (pipeline._match_names).
    names: dict[tuple[str, str], dict[str, str]] = field(default_factory=dict)
    # Backtest summaries by report page (REPORTS), re-rendered in the site's frame.
    reports: dict[str, dict[str, Any]] = field(default_factory=dict)
    cfg: ModelConfig = MODEL


# ============================================================ small helpers
def when(kickoff_utc: Any, now: datetime) -> dict[str, str]:
    """'10/17', '19:30', '週六', '還有 9 小時 50 分' for a kickoff."""
    start = parse_time(kickoff_utc)
    if start is None:
        return {"date": "", "time": "", "weekday": "", "eta": ""}
    local = start.astimezone(LOCAL_TZ)
    return {
        "date": local.strftime("%m/%d"),
        "time": local.strftime("%H:%M"),
        "weekday": f"週{WEEKDAYS[local.weekday()]}",
        "eta": f"還有 {_until(start, now)}" if start > now else "已開賽",
    }


def _id(card: Mapping[str, Any]) -> str:
    return str(card.get("match_id") or card.get("game_pk") or "")


def _sport(card: Mapping[str, Any]) -> str:
    return "mlb" if card.get("game_pk") else "football"


def anchor(sport: str, match_id: Any) -> str:
    return f"m-{sport}-{match_id}"


def names(card: Mapping[str, Any]) -> tuple[str, str]:
    """運彩's Chinese names when the match was on 運彩, else the international ones."""
    tw = card.get("tw") or {}
    return str(tw.get("home_zh") or card.get("home", "")), str(
        tw.get("away_zh") or card.get("away", "")
    )


def known_names(site: Site, sport: str, match_id: Any, home: str, away: str) -> tuple[str, str]:
    """運彩's Chinese names of a past match when a recent snapshot had them."""
    known = site.names.get((sport, str(match_id))) or {}
    return str(known.get("home_zh") or home), str(known.get("away_zh") or away)


def comp_name(sport: str, competition: Any) -> str:
    """'英超' for a football-data.org code ('PL'); MLB is MLB."""
    if sport == "mlb":
        return "MLB"
    code = str(competition or "")
    return COMPETITION_BY_CODE[code].name if code in COMPETITION_BY_CODE else code


def football_cards(site: Site) -> list[dict[str, Any]]:
    return [
        c for comp in (site.football or {}).get("competitions", []) for c in comp.get("cards", [])
    ]


def mlb_cards(site: Site) -> list[dict[str, Any]]:
    return list((site.mlb or {}).get("games", []))


def upcoming(cards: Iterable[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    out = [c for c in cards if (parse_time(c.get("kickoff_utc")) or now) > now]
    return sorted(out, key=lambda c: str(c.get("kickoff_utc")))


def tape_by_match(
    tape: Iterable[Mapping[str, str]],
) -> dict[tuple[str, str], list[Mapping[str, str]]]:
    out: dict[tuple[str, str], list[Mapping[str, str]]] = defaultdict(list)
    for r in tape:
        out[(r["sport"], str(r["match_id"]))].append(r)
    return out


def _pct(p: float | None, digits: int = 0) -> str:
    return "—" if p is None else f"{p * 100:.{digits}f}%"


def _signed(x: float | None, unit: str = "%", digits: int = 1) -> str:
    return "—" if x is None else f"{x * 100:+.{digits}f}{unit}"


def _base(site: Site, page: str, title: str) -> dict[str, Any]:
    return {
        "page": page,
        "title": title,
        "pages": PAGES,
        "updated": site.now.astimezone(LOCAL_TZ).strftime("%m/%d %H:%M"),
        "cfg": site.cfg,
    }


# ===================================================== the prices over time
@dataclass(frozen=True)
class Obs:
    """One time we saw a market: international probabilities and 運彩 (prob, price)."""

    at: datetime
    intl: dict[str, float]
    tw: dict[str, tuple[float, float | None]]


def observations(
    rows: Iterable[Mapping[str, str]], market: str, line: str, kickoff: datetime | None
) -> list[Obs]:
    """Every time the tape saw one market of one match before kickoff, oldest first."""
    by_at: dict[datetime, tuple[dict[str, float], dict[str, tuple[float, float | None]]]] = {}
    for r in rows:
        if r["market"] != market or (r.get("line") or "") != line:
            continue
        at = parse_time(r.get("observed_at"))
        if at is None or (kickoff is not None and at >= kickoff):
            continue
        intl, tw = by_at.setdefault(at, ({}, {}))
        try:
            prob = float(r["prob"])
        except (KeyError, TypeError, ValueError):
            continue
        if r["source"] == "intl":
            intl[r["side"]] = prob
        else:
            tw[r["side"]] = (prob, float(r["price"]) if r.get("price") else None)
    return [Obs(at, *by_at[at]) for at in sorted(by_at)]


def nearest_intl(obs: list[Obs], at: datetime, within: timedelta) -> Obs | None:
    """The international observation closest to `at`, if within the freshness window."""
    best = None
    for o in obs:
        if not o.intl:
            continue
        gap = abs(o.at - at)
        if gap <= within and (best is None or gap < abs(best.at - at)):
            best = o
    return best


# Sparkline geometry, in viewBox units (scaled to the card's width).
SPARK_W, SPARK_H = 320.0, 110.0
PAD_L, PAD_R, PAD_T, PAD_B = 34.0, 46.0, 10.0, 22.0


def spark(
    series: list[dict[str, Any]],
    refs: Iterable[dict[str, Any]] = (),
    kind: str = "pct",
    min_span: float = 0.08,
) -> dict[str, Any] | None:
    """A small line chart: one time axis, one value axis (probability, or G in points).
    series: [{"key", "label", "points": [(datetime, value, note)]}]; refs: horizontal
    reference lines [{"value", "label"}]. Returns None with fewer than two points."""
    pts = [p for s in series for p in s["points"]]
    if len(pts) < 2:
        return None
    t0 = min(p[0] for p in pts)
    t1 = max(p[0] for p in pts)
    span_t = (t1 - t0).total_seconds() or 1.0
    values = [p[1] for p in pts] + [r["value"] for r in refs]
    lo, hi = min(values), max(values)
    if hi - lo < min_span:
        mid = (hi + lo) / 2
        lo, hi = mid - min_span / 2, mid + min_span / 2
    pad = (hi - lo) * 0.12
    lo, hi = lo - pad, hi + pad
    inner_w = SPARK_W - PAD_L - PAD_R
    inner_h = SPARK_H - PAD_T - PAD_B

    def x(t: datetime) -> float:
        if t1 == t0:
            return round(PAD_L + inner_w / 2, 1)
        return round(PAD_L + (t - t0).total_seconds() / span_t * inner_w, 1)

    def y(v: float) -> float:
        return round(PAD_T + (hi - v) / (hi - lo) * inner_h, 1)

    def label(v: float) -> str:
        return f"{v * 100:.0f}%" if kind == "pct" else f"{v * 100:+.1f}"

    lines, ends, tips = [], [], defaultdict(list)
    for s in series:
        p = sorted(s["points"], key=lambda q: q[0])
        if not p:
            continue
        coords = [(x(t), y(v)) for t, v, _ in p]
        lines.append(
            {
                "key": s["key"],
                "label": s["label"],
                "d": "M" + " L".join(f"{cx},{cy}" for cx, cy in coords),
                "dots": [{"x": cx, "y": cy} for cx, cy in coords],
            }
        )
        ends.append({"x": coords[-1][0] + 6, "y": coords[-1][1] + 4, "text": label(p[-1][1])})
        for t, v, note in p:
            tips[t].append([s["key"], s["label"], label(v) + (f"（{note}）" if note else "")])
    # Two end labels that would overlap: the legend and the tooltip carry the second.
    ends.sort(key=lambda e: e["y"])
    shown = []
    for e in ends:
        if all(abs(e["y"] - o["y"]) >= 12 for o in shown):
            shown.append(e)
    step = _nice_step(hi - lo, kind)
    first = step * int(lo / step + (1 if lo > 0 else 0))
    ticks = []
    v = first
    while v <= hi and len(ticks) < 4:
        # Axis labels without decimals or units: "45" (%), "+4" / "0" (G in points).
        if kind == "pct":
            text = f"{v * 100:.0f}"
        else:
            text = f"{v * 100:+.0f}" if round(v * 100) else "0"
        ticks.append({"y": y(v), "label": text})
        v += step
    return {
        "w": SPARK_W,
        "h": SPARK_H,
        "x0": PAD_L,
        "x1": SPARK_W - PAD_R,
        "y0": PAD_T,
        "y1": SPARK_H - PAD_B,
        "lines": lines,
        "ends": shown,
        "ticks": ticks,
        "refs": [{**r, "y": y(r["value"])} for r in refs],
        "start": t0.astimezone(LOCAL_TZ).strftime("%m/%d %H:%M"),
        "end": t1.astimezone(LOCAL_TZ).strftime("%m/%d %H:%M"),
        "tips": [
            {"x": x(t), "t": t.astimezone(LOCAL_TZ).strftime("%m/%d %H:%M"), "v": tips[t]}
            for t in sorted(tips)
        ],
        "unit": "%" if kind == "pct" else "pp",
    }


def _nice_step(span: float, kind: str) -> float:
    for step in (0.02, 0.05, 0.1, 0.2) if kind == "pct" else (0.02, 0.04, 0.05, 0.1):
        if span / step <= 4:
            return step
    return 0.25


def price_series(obs: list[Obs], side: str) -> list[dict[str, Any]]:
    """The two lines of a side: the international probability and 運彩's own (de-vigged)."""
    intl = [(o.at, o.intl[side], "") for o in obs if side in o.intl]
    tw = [
        (o.at, o.tw[side][0], f"{o.tw[side][1]:.2f}" if o.tw[side][1] else "")
        for o in obs
        if side in o.tw
    ]
    out = []
    if intl:
        out.append({"key": "intl", "label": "國際", "points": intl})
    if tw:
        out.append({"key": "tw", "label": "運彩", "points": tw})
    return out


def timeline_rows(
    obs: list[Obs],
    sides: Iterable[str],
    read: Any,
) -> list[dict[str, Any]]:
    """The table under a chart: per time, the international % and 運彩 price of each side,
    and the method's reading of that moment (`read(obs, index) -> (text, tone)`)."""
    sides = list(sides)
    rows = []
    for i, o in enumerate(obs):
        text, tone = read(obs, i)
        rows.append(
            {
                "at": o.at.astimezone(LOCAL_TZ).strftime("%m/%d %H:%M"),
                "intl": [f"{o.intl[s] * 100:.0f}" if s in o.intl else "" for s in sides],
                "tw": [f"{o.tw[s][1]:.2f}" if s in o.tw and o.tw[s][1] else "" for s in sides],
                "text": text,
                "tone": tone,
            }
        )
    return rows[::-1]  # newest first: the moment that matters now is on top


def market_sides(sport: str, market: str) -> list[str]:
    if market == "h2h":
        return ["home", "away"] if sport == "mlb" else ["home", "draw", "away"]
    if market == "totals":
        return ["over", "under"]
    return ["home", "away"]


def ev_reader(sport: str, cfg: ModelConfig, labels: Mapping[str, str]) -> Any:
    """Value reading: at a 運彩 upload, the EV of each price against the international
    price nearest in time (within the freshness window), else no judgement."""
    within = timedelta(minutes=cfg.max_price_gap_minutes)
    threshold = value_threshold(sport, cfg)

    def read(obs: list[Obs], i: int) -> tuple[str, str]:
        o = obs[i]
        if not o.tw:
            return "國際更新", "neutral"
        fair = o if o.intl else nearest_intl(obs, o.at, within)
        if fair is None:
            return "國際價隔太久，不判斷", "warn"
        evs = [
            (fair.intl[s] * price - 1, s)
            for s, (_, price) in o.tw.items()
            if price and s in fair.intl
        ]
        if not evs:
            return "沒有可比的國際價", "neutral"
        ev, side = max(evs)
        word = "可下" if ev >= threshold else "最接近"
        return f"{word}：{labels.get(side, side)} EV {ev * 100:+.1f}%", (
            "good" if ev >= threshold else "neutral"
        )

    return read


# =============================================================== 運彩價值
def value_status(card: Mapping[str, Any]) -> str:
    tw = card.get("tw") or {}
    if not tw:
        return "none"
    if tw.get("fair_fresh") is False:
        return "stale"
    return "value" if tw.get("values") else "pass"


def _bet_rows(card: Mapping[str, Any], sport: str, cfg: ModelConfig) -> list[dict[str, Any]]:
    """Every direct 運彩 bet on the card against its fair price."""
    tw = card.get("tw") or {}
    home, away = names(card)
    threshold = value_threshold(sport, cfg)
    rows = []
    for market in ("h2h", "spread", "totals"):
        block = tw.get(market)
        if not isinstance(block, dict):
            continue
        line = block.get("line")
        for side in market_sides(sport, market):
            b = block.get(side)
            if not isinstance(b, dict) or not b.get("price"):
                continue
            label = side_label(
                sport, market, side, f"{line:g}" if line is not None else "", home, away
            )
            ev = b.get("ev")
            rows.append(
                {
                    "label": label,
                    "price": b.get("price"),
                    "fair_odds": b.get("fair_odds"),
                    "ev": ev,
                    "value": ev is not None and ev >= threshold and tw.get("fair_fresh", True),
                }
            )
    return rows


def _card_head(card: Mapping[str, Any], site: Site) -> dict[str, Any]:
    sport = _sport(card)
    home, away = names(card)
    return {
        "id": anchor(sport, _id(card)),
        "sport": sport,
        "sport_label": SPORT_LABELS[sport],
        "competition": card.get("competition_name") or SPORT_LABELS[sport],
        "when": when(card.get("kickoff_utc"), site.now),
        "home": home,
        "away": away,
        "home_intl": card.get("home", ""),
        "away_intl": card.get("away", ""),
        "title": match_title(sport, home, away),
    }


def value_card(card: dict[str, Any], rows: list[Mapping[str, str]], site: Site) -> dict[str, Any]:
    sport = _sport(card)
    cfg = site.cfg
    tw = card.get("tw") or {}
    status = value_status(card)
    bets = _bet_rows(card, sport, cfg)
    best = max(bets, key=lambda b: b["ev"] if b["ev"] is not None else -9, default=None)
    derived = sorted(tw.get("derived") or [], key=lambda d: -d.get("ev", -9))[:3]
    home, away = names(card)
    labels = {s: side_label(sport, "h2h", s, "", home, away) for s in market_sides(sport, "h2h")}
    obs = observations(rows, "h2h", "", parse_time(card.get("kickoff_utc")))
    focus = (tw.get("values") or [{}])[0].get("selection_key") if tw.get("values") else None
    h2h = tw.get("h2h") or {}
    if focus not in labels:
        evs = {s: (h2h.get(s) or {}).get("ev") for s in labels}
        focus = max((s for s in evs if evs[s] is not None), key=lambda s: evs[s], default="home")
    return {
        **_card_head(card, site),
        "status": status,
        "bets": bets,
        "best": best,
        "values": tw.get("values") or [],
        "derived": derived,
        "captured": taipei_time(tw.get("captured_at")),
        "fair_at": taipei_time(tw.get("fair_at")),
        "focus_label": labels.get(focus, ""),
        "chart": spark(price_series(obs, focus)),
        "sides": [SIDE_ZH[s] for s in labels],
        "timeline": timeline_rows(obs, labels, ev_reader(sport, cfg, labels)),
    }


def value_page(site: Site, by_match: Mapping[tuple[str, str], list]) -> dict[str, Any]:
    cards = [
        value_card(c, by_match.get((_sport(c), _id(c)), []), site)
        for c in upcoming(football_cards(site) + mlb_cards(site), site.now)
        if c.get("tw")
    ]
    order = {"value": 0, "pass": 1, "stale": 2}
    cards.sort(key=lambda c: (order.get(c["status"], 3), c["when"]["date"], c["when"]["time"]))
    history = value_history(site)
    reports = [
        {"sport": SPORT_LABELS[sport], **snap["tw_report"]}
        for sport, snap in (("football", site.football), ("mlb", site.mlb))
        if snap and snap.get("tw_report")
    ]
    return {
        **_base(site, "value", "運彩價值"),
        "cards": cards,
        "history": history,
        "reports": reports,
        "kpis": {
            "upcoming": len(cards),
            "value": sum(1 for c in cards if c["status"] == "value"),
            **NO_BETS,
            **(site.tw_summary or {}),
        },
    }


# tw_ledger.summary() with no bets yet, so the page reads the same keys either way.
NO_BETS = {"pending": 0, "settled": 0, "wins": 0, "units": 0.0, "avg_clv": None, "n_clv": 0}


def _review_rows(site: Site) -> dict[tuple[str, str], dict[str, str]]:
    out = {("football", str(r.get("match_id"))): r for r in site.football_reviews}
    out.update({("mlb", str(r.get("game_pk"))): r for r in site.mlb_reviews})
    return out


def value_history(site: Site) -> dict[str, Any]:
    """Every 運彩 value bet the system recorded, judged by its closing-line value apart
    from the result; then the matches it looked at and passed on, priced at the close."""
    reviews = _review_rows(site)
    bets = []
    for r in sorted(site.value_rows, key=lambda r: str(r.get("captured_at")), reverse=True):
        sport = str(r.get("sport"))
        row = reviews.get((sport, str(r.get("match_id")))) or {}
        pick = review.explain_pick(review.pick_from_value(r), row, sport)
        home, away = known_names(
            site, sport, r.get("match_id"), row.get("home") or r.get("home", ""),
            row.get("away") or r.get("away", ""),
        )  # fmt: skip
        bets.append(
            {
                "sport_label": SPORT_LABELS.get(sport, sport),
                "competition": comp_name(sport, r.get("competition")),
                "when": when(r.get("kickoff_utc"), site.now),
                "title": match_title(sport, home, away),
                "market": r.get("market", ""),
                "selection": r.get("selection", ""),
                "price": r.get("tw_price"),
                "ev": _float(r.get("ev")),
                "score": _score(row, sport),
                **pick,
            }
        )
    passed = _passed(site, {(str(r.get("sport")), str(r.get("match_id"))) for r in site.value_rows})
    return {"bets": bets[:HISTORY_LIMIT], "passed": passed[:HISTORY_LIMIT], "n": len(bets)}


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _score(row: Mapping[str, Any], sport: str) -> str:
    if not row:
        return ""
    if sport == "football":
        h, a = row.get("ft_h"), row.get("ft_a")
        return f"{h}–{a}" if h not in (None, "") else ""
    h, a = row.get("final_h"), row.get("final_a")
    return f"{a}–{h}" if h not in (None, "") else ""


def _passed(site: Site, bet_matches: set[tuple[str, str]]) -> list[dict[str, Any]]:
    """Reviewed matches with 運彩 prices and no value bet: each price against the closing
    fair price. Passing was right when none of them would have paid at the close."""
    out = []
    for row in site.football_reviews:
        key = ("football", str(row.get("match_id")))
        if key in bet_matches:
            continue
        sides = [
            ("主", "tw_h", "close_h", "H"),
            ("和", "tw_d", "close_d", "D"),
            ("客", "tw_a", "close_a", "A"),
        ]
        out.append(_passed_row(row, "football", sides, row.get("outcome"), site))
    for row in site.mlb_reviews:
        key = ("mlb", str(row.get("game_pk")))
        if key in bet_matches:
            continue
        won = review.assess_mlb(row)["winner"]
        winner = "H" if won == "home" else ("A" if won == "away" else "")
        sides = [("主", "tw_home", "close_home", "H"), ("客", "tw_away", "close_away", "A")]
        out.append(_passed_row(row, "mlb", sides, winner, site))
    out = [o for o in out if o is not None]
    out.sort(key=lambda o: o["kickoff_utc"], reverse=True)
    return out


def _passed_row(
    row: Mapping[str, Any],
    sport: str,
    sides: list[tuple[str, str, str, str]],
    outcome: Any,
    site: Site,
) -> dict[str, Any] | None:
    cells = []
    for label, tw_key, close_key, code in sides:
        price, close = _float(row.get(tw_key)), _float(row.get(close_key))
        if price is None or close is None:
            continue
        cells.append(
            {"label": label, "price": price, "ev": close * price - 1, "won": outcome == code}
        )
    if not cells:
        return None
    best = max(cells, key=lambda c: c["ev"])
    mid = row.get("match_id") if sport == "football" else row.get("game_pk")
    home, away = known_names(site, sport, mid, str(row.get("home", "")), str(row.get("away", "")))
    return {
        "kickoff_utc": str(row.get("kickoff_utc")),
        "when": when(row.get("kickoff_utc"), site.now),
        "title": match_title(sport, home, away),
        "competition": comp_name(sport, row.get("competition") or row.get("competition_name")),
        "sport_label": SPORT_LABELS[sport],
        "score": _score(row, sport),
        "cells": cells,
        "best": best,
        "right": best["ev"] < value_threshold(sport, site.cfg),
    }


# ==================================================================== T-10
def g_series(
    rows: Iterable[Mapping[str, str]], kickoff: datetime | None
) -> list[tuple[datetime, float, str]]:
    """The international G at every time the moneyline and the ±1.5 were bought together."""
    from ..moves import g_points

    points = g_points(rows, kickoff)
    out = []
    for at, g in sorted(points.items()):
        t = parse_time(at)
        if t is not None:
            out.append((t, g, G_SHAPES[t10_signal(g, MODEL.g_threshold)]))
    return out


def t10_card(card: dict[str, Any], rows: list[Mapping[str, str]], site: Site) -> dict[str, Any]:
    cfg = site.cfg
    kickoff = parse_time(card.get("kickoff_utc"))
    t = card.get("t10v") or {}
    g = (card.get("t10") or {}).get("g")
    gs = g_series(rows, kickoff)
    home, away = names(card)
    labels = {"home": f"{home}", "away": f"{away}"}
    obs = observations(rows, "h2h", "", kickoff)
    chart = (
        spark(
            [{"key": "g", "label": "G", "points": gs}],
            refs=[
                {"value": cfg.g_threshold, "label": "+6"},
                {"value": -cfg.g_threshold, "label": "−6"},
            ],
            kind="g",
            min_span=0.12,
        )
        if len(gs) >= 2
        else None
    )
    g_by_time = {p[0]: p for p in gs}
    price_read = ev_reader("mlb", cfg, {s: f"{labels[s]} 不讓分" for s in labels})

    def read(o: list[Obs], i: int) -> tuple[str, str]:
        p = g_by_time.get(o[i].at)
        if p is not None:
            return f"G {p[1] * 100:+.1f}（{p[2]}）", "good" if abs(
                p[1]
            ) >= cfg.g_threshold else "neutral"
        return price_read(o, i)

    shape = None
    if g is not None:
        shape = G_SHAPES[t10_signal(g, cfg.g_threshold)]
    return {
        **_card_head(card, site),
        "status": t.get("status") or ("none" if not card.get("tw") else "pass"),
        "t10v": t,
        "g": g,
        "shape": shape,
        "zone": g is not None and abs(g) >= cfg.g_threshold,
        "probables": (card.get("away_probable") or "未定", card.get("home_probable") or "未定"),
        "chart": chart,
        "price_chart": spark(price_series(obs, "home")),
        "sides": ["主", "客"],
        "timeline": timeline_rows(obs, ["home", "away"], read),
    }


def t10_page(site: Site, by_match: Mapping[tuple[str, str], list]) -> dict[str, Any]:
    cards = [
        t10_card(c, by_match.get(("mlb", _id(c)), []), site)
        for c in upcoming(mlb_cards(site), site.now)
    ]
    history = t10_history(site)
    return {
        **_base(site, "t10", "T-10"),
        "cards": cards,
        "history": history,
        "kpis": {
            "upcoming": len(cards),
            "value": sum(1 for c in cards if c["status"] == "value"),
            "zone": sum(1 for c in cards if c["zone"]),
            **history["summary"],
        },
    }


def shape_check(row: Mapping[str, Any]) -> dict[str, str] | None:
    """Did the game take the shape G described? 強隊大勝型: the favourite won by 2+;
    一分差型: the favourite did not (the underdog won or lost by one)."""
    g = _float(row.get("t10_g"))
    cover = review.assess_mlb(row)["fav_cover"]
    if g is None or cover == "":
        return None
    signal = t10_signal(g, MODEL.g_threshold)
    covered = cover == "1"
    shape = G_SHAPES[signal]
    if signal is None:
        return {"text": f"G {g * 100:+.1f}（{shape}）：沒有判斷型態。", "tone": "neutral"}
    right = covered if signal == "FAV_ML" else not covered
    happened = "熱門贏 2 分以上" if covered else "熱門沒有贏 2 分以上"
    return {
        "text": f"G {g * 100:+.1f} 說是{shape}，實際{happened}：型態{'說對了' if right else '說錯了'}。",
        "tone": "good" if right else "bad",
    }


def t10_history(site: Site) -> dict[str, Any]:
    picks = match_picks(site.signal_rows, site.value_rows)
    items = []
    for row in sorted(site.mlb_reviews, key=lambda r: str(r.get("kickoff_utc")), reverse=True):
        x = review.explain_mlb(row, picks.get(("mlb", str(row.get("game_pk"))), []))
        items.append(
            {
                "row": row,
                "when": when(row.get("kickoff_utc"), site.now),
                "title": match_title(
                    "mlb",
                    *known_names(
                        site, "mlb", row.get("game_pk"), str(row.get("home")), str(row.get("away"))
                    ),
                ),  # fmt: skip
                "score": _score(row, "mlb"),
                "shape": shape_check(row),
                **x,
            }
        )
    summary = review.summarize_mlb(site.mlb_reviews)
    shapes = [i["shape"] for i in items if i["shape"] and i["shape"]["tone"] != "neutral"]
    return {
        "games": items[:HISTORY_LIMIT],
        "n": len(items),
        "summary": {
            "games": summary.get("games", 0),
            "bets": summary.get("t10_bets", 0),
            "wins": summary.get("t10_wins", 0),
            "units": summary.get("t10_units", 0.0),
            "shape_right": sum(1 for s in shapes if s["tone"] == "good"),
            "shape_n": len(shapes),
        },
    }


# ================================================================ 延遲雷達
def radar_page(site: Site, by_match: Mapping[tuple[str, str], list]) -> dict[str, Any]:
    view = (
        radar_view(site.radar, site.now)
        if site.radar
        else {"candidates": [], "watch": [], "plan": [], "last": []}
    )
    cards = []
    for kind, entries in (("candidate", view["candidates"]), ("watch", view["watch"])):
        for e in entries:
            rows = by_match.get((e["sport"], str(e["match_id"])), [])
            obs = observations(rows, e["market"], e["line"], parse_time(e["kickoff_utc"]))
            uploaded = parse_time(e["tw_at"])
            threshold = e["threshold"]

            def read(
                o: list[Obs],
                i: int,
                side: str = e["side"],
                up: Any = uploaded,
                price: float = e["tw_price"],
                thr: float = threshold,
            ) -> tuple[str, str]:
                x = o[i]
                if side in x.tw:
                    return f"運彩上傳 {x.tw[side][1]:.2f}" if x.tw[side][
                        1
                    ] else "運彩上傳", "neutral"
                if up is not None and x.at > up and side in x.intl:
                    ev = x.intl[side] * price - 1
                    return f"若運彩沒變：EV {ev * 100:+.1f}%", "good" if ev >= thr else "neutral"
                return "國際更新", "neutral"

            sides = market_sides(e["sport"], e["market"])
            cards.append(
                {
                    **e,
                    "kind": kind,
                    "id": anchor(e["sport"], e["match_id"]),
                    "when": when(e["kickoff_utc"], site.now),
                    "chart": spark(price_series(obs, e["side"])),
                    "sides": [SIDE_ZH[s] for s in sides],
                    "timeline": timeline_rows(obs, sides, read),
                }
            )
    events = [
        m
        for m in site.history
        if m.get("closed") and any(ev["type"] == "radar" for ev in m["events"])
    ]
    history = [_event_match(m, ("radar",), site) for m in events]
    return {
        **_base(site, "radar", "延遲雷達"),
        "cards": cards,
        "plan": view["plan"],
        "last": view["last"],
        "times": view.get("times", ""),
        "history": history[:HISTORY_LIMIT],
        "summary": site.history_summary.get("radar") or {},
        "kpis": {
            "candidates": len(view["candidates"]),
            "watch": view.get("watch_total", 0),
            "events": (site.history_summary.get("radar") or {}).get("events", 0),
        },
    }


def _event_match(m: Mapping[str, Any], kinds: tuple[str, ...], site: Site) -> dict[str, Any]:
    """One match of the 大戶 record (moves.event_history) for a history list, with the
    events of the given kinds and what went right or wrong for each."""
    v = bighand_view(m, site.cfg)
    events = [e for e in v["events"] if e["type"] in kinds]
    for e in events:
        e["lessons"] = _event_lessons(e)
    # The card's edge: what the first judged lesson says (the move held, or it did not).
    tone = next(
        (le["tone"] for e in events for le in e["lessons"] if le["tone"] != "neutral"), "neutral"
    )
    # The record keeps home-away; MLB is read away first, like its title (away @ home).
    h, _, a = str(m.get("score") or "").partition("-")
    score = (f"{a}–{h}" if m["sport"] == "mlb" else f"{h}–{a}") if a else ""
    return {
        **v,
        "events": events,
        "tone": tone,
        "score_text": score,
        "when": when(m.get("kickoff_utc"), site.now),
    }


def _event_lessons(e: Mapping[str, Any]) -> list[dict[str, str]]:
    """Plain words on what a caught move turned out to be."""
    out = []
    after = e.get("after")
    if e["type"] == "radar":
        price, close_price = e.get("tw_price"), e.get("close_tw_price")
        if close_price and price:
            if close_price < price - 0.005:
                out.append(
                    {
                        "text": f"運彩後來把價格調到 {close_price:.2f}：雷達抓到的確實是還沒跟上的價格。",
                        "tone": "good",
                    }
                )
            elif close_price > price + 0.005:
                out.append(
                    {
                        "text": f"運彩後來反而調高到 {close_price:.2f}：市場的移動沒有延續到運彩。",
                        "tone": "bad",
                    }
                )
            else:
                out.append({"text": "到開賽前運彩都沒改價：落後一直存在。", "tone": "good"})
    if after is not None:
        if after >= 0.005:
            out.append(
                {
                    "text": f"之後國際盤續往同一邊 {after * 100:+.1f}pp：這個動向是對的方向。",
                    "tone": "good",
                }
            )
        elif after <= -0.005:
            out.append(
                {"text": f"之後國際盤回落 {after * 100:+.1f}pp：動向沒有延續。", "tone": "bad"}
            )
    result = e.get("result")
    if result in ("win", "loss") and e.get("expected") is not None:
        word = "贏了" if result == "win" else "輸了"
        out.append(
            {
                "text": f"這一邊{word}（國際盤當時給 {e['expected'] * 100:.0f}%）：單場結果大多是運氣。",
                "tone": "neutral",
            }
        )
    return out


# ================================================================ 盤口移動
def moves_page(site: Site, by_match: Mapping[tuple[str, str], list]) -> dict[str, Any]:
    cfg = site.cfg
    upcoming_cards = upcoming(football_cards(site) + mlb_cards(site), site.now)
    tw_events = defaultdict(list)
    for m in site.history:
        for e in m["events"]:
            if e["type"] == "tw_only" and not m.get("closed"):
                tw_events[(m["sport"], str(m["match_id"]))].append(e)
    cards = []
    for c in upcoming_cards:
        sport, mid = _sport(c), _id(c)
        tracked = site.tracked.get((sport, mid)) or {}
        movers = [s for s in tracked.get("sides", []) if s["move"] > 0 and s["market"] == "h2h"]
        top = max(movers, key=lambda s: s["move"], default=None)
        evs = tw_events.get((sport, mid), [])
        if not evs and (top is None or top["move"] < cfg.radar_watch_move):
            continue
        side = top["side"] if top else evs[-1]["side"]
        market = top["market"] if top else evs[-1]["market"]
        line = top["line"] if top else evs[-1]["line"]
        rows = by_match.get((sport, mid), [])
        obs = observations(rows, market, line, parse_time(c.get("kickoff_utc")))
        first = next((o.intl[side] for o in obs if side in o.intl), None)

        def read(
            o: list[Obs], i: int, side: str = side, first: float | None = first
        ) -> tuple[str, str]:
            x = o[i]
            if side in x.intl and first is not None:
                move = x.intl[side] - first
                return (
                    f"比第一次 {move * 100:+.1f}pp",
                    "good" if move >= cfg.big_move_threshold else "neutral",
                )
            if side in x.tw and x.tw[side][1]:
                return f"運彩 {x.tw[side][1]:.2f}（隱含 {x.tw[side][0] * 100:.0f}%）", "neutral"
            return "", "neutral"

        home, away = names(c)
        sides = market_sides(sport, market)
        tw_block = (c.get("tw") or {}).get(market) or {}
        tw_side = tw_block.get(side) if isinstance(tw_block, dict) else None
        cards.append(
            {
                **_card_head(c, site),
                "big": bool(top and top["move"] >= cfg.big_move_threshold),
                "move": top,
                "tw_events": [
                    {**e, "label": side_label(sport, e["market"], e["side"], e["line"], home, away)}
                    for e in evs
                ],
                "label": side_label(sport, market, side, line, home, away),
                "ev": (tw_side or {}).get("ev"),
                "fresh": (c.get("tw") or {}).get("fair_fresh", True),
                "chart": spark(price_series(obs, side)),
                "sides": [SIDE_ZH[s] for s in sides],
                "timeline": timeline_rows(obs, sides, read),
            }
        )
    cards.sort(key=lambda c: (not c["big"], -(c["move"]["move"] if c["move"] else 0)))
    kinds = ("tw_only", "intl_big")
    history = [
        _event_match(m, kinds, site)
        for m in site.history
        if m.get("closed") and any(e["type"] in kinds for e in m["events"])
    ]
    summary = [
        {"label": label, **site.history_summary[kind]}
        for kind, label in (("intl_big", "國際大單"), ("tw_only", "運彩大戶"))
        if kind in site.history_summary
    ]
    return {
        **_base(site, "moves", "盤口移動"),
        "cards": cards,
        "history": history[:HISTORY_LIMIT],
        "summary": summary,
        "kpis": {
            "big": sum(1 for c in cards if c["big"]),
            "moving": len(cards),
            "tw_only": sum(len(c["tw_events"]) for c in cards),
            "record": sum(s.get("matches", 0) for s in summary),
        },
    }


# ================================================================ 模型分歧
# A 模型分歧 signal's market (analysis/football._lookup) -> the tape's market and side.
SIGNAL_MARKETS = {"1X2": "h2h", "大小": "totals", "讓球": "spread"}


def model_card(
    card: dict[str, Any],
    rows: list[Mapping[str, str]],
    signals: list[Mapping[str, Any]],
    site: Site,
) -> dict[str, Any]:
    """The model's 1X2 next to the market's, and the prices over time of the side the model
    leans to: the signal's own market when there is one, else the 1X2 outcome with the
    largest gap. The model's probability is the chart's reference line."""
    model = card.get("model") or {}
    market = (card.get("market") or {}).get("h2h") or {}
    home, away = names(card)
    outcomes = []
    for side, key, lab in (
        ("home", "p_home", f"主勝 {home}"),
        ("draw", "p_draw", "和局"),
        ("away", "p_away", f"客勝 {away}"),
    ):
        mp = model.get(key)
        fair = (market.get(side) or {}).get("fair")
        outcomes.append(
            {
                "side": side,
                "label": lab,
                "model": mp,
                "market": fair,
                "gap": (mp - fair) if mp is not None and fair is not None else None,
                "fair_odds": (market.get(side) or {}).get("fair_odds"),
            }
        )  # fmt: skip
    lean = max((o for o in outcomes if o["gap"] is not None), key=lambda o: o["gap"], default=None)
    signal = signals[0] if signals else None
    if signal:
        mkt = SIGNAL_MARKETS.get(str(signal.get("market")), "h2h")
        key = str(signal.get("selection_key"))
        side = key.removeprefix("spread_")
        raw_line = signal.get("line")
        line = "" if mkt == "h2h" or raw_line in (None, "") else f"{float(raw_line):g}"
        focus = {
            "market": mkt,
            "side": side,
            "line": line,
            "label": side_label("football", mkt, side, line, home, away),
            "model": signal.get("model_p"),
            "market_p": signal.get("market_p"),
            "gap": signal.get("edge"),
        }
    elif lean:
        focus = {"market": "h2h", "side": lean["side"], "line": "", "label": lean["label"],
                 "model": lean["model"], "market_p": lean["market"], "gap": lean["gap"]}  # fmt: skip
    else:
        focus = {"market": "h2h", "side": "home", "line": "", "label": f"主勝 {home}",
                 "model": None, "market_p": None, "gap": None}  # fmt: skip
    obs = observations(rows, focus["market"], focus["line"], parse_time(card.get("kickoff_utc")))
    side, model_p = focus["side"], focus["model"]

    def read(o: list[Obs], i: int) -> tuple[str, str]:
        x = o[i]
        if side in x.intl and model_p is not None:
            gap = model_p - x.intl[side]
            return (
                f"模型比國際 {gap * 100:+.1f}pp",
                "good" if gap >= site.cfg.edge_threshold else "neutral",
            )
        if side in x.tw and x.tw[side][1]:
            return f"運彩 {x.tw[side][1]:.2f}", "neutral"
        return "", "neutral"

    sides = market_sides("football", focus["market"])
    return {
        **_card_head(card, site),
        "fitted": bool(model.get("fitted")),
        "outcomes": outcomes,
        "lean": lean,
        "focus": focus,
        "signal": signal,
        "model": model,
        "notes": card.get("notes") or [],
        "chart": spark(
            price_series(obs, side),
            refs=[{"value": model_p, "label": "模型"}] if model_p is not None else [],
        ),
        "focus_label": focus["label"],
        "sides": [SIDE_ZH[s] for s in sides],
        "timeline": timeline_rows(obs, sides, read),
    }


def model_page(site: Site, by_match: Mapping[tuple[str, str], list]) -> dict[str, Any]:
    by_id = {_id(c): c for c in football_cards(site)}
    signals = defaultdict(list)
    for comp in (site.football or {}).get("competitions", []):
        for s in comp.get("signals", []):
            if signal_allowed(s, by_id.get(str(s.get("match_id"))), site.cfg):
                signals[str(s.get("match_id"))].append(s)
    cards = [
        model_card(c, by_match.get(("football", _id(c)), []), signals.get(_id(c), []), site)
        for c in upcoming(football_cards(site), site.now)
    ]
    cards.sort(key=lambda c: (c["signal"] is None, c["when"]["date"], c["when"]["time"]))
    reviews = review_views(site.football_reviews, [], site.signal_rows, site.value_rows)
    return {
        **_base(site, "model", "模型分歧"),
        "cards": cards,
        "reviews": reviews,
        "ledger": site.ledger_summary,
        "backtest_total": site.backtest_total,
        "backtest_exists": site.backtest_exists,
        "kpis": {
            "upcoming": len(cards),
            "signals": sum(1 for c in cards if c["signal"]),
            "reviewed": reviews["summary"].get("matches", 0),
        },
    }


def signal_allowed(
    signal: Mapping[str, Any], card: Mapping[str, Any] | None, cfg: ModelConfig
) -> bool:
    """The rules analysis/football.py applies when it builds a card, so a signal in a
    snapshot made before a rule change still obeys today's rules: no cup signals, no
    team with fewer than min_signal_matches fitted matches."""
    if signal.get("competition") == "CL" and not cfg.cup_signals:
        return False
    model = (card or {}).get("model") or {}
    counts = [model.get("home_matches"), model.get("away_matches")]
    return all(n is None or n >= cfg.min_signal_matches for n in counts)


# ================================================================ 反者道之動
def fade_page(site: Site) -> dict[str, Any]:
    live = [
        {
            **r,
            "names": [fade.STRATEGIES[k]["name"] for k in r["strategies"]],
            "id": anchor(r.get("sport_key", ""), r.get("match_id", "")),
            "when": when(r.get("kickoff_utc"), site.now),
        }
        for r in site.fade_live
    ]
    record = [
        {
            **r,
            "strategy_name": fade.STRATEGIES[r["strategy"]]["name"],
            "result_label": FADE_RESULTS.get(r["result"], r["result"]),
        }
        for r in site.fade_record
    ]
    return {
        **_base(site, "fade", "反者道之動"),
        "s": fade_summary_view(site.fade_summary) if site.fade_summary else None,
        "live": live,
        "record": record[:60],
        "record_n": len(record),
        "record_summary": [
            {"key": k, **fade.STRATEGIES[k], **v}
            for k, v in fade.record_summary(site.fade_record).items()
        ],
        "strategies": fade.STRATEGIES,
    }


# ===================================================================== 總覽
def hub_page(site: Site, pages: Mapping[str, dict[str, Any]]) -> dict[str, Any]:
    """The front page: what is coming up and which methods have something to say about
    each match, the methods with their counts, and when to upload next."""
    flags: dict[str, list[dict[str, str]]] = defaultdict(list)

    def flag(sport: str, mid: Any, page: str, label: str, tone: str) -> None:
        key = anchor(sport, mid)
        if not any(f["page"] == page and f["label"] == label for f in flags[key]):
            flags[key].append(
                {"page": page, "href": f"{page}.html#{key}", "label": label, "tone": tone}
            )

    for c in pages["value"]["cards"]:
        if c["status"] == "value":
            flag(c["sport"], c["id"].split("-")[-1], "value", "可下", "good")
    for c in pages["radar"]["cards"]:
        if c["kind"] == "candidate":
            flag(c["sport"], c["match_id"], "radar", "雷達", "good")
    for c in pages["moves"]["cards"]:
        if c["big"]:
            flag(c["sport"], c["id"].split("-")[-1], "moves", "大單", "warn")
        if c["tw_events"]:
            flag(c["sport"], c["id"].split("-")[-1], "moves", "運彩大戶", "warn")
    for c in pages["t10"]["cards"]:
        if c["shape"] and c["zone"]:
            flag("mlb", c["id"].split("-")[-1], "t10", c["shape"], "warn")
    # Most games carry some 反指標 and many a 模型分歧: only the ones worth acting on are
    # flagged loudly (a fade at a price that pays); 模型分歧 stays a quiet pointer.
    for r in site.fade_live:
        if r.get("status") == "value":
            flag(r.get("sport_key", ""), r.get("match_id", ""), "fade", "反指標可下", "good")
    for c in pages["model"]["cards"]:
        if c["signal"]:
            flag("football", c["id"].split("-")[-1], "model", "模型分歧", "ghost")

    horizon = site.now + timedelta(hours=BOARD_HOURS)
    board: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for c in upcoming(football_cards(site) + mlb_cards(site), site.now):
        start = parse_time(c.get("kickoff_utc"))
        if start is None or start > horizon:
            continue
        head = _card_head(c, site)
        day = f"{head['when']['date']} {head['when']['weekday']}"
        board[day].append(
            {
                **head,
                "flags": flags.get(head["id"], []),
                "on_tw": bool(c.get("tw")),
            }
        )
    plan = pages["radar"]["plan"]
    next_upload = next((p for p in plan if not p["done"]), None)
    v, t, r, m, mo = (pages[k]["kpis"] for k in ("value", "t10", "radar", "moves", "model"))
    tiles = [
        {"page": "value", "name": "運彩價值", "what": "運彩賠率比國際公允價高、EV 過門檻的注。",
         "now": f"可下 {v['value']}", "sub": f"有運彩價的比賽 {v['upcoming']} 場"},
        {"page": "t10", "name": "T-10", "what": "MLB：G 值看比賽型態，運彩四種下法各算 EV。",
         "now": f"可下 {t['value']}", "sub": f"MLB {t['upcoming']} 場，G 到 ±6 的 {t['zone']} 場"},
        {"page": "radar", "name": "延遲雷達", "what": "國際盤動了、運彩上次的賠率可能還沒跟上。",
         "now": f"候選 {r['candidates']}", "sub": f"國際盤在動 {r['watch']} 個"},
        {"page": "moves", "name": "盤口移動", "what": "國際大單和運彩自己的大戶動向。",
         "now": f"大單 {m['big']}", "sub": f"有在動的比賽 {m['moving']} 場"},
        {"page": "model", "name": "模型分歧", "what": "足球模型和市場看法差很多的比賽（觀察用）。",
         "now": f"分歧 {mo['signals']}", "sub": f"足球 {mo['upcoming']} 場"},
        {"page": "fade", "name": "反者道之動", "what": "回測最差的押法，反著做在運彩值不值得。",
         "now": f"可下 {sum(1 for x in site.fade_live if x.get('status') == 'value')}",
         "sub": f"今日反指標 {len(site.fade_live)} 個"},
    ]  # fmt: skip
    if v["value"]:
        headline, tone = f"現在有 {v['value']} 場可下", "good"
    elif r["candidates"]:
        headline, tone = f"雷達有 {r['candidates']} 個候選", "good"
    else:
        headline, tone = "目前沒有可下的注", "neutral"
    return {
        **_base(site, "index", "總覽"),
        "headline": headline,
        "headline_tone": tone,
        "counts": {
            "value": v["value"],
            "radar": r["candidates"],
            "big": m["big"],
            "matches": sum(len(ms) for ms in board.values()),
        },
        "board": [{"day": d, "matches": ms} for d, ms in board.items()],
        "tiles": tiles,
        "last": pages["radar"]["last"],
        "budget": site.budget,
        "next_upload": next_upload,
        "plan": plan,
        "reviews_n": len(site.football_reviews) + len(site.mlb_reviews),
    }


# ================================================================== reviews
def waiting(site: Site) -> list[dict[str, Any]]:
    """Matches that have kicked off and have no review yet; the ones a recommendation
    was made for are marked (the owner wants to see those first)."""
    reviewed = {("football", str(r.get("match_id"))) for r in site.football_reviews}
    reviewed |= {("mlb", str(r.get("game_pk"))) for r in site.mlb_reviews}
    picks = match_picks(site.signal_rows, site.value_rows)
    out = []
    for c in football_cards(site) + mlb_cards(site):
        start = parse_time(c.get("kickoff_utc"))
        key = (_sport(c), _id(c))
        if start is None or start > site.now or key in reviewed:
            continue
        home, away = names(c)
        out.append({"title": match_title(key[0], home, away), "recommended": key in picks})
    return out


def reviews_page(site: Site) -> dict[str, Any]:
    return {
        **_base(site, "reviews", "全部檢討"),
        "reviews": review_views(
            site.football_reviews, site.mlb_reviews, site.signal_rows, site.value_rows
        ),
        "waiting": waiting(site),
    }


# =================================================================== render
def build_pages(site: Site) -> dict[str, dict[str, Any]]:
    by_match = tape_by_match(site.tape)
    pages = {
        "value": value_page(site, by_match),
        "t10": t10_page(site, by_match),
        "radar": radar_page(site, by_match),
        "moves": moves_page(site, by_match),
        "model": model_page(site, by_match),
        "fade": fade_page(site),
        "reviews": reviews_page(site),
    }
    pages["index"] = hub_page(site, pages)
    return pages


def render_page(site: Site, key: str) -> str:
    """One page's HTML (tests use it)."""
    return _env().get_template(f"{key}.html.j2").render(**build_pages(site)[key])


def render_report(name: str, summary: Mapping[str, Any], docs: Path, now: datetime) -> Path:
    """A backtest report (docs/backtest.html, docs/t10_backtest.html) from its summary, in
    the site's frame; the backtest scripts write it the same way."""
    _, title, page = REPORTS[name]
    ctx = {
        "page": page,
        "title": title,
        "pages": PAGES,
        "updated": now.astimezone(LOCAL_TZ).strftime("%m/%d %H:%M"),
        "s": summary,
    }
    docs.mkdir(parents=True, exist_ok=True)
    path = docs / f"{name}.html"
    path.write_text(_env().get_template(f"{name}.html.j2").render(**ctx), encoding="utf-8")
    return path


def render_site(site: Site, docs: Path) -> dict[str, Path]:
    """Write every page; returns page key -> path. bighand.html (the old 大戶紀錄 address)
    becomes a pointer to the 盤口移動 history."""
    env = _env()
    docs.mkdir(parents=True, exist_ok=True)
    out = {}
    files = {k: f for k, _, f in PAGES}
    for key, ctx in build_pages(site).items():
        path = docs / files[key]
        path.write_text(env.get_template(f"{key}.html.j2").render(**ctx), encoding="utf-8")
        out[key] = path
    for name, summary in site.reports.items():
        out[name] = render_report(name, summary, docs, site.now)
    moved = docs / "bighand.html"
    moved.write_text(
        env.get_template("moved.html.j2").render(target="moves.html#history"), encoding="utf-8"
    )
    out["bighand"] = moved
    return out
