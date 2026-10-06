"""Fail-closed coverage for bounded vulnerability acceptance and raw OSV evidence."""

import copy
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from scripts.advisory_policy import (
    PolicyError,
    adjudicate,
    load_policy,
    locked_packages,
    main,
    read_json,
    validate_report,
)

NOW = datetime(2026, 10, 6, tzinfo=UTC)
LOCKED = {("demo", "1.0.0")}
GHSA = "GHSA-2345-6789-cfgh"


def entry() -> dict[str, object]:
    return {
        "advisory": GHSA,
        "ecosystem": "PyPI",
        "package": "demo",
        "version": "1.0.0",
        "expiresAt": "2026-10-21T00:00:00.000Z",
        "reason": "Dev-only risk reviewed for this release",
        "noFixReason": "Upstream confirms no patched release exists",
        "upstream": "https://example.com/issues/123",
    }


def policy(*entries: dict[str, object]) -> dict[str, object]:
    return {"version": 1, "exceptions": list(entries)}


def vulnerability(identifier: str = GHSA) -> dict[str, object]:
    return {
        "id": identifier,
        "aliases": [],
        "affected": [
            {
                "package": {"name": "demo", "ecosystem": "PyPI"},
                "ranges": [{"type": "ECOSYSTEM", "events": [{"introduced": "0"}]}],
            }
        ],
    }


def report(
    *vulns: dict[str, object],
    name: str = "demo",
    version: str = "1.0.0",
    source: str = "uv.lock",
) -> dict[str, object]:
    item: dict[str, object] = {
        "package": {"name": name, "version": version, "ecosystem": "PyPI"}
    }
    if vulns:
        item.update(
            {
                "vulnerabilities": list(vulns),
                "groups": [{"ids": [value["id"]]} for value in vulns],
            }
        )
    return {
        "results": [
            {"source": {"type": "lockfile", "path": source}, "packages": [item]}
        ]
    }


def test_empty_policy_keeps_clean_scans_clean() -> None:
    entries = load_policy(policy(), LOCKED, NOW)
    assert validate_report(report(), entries, LOCKED, Path.cwd(), 0) == [
        "No accepted risks; no known vulnerabilities in the complete OSV report."
    ]


def test_accepted_risk_is_visible_and_only_the_reporter_view_changes() -> None:
    raw = report(vulnerability())
    original = copy.deepcopy(raw)
    result, messages = adjudicate(
        raw, load_policy(policy(entry()), LOCKED, NOW), LOCKED, Path.cwd(), 1
    )
    assert raw == original
    assert "vulnerabilities" not in json.dumps(result)
    assert (
        "ACCEPTED RISK" in messages[0]
        and GHSA in messages[0]
        and "demo@1.0.0" in messages[0]
    )
    assert "2026-10-21" in messages[0] and "upstream" in messages[0]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("advisory", "*"),
        ("ecosystem", "npm"),
        ("package", "other"),
        ("version", "1.*"),
        ("expiresAt", "2026-10-06T00:00:00.000Z"),
        ("expiresAt", "2026-10-06"),
        ("expiresAt", "2026-13-40T00:00:00.000Z"),
        ("reason", " "),
        ("noFixReason", ""),
        ("upstream", "http://example.com"),
        ("upstream", "https://secret@example.com"),
        ("upstream", True),
        ("extra", "unknown"),
    ],
)
def test_policy_rejects_invalid_or_expired_entries(field: str, value: object) -> None:
    item = entry()
    item[field] = value
    with pytest.raises((PolicyError, ValueError)):
        load_policy(policy(item), LOCKED, NOW)


@pytest.mark.parametrize(
    "bad",
    [
        None,
        [],
        {},
        {"version": True, "exceptions": []},
        {"version": 1, "exceptions": {}},
        {"version": 1, "exceptions": [], "extra": 1},
    ],
)
def test_policy_schema_is_closed(bad: object) -> None:
    with pytest.raises(PolicyError):
        load_policy(bad, LOCKED, NOW)


def test_duplicate_entries_are_rejected() -> None:
    with pytest.raises(PolicyError, match="duplicate"):
        load_policy(policy(entry(), entry()), LOCKED, NOW)


@pytest.mark.parametrize(
    "text", ['{"version":1,"version":1,"exceptions":[]}', '{"value":NaN}', "{"]
)
def test_json_is_strict(tmp_path: Path, text: str) -> None:
    path = tmp_path / "policy.json"
    path.write_text(text)
    with pytest.raises(ValueError):
        read_json(path)


@pytest.mark.parametrize("exit_code", [-1, 2, 126, 127, 128, 255])
def test_operational_errors_never_become_accepted_risks(exit_code: int) -> None:
    with pytest.raises(PolicyError, match="operational"):
        validate_report(
            report(vulnerability()),
            load_policy(policy(entry()), LOCKED, NOW),
            LOCKED,
            Path.cwd(),
            exit_code,
        )


@pytest.mark.parametrize(("raw", "code"), [(report(), 1), (report(vulnerability()), 0)])
def test_exit_and_report_must_agree(raw: object, code: int) -> None:
    entries = load_policy(policy(entry()) if code == 0 else policy(), LOCKED, NOW)
    with pytest.raises(PolicyError, match="mismatch"):
        validate_report(raw, entries, LOCKED, Path.cwd(), code)


@pytest.mark.parametrize(
    "raw",
    [
        report(vulnerability("GHSA-cfgh-jmpq-rvwx")),
        report(vulnerability(), name="other"),
        report(vulnerability(), version="2.0.0"),
        report(vulnerability(), source="nested/uv.lock"),
        report(vulnerability(), vulnerability("GHSA-cfgh-jmpq-rvwx")),
    ],
)
def test_unrelated_advisory_package_version_or_source_still_blocks(raw: object) -> None:
    with pytest.raises(PolicyError, match="unaccepted"):
        validate_report(
            raw, load_policy(policy(entry()), LOCKED, NOW), LOCKED, Path.cwd(), 1
        )


def test_no_exception_fails_every_finding() -> None:
    with pytest.raises(PolicyError, match="unaccepted"):
        validate_report(report(vulnerability()), (), LOCKED, Path.cwd(), 1)


def test_known_alias_is_accepted_only_for_exact_package() -> None:
    vuln = vulnerability("PYSEC-2026-1")
    vuln["aliases"] = [GHSA]
    assert validate_report(
        report(vuln), load_policy(policy(entry()), LOCKED, NOW), LOCKED, Path.cwd(), 1
    )


@pytest.mark.parametrize(
    "affected",
    [
        None,
        [],
        [{"package": {"name": "other", "ecosystem": "PyPI"}}],
        [
            {
                "package": {"name": "demo", "ecosystem": "PyPI"},
                "ranges": [
                    {
                        "type": "ECOSYSTEM",
                        "events": [{"introduced": "0"}, {"fixed": "1.0.1"}],
                    }
                ],
            }
        ],
        [
            {
                "package": {"name": "demo", "ecosystem": "PyPI"},
                "ranges": [{"type": "GIT", "events": [{"introduced": "0"}]}],
            }
        ],
    ],
)
def test_missing_or_fixed_upstream_evidence_blocks(affected: object) -> None:
    vuln = vulnerability()
    vuln["affected"] = affected
    with pytest.raises(PolicyError):
        validate_report(
            report(vuln),
            load_policy(policy(entry()), LOCKED, NOW),
            LOCKED,
            Path.cwd(),
            1,
        )


@pytest.mark.parametrize(
    "raw",
    [
        None,
        {},
        {"results": []},
        {"results": "bad"},
        {"results": [], "error": "scan failed"},
        report(name="other"),
        {
            "results": [
                {
                    "source": {"path": "uv.lock", "type": "lockfile"},
                    "packages": [
                        {"package": dict[str, object](), "groups": [{"ids": [GHSA]}]}
                    ],
                }
            ]
        },
    ],
)
def test_malformed_or_incomplete_reports_fail_closed(raw: object) -> None:
    with pytest.raises(PolicyError):
        validate_report(raw, (), LOCKED, Path.cwd(), 0)


def test_stale_policy_requires_removal() -> None:
    with pytest.raises(PolicyError, match="stale"):
        validate_report(
            report(), load_policy(policy(entry()), LOCKED, NOW), LOCKED, Path.cwd(), 0
        )


def test_uv_lock_requires_real_registry_identities() -> None:
    lock = {
        "package": [
            {
                "name": "demo",
                "version": "1.0.0",
                "source": {"registry": "https://pypi.org/simple"},
            },
            {"name": "project", "version": "1", "source": {"editable": "."}},
        ]
    }
    assert locked_packages(lock) == LOCKED
    with pytest.raises(PolicyError):
        locked_packages({"package": []})


def test_cli_never_writes_reporter_input_after_failure(tmp_path: Path) -> None:
    root = Path(__file__).parent.parent
    bad = tmp_path / "bad.json"
    bad.write_text("{}")
    output = tmp_path / "result.json"
    assert (
        main(
            [
                "--policy",
                str(root / ".github/advisory-exceptions.json"),
                "--lock",
                str(root / "uv.lock"),
                "--report",
                str(bad),
                "--scan-exit",
                "127",
                "--output",
                str(output),
            ]
        )
        == 1
    )
    assert not output.exists()


def test_raw_scanner_config_cannot_hide_findings(tmp_path: Path) -> None:
    config = tmp_path / "osv-scanner.toml"
    config.write_text('[[IgnoredVulns]]\nid = "GHSA-2345-6789-cfgh"\n')
    assert main(["--scanner-config", str(config)]) == 1


def test_explicit_affected_version_without_fixed_release_can_be_accepted() -> None:
    vuln = vulnerability()
    vuln["affected"] = [
        {"package": {"name": "demo", "ecosystem": "PyPI"}, "versions": ["1.0.0"]}
    ]
    assert (
        "ACCEPTED RISK"
        in validate_report(
            report(vuln),
            load_policy(policy(entry()), LOCKED, NOW),
            LOCKED,
            Path.cwd(),
            1,
        )[0]
    )


def test_cli_offline_and_clean_report_paths(tmp_path: Path) -> None:
    lock = tmp_path / "uv.lock"
    lock.write_text(
        '[[package]]\nname="demo"\nversion="1.0.0"\nsource={registry="https://pypi.org/simple"}\n',
        encoding="utf-8",
    )
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps(policy()), encoding="utf-8")
    scan = tmp_path / "scan.json"
    raw = json.dumps(report())
    scan.write_text(raw, encoding="utf-8")
    output = tmp_path / "adjudicated.json"
    summary = tmp_path / "summary.md"
    args = ["--policy", str(policy_path), "--lock", str(lock)]
    assert main(args) == 0
    assert main([*args, "--output", str(output)]) == 1
    args += ["--report", str(scan), "--output", str(output), "--scan-exit", "0"]
    assert main(args) == 0
    assert main([*args, "--summary", str(summary)]) == 0
    assert scan.read_text(encoding="utf-8") == raw
    assert "No accepted risks" in summary.read_text(encoding="utf-8")
    assert json.loads(output.read_text(encoding="utf-8")) == report()
