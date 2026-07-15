"""Eval-set splitting, model resolution, and candidate-selection helpers."""

import math
import random

from skill_optimizer.models import MODEL_ALIASES, EvalQuery, EvalResult


def stratified_split(
    eval_set: list[EvalQuery], test_frac: float, seed: int = 42
) -> tuple[list[int], list[int]]:
    """Split query *indices* into train/test, preserving the trigger/no-trigger ratio.

    A ``test_frac`` of zero disables the held-out set (skill-creator's ``--holdout 0``
    semantics): every index becomes a training index, in original order, and no class
    balancing is required. A positive fraction must be finite and in ``[0, 1)``, each
    trigger class must hold at least two queries, and the seeded per-class allocation
    must leave at least one training and one held-out query in each class; otherwise a
    :class:`ValueError` is raised before any split is produced.

    Each class is shuffled with a fixed-seed local RNG first, so a positive split is
    reproducible but independent of how the eval file happens to be ordered (otherwise
    the held-out set is just the first N as written, which clusters similar queries).
    Returning positional indices (not query sublists) keeps the dedup invariant intact
    and lets callers slice ``per_query`` by the same indices without matching on text.

    Args:
        eval_set: All evaluation queries.
        test_frac: Fraction of each class to hold out for testing; ``0`` disables it.
        seed: RNG seed for the per-class shuffle.

    Returns:
        A ``(train_idx, test_idx)`` tuple of original positional indices into
        ``eval_set``. The two lists are disjoint and together cover every index.

    Raises:
        ValueError: If ``test_frac`` is non-finite or outside ``[0, 1)``, or (for a
            positive fraction) a class has fewer than two queries or cannot retain a
            training query at the requested fraction.
    """
    if not math.isfinite(test_frac) or not 0 <= test_frac < 1:
        raise ValueError("holdout must be finite and satisfy 0 <= holdout < 1")
    if test_frac == 0:
        # Zero disables the held-out set: all-train, no-test, original order preserved,
        # with no class-balance requirement (mirrors skill-creator's --holdout 0).
        return list(range(len(eval_set))), []
    positive = [i for i, q in enumerate(eval_set) if q["should_trigger"]]
    negative = [i for i, q in enumerate(eval_set) if not q["should_trigger"]]
    for group, label in ((positive, "positive"), (negative, "negative")):
        if len(group) < 2:
            raise ValueError(
                f"{label} class must contain at least 2 queries when holdout > 0"
            )
    rng = random.Random(seed)
    rng.shuffle(positive)
    rng.shuffle(negative)
    train: list[int] = []
    test: list[int] = []
    for group, label in ((positive, "positive"), (negative, "negative")):
        n_test = max(1, round(len(group) * test_frac))
        if n_test >= len(group):
            raise ValueError(
                f"{label} class cannot retain a training query at this holdout"
            )
        test += group[:n_test]
        train += group[n_test:]
    return train, test


def resolve_models(spec: str | None) -> list[str]:
    """Resolve a comma-separated model spec into a list of full model ids.

    Args:
        spec: Comma-separated aliases/ids (e.g. ``"haiku,sonnet"``), or ``None``.

    Returns:
        Resolved model ids. Defaults to ``[sonnet]`` when ``spec`` is empty/``None``;
        a spec of only separators (e.g. ``","``) yields an empty list.
    """
    if not spec:
        return [MODEL_ALIASES["sonnet"]]
    return [
        MODEL_ALIASES.get(s.strip(), s.strip()) for s in spec.split(",") if s.strip()
    ]


def _short_model(model: str) -> str:
    """Return a model's short display name (the segment after the first ``-``).

    Args:
        model: A full model id (e.g. ``"claude-sonnet-5"``) or bare name.

    Returns:
        The short name (e.g. ``"sonnet"``), or the full name if it has no ``-``.
    """
    return model.split("-")[1] if "-" in model else model


def summarize(tag: str, ev: EvalResult) -> str:
    """Render a one-line summary of an evaluation result.

    Args:
        tag: Short label for the line (e.g. ``"BASELINE"``).
        ev: The evaluation result to summarize.

    Returns:
        A line of the form ``"<tag>: mean=… min=…  (model=acc …)"`` where each model
        is shown by its short name; a model with no judged query renders as ``n/a``.
    """
    per_model = " ".join(
        f"{_short_model(m)}={'n/a' if a is None else f'{a:.2f}'}"
        for m, a in ev["per_model_accuracy"].items()
    )
    return (
        f"{tag}: mean={ev['mean_accuracy']:.3f} "
        f"min={ev['min_accuracy']:.3f}  ({per_model})"
    )


def summarize_verbose(tag: str, ev: EvalResult) -> str:
    """Render a multi-line, skill-creator-style precision/recall/accuracy summary.

    Leads with the one-line :func:`summarize`, then an aggregate confusion header, a
    per-model precision/recall/accuracy line, and a PASS/FAIL/N-A line per query. A
    fully-unjudged model (its ``per_model_accuracy`` is ``None``) renders as ``n/a``,
    as does the aggregate when every model is unjudged, mirroring :func:`summarize`'s
    tri-state guard instead of the misleading ``100%/100%/0%`` an all-zeros confusion
    matrix would otherwise show.

    Args:
        tag: Short label for the block (e.g. ``"BASELINE"``).
        ev: The evaluation result to summarize.

    Returns:
        The multi-line summary.
    """
    c = ev["confusion"]
    judged = c["tp"] + c["fp"] + c["tn"] + c["fn"]
    agg = (
        "  aggregate: n/a"
        if judged == 0
        else f"  aggregate: {c['tp'] + c['tn']}/{judged} correct  "
        f"precision={c['precision']:.0%} recall={c['recall']:.0%} "
        f"accuracy={c['accuracy']:.0%}"
    )
    lines = [summarize(tag, ev), agg]
    acc = ev["per_model_accuracy"]
    for m, cm in ev["per_model_confusion"].items():
        if acc[m] is None:
            lines.append(f"  {_short_model(m)}: n/a")
        else:
            lines.append(
                f"  {_short_model(m)}: precision={cm['precision']:.0%} "
                f"recall={cm['recall']:.0%} accuracy={cm['accuracy']:.0%}"
            )
    for pq in ev["per_query"]:
        status = (
            "PASS"
            if pq["all_pass"] is True
            else "FAIL"
            if pq["all_pass"] is False
            else "N/A "
        )
        lines.append(
            f"  [{status}] expected={pq['should_trigger']}: {pq['query'][:60]}"
        )
    return "\n".join(lines)


def is_better_candidate(
    cand_mean: float,
    cand_min: float,
    best_mean: float,
    best_min: float,
    epsilon: float,
    cand_chars: int | None = None,
    best_chars: int | None = None,
    max_chars: int | None = None,
    *,
    score_label: str = "held-out",
) -> tuple[bool, str]:
    """Decide whether a candidate beats the incumbent on the selection score.

    The primary criterion is the selection *mean* accuracy (selecting on held-out
    rather than train is what avoids overfitting the eval set). But on a small
    selection split a sub-``epsilon`` mean difference is noise, not signal -- so when
    two descriptions tie within ``epsilon``, break the tie in favor of the higher
    *min* (weakest-model) accuracy. Set ``epsilon=0`` for strict mean-only selection.

    When ``max_chars`` is set the character budget is a HARD constraint that dominates
    the score comparison: an over-budget candidate can never win, a legal candidate
    beats an over-budget incumbent even at an equal-or-slightly-lower mean (the
    incumbent cannot ship), and among two legal ties within ``epsilon`` the shorter
    wins. Passing no ``*_chars`` (the 5-argument call) leaves length out entirely and
    reproduces the pre-budget behavior byte-for-byte.

    Args:
        cand_mean: Candidate selection mean accuracy.
        cand_min: Candidate selection min (weakest-model) accuracy.
        best_mean: Incumbent selection mean accuracy.
        best_min: Incumbent selection min accuracy.
        epsilon: Mean-accuracy band within which scores count as tied.
        cand_chars: Candidate description length, for the budget constraint.
        best_chars: Incumbent description length, for the budget constraint.
        max_chars: Hard character ceiling, or ``None`` to ignore length entirely.
        score_label: Name of the selection score used in the reason strings
            (``"held-out"`` with a holdout, ``"train"`` when the holdout is disabled).

    Returns:
        ``(win, reason)`` where ``win`` is ``True`` if the candidate should replace
        the incumbent, and ``reason`` is a short human-readable explanation.
    """

    def _over(chars: int | None) -> bool:
        return max_chars is not None and chars is not None and chars > max_chars

    if _over(cand_chars):
        return False, f"over char budget ({cand_chars} > {max_chars})"
    if _over(best_chars):
        # Incumbent can't ship; any legal candidate replaces it -- ideally with a
        # better/tied mean, but even a slightly lower one beats unshippable.
        rel = "non-regressing" if cand_mean >= best_mean - epsilon else "lower"
        return True, (
            f"incumbent over budget ({best_chars} > {max_chars}); adopting legal "
            f"candidate ({cand_chars} chars) with {rel} {score_label} mean "
            f"({cand_mean:.3f} vs {best_mean:.3f})"
        )
    if cand_mean > best_mean + epsilon:
        return True, f"improved {score_label} mean ({cand_mean:.3f} > {best_mean:.3f})"
    if epsilon > 0 and abs(cand_mean - best_mean) <= epsilon and cand_min > best_min:
        return True, (
            f"{score_label} mean within {epsilon:.3f} "
            f"({cand_mean:.3f} vs {best_mean:.3f}); "
            f"better weakest-model min ({cand_min:.3f} > {best_min:.3f})"
        )
    if (
        max_chars is not None
        and abs(cand_mean - best_mean) <= epsilon
        and cand_chars is not None
        and best_chars is not None
        and cand_chars < best_chars
    ):
        return True, (
            f"{score_label} mean within {epsilon:.3f} "
            f"({cand_mean:.3f} vs {best_mean:.3f}); "
            f"shorter description preferred ({cand_chars} < {best_chars} chars)"
        )
    return False, f"no {score_label} gain"
