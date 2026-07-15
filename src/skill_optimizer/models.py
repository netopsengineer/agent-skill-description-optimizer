"""Shared types and constants for the skill-description optimizer."""

from dataclasses import dataclass
from typing import TypedDict

# Convenience aliases so callers can pass ``--models haiku,sonnet,opus``. These are
# source-pinned to current tier IDs (verified against Anthropic's official model table);
# any full model id also passes through unchanged. For reproducibility, pass an explicit
# full ``--model``/``--models`` id rather than relying on these moving-target defaults.
MODEL_ALIASES: dict[str, str] = {
    "haiku": "claude-haiku-4-5-20251001",
    "sonnet": "claude-sonnet-5",
    "opus": "claude-opus-4-8",
}


class EvalQuery(TypedDict):
    """A single evaluation query.

    Attributes:
        query: The natural-language task shown to the model.
        should_trigger: Whether a correct skill description should fire for it.
    """

    query: str
    should_trigger: bool


# ``pass`` is a reserved word, so this member needs the functional TypedDict syntax.
# ``pass`` is ``None`` when every probe for this ``(query, model)`` cell was
# unjudgeable (timeout/CLI error), so the cell is excluded from accuracy denominators
# rather than scored as a miss. ``triggers``/``runs`` are the judged-run counts (so a
# cell with 2 judged + 1 error is representable), and ``errors`` is the unjudged count.
ModelResult = TypedDict(
    "ModelResult",
    {
        "trigger_rate": float,
        "pass": bool | None,
        "triggers": int,
        "runs": int,
        "errors": int,
    },
)
"""Per-model outcome for one query: trigger rate, pass/fail/unjudged, and counts."""


class PerQuery(TypedDict):
    """Per-query roll-up across all evaluated models.

    Attributes:
        index: Original positional index into the full eval set (dedup-safe key used
            by train/test slicing and the HTML report, never the query text).
        query: The evaluated query text.
        should_trigger: Whether a correct description should fire for it.
        models: Per-model outcomes keyed by model id.
        all_pass: ``True`` if every judged model passes, ``False`` if at least one
            judged model fails, ``None`` if every model was unjudged for this query.
    """

    index: int
    query: str
    should_trigger: bool
    models: dict[str, ModelResult]
    all_pass: bool | None


class ImproverAttempt(TypedDict):
    """One previously-tried description plus its **train-only** per-query results.

    Held-out (test) results are deliberately excluded so the improver never sees the
    selection set (blinding), which would otherwise invite overfitting.

    Attributes:
        description: The description that was tried.
        train_results: The training-set :data:`PerQuery` roll-ups for that attempt.
    """

    description: str
    train_results: list[PerQuery]


class ConfusionMatrix(TypedDict):
    """Trigger confusion matrix over judged runs, with derived ratios.

    Attributes:
        tp: True positives (should-trigger runs that triggered).
        fp: False positives (should-not-trigger runs that triggered).
        tn: True negatives (should-not-trigger runs that stayed silent).
        fn: False negatives (should-trigger runs that stayed silent).
        precision: ``tp / (tp + fp)`` (``1.0`` when there are no positives predicted).
        recall: ``tp / (tp + fn)`` (``1.0`` when there are no positives expected).
        accuracy: ``(tp + tn) / total`` (``0.0`` when there are no judged runs).
    """

    tp: int
    fp: int
    tn: int
    fn: int
    precision: float
    recall: float
    accuracy: float


class EvalResult(TypedDict):
    """Aggregated evaluation of one description across queries and models.

    Attributes:
        description: The description that was evaluated.
        per_model_accuracy: Accuracy in ``[0, 1]`` keyed by model id; ``None`` for a
            model with zero judged queries in the (sub)set.
        mean_accuracy: Mean of the non-``None`` ``per_model_accuracy`` values.
        min_accuracy: Minimum (weakest-model) non-``None`` accuracy.
        per_query: One :data:`PerQuery` entry per evaluation query.
        errors: Total unjudged probe count across all cells.
        unjudged: Number of unjudged ``query x model`` cells.
        score_valid: ``False`` when no model had any judged query (accuracy unusable
            for selection); ``True`` otherwise.
        confusion: Aggregate confusion matrix over all judged runs.
        per_model_confusion: Per-model confusion matrix keyed by model id.
    """

    description: str
    per_model_accuracy: dict[str, float | None]
    mean_accuracy: float
    min_accuracy: float
    per_query: list[PerQuery]
    errors: int
    unjudged: int
    score_valid: bool
    confusion: ConfusionMatrix
    per_model_confusion: dict[str, ConfusionMatrix]


@dataclass(frozen=True, slots=True)
class EvalConfig:
    """Settings shared across every query in an evaluation run.

    Bundling these avoids threading the same long positional-argument list through
    ``evaluate`` and its callers.

    Attributes:
        models: Resolved model ids to evaluate against.
        repeats: Number of runs per ``(query, model)`` pair.
        timeout: Per-run wall-clock budget, in seconds.
        workers: Maximum concurrent ``claude -p`` subprocesses.
        threshold: Trigger-rate at or above which a query counts as triggered.
        settings_json: Optional ``--settings`` JSON blob, or ``None`` to omit it.
    """

    models: tuple[str, ...]
    repeats: int
    timeout: int
    workers: int
    threshold: float
    settings_json: str | None = None
