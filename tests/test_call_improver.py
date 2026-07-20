"""Tests for ``call_improver`` output parsing, with ``subprocess.run`` mocked."""

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from skill_optimizer import call_improver
from skill_optimizer.improver import (
    _DECODER,  # pyright: ignore[reportPrivateUsage]
    ImproverFatalProcessError,
    ImproverRetryableError,
    _LaunchBudget,  # pyright: ignore[reportPrivateUsage]
    _parse_improver_output,  # pyright: ignore[reportPrivateUsage]
    _parse_or_retryable,  # pyright: ignore[reportPrivateUsage]
    _run_improver_subprocess,  # pyright: ignore[reportPrivateUsage]
)

_NO_JSON = "Improver returned no JSON"
_MISSING = "Improver JSON missing 'description'"
_INVALID = "Improver returned invalid JSON"
_AMBIGUOUS = "Improver returned ambiguous JSON: multiple usable description objects"

# Stub ``fake_run``/``fake_improver`` callables below use ``*_: Any, **__: Any`` to match
# the real callee's call shape while ignoring the arguments they don't assert on. Bare
# ``_`` can't be repeated in one signature, so the keyword catch-all is ``__``.


def _stub_run(
    monkeypatch: pytest.MonkeyPatch,
    stdout: str,
    recorder: dict[str, Any] | None = None,
    returncode: int = 0,
) -> None:
    def fake_run(cmd: list[str], **kwargs: Any) -> SimpleNamespace:
        if recorder is not None:
            recorder["cmd"] = cmd
            recorder["kwargs"] = kwargs
        return SimpleNamespace(stdout=stdout, returncode=returncode, stderr="")

    monkeypatch.setattr("skill_optimizer.improver.subprocess.run", fake_run)


class TestOutputParsing:
    def test_clean_json(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _stub_run(monkeypatch, '{"description": "new desc", "rationale": "r"}')
        data = call_improver("prompt", "model", "high", 10)
        assert data["description"] == "new desc"
        assert data["rationale"] == "r"

    def test_strips_json_code_fence(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _stub_run(monkeypatch, '```json\n{"description": "d", "rationale": "r"}\n```')
        assert call_improver("p", "m", None, 10)["description"] == "d"

    def test_extracts_json_from_surrounding_prose(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_run(monkeypatch, 'Sure, here:\n{"description": "d"}\nHope that helps!')
        assert call_improver("p", "m", None, 10)["description"] == "d"


class TestErrorPaths:
    def test_no_json_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _stub_run(monkeypatch, "there is no json here")
        with pytest.raises(ValueError, match="no JSON"):
            call_improver("p", "m", None, 10)

    def test_missing_description_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _stub_run(monkeypatch, '{"rationale": "r"}')
        with pytest.raises(ValueError, match="missing 'description'"):
            call_improver("p", "m", None, 10)

    def test_blank_description_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _stub_run(monkeypatch, '{"description": "   "}')
        with pytest.raises(ValueError, match="missing 'description'"):
            call_improver("p", "m", None, 10)


class TestLengthLimit:
    def test_shortens_overlong_description(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        long_desc = "x" * (1024 + 50)
        outputs = [
            f'{{"description": "{long_desc}", "rationale": "first"}}',
            '{"description": "short and tidy", "rationale": "trimmed"}',
        ]
        calls: list[tuple[list[str], dict[str, Any]]] = []

        def fake_run(cmd: list[str], **kwargs: Any) -> SimpleNamespace:
            calls.append((cmd, kwargs))
            return SimpleNamespace(
                stdout=outputs[len(calls) - 1], returncode=0, stderr=""
            )

        monkeypatch.setattr("skill_optimizer.improver.subprocess.run", fake_run)
        data = call_improver("prompt", "m", "high", 10)
        assert data["description"] == "short and tidy"
        assert len(calls) == 2  # original + one shorten retry
        # The prompt now travels over stdin, so the retry message is in `input`.
        assert "prompt" not in calls[0][0]
        assert "over the 1024-character hard limit" in calls[1][1]["input"]

    def test_custom_max_chars_retry_message(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        outputs = [
            f'{{"description": "{"x" * 600}"}}',
            '{"description": "short"}',
        ]
        calls: list[tuple[list[str], dict[str, Any]]] = []

        def fake_run(cmd: list[str], **kwargs: Any) -> SimpleNamespace:
            calls.append((cmd, kwargs))
            return SimpleNamespace(
                stdout=outputs[len(calls) - 1], returncode=0, stderr=""
            )

        monkeypatch.setattr("skill_optimizer.improver.subprocess.run", fake_run)
        data = call_improver("p", "m", None, 10, max_chars=500)
        assert data["description"] == "short"
        assert "over the 500-character hard limit" in calls[1][1]["input"]

    def test_retry_still_overlong_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        still_long = "y" * (1024 + 10)

        def fake_run(cmd: list[str], **_: Any) -> SimpleNamespace:
            return SimpleNamespace(
                stdout=f'{{"description": "{still_long}"}}', returncode=0, stderr=""
            )

        monkeypatch.setattr("skill_optimizer.improver.subprocess.run", fake_run)
        with pytest.raises(
            ImproverRetryableError,
            match="exceeded the configured character limit after shortening",
        ):
            call_improver("prompt", "m", None, 10)

    def test_no_retry_when_within_limit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[list[str]] = []

        def fake_run(cmd: list[str], **_: Any) -> SimpleNamespace:
            calls.append(cmd)
            return SimpleNamespace(
                stdout='{"description": "fits fine"}', returncode=0, stderr=""
            )

        monkeypatch.setattr("skill_optimizer.improver.subprocess.run", fake_run)
        assert call_improver("prompt", "m", None, 10)["description"] == "fits fine"
        assert len(calls) == 1


class TestReturncode:
    def test_nonzero_returncode_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_run(*_: Any, **__: Any) -> SimpleNamespace:
            return SimpleNamespace(stdout="", returncode=1, stderr="auth error")

        monkeypatch.setattr("skill_optimizer.improver.subprocess.run", fake_run)
        with pytest.raises(
            ImproverFatalProcessError, match="Improver process exited with status 1"
        ):
            call_improver("p", "m", None, 10)


class TestCommandConstruction:
    def test_effort_included_when_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rec: dict[str, Any] = {}
        _stub_run(monkeypatch, '{"description": "d"}', rec)
        call_improver("p", "mymodel", "high", 42)
        cmd: list[str] = rec["cmd"]
        assert "--effort" in cmd
        assert cmd[cmd.index("--effort") + 1] == "high"
        assert "mymodel" in cmd
        assert rec["kwargs"]["timeout"] == 42

    def test_effort_omitted_when_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rec: dict[str, Any] = {}
        _stub_run(monkeypatch, '{"description": "d"}', rec)
        call_improver("p", "mymodel", None, 10)
        assert "--effort" not in rec["cmd"]

    def test_prompt_sent_over_stdin(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rec: dict[str, Any] = {}
        _stub_run(monkeypatch, '{"description": "d"}', rec)
        call_improver("MY_PROMPT", "m", None, 10)
        assert "MY_PROMPT" not in rec["cmd"]  # not on argv
        assert rec["kwargs"]["input"] == "MY_PROMPT"  # on stdin


class TestTranscript:
    def test_transcript_written_when_log_path_set(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _stub_run(monkeypatch, '{"description": "new desc", "rationale": "r"}')
        log = tmp_path / "improve.json"
        call_improver("p", "m", "high", 10, log_path=log)
        transcript = json.loads(log.read_text())
        assert transcript["response"] == '{"description": "new desc", "rationale": "r"}'
        assert transcript["parsed_description"] == "new desc"
        assert transcript["char_count"] == len("new desc")
        assert transcript["over_limit"] is False

    def test_no_transcript_when_log_path_none(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _stub_run(monkeypatch, '{"description": "d"}')
        call_improver("p", "m", None, 10)
        assert not list(tmp_path.iterdir())  # nothing written


class TestFailureTranscript:
    def test_nonzero_exit_still_writes_transcript(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # A first-call non-zero exit must still leave a diagnostic transcript behind.
        _stub_run(monkeypatch, "", returncode=1)
        log = tmp_path / "improve.json"
        with pytest.raises(ImproverFatalProcessError, match="status 1"):
            call_improver("PROMPT", "m", None, 10, log_path=log)
        assert log.exists()
        transcript = json.loads(log.read_text())
        assert transcript["prompt"] == "PROMPT"
        assert "error" in transcript

    def test_unparseable_first_response_still_writes_transcript(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # A malformed first response must preserve the raw stdout for diagnosis.
        _stub_run(monkeypatch, "no json here")
        log = tmp_path / "improve.json"
        with pytest.raises(ValueError, match="no JSON"):
            call_improver("PROMPT", "m", None, 10, log_path=log)
        assert log.exists()
        transcript = json.loads(log.read_text())
        assert transcript["prompt"] == "PROMPT"
        assert transcript["response"] == "no json here"
        assert "error" in transcript

    def test_malformed_shorten_retry_still_writes_transcript(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        responses = [
            SimpleNamespace(
                stdout='{"description": "xxxxxx"}', returncode=0, stderr=""
            ),
            SimpleNamespace(stdout="not json", returncode=0, stderr=""),
        ]

        def fake_run(*_: Any, **__: Any) -> SimpleNamespace:
            return responses.pop(0)

        monkeypatch.setattr("skill_optimizer.improver.subprocess.run", fake_run)
        log = tmp_path / "improve.json"
        with pytest.raises(ValueError, match="no JSON"):
            call_improver("PROMPT", "m", None, 10, max_chars=5, log_path=log)

        transcript = json.loads(log.read_text())
        assert "over the 5-character hard limit" in transcript["rewrite_prompt"]
        assert transcript["rewrite_response"] == "not json"
        assert "error" in transcript

    def test_nonzero_shorten_retry_still_writes_transcript(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        responses = [
            SimpleNamespace(
                stdout='{"description": "xxxxxx"}', returncode=0, stderr=""
            ),
            SimpleNamespace(stdout="retry stdout", returncode=2, stderr="retry failed"),
        ]

        def fake_run(*_: Any, **__: Any) -> SimpleNamespace:
            return responses.pop(0)

        monkeypatch.setattr("skill_optimizer.improver.subprocess.run", fake_run)
        log = tmp_path / "improve.json"
        with pytest.raises(ImproverFatalProcessError, match="status 2"):
            call_improver("PROMPT", "m", None, 10, max_chars=5, log_path=log)

        transcript = json.loads(log.read_text())
        assert "over the 5-character hard limit" in transcript["rewrite_prompt"]
        assert transcript["rewrite_response"] == "retry stdout"
        assert "error" in transcript


# --------------------------------------------------------------------------- #
# S1: whole-response parse first, then a string/container-aware recovery scan.
# _parse_improver_output raises stable ValueError messages (no raw text).
# --------------------------------------------------------------------------- #
class TestParserReturns:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            # clean whole-response object
            ('{"description": "d", "rationale": "r"}', "d"),
            # json code fence
            ('```json\n{"description": "d"}\n```', "d"),
            # bare code fence (no language tag)
            ('```\n{"description": "d"}\n```', "d"),
            # surrounding prose (whole-parse fails -> recovery scan)
            ('Sure:\n{"description": "d"}\nThanks!', "d"),
            # prose that itself contains balanced braces before the real object
            ('Use {braces} wisely. {"description": "d"}', "d"),
            # a balanced-but-malformed span before the real object is skipped
            ('{not: valid, json} {"description": "d"}', "d"),
            # a complete description-less object before the real object is skipped
            ('{"other": 1} {"description": "d"}', "d"),
            # nested objects in a whole-response object are not separately mined
            ('{"description": "d", "meta": {"a": {"b": 1}}}', "d"),
            # delimiters inside strings do not confuse the brace/bracket stack
            ('{"description": "a } b ] c { d ["}', "a } b ] c { d ["),
            # escaped quotes inside the string stay inside it
            ('{"description": "she said \\"hi\\""}', 'she said "hi"'),
        ],
    )
    def test_returns_expected_description(self, raw: str, expected: str) -> None:
        assert _parse_improver_output(raw)["description"] == expected

    def test_even_backslash_run_closes_the_string(self) -> None:
        # Two backslashes (even) before the quote -> the quote closes the string, the
        # object balances, and it decodes to a description of "x" + one backslash.
        raw = '{"description": "x\\\\"}'  # -> {"description": "x\\"}
        assert _parse_improver_output(raw)["description"] == "x\\"


class TestParserErrors:
    @pytest.mark.parametrize(
        ("raw", "message"),
        [
            # no container at all
            ("there is no json here", _NO_JSON),
            ("", _NO_JSON),
            # whole-response object without a usable description
            ('{"rationale": "r"}', _MISSING),
            ('{"description": "   "}', _MISSING),
            # a lone complete description-less span in prose
            ('prose {"other": 1} more prose', _MISSING),
            # scalar whole roots are rejected without scanning children
            ("42", _INVALID),
            ('"hello"', _INVALID),
            ("true", _INVALID),
            ("null", _INVALID),
            # list whole root, and an array is never mined for a nested object
            ("[1, 2, 3]", _INVALID),
            ('[{"description": "d"}]', _INVALID),
            ('prefix [{"description": "d"}] suffix', _INVALID),
            # a lone balanced-but-malformed span
            ("{not: valid json}", _INVALID),
            # two usable objects -> ambiguous
            ('{"description": "a"} {"description": "b"}', _AMBIGUOUS),
            # mismatched closers
            ("{]", _INVALID),
            ("[}", _INVALID),
            # a mismatch after a usable object still overrides it (terminal)
            ('{"description": "d"} {]', _INVALID),
            # unmatched object opener before / after a usable object (terminal)
            ('{ oops {"description": "d"}', _INVALID),
            ('{"description": "d"} {oops', _INVALID),
            # unmatched array opener before / after a usable object (terminal)
            ('[1, 2 {"description": "d"}', _INVALID),
            ('{"description": "d"} [1, 2', _INVALID),
            # unterminated string inside a container (the closer is swallowed),
            # both on its own and after an otherwise-usable object (terminal)
            ('{"description": "d}', _INVALID),
            ('{"description": "d"} {"more": "unterminated}', _INVALID),
        ],
    )
    def test_raises_expected_message(self, raw: str, message: str) -> None:
        with pytest.raises(ValueError) as excinfo:
            _parse_improver_output(raw)
        assert str(excinfo.value) == message

    def test_odd_backslash_run_leaves_string_open(self) -> None:
        # One backslash (odd) escapes the closing quote, so the string never terminates
        # and the trailing brace is swallowed -> a terminal invalid-JSON structure.
        raw = '{"description": "x\\"}'  # -> {"description": "x\"}
        with pytest.raises(ValueError) as excinfo:
            _parse_improver_output(raw)
        assert str(excinfo.value) == _INVALID


class TestParserRecursionSafety:
    """Pathologically nested ``claude`` stdout must map to the stable invalid-JSON
    ValueError, never escape as an uncaught RecursionError (which would abort the run
    with a traceback and no result envelope).

    A deeply nested JSON document overflows the decoder's C-stack recursion guard,
    surfacing as ``RecursionError`` (independent of ``sys.setrecursionlimit`` on 3.14).
    That the decoder overflows at *some* depth is CPython's behavior, not this parser's;
    what these tests own is that when ``RecursionError`` is raised at a decode boundary
    it is caught and mapped to the stable failure. So they inject ``RecursionError`` at
    each boundary directly (``json.loads`` for the whole-response path, ``_DECODER``'s
    ``raw_decode`` for the recovery scanner) -- deterministic and portable, unlike really
    overflowing a small thread stack, which segfaults on some libc builds and fails to
    overflow at all where the minimum thread stack is clamped larger.
    """

    @staticmethod
    def _raise_recursion(*_: Any, **__: Any) -> Any:
        raise RecursionError("maximum recursion depth exceeded")

    def test_deeply_nested_whole_response_is_invalid_json(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Whole-response parse: json.loads overflows -> mapped to invalid JSON.
        monkeypatch.setattr(json, "loads", self._raise_recursion)
        with pytest.raises(ValueError) as excinfo:
            _parse_improver_output('{"description": "d"}')
        assert str(excinfo.value) == _INVALID

    def test_deeply_nested_object_in_prose_is_invalid_json(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Leading prose forces the recovery scanner; its per-span raw_decode overflows
        # and must be treated as a malformed span (terminal invalid JSON), not a crash.
        monkeypatch.setattr(_DECODER, "raw_decode", self._raise_recursion)
        with pytest.raises(ValueError) as excinfo:
            _parse_improver_output('here you go: {"description": "d"}')
        assert str(excinfo.value) == _INVALID

    def test_deeply_nested_stdout_is_retryable_not_fatal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # End to end: a hostile deeply nested improver response is classified as a
        # retryable ``invalid_output`` rather than crashing call_improver.
        _stub_run(monkeypatch, '{"description": "d"}')
        monkeypatch.setattr(json, "loads", self._raise_recursion)
        with pytest.raises(ImproverRetryableError) as excinfo:
            call_improver("p", "m", None, 10)
        assert excinfo.value.kind == "invalid_output"
        assert excinfo.value.message == _INVALID


class TestParseOrRetryable:
    """``_parse_or_retryable`` wraps only allowlisted parser messages as retryable."""

    def test_typed_retryable_error_passes_through(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A parser that itself raises ImproverRetryableError is re-raised unchanged
        # (not re-wrapped), preserving its kind/message.
        def boom(_raw: str) -> dict[str, Any]:
            raise ImproverRetryableError("invalid_output", _INVALID)

        monkeypatch.setattr("skill_optimizer.improver._parse_improver_output", boom)
        with pytest.raises(ImproverRetryableError) as excinfo:
            _parse_or_retryable("x")
        assert excinfo.value.kind == "invalid_output"

    def test_unexpected_valueerror_propagates_fatally(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A non-allowlisted ValueError (a programming/config fault) is NOT reclassified
        # as retryable -- it propagates unchanged so real bugs surface fatally.
        def boom(_raw: str) -> dict[str, Any]:
            raise ValueError("some unexpected internal fault")

        monkeypatch.setattr("skill_optimizer.improver._parse_improver_output", boom)
        with pytest.raises(ValueError, match="some unexpected internal fault"):
            _parse_or_retryable("x")


# --------------------------------------------------------------------------- #
# M3: typed failure taxonomy, launch budget, transcript diagnostics.
# --------------------------------------------------------------------------- #
class TestRetryableErrorConstructor:
    def test_valid_kind_and_message(self) -> None:
        err = ImproverRetryableError("timeout", "Improver timed out")
        assert err.kind == "timeout"
        assert err.message == "Improver timed out"
        assert isinstance(err, ValueError)

    def test_unknown_kind_rejected(self) -> None:
        with pytest.raises(ValueError, match="invalid retryable error"):
            ImproverRetryableError("boom", "Improver timed out")

    def test_message_outside_kind_allowlist_rejected(self) -> None:
        with pytest.raises(ValueError, match="invalid retryable error"):
            ImproverRetryableError("timeout", "Improver returned no JSON")


class TestFailureTaxonomy:
    def test_timeout_becomes_retryable_timeout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_run(*_: Any, **__: Any) -> SimpleNamespace:
            raise subprocess.TimeoutExpired(cmd="claude", timeout=1)

        monkeypatch.setattr("skill_optimizer.improver.subprocess.run", fake_run)
        with pytest.raises(ImproverRetryableError) as excinfo:
            call_improver("p", "m", None, 10)
        assert excinfo.value.kind == "timeout"
        assert excinfo.value.message == "Improver timed out"

    @pytest.mark.parametrize(
        ("stdout", "message"),
        [
            ("no json at all", "Improver returned no JSON"),
            ('{"rationale": "r"}', "Improver JSON missing 'description'"),
            ("[1, 2, 3]", "Improver returned invalid JSON"),
            (
                '{"description": "a"} {"description": "b"}',
                "Improver returned ambiguous JSON: multiple usable description objects",
            ),
        ],
    )
    def test_parser_failures_become_retryable_invalid_output(
        self, monkeypatch: pytest.MonkeyPatch, stdout: str, message: str
    ) -> None:
        _stub_run(monkeypatch, stdout)
        with pytest.raises(ImproverRetryableError) as excinfo:
            call_improver("p", "m", None, 10)
        assert excinfo.value.kind == "invalid_output"
        assert excinfo.value.message == message

    def test_length_exhaustion_becomes_retryable_length_limit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_run(monkeypatch, f'{{"description": "{"y" * 40}"}}')
        with pytest.raises(ImproverRetryableError) as excinfo:
            call_improver("p", "m", None, 10, max_chars=5)
        assert excinfo.value.kind == "length_limit"
        assert (
            excinfo.value.message
            == "Improver description exceeded the configured character limit after "
            "shortening"
        )

    def test_nonzero_exit_is_fatal_not_retryable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_run(monkeypatch, "", returncode=3)
        with pytest.raises(ImproverFatalProcessError) as excinfo:
            call_improver("p", "m", None, 10)
        assert str(excinfo.value) == "Improver process exited with status 3"
        assert not isinstance(excinfo.value, ImproverRetryableError)

    @pytest.mark.parametrize("exc", [FileNotFoundError("claude"), PermissionError("x")])
    def test_environment_errors_propagate_unchanged(
        self, monkeypatch: pytest.MonkeyPatch, exc: Exception
    ) -> None:
        def fake_run(*_: Any, **__: Any) -> SimpleNamespace:
            raise exc

        monkeypatch.setattr("skill_optimizer.improver.subprocess.run", fake_run)
        with pytest.raises(type(exc)):
            call_improver("p", "m", None, 10)


class TestLaunchBudget:
    def test_consume_until_exhausted(self) -> None:
        budget = _LaunchBudget(3)
        budget.consume()
        budget.consume()
        budget.consume()
        with pytest.raises(
            ImproverFatalProcessError, match="Improver launch budget exceeded"
        ):
            budget.consume()

    def test_subprocess_refuses_launch_on_empty_budget(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        spawned = {"n": 0}

        def fake_run(*_: Any, **__: Any) -> SimpleNamespace:
            spawned["n"] += 1
            return SimpleNamespace(stdout="", returncode=0, stderr="")

        monkeypatch.setattr("skill_optimizer.improver.subprocess.run", fake_run)
        with pytest.raises(
            ImproverFatalProcessError, match="Improver launch budget exceeded"
        ):
            _run_improver_subprocess("p", "m", None, 10, _LaunchBudget(0))
        assert spawned["n"] == 0  # refused before spawning

    def test_standalone_budget_allows_initial_plus_one_shorten(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        outputs = [f'{{"description": "{"x" * 40}"}}', '{"description": "short"}']
        calls: list[int] = []

        def fake_run(*_: Any, **__: Any) -> SimpleNamespace:
            calls.append(1)
            return SimpleNamespace(
                stdout=outputs[len(calls) - 1], returncode=0, stderr=""
            )

        monkeypatch.setattr("skill_optimizer.improver.subprocess.run", fake_run)
        # No injected budget -> local two-launch budget covers initial + shorten.
        assert call_improver("p", "m", None, 10, max_chars=5)["description"] == "short"
        assert len(calls) == 2


class TestTranscriptDiagnostics:
    def test_transcript_records_return_code_and_stderr(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        def fake_run(*_: Any, **__: Any) -> SimpleNamespace:
            return SimpleNamespace(
                stdout='{"description": "d"}', returncode=0, stderr="warn line"
            )

        monkeypatch.setattr("skill_optimizer.improver.subprocess.run", fake_run)
        log = tmp_path / "improve.json"
        call_improver("p", "m", None, 10, log_path=log)
        transcript = json.loads(log.read_text())
        assert transcript["returncode"] == 0
        assert transcript["stderr"] == "warn line"

    def test_transcript_records_rewrite_diagnostics(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        outputs = [
            SimpleNamespace(
                stdout=f'{{"description": "{"x" * 40}"}}', returncode=0, stderr="s1"
            ),
            SimpleNamespace(
                stdout='{"description": "short"}', returncode=0, stderr="s2"
            ),
        ]

        def fake_run(*_: Any, **__: Any) -> SimpleNamespace:
            return outputs.pop(0)

        monkeypatch.setattr("skill_optimizer.improver.subprocess.run", fake_run)
        log = tmp_path / "improve.json"
        call_improver("p", "m", None, 10, max_chars=5, log_path=log)
        transcript = json.loads(log.read_text())
        assert transcript["rewrite_returncode"] == 0
        assert transcript["rewrite_stderr"] == "s2"

    @pytest.mark.parametrize(
        "write_error",
        [OSError("disk full"), UnicodeEncodeError("ascii", "x", 0, 1, "bad")],
    )
    def test_transcript_write_failure_preserves_success(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
        write_error: Exception,
    ) -> None:
        _stub_run(monkeypatch, '{"description": "d"}')

        def boom(*_: Any, **__: Any) -> None:
            raise write_error

        monkeypatch.setattr(Path, "write_text", boom)
        # The write failure must not replace the successful parse.
        assert (
            call_improver("p", "m", None, 10, log_path=tmp_path / "t.json")[
                "description"
            ]
            == "d"
        )
        assert "Improver transcript write failed." in caplog.text

    def test_transcript_write_failure_preserves_fatal_outcome(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _stub_run(monkeypatch, "", returncode=1)

        def boom(*_: Any, **__: Any) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(Path, "write_text", boom)
        # The write failure must not mask the fatal nonzero-exit outcome.
        with pytest.raises(ImproverFatalProcessError, match="status 1"):
            call_improver("p", "m", None, 10, log_path=tmp_path / "t.json")

    def test_transcript_write_failure_preserves_retryable_outcome(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _stub_run(monkeypatch, "not json")

        def boom(*_: Any, **__: Any) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(Path, "write_text", boom)
        with pytest.raises(ImproverRetryableError) as excinfo:
            call_improver("p", "m", None, 10, log_path=tmp_path / "t.json")
        assert excinfo.value.kind == "invalid_output"
