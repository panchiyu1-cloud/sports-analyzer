"""Post-match review: each finished match's pre-match card next to what happened.

Two questions are kept apart on purpose:

1. Outcome: on this match, who was closer, the model or the market? Measured by the
   log-loss of the actual result, -ln(probability given to what happened).
2. Process: was the model's lean reasonable? Evidence that does not depend on the
   final score: did the market move toward the lean before kickoff (the information
   the closing line adds), and did the run of play (xG, else shots on target) favour it?

A lean can be right and still lose (bad luck), or win for the wrong reasons (good
luck). One match mostly measures variance; the cumulative block (`summarize_*`) is what
says something about the model.

Rows are flat dicts so they can live in a CSV the owner can open in a spreadsheet.
Everything a reader sees (verdict, explanations) is derived from a row by
`explain_*`, so improving the wording later re-explains old matches too.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime
from typing import Any

from ..config import LOCAL_TZ
from ..fetch.football import Match
from ..fetch.mlb import BoxScore, MlbGame, PitcherLine
from ..history import MatchStats
from ..ledger import HALF_LOSS, HALF_WIN, LOSS, PUSH, WIN, grade, profit

# A model view at least this far above the market (1X2) counts as a lean.
LEAN_MIN_GAP = 0.05
# A pre-kickoff market move this large on the lean's side counts as information...
MOVE_MIN = 0.02
# ...but only between snapshots at least this far apart (two runs minutes apart are one price).
MOVE_MIN_HOURS = 3.0
# xG difference that counts as one side controlling a match.
XG_EDGE = 0.5
# Distance between the xG picture and a handicap/totals line that counts as support.
LINE_EDGE = 0.4
# Shots-on-target difference, used only when xG is missing.
SOT_EDGE = 3
# Goals away from the model's expectation worth a sentence.
GOAL_MISS = 1.5
# Goals minus xG beyond this is mostly finishing variance ("luck").
LUCK_GOALS = 1.2
# xG margin this far from the model's expected margin = the model misread the match.
VIEW_GOALS = 1.0

OUTCOME_LABELS = {"H": "主勝", "D": "和局", "A": "客勝"}
RESULT_TEXT = {
    WIN: "命中 ✓",
    HALF_WIN: "贏一半",
    LOSS: "未命中 ✗",
    HALF_LOSS: "輸一半",
    PUSH: "走盤（退回本金）",
}

VERDICTS: dict[str, tuple[str, str]] = {
    # code: (label, tone) - tone picks the chip colour on the page
    "right": ("判斷正確", "good"),
    "hit": ("命中（原因待確認）", "good"),
    "lucky": ("命中，但偏運氣", "warn"),
    "unlucky": ("判斷合理，結果不走運", "warn"),
    "miss": ("未命中（原因待確認）", "bad"),
    "wrong": ("判斷錯誤", "bad"),
    "push": ("走盤", "neutral"),
    "agree": ("模型與市場一致", "neutral"),
    "no_model": ("模型資料不足", "neutral"),
    "t10_hit": ("T-10 命中", "good"),
    "t10_miss": ("T-10 未命中", "bad"),
    "no_signal": ("T-10 無訊號", "neutral"),
}


# =========================================================================== shared
def collect_prematch(
    snapshots: Iterable[Mapping[str, Any]],
    cards_of: Callable[[Mapping[str, Any]], list[dict[str, Any]]],
    key: str,
) -> dict[str, dict[str, Any]]:
    """Per match: the last card made before kickoff, the earliest and latest market seen
    before kickoff (with the card that went with the earliest), and the newest 運彩 block.

    A snapshot's `generated_at` is when its numbers were made. A later 運彩 upload rewrites
    the file but keeps that stamp, so it is the right clock for "before kickoff".
    """
    out: dict[str, dict[str, Any]] = {}
    for snap in sorted(snapshots, key=lambda s: str(s.get("generated_at") or "")):
        made = _dt(snap.get("generated_at"))
        for card in cards_of(snap):
            kickoff = _dt(card.get("kickoff_utc"))
            if made is None or kickoff is None or made >= kickoff:
                continue
            entry = out.setdefault(str(card.get(key)), {})
            entry["card"] = card
            entry["card_at"] = snap.get("generated_at")
            if card.get("market"):
                if "open" not in entry:
                    entry["open"] = card["market"]
                    entry["open_at"] = snap.get("generated_at")
                    entry["first"] = card
                entry["close"] = card["market"]
                entry["close_at"] = snap.get("generated_at")
            tw = card.get("tw")
            if tw and _newer_tw(tw, entry.get("tw"), kickoff):
                entry["tw"] = tw
    return out


def side_prob(
    mv: Mapping[str, Any] | None, key: str, line: float | None, fresh_only: bool = False
) -> float | None:
    """Fair probability of one selection in a card's market block (None if not offered).

    `fresh_only` skips blocks an evening run carried over from the morning ("stale"),
    which would otherwise look like a market that never moved.
    """
    if not mv:
        return None
    if key in ("home", "draw", "away"):
        block = mv.get("h2h") or {}
        if fresh_only and block.get("stale"):
            return None
        return _num((block.get(key) or {}).get("fair"))
    name = "totals" if key in ("over", "under") else "spread" if key.startswith("spread_") else ""
    block = mv.get(name) if name else None
    if not block or line is None or (fresh_only and block.get("stale")):
        return None
    if abs(float(block.get("line", math.nan)) - line) > 1e-9:
        return None
    side = key if name == "totals" else key.split("_", 1)[1]
    return _num((block.get(side) or {}).get("fair"))


# ========================================================================= football
def football_row(
    entry: Mapping[str, Any],
    match: Match,
    stats: MatchStats | None,
    signal: Mapping[str, str] | None,
    now: datetime,
) -> dict[str, Any]:
    """One review row: the pre-match numbers, the result, the stats, and the assessment."""
    card = entry["card"]
    model = card.get("model") or {}
    fitted = bool(model.get("fitted"))
    m_open, m_close = entry.get("open") or {}, entry.get("close") or {}
    tw = entry.get("tw") or {}
    tw_h2h = tw.get("h2h") or {}
    notes = [str(n) for n in card.get("notes") or []]
    hg, ag = match.home_goals, match.away_goals
    row: dict[str, Any] = {
        "match_id": str(match.id),
        "competition": card.get("competition", match.competition),
        "competition_name": card.get("competition_name", ""),
        "kickoff_utc": card.get("kickoff_utc", ""),
        "kickoff_local": card.get("kickoff_local", ""),
        "home": card.get("home", match.home),
        "away": card.get("away", match.away),
        "model_ok": _flag(fitted and bool(model.get("data_ok"))),
        "pooled": _flag(any(n.startswith("跨聯賽") for n in notes)),
        "model_note": next((n for n in notes if n.startswith("資料不足")), ""),
        "model_h": model.get("p_home") if fitted else None,
        "model_d": model.get("p_draw") if fitted else None,
        "model_a": model.get("p_away") if fitted else None,
        "model_xg_h": model.get("lambda_home") if fitted else None,
        "model_xg_a": model.get("mu_away") if fitted else None,
        "model_over25": model.get("over25") if fitted else None,
        "most_likely": model.get("most_likely", "") if fitted else "",
        "open_h": side_prob(m_open, "home", None),
        "open_d": side_prob(m_open, "draw", None),
        "open_a": side_prob(m_open, "away", None),
        "open_at": entry.get("open_at", ""),
        "close_h": side_prob(m_close, "home", None, fresh_only=True),
        "close_d": side_prob(m_close, "draw", None, fresh_only=True),
        "close_a": side_prob(m_close, "away", None, fresh_only=True),
        "close_at": entry.get("close_at", ""),
        "mkt_over25": side_prob(m_close, "over", 2.5) or side_prob(m_open, "over", 2.5),
        "tw_h": _num((tw_h2h.get("home") or {}).get("price")),
        "tw_d": _num((tw_h2h.get("draw") or {}).get("price")),
        "tw_a": _num((tw_h2h.get("away") or {}).get("price")),
        "tw_at": tw.get("captured_at", "") if tw_h2h else "",
        "ft_h": hg,
        "ft_a": ag,
        "ht_h": match.home_ht if match.home_ht is not None else (stats.ht_home if stats else None),
        "ht_a": match.away_ht if match.away_ht is not None else (stats.ht_away if stats else None),
        "outcome": _outcome(hg, ag),
        **_stats_fields(stats),
        "reviewed_at": now.isoformat(timespec="seconds"),
    }
    row.update(_lean_fields(entry, signal))
    row.update(assess_football(row))
    return row


def _lean_fields(entry: Mapping[str, Any], signal: Mapping[str, str] | None) -> dict[str, Any]:
    """What the model leaned toward when the card was first published.

    A 模型分歧 signal (ledger row) is the lean when there is one. Otherwise the 1X2 side
    where the first card's model sat furthest above the market, if at least LEAN_MIN_GAP.
    The first card is used (not the last) because a market that later moves toward the
    model would shrink the gap and hide a lean that turned out to be right.
    """
    m_open, m_close = entry.get("open") or {}, entry.get("close") or {}
    empty = {
        "lean_source": "",
        "lean_key": "",
        "lean_line": None,
        "lean_label": "",
        "lean_model_p": None,
        "lean_open_p": None,
        "lean_close_p": None,
        "lean_price": None,
    }
    if signal:
        key, line = signal.get("selection_key", ""), _num(signal.get("line"))
        return {
            "lean_source": "signal",
            "lean_key": key,
            "lean_line": line,
            "lean_label": signal.get("selection", ""),
            "lean_model_p": _num(signal.get("model_p")),
            "lean_open_p": side_prob(m_open, key, line) or _num(signal.get("market_p")),
            "lean_close_p": side_prob(m_close, key, line, fresh_only=True),
            "lean_price": _num(signal.get("price")),
        }
    first = entry.get("first") or {}
    model = first.get("model") or {}
    if not (model.get("fitted") and model.get("data_ok")):
        return empty
    best: tuple[str, float, float, float] | None = None
    for key, mk in (("home", "p_home"), ("draw", "p_draw"), ("away", "p_away")):
        o, p = side_prob(m_open, key, None), _num(model.get(mk))
        if o is None or p is None:
            continue
        if best is None or p - o > best[1]:
            best = (key, p - o, p, o)
    if best is None or best[1] < LEAN_MIN_GAP:
        return empty
    key = best[0]
    labels = {
        "home": f"主勝 {first.get('home', '')}",
        "draw": "和局",
        "away": f"客勝 {first.get('away', '')}",
    }
    h2h = (m_open.get("h2h") or {}).get(key) or {}
    return {
        "lean_source": "model",
        "lean_key": key,
        "lean_line": None,
        "lean_label": labels[key],
        "lean_model_p": round(best[2], 4),
        "lean_open_p": best[3],
        "lean_close_p": side_prob(m_close, key, None, fresh_only=True),
        "lean_price": _num(h2h.get("best")),
    }


def assess_football(row: Mapping[str, Any]) -> dict[str, Any]:
    """Derived columns: per-match log-loss, how the lean settled, evidence, verdict."""
    outcome = str(row.get("outcome") or "")
    model_ok = str(row.get("model_ok")) == "1"
    side = {"H": "h", "D": "d", "A": "a"}.get(outcome)
    mp = _f(row, f"model_{side}") if side else None
    cp = (_f(row, f"close_{side}") or _f(row, f"open_{side}")) if side else None
    out: dict[str, Any] = {
        "model_ll": round(-math.log(mp), 4) if model_ok and mp else None,
        "market_ll": round(-math.log(cp), 4) if cp else None,
    }

    key, line = str(row.get("lean_key") or ""), _f(row, "lean_line")
    hg, ag = _i(row, "ft_h"), _i(row, "ft_a")
    result = grade(key, line, hg, ag) if key and hg is not None and ag is not None else ""
    price = _f(row, "lean_price")
    out["lean_result"] = result
    out["lean_profit"] = round(profit(result, price), 3) if result and price else None

    move = None
    o, c = _f(row, "lean_open_p"), _f(row, "lean_close_p")
    if (
        key
        and o is not None
        and c is not None
        and _hours(row.get("open_at"), row.get("close_at")) >= MOVE_MIN_HOURS
    ):
        move = c - o
    out["move_pp"] = round(move * 100, 1) if move is not None else None
    move_sign = 0 if move is None or abs(move) < MOVE_MIN else (1 if move > 0 else -1)
    stats_sign, basis = _stats_sign(key, line, row) if key else (0, "")
    out["move_sign"] = move_sign
    out["stats_sign"] = stats_sign
    out["stats_basis"] = basis

    if not key:
        out["verdict"] = "agree" if model_ok else "no_model"
    elif result in (WIN, HALF_WIN):
        score = move_sign + stats_sign
        out["verdict"] = "right" if score > 0 else ("lucky" if score < 0 else "hit")
    elif result in (LOSS, HALF_LOSS):
        score = move_sign + stats_sign
        out["verdict"] = "unlucky" if score > 0 else ("wrong" if score < 0 else "miss")
    else:
        out["verdict"] = "push"
    return out


def _stats_sign(key: str, line: float | None, row: Mapping[str, Any]) -> tuple[int, str]:
    """+1 if the run of play favoured the lean, -1 if it went against it, 0 if unclear."""
    xh, xa = _f(row, "xg_h"), _f(row, "xg_a")
    if xh is not None and xa is not None:
        d, tot = xh - xa, xh + xa
        if key in ("home", "away"):
            v = d if key == "home" else -d
            return (1 if v >= XG_EDGE else -1 if v <= -XG_EDGE else 0), "xG"
        if key == "draw":
            return (1 if abs(d) < XG_EDGE * 0.8 else -1 if abs(d) >= 2 * XG_EDGE else 0), "xG"
        if line is None:
            return 0, ""
        if key in ("spread_home", "spread_away"):
            v = d + line if key == "spread_home" else -(d + line)
        elif key in ("over", "under"):
            v = tot - line if key == "over" else line - tot
        else:
            return 0, ""
        return (1 if v >= LINE_EDGE else -1 if v <= -LINE_EDGE else 0), "xG"
    sh, sa = _i(row, "sot_h"), _i(row, "sot_a")
    if sh is not None and sa is not None and key in ("home", "away", "draw"):
        d = sh - sa
        if key == "draw":
            return (1 if abs(d) <= 1 else -1 if abs(d) >= SOT_EDGE + 1 else 0), "射正"
        v = d if key == "home" else -d
        return (1 if v >= SOT_EDGE else -1 if v <= -SOT_EDGE else 0), "射正"
    return 0, ""


def explain_football(row: Mapping[str, Any]) -> dict[str, Any]:
    """Everything the page shows for one finished match, in reading order."""
    a = assess_football(row)
    home, away = str(row.get("home") or "主隊"), str(row.get("away") or "客隊")
    hg, ag = _i(row, "ft_h"), _i(row, "ft_a")
    outcome = str(row.get("outcome") or "")
    side = {"H": "h", "D": "d", "A": "a"}.get(outcome, "")
    model_ok = str(row.get("model_ok")) == "1"
    bullets: list[str] = []

    # 1. The lean / signal and how it settled.
    key = str(row.get("lean_key") or "")
    if key:
        mp, op = _f(row, "lean_model_p"), _f(row, "lean_open_p")
        gap = f"，分歧 {_pp(mp - op)}" if mp is not None and op is not None else ""
        what = "模型分歧" if row.get("lean_source") == "signal" else "模型偏向"
        money = ""
        if a["lean_profit"] is not None and row.get("lean_source") == "signal":
            money = f"（帳面 {a['lean_profit']:+.2f} 單位 @ {_f(row, 'lean_price')}）"
        margin = _lean_margin_text(key, home, away, hg, ag)
        bullets.append(
            f"{what}：{row.get('lean_label')}（模型 {_pct(mp)} vs 市場 {_pct(op)}{gap}）"
            f"→ {RESULT_TEXT.get(a['lean_result'], '—')}{margin}{money}"
        )

    # 2. Who was closer, and how surprising the result was.
    cp = _f(row, f"close_{side}") or _f(row, f"open_{side}") if side else None
    mp_out = _f(row, f"model_{side}") if side else None
    if cp is not None:
        text = (
            f"實際{OUTCOME_LABELS.get(outcome, '結果')}：市場賽前給 {_pct(cp)}（{_surprise(cp)}）"
        )
        if model_ok and mp_out is not None:
            text += f"，模型給 {_pct(mp_out)}"
            if a["model_ll"] is not None and a["market_ll"] is not None:
                diff = a["market_ll"] - a["model_ll"]
                text += (
                    " → 這場模型比市場準"
                    if diff > 0.02
                    else " → 這場市場比模型準"
                    if diff < -0.02
                    else " → 兩者差不多"
                )
        bullets.append(text)

    # 3. What the market did before kickoff.
    if key:
        move = a["move_pp"]
        if move is None:
            if _hours(row.get("open_at"), row.get("close_at")) < MOVE_MIN_HOURS:
                bullets.append("賽前只抓到一次盤口，看不出市場後來往哪邊移動。")
        elif a["move_sign"] > 0:
            bullets.append(
                f"開賽前市場往模型的方向移動（{row.get('lean_label')} {_pct(_f(row, 'lean_open_p'))}"
                f" → {_pct(_f(row, 'lean_close_p'))}，{move:+.1f}pp）：模型可能抓到了市場後來才反映的東西。"
            )
        elif a["move_sign"] < 0:
            bullets.append(
                f"開賽前市場往反方向移動（{row.get('lean_label')} {_pct(_f(row, 'lean_open_p'))}"
                f" → {_pct(_f(row, 'lean_close_p'))}，{move:+.1f}pp）：多半是傷兵、輪換或先發名單"
                "等模型看不到的消息。"
            )
        else:
            bullets.append(f"開賽前市場幾乎沒動（{move:+.1f}pp）。")

    # 4. Goals against the model's expectation.
    lam, mu = _f(row, "model_xg_h"), _f(row, "model_xg_a")
    if not model_ok:
        lam = mu = None  # numbers from a model without enough data would only mislead
    if lam is not None and mu is not None and hg is not None and ag is not None:
        diff = (hg - ag) - (lam - mu)
        text = f"模型預期進球 {lam:.1f} : {mu:.1f}，實際 {hg} : {ag}"
        if diff >= GOAL_MISS:
            text += f" —— {home} 的表現比模型預期好很多（淨勝差多 {diff:.1f} 球）"
        elif diff <= -GOAL_MISS:
            text += f" —— {away} 的表現比模型預期好很多（淨勝差多 {-diff:.1f} 球）"
        else:
            text += " —— 勝負差距在模型預期範圍內"
        total_diff = (hg + ag) - (lam + mu)
        if abs(total_diff) >= GOAL_MISS:
            text += f"；總進球 {hg + ag}，{'比預期開放' if total_diff > 0 else '比預期沉悶'}"
        bullets.append(text + "。")

    # 5. Run of play: xG (domestic leagues, a few days later) or shots on target.
    bullets.extend(_play_bullets(row, home, away, lam, mu, hg, ag))

    # 6. Red cards and the half-time story.
    for team, reds in ((home, _i(row, "red_h")), (away, _i(row, "red_a"))):
        if reds:
            bullets.append(
                f"{team} 吃到 {reds} 張紅牌：少一人作戰會大幅改變比賽，這是賽前無法預測的事件。"
            )
    ht = _half_time_story(row, home, away)
    if ht:
        bullets.append(ht)

    # 7. 運彩 price that was on offer.
    tw = [(lab, _f(row, f"tw_{s}")) for lab, s in (("主", "h"), ("和", "d"), ("客", "a"))]
    if any(p for _, p in tw):
        at = _local(row.get("tw_at"))
        bullets.append(
            "運彩賠率 "
            + " · ".join(f"{lab} {p:g}" for lab, p in tw if p)
            + (f"（{at} 擷取）" if at else "")
        )

    # 8. Structural reasons the model's numbers deserve less weight here.
    if str(row.get("pooled")) == "1" and key:
        bullets.append(
            "歐冠用的是跨聯賽合併模型：各聯賽球隊的強弱只靠少數歐戰比賽互相連結，"
            "所以歐冠的分歧常常來自模型本身的限制，不代表市場看錯。"
        )
    if not model_ok and row.get("model_note"):
        bullets.append(f"{row.get('model_note')}：這場的模型數字僅供參考，不算模型判斷。")

    return {
        "verdict": a["verdict"],
        "label": VERDICTS[a["verdict"]][0],
        "tone": VERDICTS[a["verdict"]][1],
        "bullets": bullets,
        "conclusion": _football_conclusion(row, a),
        "assess": a,
    }


def _play_bullets(
    row: Mapping[str, Any],
    home: str,
    away: str,
    lam: float | None,
    mu: float | None,
    hg: int | None,
    ag: int | None,
) -> list[str]:
    xh, xa = _f(row, "xg_h"), _f(row, "xg_a")
    shots = ""
    if _i(row, "shots_h") is not None and _i(row, "shots_a") is not None:
        shots = f"射門 {_i(row, 'shots_h')} : {_i(row, 'shots_a')}"
        if _i(row, "sot_h") is not None and _i(row, "sot_a") is not None:
            shots += f"，射正 {_i(row, 'sot_h')} : {_i(row, 'sot_a')}"
    out: list[str] = []
    if xh is not None and xa is not None and hg is not None and ag is not None:
        out.append(
            f"實際 xG（機會品質）{xh:.1f} : {xa:.1f}" + (f"（{shots}）" if shots else "") + "。"
        )
        luck = (hg - ag) - (xh - xa)
        if luck >= LUCK_GOALS:
            out.append(
                f"比分比機會品質好看：{home} 的把握度（或運氣）多出約 {luck:.1f} 球 —— 這部分是運氣，不是模型能預測的。"
            )
        elif luck <= -LUCK_GOALS:
            out.append(
                f"比分比機會品質好看：{away} 的把握度（或運氣）多出約 {-luck:.1f} 球 —— 這部分是運氣，不是模型能預測的。"
            )
        if lam is not None and mu is not None:
            view = (xh - xa) - (lam - mu)
            if view >= VIEW_GOALS:
                out.append(
                    f"連機會品質都是 {home} 明顯佔優（xG 差 {xh - xa:+.1f}，模型預期 {lam - mu:+.1f}）：模型低估了 {home}。"
                )
            elif view <= -VIEW_GOALS:
                out.append(
                    f"連機會品質都是 {away} 明顯佔優（xG 差 {xh - xa:+.1f}，模型預期 {lam - mu:+.1f}）：模型低估了 {away}。"
                )
            else:
                out.append("機會品質大致符合模型預期：模型對這場比賽的判讀沒有大錯。")
    elif shots:
        out.append(shots + "（這場沒有 xG）。")
    elif str(row.get("competition")) == "CL":
        out.append("歐冠沒有免費的比賽數據（xG、射門），無法拆開「判讀錯」和「運氣差」。")
    else:
        out.append("比賽數據（xG、射門、紅牌）通常 2–3 天後才公布，屆時這段分析會自動補上。")
    return out


def _lean_margin_text(key: str, home: str, away: str, hg: int | None, ag: int | None) -> str:
    """Plain words for how a handicap or totals lean was settled."""
    if hg is None or ag is None:
        return ""
    if key in ("spread_home", "spread_away"):
        team, margin = (home, hg - ag) if key == "spread_home" else (away, ag - hg)
        if margin > 0:
            return f"（{team} 贏 {margin} 球）"
        if margin < 0:
            return f"（{team} 輸 {-margin} 球）"
        return "（打平）"
    if key in ("over", "under"):
        return f"（總進球 {hg + ag}）"
    return ""


def _half_time_story(row: Mapping[str, Any], home: str, away: str) -> str:
    hh, ha, fh, fa = _i(row, "ht_h"), _i(row, "ht_a"), _i(row, "ft_h"), _i(row, "ft_a")
    if None in (hh, ha, fh, fa):
        return ""
    ht, ft = _outcome(hh, ha), _outcome(fh, fa)
    if ht == ft:
        return ""
    if ht == "H":
        return f"半場 {hh}–{ha} 由 {home} 領先，下半場{'被逆轉' if ft == 'A' else '被扳平'}。"
    if ht == "A":
        return f"半場 {hh}–{ha} 由 {away} 領先，下半場{'被逆轉' if ft == 'H' else '被扳平'}。"
    return f"半場 {hh}–{ha} 平手，勝負在下半場才決定。"


def _football_conclusion(row: Mapping[str, Any], a: Mapping[str, Any]) -> str:
    support = _evidence_text(a, positive=True)
    against = _evidence_text(a, positive=False)
    surprise = ""
    side = {"H": "h", "D": "d", "A": "a"}.get(str(row.get("outcome") or ""))
    cp = (_f(row, f"close_{side}") or _f(row, f"open_{side}")) if side else None
    if cp is not None:
        surprise = _surprise(cp)
    return {
        "right": f"判斷有{support}支持，結果也對。",
        "hit": "命中了，但沒有額外證據證明是判斷正確——單場命中不代表模型有優勢。",
        "lucky": f"雖然命中，但{against}對模型不利，這次比較像運氣。",
        "unlucky": f"判斷有{support}支持，輸在比賽中的隨機性（把握度、意外事件）。這類輸球不需要因此改策略。",
        "miss": "沒命中，目前也沒有證據顯示判斷合理。同類分歧如果持續輸，就是該調整的訊號。",
        "wrong": f"{against}都對模型不利：模型漏看了重要資訊，這類分歧要降低信任。",
        "push": "走盤，不輸不贏。",
        "agree": f"模型和市場看法一致，結果{surprise}。" if surprise else "模型和市場看法一致。",
        "no_model": f"模型資料不足，這場只看市場：結果{surprise}。"
        if surprise
        else "模型資料不足，這場只看市場。",
    }[str(a["verdict"])]


def _evidence_text(a: Mapping[str, Any], positive: bool) -> str:
    want = 1 if positive else -1
    parts = []
    if a.get("move_sign") == want:
        parts.append("賽前市場移動")
    if a.get("stats_sign") == want:
        parts.append(f"比賽{a.get('stats_basis') or '數據'}")
    return "、".join(parts) or "證據"


def summarize_football(rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    """Cumulative diagnostics: is the model better than the market, and where is it biased?"""
    done = [r for r in rows if str(r.get("outcome") or "") in ("H", "D", "A")]
    assessed = [(r, assess_football(r)) for r in done]
    scored = [
        (r, a) for r, a in assessed if a["model_ll"] is not None and a["market_ll"] is not None
    ]
    leans = [(r, a) for r, a in assessed if a["lean_result"] in (WIN, LOSS, HALF_WIN, HALF_LOSS)]
    weight = {WIN: (1.0, 1.0), LOSS: (0.0, 1.0), HALF_WIN: (0.5, 0.5), HALF_LOSS: (0.0, 0.5)}

    def lean_block(items: list[tuple[Mapping[str, Any], Mapping[str, Any]]]) -> dict[str, Any]:
        hits = sum(weight[a["lean_result"]][0] for _, a in items)
        decided = sum(weight[a["lean_result"]][1] for _, a in items)
        # What the market expected the lean to win: its fair probability, half-weighted on
        # quarter lines the same way the result is.
        expected = sum(
            (_f(r, "lean_close_p") or _f(r, "lean_open_p") or 0.0) * weight[a["lean_result"]][1]
            for r, a in items
        )
        units = sum(a["lean_profit"] or 0.0 for r, a in items if r.get("lean_source") == "signal")
        return {
            "n": len(items),
            "hits": round(hits, 1),
            "decided": round(decided, 1),
            "expected": round(expected, 1),
            "signals": sum(1 for r, _ in items if r.get("lean_source") == "signal"),
            "units": round(units, 2),
        }

    def ll_block(items: list[tuple[Mapping[str, Any], Mapping[str, Any]]]) -> dict[str, Any]:
        n = len(items)
        return {
            "n": n,
            "model_ll": round(sum(a["model_ll"] for _, a in items) / n, 3) if n else None,
            "market_ll": round(sum(a["market_ll"] for _, a in items) / n, 3) if n else None,
            "model_closer": sum(1 for _, a in items if a["model_ll"] < a["market_ll"] - 0.02),
            "market_closer": sum(1 for _, a in items if a["market_ll"] < a["model_ll"] - 0.02),
        }

    goals = [r for r in done if str(r.get("model_ok")) == "1" and _f(r, "model_xg_h") is not None]
    xg = [r for r in goals if _f(r, "xg_h") is not None]
    by_comp = []
    for code in sorted({str(r.get("competition")) for r in done}):
        comp_rows = [(r, a) for r, a in assessed if str(r.get("competition")) == code]
        name = next(
            (str(r.get("competition_name")) for r, _ in comp_rows if r.get("competition_name")),
            code,
        )
        by_comp.append(
            {
                "code": code,
                "name": name,
                "matches": len(comp_rows),
                **{
                    f"ll_{k}": v for k, v in ll_block([x for x in comp_rows if x in scored]).items()
                },
                **{
                    f"lean_{k}": v
                    for k, v in lean_block([x for x in comp_rows if x in leans]).items()
                },
            }
        )
    summary = {
        "matches": len(done),
        **ll_block(scored),
        "leans": lean_block(leans),
        "verdicts": dict(Counter(a["verdict"] for _, a in assessed)),
        "goal_bias_h": _mean([_f(r, "model_xg_h") - _f(r, "ft_h") for r in goals]),
        "goal_bias_a": _mean([_f(r, "model_xg_a") - _f(r, "ft_a") for r in goals]),
        "n_goals": len(goals),
        "xg_bias_h": _mean([_f(r, "model_xg_h") - _f(r, "xg_h") for r in xg]),
        "xg_bias_a": _mean([_f(r, "model_xg_a") - _f(r, "xg_a") for r in xg]),
        "n_xg": len(xg),
        "by_comp": by_comp,
    }
    summary["takeaways"] = _football_takeaways(summary)
    return summary


def _football_takeaways(s: Mapping[str, Any]) -> list[str]:
    out: list[str] = []
    n = s["n"]
    if n < 10:
        out.append(
            f"樣本還少（{n} 場有模型和市場可比）。先累積：30 場以上再看趨勢，100 場以上才算可靠。"
        )
    elif s["model_ll"] is not None and s["market_ll"] is not None:
        if s["market_ll"] < s["model_ll"]:
            out.append(
                f"市場的平均 log-loss {s['market_ll']:.3f} 低於模型的 {s['model_ll']:.3f}："
                f"整體來說市場比模型準（{n} 場），模型分歧不能直接當下注依據。"
            )
        else:
            out.append(
                f"模型的平均 log-loss {s['model_ll']:.3f} 低於市場的 {s['market_ll']:.3f}（{n} 場）"
                "——如果 100 場以上還維持，才代表模型真的有市場沒有的資訊。"
            )
    leans = s["leans"]
    if leans["decided"] >= 10:
        if leans["hits"] < leans["expected"]:
            out.append(
                f"模型偏向的一方實際贏了 {leans['hits']:g} 次，照市場機率應該贏 {leans['expected']:g} 次："
                "分歧的方向沒有帶來額外資訊，比較像雜訊。"
            )
        else:
            out.append(
                f"模型偏向的一方贏了 {leans['hits']:g} 次，比市場預期的 {leans['expected']:g} 次多"
                "（樣本還小，繼續觀察）。"
            )
    nb, basis = (s["n_xg"], "xG") if s["n_xg"] >= 10 else (s["n_goals"], "實際進球")
    bias_h = s["xg_bias_h"] if basis == "xG" else s["goal_bias_h"]
    bias_a = s["xg_bias_a"] if basis == "xG" else s["goal_bias_a"]
    if nb >= 10:
        for who, bias in (("主隊", bias_h), ("客隊", bias_a)):
            if bias is not None and abs(bias) >= 0.25:
                out.append(
                    f"和{basis}相比，模型平均{'高估' if bias > 0 else '低估'}{who}進球 {abs(bias):.2f} 球（{nb} 場）。"
                )
    for comp in s["by_comp"]:
        if comp["lean_decided"] >= 5 and comp["lean_hits"] < comp["lean_expected"] - 1:
            out.append(
                f"{comp['name']}的分歧表現低於市場預期（贏 {comp['lean_hits']:g}，"
                f"市場預期 {comp['lean_expected']:g}）：{comp['name']}可以考慮只看市場。"
            )
    return out


# ============================================================================== MLB
def mlb_row(
    entry: Mapping[str, Any], game: MlbGame, box: BoxScore | None, now: datetime
) -> dict[str, Any]:
    card = entry["card"]
    m_open, m_close = entry.get("open") or {}, entry.get("close") or {}
    t10 = card.get("t10") or {}
    tw = entry.get("tw") or {}
    tw_h2h = tw.get("h2h") or {}
    spread = (m_close.get("spread") or m_open.get("spread")) or {}
    totals = (m_close.get("totals") or m_open.get("totals")) or {}
    row: dict[str, Any] = {
        "game_pk": str(game.game_pk),
        "kickoff_utc": card.get("kickoff_utc", ""),
        "local": card.get("local", ""),
        "home": card.get("home", game.home),
        "away": card.get("away", game.away),
        "home_probable": card.get("home_probable") or "",
        "away_probable": card.get("away_probable") or "",
        "open_home": side_prob(m_open, "home", None),
        "open_away": side_prob(m_open, "away", None),
        "open_at": entry.get("open_at", ""),
        "close_home": side_prob(m_close, "home", None, fresh_only=True),
        "close_away": side_prob(m_close, "away", None, fresh_only=True),
        "close_at": entry.get("close_at", ""),
        "rl_line": _num(spread.get("line")),
        "rl_home": _num((spread.get("home") or {}).get("fair")),
        "rl_away": _num((spread.get("away") or {}).get("fair")),
        "total_line": _num(totals.get("line")),
        "over_p": _num((totals.get("over") or {}).get("fair")),
        "t10_g": _num(t10.get("g")),
        "t10_fav": t10.get("favourite", ""),
        "t10_signal": t10.get("signal") or "",
        "t10_threshold": _num(t10.get("threshold")),
        "tw_home": _num((tw_h2h.get("home") or {}).get("price")),
        "tw_away": _num((tw_h2h.get("away") or {}).get("price")),
        "tw_at": tw.get("captured_at", "") if tw_h2h else "",
        "final_h": game.home_score,
        "final_a": game.away_score,
        **_box_fields(box),
        "reviewed_at": now.isoformat(timespec="seconds"),
    }
    row.update(assess_mlb(row))
    return row


def _box_fields(box: BoxScore | None) -> dict[str, Any]:
    def innings(runs: tuple[int | None, ...]) -> str:
        return " ".join("x" if r is None else str(r) for r in runs)

    def sp(prefix: str, line: PitcherLine | None) -> dict[str, Any]:
        if line is None:
            return {
                f"{prefix}": "",
                f"{prefix}_ip": "",
                f"{prefix}_r": None,
                f"{prefix}_er": None,
                f"{prefix}_h": None,
                f"{prefix}_bb": None,
                f"{prefix}_k": None,
            }
        return {
            f"{prefix}": line.name,
            f"{prefix}_ip": line.ip,
            f"{prefix}_r": line.runs,
            f"{prefix}_er": line.earned_runs,
            f"{prefix}_h": line.hits,
            f"{prefix}_bb": line.walks,
            f"{prefix}_k": line.strikeouts,
        }

    if box is None:
        return {
            "hits_h": None,
            "hits_a": None,
            "errors_h": None,
            "errors_a": None,
            "innings_h": "",
            "innings_a": "",
            **sp("sp_h", None),
            **sp("sp_a", None),
        }
    return {
        "hits_h": box.hits_home,
        "hits_a": box.hits_away,
        "errors_h": box.errors_home,
        "errors_a": box.errors_away,
        "innings_h": innings(box.innings_home),
        "innings_a": innings(box.innings_away),
        **sp("sp_h", box.starter_home),
        **sp("sp_a", box.starter_away),
    }


def assess_mlb(row: Mapping[str, Any]) -> dict[str, Any]:
    fh, fa = _i(row, "final_h"), _i(row, "final_a")
    if fh is None or fa is None or fh == fa:
        return {
            "winner": "",
            "market_ll": None,
            "fav": "",
            "fav_won": "",
            "fav_cover": "",
            "t10_result": "",
            "verdict": "no_signal",
        }
    winner = "home" if fh > fa else "away"
    p_home = _f(row, "close_home") or _f(row, "open_home")
    p_away = _f(row, "close_away") or _f(row, "open_away")
    p_win = p_home if winner == "home" else p_away
    fav = str(row.get("t10_fav") or "")
    if not fav and p_home is not None and p_away is not None:
        fav = "home" if p_home >= p_away else "away"
    margin = None
    if fav:
        margin = (fh - fa) if fav == "home" else (fa - fh)
    signal = str(row.get("t10_signal") or "")
    t10_result = ""
    if signal == "FAV_ML" and margin is not None:
        t10_result = WIN if margin > 0 else LOSS
    elif signal == "DOG_PLUS_1_5" and margin is not None:
        t10_result = WIN if margin <= 1 else LOSS
    return {
        "winner": winner,
        "market_ll": round(-math.log(p_win), 4) if p_win else None,
        "fav": fav,
        "fav_won": _flag(margin is not None and margin > 0) if margin is not None else "",
        "fav_cover": _flag(margin is not None and margin >= 2) if margin is not None else "",
        "t10_result": t10_result,
        "verdict": "t10_hit"
        if t10_result == WIN
        else "t10_miss"
        if t10_result == LOSS
        else "no_signal",
    }


def explain_mlb(row: Mapping[str, Any]) -> dict[str, Any]:
    a = assess_mlb(row)
    home, away = str(row.get("home") or "主隊"), str(row.get("away") or "客隊")
    fh, fa = _i(row, "final_h"), _i(row, "final_a")
    name = {"home": home, "away": away}
    bullets: list[str] = []
    if fh is not None and fa is not None:
        line = f"比分 {away} {fa} : {fh} {home}"
        if _i(row, "hits_h") is not None:
            line += f" · 安打 {_i(row, 'hits_a')} : {_i(row, 'hits_h')}"
        if _i(row, "errors_h") is not None:
            line += f" · 失誤 {_i(row, 'errors_a')} : {_i(row, 'errors_h')}"
        bullets.append(line)
    winner = a["winner"]
    if winner:
        p = _f(row, f"close_{winner}") or _f(row, f"open_{winner}")
        if p is not None:
            bullets.append(f"{name[winner]} 勝：市場賽前給 {_pct(p)}（{_surprise(p)}）。")
        o, c = _f(row, "open_home"), _f(row, "close_home")
        if (
            o is not None
            and c is not None
            and _hours(row.get("open_at"), row.get("close_at")) >= MOVE_MIN_HOURS
            and abs(c - o) >= MOVE_MIN
        ):
            bullets.append(
                f"賽前市場：{home} 勝率 {_pct(o)} → {_pct(c)}（{(c - o) * 100:+.1f}pp）。"
            )
    fav = a["fav"]
    g = _f(row, "t10_g")
    if g is not None and fav:
        thr = _f(row, "t10_threshold") or 0.06
        signal = str(row.get("t10_signal") or "")
        pick = {
            "FAV_ML": f"熱門 ML（{name[fav]}）",
            "DOG_PLUS_1_5": f"冷門 +1.5（{name['away' if fav == 'home' else 'home']}）",
        }.get(signal, f"無訊號（門檻 ±{thr * 100:.0f}pp）")
        text = f"T-10 G 值 {g * 100:+.1f}pp → {pick}"
        if a["t10_result"]:
            text += f" → {RESULT_TEXT[a['t10_result']]}"
        bullets.append(text + "。")
    if fav and a["fav_won"] != "":
        rl = "過" if a["fav_cover"] == "1" else "沒過"
        bullets.append(
            f"熱門 {name[fav]}：ML {'贏' if a['fav_won'] == '1' else '輸'}，−1.5 讓分{rl}"
            "（每一場都記下來，累積後就能檢查 T-10 的 G 值門檻）。"
        )
    elif g is None:
        bullets.append("沒有 T-10 數據：賽前沒有抓到 ML 和 ±1.5 讓分盤。")
    bullets.extend(_starter_bullets(row, home, away))
    bullets.extend(_big_inning_bullets(row, home, away))
    if fh is not None and fa is not None and abs(fh - fa) == 1:
        bullets.append("一分差比賽：勝負接近擲硬幣，單場不必過度解讀。")
    if row.get("tw_home") or row.get("tw_away"):
        at = _local(row.get("tw_at"))
        bullets.append(
            f"運彩賠率 客 {_f(row, 'tw_away') or '—'} · 主 {_f(row, 'tw_home') or '—'}"
            + (f"（{at} 擷取）" if at else "")
        )
    conclusion = {
        "t10_hit": "T-10 命中。單場命中不代表方法有效，看累計紀錄。",
        "t10_miss": "T-10 未命中。看上面的先發和大局數，判斷是方法的問題還是比賽中的意外。",
        "no_signal": "T-10 這場沒有訊號。",
    }[a["verdict"]]
    return {
        "verdict": a["verdict"],
        "label": VERDICTS[a["verdict"]][0],
        "tone": VERDICTS[a["verdict"]][1],
        "bullets": bullets,
        "conclusion": conclusion,
        "assess": a,
    }


def _starter_bullets(row: Mapping[str, Any], home: str, away: str) -> list[str]:
    out = []
    for prefix, team, probable_key in (
        ("sp_a", away, "away_probable"),
        ("sp_h", home, "home_probable"),
    ):
        name = str(row.get(prefix) or "")
        if not name:
            continue
        ip = str(row.get(f"{prefix}_ip") or "")
        runs, er = _i(row, f"{prefix}_r"), _i(row, f"{prefix}_er")
        text = f"{team} 先發 {name}：{ip} 局 {runs if runs is not None else '?'} 失分"
        if er is not None:
            text += f"（責失 {er}）"
        if _i(row, f"{prefix}_k") is not None:
            text += f"，{_i(row, f'{prefix}_k')} K"
        outs = PitcherLine(name, ip).outs
        if (er is not None and er >= 4) or (ip and outs < 12):
            text += " —— 提早崩盤，比賽很早就失控"
        probable = str(row.get(probable_key) or "")
        if probable and probable != name:
            text += f"。注意：賽前預告的是 {probable}，臨時換人"
        out.append(text + "。")
    return out


def _big_inning_bullets(row: Mapping[str, Any], home: str, away: str) -> list[str]:
    out = []
    for batting, team, pitching_prefix in (
        ("innings_a", away, "sp_h"),
        ("innings_h", home, "sp_a"),
    ):
        runs = [None if r in ("x", "") else int(r) for r in str(row.get(batting) or "").split()]
        starter_outs = PitcherLine("", str(row.get(f"{pitching_prefix}_ip") or "")).outs
        for i, r in enumerate(runs, start=1):
            if r is None or r < 3:
                continue
            note = ""
            if starter_outs and (i - 1) * 3 >= starter_outs:
                note = "（對方先發已退場，打的是牛棚）"
            elif starter_outs and starter_outs < i * 3:
                note = "（對方先發在這局退場）"
            out.append(f"{team} 第 {i} 局攻下 {r} 分{note}。")
    return out


def summarize_mlb(rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    done = [(r, assess_mlb(r)) for r in rows if _i(r, "final_h") is not None]
    favs = [(r, a) for r, a in done if a["fav_won"] != ""]
    expected_fav = sum(
        (_f(r, f"close_{a['fav']}") or _f(r, f"open_{a['fav']}") or 0.0) for r, a in favs
    )
    t10 = [(r, a) for r, a in done if a["t10_result"]]
    by_signal: dict[str, dict[str, int]] = {}
    for r, a in t10:
        b = by_signal.setdefault(str(r.get("t10_signal")), {"n": 0, "hits": 0})
        b["n"] += 1
        b["hits"] += int(a["t10_result"] == WIN)
    lls = [a["market_ll"] for _, a in done if a["market_ll"] is not None]
    return {
        "games": len(done),
        "fav_n": len(favs),
        "fav_wins": sum(1 for _, a in favs if a["fav_won"] == "1"),
        "fav_expected": round(expected_fav, 1),
        "fav_covers": sum(1 for _, a in favs if a["fav_cover"] == "1"),
        "t10": by_signal,
        "market_ll": round(sum(lls) / len(lls), 3) if lls else None,
    }


# ========================================================================== helpers
def _stats_fields(st: MatchStats | None) -> dict[str, Any]:
    names = (
        ("xg", "xg"),
        ("shots", "shots"),
        ("sot", "sot"),
        ("corners", "corners"),
        ("yellow", "yellow"),
        ("red", "red"),
    )
    out: dict[str, Any] = {}
    for col, attr in names:
        out[f"{col}_h"] = getattr(st, f"{attr}_home") if st else None
        out[f"{col}_a"] = getattr(st, f"{attr}_away") if st else None
    out["stats_source"] = "football-data.co.uk" if st else ""
    return out


def _newer_tw(tw: Mapping[str, Any], current: Mapping[str, Any] | None, kickoff: datetime) -> bool:
    """Prefer the latest 運彩 capture made before kickoff."""
    if not current:
        return True
    new_at, cur_at = _dt(tw.get("captured_at")), _dt(current.get("captured_at"))
    if new_at is None:
        return False
    if cur_at is None:
        return True
    new_before, cur_before = new_at <= kickoff, cur_at <= kickoff
    if new_before != cur_before:
        return new_before
    return new_at > cur_at if new_before else new_at < cur_at


def _outcome(hg: int | None, ag: int | None) -> str:
    if hg is None or ag is None:
        return ""
    return "H" if hg > ag else "A" if ag > hg else "D"


def _surprise(p: float) -> str:
    if p >= 0.5:
        return "符合預期"
    if p >= 0.3:
        return "正常範圍"
    if p >= 0.15:
        return "偏冷"
    return "大冷門"


def _pct(p: float | None) -> str:
    return "—" if p is None else f"{p * 100:.0f}%"


def _pp(x: float) -> str:
    return f"{x * 100:+.0f}pp"


def _flag(value: bool) -> str:
    return "1" if value else "0"


def _num(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f


def _f(row: Mapping[str, Any], key: str) -> float | None:
    return _num(row.get(key))


def _i(row: Mapping[str, Any], key: str) -> int | None:
    f = _f(row, key)
    return int(f) if f is not None else None


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 3) if values else None


def _dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _local(value: Any) -> str:
    """ISO timestamp -> "10/14 01:12" in Taipei time (the page's clock)."""
    d = _dt(value)
    if d is None or d.tzinfo is None:
        return ""
    return d.astimezone(LOCAL_TZ).strftime("%m/%d %H:%M")


def _hours(a: Any, b: Any) -> float:
    da, db = _dt(a), _dt(b)
    if da is None or db is None:
        return 0.0
    return abs((db - da).total_seconds()) / 3600
