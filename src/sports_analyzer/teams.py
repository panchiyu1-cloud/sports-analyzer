"""Team-name reconciliation across the three data sources.

football-data.org says "Wolverhampton Wanderers FC", The Odds API says
"Wolverhampton Wanderers", football-data.co.uk says "Wolves". Every join in this
project goes through `canonical()` so the three agree on one key.

Strategy, in order:
1. normalise (lower-case, strip accents, drop club-suffix tokens like FC/CF/1909),
2. look the normalised string up in ALIASES,
3. if still unknown, the caller may fall back to `best_match()` (difflib) against
   the small set of candidates that play on the same day.
"""

from __future__ import annotations

import difflib
import re
import unicodedata

# Tokens that carry no identity: legal forms, founding years, honorifics.
_DROP_TOKENS = {
    "fc", "afc", "cf", "cfc", "sc", "bc", "ac", "as", "ss", "ssc", "us", "rc", "ogc", "osc",
    "hsc", "sco", "aj", "vfl", "vfb", "tsg", "fsv", "sv", "rcd", "ud", "cd", "sd", "ca",
    "club", "calcio", "balompie", "football", "de", "da", "di", "the", "1", "04", "05", "29",
    "1846", "1899", "1901", "1907", "1909", "1913", "1848", "acf", "ssd", "cp", "fk", "sk",
    "bk", "if", "ff",
}

# normalised variant -> canonical key. Keep canonical keys plain English, lower-case.
ALIASES: dict[str, str] = {
    # ---- Premier League
    "manchester united": "manchester united", "man united": "manchester united", "man utd": "manchester united",
    "manchester city": "manchester city", "man city": "manchester city",
    "wolverhampton wanderers": "wolves", "wolves": "wolves",
    "brighton and hove albion": "brighton", "brighton hove albion": "brighton", "brighton": "brighton",
    "tottenham hotspur": "tottenham", "tottenham": "tottenham", "spurs": "tottenham",
    "west ham united": "west ham", "west ham": "west ham",
    "newcastle united": "newcastle", "newcastle": "newcastle",
    "nottingham forest": "nottingham forest", "nottm forest": "nottingham forest", "nott'm forest": "nottingham forest",
    "bournemouth": "bournemouth",
    "leicester city": "leicester", "leicester": "leicester",
    "ipswich town": "ipswich", "ipswich": "ipswich",
    "southampton": "southampton",
    "crystal palace": "crystal palace",
    "aston villa": "aston villa",
    "everton": "everton", "fulham": "fulham", "brentford": "brentford", "chelsea": "chelsea",
    "arsenal": "arsenal", "liverpool": "liverpool",
    "leeds united": "leeds", "leeds": "leeds",
    "burnley": "burnley", "sunderland": "sunderland",
    "sheffield united": "sheffield united", "sheffield utd": "sheffield united",
    "luton town": "luton", "luton": "luton",
    # ---- La Liga
    "atletico madrid": "atletico madrid", "ath madrid": "atletico madrid", "atletico de madrid": "atletico madrid",
    "barcelona": "barcelona", "real madrid": "real madrid",
    "athletic bilbao": "athletic bilbao", "ath bilbao": "athletic bilbao", "athletic": "athletic bilbao",
    "real betis": "real betis", "betis": "real betis",
    "celta vigo": "celta vigo", "celta": "celta vigo", "celta de vigo": "celta vigo",
    "deportivo alaves": "alaves", "alaves": "alaves",
    "real sociedad": "real sociedad", "sociedad": "real sociedad",
    "rayo vallecano": "rayo vallecano", "vallecano": "rayo vallecano",
    "espanyol": "espanyol", "espanol": "espanyol", "espanyol barcelona": "espanyol",
    "villarreal": "villarreal", "valencia": "valencia", "sevilla": "sevilla", "getafe": "getafe",
    "osasuna": "osasuna", "mallorca": "mallorca", "girona": "girona", "las palmas": "las palmas",
    "leganes": "leganes", "valladolid": "valladolid", "real valladolid": "valladolid",
    "levante": "levante", "elche": "elche", "real oviedo": "oviedo", "oviedo": "oviedo",
    # ---- Bundesliga
    "bayern munchen": "bayern munich", "bayern munich": "bayern munich", "bayern": "bayern munich",
    "borussia dortmund": "dortmund", "dortmund": "dortmund",
    "bayer leverkusen": "leverkusen", "leverkusen": "leverkusen",
    "rb leipzig": "rb leipzig", "leipzig": "rb leipzig",
    "union berlin": "union berlin",
    "eintracht frankfurt": "eintracht frankfurt", "ein frankfurt": "eintracht frankfurt",
    "stuttgart": "stuttgart", "freiburg": "freiburg",
    "hoffenheim": "hoffenheim", "mainz": "mainz", "wolfsburg": "wolfsburg",
    "borussia monchengladbach": "monchengladbach", "monchengladbach": "monchengladbach", "m'gladbach": "monchengladbach", "mgladbach": "monchengladbach", "gladbach": "monchengladbach",
    "augsburg": "augsburg", "werder bremen": "werder bremen", "bremen": "werder bremen",
    "heidenheim": "heidenheim", "st pauli": "st pauli", "holstein kiel": "holstein kiel",
    "bochum": "bochum", "hamburger": "hamburg", "hamburg": "hamburg", "hamburger sv": "hamburg",
    "koln": "koln", "cologne": "koln", "fc koln": "koln",
    # ---- Serie A
    "internazionale milano": "inter", "internazionale": "inter", "inter milan": "inter", "inter": "inter",
    "milan": "ac milan", "ac milan": "ac milan",
    "juventus": "juventus", "napoli": "napoli", "roma": "roma", "lazio": "lazio",
    "atalanta": "atalanta", "fiorentina": "fiorentina", "bologna": "bologna", "torino": "torino",
    "udinese": "udinese", "genoa": "genoa", "hellas verona": "verona", "verona": "verona",
    "cagliari": "cagliari", "lecce": "lecce", "parma": "parma", "empoli": "empoli",
    "venezia": "venezia", "monza": "monza", "como": "como", "sassuolo": "sassuolo",
    "pisa": "pisa", "cremonese": "cremonese",
    # ---- Ligue 1
    "paris saint germain": "psg", "paris saint-germain": "psg", "paris sg": "psg", "psg": "psg",
    "olympique marseille": "marseille", "marseille": "marseille",
    "olympique lyonnais": "lyon", "lyon": "lyon",
    "monaco": "monaco", "lille": "lille", "nice": "nice", "lens": "lens",
    "stade rennais": "rennes", "rennes": "rennes",
    "strasbourg": "strasbourg", "strasbourg alsace": "strasbourg",
    "nantes": "nantes", "stade brestois": "brest", "brest": "brest",
    "toulouse": "toulouse", "montpellier": "montpellier", "angers": "angers",
    "auxerre": "auxerre", "le havre": "le havre", "saint etienne": "saint etienne", "st etienne": "saint etienne",
    "reims": "reims", "stade de reims": "reims", "lorient": "lorient", "metz": "metz",
    "paris": "paris fc", "paris fc": "paris fc",
}


def normalise(name: str) -> str:
    """Lower-case, strip accents and punctuation, drop legal-form tokens."""
    text = unicodedata.normalize("NFKD", name)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower().replace("&", " and ").replace("-", " ").replace(".", " ")
    text = re.sub(r"[^a-z0-9' ]+", " ", text)
    tokens = [t for t in text.split() if t not in _DROP_TOKENS]
    return " ".join(tokens).strip()


def canonical(name: str) -> str:
    """Canonical key for a team name from any source. Unknown names fall back to normalised form."""
    norm = normalise(name)
    if norm in ALIASES:
        return ALIASES[norm]
    # Second chance: the raw lower-case string (handles "Nott'm Forest" style apostrophes).
    raw = name.strip().lower()
    if raw in ALIASES:
        return ALIASES[raw]
    return norm


def best_match(name: str, candidates: list[str], cutoff: float = 0.6) -> str | None:
    """Fuzzy-match `name` against canonical `candidates`; None if nothing is close enough."""
    key = canonical(name)
    if key in candidates:
        return key
    matches = difflib.get_close_matches(key, candidates, n=1, cutoff=cutoff)
    return matches[0] if matches else None
