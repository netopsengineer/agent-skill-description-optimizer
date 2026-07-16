"""Integration tests for ``run_single_query`` via a fake ``claude`` binary.

Exercises the real subprocess spawn, ``select``/``os.read`` transport, JSON decoding,
and temp-dir lifecycle — only the CLI itself is faked. Verdicts must match the
fixtures' documented ground truth.
"""

import tempfile
from collections.abc import Callable
from pathlib import Path

import pytest

from skill_optimizer import run_query_with_retry, run_single_query

UseStream = Callable[..., None]


def _run(
    skill_name: str = "myskill",
    query: str = "extract the tables from report.pdf",
    model: str | None = "claude-haiku-4-5",
    settings_json: str | None = None,
) -> bool | None:
    return run_single_query(
        query=query,
        skill_name=skill_name,
        description="Extract tables from PDFs.",
        model=model,
        timeout=15,
        settings_json=settings_json,
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


def test_malicious_skill_name_cannot_escape_project_dir(
    fake_claude: UseStream, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A hostile SKILL.md ``name:`` is attacker-controlled. Unsanitized, the name
    # ``../../../ESCAPE`` from ``<tmp>/skilleval-*/.claude/commands`` would traverse up
    # and drop an ``ESCAPE-cand-*.md`` file directly in ``tmp_path`` (outside the
    # auto-cleaned project dir). The sanitizer must keep the write inside the project.
    fake_claude("real_no_trigger")
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    assert _run(skill_name="../../../ESCAPE") is False
    # No attacker-marked file survives anywhere under tmp_path: the sanitized command
    # file stays inside the auto-cleaned project dir, and an escaped file would land at
    # tmp_path's top level (outside cleanup). ``rglob`` covers the top level too.
    assert not list(tmp_path.rglob("*ESCAPE*"))


def test_default_model_and_settings_are_passed(
    fake_claude: UseStream, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # model=None (CLI default -> no ``--model``) and a settings blob exercise the two
    # otherwise-unhit cmd-construction branches; the fake replays a decisive stream.
    fake_claude("real_no_trigger")
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    assert _run(model=None, settings_json='{"permissions": {"allow": []}}') is False


def test_run_query_with_retry_returns_none_when_all_unjudgeable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Every attempt is unjudgeable (None): the retry loop exhausts its budget and
    # returns None rather than scoring a transient failure as a decisive non-trigger.
    calls = {"n": 0}

    def always_none(*_: object, **__: object) -> None:
        calls["n"] += 1
        return None

    monkeypatch.setattr("skill_optimizer.evaluation.run_single_query", always_none)
    assert run_query_with_retry("q", "s", "d", "m", 5, None, retries=2) is None
    assert calls["n"] == 3  # initial attempt + 2 retries
