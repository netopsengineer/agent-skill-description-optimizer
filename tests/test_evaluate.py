"""Tests for ``evaluate`` orchestration, with ``run_single_query`` stubbed.

Covers the fan-out/aggregation wiring and positional bucketing behavior
(including the duplicate-query case that was fixed from the original).
"""

from typing import Any

import pytest

from skill_optimizer import EvalConfig, EvalQuery, aggregate, evaluate, subset_result

CONFIG = EvalConfig(models=("m",), repeats=1, timeout=1, workers=2, threshold=0.5)


def _stub_rsq(monkeypatch: pytest.MonkeyPatch, fn: Any) -> None:
    monkeypatch.setattr("skill_optimizer.evaluation.run_single_query", fn)


def test_basic_aggregation(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake(query: str, *_: Any) -> bool:
        return query == "yes"

    _stub_rsq(monkeypatch, fake)
    queries: list[EvalQuery] = [
        {"query": "yes", "should_trigger": True},
        {"query": "no", "should_trigger": False},
    ]
    ev = evaluate(queries, "skill", "desc", CONFIG)
    assert ev["per_query"][0]["models"]["m"]["trigger_rate"] == 1.0
    assert ev["per_query"][1]["models"]["m"]["trigger_rate"] == 0.0
    # Both queries are classified correctly -> perfect accuracy.
    assert ev["per_model_accuracy"]["m"] == 1.0


def test_repeats_are_averaged(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def fake(query: str, *_: Any) -> bool:
        seen.append(query)
        return True

    _stub_rsq(monkeypatch, fake)
    config = EvalConfig(models=("m",), repeats=3, timeout=1, workers=2, threshold=0.5)
    queries: list[EvalQuery] = [{"query": "q", "should_trigger": True}]
    ev = evaluate(queries, "skill", "desc", config)
    assert len(seen) == 3  # one query x one model x three repeats
    assert ev["per_query"][0]["models"]["m"]["trigger_rate"] == 1.0


def test_evaluate_duplicate_queries_bucketed_positionally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Two exact-duplicate query dicts are bucketed by their positional index, so
    # both report their true trigger rate. (The original queries.index(q) sent
    # both to index 0, leaving the second with an empty bucket and a spurious 0.0;
    # positional indexing matches aggregate()'s enumerate and skill-creator's
    # unique-query assumption.)
    def fake(*_: Any) -> bool:
        return True

    _stub_rsq(monkeypatch, fake)
    queries: list[EvalQuery] = [
        {"query": "dup", "should_trigger": True},
        {"query": "dup", "should_trigger": True},
    ]
    ev = evaluate(queries, "skill", "desc", CONFIG)
    assert ev["per_query"][0]["models"]["m"]["trigger_rate"] == 1.0
    assert ev["per_query"][1]["models"]["m"]["trigger_rate"] == 1.0


# --------------------------------------------------------------------------- #
# subset_result
# --------------------------------------------------------------------------- #
_SUBSET_EVAL: list[EvalQuery] = [
    {"query": "a", "should_trigger": True},
    {"query": "b", "should_trigger": False},
]


def _full_eval() -> Any:
    # q0 triggers (correct); q1 also triggers (a false positive).
    raw = {(0, "m"): [True, True], (1, "m"): [True, False]}
    return aggregate(raw, _SUBSET_EVAL, ("m",), 0.5, "d")


def test_subset_result_slice_equals_direct_eval() -> None:
    # Slicing the full eval to [0] equals aggregating just q0's raw data.
    full = _full_eval()
    sub = subset_result(full, _SUBSET_EVAL, [0], ("m",))
    direct = aggregate({(0, "m"): [True, True]}, [_SUBSET_EVAL[0]], ("m",), 0.5, "d")
    assert sub["per_model_accuracy"] == direct["per_model_accuracy"]
    assert sub["mean_accuracy"] == direct["mean_accuracy"]
    assert sub["min_accuracy"] == direct["min_accuracy"]
    assert sub["confusion"] == direct["confusion"]
    assert len(sub["per_query"]) == 1
    # The original positional index is preserved (dedup-safe), not renumbered.
    assert sub["per_query"][0]["index"] == 0
    assert sub["per_query"][0]["query"] == "a"


def test_subset_result_out_of_range_raises() -> None:
    with pytest.raises(ValueError, match="out of range"):
        subset_result(_full_eval(), _SUBSET_EVAL, [2], ("m",))


def test_subset_result_duplicate_index_raises() -> None:
    with pytest.raises(ValueError, match="duplicate index"):
        subset_result(_full_eval(), _SUBSET_EVAL, [0, 0], ("m",))


def test_subset_result_length_mismatch_raises() -> None:
    with pytest.raises(ValueError, match="length"):
        subset_result(_full_eval(), _SUBSET_EVAL[:1], [0], ("m",))


def test_subset_result_deep_copies_entries() -> None:
    # Mutating a sliced entry must not mutate the source full eval.
    full = _full_eval()
    sub = subset_result(full, _SUBSET_EVAL, [0], ("m",))
    sub["per_query"][0]["models"]["m"]["trigger_rate"] = 999.0
    assert full["per_query"][0]["models"]["m"]["trigger_rate"] != 999.0
