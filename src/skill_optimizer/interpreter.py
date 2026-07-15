"""Pure trigger-decision logic over a ``claude -p`` stream-json event sequence.

Separated from subprocess transport so it can be tested exhaustively against
synthetic and recorded event streams. The decision is made from tool-call *intent*,
so it does not depend on tool execution.
"""

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

# Tool names that, when invoked, may reference the injected candidate command.
_SKILL_TOOLS = ("Skill", "Read")


def _scan_assistant(message: Mapping[str, Any], cmd_name: str) -> bool | None:
    """Decide from a non-streaming ``assistant`` message.

    Args:
        message: The assistant message object.
        cmd_name: The injected command name to look for.

    Returns:
        ``True``/``False`` from the first ``tool_use`` item, or ``None`` if the
        message contains no tool call.
    """
    tool_use = next(
        (c for c in message.get("content", []) if c.get("type") == "tool_use"),
        None,
    )
    if tool_use is None:
        return None
    if tool_use.get("name") not in _SKILL_TOOLS:
        return False
    return cmd_name in json.dumps(tool_use.get("input", {}))


@dataclass(slots=True)
class _ScanState:
    """Mutable state for scanning a streamed tool call.

    Attributes:
        cmd_name: The injected command name to detect.
        pending: Name of the in-progress skill tool, or ``None`` if none is open.
        acc: Accumulated ``input_json_delta`` text for the pending tool.
    """

    cmd_name: str
    pending: str | None = None
    acc: str = ""

    def feed_stream_event(self, stream: Mapping[str, Any]) -> bool | None:
        """Advance the scan with one ``stream_event`` payload.

        Args:
            stream: The inner ``event`` object of a ``stream_event``.

        Returns:
            ``True``/``False`` once decided, or ``None`` to keep scanning.
        """
        stream_type = stream.get("type", "")
        if stream_type == "content_block_start":
            return self._on_block_start(stream.get("content_block", {}))
        if stream_type == "content_block_delta":
            return self._on_delta(stream.get("delta", {}))
        if stream_type in ("content_block_stop", "message_stop"):
            if self.pending is not None:
                return self.cmd_name in self.acc
            if stream_type == "message_stop":
                return False
        return None

    def _on_block_start(self, block: Mapping[str, Any]) -> bool | None:
        """Handle a ``content_block_start``.

        Args:
            block: The ``content_block`` object.

        Returns:
            ``False`` if a non-skill tool starts first, else ``None``.
        """
        if block.get("type") == "tool_use":
            if block.get("name") in _SKILL_TOOLS:
                self.pending = block.get("name")
                self.acc = ""
            else:
                return False
        return None

    def _on_delta(self, delta: Mapping[str, Any]) -> bool | None:
        """Handle a ``content_block_delta``, accumulating tool input.

        Args:
            delta: The ``delta`` object.

        Returns:
            ``True`` once ``cmd_name`` appears in the accumulated input, else
            ``None``.
        """
        if self.pending is not None and delta.get("type") == "input_json_delta":
            self.acc += delta.get("partial_json", "")
            if self.cmd_name in self.acc:
                return True
        return None


def _interpret_events_status(
    events: Iterable[Mapping[str, Any]], cmd_name: str
) -> bool | None:
    """Decide the trigger outcome, distinguishing a decisive result from an
    exhausted stream.

    Consumes events lazily and returns as soon as the outcome is decided, so a
    generator backed by a live subprocess can be abandoned early. The decision rules:

    - The first ``tool_use`` block that is **not** ``Skill``/``Read`` means the model
      went elsewhere first: ``False`` (even if a skill is used later).
    - A ``Skill``/``Read`` ``tool_use`` whose streamed input contains ``cmd_name``
      (accumulated across ``input_json_delta`` chunks): ``True``.
    - A block/message stop while a skill tool is pending resolves to whether
      ``cmd_name`` was seen in that block's input.
    - A non-streaming ``assistant`` ``tool_use`` is judged on its full input.
    - ``message_stop`` with nothing pending, or a ``result`` event: ``False``.

    ``thinking`` and ``text`` content blocks are ignored.

    Unlike :func:`interpret_events`, this returns ``None`` when the stream is exhausted
    with **no** decisive terminal event (a timeout-truncated, empty, or otherwise
    incomplete stream), so the caller can treat an unjudgeable probe differently from a
    genuine non-trigger.

    Args:
        events: Parsed stream-json events, in order.
        cmd_name: The injected command name to look for in tool input.

    Returns:
        ``True`` (decisive trigger), ``False`` (decisive non-trigger), or ``None``
        (no decisive terminal event was observed).
    """
    state = _ScanState(cmd_name)
    for event in events:
        event_type = event.get("type")
        if event_type == "stream_event":
            decision = state.feed_stream_event(event.get("event", {}))
        elif event_type == "assistant":
            decision = _scan_assistant(event.get("message", {}), cmd_name)
        elif event_type == "result":
            decision = False
        else:
            decision = None
        if decision is not None:
            return decision
    return None


def interpret_events(events: Iterable[Mapping[str, Any]], cmd_name: str) -> bool:
    """Decide whether a stream invokes the injected candidate command.

    A thin, pure-bool wrapper over :func:`_interpret_events_status` that coerces the
    "no decisive terminal event" case (``None``) to ``False``. See that function for
    the full decision rules.

    Args:
        events: Parsed stream-json events, in order.
        cmd_name: The injected command name to look for in tool input.

    Returns:
        ``True`` if the model invoked the candidate command, else ``False``.
    """
    return _interpret_events_status(events, cmd_name) is True
