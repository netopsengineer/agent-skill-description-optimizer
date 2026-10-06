"""Exercise both scanner adapters through the real shell entry point."""

import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
GHSA = "GHSA-2345-6789-cfgh"


def _fixtures(accepted: bool) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    entry = {
        "advisory": GHSA,
        "ecosystem": "PyPI",
        "package": "example",
        "version": "1.0",
        "expiresAt": (datetime.now(UTC) + timedelta(days=1)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "reason": "Synthetic test only",
        "noFixReason": "No patched release in synthetic report",
        "upstream": "https://example.test/advisory",
    }
    uv_finding: dict[str, Any] = {
        "dependency": {"name": "example", "version": "1.0"},
        "id": GHSA,
        "display_id": GHSA,
        "aliases": [],
        "summary": None,
        "description": None,
        "link": None,
        "fix_versions": [],
        "published": None,
        "modified": None,
    }
    osv_finding = {
        "id": GHSA,
        "aliases": [],
        "affected": [
            {
                "package": {"name": "example", "ecosystem": "PyPI"},
                "ranges": [{"type": "ECOSYSTEM", "events": [{"introduced": "0"}]}],
            }
        ],
    }
    uv_report: dict[str, Any] = {
        "schema": {"version": "preview"},
        "summary": {
            "audited_packages": 1,
            "vulnerabilities": int(accepted),
            "adverse_statuses": 0,
        },
        "vulnerabilities": [uv_finding] if accepted else [],
        "adverse_statuses": [],
    }
    package: dict[str, Any] = {
        "package": {"name": "example", "version": "1.0", "ecosystem": "PyPI"}
    }
    if accepted:
        package.update(vulnerabilities=[osv_finding], groups=[{"ids": [GHSA]}])
    osv_report = {
        "results": [
            {"source": {"type": "lockfile", "path": "uv.lock"}, "packages": [package]}
        ]
    }
    return (
        {"version": 1, "exceptions": [entry] if accepted else []},
        uv_report,
        osv_report,
    )


@pytest.mark.parametrize(
    "scenario",
    [
        "clean",
        "accepted",
        "uv-operational",
        "osv-operational",
        "uv-fixed",
        "osv-unrelated",
        "build-failure",
    ],
)
def test_end_to_end_scanner_policy(tmp_path: Path, scenario: str) -> None:
    work = tmp_path / "work"
    work.mkdir()
    shutil.copytree(
        ROOT / "scripts", work / "scripts", ignore=shutil.ignore_patterns("__pycache__")
    )
    shutil.copytree(
        ROOT / ".github/security-scanner", work / ".github/security-scanner"
    )
    shutil.copyfile(
        ROOT / ".github/osv-scanner-empty.toml", work / ".github/osv-scanner-empty.toml"
    )
    (work / "uv.lock").write_text(
        '[[package]]\nname="example"\nversion="1.0"\nsource={registry="https://pypi.org/simple"}\n',
        encoding="utf-8",
    )
    accepted = scenario != "clean"
    policy, uv_report, osv_report = _fixtures(accepted)
    if scenario == "uv-fixed":
        uv_report["vulnerabilities"][0]["fix_versions"] = ["1.1"]
    if scenario == "osv-unrelated":
        osv_report["results"][0]["packages"].append(
            {
                "package": {"name": "other", "version": "9.0", "ecosystem": "PyPI"},
                "vulnerabilities": [{"id": GHSA}],
                "groups": [{"ids": [GHSA]}],
            }
        )
    (work / ".github/advisory-exceptions.json").write_text(
        json.dumps(policy), encoding="utf-8"
    )
    for name, report in (("uv", uv_report), ("osv", osv_report)):
        (tmp_path / f"{name}.json").write_text(json.dumps(report), encoding="utf-8")
    binaries = tmp_path / "bin"
    binaries.mkdir()
    fake_uv = binaries / "uv"
    fake_uv.write_text(
        f"#!{sys.executable}\n"
        + """import os, sys
from pathlib import Path
if sys.argv[1] == "audit":
    assert "--no-config" in sys.argv and "--locked" in sys.argv
    print(Path(os.environ["UV_FIXTURE"]).read_text())
    raise SystemExit(int(os.environ["UV_EXIT"]))
os.execv(sys.executable, [sys.executable, *sys.argv[3:]])
""",
        encoding="utf-8",
    )
    fake_docker = binaries / "docker"
    fake_docker.write_text(
        f"#!{sys.executable}\n"
        + """import os, sys
from pathlib import Path
if sys.argv[1] == "build":
    raise SystemExit(int(os.environ["BUILD_EXIT"]))
assert "--recursive" in sys.argv and "--all-packages" in sys.argv
assert "--read-only" in sys.argv and "--cap-drop=ALL" in sys.argv
assert any(value.endswith(":/src:ro") for value in sys.argv)
assert "--config=/src/.github/osv-scanner-empty.toml" in sys.argv
print(Path(os.environ["OSV_FIXTURE"]).read_text())
raise SystemExit(int(os.environ["OSV_EXIT"]))
""",
        encoding="utf-8",
    )
    fake_uv.chmod(0o755)
    fake_docker.chmod(0o755)
    summary = tmp_path / "summary.md"
    env = {
        **os.environ,
        "PATH": f"{binaries}{os.pathsep}{os.environ['PATH']}",
        "RUNNER_TEMP": str(tmp_path),
        "GITHUB_STEP_SUMMARY": str(summary),
        "UV_FIXTURE": str(tmp_path / "uv.json"),
        "OSV_FIXTURE": str(tmp_path / "osv.json"),
        "UV_EXIT": "2" if scenario == "uv-operational" else str(int(accepted)),
        "OSV_EXIT": "127" if scenario == "osv-operational" else str(int(accepted)),
        "BUILD_EXIT": "1" if scenario == "build-failure" else "0",
    }
    result = subprocess.run(
        ["bash", "scripts/audit-dependencies.sh"],
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == (0 if scenario in {"clean", "accepted"} else 1), (
        result.stdout + result.stderr
    )
    output = tmp_path / "optimizer-dependency-audit"
    assert json.loads((output / "uv.json").read_text(encoding="utf-8")) == uv_report
    if scenario != "build-failure":
        assert (
            json.loads((output / "osv.json").read_text(encoding="utf-8")) == osv_report
        )
    if scenario == "accepted":
        assert summary.read_text(encoding="utf-8").count("ACCEPTED RISK") == 2
    if scenario == "clean":
        assert "ACCEPTED RISK" not in summary.read_text(encoding="utf-8")
