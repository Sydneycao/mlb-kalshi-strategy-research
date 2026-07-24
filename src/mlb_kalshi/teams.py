from __future__ import annotations

import re
import unicodedata


def _slug(value: str) -> str:
    ascii_value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    normalized = ascii_value.lower().replace("&", " and ")
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", normalized)).strip()


_TEAM_ALIASES: dict[str, tuple[str, ...]] = {
    "ARI": ("arizona", "arizona diamondbacks", "diamondbacks", "d backs"),
    "ATL": ("atlanta", "atlanta braves", "braves"),
    "BAL": ("baltimore", "baltimore orioles", "orioles"),
    "BOS": ("boston", "boston red sox", "red sox"),
    "CHC": ("chicago cubs", "chicago c", "chi cubs", "cubs"),
    "CWS": (
        "chicago white sox",
        "chicago ws",
        "chi white sox",
        "white sox",
        "cws",
    ),
    "CIN": ("cincinnati", "cincinnati reds", "reds"),
    "CLE": ("cleveland", "cleveland guardians", "guardians"),
    "COL": ("colorado", "colorado rockies", "rockies"),
    "DET": ("detroit", "detroit tigers", "tigers"),
    "HOU": ("houston", "houston astros", "astros"),
    "KC": ("kansas city", "kansas city royals", "kc royals", "royals"),
    "LAA": (
        "los angeles angels",
        "los angeles a",
        "la angels",
        "angels",
        "anaheim angels",
    ),
    "LAD": ("los angeles dodgers", "los angeles d", "la dodgers", "dodgers"),
    "MIA": ("miami", "miami marlins", "marlins", "florida marlins"),
    "MIL": ("milwaukee", "milwaukee brewers", "brewers"),
    "MIN": ("minnesota", "minnesota twins", "twins"),
    "NYM": ("new york mets", "new york m", "ny mets", "mets"),
    "NYY": ("new york yankees", "new york y", "ny yankees", "yankees"),
    "ATH": (
        "athletics",
        "the athletics",
        "oakland",
        "oakland athletics",
        "sacramento athletics",
        "a s",
        "as",
    ),
    "PHI": ("philadelphia", "philadelphia phillies", "phillies"),
    "PIT": ("pittsburgh", "pittsburgh pirates", "pirates"),
    "SD": ("san diego", "san diego padres", "padres"),
    "SF": ("san francisco", "san francisco giants", "giants"),
    "SEA": ("seattle", "seattle mariners", "mariners"),
    "STL": ("st louis", "st louis cardinals", "cardinals"),
    "TB": ("tampa bay", "tampa bay rays", "rays"),
    "TEX": ("texas", "texas rangers", "rangers"),
    "TOR": ("toronto", "toronto blue jays", "blue jays"),
    "WSH": ("washington", "washington nationals", "nationals"),
    "AL": ("al", "american league", "american league all stars"),
    "NL": ("nl", "national league", "national league all stars"),
}

_ALIAS_TO_CODE = {
    _slug(alias): code for code, aliases in _TEAM_ALIASES.items() for alias in aliases
}
_ALIAS_TO_CODE.update({code.lower(): code for code in _TEAM_ALIASES})


def normalize_team(value: str) -> str:
    """Return a stable MLB team code, retaining unknown names explicitly."""

    slug = _slug(value)
    if not slug:
        raise ValueError("team name cannot be empty")
    return _ALIAS_TO_CODE.get(slug, f"UNKNOWN:{slug}")
