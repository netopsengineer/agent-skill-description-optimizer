"""Tests that the ruff lint selection still satisfies the Modern Python floor.

`PY-STYLE-001` requires at least the `E4,E7,E9,F,I,UP,B,SIM,C4,PIE,RUF` families, and
requires them to be NAMED in configuration rather than inherited from ruff's default
selection. Ruff 0.16 proved why: it replaced its defaults (59 -> 413 rules) and dropped
18 pycodestyle/pyflakes rules out of them, which silently weakened this gate while CI
stayed green. Asserting the families here turns that prose requirement into a gate, so
deleting one fails a test instead of passing quietly.

These tests read `pyproject.toml` only; they never invoke ruff, so they stay fast and
cannot be affected by which ruff version happens to be installed.
"""

import tomllib
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = PROJECT_ROOT / "pyproject.toml"

# The mandatory floor from PY-STYLE-001. Families, never individual rule codes: codes get
# renamed and removed upstream (E999 already was), families do not.
_REQUIRED_FAMILIES = frozenset(
    {"E4", "E7", "E9", "F", "I", "UP", "B", "SIM", "C4", "PIE", "RUF"}
)

# The "add applicable" families from the same rule that this project has adopted. ASYNC
# and DTZ are intentionally absent: they arrive via ruff's own defaults.
_ADOPTED_APPLICABLE_FAMILIES = frozenset({"S", "LOG", "G", "PERF"})


def _lint_config() -> dict[str, Any]:
    """Read the `[tool.ruff.lint]` table from the project's pyproject.toml.

    Returns:
        The parsed `[tool.ruff.lint]` table.
    """
    with PYPROJECT.open("rb") as handle:
        data: dict[str, Any] = tomllib.load(handle)
    lint: dict[str, Any] = data["tool"]["ruff"]["lint"]
    return lint


def _selected_families() -> frozenset[str]:
    """Collect the rule families named in `extend-select`.

    Returns:
        The selected family prefixes.
    """
    selected: list[str] = _lint_config()["extend-select"]
    return frozenset(selected)


def test_mandatory_rule_families_are_named_explicitly() -> None:
    # Every PY-STYLE-001 family must appear literally in extend-select. Relying on ruff's
    # defaults to supply any of them is exactly the failure this test exists to catch.
    missing = sorted(_REQUIRED_FAMILIES - _selected_families())
    assert not missing, (
        f"PY-STYLE-001 floor not named in [tool.ruff.lint] extend-select: {missing}. "
        "Do not rely on ruff's default selection to supply these -- ruff narrows its "
        "defaults across releases (0.16 dropped 18 E/F rules), which weakens the gate "
        "silently."
    )


def test_adopted_applicable_families_are_retained() -> None:
    # Guards the security/logging/perf families this project opted into, so a future
    # trim of extend-select cannot quietly drop them either.
    missing = sorted(_ADOPTED_APPLICABLE_FAMILIES - _selected_families())
    assert not missing, f"adopted PY-STYLE-001 applicable families dropped: {missing}"


def test_ignores_are_narrow_and_justified() -> None:
    # PY-STYLE-001 requires narrow, documented ignores. Pin the exact set so adding one
    # is a deliberate, reviewed edit rather than an unnoticed widening of the gate.
    assert sorted(_lint_config()["ignore"]) == [
        "RUF001",
        "RUF002",
        "RUF003",
        "TRY004",
    ]


def test_no_required_family_is_ignored_wholesale() -> None:
    # A whole-family ignore (e.g. "F" or "E4") would cancel the floor outright while
    # leaving extend-select looking correct. Narrow single-rule ignores inside a required
    # family are permitted by PY-STYLE-001 -- RUF001-RUF003 are exactly that, and the
    # exact ignore set is pinned by test_ignores_are_narrow_and_justified above.
    ignored = frozenset(_lint_config()["ignore"])
    cancelled = sorted(ignored & _REQUIRED_FAMILIES)
    assert not cancelled, (
        f"ignore cancels whole PY-STYLE-001 families: {cancelled}. Ignore individual "
        "rule codes with a documented reason instead of disabling a required family."
    )
