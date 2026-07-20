"""Triggering evaluation: drive ``claude -p`` per query and aggregate results."""

import contextlib
import copy
import json
import logging
import os
import select
import subprocess
import tempfile
import time
import uuid
from collections.abc import Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from skill_optimizer._process import claude_bin, subprocess_env
from skill_optimizer.interpreter import (
    _interpret_events_status,  # pyright: ignore[reportPrivateUsage]
)
from skill_optimizer.models import (
    ConfusionMatrix,
    EvalConfig,
    EvalQuery,
    EvalResult,
    ModelResult,
    PerQuery,
)
from skill_optimizer.skill_md import safe_name_token

logger = logging.getLogger(__name__)

_READ_CHUNK = 8192


def _stream_events(
    proc: subprocess.Popen[bytes], timeout: float
) -> Iterator[dict[str, Any]]:
    """Yield parsed stream-json events from a running ``claude -p`` process.

    Reads stdout incrementally and yields one decoded JSON object per complete line,
    skipping blanks and undecodable lines, until ``timeout`` elapses or the process
    exits with a drained buffer. Pairs with :func:`interpret_events`, which may stop
    consuming early once the outcome is decided.

    Args:
        proc: The running subprocess, with ``stdout`` piped as bytes.
        timeout: Wall-clock budget in seconds.

    Yields:
        Decoded stream-json event objects, in order.
    """
    # Type-narrowing, not a runtime guard: proc is always constructed with
    # stdout=PIPE, which guarantees a non-None stream.
    assert proc.stdout is not None  # noqa: S101 # nosec B101
    start = time.monotonic()
    buffer = ""
    while time.monotonic() - start < timeout:
        if proc.poll() is None:
            ready, _, _ = select.select([proc.stdout], [], [], 1.0)
            if not ready:
                continue
            if chunk := os.read(proc.stdout.fileno(), _READ_CHUNK):
                buffer += chunk.decode("utf-8", "replace")
        elif remainder := proc.stdout.read():
            buffer += remainder.decode("utf-8", "replace")
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue
        if proc.poll() is not None and "\n" not in buffer:
            break
    if line := buffer.strip():
        with contextlib.suppress(json.JSONDecodeError):
            yield json.loads(line)


def run_single_query(
    query: str,
    skill_name: str,
    description: str,
    model: str | None,
    timeout: int,
    settings_json: str | None,
) -> bool | None:
    """Run one eval query and report whether the model invoked the candidate skill.

    Injects ``description`` as a temporary slash-command in a throwaway project, runs
    ``claude -p`` against ``query``, and inspects the streamed tool-call intent.

    Args:
        query: The task to send to the model.
        skill_name: Name of the skill (used to build the candidate command name).
        description: The candidate description to inject.
        model: Model id, or ``None`` to use the CLI default.
        timeout: Per-run wall-clock budget, in seconds.
        settings_json: Optional ``--settings`` JSON blob.

    Returns:
        ``True`` if the model decided to invoke the injected command, ``False`` on a
        decisive non-trigger, or ``None`` when the probe is unjudgeable (timeout, a
        non-zero ``claude -p`` exit, or an empty/incomplete stream with no decisive
        terminal event) so callers can exclude it rather than score it as a miss.
    """
    rid = uuid.uuid4().hex[:8]
    # ``skill_name`` comes from attacker-controlled SKILL.md frontmatter; sanitize it to
    # a path-safe token before it becomes the slash-command filename, so a hostile name
    # (``/abs/path``, ``../..``) cannot escape the throwaway project dir. The same token
    # is the command name detected in the stream, so filename and detection stay aligned.
    cmd_name = f"{safe_name_token(skill_name)}-cand-{rid}"
    with tempfile.TemporaryDirectory(
        prefix=f"skilleval-{rid}-", ignore_cleanup_errors=True
    ) as tmp:
        commands_dir = Path(tmp) / ".claude" / "commands"
        commands_dir.mkdir(parents=True, exist_ok=True)
        indented = "\n  ".join(description.split("\n"))
        (commands_dir / f"{cmd_name}.md").write_text(
            f"---\ndescription: |\n  {indented}\n---\n\n"
            f"# {skill_name}\n\nThis skill handles: {description}\n",
            encoding="utf-8",
        )
        cmd = [
            claude_bin(),
            "-p",
            query,
            "--output-format",
            "stream-json",
            "--verbose",
            "--include-partial-messages",
        ]
        if model:
            cmd += ["--model", model]
        if settings_json:
            cmd += ["--settings", settings_json]
        # cmd is a fixed argv list built from internal constants and CLI-provided
        # model/settings strings, never a shell string -- no shell=True, no injection
        # surface.
        proc = subprocess.Popen(  # noqa: S603 # nosec B603
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd=tmp,
            env=subprocess_env(),
        )
        try:
            # A decisive terminal event (True/False) wins regardless of exit code; a
            # ``None`` means no decisive event was seen (timeout, empty/incomplete
            # output, or a failed process) -> the probe is unjudgeable, not a genuine
            # non-trigger, so it propagates as ``None``.
            return _interpret_events_status(_stream_events(proc, timeout), cmd_name)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()


def run_query_with_retry(
    query: str,
    skill_name: str,
    description: str,
    model: str | None,
    timeout: int,
    settings_json: str | None,
    retries: int = 1,
) -> bool | None:
    """Probe triggering, retrying unjudgeable (``None``) probes up to ``retries`` times.

    A one-off timeout or CLI hiccup returns ``None`` from :func:`run_single_query`;
    retrying keeps a transient failure from being recorded (and later scored) as a
    non-trigger. A decisive ``True``/``False`` is returned immediately.

    Args:
        query: The task to send to the model.
        skill_name: Name of the skill (used to build the candidate command name).
        description: The candidate description to inject.
        model: Model id, or ``None`` to use the CLI default.
        timeout: Per-run wall-clock budget, in seconds.
        settings_json: Optional ``--settings`` JSON blob.
        retries: Extra attempts after the first when a probe is unjudgeable.

    Returns:
        ``True``/``False`` from the first decisive probe, or ``None`` if every attempt
        was unjudgeable.
    """
    for _ in range(retries + 1):
        result = run_single_query(
            query, skill_name, description, model, timeout, settings_json
        )
        if result is not None:
            return result
    return None


def _confusion_matrix(tp: int, fp: int, tn: int, fn: int) -> ConfusionMatrix:
    """Build a :class:`ConfusionMatrix` from raw counts.

    Args:
        tp: True positives.
        fp: False positives.
        tn: True negatives.
        fn: False negatives.

    Returns:
        The confusion matrix with precision/recall/accuracy, guarding divide-by-zero
        the same way skill-creator does (empty predicted/expected positives ⇒ ``1.0``).
    """
    total = tp + fp + tn + fn
    precision = tp / (tp + fp) if (tp + fp) > 0 else 1.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 1.0
    accuracy = (tp + tn) / total if total > 0 else 0.0
    return {
        "tp": tp,
        "fp": fp,
        "tn": tn,
        "fn": fn,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "accuracy": round(accuracy, 4),
    }


def aggregate(
    raw: Mapping[tuple[int, str], list[bool]],
    eval_set: list[EvalQuery],
    models: tuple[str, ...],
    threshold: float,
    description: str,
    errors: Mapping[tuple[int, str], int] | None = None,
) -> EvalResult:
    """Roll raw per-run trigger outcomes up into accuracy scores.

    Cells with no judged runs (every probe was unjudgeable) are left ``pass=None`` and
    excluded from the accuracy denominators, so a transient CLI outage does not read as
    a wave of misses. Accuracy is meaned/mined only over models that have at least one
    judged query; if no model does, ``score_valid`` is ``False``.

    Args:
        raw: Trigger booleans keyed by ``(query_index, model)``.
        eval_set: The queries, indexed positionally to match ``raw`` keys.
        models: Models that were evaluated.
        threshold: Trigger-rate at or above which a query counts as triggered.
        description: The description these results belong to (echoed in the output).
        errors: Per-cell unjudged-probe counts keyed by ``(query_index, model)``.

    Returns:
        The aggregated :class:`EvalResult`.
    """
    err_map = errors or {}
    per_query: list[PerQuery] = []
    for i, q in enumerate(eval_set):
        md: dict[str, ModelResult] = {}
        for m in models:
            trig = raw.get((i, m), [])
            n = len(trig)
            triggers = sum(trig)
            if n:
                rate = triggers / n
                passed = (
                    (rate >= threshold) if q["should_trigger"] else (rate < threshold)
                )
            else:
                # Every probe for this cell was unjudgeable -> leave it out of scoring.
                rate, passed = 0.0, None
            md[m] = {
                "trigger_rate": round(rate, 3),
                "pass": passed,
                "triggers": triggers,
                "runs": n,
                "errors": err_map.get((i, m), 0),
            }
        judged = [md[m]["pass"] for m in models if md[m]["pass"] is not None]
        per_query.append(
            {
                "index": i,
                "query": q["query"],
                "should_trigger": q["should_trigger"],
                "models": md,
                # None when every model was unjudged: neither a decisive pass nor a
                # decisive failure, so it must not read as a falsey miss downstream.
                "all_pass": all(judged) if judged else None,
            }
        )
    return _rollup_stats(description, per_query, models)


def _rollup_stats(
    description: str, per_query: list[PerQuery], models: tuple[str, ...]
) -> EvalResult:
    """Roll per-query model outcomes up into accuracy, confusion, and error totals.

    Shared by :func:`aggregate` (from raw probes) and :func:`subset_result` (from a
    sliced copy) so both derive identical stats from the tri-state ``per_query`` data.

    Args:
        description: The description these results belong to.
        per_query: The per-query roll-ups to summarize (owned by the caller).
        models: The evaluated model ids.

    Returns:
        The aggregated :class:`EvalResult`.
    """
    per_model_correct = dict.fromkeys(models, 0)
    per_model_total = dict.fromkeys(models, 0)
    conf = {m: {"tp": 0, "fp": 0, "tn": 0, "fn": 0} for m in models}
    total_errors = 0
    unjudged = 0
    for pq in per_query:
        for m in models:
            mr = pq["models"][m]
            total_errors += mr["errors"]
            if mr["pass"] is None:
                unjudged += 1
                continue
            per_model_total[m] += 1
            per_model_correct[m] += int(mr["pass"])
            if pq["should_trigger"]:
                conf[m]["tp"] += mr["triggers"]
                conf[m]["fn"] += mr["runs"] - mr["triggers"]
            else:
                conf[m]["fp"] += mr["triggers"]
                conf[m]["tn"] += mr["runs"] - mr["triggers"]
    per_model_acc: dict[str, float | None] = {
        m: (round(per_model_correct[m] / per_model_total[m], 4))
        if per_model_total[m]
        else None
        for m in models
    }
    if judged_accs := [a for a in per_model_acc.values() if a is not None]:
        mean_acc = round(sum(judged_accs) / len(judged_accs), 4)
        min_acc = round(min(judged_accs), 4)
        score_valid = True
    else:
        mean_acc = min_acc = 0.0
        score_valid = False
    per_model_confusion = {
        m: _confusion_matrix(conf[m]["tp"], conf[m]["fp"], conf[m]["tn"], conf[m]["fn"])
        for m in models
    }
    agg = _confusion_matrix(
        sum(c["tp"] for c in conf.values()),
        sum(c["fp"] for c in conf.values()),
        sum(c["tn"] for c in conf.values()),
        sum(c["fn"] for c in conf.values()),
    )
    return {
        "description": description,
        "per_model_accuracy": per_model_acc,
        "mean_accuracy": mean_acc,
        "min_accuracy": min_acc,
        "per_query": per_query,
        "errors": total_errors,
        "unjudged": unjudged,
        "score_valid": score_valid,
        "confusion": agg,
        "per_model_confusion": per_model_confusion,
    }


def subset_result(
    full_eval: EvalResult,
    eval_set: list[EvalQuery],
    indices: list[int],
    models: tuple[str, ...],
) -> EvalResult:
    """Derive a train/test view of one full-set evaluation, sliced by index.

    Deep-copies the :data:`PerQuery` entries at ``indices`` (they are mutable, so a
    shallow slice would let train/test/history views cross-mutate) and recomputes every
    stat from the copies, honoring the tri-state contract (unjudged cells excluded).
    Slicing by positional index — never query text — preserves the dedup invariant.

    Args:
        full_eval: The full-set evaluation to slice.
        eval_set: The full eval set; its length must match ``full_eval["per_query"]``.
        indices: Original positional indices to include (unique, in range).
        models: The evaluated model ids.

    Returns:
        An :class:`EvalResult` over just the selected queries.

    Raises:
        ValueError: If the per-query length disagrees with ``eval_set``, or an index is
            out of range or duplicated.
    """
    n = len(eval_set)
    if len(full_eval["per_query"]) != n:
        raise ValueError(
            f"per_query length {len(full_eval['per_query'])} != eval_set length {n}"
        )
    seen: set[int] = set()
    for i in indices:
        if not 0 <= i < n:
            raise ValueError(f"index {i} out of range [0, {n})")
        if i in seen:
            raise ValueError(f"duplicate index {i}")
        seen.add(i)
    per_query = [copy.deepcopy(full_eval["per_query"][i]) for i in indices]
    return _rollup_stats(full_eval["description"], per_query, models)


def evaluate(
    eval_set: list[EvalQuery],
    skill_name: str,
    description: str,
    config: EvalConfig,
    *,
    verbose: bool = True,
) -> EvalResult:
    """Evaluate one description across the eval set and models, concurrently.

    Args:
        eval_set: Queries to evaluate.
        skill_name: Name of the skill under test.
        description: The description to evaluate.
        config: Shared run settings (models, repeats, timeout, workers, threshold).
        verbose: Whether to log periodic progress.

    Returns:
        The aggregated :class:`EvalResult`.
    """
    jobs = [
        (i, q, m)
        for i, q in enumerate(eval_set)
        for m in config.models
        for _ in range(config.repeats)
    ]
    raw: dict[tuple[int, str], list[bool]] = {}
    errors: dict[tuple[int, str], int] = {}
    with ThreadPoolExecutor(max_workers=config.workers) as executor:
        # Bucket by the query's positional index -- the same index aggregate()
        # uses via enumerate(eval_set). (The original used eval_set.index(q),
        # which is O(n^2) and mis-buckets exact-duplicate query dicts to the first
        # match; positional indexing matches skill-creator's unique-query model and
        # reports each entry's true rate.)
        futures = {
            executor.submit(
                run_query_with_retry,
                q["query"],
                skill_name,
                description,
                m,
                config.timeout,
                config.settings_json,
            ): (i, m)
            for i, q, m in jobs
        }
        for done, fut in enumerate(as_completed(futures), start=1):
            i, m = futures[fut]
            try:
                result = fut.result()
            except Exception:  # noqa: BLE001 - task boundary: logged and converted to None
                logger.warning("query %d (%s) failed", i, m, exc_info=True)
                result = None
            # A None probe is unjudgeable (timeout/CLI error): tally it separately and
            # keep it out of the trigger-rate bucket rather than scoring it as a miss.
            if result is None:
                errors[(i, m)] = errors.get((i, m), 0) + 1
            else:
                raw.setdefault((i, m), []).append(result)
            if verbose and done % 10 == 0:
                logger.info("    ...%d/%d runs", done, len(jobs))
    if total_errors := sum(errors.values()):
        logger.info(
            "    %d probe(s) unjudgeable (timeout/CLI error); excluded from rates.",
            total_errors,
        )
    return aggregate(
        raw, eval_set, config.models, config.threshold, description, errors
    )
