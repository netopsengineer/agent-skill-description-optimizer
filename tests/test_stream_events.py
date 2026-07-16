"""Unit tests for ``_stream_events`` transport branches, driven by a scripted proc.

``_stream_events`` is the ``select``/``os.read``/``poll`` state machine that turns a live
``claude -p`` stdout pipe into decoded JSON events. Its happy path is covered end to end
via the fake-``claude`` integration tests; these drive it directly with a fully scripted
fake process so every edge branch is deterministic and portable (no real subprocess, no
wall-clock timing): select-not-ready, incremental read, blank/undecodable lines, the
process-exit drain, break-on-exit, timeout exit, and the trailing-line final flush.
"""

from typing import Any

import pytest

from skill_optimizer import evaluation
from skill_optimizer.evaluation import (
    _stream_events,  # pyright: ignore[reportPrivateUsage]
)


class _FakeStdout:
    """A stand-in for a subprocess stdout pipe with a scripted drain read."""

    def __init__(self, drain: bytes = b"") -> None:
        self._drain = drain

    def fileno(self) -> int:
        """Return a dummy descriptor (``os.read`` is patched, so it is never used).

        Returns:
            A fixed sentinel file descriptor.
        """
        return -1

    def read(self) -> bytes:
        """Return the once-only post-exit drain payload, then empty.

        Returns:
            The drain bytes on the first call, ``b""`` thereafter.
        """
        drained, self._drain = self._drain, b""
        return drained


class _FakeProc:
    """A subprocess stand-in whose ``poll()`` follows a fixed script."""

    def __init__(self, polls: list[int | None], drain: bytes = b"") -> None:
        self._polls = list(polls)
        self._last: int | None = 0
        self.stdout = _FakeStdout(drain)

    def poll(self) -> int | None:
        """Return the next scripted poll value, holding the last once exhausted.

        Returns:
            ``None`` while running or an int exit status once exited, per the script.
        """
        if self._polls:
            self._last = self._polls.pop(0)
        return self._last


def _drive(
    monkeypatch: pytest.MonkeyPatch,
    proc: _FakeProc,
    *,
    ready: list[list[Any]],
    reads: list[bytes],
    timeout: float = 1000.0,
    monotonic: list[float] | None = None,
) -> list[dict[str, Any]]:
    """Run ``_stream_events`` against ``proc`` with scripted select/read/clock.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        proc: The scripted fake process.
        ready: Successive ``select.select`` readiness results (``[]`` == not ready).
        reads: Successive ``os.read`` return payloads.
        timeout: Wall-clock budget passed to ``_stream_events``.
        monotonic: Optional scripted ``time.monotonic`` sequence to force a timeout exit.

    Returns:
        The list of decoded events the generator yielded.
    """
    ready_q = list(ready)
    reads_q = list(reads)

    def fake_select(
        _rlist: Any, _wlist: Any, _xlist: Any, _timeout: float
    ) -> tuple[list[Any], list[Any], list[Any]]:
        return (ready_q.pop(0) if ready_q else [], [], [])

    def fake_read(_fd: int, _n: int) -> bytes:
        return reads_q.pop(0) if reads_q else b""

    monkeypatch.setattr(evaluation.select, "select", fake_select)
    monkeypatch.setattr(evaluation.os, "read", fake_read)
    if monotonic is not None:
        mono_q = list(monotonic)
        monkeypatch.setattr(
            evaluation.time, "monotonic", lambda: mono_q.pop(0) if mono_q else 1e18
        )
    return list(_stream_events(proc, timeout))  # pyright: ignore[reportArgumentType]


def test_yields_event_skips_blank_and_undecodable_then_breaks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # One read carries a valid event, a blank line, and a garbage line; the process has
    # exited by the bottom poll, so the loop breaks with the buffer drained.
    proc = _FakeProc(polls=[None, 0])
    events = _drive(
        monkeypatch,
        proc,
        ready=[["r"]],
        reads=[b'{"type":"a"}\n\nnot json\n'],
    )
    assert events == [{"type": "a"}]


def test_select_not_ready_continues(monkeypatch: pytest.MonkeyPatch) -> None:
    # First poll: running but select reports nothing ready -> continue. Second poll:
    # ready but the read is empty; the process then exits and the loop breaks.
    proc = _FakeProc(polls=[None, None, 0])
    events = _drive(monkeypatch, proc, ready=[[], ["r"]], reads=[b""])
    assert events == []


def test_drains_remainder_after_process_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    # The process has already exited at the top poll, so the final buffered output is
    # drained via proc.stdout.read() rather than os.read().
    proc = _FakeProc(polls=[0, 0], drain=b'{"type":"z"}\n')
    events = _drive(monkeypatch, proc, ready=[], reads=[])
    assert events == [{"type": "z"}]


def test_exited_with_empty_remainder_falls_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The process has exited but the drain read yields nothing: the ``elif remainder``
    # is false, so control falls through to the (empty) line-split loop and then breaks.
    proc = _FakeProc(polls=[0, 0], drain=b"")
    events = _drive(monkeypatch, proc, ready=[], reads=[])
    assert events == []


def test_final_flush_of_trailing_unterminated_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A trailing line with no newline stays buffered through the loop and is flushed by
    # the post-loop tail handler.
    proc = _FakeProc(polls=[None, 0])
    events = _drive(monkeypatch, proc, ready=[["r"]], reads=[b'{"type":"final"}'])
    assert events == [{"type": "final"}]


def test_timeout_exit_with_undecodable_tail_is_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The clock advances past the timeout so the while-condition (not a break) ends the
    # loop, and the buffered non-JSON tail is swallowed by the final-flush except.
    proc = _FakeProc(polls=[None, None])
    events = _drive(
        monkeypatch,
        proc,
        ready=[["r"]],
        reads=[b"garbage-no-json"],
        timeout=10.0,
        monotonic=[0.0, 0.0, 100.0],
    )
    assert events == []
