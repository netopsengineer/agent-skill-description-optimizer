"""Shared fixtures for integration tests.

Provides a fake ``claude`` executable so ``run_single_query`` can exercise its real
subprocess/select/decode transport against recorded streams without invoking the
live CLI. The fake discovers the injected candidate command from its working
directory (exactly where real ``claude`` would see the slash-command) and rewrites
the fixture's placeholder name to it, so the randomized per-run command name matches.
"""

import sys
from collections.abc import Callable
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
_PLACEHOLDER = "pdfextract-cand-fix01"

_FAKE_SRC = f"""#!{sys.executable}
import glob, os, sys

cmds = sorted(glob.glob(".claude/commands/*.md"))
cmd_name = os.path.basename(cmds[0])[:-3] if cmds else "NO_CMD"
stream = os.environ.get("FAKE_CLAUDE_STREAM", "")
text = open(stream).read() if stream and os.path.exists(stream) else ""
sys.stdout.write(text.replace({_PLACEHOLDER!r}, cmd_name))
sys.stdout.flush()
sys.exit(int(os.environ.get("FAKE_CLAUDE_EXIT", "0")))
"""


def _noop_open(*_: object, **__: object) -> bool:
    """Stand in for ``webbrowser.open`` in tests (never launches a browser).

    Returns:
        Always ``False``.
    """
    return False


@pytest.fixture(autouse=True)
def _no_browser(  # pyright: ignore[reportUnusedFunction]
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Neutralize ``webbrowser.open`` so the suite never launches a real browser.

    The CLI defaults to ``--report auto``, which opens the HTML report; this autouse
    fixture makes that a no-op for every test.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
    """
    monkeypatch.setattr(
        "skill_optimizer.cli.webbrowser.open", _noop_open, raising=False
    )


@pytest.fixture(autouse=True)
def _claude_available(  # pyright: ignore[reportUnusedFunction]
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Make the CLI's ``claude``-availability preflight pass for every test.

    ``run``/``main`` verify the ``claude`` CLI is invocable before evaluating; the suite
    stubs ``evaluate``/``call_improver`` and never needs a real binary, so neutralize the
    check by default (mirroring ``_no_browser``) to keep the suite hermetic regardless of
    whether ``claude`` is on the runner's ``PATH``. Tests that exercise the real check
    re-patch this symbol (or call :func:`skill_optimizer._process.claude_available`).

    Args:
        monkeypatch: Pytest monkeypatch fixture.
    """
    monkeypatch.setattr("skill_optimizer.cli.claude_available", lambda: True)


@pytest.fixture
def fake_claude(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    """Install a fake ``claude`` and return a selector for which stream it replays.

    Args:
        tmp_path: Per-test temporary directory.
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        A function ``use(fixture_name, exit_code=0)`` that points the fake at
        ``tests/fixtures/<fixture_name>.jsonl`` (pass ``None`` for empty output) and
        sets the fake's process exit code (non-zero exercises the unjudgeable path).
    """
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    script = bindir / "claude"
    script.write_text(_FAKE_SRC)
    script.chmod(0o755)
    monkeypatch.setenv("SKILL_OPTIMIZER_CLAUDE_BIN", str(script))

    def use(fixture_name: str | None, exit_code: int = 0) -> None:
        if fixture_name is None:
            monkeypatch.delenv("FAKE_CLAUDE_STREAM", raising=False)
        else:
            monkeypatch.setenv(
                "FAKE_CLAUDE_STREAM", str(FIXTURES / f"{fixture_name}.jsonl")
            )
        monkeypatch.setenv("FAKE_CLAUDE_EXIT", str(exit_code))

    return use
