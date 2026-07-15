"""Integration tests for ``run_single_query`` via a fake ``claude`` binary.

Exercises the real subprocess spawn, ``select``/``os.read`` transport, JSON decoding,
and temp-dir lifecycle — only the CLI itself is faked. Verdicts must match the
fixtures' documented ground truth.
"""

import tempfile
from collections.abc import Callable
from pathlib import Path

import pytest

from skill_optimizer import run_single_query

UseStream = Callable[..., None]


def _run() -> bool | None:
    return run_single_query(
        query="extract the tables from report.pdf",
        skill_name="myskill",
        description="Extract tables from PDFs.",
        model="claude-haiku-4-5",
        timeout=15,
        settings_json=None,
    )


def test_real_no_trigger_is_false(fake_claude: UseStream) -> None:
    fake_claude("real_no_trigger")
    assert _run() is False


def test_real_trigger_is_false_bash_first(fake_claude: UseStream) -> None:
    fake_claude("real_trigger")
    assert _run() is False


def test_skill_first_trigger_is_true(fake_claude: UseStream) -> None:
    # The fake rewrites the fixture's placeholder to the real randomized command
    # name (read from the injected slash-command), so detection matches end to end.
    fake_claude("skill_first_trigger")
    assert _run() is True


def test_empty_output_is_none(fake_claude: UseStream) -> None:
    # An empty/incomplete stream with no decisive terminal event is unjudgeable,
    # not a genuine non-trigger -> None (so it's excluded from scoring, not a miss).
    fake_claude(None)
    assert _run() is None


def test_nonzero_exit_is_none(fake_claude: UseStream) -> None:
    # A non-zero `claude -p` exit with no decisive stream event is unjudgeable -> None.
    fake_claude(None, exit_code=1)
    assert _run() is None


def test_tempdir_is_cleaned_up(
    fake_claude: UseStream, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake_claude("skill_first_trigger")
    # Force the throwaway project under tmp_path so we can assert it's removed.
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    assert _run() is True
    assert not list(tmp_path.glob("skilleval-*"))
