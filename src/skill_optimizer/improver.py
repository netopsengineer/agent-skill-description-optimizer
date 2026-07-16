"""The description improver: prompt construction and the ``claude -p`` call."""

import json
import logging
import re
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

from skill_optimizer._process import claude_bin, subprocess_env
from skill_optimizer.models import EvalResult, ImproverAttempt, PerQuery

logger = logging.getLogger(__name__)

# Hard limit on description length; longer descriptions get truncated downstream,
# so the improver output is rewritten to fit (mirrors skill-creator).
DESCRIPTION_CHAR_LIMIT = 1024

# Closed allowlist of retryable failure kinds -> their exact public messages. The
# messages are fixed, non-sensitive templates (no prompt/stdout/stderr/path), so a
# public failure ledger built only from these cannot disclose raw diagnostics.
_RETRYABLE_MESSAGES: dict[str, frozenset[str]] = {
    "timeout": frozenset({"Improver timed out"}),
    "invalid_output": frozenset(
        {
            "Improver returned no JSON",
            "Improver JSON missing 'description'",
            "Improver returned invalid JSON",
            "Improver returned ambiguous JSON: multiple usable description objects",
        }
    ),
    "length_limit": frozenset(
        {
            "Improver description exceeded the configured character limit after shortening"
        }
    ),
}


class ImproverRetryableError(ValueError):
    """A typed, retryable improver failure carrying only a validated kind + message.

    Subclasses :class:`ValueError` so the parser's stable ``ValueError`` contract still
    holds. The constructor rejects any kind/message outside :data:`_RETRYABLE_MESSAGES`,
    so a public record built from these is provably non-sensitive.
    """

    def __init__(self, kind: str, message: str) -> None:
        """Validate ``kind``/``message`` against the closed allowlist.

        Args:
            kind: One of ``timeout``/``invalid_output``/``length_limit``.
            message: An exact message from that kind's allowlist.

        Raises:
            ValueError: If ``kind`` is unknown or ``message`` is not allowlisted for it.
        """
        allowed = _RETRYABLE_MESSAGES.get(kind)
        if allowed is None or message not in allowed:
            raise ValueError(f"invalid retryable error: {kind!r}/{message!r}")
        self.kind: str = kind
        self.message: str = message
        super().__init__(message)


class ImproverFatalProcessError(RuntimeError):
    """A non-retryable improver failure: a completed nonzero exit or budget exhaustion.

    Its message is a fixed template (an exit status or the budget line); raw child
    stderr is never placed in it.
    """


class _LaunchBudget:
    """A mutable, decrementing ceiling on improver child-process launches.

    :meth:`consume` is spent once at the low-level spawn boundary before each child, so
    no caller can bypass the ceiling. Exhaustion is fatal (never retried).
    """

    def __init__(self, tokens: int) -> None:
        """Initialize the budget.

        Args:
            tokens: The maximum number of child launches allowed.
        """
        self._remaining = tokens

    def consume(self) -> None:
        """Spend one launch token.

        Raises:
            ImproverFatalProcessError: If no launch tokens remain.
        """
        if self._remaining <= 0:
            raise ImproverFatalProcessError("Improver launch budget exceeded")
        self._remaining -= 1


IMPROVER_TEMPLATE = """\
You are tuning the `description` field of a Claude Code/Agent skill named "{name}".
The description is the ONLY text Claude sees when deciding whether to invoke this
skill — so it must clearly signal the tasks the skill is for, and clearly NOT match
adjacent tasks it is not for.

What the skill actually does (from its body, for accuracy — do not exceed this scope):
---
{body_excerpt}
---

Current description:
---
{description}
---

Evaluation of the current description (per model, trigger rate over repeated runs):
- A "should_trigger=true" query that didn't trigger is a FALSE NEGATIVE (under-triggering).
- A "should_trigger=false" query that did trigger is a FALSE POSITIVE (over-triggering).

Failing queries (where at least one model got it wrong):
{failures}

Per-model accuracy: {acc}

Descriptions already tried, with their training-set results (do NOT repeat these —
produce something structurally different in wording and emphasis):
{prior_attempts}

Rewrite the description to fix these failures. Guidance:
- Lead with concrete triggers: the verbs, file types, tool names, and concepts that
  should fire this skill.
- To curb false positives, it's fine to state what the skill is NOT for when it
  collides with an adjacent task.
- Keep it accurate to the skill body above; do not oversell.
- A little "pushiness" helps under-triggering, but pushiness that causes false
  positives is a failure, not a win. Keep it tight (roughly 1-4 sentences), and spend
  the words on the range of intents and concrete cues that should fire it - not on
  quoting specific example queries, which overfits and tends to narrow triggering.

Return ONLY a JSON object, no prose, no code fences:
{{"description": "<new description>", "rationale": "<2-3 sentences on what you changed>"}}
"""

# Max characters of skill body to include as context for the improver.
_BODY_EXCERPT_LIMIT = 1500


def _short_model(model: str) -> str:
    """Return a model's short display name (segment after the first ``-``).

    Args:
        model: A full model id or bare name.

    Returns:
        The short name, or the full name when there is no ``-``.
    """
    return model.split("-")[1] if "-" in model else model


def _render_query_line(pq: PerQuery) -> str:
    """Render one training-query result line for a prior-attempt block.

    Args:
        pq: The per-query roll-up (train-only) to render.

    Returns:
        A ``[STATUS] "query" triggered N/M (model=n/m ...)`` line, where ``N/M`` are
        summed across models and the parenthetical keeps each model's own rate.
    """
    models = pq["models"]
    triggers = sum(models[m]["triggers"] for m in models)
    runs = sum(models[m]["runs"] for m in models)
    status = (
        "PASS"
        if pq["all_pass"] is True
        else "FAIL"
        if pq["all_pass"] is False
        else "N/A"
    )
    per_model = ", ".join(
        f"{_short_model(m)}={models[m]['triggers']}/{models[m]['runs']}" for m in models
    )
    return (
        f'  [{status}] "{pq["query"][:80]}" triggered {triggers}/{runs} ({per_model})'
    )


def _render_prior_attempts(prior_attempts: Sequence[ImproverAttempt]) -> str:
    """Render tried descriptions as ``<attempt>`` blocks with their train results.

    Only training-set results are rendered; held-out results are never included, so the
    improver stays blind to the selection set.

    Args:
        prior_attempts: The previously-tried attempts (train-only results).

    Returns:
        The rendered blocks, or ``"(none yet)"`` when there are no prior attempts.
    """
    if not prior_attempts:
        return "(none yet)"
    blocks: list[str] = []
    for att in prior_attempts:
        desc = att["description"]
        lines = [f"<attempt ({len(desc)} chars)>", f'description: "{desc}"']
        if att["train_results"]:
            lines.append("train results:")
            lines.extend(_render_query_line(pq) for pq in att["train_results"])
        lines.append("</attempt>")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def build_improver_prompt(
    name: str,
    description: str,
    body: str,
    ev: EvalResult,
    prior_attempts: Sequence[ImproverAttempt] = (),
) -> str:
    """Build the improver prompt from the current description and its failures.

    Only training-set data is passed in (``ev`` is the training view and
    ``prior_attempts`` carry train-only results), so no held-out query text reaches the
    improver — the selection set stays blind to avoid overfitting.

    Args:
        name: The skill name.
        description: The current description being improved.
        body: The skill body; truncated to a fixed excerpt for context.
        ev: Training-set evaluation of the current description; only decisively failing
            queries (``all_pass is False``) are listed as failures.
        prior_attempts: Descriptions already tried this run, each with its train-only
            per-query results, listed so the improver avoids repeating them.

    Returns:
        The fully formatted improver prompt.
    """
    failures: list[str] = []
    for pq in ev["per_query"]:
        # Only decisive failures are shown; passes and unjudged queries are skipped.
        if pq["all_pass"] is not False:
            continue
        rates = {m: pq["models"][m]["trigger_rate"] for m in pq["models"]}
        kind = "should trigger" if pq["should_trigger"] else "should NOT trigger"
        failures.append(
            f'- ({kind}) "{pq["query"]}"  trigger_rates={json.dumps(rates)}'
        )
    return IMPROVER_TEMPLATE.format(
        name=name,
        body_excerpt=body.strip()[:_BODY_EXCERPT_LIMIT],
        description=description,
        failures="\n".join(failures) or "(none — all queries pass)",
        acc=json.dumps(ev["per_model_accuracy"]),
        prior_attempts=_render_prior_attempts(prior_attempts),
    )


# One shared decoder for span validation (see :func:`_decode_span`).
_DECODER = json.JSONDecoder()


def _has_description(obj: dict[str, Any]) -> bool:
    """Return whether a decoded object carries a non-blank ``description``.

    Args:
        obj: A decoded JSON object.

    Returns:
        ``True`` when ``str(obj.get("description", "")).strip()`` is non-empty.
    """
    return bool(str(obj.get("description", "")).strip())


def _match_container_span(text: str, start: int) -> int:
    """Return the index of the closer matching the ``{``/``[`` opener at ``start``.

    Walks a mixed brace/bracket stack, ignoring delimiters inside double-quoted JSON
    strings. A double-quote toggles string state only when it is not escaped, i.e. when
    the immediately preceding backslash run has even length.

    Args:
        text: The cleaned response text.
        start: Index of the ``{`` or ``[`` opener to match.

    Returns:
        The index of the matching closer.

    Raises:
        ValueError: ``Improver returned invalid JSON`` when the opener has a mismatched
            closer, an unclosed stack at end of text, or an unterminated string.
    """
    stack: list[str] = []
    in_string = False
    escape = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "{[":
            stack.append(char)
        elif char in "}]":
            opener = stack.pop()
            if (char == "}") != (opener == "{"):
                raise ValueError("Improver returned invalid JSON")
            if not stack:
                return index
    raise ValueError("Improver returned invalid JSON")


def _decode_span(cleaned: str, start: int, end: int) -> tuple[bool, Any]:
    """Decode the balanced span ``cleaned[start:end + 1]`` as one JSON value.

    Args:
        cleaned: The cleaned response text.
        start: Index of the span's opener.
        end: Index of the span's matching closer.

    Returns:
        ``(True, value)`` if the span decodes to a complete JSON value that ends exactly
        at ``end + 1``; otherwise ``(False, None)`` (a malformed or only-partially-valid
        span, which the caller skips as a whole).
    """
    try:
        value, decode_end = _DECODER.raw_decode(cleaned, start)
    except json.JSONDecodeError:
        return False, None
    except RecursionError:
        # A pathologically nested span overflows the JSON decoder's recursion guard;
        # treat it as a malformed span (skipped as a whole) rather than letting an
        # uncaught RecursionError escape the parser's stable-ValueError contract. Kept as
        # its own clause (not ``except (JSONDecodeError, RecursionError):``) because
        # ruff-format rewrites that tuple into the invalid Py2 ``except A, B:`` syntax.
        return False, None
    return (True, value) if decode_end == end + 1 else (False, None)


def _extract_top_level_objects(cleaned: str) -> dict[str, Any]:
    """Scan for balanced top-level containers and select the single usable object.

    Recovery path when the whole response is not itself one JSON object. Scans
    left-to-right; each top-level ``{``/``[`` opener is matched to its closer as one
    indivisible span (:func:`_match_container_span`) and decoded as a whole
    (:func:`_decode_span`). Nested openers are never re-scanned and arrays are skipped
    as containers, so an illustrative or nested object is never promoted.

    Args:
        cleaned: The cleaned response text (fences stripped, whitespace trimmed).

    Returns:
        The single top-level object that carries a non-blank ``description``.

    Raises:
        ValueError: ``Improver returned ambiguous JSON: multiple usable description
            objects`` if two or more usable objects are found; ``Improver returned
            invalid JSON`` on a structural failure or a malformed/non-object span with
            no usable object; ``Improver JSON missing 'description'`` when only complete
            description-less objects are found; ``Improver returned no JSON`` when no
            container is present.
    """
    usable: list[dict[str, Any]] = []
    saw_invalid = False
    saw_dict_without_desc = False
    index = 0
    while index < len(cleaned):
        char = cleaned[index]
        if char not in "{[":
            index += 1
            continue
        end = _match_container_span(cleaned, index)
        decoded, value = _decode_span(cleaned, index, end)
        if not decoded or not isinstance(value, dict):
            # Malformed span, or a balanced non-object (e.g. an array): skip as a whole.
            saw_invalid = True
        elif _has_description(cast("dict[str, Any]", value)):
            usable.append(cast("dict[str, Any]", value))
        else:
            saw_dict_without_desc = True
        index = end + 1
    if len(usable) >= 2:
        raise ValueError(
            "Improver returned ambiguous JSON: multiple usable description objects"
        )
    if len(usable) == 1:
        return usable[0]
    if saw_invalid:
        raise ValueError("Improver returned invalid JSON")
    if saw_dict_without_desc:
        raise ValueError("Improver JSON missing 'description'")
    raise ValueError("Improver returned no JSON")


def _parse_improver_output(raw: str) -> dict[str, Any]:
    """Extract and validate the JSON object from a raw improver response.

    Strips code fences, then tries to parse the whole cleaned response as one JSON
    value. A whole-response object must carry a non-blank ``description``; a scalar or
    list root is rejected outright. Only when whole-response parsing fails does the
    string/container-aware recovery scanner (:func:`_extract_top_level_objects`) run.

    Args:
        raw: The raw ``claude -p`` stdout text.

    Returns:
        The parsed JSON object, guaranteed to contain a non-blank ``description``.

    Raises:
        ValueError: With one of the fixed messages ``Improver returned no JSON``,
            ``Improver JSON missing 'description'``, ``Improver returned invalid JSON``,
            or ``Improver returned ambiguous JSON: multiple usable description
            objects`` when the response cannot yield exactly one usable object.
    """
    cleaned = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.MULTILINE).strip()
    try:
        whole = json.loads(cleaned)
    except RecursionError as exc:
        # Pathologically nested JSON overflows the decoder's recursion. Map it to the
        # stable invalid-JSON message (which the caller treats as a retryable
        # ``invalid_output``) instead of letting an uncaught RecursionError escape the
        # parser's ValueError contract and abort the run with a traceback.
        raise ValueError("Improver returned invalid JSON") from exc
    except json.JSONDecodeError:
        return _extract_top_level_objects(cleaned)
    if isinstance(whole, dict):
        data = cast("dict[str, Any]", whole)
        if _has_description(data):
            return data
        raise ValueError("Improver JSON missing 'description'")
    raise ValueError("Improver returned invalid JSON")


def _parse_or_retryable(raw: str) -> dict[str, Any]:
    """Parse the improver output, wrapping only known parser failures as retryable.

    Known :func:`_parse_improver_output` messages become
    :class:`ImproverRetryableError` (``invalid_output``); any other ``ValueError`` is
    left untouched so configuration/programming faults still propagate fatally.

    Args:
        raw: The raw ``claude -p`` stdout text.

    Returns:
        The parsed object with a non-blank ``description``.

    Raises:
        ImproverRetryableError: When parsing fails with an allowlisted parser message.
        ValueError: Any other parse failure, propagated unchanged.
    """
    try:
        return _parse_improver_output(raw)
    except ImproverRetryableError:
        raise
    except ValueError as exc:
        message = str(exc)
        if message in _RETRYABLE_MESSAGES["invalid_output"]:
            raise ImproverRetryableError("invalid_output", message) from exc
        raise


def _write_transcript(log_path: Path | None, transcript: dict[str, Any]) -> None:
    """Persist the improver transcript, best-effort (never replaces the primary outcome).

    Ordinary serialization/write errors are swallowed with a fixed warning so a
    transcript-write failure cannot mask a success, retry, or fatal result.
    ``BaseException`` (``KeyboardInterrupt``/``SystemExit``) is not caught.

    Args:
        log_path: Where to write the JSON transcript, or ``None`` to skip.
        transcript: The transcript mapping to serialize.
    """
    if log_path is None:
        return
    try:
        log_path.write_text(json.dumps(transcript, indent=2))
    except Exception:  # noqa: BLE001 - best-effort; must not replace the primary outcome
        logger.warning("Improver transcript write failed.")


def _run_improver_subprocess(
    prompt: str, model: str, effort: str | None, timeout: int, budget: _LaunchBudget
) -> subprocess.CompletedProcess[str]:
    """Run one ``claude -p`` improver subprocess and return the completed process.

    Consumes one launch token from ``budget`` as its first action, before spawning, so
    the launch ceiling cannot be bypassed. Kept separate from parsing so the raw stdout
    is available for the transcript before the return-code check or JSON parse can
    raise. The prompt is sent over stdin (not argv): it embeds the full SKILL.md body
    and prior attempts, which can exceed a comfortable argv length.

    Args:
        prompt: The prompt to send on stdin.
        model: Model id for the improver.
        effort: Reasoning effort, or ``None`` to omit ``--effort``.
        timeout: Subprocess timeout, in seconds.
        budget: The launch budget; one token is consumed before spawning.

    Returns:
        The completed ``claude -p`` process, with stdout/stderr captured as text.

    Raises:
        ImproverFatalProcessError: If the launch budget is already exhausted.
    """
    budget.consume()
    cmd = [claude_bin(), "-p", "--model", model, "--output-format", "text"]
    if effort:
        cmd += ["--effort", effort]
    return subprocess.run(
        cmd,
        input=prompt,
        capture_output=True,
        text=True,
        env=subprocess_env(),
        timeout=timeout,
    )


def call_improver(
    prompt: str,
    model: str,
    effort: str | None,
    timeout: int,
    max_chars: int = DESCRIPTION_CHAR_LIMIT,
    log_path: Path | None = None,
    budget: _LaunchBudget | None = None,
) -> dict[str, Any]:
    """Ask the improver for a rewritten description, enforcing the length limit.

    If the first proposal exceeds ``max_chars``, makes one further ``claude -p`` call
    that quotes the over-long text and asks for a shorter rewrite. When ``log_path`` is
    set, a JSON transcript (prompt, raw response + return code + stderr, parsed
    description, char count, and any rewrite round) is written there, best-effort. When
    no ``budget`` is injected, a local two-launch budget bounds this call to the initial
    child plus at most one shortening child.

    Failure classification: a subprocess timeout, a known parser failure, and a
    still-over-limit description after shortening are typed
    :class:`ImproverRetryableError`; a completed nonzero exit and launch-budget
    exhaustion are :class:`ImproverFatalProcessError`; every other error (missing
    executable, permissions, configuration, programming faults) propagates unchanged.

    Args:
        prompt: The improver prompt from :func:`build_improver_prompt`.
        model: Model id for the improver.
        effort: Reasoning effort (``high``/``medium``/``low``), or ``None`` to omit.
        timeout: Subprocess timeout, in seconds.
        max_chars: Hard character ceiling for the description (default 1024).
        log_path: Where to write the JSON transcript, or ``None`` to skip logging.
        budget: Shared launch budget, or ``None`` to use a local two-launch budget.

    Returns:
        The parsed JSON object, guaranteed to contain a non-empty ``description``.

    Raises:
        ImproverRetryableError: On a timeout, a known parser failure, or a description
            still over ``max_chars`` after the shorten retry.
        ImproverFatalProcessError: On a completed nonzero child exit or launch-budget
            exhaustion.
    """
    if budget is None:
        # A standalone call: enough for the initial child and at most one shortening
        # child. A third child in the same call is refused.
        budget = _LaunchBudget(2)
    # Build the transcript incrementally so a first-call failure (non-zero exit or a
    # malformed response) still leaves the raw output behind for diagnosis, rather than
    # raising before anything is written.
    transcript: dict[str, Any] = {"prompt": prompt}
    try:
        result = _run_improver_subprocess(prompt, model, effort, timeout, budget)
        transcript["response"] = result.stdout.strip()
        transcript["returncode"] = result.returncode
        transcript["stderr"] = result.stderr
        if result.returncode != 0:
            raise ImproverFatalProcessError(
                f"Improver process exited with status {result.returncode}"
            )
        data = _parse_or_retryable(result.stdout.strip())
        description = str(data["description"]).strip()
        transcript["parsed_description"] = description
        transcript["char_count"] = len(description)
        transcript["over_limit"] = len(description) > max_chars
        if len(description) > max_chars:
            shorten_prompt = prompt + (
                "\n\n---\n\n"
                f"A previous attempt produced this description, which at "
                f"{len(description)} characters is over the {max_chars}-"
                f"character hard limit:\n\n"
                f'"{description}"\n\n'
                f"Rewrite it to be under {max_chars} characters while "
                "keeping the most important trigger words and intent coverage. Return "
                'ONLY a JSON object like {"description": "...", "rationale": "..."}, '
                "no prose, no code fences."
            )
            # Record retry inputs and stdout before either the process status or JSON
            # parsing can fail. Failed retries are otherwise exactly the cases where
            # the transcript is most useful for diagnosis.
            transcript["rewrite_prompt"] = shorten_prompt
            rewrite_result = _run_improver_subprocess(
                shorten_prompt, model, effort, timeout, budget
            )
            transcript["rewrite_response"] = rewrite_result.stdout.strip()
            transcript["rewrite_returncode"] = rewrite_result.returncode
            transcript["rewrite_stderr"] = rewrite_result.stderr
            if rewrite_result.returncode != 0:
                raise ImproverFatalProcessError(
                    f"Improver process exited with status {rewrite_result.returncode}"
                )
            data = _parse_or_retryable(rewrite_result.stdout.strip())
            shortened = str(data["description"]).strip()
            transcript["rewrite_char_count"] = len(shortened)
            if len(shortened) > max_chars:
                raise ImproverRetryableError(
                    "length_limit",
                    "Improver description exceeded the configured character limit "
                    "after shortening",
                )
        return data
    except subprocess.TimeoutExpired as exc:
        transcript.setdefault("error", repr(exc))
        raise ImproverRetryableError("timeout", "Improver timed out") from exc
    except Exception as exc:  # noqa: BLE001 - re-raised; only records a diagnostic note
        transcript.setdefault("error", repr(exc))
        raise
    finally:
        _write_transcript(log_path, transcript)
