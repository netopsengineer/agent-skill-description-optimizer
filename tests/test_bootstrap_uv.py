"""Prevent bootstrap pin drift and prove the scanner inventories include uv."""

import copy
import re
import tomllib
from pathlib import Path
from typing import Any

import pytest

from scripts.advisory_policy import PolicyError, locked_packages, validate_report
from scripts.bootstrap_uv import bootstrap_requirement, main
from scripts.uv_advisory_policy import validate_uv_report

ROOT = Path(__file__).resolve().parent.parent
PROJECT: dict[str, Any] = {"dependency-groups": {"bootstrap": ["uv==0.13.4"]}}
LOCK: dict[str, Any] = {
    "package": [
        {
            "name": "uv",
            "version": "0.13.4",
            "source": {"registry": "https://pypi.org/simple"},
        }
    ]
}


def test_exact_bootstrap_requirement() -> None:
    assert bootstrap_requirement(PROJECT, LOCK) == "uv==0.13.4"


@pytest.mark.parametrize(
    "requirement", [[], ["uv>=0.13.4"], ["uv==0.13.4", "uv==0.13.5"], ["pip==25.0.1"]]
)
def test_missing_ranged_or_duplicate_pin_fails(requirement: list[str]) -> None:
    with pytest.raises(PolicyError):
        bootstrap_requirement({"dependency-groups": {"bootstrap": requirement}}, LOCK)


@pytest.mark.parametrize("scenario", ["missing", "duplicate", "version", "source"])
def test_lock_divergence_fails(scenario: str) -> None:
    lock = copy.deepcopy(LOCK)
    if scenario == "missing":
        lock["package"] = []
    elif scenario == "duplicate":
        lock["package"].append(lock["package"][0])
    elif scenario == "version":
        lock["package"][0]["version"] = "0.11.29"
    else:
        lock["package"][0]["source"] = {"url": "https://example.test/uv.whl"}
    with pytest.raises(PolicyError):
        bootstrap_requirement(PROJECT, lock)


def test_main(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    project = tmp_path / "pyproject.toml"
    lock = tmp_path / "uv.lock"
    project.write_text(
        '[dependency-groups]\nbootstrap=["uv==0.13.4"]\n', encoding="utf-8"
    )
    lock.write_text(
        '[[package]]\nname="uv"\nversion="0.13.4"\n'
        'source={registry="https://pypi.org/simple"}\n',
        encoding="utf-8",
    )
    args = ["--project", str(project), "--lock", str(lock)]
    assert main(args) == 0
    assert capsys.readouterr().out == "uv==0.13.4\n"
    assert main([*args, "--installed-version", "uv 0.13.4 (platform)"]) == 0
    assert main([*args, "--installed-version", "uv 0.11.29"]) == 1
    assert "installed uv version differs" in capsys.readouterr().err
    project.write_text("broken = [", encoding="utf-8")
    assert main(args) == 1
    project.unlink()
    assert main(args) == 1


def test_repository_has_one_pin_consumed_by_every_bootstrap() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    requirement = bootstrap_requirement(project, lock)
    assert ("uv", requirement.removeprefix("uv==")) in locked_packages(lock)
    assert not any(
        item.startswith("uv==") for item in project["dependency-groups"]["dev"]
    )
    assert "bootstrap" not in project["tool"]["uv"].get("default-groups", ["dev"])
    steps: list[str] = []
    for path in (ROOT / ".github/workflows").glob("*.y*ml"):
        for block in re.split(r"(?m)^      - ", path.read_text(encoding="utf-8")):
            if "uses: astral-sh/setup-uv@" in block:
                steps.append(block)
                assert re.findall(r"(?m)^          version-file: (.+)$", block) == [
                    "uv.lock"
                ]
                assert not re.search(r"(?m)^          version:", block)
    assert len(steps) == 6
    hook = project["tool"]["semantic_release"]["build_command"]
    assert 'UV_REQUIREMENT="$(python -m scripts.bootstrap_uv)"' in hook
    assert 'python -m pip install --disable-pip-version-check "$UV_REQUIREMENT"' in hook
    assert (
        'python -m scripts.bootstrap_uv --installed-version "$(uv --version)"' in hook
    )
    assert not re.search(r"uv==[0-9]", hook)
    audit = (ROOT / "scripts/audit-dependencies.sh").read_text(encoding="utf-8")
    assert audit.index("scripts.bootstrap_uv") < audit.index("uv audit")
    assert '--installed-version "$(uv --version)"' in audit


def test_scanners_cannot_omit_bootstrap() -> None:
    locked = locked_packages(LOCK)
    uv_report: dict[str, Any] = {
        "schema": {"version": "preview"},
        "summary": {"audited_packages": 0, "vulnerabilities": 0, "adverse_statuses": 0},
        "vulnerabilities": [],
        "adverse_statuses": [],
    }
    with pytest.raises(PolicyError, match="every locked registry package"):
        validate_uv_report(uv_report, (), locked, 0)
    uv_report["summary"]["audited_packages"] = 1
    assert validate_uv_report(uv_report, (), locked, 0)
    osv_report: dict[str, Any] = {
        "results": [{"source": {"type": "lockfile", "path": "uv.lock"}, "packages": []}]
    }
    with pytest.raises(PolicyError):
        validate_report(osv_report, (), locked, ROOT, 0)


def test_vulnerable_bootstrap_cannot_pass_either_scanner() -> None:
    advisory = "GHSA-hq85-5cpj-mr62"
    locked = {("uv", "0.11.29")}
    finding: dict[str, Any] = {
        "dependency": {"name": "uv", "version": "0.11.29"},
        "id": advisory,
        "display_id": advisory,
        "aliases": [],
        "summary": None,
        "description": None,
        "link": None,
        "fix_versions": ["0.13.4"],
        "published": None,
        "modified": None,
    }
    uv_report: dict[str, Any] = {
        "schema": {"version": "preview"},
        "summary": {"audited_packages": 1, "vulnerabilities": 1, "adverse_statuses": 0},
        "vulnerabilities": [finding],
        "adverse_statuses": [],
    }
    with pytest.raises(PolicyError, match="has a fixed release"):
        validate_uv_report(uv_report, (), locked, 1)
    osv_report: dict[str, Any] = {
        "results": [
            {
                "source": {"type": "lockfile", "path": "uv.lock"},
                "packages": [
                    {
                        "package": {
                            "name": "uv",
                            "version": "0.11.29",
                            "ecosystem": "PyPI",
                        },
                        "vulnerabilities": [{"id": advisory}],
                        "groups": [{"ids": [advisory]}],
                    }
                ],
            }
        ]
    }
    with pytest.raises(PolicyError, match="unaccepted vulnerability"):
        validate_report(osv_report, (), locked, ROOT, 1)
