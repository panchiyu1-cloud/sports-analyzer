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
    "bk", "if", "ff", "es", "kv", "sl", "gnk", "nk", "hnk", "pae", "bsc", "tc", "slc", "fsv",
    "royale", "kc",
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
    "racing lens": "lens", "racing strasbourg alsace": "strasbourg", "troyes": "troyes",
    "le mans": "le mans",
    # ---- promoted / API long forms seen in 2026-27
    "coventry city": "coventry", "coventry": "coventry", "hull city": "hull", "hull": "hull",
    "deportivo la coruna": "la coruna", "la coruna": "la coruna", "deportivo": "la coruna",
    "real racing santander": "santander", "racing santander": "santander", "santander": "santander",
    "rayo vallecano madrid": "rayo vallecano", "real sociedad futbol": "real sociedad",
    "malaga": "malaga", "paderborn": "paderborn", "elversberg": "elversberg", "schalke": "schalke",
    "darmstadt": "darmstadt", "frosinone": "frosinone", "cremonese": "cremonese",
    # ---- Champions League clubs outside the top five leagues (API name / odds name / CSV name)
    "sporting clube portugal": "sporting cp", "sporting lisbon": "sporting cp", "sporting": "sporting cp", "sporting cp": "sporting cp",
    "benfica": "benfica", "porto": "porto", "sporting braga": "braga", "braga": "braga",
    "psv": "psv", "psv eindhoven": "psv", "feyenoord rotterdam": "feyenoord", "feyenoord": "feyenoord",
    "ajax": "ajax", "ajax amsterdam": "ajax",
    "club brugge": "club brugge", "club brugge kv": "club brugge", "brugge": "club brugge",
    "union saint gilloise": "union sg", "union sg": "union sg", "union st gilloise": "union sg",
    "galatasaray": "galatasaray", "fenerbahce": "fenerbahce", "besiktas": "besiktas",
    "slavia praha": "slavia prague", "slavia prague": "slavia prague", "sparta praha": "sparta prague", "sparta prague": "sparta prague",
    "sabah": "sabah", "sabah fa": "sabah", "qarabag": "qarabag", "qarabag agdam": "qarabag",
    "viking": "viking", "bodo glimt": "bodo glimt", "molde": "molde",
    "lask": "lask", "lask linz": "lask", "sturm graz": "sturm graz", "salzburg": "salzburg", "red bull salzburg": "salzburg",
    "shakhtar": "shakhtar", "shakhtar donetsk": "shakhtar", "dynamo kyiv": "dynamo kyiv", "dynamo kiev": "dynamo kyiv",
    "aek": "aek athens", "aek athens": "aek athens", "olympiacos": "olympiacos", "olympiakos": "olympiacos", "olympiacos piraeus": "olympiacos", "olympiakos piraeus": "olympiacos", "paok": "paok", "paok thessaloniki": "paok", "panathinaikos": "panathinaikos",
    "slovan bratislava": "slovan bratislava", "celtic": "celtic", "rangers": "rangers",
    "copenhagen": "copenhagen", "kobenhavn": "copenhagen", "fc copenhagen": "copenhagen", "fc kobenhavn": "copenhagen",
    "red star belgrade": "red star", "crvena zvezda": "red star", "red star": "red star",
    "dinamo zagreb": "dinamo zagreb", "young boys": "young boys", "basel": "basel",
    "pafos": "pafos", "kairat": "kairat", "kairat almaty": "kairat", "ferencvaros": "ferencvaros", "ferencvarosi": "ferencvaros",
    "ludogorets": "ludogorets", "ludogorets razgrad": "ludogorets", "bruges": "club brugge",
    # ---- MLB: the club dropped its city name in 2025; feeds disagree on what to call it
    "oakland athletics": "athletics", "sacramento athletics": "athletics", "athletics": "athletics",
    "st louis cardinals": "st louis cardinals", "saint louis cardinals": "st louis cardinals",
}


# Letters that NFKD does not decompose into base + accent.
_TRANSLIT = str.maketrans({"ø": "o", "Ø": "O", "æ": "ae", "Æ": "AE", "ß": "ss", "đ": "d", "Đ": "D",
                           "ł": "l", "Ł": "L", "ı": "i", "œ": "oe", "Œ": "OE"})


def normalise(name: str) -> str:
    """Lower-case, strip accents and punctuation, drop legal-form tokens."""
    text = unicodedata.normalize("NFKD", name.translate(_TRANSLIT))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower().replace("&", " and ").replace("-", " ").replace(".", " ")
    text = re.sub(r"[^a-z0-9' ]+", " ", text)
    # Pure numbers are founding years or legal suffixes ("Paderborn 07", "Schalke 04").
    tokens = [t for t in text.split() if t not in _DROP_TOKENS and not t.isdigit()]
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
