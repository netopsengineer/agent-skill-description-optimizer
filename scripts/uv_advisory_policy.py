"""Adjudicate raw uv audit JSON without advisory-wide suppression flags.

Only exact, unexpired no-fix findings already approved in the shared policy may
pass. The original JSON and scanner exit status stay available in CI artifacts.
"""

import argparse
import sys
import tomllib
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from scripts.advisory_policy import (
    ExceptionEntry,
    PolicyError,
    array,
    load_policy,
    locked_packages,
    mapping,
    read_json,
    require,
    string,
)

VULNERABILITY_FIELDS = {
    "dependency",
    "id",
    "display_id",
    "aliases",
    "summary",
    "description",
    "link",
    "fix_versions",
    "published",
    "modified",
}


def validate_uv_report(
    report: object,
    entries: tuple[ExceptionEntry, ...],
    locked: set[tuple[str, str]],
    exit_code: int,
) -> list[str]:
    """Require a complete raw uv report and exact no-fix exception matches."""
    require(type(exit_code) is int and exit_code in {0, 1}, "uv operational failure")
    data = mapping(report, "uv report")
    require(
        set(data) == {"schema", "summary", "vulnerabilities", "adverse_statuses"},
        "unknown or missing uv report fields",
    )
    require(data["schema"] == {"version": "preview"}, "unsupported uv JSON schema")
    summary = mapping(data["summary"], "summary")
    require(
        set(summary) == {"audited_packages", "vulnerabilities", "adverse_statuses"},
        "unknown or missing uv summary fields",
    )
    require(
        all(type(value) is int and value >= 0 for value in summary.values()),
        "invalid uv summary counters",
    )
    require(
        summary["audited_packages"] == len(locked) and bool(locked),
        "uv did not audit every locked registry package",
    )
    findings = array(data["vulnerabilities"], "vulnerabilities")
    statuses = array(data["adverse_statuses"], "adverse_statuses")
    require(
        summary["vulnerabilities"] == len(findings)
        and summary["adverse_statuses"] == len(statuses),
        "uv summary does not match findings",
    )
    warnings: list[str] = []
    for raw_status in statuses:
        status = mapping(raw_status, "adverse status")
        require(
            set(status) == {"name", "status", "reason"}, "malformed uv adverse status"
        )
        name = string(status["name"], "status package")
        require(
            name in {package for package, _ in locked}, "status is outside the lockfile"
        )
        require(
            status["status"] in ("archived", "deprecated", "quarantined"),
            "unknown adverse status",
        )
        reason = status["reason"]
        require(
            reason is None or isinstance(reason, str), "invalid adverse status reason"
        )
        warnings.append(
            f"PACKAGE STATUS WARNING (uv): {name} is {status['status']}; reason: {reason!r}"
        )
    require(exit_code == int(bool(findings)), "uv exit status contradicts findings")
    used: set[ExceptionEntry] = set()
    for raw in findings:
        finding = mapping(raw, "vulnerability")
        require(set(finding) == VULNERABILITY_FIELDS, "malformed uv vulnerability")
        dependency = mapping(finding["dependency"], "dependency")
        require(set(dependency) == {"name", "version"}, "malformed uv dependency")
        name = string(dependency["name"], "dependency name")
        version = string(dependency["version"], "dependency version")
        require((name, version) in locked, "uv finding is outside the lockfile")
        identifier = string(finding["id"], "advisory id")
        aliases = {
            string(value, "alias") for value in array(finding["aliases"], "aliases")
        }
        display_id = string(finding["display_id"], "display id")
        require(display_id in {identifier, *aliases}, "uv display ID is not an alias")
        for field in ("summary", "description", "link", "published", "modified"):
            require(
                finding[field] is None or isinstance(finding[field], str),
                f"invalid uv {field}",
            )
        fixes = array(finding["fix_versions"], "fix_versions")
        require(
            not fixes, f"upgrade {name}@{version}: {identifier} has a fixed release"
        )
        matching = [
            entry
            for entry in entries
            if entry.advisory in {identifier, *aliases}
            and entry.package == name
            and entry.version == version
        ]
        require(
            len(matching) == 1,
            f"unaccepted vulnerability: {identifier} in PyPI {name}@{version}",
        )
        used.add(matching[0])
    require(used == set(entries), "stale exception not matched by raw uv scan")
    decisions = [
        f"ACCEPTED RISK (uv): {entry.advisory} / PyPI {entry.package}@{entry.version}; "
        f"expires {entry.expires_at.isoformat()}; {entry.reason}; "
        f"no-fix rationale: {entry.no_fix_reason}; upstream: {entry.upstream}"
        for entry in entries
    ] or ["No accepted risks; no known vulnerabilities in the complete uv report."]
    return decisions + warnings


def main(argv: Sequence[str] | None = None) -> int:
    """Check one raw uv scan and publish its explicit risk decision."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--policy", type=Path, default=Path(".github/advisory-exceptions.json")
    )
    parser.add_argument("--lock", type=Path, default=Path("uv.lock"))
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--exit-code", type=int, required=True)
    parser.add_argument("--summary", type=Path)
    args = parser.parse_args(argv)
    try:
        locked = locked_packages(tomllib.loads(args.lock.read_text(encoding="utf-8")))
        entries = load_policy(read_json(args.policy), locked, datetime.now(UTC))
        messages = validate_uv_report(
            read_json(args.report), entries, locked, args.exit_code
        )
        summary = "\n".join(messages) + "\n"
        print(summary, end="")
        if args.summary is not None:
            with args.summary.open("a", encoding="utf-8") as stream:
                stream.write(summary)
    except (PolicyError, ValueError, OSError) as error:
        print(f"uv advisory policy failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
