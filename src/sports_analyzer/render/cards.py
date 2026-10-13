"""Render the snapshot dicts into docs/index.html."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from ..config import LOCAL_TZ, MODEL, ModelConfig

TEMPLATE_DIR = Path(__file__).parent / "templates"


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
) -> Path:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(["html", "j2"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    template = env.get_template("index.html.j2")

    competitions: list[dict[str, Any]] = []
    signals: list[dict[str, Any]] = []
    credits = None
    if football:
        credits = football.get("credits_remaining")
        for comp in football.get("competitions", []):
            competitions.append(comp)
            for s in comp.get("signals", []):
                signals.append(s)
    signals.sort(key=lambda s: s.get("kickoff_utc", ""))

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
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    return out_path
