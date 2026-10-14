"""Static configuration and environment-backed settings.

Everything that is a *choice* (which competitions, which thresholds, where files
go) lives here so that the rest of the code reads like the plan.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

# --------------------------------------------------------------------------- paths
ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"
SNAPSHOT_DIR = DATA_DIR / "snapshots"
HISTORY_DIR = DATA_DIR / "history"
LEDGER_PATH = DATA_DIR / "ledger.csv"
TW_LEDGER_PATH = DATA_DIR / "tw_ledger.csv"
TW_DIR = DATA_DIR / "tw"
REVIEW_DIR = DATA_DIR / "reviews"
FOOTBALL_REVIEWS_PATH = REVIEW_DIR / "football.csv"
MLB_REVIEWS_PATH = REVIEW_DIR / "mlb.csv"
ODDS_BUDGET_PATH = DATA_DIR / "odds_budget.json"
# Line-move tape (moves.py): every international and 運彩 price seen, per month.
MOVES_DIR = DATA_DIR / "moves"
# Written by scripts/validate_implied.py; per-market corrections for the implied model.
IMPLIED_CALIBRATION_PATH = DATA_DIR / "implied_calibration.json"
DOCS_DIR = ROOT / "docs"
# Where GitHub Pages serves docs/ (notifications open it).
PAGE_URL = "https://panchiyu1-cloud.github.io/sports-analyzer/"

LOCAL_TZ = ZoneInfo("Asia/Taipei")


# --------------------------------------------------------------------- competitions
@dataclass(frozen=True)
class Competition:
    """One football competition and its identifiers across the three data sources."""

    code: str  # football-data.org competition code
    name: str  # display name (Traditional Chinese)
    odds_key: str  # The Odds API sport key
    fdcouk_code: str | None  # football-data.co.uk CSV code (None = not available there)
    is_cup: bool = False


FOOTBALL_COMPETITIONS: tuple[Competition, ...] = (
    Competition("PL", "英超", "soccer_epl", "E0"),
    Competition("PD", "西甲", "soccer_spain_la_liga", "SP1"),
    Competition("BL1", "德甲", "soccer_germany_bundesliga", "D1"),
    Competition("SA", "義甲", "soccer_italy_serie_a", "I1"),
    Competition("FL1", "法甲", "soccer_france_ligue_one", "F1"),
    Competition("CL", "歐冠", "soccer_uefa_champs_league", None, is_cup=True),
)

COMPETITION_BY_CODE = {c.code: c for c in FOOTBALL_COMPETITIONS}

MLB_ODDS_KEY = "baseball_mlb"


# ------------------------------------------------------------------- model knobs
@dataclass(frozen=True)
class ModelConfig:
    """Tunable numbers. Defaults are the usual literature values; change with a backtest."""

    # Dixon-Coles time decay per day. 0.0065 ~ half-life of ~107 days (about a season third).
    dc_xi: float = 0.0065
    dc_max_goals: int = 10
    # A team needs at least this many fitted matches before its card is marked "資料充足".
    min_team_matches: int = 6
    # Edge (model prob - market prob) needed before a signal is emitted, in probability.
    # Raised from 0.04 after the first backtest (2026-10-13): the raw Dixon-Coles model
    # lost ~10% ROI to closing lines at every edge level, so signals are observational
    # until Phase 2 (market-anchored shrinkage) passes a backtest.
    edge_threshold: float = 0.10
    # Confidence tiers by edge size.
    edge_mid: float = 0.15
    edge_high: float = 0.20
    # Flip to True only when a walk-forward backtest shows positive ROI for the signals.
    signals_validated: bool = False
    # 運彩 price must beat the fair price by this much EV (fair_prob x price - 1) to count as a value bet.
    value_threshold: float = 0.02
    # MLB (T-10 價值版, 2026-10-14): a 3% cushion. The fair price is a consensus of
    # US-facing books whose de-vig carries ~1pp of error, which is ~2% EV at even odds.
    mlb_value_threshold: float = 0.03
    # A 運彩 price is judged only against a fair price bought within this many minutes
    # of it: an older consensus misses the news 運彩 may already have priced in.
    max_price_gap_minutes: int = 180
    # When a 運彩 upload arrives, games starting within this many hours get freshly
    # bought international prices (if the last ones are older than the minimum age).
    pregame_window_hours: float = 3.0
    pregame_min_age_minutes: int = 20
    # 大單: a side's international probability moved this much since we first saw the
    # match. 2019-2027, 22 leagues: such moves were right (steamed sides won 55% after
    # opening at 43%), and backing them at a price that had not moved returned +7%;
    # moves of 5-8pp did not beat 運彩's margin.
    big_move_threshold: float = 0.08
    # 運彩-only move: between two uploads 運彩's own (de-vigged) price moved this much
    # more than the international market's over the same times.
    tw_divergence_threshold: float = 0.02
    # Markets priced through the market-implied goal model carry its error too
    # (validated: ~0.7pp on Asian handicaps, <=0.5pp mean bias after calibration,
    # 1-3pp in the worst probability band), so they need a larger margin of safety.
    derived_value_threshold: float = 0.04
    ht_value_threshold: float = 0.05
    # Do not recommend very long shots even with edge; variance is too high to judge.
    min_model_prob: float = 0.25
    # 模型分歧 only where the model can know the teams (2026-10-14 review): the pooled
    # Champions League model cannot compare leagues (both CL signals lost; it gave Man City
    # 82% vs PSG when the market said 45%), and promoted teams have only this season's
    # handful of top-flight matches.
    cup_signals: bool = False
    min_signal_matches: int = 20
    # T-10 (baseball) G-value thresholds.
    g_threshold: float = 0.06
    # De-vig method: "power" (default) or "multiplicative".
    devig_method: str = "power"


MODEL = ModelConfig()


# ----------------------------------------------------------------------- settings
@dataclass(frozen=True)
class Settings:
    """Runtime settings pulled from the environment (and `.env` locally)."""

    football_data_token: str | None
    odds_api_key: str | None
    odds_regions: str = "eu"
    fixture_window_days: int = 2
    offline: bool = False
    # ntfy.sh topic for phone notifications of new value bets (optional). The topic
    # name is the only key to the channel, so it is kept as a secret like the tokens.
    ntfy_topic: str | None = None

    @classmethod
    def from_env(cls, offline: bool = False) -> Settings:
        _load_dotenv_if_present()
        settings = cls(
            football_data_token=os.environ.get("FOOTBALL_DATA_TOKEN") or None,
            odds_api_key=os.environ.get("ODDS_API_KEY") or None,
            odds_regions=os.environ.get("ODDS_REGIONS", "eu"),
            fixture_window_days=int(os.environ.get("FIXTURE_WINDOW_DAYS", "2")),
            offline=offline,
            ntfy_topic=os.environ.get("NTFY_TOPIC") or None,
        )
        if not offline:
            settings.require()
        return settings

    def require(self) -> None:
        """Fail fast with a clear message rather than halfway through a run."""
        missing = [
            name
            for name, value in (
                ("FOOTBALL_DATA_TOKEN", self.football_data_token),
                ("ODDS_API_KEY", self.odds_api_key),
            )
            if not value
        ]
        if missing:
            raise SystemExit(
                "Missing required environment variables: "
                + ", ".join(missing)
                + ". Copy .env.example to .env (local) or add GitHub Secrets (Actions)."
            )


def _load_dotenv_if_present() -> None:
    """Load a local `.env` if python-dotenv is installed and the file exists.

    On GitHub Actions the secrets are already in the environment, so this is a no-op.
    """
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover - optional dependency
        return
    load_dotenv(env_path, override=False)
