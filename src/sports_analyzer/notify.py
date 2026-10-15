"""Phone notifications through ntfy.sh (free: the ntfy app subscribes to a topic).

Only new 運彩 value bets and new 延遲雷達 findings (the market moved after the last
upload; 運彩's price then, if unchanged, would be one) are sent. Both are rare and do not
last (運彩 follows the international market within hours), so they are worth an
interruption. Without a topic
(NTFY_TOPIC) nothing is sent. The topic name is the only key to the channel: it is
never logged or written anywhere.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

import requests

from .config import LOCAL_TZ, MODEL, PAGE_URL
from .moves import parse_time, price_needed, value_threshold
from .render.cards import match_title, side_label

log = logging.getLogger(__name__)

NTFY_URL = "https://ntfy.sh"
SIDE_LABELS = {"home": "主", "draw": "和", "away": "客", "over": "大", "under": "小"}


def value_bet_message(
    row: Mapping[str, Any], move: Mapping[str, Any] | None = None
) -> tuple[str, str]:
    """Title and body for one new value bet (a tw_ledger row). `move` is the match's
    entry from moves.track, used to say when an international 大單 is behind it."""
    ev = float(row["ev"])
    kickoff = parse_time(row.get("kickoff_utc"))
    when = kickoff.astimezone(LOCAL_TZ).strftime("%m/%d %H:%M") if kickoff else ""
    lines = [
        f"{row.get('competition', '')} {when} 開賽".strip(),
        f"{row['home']} vs {row['away']}"
        if row.get("sport") == "football"
        else f"{row['away']} @ {row['home']}",
        f"{row['market']}：{row['selection']} @ {row['tw_price']}（公允 {row['fair_odds']}）",
    ]
    for side in (move or {}).get("sides", []):
        if side.get("big"):
            label = SIDE_LABELS.get(side["side"], side["side"])
            text = f"國際盤大單：{label} {side['first'] * 100:.0f}% → {side['last'] * 100:.0f}%"
            if side.get("follow") is not None:
                text += f"，運彩跟了 {side['follow'] * 100:.0f}%"
            lines.append(text)
    lines.append("下注前先確認運彩賠率沒有變低。")
    return f"運彩價值注 EV {ev * 100:+.1f}%", "\n".join(lines)


def radar_message(event: Mapping[str, Any]) -> tuple[str, str]:
    """Title and body for one new 延遲雷達 finding (a radar event of moves.radar_events,
    with the competition and 運彩's names): the market moved after the last upload, and
    運彩's price then, if unchanged, is a value bet."""
    sport = str(event.get("sport"))
    home = str(event.get("home_zh") or event.get("home", ""))
    away = str(event.get("away_zh") or event.get("away", ""))
    prob, price = float(event["expected"]), float(event["tw_price"])
    threshold = value_threshold(sport, MODEL)
    ev = prob * price - 1
    kickoff = parse_time(event.get("kickoff_utc"))
    uploaded = parse_time(event.get("from_at"))
    label = side_label(
        sport, str(event["market"]), str(event["side"]), str(event["line"]), home, away
    )
    move = event.get("intl_move")
    then = (
        f"國際 {(prob - float(move)) * 100:.0f}% → {prob * 100:.0f}%（你上次上傳後 {float(move) * 100:+.1f}pp）"
        if move not in (None, "")
        else f"國際現在 {prob * 100:.0f}%"
    )
    lines = [
        f"{event.get('competition') or ''} "
        f"{kickoff.astimezone(LOCAL_TZ).strftime('%m/%d %H:%M') if kickoff else ''} 開賽".strip(),
        match_title(sport, home, away),
        f"{label}：{then}",
        f"運彩上次 {price:g}"
        f"（{uploaded.astimezone(LOCAL_TZ).strftime('%H:%M') if uploaded else ''}）"
        f"若還沒變，EV {ev * 100:+.1f}%",
        f"打開運彩：賠率 ≥ {price_needed(prob, threshold):.2f} 就可下。"
        "按捷徑上傳一次，系統會用最新價格確認並記帳。",
    ]
    return f"延遲雷達 EV {ev * 100:+.1f}%", "\n".join(lines)


def send(
    topic: str | None,
    title: str,
    message: str,
    session: Any = None,
    tags: tuple[str, ...] = ("moneybag",),
) -> bool:
    """Publish one notification (JSON API, so Chinese titles survive). False if not sent."""
    if not topic:
        return False
    payload = {
        "topic": topic,
        "title": title,
        "message": message,
        "tags": list(tags),
        "priority": 4,
        "click": PAGE_URL,
    }
    try:
        resp = (session or requests).post(NTFY_URL, json=payload, timeout=15)
    except requests.RequestException as exc:
        log.warning("notification not sent (%s)", type(exc).__name__)
        return False
    if not getattr(resp, "ok", False):
        log.warning("notification not sent (HTTP %s)", getattr(resp, "status_code", "?"))
        return False
    return True


def notify_value_bets(
    topic: str | None,
    rows: Iterable[Mapping[str, Any]],
    moves_by_match: Mapping[tuple[str, str], Mapping[str, Any]] | None = None,
    session: Any = None,
) -> int:
    """One notification per new value bet; returns how many were sent."""
    sent = 0
    for row in rows:
        move = (moves_by_match or {}).get((str(row.get("sport")), str(row.get("match_id"))))
        title, body = value_bet_message(row, move)
        sent += send(topic, title, body, session=session)
    if sent:
        log.info("notifications sent: %d", sent)
    return sent


def notify_radar(
    topic: str | None,
    events: Iterable[Mapping[str, Any]],
    now: datetime,
    session: Any = None,
) -> int:
    """One notification per new radar finding that can still be acted on: the match has
    not started and no newer 運彩 price has answered the question (`current`)."""
    sent = 0
    for e in events:
        kickoff = parse_time(e.get("kickoff_utc"))
        if e.get("type") != "radar" or not e.get("current") or kickoff is None or kickoff <= now:
            continue
        title, body = radar_message(e)
        sent += send(topic, title, body, session=session, tags=("satellite",))
    if sent:
        log.info("radar notifications sent: %d", sent)
    return sent
