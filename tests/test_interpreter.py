"""Tests for the extracted pure trigger-decision state machine.

Covers every branch with synthetic event lists, then replays the recorded/synthetic
``.jsonl`` fixtures to confirm the interpreter agrees with the documented ground
truth (see tests/fixtures/README.md).
"""

import json
from pathlib import Path
from typing import Any

from skill_optimizer import interpret_events
from skill_optimizer.interpreter import (
    _interpret_events_status,  # pyright: ignore[reportPrivateUsage]
)

CMD = "sk-cand-001"
FIXTURES = Path(__file__).parent / "fixtures"


# --------------------------------------------------------------------------- #
# Event builders (shape mirrors real `claude -p` stream-json)
# --------------------------------------------------------------------------- #
def _se(event: dict[str, Any]) -> dict[str, Any]:
    return {"type": "stream_event", "event": event}


def cbs_tool(name: str) -> dict[str, Any]:
    return _se(
        {
            "type": "content_block_start",
            "content_block": {"type": "tool_use", "name": name, "input": {}},
        }
    )


def cbs(block_type: str) -> dict[str, Any]:
    return _se({"type": "content_block_start", "content_block": {"type": block_type}})


def cbd_input(partial: str) -> dict[str, Any]:
    return _se(
        {
            "type": "content_block_delta",
            "delta": {"type": "input_json_delta", "partial_json": partial},
        }
    )


def cbd_thinking(text: str) -> dict[str, Any]:
    return _se(
        {
            "type": "content_block_delta",
            "delta": {"type": "thinking_delta", "thinking": text},
        }
    )


def cb_stop() -> dict[str, Any]:
    return _se({"type": "content_block_stop"})


def msg_stop() -> dict[str, Any]:
    return _se({"type": "message_stop"})


def assistant(content: list[dict[str, Any]]) -> dict[str, Any]:
    return {"type": "assistant", "message": {"content": content}}


# --------------------------------------------------------------------------- #
# Streaming branches
# --------------------------------------------------------------------------- #
class TestStreamingDecision:
    def test_non_skill_tool_first_is_false(self) -> None:
        assert interpret_events([cbs_tool("Bash")], CMD) is False

    def test_non_skill_tool_first_even_if_skill_later(self) -> None:
        # Mirrors real_trigger.jsonl: Bash before Skill -> False.
        events = [
            cbs_tool("Bash"),
            cbs_tool("Skill"),
            cbd_input(f'{{"skill":"{CMD}"}}'),
        ]
        assert interpret_events(events, CMD) is False

    def test_skill_with_cmd_in_single_delta_is_true(self) -> None:
        events = [cbs_tool("Skill"), cbd_input(f'{{"skill": "{CMD}"}}')]
        assert interpret_events(events, CMD) is True

    def test_skill_with_cmd_split_across_deltas_is_true(self) -> None:
        events = [
            cbs_tool("Skill"),
            cbd_input('{"skill": "sk-c'),
            cbd_input('and-001", "args": "x"}'),
        ]
        assert interpret_events(events, CMD) is True

    def test_read_tool_with_cmd_is_true(self) -> None:
        events = [cbs_tool("Read"), cbd_input(f'{{"file":"{CMD}.md"}}')]
        assert interpret_events(events, CMD) is True

    def test_skill_block_stop_without_cmd_is_false(self) -> None:
        events = [cbs_tool("Skill"), cbd_input('{"skill":"other"}'), cb_stop()]
        assert interpret_events(events, CMD) is False

    def test_message_stop_with_pending_but_no_cmd_is_false(self) -> None:
        events = [cbs_tool("Skill"), cbd_input('{"skill":"other"}'), msg_stop()]
        assert interpret_events(events, CMD) is False

    def test_message_stop_no_pending_is_false(self) -> None:
        assert interpret_events([msg_stop()], CMD) is False

    def test_thinking_and_text_blocks_are_ignored(self) -> None:
        events = [
            cbs("thinking"),
            cbd_thinking(f"I'll use {CMD}"),  # thinking mentioning cmd must NOT count
            cbs("text"),
            cbs_tool("Skill"),
            cbd_input(f'{{"skill":"{CMD}"}}'),
        ]
        assert interpret_events(events, CMD) is True

    def test_thinking_delta_mentioning_cmd_does_not_trigger(self) -> None:
        # Only input_json_delta counts; a pending skill + thinking mention is not enough.
        events = [cbs_tool("Skill"), cbd_thinking(f"mentions {CMD}")]
        assert interpret_events(events, CMD) is False


# --------------------------------------------------------------------------- #
# Non-streaming assistant / result branches
# --------------------------------------------------------------------------- #
class TestNonStreamingDecision:
    def test_assistant_skill_tool_with_cmd_is_true(self) -> None:
        events = [
            assistant([{"type": "tool_use", "name": "Skill", "input": {"skill": CMD}}])
        ]
        assert interpret_events(events, CMD) is True

    def test_assistant_skill_tool_without_cmd_is_false(self) -> None:
        events = [
            assistant([{"type": "tool_use", "name": "Skill", "input": {"skill": "x"}}])
        ]
        assert interpret_events(events, CMD) is False

    def test_assistant_non_skill_tool_is_false(self) -> None:
        events = [assistant([{"type": "tool_use", "name": "Bash", "input": {}}])]
        assert interpret_events(events, CMD) is False

    def test_assistant_text_only_items_are_skipped(self) -> None:
        events = [
            assistant(
                [
                    {"type": "text", "text": "thinking..."},
                    {"type": "tool_use", "name": "Skill", "input": {"skill": CMD}},
                ]
            )
        ]
        assert interpret_events(events, CMD) is True

    def test_result_event_is_false(self) -> None:
        assert interpret_events([{"type": "result"}], CMD) is False

    def test_empty_stream_is_false(self) -> None:
        assert interpret_events([], CMD) is False


# --------------------------------------------------------------------------- #
# Status variant: distinguishes a decisive outcome from an exhausted stream
# --------------------------------------------------------------------------- #
class TestInterpretEventsStatus:
    def test_empty_stream_is_none(self) -> None:
        # No decisive terminal event -> None (public wrapper coerces this to False).
        assert _interpret_events_status([], CMD) is None

    def test_pending_skill_never_resolved_is_none(self) -> None:
        # A skill block opens but the stream is cut off before any stop event.
        events = [cbs_tool("Skill"), cbd_input('{"skill":"oth')]
        assert _interpret_events_status(events, CMD) is None

    def test_result_event_is_decisive_false(self) -> None:
        assert _interpret_events_status([{"type": "result"}], CMD) is False

    def test_message_stop_no_pending_is_decisive_false(self) -> None:
        assert _interpret_events_status([msg_stop()], CMD) is False

    def test_non_skill_tool_first_is_decisive_false(self) -> None:
        assert _interpret_events_status([cbs_tool("Bash")], CMD) is False

    def test_skill_trigger_is_true(self) -> None:
        events = [cbs_tool("Skill"), cbd_input(f'{{"skill": "{CMD}"}}')]
        assert _interpret_events_status(events, CMD) is True


# --------------------------------------------------------------------------- #
# Replay of recorded / synthetic fixtures
# --------------------------------------------------------------------------- #
def _load(name: str) -> list[dict[str, Any]]:
    text = (FIXTURES / f"{name}.jsonl").read_text()
    return [json.loads(line) for line in text.splitlines() if line.strip()]


class TestFixtureReplay:
    FIXTURE_CMD = "pdfextract-cand-fix01"

    def test_real_no_trigger(self) -> None:
        assert interpret_events(_load("real_no_trigger"), self.FIXTURE_CMD) is False

    def test_real_trigger_is_false_due_to_bash_first(self) -> None:
        assert interpret_events(_load("real_trigger"), self.FIXTURE_CMD) is False

    def test_skill_first_trigger_is_true(self) -> None:
        assert interpret_events(_load("skill_first_trigger"), self.FIXTURE_CMD) is True
