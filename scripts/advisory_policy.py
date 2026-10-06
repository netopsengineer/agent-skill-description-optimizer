"""Adjudicate one complete, unsuppressed OSV result with bounded risk acceptances.

The official scanner runs once with empty config and ``--all-packages``. Its raw
exit status and every finding are checked against exact lock scope and no-fix
evidence. This never creates or extends risk acceptance and never rescans with
advisory-wide ignores. Keep the original report alongside adjudicated results.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

GHSA = re.compile(r"GHSA-[23456789cfghjmpqrvwx]{4}(?:-[23456789cfghjmpqrvwx]{4}){2}")
EXPIRY = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.000)?Z")
POLICY_FIELDS = {
    "advisory",
    "ecosystem",
    "package",
    "version",
    "expiresAt",
    "reason",
    "noFixReason",
    "upstream",
}


class PolicyError(ValueError):
    """An unsafe, incomplete, or unsupported advisory policy or report."""


def require(condition: bool, message: str) -> None:
    """Reject input that does not establish the required invariant."""
    if not condition:
        raise PolicyError(message)


def mapping(value: object, label: str) -> dict[str, object]:
    """Require a JSON/TOML object with string keys."""
    require(isinstance(value, dict), f"{label} must be an object")
    return cast("dict[str, object]", value)


def array(value: object, label: str) -> list[object]:
    """Require a JSON array."""
    require(isinstance(value, list), f"{label} must be an array")
    return cast("list[object]", value)


def string(value: object, label: str) -> str:
    """Require a nonempty, single-line, stripped string."""
    require(isinstance(value, str), f"{label} must be a string")
    result = cast("str", value)
    require(
        bool(result)
        and result == result.strip()
        and all(ord(character) >= 32 for character in result),
        f"{label} must be nonempty, stripped, and single-line",
    )
    return result


def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Reject duplicate JSON keys rather than silently selecting the last."""
    result: dict[str, object] = {}
    for key, value in pairs:
        require(key not in result, f"duplicate JSON key: {key}")
        result[key] = value
    return result


def read_json(path: Path) -> object:
    """Read strict JSON, rejecting duplicate keys and nonfinite constants."""

    def invalid_constant(value: str) -> object:
        message = f"invalid JSON constant: {value}"
        raise PolicyError(message)

    return cast(
        "object",
        json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=unique_object,
            parse_constant=invalid_constant,
        ),
    )


@dataclass(frozen=True)
class ExceptionEntry:
    """An explicit reviewed acceptance of one exact PyPI advisory finding."""

    advisory: str
    package: str
    version: str
    expires_at: datetime
    reason: str
    no_fix_reason: str
    upstream: str


def locked_packages(lock: object) -> set[tuple[str, str]]:
    """Return exact registry package identities from the canonical uv lock."""
    packages: set[tuple[str, str]] = set()
    for raw in array(mapping(lock, "lock").get("package"), "lock.package"):
        item = mapping(raw, "locked package")
        source = mapping(item.get("source"), "locked package source")
        if "registry" not in source:
            continue
        identity = (
            string(item.get("name"), "name"),
            string(item.get("version"), "version"),
        )
        require(identity not in packages, f"duplicate locked package: {identity}")
        packages.add(identity)
    require(bool(packages), "lock must contain registry packages")
    return packages


def load_policy(
    data: object, locked: set[tuple[str, str]], now: datetime
) -> tuple[ExceptionEntry, ...]:
    """Validate the closed policy schema, exact lock scope, and UTC expiry."""
    policy = mapping(data, "policy")
    require(
        set(policy) == {"version", "exceptions"}, "unknown or missing policy fields"
    )
    require(
        type(policy["version"]) is int and policy["version"] == 1,
        "policy version must be 1",
    )
    entries: list[ExceptionEntry] = []
    identities: set[tuple[str, str, str]] = set()
    for raw in array(policy["exceptions"], "exceptions"):
        item = mapping(raw, "exception")
        require(set(item) == POLICY_FIELDS, "unknown or missing exception fields")
        values = {key: string(value, key) for key, value in item.items()}
        require(
            GHSA.fullmatch(values["advisory"]) is not None,
            "advisory must be an exact GHSA identifier",
        )
        require(
            values["ecosystem"] == "PyPI", "only PyPI lockfile exceptions are supported"
        )
        require(
            (values["package"], values["version"]) in locked,
            "exception package/version must match uv.lock exactly",
        )
        require(
            EXPIRY.fullmatch(values["expiresAt"]) is not None,
            "expiresAt must be an explicit UTC timestamp",
        )
        expiry = datetime.fromisoformat(values["expiresAt"])
        require(expiry > now, f"expired exception: {values['advisory']}")
        upstream = urlsplit(values["upstream"])
        require(
            upstream.scheme == "https"
            and bool(upstream.hostname)
            and not upstream.username
            and not upstream.password,
            "upstream must be a credential-free HTTPS reference",
        )
        identity = (values["advisory"], values["package"], values["version"])
        require(identity not in identities, "duplicate exception")
        identities.add(identity)
        entries.append(
            ExceptionEntry(
                values["advisory"],
                values["package"],
                values["version"],
                expiry,
                values["reason"],
                values["noFixReason"],
                values["upstream"],
            )
        )
    return tuple(entries)


def no_fixed_release(vulnerability: dict[str, object], name: str) -> None:
    """Require affected-package evidence and reject every published fixed event."""
    matched = False
    for raw in array(vulnerability.get("affected"), "affected"):
        affected = mapping(raw, "affected entry")
        package = mapping(affected.get("package"), "affected package")
        if package.get("ecosystem") != "PyPI" or package.get("name") != name:
            continue
        matched = True
        ranges = array(affected.get("ranges", []), "affected ranges")
        versions = array(affected.get("versions", []), "affected versions")
        require(bool(ranges or versions), "missing affected range/version evidence")
        for version in versions:
            string(version, "affected version")
        for raw_range in ranges:
            interval = mapping(raw_range, "affected range")
            require(
                interval.get("type") in {"ECOSYSTEM", "SEMVER"},
                "unsupported affected range type",
            )
            events = array(interval.get("events"), "range events")
            require(bool(events), "empty affected events")
            for raw_event in events:
                event = mapping(raw_event, "range event")
                require(
                    len(event) == 1
                    and set(event) <= {"introduced", "fixed", "last_affected", "limit"},
                    "malformed range event",
                )
                require(
                    "fixed" not in event,
                    "upstream now reports a fixed release; remove the exception and upgrade",
                )
                string(next(iter(event.values())), "range event version")
    require(matched, "missing affected-package evidence for exception")


def validate_report(
    report: object,
    entries: tuple[ExceptionEntry, ...],
    locked: set[tuple[str, str]],
    root: Path,
    scan_exit: int,
) -> list[str]:
    """Validate complete unsuppressed OSV JSON and return visible accepted risks."""
    data = mapping(report, "OSV report")
    require(
        set(data)
        <= {
            "results",
            "experimental_config",
            "experimental_generic_findings",
            "image_metadata",
            "license_summary",
        },
        "unknown OSV report fields",
    )
    require(
        not data.get("experimental_generic_findings"),
        "unsupported generic OSV findings",
    )
    results = array(data.get("results"), "OSV results")
    require(bool(results), "empty OSV results; --all-packages is required")
    observed: set[tuple[str, str]] = set()
    used: set[ExceptionEntry] = set()
    findings = 0
    for raw_source in results:
        source_result = mapping(raw_source, "source result")
        require(
            set(source_result) <= {"source", "packages", "experimental_pes"},
            "unknown source result fields",
        )
        source = mapping(source_result.get("source"), "source")
        path = string(source.get("path"), "source path")
        is_lock = source.get("type") == "lockfile" and path in {
            "uv.lock",
            str(root.resolve() / "uv.lock"),
            "/github/workspace/uv.lock",
            "/src/uv.lock",
        }
        require(
            not source_result.get("experimental_pes"),
            "unexpected prefiltered VEX evidence",
        )
        for raw_package in array(source_result.get("packages"), "packages"):
            package_result = mapping(raw_package, "package result")
            require(
                set(package_result)
                <= {
                    "package",
                    "dependency_groups",
                    "vulnerabilities",
                    "groups",
                    "licenses",
                    "license_violations",
                },
                "unknown package result fields",
            )
            package = mapping(package_result.get("package"), "package")
            vulnerabilities = array(
                package_result.get("vulnerabilities", []), "vulnerabilities"
            )
            vulnerability_ids = [
                string(mapping(item, "vulnerability").get("id"), "vulnerability id")
                for item in vulnerabilities
            ]
            group_ids: list[str] = []
            for raw_group in array(package_result.get("groups", []), "groups"):
                group = mapping(raw_group, "group")
                ids = array(group.get("ids"), "group ids")
                require(bool(ids), "empty advisory group")
                group_ids.extend(string(value, "group id") for value in ids)
            require(
                len(set(vulnerability_ids)) == len(vulnerability_ids)
                and sorted(group_ids) == sorted(vulnerability_ids),
                "inconsistent vulnerability groups",
            )
            require(
                not package_result.get("license_violations"),
                "license violations cannot be excepted",
            )
            require(
                not package.get("deprecated"), "deprecated packages cannot be excepted"
            )
            if is_lock and package.get("ecosystem") == "PyPI":
                observed.add(
                    (
                        string(package.get("name"), "package name"),
                        string(package.get("version"), "package version"),
                    )
                )
            for raw_vulnerability in vulnerabilities:
                findings += 1
                vulnerability = mapping(raw_vulnerability, "vulnerability")
                identifier = string(vulnerability.get("id"), "vulnerability id")
                aliases = {
                    string(value, "alias")
                    for value in array(vulnerability.get("aliases", []), "aliases")
                }
                matching = [
                    entry
                    for entry in entries
                    if entry.advisory in {identifier, *aliases}
                    and is_lock
                    and package.get("ecosystem") == "PyPI"
                    and package.get("name") == entry.package
                    and package.get("version") == entry.version
                ]
                require(
                    len(matching) == 1,
                    f"unaccepted vulnerability: {identifier} in {package.get('name')}@{package.get('version')} ({path})",
                )
                entry = matching[0]
                no_fixed_release(vulnerability, entry.package)
                used.add(entry)
    require(
        locked <= observed,
        f"incomplete OSV report; missing locked packages: {sorted(locked - observed)}",
    )
    require(
        type(scan_exit) is int and scan_exit in {0, 1}, "scanner operational failure"
    )
    require(scan_exit == int(findings > 0), "scanner exit/report mismatch")
    require(
        used == set(entries),
        "stale exception: remove entries not matched by the raw scan",
    )
    messages = [
        f"ACCEPTED RISK: {entry.advisory} / PyPI {entry.package}@{entry.version}; "
        f"expires {entry.expires_at.isoformat()}; {entry.reason}; "
        f"no-fix rationale: {entry.no_fix_reason}; upstream: {entry.upstream}"
        for entry in entries
    ]
    if not messages:
        messages.append(
            "No accepted risks; no known vulnerabilities in the complete OSV report."
        )
    return messages


def adjudicate(
    report: object,
    entries: tuple[ExceptionEntry, ...],
    locked: set[tuple[str, str]],
    root: Path,
    scan_exit: int,
) -> tuple[dict[str, object], list[str]]:
    """Return reporter input only after every raw finding passes exact validation."""
    messages = validate_report(report, entries, locked, root, scan_exit)
    result = mapping(json.loads(json.dumps(report)), "validated report")
    for raw_source in array(result["results"], "results"):
        for raw_package in array(mapping(raw_source, "source")["packages"], "packages"):
            package = mapping(raw_package, "package")
            # Validation has proven every removed finding was explicitly accepted.
            # This is only the reporter view; the full raw evidence is retained.
            package.pop("vulnerabilities", None)
            package.pop("groups", None)
    return result, messages


def main(argv: Sequence[str] | None = None) -> int:
    """Validate policy offline, or adjudicate one raw scan for native reporting."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--policy", type=Path, default=Path(".github/advisory-exceptions.json")
    )
    parser.add_argument("--lock", type=Path, default=Path("uv.lock"))
    parser.add_argument(
        "--scanner-config", type=Path, default=Path(".github/osv-scanner-empty.toml")
    )
    parser.add_argument("--report", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--summary", type=Path)
    parser.add_argument("--scan-exit", type=int)
    args = parser.parse_args(argv)
    try:
        require(
            tomllib.loads(args.scanner_config.read_text(encoding="utf-8")) == {},
            "raw scanner configuration must be empty",
        )
        locked = locked_packages(tomllib.loads(args.lock.read_text(encoding="utf-8")))
        entries = load_policy(read_json(args.policy), locked, datetime.now(UTC))
        if args.report is None:
            require(
                args.output is None and args.summary is None and args.scan_exit is None,
                "report is required to generate reporter input",
            )
            print(f"Advisory policy valid: {len(entries)} explicit exception(s).")
            return 0
        require(args.output is not None, "output is required with report")
        result, messages = adjudicate(
            read_json(args.report), entries, locked, args.lock.parent, args.scan_exit
        )
        args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        summary = "\n".join(messages) + "\n"
        print(summary, end="")
        if args.summary is not None:
            with args.summary.open("a", encoding="utf-8") as stream:
                stream.write(summary)
    except (PolicyError, ValueError, OSError) as error:
        print(f"Advisory policy failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
