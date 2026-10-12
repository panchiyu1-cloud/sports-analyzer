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
DOCS_DIR = ROOT / "docs"

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
    edge_threshold: float = 0.04
    # Confidence tiers by edge size.
    edge_mid: float = 0.07
    edge_high: float = 0.10
    # Do not recommend very long shots even with edge; variance is too high to judge.
    min_model_prob: float = 0.25
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

    @classmethod
    def from_env(cls, offline: bool = False) -> Settings:
        _load_dotenv_if_present()
        settings = cls(
            football_data_token=os.environ.get("FOOTBALL_DATA_TOKEN") or None,
            odds_api_key=os.environ.get("ODDS_API_KEY") or None,
            odds_regions=os.environ.get("ODDS_REGIONS", "eu"),
            fixture_window_days=int(os.environ.get("FIXTURE_WINDOW_DAYS", "2")),
            offline=offline,
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
