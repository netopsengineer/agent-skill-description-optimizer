"""Execute the trusted workflow's exact guard without credentials or GitHub writes."""

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

WORKFLOW = Path(__file__).resolve().parent.parent / ".github/workflows/auto-fix.yml"


def _run_guard(
    tmp_path: Path, path: str, status: str = "M", mode: str = "100644"
) -> subprocess.CompletedProcess[str]:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    start = workflow.index("          # BEGIN AUTOFIX SCOPE GUARD")
    end = workflow.index("          # END AUTOFIX SCOPE GUARD")
    script = textwrap.dedent(workflow[start:end])
    tracked = [
        "uv.lock",
        "src/skill_optimizer/cli.py",
        "tests/test_cli.py",
        "README.md",
        ".pre-commit-config.yaml",
        ".github/advisory-exceptions.json",
        ".github/osv-scanner-empty.toml",
        ".github/security-scanner/Dockerfile",
        ".github/workflows/validate.yml",
        ".github/workflows/auto-fix.yml",
        "scripts/advisory_policy.py",
        "scripts/__init__.py",
        "scripts/uv_advisory_policy.py",
        "scripts/audit-dependencies.sh",
        "pyproject.toml",
    ]
    (tmp_path / "trusted-tree.json").write_text(
        json.dumps(
            {
                "truncated": False,
                "tree": [
                    {"type": "blob", "mode": "100644", "path": name} for name in tracked
                ],
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "fixes").mkdir()
    (tmp_path / "fixes/fixes.json").write_text(
        json.dumps(
            [{"path": path, "status": status, "mode": mode, "content": "dGVzdA=="}]
        ),
        encoding="utf-8",
    )
    return subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "GITHUB_OUTPUT": str(tmp_path / "output")},
    )


@pytest.mark.parametrize(
    "path",
    [
        "uv.lock",
        "src/skill_optimizer/cli.py",
        "tests/test_cli.py",
        "README.md",
        ".pre-commit-config.yaml",
    ],
)
def test_ordinary_existing_mechanical_repairs_remain_allowed(
    tmp_path: Path, path: str
) -> None:
    result = _run_guard(tmp_path, path)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "output").read_text(encoding="utf-8") == "safe=true\n"


@pytest.mark.parametrize(
    "path",
    [
        ".github/advisory-exceptions.json",
        ".github/osv-scanner-empty.toml",
        ".github/security-scanner/Dockerfile",
        ".github/workflows/validate.yml",
        ".github/workflows/auto-fix.yml",
        "scripts/advisory_policy.py",
        "scripts/__init__.py",
        "scripts/uv_advisory_policy.py",
        "scripts/audit-dependencies.sh",
        "pyproject.toml",
        "scripts//advisory_policy.py",
        "./scripts/advisory_policy.py",
        "src/../scripts/advisory_policy.py",
        "/scripts/advisory_policy.py",
        "scripts\\advisory_policy.py",
        "",
        "sitecustomize.py",
        "json.py",
        ".venv/lib/python3.14/site-packages/sitecustomize.py",
    ],
)
def test_protected_and_noncanonical_paths_never_gain_write_token(
    tmp_path: Path, path: str
) -> None:
    result = _run_guard(tmp_path, path)
    assert result.returncode != 0
    assert not (tmp_path / "output").exists()


@pytest.mark.parametrize(
    "status,mode", [("A", "100644"), ("D", "000000"), ("T", "120000"), ("M", "120000")]
)
def test_new_files_deletions_and_symlinks_require_review(
    tmp_path: Path, status: str, mode: str
) -> None:
    assert _run_guard(tmp_path, "sitecustomize.py", status, mode).returncode != 0
    assert not (tmp_path / "output").exists()


def test_write_steps_require_successful_scope_guard() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    for step in (
        "Mint a GitHub App token",
        "Rebuild the fix commit via the Git Data API and update the branch",
    ):
        assert f"- name: {step}\n" in workflow
        section = workflow.split(f"- name: {step}\n", 1)[1].split("      - name:", 1)[0]
        assert "if: steps.scope.outputs.safe == 'true'" in section
    assert workflow.index("- name: Read the trusted PR file tree") < workflow.index(
        "- name: Validate mechanical fix scope"
    )
    assert workflow.index("- name: Validate mechanical fix scope") < workflow.index(
        "- name: Mint a GitHub App token"
    )


def test_both_dependency_scans_are_isolated_from_test_and_hook_execution() -> None:
    root = WORKFLOW.parent
    for filename in ("validate.yml", "security-scan.yml"):
        workflow = (root / filename).read_text(encoding="utf-8")
        dependency_job = workflow.split("  deps-security:\n", 1)[1].split(
            "\n  # Conventional-Commit", 1
        )[0]
        assert "bash scripts/audit-dependencies.sh" in dependency_job
        assert "persist-credentials: false" in dependency_job
        assert "prek run" not in dependency_job
        assert "uv run pytest" not in dependency_job
    weekly = (root / "security-scan.yml").read_text(encoding="utf-8")
    full_gate = weekly.split("  full-gate:\n", 1)[1].split("  deps-security:\n", 1)[0]
    assert "prek run" in full_gate
    assert "audit-dependencies.sh" not in full_gate
