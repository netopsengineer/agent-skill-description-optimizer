"""Shared helpers for invoking the ``claude`` CLI as a subprocess."""

import os
import shutil

# Env var to override the CLI binary, primarily so tests can point at a fake
# executable. Defaults to ``claude`` on PATH.
_CLAUDE_BIN_ENV = "SKILL_OPTIMIZER_CLAUDE_BIN"


def claude_bin() -> str:
    """Return the ``claude`` executable to invoke.

    Returns:
        The path/name from ``$SKILL_OPTIMIZER_CLAUDE_BIN``, or ``"claude"``.
    """
    return os.environ.get(_CLAUDE_BIN_ENV, "claude")


def claude_available() -> bool:
    """Report whether the resolved ``claude`` binary is present and executable.

    Uses :func:`shutil.which`, which resolves a bare name against ``PATH`` and checks a
    path with a directory separator directly — matching how the subprocess spawn locates
    the binary. Detects only that the executable is invocable, not whether it is logged
    in (that needs a live call).

    Returns:
        ``True`` when :func:`claude_bin` resolves to an existing executable file, else
        ``False``.
    """
    return shutil.which(claude_bin()) is not None


def subprocess_env() -> dict[str, str]:
    """Build the child-process environment.

    Returns:
        A copy of the current environment with ``CLAUDECODE`` removed so a nested
        ``claude -p`` does not detect that it is running inside Claude Code.
    """
    return {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
