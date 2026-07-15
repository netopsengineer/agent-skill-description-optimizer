"""Tests for the ``optimize_description_v2.py`` pre-import version guard.

The lower-version behavior is proven portably (compile + exec with a simulated old
``sys.version_info`` and an import trap), so it does not depend on the test interpreter
actually being old. The successful-import branch and the external-CWD help proof run on
the current (3.14+) interpreter.
"""

import builtins
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENTRYPOINT = PROJECT_ROOT / "optimize_description_v2.py"

# The exact one-line requirement the guard must print to stderr (plus a newline).
_GUARD_LINE = (
    "Requires Python >=3.14; use uv run --project "
    "/ABSOLUTE/PATH/TO/agent-skill-description-optimizer "
    "optimize-skill-description."
)


def test_guard_exits_1_below_314_without_importing_package(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code = compile(ENTRYPOINT.read_text(), str(ENTRYPOINT), "exec")
    monkeypatch.setattr(sys, "version_info", (3, 9, 6))
    real_import = builtins.__import__
    imported: list[str] = []

    def recording_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "skill_optimizer" or name.startswith("skill_optimizer."):
            imported.append(name)
            raise AssertionError(f"skill_optimizer was imported: {name}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", recording_import)
    module_globals: dict[str, Any] = {
        "__name__": "entrypoint_under_test",
        "__file__": str(ENTRYPOINT),
    }
    with pytest.raises(SystemExit) as excinfo:
        exec(code, module_globals)  # noqa: S102 - compiled entrypoint under test
    assert excinfo.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""  # empty stdout
    assert captured.err == _GUARD_LINE + "\n"  # exact single stderr line
    assert not imported  # no skill_optimizer import was attempted


def test_import_succeeds_on_current_interpreter() -> None:
    # The 3.14+ branch: importing the shim exposes the unchanged public surface.
    import optimize_description_v2 as entrypoint

    assert callable(entrypoint.main)
    assert "main" in entrypoint.__all__
    assert "best_description" not in entrypoint.__all__  # sanity: it is a data key


def test_external_cwd_help_succeeds(tmp_path: Path) -> None:
    # Project-targeted uv from an unrelated CWD: the canonical cross-repo invocation.
    result = subprocess.run(
        [
            "uv",
            "run",
            "--project",
            str(PROJECT_ROOT),
            "optimize-skill-description",
            "--help",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "--eval-set" in result.stdout
    assert "--skill-path" in result.stdout
