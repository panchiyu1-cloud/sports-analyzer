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

    html = template.render(
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
