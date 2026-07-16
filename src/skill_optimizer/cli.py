"""Command-line entry point and run orchestration."""

import argparse
import json
import logging
import sys
import tempfile
import time
import webbrowser
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from skill_optimizer._process import (
    claude_available,
    claude_bin,
)
from skill_optimizer.evaluation import evaluate, subset_result
from skill_optimizer.improver import (
    ImproverFatalProcessError,
    ImproverRetryableError,
    _LaunchBudget,  # pyright: ignore[reportPrivateUsage]
    build_improver_prompt,
    call_improver,
)
from skill_optimizer.models import (
    MODEL_ALIASES,
    EvalConfig,
    EvalQuery,
    EvalResult,
    ImproverAttempt,
    PerQuery,
)
from skill_optimizer.report import generate_html
from skill_optimizer.selection import (
    is_better_candidate,
    resolve_models,
    stratified_split,
    summarize,
    summarize_verbose,
)
from skill_optimizer.skill_md import parse_skill_md, safe_name_token, write_description

logger = logging.getLogger(__name__)

# Upper bound on improve->re-eval rounds. Bounds autonomous cost: composed with the
# per-slot retry and the internal shortening call, this caps improver child launches
# (see ``_LaunchBudget``). ``--iterations`` is validated to the inclusive [0, 50] range.
MAX_ITERATIONS = 50


def _validate_iterations(iterations: int) -> int:
    """Return an iteration count in ``[0, 50]``, or raise the exact ``ValueError``.

    Args:
        iterations: The requested iteration count.

    Returns:
        ``iterations`` unchanged when it lies in the inclusive ``[0, 50]`` range.

    Raises:
        ValueError: If ``iterations`` is outside ``[0, 50]``.
    """
    if not 0 <= iterations <= MAX_ITERATIONS:
        raise ValueError(f"--iterations must be between 0 and {MAX_ITERATIONS}")
    return iterations


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line argument parser.

    Returns:
        The configured :class:`argparse.ArgumentParser`.
    """
    from skill_optimizer import __version__

    parser = argparse.ArgumentParser(
        description="No-API-key skill description optimizer (uses `claude -p`)."
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    parser.add_argument(
        "--skill-path", required=True, help="Path to the skill dir (contains SKILL.md)"
    )
    parser.add_argument(
        "--eval-set", required=True, help="JSON: [{query, should_trigger}, ...]"
    )
    parser.add_argument(
        "--out", default=None, help="Output dir for run artifacts (default: a temp dir)"
    )
    parser.add_argument(
        "--models",
        default=None,
        help="Comma list of eval models (aliases haiku/sonnet/opus or full ids). "
        "Default: sonnet (or --model)",
    )
    parser.add_argument(
        "--improver-model",
        default=None,
        help="Model for the improver (alias or id). Default: opus (or --model)",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Single model id for BOTH eval and improver (skill-creator "
        "compatibility); --models/--improver-model take precedence",
    )
    parser.add_argument(
        "--description",
        default=None,
        help="Override the starting description instead of reading SKILL.md's",
    )
    parser.add_argument(
        "--improver-effort",
        default="high",
        help="Effort for improver (high/medium/low/none-> omit)",
    )
    parser.add_argument(
        "--repeats",
        "--runs-per-query",
        dest="repeats",
        type=int,
        default=3,
        help="Runs per (query, model)",
    )
    parser.add_argument(
        "--iterations", "--max-iterations", dest="iterations", type=int, default=5
    )
    parser.add_argument("--timeout", type=int, default=90)
    parser.add_argument(
        "--workers", "--num-workers", dest="workers", type=int, default=10
    )
    parser.add_argument(
        "--threshold",
        "--trigger-threshold",
        dest="threshold",
        type=float,
        default=0.5,
        help="Trigger-rate pass threshold",
    )
    parser.add_argument(
        "--test-frac",
        "--holdout",
        dest="test_frac",
        type=float,
        default=0.4,
        help="Held-out fraction (stratified by class), in [0, 1). 0 disables the "
        "holdout and selects on train; a positive fraction needs >=2 queries per "
        "class and must leave a train and a test member in each.",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="RNG seed for the stratified train/test split. Fixed by default so a run "
        "is reproducible; echoed in the output JSON so a run reproduces from its own "
        "record. Vary it to check split robustness.",
    )
    parser.add_argument(
        "--select-epsilon",
        type=float,
        default=0.05,
        help="Held-out mean differences within this band count as ties and are "
        "broken by the weakest-model (min) accuracy. 0 = strict mean-only selection.",
    )
    parser.add_argument(
        "--max-desc-chars",
        type=int,
        default=1024,
        help="Hard character budget for the description (default 1024). Over-budget "
        "candidates can never be selected, and --write refuses an over-budget winner.",
    )
    parser.add_argument(
        "--disable-plugin",
        action="append",
        default=[],
        help="Disable an installed plugin during eval so it can't out-compete the "
        "injected candidate, e.g. --disable-plugin astral@astral-sh (repeatable)",
    )
    parser.add_argument(
        "--report",
        default="auto",
        help="HTML report: 'auto' (temp file, opened in a browser), 'none' to disable, "
        "or an explicit output path.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate inputs (eval set, skill, holdout split, claude availability) and "
        "print the run plan as JSON to stdout (with an estimated claude -p call count), "
        "then exit without spending any tokens or writing artifacts.",
    )
    parser.add_argument(
        "--results-dir",
        default=None,
        help="Save results.json, report.html, and logs/ under a timestamped "
        "subdirectory here. Mutually exclusive with --out.",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="Write the best description back into SKILL.md (backs up to SKILL.md.bak)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable the detailed per-model, confusion-matrix, and per-query summaries "
        "(the compact one-line summaries are used otherwise)",
    )
    return parser


def _unwrap_eval_root(data: Any) -> list[Any]:
    """Return the query list from a bare list or a single-key wrapper object.

    A wrapper may carry unrelated metadata, which is ignored, but it must contain
    exactly one of the recognized ``queries`` / ``evals`` keys with a list value.

    Args:
        data: The decoded JSON root.

    Returns:
        The (still unvalidated) list of query items.

    Raises:
        ValueError: With an ``Invalid eval set: ...`` message if the root is neither a
            list nor an object, has neither or both recognized wrapper keys, or the
            recognized wrapper value is not a list.
    """
    if isinstance(data, list):
        return cast("list[Any]", data)
    if isinstance(data, dict):
        mapping = cast("dict[str, Any]", data)
        present = [key for key in ("queries", "evals") if key in mapping]
        if len(present) != 1:
            raise ValueError(
                "Invalid eval set: wrapper must contain exactly one of "
                "'queries' or 'evals'"
            )
        key = present[0]
        value = mapping[key]
        if not isinstance(value, list):
            raise ValueError(f"Invalid eval set: '{key}' must be a list")
        return cast("list[Any]", value)
    raise ValueError("Invalid eval set: root must be a list or wrapper object")


def _validate_eval_item(index: int, item: Any) -> None:
    """Validate one eval item's shape and field runtime types.

    Requires ``query`` to be a ``str`` and ``should_trigger`` to be exactly a ``bool``
    (integers and truthy values are not coerced). Extra keys are permitted.

    Args:
        index: Zero-based position of the item, used in the error message.
        item: The decoded item to validate.

    Raises:
        ValueError: With an ``Invalid eval set: item <index> ...`` message if the item
            is not an object or a required field is missing or of the wrong type.
    """
    if not isinstance(item, dict):
        raise ValueError(f"Invalid eval set: item {index} must be an object")
    mapping = cast("dict[str, Any]", item)
    if not isinstance(mapping.get("query"), str):
        raise ValueError(
            f"Invalid eval set: item {index} field 'query' must be a string"
        )
    if not isinstance(mapping.get("should_trigger"), bool):
        raise ValueError(
            f"Invalid eval set: item {index} field 'should_trigger' must be a boolean"
        )


def _load_eval_set(path: Path) -> list[EvalQuery]:
    """Load and validate the eval set, tolerating recognized wrappers and metadata.

    Accepts a bare JSON list, or an object with exactly one of ``queries`` / ``evals``
    (a list value; unrelated metadata is ignored). Every item must be an object with a
    string ``query`` and a boolean ``should_trigger``; item order, duplicate queries,
    and extra item keys are preserved. A read error (a missing or unreadable file) and
    invalid JSON are both mapped to a friendly ``Invalid eval set: ...`` message so a
    stdout-parsing caller fails legibly instead of on a raw traceback.

    Args:
        path: Path to the eval-set JSON file.

    Returns:
        The validated list of evaluation queries.

    Raises:
        ValueError: With an ``Invalid eval set: ...`` message when the file cannot be
            read, on invalid JSON, a bad root/wrapper shape, an empty list, or any item
            that violates the contract.
    """
    try:
        text = path.read_text()
    except OSError as exc:
        reason = exc.strerror or exc.__class__.__name__
        raise ValueError(f"Invalid eval set: cannot read {path}: {reason}") from exc
    try:
        data: Any = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("Invalid eval set: invalid JSON") from exc
    except RecursionError as exc:
        # A pathologically nested eval file overflows json's C-stack recursion guard,
        # surfacing as RecursionError (a RuntimeError, not JSONDecodeError). Map it to
        # the same friendly message so the precondition contract holds (fail legibly,
        # never a mid-run traceback). A separate clause -- not
        # ``except (json.JSONDecodeError, RecursionError)`` -- because ruff-format
        # rewrites that tuple into the invalid Py2 ``except A, B:`` syntax.
        raise ValueError("Invalid eval set: invalid JSON") from exc
    items = _unwrap_eval_root(data)
    if not items:
        raise ValueError("Invalid eval set: must contain at least one query")
    for index, item in enumerate(items):
        _validate_eval_item(index, item)
    return cast("list[EvalQuery]", items)


@dataclass(frozen=True, slots=True)
class _LoopInputs:
    """Static inputs to the improvement loop.

    Attributes:
        name: Skill name.
        body: Skill body, passed to the improver for context.
        config: Shared evaluation settings.
        improver_model: Resolved improver model id.
        effort: Improver reasoning effort, or ``None``.
        iterations: Maximum improve->re-eval rounds.
        timeout: Per-run eval timeout, in seconds.
        select_epsilon: Held-out tie-break band.
        out: Directory for per-iteration artifacts.
        verbose: Whether to emit the confusion/per-model verbose summaries.
        max_desc_chars: Hard character budget for the description.
    """

    name: str
    body: str
    config: EvalConfig
    improver_model: str
    effort: str | None
    iterations: int
    timeout: int
    select_epsilon: float
    out: Path
    verbose: bool = False
    max_desc_chars: int = 1024


@dataclass(frozen=True, slots=True)
class _LoopResult:
    """Outcome of the improvement loop.

    Attributes:
        best_desc: The selected best description.
        best_test: Its held-out mean accuracy, or ``None`` when the holdout is disabled.
        best_test_min: Its held-out weakest-model accuracy, or ``None`` with no holdout.
        history: Per-stage records (baseline plus each iteration), each with ``is_best``.
        best_train: Training-set view of the selected best description.
        best_test_eval: Held-out view of the selected best description (an empty,
            iterable-compatible result when the holdout is disabled).
        exit_reason: Why the loop stopped (``all_passed``/``max_iterations`` with the
            iteration number).
        iterations_run: Number of proposal slots entered (past the early-exit check),
            whether they produced a candidate or exhausted their retries.
        final_description: The last candidate evaluated (baseline if none).
        improver_failed_iterations: Bounded ledger of slots whose two outer improver
            attempts both failed retryably; empty when none did.
    """

    best_desc: str
    best_test: float | None
    best_test_min: float | None
    history: list[dict[str, Any]]
    best_train: EvalResult
    best_test_eval: EvalResult
    exit_reason: str
    iterations_run: int
    final_description: str
    improver_failed_iterations: list[dict[str, Any]]


@dataclass(frozen=True, slots=True)
class _RetryExhausted(Exception):
    """Both outer improver attempts for one slot failed retryably.

    An internal signal (never surfaced publicly): it carries the exact two typed
    retryable errors, distinguishing expected exhaustion from a missing or fatal
    outcome so exhaustion never masquerades as terminal.

    Attributes:
        errors: The two retryable errors, in attempt order.
    """

    errors: tuple[ImproverRetryableError, ImproverRetryableError]


def _improver_failure_record(
    iteration: int, exhausted: _RetryExhausted
) -> dict[str, Any]:
    """Build one public ``improver_failed_iterations`` record from an exhaustion.

    Every field is derived from validated retryable errors (allowlisted kinds and
    messages), so no raw diagnostic can reach this public record.

    Args:
        iteration: 1-based slot number that exhausted its retries.
        exhausted: The exhaustion signal carrying the two typed errors.

    Returns:
        A record with ``iteration``, ``attempt_count`` (always 2), and an ``errors``
        list of ``{attempt, kind, message}`` in ascending attempt order.
    """
    return {
        "iteration": iteration,
        "attempt_count": 2,
        "errors": [
            {
                "attempt": 1,
                "kind": exhausted.errors[0].kind,
                "message": exhausted.errors[0].message,
            },
            {
                "attempt": 2,
                "kind": exhausted.errors[1].kind,
                "message": exhausted.errors[1].message,
            },
        ],
    }


def _split_counts(per_query: list[PerQuery]) -> tuple[int, int, int, int]:
    """Count passed / failed / judged-total / unjudged queries (tri-state aware).

    Args:
        per_query: The per-query roll-ups to count.

    Returns:
        ``(passed, failed, judged_total, unjudged)`` where ``judged_total`` excludes
        unjudged (``all_pass is None``) queries.
    """
    passed = sum(pq["all_pass"] is True for pq in per_query)
    failed = sum(pq["all_pass"] is False for pq in per_query)
    unjudged = sum(pq["all_pass"] is None for pq in per_query)
    return passed, failed, passed + failed, unjudged


def _score_str(per_query: list[PerQuery]) -> str:
    """Render a ``"k/N"`` query-pass score with a judged-query denominator.

    Args:
        per_query: The per-query roll-ups to score.

    Returns:
        ``"k/N"`` (judged-and-passed over judged), with ``" (+u unjudged)"`` appended
        when any query was unjudged, or ``"n/a"`` when no query was judged.
    """
    passed, _, total, unjudged = _split_counts(per_query)
    if total == 0:
        return "n/a"
    suffix = f" (+{unjudged} unjudged)" if unjudged else ""
    return f"{passed}/{total}{suffix}"


def _result_entries(ev: EvalResult) -> list[dict[str, Any]]:
    """Flatten an eval result's per-query rows into report/history result entries.

    Each entry is keyed on the original positional ``index`` (dedup-safe) and sums
    ``triggers``/``runs``/``errors`` across models while keeping a per-model breakdown.

    Args:
        ev: The evaluation result to flatten.

    Returns:
        One entry per query: ``{index, query, should_trigger, triggers, runs, errors,
        pass, models}``.
    """
    entries: list[dict[str, Any]] = []
    for pq in ev["per_query"]:
        models = pq["models"]
        entries.append(
            {
                "index": pq["index"],
                "query": pq["query"],
                "should_trigger": pq["should_trigger"],
                "triggers": sum(models[m]["triggers"] for m in models),
                "runs": sum(models[m]["runs"] for m in models),
                "errors": sum(models[m]["errors"] for m in models),
                "pass": pq["all_pass"],
                "models": {
                    m: {"triggers": models[m]["triggers"], "runs": models[m]["runs"]}
                    for m in models
                },
            }
        )
    return entries


def _history_entry(
    iteration: int,
    description: str,
    rationale: str,
    full_mean: float,
    train_ev: EvalResult,
    test_ev: EvalResult,
    has_holdout: bool,
) -> dict[str, Any]:
    """Build one history record (baseline or an iteration) for the envelope + report.

    With no holdout, every held-out *measurement* field is ``None`` and ``test_results``
    is an empty list (the structural collection is retained so report/consumers stay
    iterable); ``test_ev`` is ignored in that case.

    Args:
        iteration: 0 for the baseline, 1..N for improve iterations.
        description: The description this record scored.
        rationale: The improver rationale (empty for the baseline).
        full_mean: Full-set mean accuracy for this description.
        train_ev: Training-set view of this description.
        test_ev: Held-out view of this description (used only when ``has_holdout``).
        has_holdout: Whether a held-out set exists; ``False`` nulls the test metrics.

    Returns:
        The history entry, with ``is_best`` defaulted to ``False`` (set post-loop).
    """
    tp, tf, tt, tu = _split_counts(train_ev["per_query"])
    if has_holdout:
        ep, ef, et, eu = _split_counts(test_ev["per_query"])
        test_mean: float | None = test_ev["mean_accuracy"]
        test_min: float | None = test_ev["min_accuracy"]
        test_passed: int | None = ep
        test_failed: int | None = ef
        test_total: int | None = et
        test_unjudged: int | None = eu
        test_score: str | None = _score_str(test_ev["per_query"])
        test_results = _result_entries(test_ev)
    else:
        test_mean = test_min = None
        test_passed = test_failed = test_total = test_unjudged = None
        test_score = None
        test_results = []
    return {
        "stage": "baseline" if iteration == 0 else f"iter{iteration}",
        "iteration": iteration,
        "description": description,
        "rationale": rationale,
        "chars": len(description),
        "full_mean": full_mean,
        "test_mean": test_mean,
        "test_min": test_min,
        "train_passed": tp,
        "train_failed": tf,
        "train_total": tt,
        "train_unjudged": tu,
        "test_passed": test_passed,
        "test_failed": test_failed,
        "test_total": test_total,
        "test_unjudged": test_unjudged,
        "train_score": _score_str(train_ev["per_query"]),
        "test_score": test_score,
        "train_results": _result_entries(train_ev),
        "test_results": test_results,
        "is_best": False,
    }


def _call_improver_with_retry(
    inputs: _LoopInputs, prompt: str, it: int, budget: _LaunchBudget
) -> dict[str, Any]:
    """Call the improver for one slot, retrying once on a typed retryable failure.

    Makes at most two outer attempts, each writing a distinct transcript. A fatal or
    unclassified exception from :func:`call_improver` propagates immediately.

    Args:
        inputs: Static loop inputs.
        prompt: The improver prompt.
        it: 1-based slot number, used for the transcript filenames.
        budget: Shared launch budget threaded to every child spawn.

    Returns:
        The accepted proposal object.

    Raises:
        _RetryExhausted: When both outer attempts fail with typed retryable errors.
    """
    log_names = (f"iter{it}_improve.json", f"iter{it}_improve_retry.json")
    errors: list[ImproverRetryableError] = []
    for attempt in (1, 2):
        try:
            return call_improver(
                prompt,
                inputs.improver_model,
                inputs.effort,
                max(inputs.timeout * 4, 300),
                max_chars=inputs.max_desc_chars,
                log_path=inputs.out / log_names[attempt - 1],
                budget=budget,
            )
        except ImproverRetryableError as exc:
            errors.append(exc)
            if attempt == 1:
                logger.warning("Improver retryable attempt 1 failed (%s).", exc.kind)
    raise _RetryExhausted((errors[0], errors[1]))


def _propose_and_score(
    inputs: _LoopInputs,
    best_desc: str,
    train_eval: EvalResult,
    prior_attempts: Sequence[ImproverAttempt],
    queries: list[EvalQuery],
    train_idx: list[int],
    test_idx: list[int],
    it: int,
    budget: _LaunchBudget,
) -> tuple[str, dict[str, Any], EvalResult, EvalResult, EvalResult]:
    """Propose a new description and score it, evaluating the full set exactly once.

    The candidate is evaluated once over the full eval set; the train and held-out
    views are then sliced from that single result with :func:`subset_result`, halving
    the ``claude -p`` call count versus separate train/test/full evaluations.

    Args:
        inputs: Static loop inputs.
        best_desc: The current best description to improve from.
        train_eval: Training-set view driving the improver.
        prior_attempts: Descriptions already attempted, with train-only results, so the
            improver avoids repeating them (held-out results are never included).
        queries: The full eval set.
        train_idx: Positional indices of the training queries.
        test_idx: Positional indices of the held-out queries.
        it: 1-based iteration number, used for artifact filenames.
        budget: Shared launch budget threaded to every improver child spawn.

    Returns:
        ``(candidate, proposal, full_eval, train_eval, test_eval)``.

    Raises:
        _RetryExhausted: When both outer improver attempts fail retryably.
    """
    prompt = build_improver_prompt(
        inputs.name, best_desc, inputs.body, train_eval, prior_attempts
    )
    (inputs.out / f"iter{it}_prompt.txt").write_text(prompt)
    proposal = _call_improver_with_retry(inputs, prompt, it, budget)
    cand = str(proposal["description"]).strip()
    (inputs.out / f"iter{it}_proposal.json").write_text(json.dumps(proposal, indent=2))
    logger.info("  rationale: %s", proposal.get("rationale", ""))
    cand_full = evaluate(queries, inputs.name, cand, inputs.config, verbose=False)
    cand_train = subset_result(cand_full, queries, train_idx, inputs.config.models)
    cand_test = subset_result(cand_full, queries, test_idx, inputs.config.models)
    (inputs.out / f"iter{it}_eval.json").write_text(json.dumps(cand_full, indent=2))
    label = f"ITER {it} (full)"
    if inputs.verbose:
        logger.info("%s", summarize_verbose(label, cand_full))
    else:
        logger.info("%s", summarize(label, cand_full))
    return cand, proposal, cand_full, cand_train, cand_test


def _candidate_wins(
    cand: str,
    cand_selection: EvalResult,
    best_desc: str,
    best_selection: EvalResult,
    inputs: _LoopInputs,
    score_label: str,
) -> tuple[bool, str]:
    """Decide whether a scored candidate should replace the incumbent.

    Applies the selection pre-condition (both the candidate's and the incumbent's
    selection views must be usable) before the mean/min/char-budget comparison. The
    selection view is the held-out result with a holdout, or the training result when
    the holdout is disabled; ``score_label`` names it in the reason strings.

    Args:
        cand: The candidate description.
        cand_selection: The candidate's selection-view evaluation.
        best_desc: The incumbent best description.
        best_selection: The incumbent's selection-view evaluation.
        inputs: Static loop inputs (epsilon, char budget).
        score_label: ``"held-out"`` with a holdout, ``"train"`` when it is disabled.

    Returns:
        ``(win, reason)`` from :func:`is_better_candidate`, or ``(False, ...)`` when the
        selection view is unusable for selection.
    """
    if not cand_selection["score_valid"] or not best_selection["score_valid"]:
        return False, f"{score_label} set unusable for selection"
    return is_better_candidate(
        cand_selection["mean_accuracy"],
        cand_selection["min_accuracy"],
        best_selection["mean_accuracy"],
        best_selection["min_accuracy"],
        inputs.select_epsilon,
        cand_chars=len(cand),
        best_chars=len(best_desc),
        max_chars=inputs.max_desc_chars,
        score_label=score_label,
    )


@dataclass(slots=True)
class _BestState:
    """Mutable running-best across the optimization loop.

    Attributes:
        desc: The current best description.
        full: Its full-set evaluation (sliced each iteration to drive the improver).
        train: Its training-set view.
        test_eval: Its held-out view (an empty result when the holdout is disabled).
        selection: Its selection view (held-out with a holdout, else train).
        history_idx: Index of its history entry (the row flagged ``is_best``).
    """

    desc: str
    full: EvalResult
    train: EvalResult
    test_eval: EvalResult
    selection: EvalResult
    history_idx: int


@dataclass(frozen=True, slots=True)
class _LoopContext:
    """Immutable per-run context shared across the improve loop.

    Attributes:
        inputs: Static loop inputs (models, improver, iteration budget, ...).
        queries: The full eval set.
        train_idx: Positional indices of the training queries.
        test_idx: Positional indices of the held-out queries.
        has_holdout: Whether a held-out set exists.
        score_label: ``"held-out"`` with a holdout, ``"train"`` otherwise.
        budget: Shared improver launch budget.
        emit: Optional live-report callback, or ``None``.
    """

    inputs: _LoopInputs
    queries: list[EvalQuery]
    train_idx: list[int]
    test_idx: list[int]
    has_holdout: bool
    score_label: str
    budget: _LaunchBudget
    emit: Callable[[list[dict[str, Any]], int, int, list[dict[str, Any]]], None] | None


def _log_selection_mean(
    has_holdout: bool, cand_selection: EvalResult, best_selection: EvalResult
) -> None:
    """Log the candidate's selection mean against the incumbent, per selection view.

    Chooses the label so the optional held-out float is never formatted for a
    no-holdout run.

    Args:
        has_holdout: Whether a held-out set exists.
        cand_selection: The candidate's selection-view evaluation.
        best_selection: The incumbent's selection-view evaluation.
    """
    label = "held-out test mean" if has_holdout else "train mean"
    logger.info(
        "  %s: %.3f (best so far %.3f)",
        label,
        cand_selection["mean_accuracy"],
        best_selection["mean_accuracy"],
    )


def _consider_candidate(
    scored: tuple[str, dict[str, Any], EvalResult, EvalResult, EvalResult],
    it: int,
    ctx: _LoopContext,
    best: _BestState,
    history: list[dict[str, Any]],
    attempts: list[ImproverAttempt],
) -> str:
    """Record a scored candidate in history and apply the selection decision.

    Appends the candidate's train-only attempt record and its history entry, then
    updates ``best`` in place when the candidate wins.

    Args:
        scored: ``(candidate, proposal, full_eval, train_eval, test_eval)``.
        it: 1-based iteration number.
        ctx: The shared per-run loop context.
        best: The running-best state, mutated in place on a win.
        history: The history list, appended to.
        attempts: The prior-attempt list, appended to.

    Returns:
        The candidate description (the run's new ``final_description``).
    """
    cand, proposal, cand_full, cand_train, cand_test = scored
    attempts.append({"description": cand, "train_results": cand_train["per_query"]})
    cand_selection = cand_test if ctx.has_holdout else cand_train
    _log_selection_mean(ctx.has_holdout, cand_selection, best.selection)
    history.append(
        _history_entry(
            it,
            cand,
            proposal.get("rationale", ""),
            cand_full["mean_accuracy"],
            cand_train,
            cand_test,
            ctx.has_holdout,
        )
    )
    win, reason = _candidate_wins(
        cand, cand_selection, best.desc, best.selection, ctx.inputs, ctx.score_label
    )
    if win:
        best.desc = cand
        best.full = cand_full
        best.train = cand_train
        best.test_eval = cand_test
        best.selection = cand_selection
        best.history_idx = len(history) - 1
        logger.info("  -> new best (%s)", reason)
    else:
        logger.info("  -> rejected (%s)", reason)
    return cand


def _run_improve_loop(
    ctx: _LoopContext,
    best: _BestState,
    history: list[dict[str, Any]],
    attempts: list[ImproverAttempt],
    improver_failed: list[dict[str, Any]],
) -> tuple[str, int, str]:
    """Run the improve iterations, mutating ``best``/``history``/``attempts``/ledger.

    Each slot gets at most two outer improver attempts; a slot whose retries both fail
    retryably records one bounded entry and the loop continues, while a fatal improver
    error propagates.

    Args:
        ctx: The shared per-run loop context.
        best: The running-best state, mutated in place.
        history: The history list, appended to.
        attempts: The prior-attempt list, appended to.
        improver_failed: The bounded failure ledger, appended to on exhaustion.

    Returns:
        ``(exit_reason, iterations_run, final_description)`` where ``iterations_run`` is
        the number of entered slots and ``final_description`` is the last candidate that
        was successfully proposed and scored (the baseline if none was).
    """
    models = ctx.inputs.config.models
    exit_reason = f"max_iterations ({ctx.inputs.iterations})"
    iterations_run = 0
    final_description = best.desc
    for it in range(1, ctx.inputs.iterations + 1):
        logger.info(
            "\n=== Iteration %d: improver=%s effort=%s ===",
            it,
            ctx.inputs.improver_model,
            ctx.inputs.effort,
        )
        train_eval = subset_result(best.full, ctx.queries, ctx.train_idx, models)
        if train_eval["per_query"] and all(
            pq["all_pass"] is True for pq in train_eval["per_query"]
        ):
            exit_reason = f"all_passed (iteration {it})"
            logger.info(
                "  all %d train queries pass; stopping early.",
                len(train_eval["per_query"]),
            )
            break
        # This slot is entered: count it before the first outer attempt, so the live
        # count and the failure ledger stay consistent even when a slot is skipped.
        iterations_run = it
        try:
            scored = _propose_and_score(
                ctx.inputs,
                best.desc,
                train_eval,
                attempts,
                ctx.queries,
                ctx.train_idx,
                ctx.test_idx,
                it,
                ctx.budget,
            )
        except _RetryExhausted as exhausted:
            # Preserve the last verified candidate and all prior state; record one
            # bounded public entry, refresh live state, and continue later slots.
            improver_failed.append(_improver_failure_record(it, exhausted))
            logger.warning(
                "Improver retry attempts exhausted for iteration %d; continuing.", it
            )
            if ctx.emit is not None:
                ctx.emit(history, best.history_idx, iterations_run, improver_failed)
            continue
        final_description = _consider_candidate(
            scored, it, ctx, best, history, attempts
        )
        if ctx.emit is not None:
            ctx.emit(history, best.history_idx, iterations_run, improver_failed)
    return exit_reason, iterations_run, final_description


def _optimize(
    inputs: _LoopInputs,
    train_idx: list[int],
    test_idx: list[int],
    queries: list[EvalQuery],
    base_desc: str,
    base_full: EvalResult,
    emit: Callable[[list[dict[str, Any]], int, int, list[dict[str, Any]]], None]
    | None = None,
) -> _LoopResult:
    """Run the improve->re-eval loop, selecting the best description by held-out score.

    Each description is evaluated once over the full set; the train view (used to drive
    the improver) and the held-out view (used for selection) are sliced from the
    current best's full result, so no set is re-evaluated redundantly. The iteration
    budget is validated first, and a shared 200-launch budget bounds all improver
    children before any slot runs.

    Args:
        inputs: Static loop inputs (models, improver, iteration budget, ...).
        train_idx: Positional indices of the training queries.
        test_idx: Positional indices of the held-out queries.
        queries: The full eval set.
        base_desc: The starting description.
        base_full: Full-set evaluation of ``base_desc``.
        emit: Optional callback ``(history, best_history_idx, iterations_run,
            improver_failed_iterations)`` invoked after every scored candidate and every
            exhausted slot to refresh a live report.

    Returns:
        The best description found and its held-out scores, plus per-stage history and
        the bounded improver-failure ledger.
    """
    _validate_iterations(inputs.iterations)
    models = inputs.config.models
    has_holdout = bool(test_idx)
    base_test = subset_result(base_full, queries, test_idx, models)
    base_train = subset_result(base_full, queries, train_idx, models)
    # Selection view: the held-out result with a holdout, else the training result.
    best = _BestState(
        desc=base_desc,
        full=base_full,
        train=base_train,
        test_eval=base_test,
        selection=base_test if has_holdout else base_train,
        history_idx=0,
    )
    history: list[dict[str, Any]] = [
        _history_entry(
            0,
            base_desc,
            "",
            base_full["mean_accuracy"],
            base_train,
            base_test,
            has_holdout,
        )
    ]
    # Train-only attempt records (blinding: held-out results never reach the improver).
    attempts: list[ImproverAttempt] = [
        {"description": base_desc, "train_results": base_train["per_query"]}
    ]
    improver_failed: list[dict[str, Any]] = []
    ctx = _LoopContext(
        inputs=inputs,
        queries=queries,
        train_idx=train_idx,
        test_idx=test_idx,
        has_holdout=has_holdout,
        score_label="held-out" if has_holdout else "train",
        # 50 slots * 2 outer attempts * 2 children (initial + shortening) = 200. Shared
        # across the whole run so no 201st improver child can start.
        budget=_LaunchBudget(MAX_ITERATIONS * 2 * 2),
        emit=emit,
    )
    exit_reason, iterations_run, final_description = _run_improve_loop(
        ctx, best, history, attempts, improver_failed
    )

    for i, entry in enumerate(history):
        entry["is_best"] = i == best.history_idx

    best_test = best.test_eval["mean_accuracy"] if has_holdout else None
    best_test_min = best.test_eval["min_accuracy"] if has_holdout else None
    return _LoopResult(
        best.desc,
        best_test,
        best_test_min,
        history,
        best.train,
        best.test_eval,
        exit_reason,
        iterations_run,
        final_description,
        improver_failed,
    )


def _resolve_config(args: argparse.Namespace) -> tuple[str, EvalConfig]:
    """Resolve the improver model and the shared :class:`EvalConfig` from CLI args.

    Args:
        args: Parsed CLI arguments.

    Returns:
        ``(improver_model, config)``.
    """
    eval_spec: str = args.models or args.model or "sonnet"
    improver_spec: str = args.improver_model or args.model or "opus"
    improver_model = MODEL_ALIASES.get(improver_spec, improver_spec)
    settings_json = (
        json.dumps({"enabledPlugins": {p: False for p in args.disable_plugin}})
        if args.disable_plugin
        else None
    )
    config = EvalConfig(
        models=tuple(resolve_models(eval_spec)),
        repeats=args.repeats,
        timeout=args.timeout,
        workers=args.workers,
        threshold=args.threshold,
        settings_json=settings_json,
    )
    return improver_model, config


def _build_plan(
    name: str,
    queries: list[EvalQuery],
    train_idx: list[int],
    test_idx: list[int],
    config: EvalConfig,
    iterations: int,
    improver_model: str,
    effort: str | None,
    holdout: float,
    seed: int,
) -> dict[str, Any]:
    """Build the machine-readable run plan (also the ``--dry-run`` payload).

    ``estimated_eval_calls`` is an upper bound: it assumes every improve iteration runs
    (the loop exits early once all train queries pass) and counts the baseline plus one
    full-set evaluation per iteration. ``estimated_improver_calls`` is the typical one
    call per iteration (a call may retry, bounded by the launch budget). A consumer can
    read ``estimated_claude_calls`` to budget a run before spending any tokens.

    Args:
        name: Skill name.
        queries: The full eval set.
        train_idx: Positional indices of the training queries.
        test_idx: Positional indices of the held-out queries.
        config: Resolved evaluation settings (models, repeats, threshold, ...).
        iterations: Maximum improve->re-eval rounds.
        improver_model: Resolved improver model id.
        effort: Improver reasoning effort, or ``None``.
        holdout: The held-out fraction (``--test-frac``).
        seed: Split RNG seed.

    Returns:
        The plan dict: a resolved-config echo plus ``estimated_eval_calls`` /
        ``estimated_improver_calls`` / ``estimated_claude_calls``.
    """
    n = len(queries)
    eval_calls = n * len(config.models) * config.repeats * (iterations + 1)
    return {
        "skill": name,
        "queries": n,
        "train_size": len(train_idx),
        "test_size": len(test_idx),
        "holdout": holdout,
        "seed": seed,
        "models": list(config.models),
        "improver_model": improver_model,
        "improver_effort": effort,
        "repeats": config.repeats,
        "iterations": iterations,
        "threshold": config.threshold,
        "estimated_eval_calls": eval_calls,
        "estimated_improver_calls": iterations,
        "estimated_claude_calls": eval_calls + iterations,
    }


def _plan_summary(plan: dict[str, Any]) -> str:
    """Render a one-line stderr summary of a run plan.

    Args:
        plan: A plan dict from :func:`_build_plan`.

    Returns:
        A compact ``Plan: ...`` line naming the estimate, split sizes, and seed.
    """
    return (
        f"Plan: {plan['queries']} queries x {len(plan['models'])} model(s) x "
        f"{plan['repeats']} repeats over <={plan['iterations'] + 1} evals "
        f"~= {plan['estimated_claude_calls']} claude calls "
        f"(train={plan['train_size']} test={plan['test_size']} seed={plan['seed']})"
    )


_PLACEHOLDER_HTML = (
    "<html><body><h1>Starting optimization loop…</h1>"
    "<meta http-equiv='refresh' content='5'></body></html>"
)


def _report_paths(
    args: argparse.Namespace, skill_name: str, timestamp: str
) -> tuple[Path, Path | None, Path | None]:
    """Resolve the artifact dir, results dir, and live-report path from the flags.

    Args:
        args: Parsed CLI arguments.
        skill_name: Skill name, used in the auto report filename.
        timestamp: Run timestamp for the results-dir / auto-report name.

    Returns:
        ``(out, results_dir, live_report_path)`` where ``out`` is the per-iteration
        artifact dir (created), ``results_dir`` is the timestamped results dir or
        ``None``, and ``live_report_path`` is where the HTML report is written or
        ``None`` (``--report none``). The mutually-exclusive ``--out`` / ``--results-dir``
        combination is rejected earlier, in :func:`run`'s preflight.
    """
    results_dir: Path | None = None
    if args.results_dir:
        results_dir = Path(args.results_dir) / timestamp
        out = results_dir / "logs"
    elif args.out:
        out = Path(args.out)
    else:
        out = Path(tempfile.mkdtemp(prefix="skilldesc-"))
    out.mkdir(parents=True, exist_ok=True)

    if args.report == "none":
        live: Path | None = None
    elif args.report == "auto":
        live = (
            results_dir / "report.html"
            if results_dir is not None
            # ``skill_name`` is attacker-controlled SKILL.md frontmatter; sanitize it
            # before it becomes a filename so a hostile name cannot traverse out of the
            # temp dir when writing the auto report.
            else Path(tempfile.gettempdir())
            / f"skill_description_report_{safe_name_token(skill_name)}_{timestamp}.html"
        )
    else:
        live = Path(args.report)
    return out, results_dir, live


def _write_placeholder_and_open(live_report_path: Path) -> None:
    """Write the initial placeholder report and open it in a browser (best-effort).

    The browser open is wrapped so a headless/CI environment (no browser) never aborts
    the run.

    Args:
        live_report_path: Where to write the placeholder HTML.
    """
    try:
        live_report_path.parent.mkdir(parents=True, exist_ok=True)
        live_report_path.write_text(_PLACEHOLDER_HTML)
    except OSError:
        logger.debug("could not write placeholder report", exc_info=True)
        return
    try:
        webbrowser.open(live_report_path.resolve().as_uri())
    except Exception:  # noqa: BLE001 - no browser in headless/CI; never fatal
        logger.debug("could not open browser for report", exc_info=True)


def _write_html(path: Path, report: dict[str, Any], name: str, refresh: bool) -> None:
    """Render and write an HTML report, swallowing write errors (headless/CI safe).

    Args:
        path: Destination HTML path.
        report: The report dict passed to :func:`generate_html`.
        name: Skill name for the report title.
        refresh: Whether to embed the auto-refresh meta tag.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(generate_html(report, auto_refresh=refresh, skill_name=name))
    except OSError:
        logger.debug("could not write HTML report to %s", path, exc_info=True)


def run(args: argparse.Namespace) -> None:
    """Run the optimization loop described by parsed CLI arguments.

    With ``--dry-run`` the run stops after preflight: it prints the ``{"dry_run": true,
    ...}`` plan JSON (including ``estimated_claude_calls``) to stdout and returns without
    evaluating, spending tokens, or writing artifacts.

    Args:
        args: Parsed arguments from :func:`build_parser`.

    Raises:
        SystemExit: If no ``SKILL.md`` exists at ``--skill-path``; if both ``--out`` and
            ``--results-dir`` are supplied; if ``--iterations``, the eval set, or the
            holdout split is invalid; or if the ``claude`` CLI is not found or not
            executable.
    """
    skill_md = Path(args.skill_path) / "SKILL.md"
    if not skill_md.exists():
        raise SystemExit(f"No SKILL.md at {skill_md}")
    name, base_desc, body = parse_skill_md(skill_md)
    name = name or Path(args.skill_path).name
    if args.description:
        base_desc = args.description

    # Complete input preflight before any config/path/report/browser/evaluator side
    # effect, so invalid user input is cheap and artifact-free (findings 3, 4, 8).
    try:
        _validate_iterations(args.iterations)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    try:
        queries = _load_eval_set(Path(args.eval_set))
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    if args.out and args.results_dir:
        raise SystemExit("--out and --results-dir are mutually exclusive")
    try:
        train_idx, test_idx = stratified_split(queries, args.test_frac, seed=args.seed)
    except ValueError as exc:
        raise SystemExit(f"Invalid holdout split: {exc}") from exc
    # Both loop halves shell out to ``claude -p``; verify the CLI is invocable up front
    # so a missing/non-executable binary fails as one legible setup error here (like the
    # SKILL.md and eval-set checks) rather than as a mid-run FileNotFoundError traceback
    # or a silent all-unjudged "success" when --iterations is 0.
    if not claude_available():
        raise SystemExit(
            f"claude CLI not found or not executable: {claude_bin()!r}. Install it and "
            "log in, or set SKILL_OPTIMIZER_CLAUDE_BIN to the CLI path."
        )

    improver_model, config = _resolve_config(args)
    effort = None if args.improver_effort.lower() == "none" else args.improver_effort

    # Machine-readable run plan: the --dry-run payload and, on a real run, a startup
    # estimate a consumer can budget against. Built only after preflight, so it always
    # describes a runnable config.
    plan = _build_plan(
        name,
        queries,
        train_idx,
        test_idx,
        config,
        args.iterations,
        improver_model,
        effort,
        args.test_frac,
        args.seed,
    )
    if args.dry_run:
        logger.info("Dry run (no tokens spent, no artifacts). %s", _plan_summary(plan))
        print(json.dumps({"dry_run": True, **plan}, indent=2))
        return
    logger.info("%s", _plan_summary(plan))

    timestamp = time.strftime("%Y-%m-%d_%H%M%S")
    out, results_dir, live_report_path = _report_paths(args, name, timestamp)
    if not args.out and not args.results_dir:
        logger.info("No --out/--results-dir given; writing run artifacts to %s", out)
    if live_report_path is not None:
        _write_placeholder_and_open(live_report_path)

    logger.info(
        "Skill '%s': %d queries (train=%d, test=%d), models=%s, repeats=%d",
        name,
        len(queries),
        len(train_idx),
        len(test_idx),
        list(config.models),
        config.repeats,
    )

    base_full = evaluate(queries, name, base_desc, config)
    (out / "baseline.json").write_text(json.dumps(base_full, indent=2))
    if args.verbose:
        logger.info("%s", summarize_verbose("BASELINE (full)", base_full))
    else:
        logger.info("%s", summarize("BASELINE (full)", base_full))

    inputs = _LoopInputs(
        name=name,
        body=body,
        config=config,
        improver_model=improver_model,
        effort=effort,
        iterations=args.iterations,
        timeout=args.timeout,
        select_epsilon=args.select_epsilon,
        out=out,
        verbose=args.verbose,
        max_desc_chars=args.max_desc_chars,
    )

    def _emit_live(
        history: list[dict[str, Any]],
        best_idx: int,
        iterations_run: int,
        improver_failed: list[dict[str, Any]],
    ) -> None:
        # Defensive/type-narrowing guard: ``_emit_live`` is only wired into the loop when
        # ``live_report_path`` is not None (see the ``emit=`` argument below), so this
        # early return is unreachable at runtime -- it is kept solely so the report
        # writes further down narrow ``Path | None`` to ``Path`` for the type checker.
        if live_report_path is None:  # pragma: no cover - unreachable; narrows the type
            return
        marked = [{**h, "is_best": i == best_idx} for i, h in enumerate(history)]
        best = marked[best_idx]
        live = {
            "original_description": base_desc,
            "best_description": best["description"],
            "best_score": best["test_score"] if test_idx else best["train_score"],
            "best_train_score": best["train_score"],
            "best_test_score": best["test_score"] if test_idx else None,
            # Explicit entered-slot count: a skipped (exhausted) slot has no history row,
            # so this must not be derived from len(history) - 1.
            "iterations_run": iterations_run,
            "holdout": args.test_frac,
            "train_size": len(train_idx),
            "test_size": len(test_idx),
            "history": marked,
            # Copy so a later append cannot mutate this emitted snapshot.
            "improver_failed_iterations": list(improver_failed),
        }
        _write_html(live_report_path, live, name, refresh=True)

    result = _optimize(
        inputs,
        train_idx,
        test_idx,
        queries,
        base_desc,
        base_full,
        emit=_emit_live if live_report_path is not None else None,
    )

    best_train_score = _score_str(result.best_train["per_query"])
    best_test_score = (
        _score_str(result.best_test_eval["per_query"]) if test_idx else None
    )
    report: dict[str, Any] = {
        "skill": name,
        "skill_path": str(args.skill_path),
        "models": list(config.models),
        "improver": {"model": improver_model, "effort": effort},
        "baseline_description": base_desc,
        "best_description": result.best_desc,
        "best_test_mean": result.best_test,
        "best_test_min": result.best_test_min,
        "select_epsilon": args.select_epsilon,
        "history": result.history,
        # Additive skill-creator envelope (nothing above is removed).
        "original_description": base_desc,
        "final_description": result.final_description,
        "exit_reason": result.exit_reason,
        "iterations_run": result.iterations_run,
        "improver_failed_iterations": result.improver_failed_iterations,
        "holdout": args.test_frac,
        "seed": args.seed,
        "estimated_claude_calls": plan["estimated_claude_calls"],
        "train_size": len(train_idx),
        "test_size": len(test_idx),
        "baseline_chars": len(base_desc),
        "best_chars": len(result.best_desc),
        "best_train_score": best_train_score,
        "best_test_score": best_test_score,
        "best_score": best_test_score if test_idx else best_train_score,
    }
    (out / "report.json").write_text(json.dumps(report, indent=2))
    if results_dir is not None:
        (results_dir / "results.json").write_text(json.dumps(report, indent=2))
    logger.info("\n=== DONE ===")
    if test_idx:
        logger.info(
            "Best held-out mean: %.3f (%s)", result.best_test, result.exit_reason
        )
    else:
        logger.info(
            "Best train mean: %.3f (%s)",
            result.best_train["mean_accuracy"],
            result.exit_reason,
        )
    logger.info("\nBEST DESCRIPTION:\n%s\n", result.best_desc)
    logger.info("Report: %s", out / "report.json")
    # Machine-readable result on stdout (stderr carries progress) — consumers read
    # `best_description` from this, matching skill-creator's run_loop contract.
    print(json.dumps(report, indent=2))

    if live_report_path is not None:
        _write_html(live_report_path, report, name, refresh=False)
    if results_dir is not None and args.report != "none":
        report_html = results_dir / "report.html"
        if live_report_path != report_html:
            _write_html(report_html, report, name, refresh=False)

    over_budget = len(result.best_desc) > args.max_desc_chars
    if args.write and over_budget:
        logger.warning(
            "Refusing --write: best description is %d chars, over the %d-char budget; "
            "%s left unchanged.",
            len(result.best_desc),
            args.max_desc_chars,
            skill_md,
        )
    elif args.write and result.best_desc.strip() != base_desc.strip():
        write_description(skill_md, result.best_desc)
        logger.info(
            "Wrote best description into %s (backup at %s.bak)", skill_md, skill_md
        )
    elif args.write:
        logger.info("No change to write (best == baseline).")


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: parse arguments, configure logging, and run.

    Catch :class:`ImproverFatalProcessError` (a fatal improver failure — a completed
    nonzero child exit or launch-budget exhaustion, which :func:`run` raises by design)
    and present it as one ``stderr`` line with exit code 1, no traceback, and no stdout,
    so a stdout-parsing caller fails legibly. The raw child returncode/stderr remain in
    the per-iteration improver transcript.

    Args:
        argv: Argument vector (defaults to ``sys.argv[1:]``).

    Returns:
        Process exit code (``0`` on success, ``1`` on a fatal improver failure).
    """
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(message)s")
    args = build_parser().parse_args(argv)
    try:
        run(args)
    except ImproverFatalProcessError as exc:
        logger.error("Improver failed fatally: %s (no result written).", exc)
        return 1
    return 0
