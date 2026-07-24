import pytest

from mlb_kalshi.teams import normalize_team


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Chicago WS", "CWS"),
        ("Chicago White Sox", "CWS"),
        ("NY Yankees", "NYY"),
        ("New York Yankees", "NYY"),
        ("LA Dodgers", "LAD"),
        ("Los Angeles D", "LAD"),
        ("New York M", "NYM"),
        ("St. Louis", "STL"),
        ("San Francisco Giants", "SF"),
        ("Athletics", "ATH"),
        ("A's", "ATH"),
    ],
)
def test_team_aliases_normalize_to_stable_codes(raw: str, expected: str) -> None:
    assert normalize_team(raw) == expected


def test_unknown_team_is_retained_explicitly() -> None:
    assert normalize_team("Montreal Expos") == "UNKNOWN:montreal expos"


def test_empty_team_is_rejected() -> None:
    with pytest.raises(ValueError, match="cannot be empty"):
        normalize_team("  ")
