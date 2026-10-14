"""Taiwan Sports Lottery (台灣運彩) pre-match odds, from the official site's own JSON feed.

The feed (`blob3rd.sportslottery.com.tw/apidata/Pre/<sport id>-Games.zh.json`)
is geo-blocked outside Taiwan, so the owner's phone fetches it on a schedule
and commits the raw file under `data/tw/<sport>/<stamp>.json`. This module only
parses those files; it never fetches.

Observed structure (2026-10, baseball):

    game:      {"id", "no", "an": away zh, "hn": home zh, "kt": kickoff "+08:00", "si", "sn", "ms": [...]}
    market:    {"id", "name": "不讓分" | "讓分" | "大小" | ..., "ti": type id, "mv": line?, "cs": [...], "ss": updated}
    selection: {"id", "name", "v": "A" | "H" | ..., "hv": handicap?, "pd": denominator, "pu": numerator}

Decimal price = 1 + pu / pd  (pd=2, pu=1 -> 1.50; pd=100, pu=107 -> 2.07).
Anything that does not match these shapes is skipped and counted, never fatal.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..teams import canonical

log = logging.getLogger(__name__)

SPORT_IDS = {"football": "34740.1", "baseball": "34731.1"}


@dataclass(frozen=True)
class TwSelection:
    name: str
    side: str  # "home" | "away" | "draw" | "over" | "under" | "other"
    price: float
    line: float | None = None


@dataclass(frozen=True)
class TwMarket:
    name: str
    kind: str  # "h2h" | "spread" | "totals" | "other"
    type_id: str
    selections: tuple[TwSelection, ...]
    updated: datetime | None
    line: float | None = None

    def price(self, side: str) -> float | None:
        for s in self.selections:
            if s.side == side:
                return s.price
        return None


@dataclass(frozen=True)
class TwGame:
    id: str
    no: str
    home_zh: str
    away_zh: str
    kickoff: datetime  # aware, UTC
    sport_id: str
    markets: tuple[TwMarket, ...]
    league_zh: str | None = None

    @property
    def home_key(self) -> str:
        return tw_canonical(self.home_zh)

    @property
    def away_key(self) -> str:
        return tw_canonical(self.away_zh)

    def market(self, kind: str, line: float | None = None) -> TwMarket | None:
        """A market of a kind; with `line`, the one quoted at that line (totals come in several)."""
        for m in self.markets:
            if m.kind != kind or not m.selections:
                continue
            if line is None or (m.line is not None and abs(m.line - line) < 1e-9):
                return m
        return None


@dataclass
class ParseReport:
    games: int = 0
    skipped_games: int = 0
    unknown_markets: dict[str, int] = field(default_factory=dict)
    unknown_teams: set[str] = field(default_factory=set)


# ----------------------------------------------------------------------- parsing
def parse_text(text: str) -> Any:
    """Parse the uploaded file leniently.

    The iPhone Shortcut cannot always send the feed byte-for-byte: Shortcuts turns
    a JSON array into a list and may re-serialise it as one object per line
    (NDJSON) or as objects without the surrounding brackets. Accept all of those.
    """
    text = text.lstrip("﻿").strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    decoder = json.JSONDecoder()
    items: list[Any] = []
    pos = 0
    while pos < len(text):
        while pos < len(text) and text[pos] in " \t\r\n,[]":
            pos += 1
        if pos >= len(text):
            break
        try:
            obj, end = decoder.raw_decode(text, pos)
        except ValueError:
            # Skip to the next line and try again rather than giving up on the file.
            nl = text.find("\n", pos)
            if nl == -1:
                break
            pos = nl + 1
            continue
        items.append(obj)
        pos = end
    return items


def parse_games(payload: Any, report: ParseReport | None = None) -> list[TwGame]:
    report = report if report is not None else ParseReport()
    games: list[TwGame] = []
    if isinstance(payload, dict):
        payload = [payload]
    if not isinstance(payload, list):
        log.warning("tw feed: expected a list, got %s", type(payload).__name__)
        return games
    for raw in payload:
        try:
            markets = tuple(
                _parse_market(m, report) for m in raw.get("ms", []) if isinstance(m, dict)
            )
            game = TwGame(
                id=str(raw["id"]),
                no=str(raw.get("no") or ""),
                home_zh=str(raw["hn"]).strip(),
                away_zh=str(raw["an"]).strip(),
                kickoff=_parse_dt(str(raw["kt"])),
                sport_id=str(raw.get("si") or ""),
                markets=tuple(m for m in markets if m is not None),
                league_zh=_league(raw),
            )
            games.append(game)
            report.games += 1
            for zh in (game.home_zh, game.away_zh):
                if zh not in TW_ALIASES:
                    report.unknown_teams.add(zh)
        except (KeyError, TypeError, ValueError) as exc:
            report.skipped_games += 1
            log.debug("tw feed: skipping game: %s", exc)
    return games


def _parse_market(raw: dict[str, Any], report: ParseReport) -> TwMarket | None:
    name = str(raw.get("name") or "").strip()
    kind = _market_kind(name)
    line = _float(raw.get("mv"))
    selections: list[TwSelection] = []
    for c in raw.get("cs", []):
        if not isinstance(c, dict):
            continue
        price = _price(c.get("pd"), c.get("pu"))
        if price is None:
            continue
        sel_line = _float(c.get("hv"))
        selections.append(
            TwSelection(
                name=str(c.get("name") or "").strip(),
                side=_side(kind, str(c.get("v") or ""), str(c.get("name") or "")),
                price=price,
                line=sel_line if sel_line is not None else line,
            )
        )
    # Football "讓分 1:0" is a three-way European handicap (with a draw); it is not the
    # two-way Asian line the consensus quotes, so it is not comparable.
    if kind == "spread" and any(s.side == "draw" for s in selections):
        kind = "other"
    if kind == "other":
        report.unknown_markets[name] = report.unknown_markets.get(name, 0) + 1
    if not selections:
        return None
    if line is None:
        lines = [s.line for s in selections if s.line is not None]
        line = lines[0] if lines else None
    return TwMarket(
        name=name,
        kind=kind,
        type_id=str(raw.get("ti") or ""),
        selections=tuple(selections),
        updated=_parse_dt(str(raw["ss"])) if raw.get("ss") else None,
        line=line,
    )


_TOTALS_RE = re.compile(r"^(\[總分\])?大小( [\d.]+)?$")


def _market_kind(name: str) -> str:
    """Classify a 運彩 market by its Chinese name (observed 2026-10).

    full match:  不讓分 (h2h, 3-way for football) · 讓分 -1.5 (2-way run line) · [總分]大小 2.5
    not used:    [上半場]…, [第1局]…, [1-5局]…, [角球數]…, 勝分差, 單雙, team totals (坦帕灣光芒 大小 3.5)
    """
    if any(tag in name for tag in ("角球", "半場", "上半", "下半", "局", "節", "單雙", "勝分差")):
        return "other"
    if name == "不讓分" or name in ("獨贏", "勝負"):
        return "h2h"
    if name.startswith("讓分"):
        return "spread"
    if _TOTALS_RE.match(name) or name == "總進球數":
        return "totals"
    return "other"


def _side(kind: str, v: str, name: str) -> str:
    v = v.upper()
    if kind in ("h2h", "spread"):
        if v == "H":
            return "home"
        if v == "A":
            return "away"
        if v in ("D", "X") or "和" in name:
            return "draw"
    if kind == "totals":
        # The feed reuses A/H for over/under, so only the name is trustworthy here.
        if name.startswith("大"):
            return "over"
        if name.startswith("小"):
            return "under"
    return "other"


def _price(pd: Any, pu: Any) -> float | None:
    try:
        d, u = float(pd), float(pu)
    except (TypeError, ValueError):
        return None
    if d <= 0 or u < 0:
        return None
    return round(1.0 + u / d, 3)


def _float(value: Any) -> float | None:
    if value in (None, "", "null"):
        return None
    try:
        return float(str(value).replace("+", ""))
    except ValueError:
        return None


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _league(raw: dict[str, Any]) -> str | None:
    # "tn" is the tournament (歐洲冠軍聯賽); "cn" is the country (國際俱樂部) — prefer the tournament.
    for key in ("tn", "ln", "lg", "cn"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


# Leagues this project covers, by keyword in the feed's tournament name.
RELEVANT_LEAGUE_KEYWORDS = (
    "英格蘭超級",
    "西班牙甲級",
    "德國甲級",
    "義大利甲級",
    "法國甲級",
    "歐洲冠軍",
    "美國職棒",
)


def is_relevant_league(league_zh: str | None) -> bool:
    return bool(league_zh) and any(k in league_zh for k in RELEVANT_LEAGUE_KEYWORDS)


# -------------------------------------------------------------------- files
def list_uploads(tw_dir: Path, sport: str) -> list[Path]:
    """Every uploaded file under data/tw/<sport>/, whatever the phone named it, oldest first.

    Ordering uses the git commit time of each file (the checkout's mtimes are all
    the same on a fresh runner); files git does not know yet fall back to mtime.
    """
    folder = tw_dir / sport
    if not folder.exists():
        return []
    files = [p for p in folder.rglob("*") if p.is_file() and p.name != ".gitkeep"]
    return sorted(
        files, key=lambda p: (captured_at(p) or datetime.fromtimestamp(0, tz=UTC), str(p))
    )


# What `normalize_uploads` names files: Taipei time of the upload, e.g. 2026-10-14-0800.json.
CANONICAL_UPLOAD = re.compile(r"^\d{4}-\d{2}-\d{2}-\d{4}(-\d+)?\.json$")


def captured_at(path: Path) -> datetime | None:
    """When the phone uploaded this file.

    A file already renamed by `normalize_uploads` carries that time in its name (git
    would report the later rename commit instead). Otherwise: the git commit time,
    then a stamp in the name, then the file's mtime.
    """
    if CANONICAL_UPLOAD.match(path.name):
        stamp = stamp_from_name(path)
        if stamp:
            return stamp.astimezone(UTC)
    return _arrival_time(path)


def _arrival_time(path: Path) -> datetime | None:
    import subprocess

    try:
        out = subprocess.run(
            ["git", "log", "-1", "--format=%cI", "--", path.name],
            cwd=path.parent,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        ).stdout.strip()
        if out:
            return datetime.fromisoformat(out).astimezone(UTC)
    except (OSError, ValueError):
        pass
    stamp = stamp_from_name(path)
    if stamp:
        return stamp.astimezone(UTC)
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
    except OSError:
        return None


def normalize_uploads(tw_dir: Path) -> list[tuple[Path, Path]]:
    """Rename phone uploads to the Taipei time they arrived: data/tw/<sport>/YYYY-MM-DD-HHMM.json.

    The phone builds the name from its own date format, which has produced odd names
    (a wrong day, folders instead of a file). Renaming right after each upload keeps
    the folder readable and, more importantly, frees the phone's name: GitHub refuses
    to overwrite an existing path, so a repeated name would silently block an upload.
    """
    from ..config import LOCAL_TZ

    moves: list[tuple[Path, Path]] = []
    for sport_dir in (tw_dir / "football", tw_dir / "baseball"):
        if not sport_dir.exists():
            continue
        files = [p for p in sport_dir.rglob("*") if p.is_file() and p.name != ".gitkeep"]
        for path in sorted(files):
            if path.parent == sport_dir and CANONICAL_UPLOAD.match(path.name):
                continue
            when = _arrival_time(path)
            if when is None:
                continue
            base = when.astimezone(LOCAL_TZ).strftime("%Y-%m-%d-%H%M")
            target, n = sport_dir / f"{base}.json", 1
            if target.exists() and target.read_bytes() == path.read_bytes():
                path.unlink()  # the same upload already renamed by an earlier run
                moves.append((path, target))
                continue
            while target.exists():
                target, n = sport_dir / f"{base}-{n}.json", n + 1
            path.rename(target)
            moves.append((path, target))
        # Folders the odd names created (e.g. 2026/10/) are empty now.
        for folder in sorted((d for d in sport_dir.rglob("*") if d.is_dir()), reverse=True):
            if not any(folder.iterdir()):
                folder.rmdir()
    return moves


def load_latest(tw_dir: Path, sport: str) -> tuple[list[TwGame], Path | None, ParseReport]:
    """Parse the newest upload for a sport. Returns (games, path, report)."""
    files = list_uploads(tw_dir, sport)
    report = ParseReport()
    if not files:
        return [], None, report
    path = files[-1]
    try:
        payload = parse_text(path.read_text(encoding="utf-8-sig"))
    except OSError as exc:
        log.warning("tw feed: cannot read %s: %s", path.name, exc)
        return [], path, report
    games = parse_games(payload, report)
    # Keep the report readable: team-specific props (e.g. "兵工廠 大小 2.5") appear once each.
    report.unknown_markets = dict(
        sorted(
            ((k, v) for k, v in report.unknown_markets.items() if v >= 5), key=lambda kv: -kv[1]
        )[:20]
    )
    # Only names from leagues we cover are worth adding to the alias table.
    report.unknown_teams = {
        zh
        for g in games
        if is_relevant_league(g.league_zh)
        for zh in (g.home_zh, g.away_zh)
        if zh not in TW_ALIASES and not re.search(r"[A-Za-z]", tw_canonical(zh))
    }
    return games, path, report


def stamp_from_name(path: Path) -> datetime | None:
    """`2026-10-14-0800.json` -> aware datetime in Taipei time."""
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})-(\d{2})(\d{2})", path.stem)
    if not m:
        return None
    y, mo, d, h, mi = (int(x) for x in m.groups())
    from ..config import LOCAL_TZ

    return datetime(y, mo, d, h, mi, tzinfo=LOCAL_TZ)


# ----------------------------------------------------------- Chinese names
_ZH_SUFFIXES = ("足球會", "足球俱樂部", "俱樂部", "體育會", "競技會", "隊")


def tw_canonical(zh: str) -> str:
    """Canonical key for a 運彩 Chinese team name; unknown names fall back to the raw string.

    Tries, in order: the exact name; without a parenthesised note; without a Latin
    prefix ("PSV恩荷芬" -> 恩荷芬); without a club suffix ("維京足球會" -> 維京);
    with "." dropped ("薩巴赫.馬薩齊爾" -> 薩巴赫馬薩齊爾, then its first part).
    """
    zh = zh.strip()
    candidates = [zh, re.sub(r"[（(].*?[)）]", "", zh).strip()]
    latin_stripped = re.sub(r"^[A-Za-z0-9 .\-]+", "", zh).strip()
    if latin_stripped and latin_stripped != zh:
        candidates.append(latin_stripped)
    for base in list(candidates):
        for suffix in _ZH_SUFFIXES:
            if base.endswith(suffix) and len(base) > len(suffix):
                candidates.append(base[: -len(suffix)])
    if "." in zh or "·" in zh:
        parts = re.split(r"[.·]", zh)
        candidates.append("".join(parts))
        candidates.append(parts[0])
    for cand in candidates:
        if cand in TW_ALIASES:
            return TW_ALIASES[cand]
    return canonical(zh) if re.fullmatch(r"[A-Za-z0-9 .\-]+", zh) else zh


def _mlb(*names: str) -> dict[str, str]:
    """Helper: several Chinese spellings -> canonical key of the English full name."""
    english = names[0]
    key = canonical(english)
    return {zh: key for zh in names[1:]}


TW_ALIASES: dict[str, str] = {}
# ---- MLB (canonical = normalised English full name, as the MLB Stats API and The Odds API use)
for _m in (
    _mlb("New York Yankees", "紐約洋基", "洋基"),
    _mlb("Boston Red Sox", "波士頓紅襪", "紅襪"),
    _mlb("Toronto Blue Jays", "多倫多藍鳥", "藍鳥"),
    _mlb("Tampa Bay Rays", "坦帕灣光芒", "光芒"),
    _mlb("Baltimore Orioles", "巴爾的摩金鶯", "金鶯"),
    _mlb("Cleveland Guardians", "克里夫蘭守護者", "守護者"),
    _mlb("Minnesota Twins", "明尼蘇達雙城", "雙城"),
    _mlb("Detroit Tigers", "底特律老虎", "老虎"),
    _mlb("Chicago White Sox", "芝加哥白襪", "白襪"),
    _mlb("Kansas City Royals", "堪薩斯皇家", "堪薩斯市皇家", "皇家"),
    _mlb("Houston Astros", "休士頓太空人", "太空人"),
    _mlb("Seattle Mariners", "西雅圖水手", "水手"),
    _mlb("Texas Rangers", "德州遊騎兵", "遊騎兵"),
    _mlb("Los Angeles Angels", "洛杉磯天使", "天使"),
    _mlb("Athletics", "奧克蘭運動家", "沙加緬度運動家", "運動家", "奧克蘭運動家隊"),
    _mlb("Atlanta Braves", "亞特蘭大勇士", "勇士"),
    _mlb("New York Mets", "紐約大都會", "大都會"),
    _mlb("Philadelphia Phillies", "費城費城人", "費城人"),
    _mlb("Miami Marlins", "邁阿密馬林魚", "馬林魚"),
    _mlb("Washington Nationals", "華盛頓國民", "國民"),
    _mlb("Milwaukee Brewers", "密爾瓦基釀酒人", "釀酒人"),
    _mlb("Chicago Cubs", "芝加哥小熊", "小熊"),
    _mlb("St. Louis Cardinals", "聖路易紅雀", "聖路易斯紅雀", "紅雀"),
    _mlb("Cincinnati Reds", "辛辛那提紅人", "辛辛那堤紅人", "紅人"),
    _mlb("Pittsburgh Pirates", "匹茲堡海盜", "海盜"),
    _mlb("Los Angeles Dodgers", "洛杉磯道奇", "道奇"),
    _mlb("San Diego Padres", "聖地牙哥教士", "教士"),
    _mlb("San Francisco Giants", "舊金山巨人", "巨人"),
    _mlb("Arizona Diamondbacks", "亞利桑那響尾蛇", "亞歷桑那響尾蛇", "響尾蛇"),
    _mlb("Colorado Rockies", "科羅拉多洛磯", "科羅拉多落磯", "洛磯", "落磯"),
):
    TW_ALIASES.update(_m)

# ---- Football: 運彩 Chinese -> canonical key used in teams.py
TW_ALIASES.update(
    {
        # 英超
        "兵工廠": "arsenal",
        "阿森納": "arsenal",
        "曼聯": "manchester united",
        "曼徹斯特聯": "manchester united",
        "曼城": "manchester city",
        "曼徹斯特城": "manchester city",
        "利物浦": "liverpool",
        "切爾西": "chelsea",
        "熱刺": "tottenham",
        "托特納姆熱刺": "tottenham",
        "紐卡索": "newcastle",
        "紐卡斯爾": "newcastle",
        "紐卡素": "newcastle",
        "阿斯頓維拉": "aston villa",
        "維拉": "aston villa",
        "西漢姆": "west ham",
        "西漢姆聯": "west ham",
        "布萊頓": "brighton",
        "布賴頓": "brighton",
        "狼隊": "wolves",
        "伍爾弗漢普頓": "wolves",
        "富勒姆": "fulham",
        "富咸": "fulham",
        "水晶宮": "crystal palace",
        "布倫特福德": "brentford",
        "布蘭特福德": "brentford",
        "諾丁漢森林": "nottingham forest",
        "諾丁漢": "nottingham forest",
        "伯恩茅斯": "bournemouth",
        "艾佛頓": "everton",
        "埃弗頓": "everton",
        "里茲聯": "leeds",
        "里茲": "leeds",
        "利茲聯": "leeds",
        "伯恩利": "burnley",
        "桑德蘭": "sunderland",
        "新特蘭": "sunderland",
        "萊斯特城": "leicester",
        "萊斯特": "leicester",
        "南安普敦": "southampton",
        "伊普斯維奇": "ipswich",
        "考文垂": "coventry",
        "赫爾城": "hull",
        # 西甲
        "皇家馬德里": "real madrid",
        "皇馬": "real madrid",
        "巴塞隆納": "barcelona",
        "巴薩": "barcelona",
        "馬德里競技": "atletico madrid",
        "馬競": "atletico madrid",
        "畢爾包競技": "athletic bilbao",
        "畢爾包": "athletic bilbao",
        "畢爾巴鄂競技": "athletic bilbao",
        "皇家社會": "real sociedad",
        "維拉利爾": "villarreal",
        "比利亞雷亞爾": "villarreal",
        "皇家貝蒂斯": "real betis",
        "貝蒂斯": "real betis",
        "塞維亞": "sevilla",
        "塞維利亞": "sevilla",
        "瓦倫西亞": "valencia",
        "巴倫西亞": "valencia",
        "赫塔菲": "getafe",
        "赫塔費": "getafe",
        "奧薩蘇納": "osasuna",
        "塞爾塔": "celta vigo",
        "維戈塞爾塔": "celta vigo",
        "馬約卡": "mallorca",
        "馬略卡": "mallorca",
        "阿拉維斯": "alaves",
        "巴列卡諾": "rayo vallecano",
        "雷歐瓦列卡諾": "rayo vallecano",
        "西班牙人": "espanyol",
        "赫羅納": "girona",
        "吉羅納": "girona",
        "萊萬特": "levante",
        "萊凡特": "levante",
        "埃爾切": "elche",
        "奧維耶多": "oviedo",
        "皇家奧維耶多": "oviedo",
        "拉斯帕爾馬斯": "las palmas",
        "萊加內斯": "leganes",
        "瓦拉多利德": "valladolid",
        "拉科魯尼亞": "la coruna",
        "馬拉加": "malaga",
        # 德甲
        "拜仁慕尼黑": "bayern munich",
        "拜仁": "bayern munich",
        "多特蒙德": "dortmund",
        "勒沃庫森": "leverkusen",
        "萊比錫": "rb leipzig",
        "RB萊比錫": "rb leipzig",
        "法蘭克福": "eintracht frankfurt",
        "斯圖加特": "stuttgart",
        "弗萊堡": "freiburg",
        "佛萊堡": "freiburg",
        "霍芬海姆": "hoffenheim",
        "美因茲": "mainz",
        "美因茨": "mainz",
        "沃夫斯堡": "wolfsburg",
        "沃爾夫斯堡": "wolfsburg",
        "門興格拉德巴赫": "monchengladbach",
        "門興": "monchengladbach",
        "奧格斯堡": "augsburg",
        "不來梅": "werder bremen",
        "雲達不來梅": "werder bremen",
        "海登海姆": "heidenheim",
        "聖保利": "st pauli",
        "柏林聯合": "union berlin",
        "柏林聯": "union berlin",
        "漢堡": "hamburg",
        "科隆": "koln",
        "波鴻": "bochum",
        "基爾": "holstein kiel",
        "荷爾斯泰因基爾": "holstein kiel",
        "帕德博恩": "paderborn",
        "埃爾弗斯貝格": "elversberg",
        "沙爾克": "schalke",
        # 義甲
        "國際米蘭": "inter",
        "國米": "inter",
        "AC米蘭": "ac milan",
        "米蘭": "ac milan",
        "尤文圖斯": "juventus",
        "尤文": "juventus",
        "拿坡里": "napoli",
        "那不勒斯": "napoli",
        "羅馬": "roma",
        "拉齊奧": "lazio",
        "亞特蘭大": "atalanta",
        "佛羅倫斯": "fiorentina",
        "佛羅倫薩": "fiorentina",
        "波隆那": "bologna",
        "博洛尼亞": "bologna",
        "杜林": "torino",
        "都靈": "torino",
        "烏迪內斯": "udinese",
        "熱那亞": "genoa",
        "維羅納": "verona",
        "卡利亞里": "cagliari",
        "萊切": "lecce",
        "帕爾馬": "parma",
        "科摩": "como",
        "科莫": "como",
        "薩索洛": "sassuolo",
        "比薩": "pisa",
        "克雷莫納": "cremonese",
        "恩波利": "empoli",
        "威尼斯": "venezia",
        "蒙扎": "monza",
        # 法甲
        "巴黎聖日耳曼": "psg",
        "巴黎": "psg",
        "馬賽": "marseille",
        "里昂": "lyon",
        "摩納哥": "monaco",
        "里爾": "lille",
        "尼斯": "nice",
        "朗斯": "lens",
        "雷恩": "rennes",
        "史特拉斯堡": "strasbourg",
        "斯特拉斯堡": "strasbourg",
        "南特": "nantes",
        "布雷斯特": "brest",
        "土魯斯": "toulouse",
        "圖盧茲": "toulouse",
        "蒙彼利埃": "montpellier",
        "昂熱": "angers",
        "歐塞爾": "auxerre",
        "勒哈佛爾": "le havre",
        "勒阿弗爾": "le havre",
        "洛里昂": "lorient",
        "梅斯": "metz",
        "巴黎FC": "paris fc",
        "聖伊天": "saint etienne",
        "聖艾蒂安": "saint etienne",
        "蘭斯": "reims",
        "勒芒": "le mans",
        # 歐冠常見
        "葡萄牙體育": "sporting cp",
        "里斯本競技": "sporting cp",
        "本菲卡": "benfica",
        "波爾圖": "porto",
        "布拉加": "braga",
        "PSV": "psv",
        "埃因霍溫": "psv",
        "費耶諾德": "feyenoord",
        "阿賈克斯": "ajax",
        "布魯日": "club brugge",
        "聖吉羅斯聯": "union sg",
        "加拉塔薩雷": "galatasaray",
        "費內巴切": "fenerbahce",
        "貝西克塔斯": "besiktas",
        "布拉格斯拉維亞": "slavia prague",
        "布拉格斯巴達": "sparta prague",
        "薩巴赫": "sabah",
        "卡拉巴赫": "qarabag",
        "維京": "viking",
        "博德閃耀": "bodo glimt",
        "博多格林特": "bodo glimt",
        "莫爾德": "molde",
        "林茨": "lask",
        "格拉茨": "sturm graz",
        "薩爾斯堡": "salzburg",
        "頓內次克礦工": "shakhtar",
        "礦工": "shakhtar",
        "基輔迪納摩": "dynamo kyiv",
        "AEK雅典": "aek athens",
        "奧林匹亞科斯": "olympiacos",
        "PAOK": "paok",
        "帕納辛奈科斯": "panathinaikos",
        "斯洛凡": "slovan bratislava",
        "布拉提斯拉瓦斯洛凡": "slovan bratislava",
        "塞爾提克": "celtic",
        "凱爾特人": "celtic",
        "流浪者": "rangers",
        "哥本哈根": "copenhagen",
        "貝爾格勒紅星": "red star",
        "紅星": "red star",
        "薩格勒布迪納摩": "dinamo zagreb",
        "年輕男孩": "young boys",
        "巴塞爾": "basel",
        "帕福斯": "pafos",
        "凱拉特": "kairat",
        "費倫茨瓦羅斯": "ferencvaros",
        "盧多戈雷茨": "ludogorets",
        # ---- spellings seen in the real feed (2026-10)
        "薩巴赫馬薩齊爾": "sabah",
        "比亞雷亞爾": "villarreal",
        "恩荷芬": "psv",
        "飛燕諾": "feyenoord",
        "博德格林特": "bodo glimt",
        "布拉提斯拉瓦": "slovan bratislava",
        "頓涅茨克礦工": "shakhtar",
        "皇家貝提斯": "real betis",
        "波圖": "porto",
        "塞爾塔維戈": "celta vigo",
        "雅典AEK": "aek athens",
    }
)
