"""The uv security gate accepts only exact, current, no-fix risk decisions."""

import copy
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from scripts.advisory_policy import PolicyError, load_policy
from scripts.uv_advisory_policy import main, validate_uv_report

LOCKED = {("example", "1.0"), ("another", "2.0")}
NOW = datetime(2026, 10, 6, tzinfo=UTC)
POLICY: dict[str, Any] = {
    "version": 1,
    "exceptions": [
        {
            "advisory": "GHSA-2345-6789-cfgh",
            "ecosystem": "PyPI",
            "package": "example",
            "version": "1.0",
            "expiresAt": "2026-10-21T00:00:00.000Z",
            "reason": "Synthetic test risk acceptance only",
            "noFixReason": "Upstream has no fixed release",
            "upstream": "https://example.test/advisory",
        }
    ],
}


def _finding() -> dict[str, Any]:
    return {
        "dependency": {"name": "example", "version": "1.0"},
        "id": "PYSEC-2026-1",
        "display_id": "GHSA-2345-6789-cfgh",
        "aliases": ["GHSA-2345-6789-cfgh"],
        "summary": "A synthetic finding",
        "description": None,
        "link": None,
        "fix_versions": [],
        "published": None,
        "modified": None,
    }


def _report(*findings: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": {"version": "preview"},
        "summary": {
            "audited_packages": 2,
            "vulnerabilities": len(findings),
            "adverse_statuses": 0,
        },
        "vulnerabilities": list(findings),
        "adverse_statuses": [],
    }


def test_clean_report_with_empty_policy_passes() -> None:
    assert "No accepted risks" in validate_uv_report(_report(), (), LOCKED, 0)[0]


def test_exact_alias_no_fix_match_is_visible() -> None:
    entries = load_policy(POLICY, LOCKED, NOW)
    result = validate_uv_report(_report(_finding()), entries, LOCKED, 1)
    assert "ACCEPTED RISK (uv)" in result[0]
    assert "example@1.0" in result[0]
    assert "2026-10-21" in result[0]
    assert "no-fix rationale" in result[0]


@pytest.mark.parametrize("code", [-1, 2, 127, 128, True])
def test_operational_error_never_passes_even_with_findings(code: int) -> None:
    entries = load_policy(POLICY, LOCKED, NOW)
    with pytest.raises(PolicyError, match="operational"):
        validate_uv_report(_report(_finding()), entries, LOCKED, code)


@pytest.mark.parametrize("findings,code", [([], 1), ([_finding()], 0)])
def test_exit_status_must_agree_with_raw_findings(
    findings: list[dict[str, Any]], code: int
) -> None:
    with pytest.raises(PolicyError, match="contradicts"):
        validate_uv_report(_report(*findings), (), LOCKED, code)


def test_unapproved_finding_and_mixed_findings_fail() -> None:
    with pytest.raises(PolicyError, match="unaccepted"):
        validate_uv_report(_report(_finding()), (), LOCKED, 1)
    unrelated = _finding()
    unrelated["dependency"] = {"name": "another", "version": "2.0"}
    with pytest.raises(PolicyError, match="unaccepted"):
        validate_uv_report(
            _report(_finding(), unrelated), load_policy(POLICY, LOCKED, NOW), LOCKED, 1
        )


@pytest.mark.parametrize("change", ["name", "version", "advisory"])
def test_exact_scope_is_required(change: str) -> None:
    finding = _finding()
    if change == "name":
        finding["dependency"]["name"] = "another"
    elif change == "version":
        finding["dependency"]["version"] = "9.0"
    else:
        finding["id"] = "GHSA-cfgh-2345-6789"
        finding["display_id"] = finding["id"]
        finding["aliases"] = []
    with pytest.raises(PolicyError, match=r"outside|unaccepted"):
        validate_uv_report(
            _report(finding), load_policy(POLICY, LOCKED, NOW), LOCKED, 1
        )


def test_fixed_release_requires_upgrade() -> None:
    finding = _finding()
    finding["fix_versions"] = ["1.1"]
    with pytest.raises(PolicyError, match="fixed release"):
        validate_uv_report(
            _report(finding), load_policy(POLICY, LOCKED, NOW), LOCKED, 1
        )


def test_unmatched_exception_is_stale() -> None:
    with pytest.raises(PolicyError, match="stale"):
        validate_uv_report(_report(), load_policy(POLICY, LOCKED, NOW), LOCKED, 0)


@pytest.mark.parametrize(
    "section,field,value",
    [
        (None, "extra", []),
        (None, "schema", {"version": "changed"}),
        ("summary", "extra", 0),
        ("summary", "audited_packages", True),
        ("summary", "audited_packages", 1),
        ("summary", "vulnerabilities", 1),
        (None, "vulnerabilities", None),
        (None, "adverse_statuses", None),
    ],
)
def test_malformed_or_incomplete_reports_fail(
    section: str | None, field: str, value: object
) -> None:
    report = _report()
    target = report if section is None else report[section]
    target[field] = value
    with pytest.raises(PolicyError):
        validate_uv_report(report, (), LOCKED, 0)


@pytest.mark.parametrize(
    "field,value",
    [
        ("fix_versions", None),
        ("aliases", "GHSA-2345-6789-cfgh"),
        ("dependency", {}),
        ("summary", 42),
        ("display_id", "not-an-alias"),
    ],
)
def test_malformed_finding_fails(field: str, value: object) -> None:
    finding = _finding()
    finding[field] = value
    with pytest.raises(PolicyError):
        validate_uv_report(
            _report(finding), load_policy(POLICY, LOCKED, NOW), LOCKED, 1
        )


@pytest.mark.parametrize("status", ["archived", "deprecated", "quarantined"])
@pytest.mark.parametrize("reason", [None, "Native package status warning"])
def test_adverse_statuses_preserve_native_warnings(
    status: str, reason: str | None
) -> None:
    report = _report()
    report["adverse_statuses"] = [
        {"name": "example", "status": status, "reason": reason}
    ]
    report["summary"]["adverse_statuses"] = 1
    messages = validate_uv_report(report, (), LOCKED, 0)
    assert any(
        "PACKAGE STATUS WARNING" in message and status in message
        for message in messages
    )
    report["vulnerabilities"] = [_finding()]
    report["summary"]["vulnerabilities"] = 1
    with pytest.raises(PolicyError, match="unaccepted"):
        validate_uv_report(report, (), LOCKED, 1)


@pytest.mark.parametrize(
    "field,value",
    [("status", "unknown"), ("reason", 42), ("name", "unknown"), ("extra", None)],
)
def test_malformed_adverse_statuses_fail_closed(field: str, value: object) -> None:
    report = _report()
    status: dict[str, object] = {
        "name": "example",
        "status": "archived",
        "reason": None,
    }
    status[field] = value
    report["adverse_statuses"] = [status]
    report["summary"]["adverse_statuses"] = 1
    with pytest.raises(PolicyError):
        validate_uv_report(report, (), LOCKED, 0)


def test_cli_preserves_raw_report_and_writes_summary(tmp_path: Path) -> None:
    policy = tmp_path / "policy.json"
    policy.write_text('{"version":1,"exceptions":[]}', encoding="utf-8")
    lock = tmp_path / "uv.lock"
    lock.write_text(
        '[[package]]\nname="example"\nversion="1.0"\nsource={registry="https://pypi.org/simple"}\n'
        '[[package]]\nname="another"\nversion="2.0"\nsource={registry="https://pypi.org/simple"}\n',
        encoding="utf-8",
    )
    report = tmp_path / "uv.json"
    raw = json.dumps(_report())
    report.write_text(raw, encoding="utf-8")
    summary = tmp_path / "summary.md"
    args = [
        "--policy",
        str(policy),
        "--lock",
        str(lock),
        "--report",
        str(report),
        "--exit-code",
        "0",
        "--summary",
        str(summary),
    ]
    assert main(args) == 0
    assert main(args[:-2]) == 0
    assert report.read_text(encoding="utf-8") == raw
    assert "No accepted risks" in summary.read_text(encoding="utf-8")
    report.write_text('{"schema": {}, "schema": {}}', encoding="utf-8")
    assert main(args) == 1
    report.unlink()
    assert main(args) == 1


def test_repository_policy_passes_contract() -> None:
    root = Path(__file__).resolve().parent.parent
    policy = json.loads(
        (root / ".github/advisory-exceptions.json").read_text(encoding="utf-8")
    )
    assert policy["version"] == 1
    assert isinstance(policy["exceptions"], list)


def test_expired_policy_cannot_reach_uv_adjudication() -> None:
    policy = copy.deepcopy(POLICY)
    policy["exceptions"][0]["expiresAt"] = "2026-10-06T00:00:00.000Z"
    with pytest.raises(PolicyError, match="expired"):
        load_policy(policy, LOCKED, NOW)
