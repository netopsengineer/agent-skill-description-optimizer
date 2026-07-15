"""Characterization tests for the pure selection / formatting helpers.

Pins current behavior of ``stratified_split``, ``resolve_models``, ``summarize``,
``build_improver_prompt`` and ``is_better_candidate`` before any refactor.
"""

import pytest

import optimize_description_v2 as m
from optimize_description_v2 import EvalQuery, EvalResult
from skill_optimizer.models import (
    ConfusionMatrix,
    ImproverAttempt,
    ModelResult,
    PerQuery,
)
from skill_optimizer.selection import summarize_verbose


# --------------------------------------------------------------------------- #
# EvalResult / ModelResult / PerQuery literal builders (tri-state aware)
# --------------------------------------------------------------------------- #
def _cm(
    tp: int = 0,
    fp: int = 0,
    tn: int = 0,
    fn: int = 0,
    precision: float = 1.0,
    recall: float = 1.0,
    accuracy: float = 0.0,
) -> ConfusionMatrix:
    return {
        "tp": tp,
        "fp": fp,
        "tn": tn,
        "fn": fn,
        "precision": precision,
        "recall": recall,
        "accuracy": accuracy,
    }


def _mr(
    rate: float,
    passed: bool | None,
    *,
    triggers: int = 0,
    runs: int = 0,
    errors: int = 0,
) -> ModelResult:
    return {
        "trigger_rate": rate,
        "pass": passed,
        "triggers": triggers,
        "runs": runs,
        "errors": errors,
    }


def _pq(
    index: int,
    query: str,
    should_trigger: bool,
    models: dict[str, ModelResult],
    all_pass: bool | None,
) -> PerQuery:
    return {
        "index": index,
        "query": query,
        "should_trigger": should_trigger,
        "models": models,
        "all_pass": all_pass,
    }


def _er(
    *,
    description: str = "",
    per_model_accuracy: dict[str, float | None],
    mean_accuracy: float,
    min_accuracy: float,
    per_query: list[PerQuery],
) -> EvalResult:
    return {
        "description": description,
        "per_model_accuracy": per_model_accuracy,
        "mean_accuracy": mean_accuracy,
        "min_accuracy": min_accuracy,
        "per_query": per_query,
        "errors": 0,
        "unjudged": 0,
        "score_valid": True,
        "confusion": _cm(),
        "per_model_confusion": {name: _cm() for name in per_model_accuracy},
    }


# --------------------------------------------------------------------------- #
# stratified_split
# --------------------------------------------------------------------------- #
def _q(i: int, trig: bool) -> EvalQuery:
    return {"query": f"q{i}", "should_trigger": trig}


class TestStratifiedSplit:
    def test_balanced_split(self) -> None:
        pos = [_q(i, True) for i in range(10)]
        neg = [_q(i, False) for i in range(10, 20)]
        data = pos + neg
        train, test = m.stratified_split(data, 0.35)
        # round(3.5) -> 4 per group (4 pos + 4 neg held out).
        assert len(test) == 8
        assert len(train) == 12
        # Returns original positional indices, not query sublists.
        assert all(isinstance(i, int) for i in test + train)
        # Stratified: equal hold-out from each class (looked up by index).
        assert sum(data[i]["should_trigger"] for i in test) == 4
        assert sum(not data[i]["should_trigger"] for i in test) == 4

    def test_deterministic_indices(self) -> None:
        # Fixed-seed shuffle -> the same indices on repeated calls.
        data = [_q(i, i % 2 == 0) for i in range(12)]
        assert m.stratified_split(data, 0.35) == m.stratified_split(data, 0.35)

    def test_positive_split_shuffle_is_not_pure_file_order(self) -> None:
        # Both classes present and large enough; the seeded shuffle picks a non-prefix
        # held-out set (not simply the first N indices as written).
        data = [_q(i, True) for i in range(20)] + [_q(i, False) for i in range(20, 40)]
        _, test = m.stratified_split(data, 0.35)
        assert sorted(test) != list(range(len(test)))

    def test_no_overlap_and_full_coverage(self) -> None:
        data = [_q(i, i % 2 == 0) for i in range(12)]
        train, test = m.stratified_split(data, 0.5)
        train_ids, test_ids = set(train), set(test)
        assert train_ids.isdisjoint(test_ids)
        assert train_ids | test_ids == set(range(len(data)))

    @pytest.mark.parametrize("frac", [0.2, 0.35, 0.5, 0.6])
    def test_valid_positive_split_covers_each_index_once_both_members(
        self, frac: float
    ) -> None:
        data = [_q(i, True) for i in range(8)] + [_q(i, False) for i in range(8, 16)]
        train, test = m.stratified_split(data, frac)
        # Finite, in-range fraction -> disjoint, covering every index exactly once.
        assert sorted(train + test) == list(range(len(data)))
        assert set(train).isdisjoint(set(test))
        # At least one train and one test member remain in each class.
        for want in (True, False):
            assert any(data[i]["should_trigger"] == want for i in train)
            assert any(data[i]["should_trigger"] == want for i in test)

    def test_valid_positive_split_is_deterministic_per_seed(self) -> None:
        data = [_q(i, True) for i in range(6)] + [_q(i, False) for i in range(6, 12)]
        assert m.stratified_split(data, 0.4, seed=7) == m.stratified_split(
            data, 0.4, seed=7
        )

    # ---- zero disables the holdout (all-train / no-test, original order) ----- #
    def test_zero_holdout_mixed_all_train(self) -> None:
        data = [_q(0, True), _q(1, False), _q(2, True)]
        assert m.stratified_split(data, 0.0) == ([0, 1, 2], [])

    def test_zero_holdout_single_class_all_train(self) -> None:
        data = [_q(0, True), _q(1, True), _q(2, True)]
        assert m.stratified_split(data, 0.0) == ([0, 1, 2], [])

    def test_zero_holdout_empty(self) -> None:
        assert m.stratified_split([], 0.0) == ([], [])

    # ---- invalid fractions raise before any split --------------------------- #
    @pytest.mark.parametrize(
        "bad", [float("nan"), float("inf"), float("-inf"), -0.1, 1.0, 2.0]
    )
    def test_non_finite_or_out_of_range_raises(self, bad: float) -> None:
        with pytest.raises(
            ValueError, match=r"holdout must be finite and satisfy 0 <= holdout < 1"
        ):
            m.stratified_split([_q(0, True), _q(1, False)], bad)

    def test_positive_class_too_small_raises(self) -> None:
        data = [_q(0, True), _q(1, False), _q(2, False)]
        with pytest.raises(
            ValueError,
            match=r"positive class must contain at least 2 queries when holdout > 0",
        ):
            m.stratified_split(data, 0.35)

    def test_negative_class_too_small_raises(self) -> None:
        data = [_q(0, True), _q(1, True), _q(2, False)]
        with pytest.raises(
            ValueError,
            match=r"negative class must contain at least 2 queries when holdout > 0",
        ):
            m.stratified_split(data, 0.35)

    def test_single_item_positive_holdout_raises(self) -> None:
        # A lone positive item trips the positive-class check first (loop order).
        with pytest.raises(ValueError, match=r"positive class must contain at least 2"):
            m.stratified_split([_q(0, True)], 0.35)

    def test_unsafe_high_fraction_leaves_no_train_raises(self) -> None:
        # 2 per class at 0.75: round(1.5)=2 == class size -> no training query left.
        data = [_q(0, True), _q(1, True), _q(2, False), _q(3, False)]
        with pytest.raises(
            ValueError,
            match=r"positive class cannot retain a training query at this holdout",
        ):
            m.stratified_split(data, 0.75)

    def test_bankers_rounding_half_to_even(self) -> None:
        # QUIRK: Python round() is banker's rounding. class of 10 * 0.25 == 2.5 -> 2,
        # NOT 3. This pins that the split uses round(), not ceil/int.
        data = [_q(i, True) for i in range(10)] + [_q(i, False) for i in range(10, 20)]
        _, test = m.stratified_split(data, 0.25)
        assert len(test) == 4  # 2 per class (banker's rounding of 2.5), not 3


# --------------------------------------------------------------------------- #
# resolve_models
# --------------------------------------------------------------------------- #
class TestResolveModels:
    def test_none_defaults_to_sonnet(self) -> None:
        assert m.resolve_models(None) == ["claude-sonnet-5"]

    def test_empty_string_defaults_to_sonnet(self) -> None:
        assert m.resolve_models("") == ["claude-sonnet-5"]

    def test_aliases_expand(self) -> None:
        assert m.resolve_models("haiku,sonnet,opus") == [
            "claude-haiku-4-5-20251001",
            "claude-sonnet-5",
            "claude-opus-4-8",
        ]

    def test_unknown_id_passes_through(self) -> None:
        assert m.resolve_models("my-custom-id") == ["my-custom-id"]

    def test_whitespace_and_empty_segments_dropped(self) -> None:
        assert m.resolve_models(" haiku , opus ") == [
            "claude-haiku-4-5-20251001",
            "claude-opus-4-8",
        ]
        assert m.resolve_models("a,,b") == ["a", "b"]

    def test_only_separators_yields_empty_list(self) -> None:
        # QUIRK: a truthy-but-all-empty spec returns [], not the sonnet default.
        assert m.resolve_models(",") == []


# --------------------------------------------------------------------------- #
# summarize
# --------------------------------------------------------------------------- #
class TestSummarize:
    def test_format_with_aliased_model_names(self) -> None:
        ev = _er(
            per_model_accuracy={"claude-sonnet-4-6": 0.5, "claude-haiku-4-5": 1.0},
            mean_accuracy=0.75,
            min_accuracy=0.5,
            per_query=[],
        )
        # Model short name = split('-')[1]; note the double space before '('.
        assert (
            m.summarize("BASE", ev)
            == "BASE: mean=0.750 min=0.500  (sonnet=0.50 haiku=1.00)"
        )

    def test_model_without_dash_uses_full_name(self) -> None:
        ev = _er(
            per_model_accuracy={"gpt4": 0.5},
            mean_accuracy=0.5,
            min_accuracy=0.5,
            per_query=[],
        )
        assert m.summarize("X", ev) == "X: mean=0.500 min=0.500  (gpt4=0.50)"

    def test_none_model_accuracy_renders_as_na(self) -> None:
        ev = _er(
            per_model_accuracy={"claude-sonnet-4-6": None},
            mean_accuracy=0.0,
            min_accuracy=0.0,
            per_query=[],
        )
        ev["score_valid"] = False
        assert m.summarize("X", ev) == "X: mean=0.000 min=0.000  (sonnet=n/a)"


# --------------------------------------------------------------------------- #
# summarize_verbose (tri-state: a fully-unjudged model/aggregate is n/a)
# --------------------------------------------------------------------------- #
class TestSummarizeVerbose:
    def test_unjudged_model_line_is_na(self) -> None:
        # One judged model (real confusion matrix) plus one fully-unjudged model whose
        # all-zeros confusion would otherwise render the misleading 100%/100%/0% line.
        ev = _er(
            per_model_accuracy={"claude-sonnet-4-6": 1.0, "claude-haiku-4-5": None},
            mean_accuracy=1.0,
            min_accuracy=1.0,
            per_query=[],
        )
        ev["confusion"] = _cm(tp=3, tn=3, accuracy=1.0)
        ev["per_model_confusion"] = {
            "claude-sonnet-4-6": _cm(tp=3, tn=3, accuracy=1.0),
            "claude-haiku-4-5": _cm(),  # all-zeros -> 100%/100%/0% on the old code
        }
        out = summarize_verbose("BASE", ev)
        assert "  haiku: n/a" in out
        # The misleading precision/recall/accuracy line for the unjudged model is gone.
        assert "haiku: precision" not in out
        # The judged model still renders its real numbers.
        assert "sonnet: precision=100% recall=100% accuracy=100%" in out
        # Aggregate is present (judged runs exist), not n/a.
        assert "aggregate: 6/6 correct" in out

    def test_all_unjudged_aggregate_is_na(self) -> None:
        # Every model unjudged: both the per-model line AND the aggregate render n/a.
        ev = _er(
            per_model_accuracy={"claude-sonnet-4-6": None},
            mean_accuracy=0.0,
            min_accuracy=0.0,
            per_query=[],
        )
        ev["score_valid"] = False
        # confusion + per_model_confusion default to all-zeros (_cm()).
        out = summarize_verbose("BASE", ev)
        assert "  aggregate: n/a" in out
        assert "  sonnet: n/a" in out
        # No misleading 0%-accuracy line anywhere in the block.
        assert "accuracy=0%" not in out


# --------------------------------------------------------------------------- #
# build_improver_prompt
# --------------------------------------------------------------------------- #
def _ev_with_failures() -> EvalResult:
    return _er(
        description="current desc",
        mean_accuracy=0.75,
        min_accuracy=0.75,
        per_model_accuracy={"claude-sonnet-4-6": 0.75},
        per_query=[
            _pq(
                0,
                "good one",
                True,
                {"claude-sonnet-4-6": _mr(0.0, False, runs=3)},
                False,
            ),
            _pq(
                1,
                "bad one",
                False,
                {"claude-sonnet-4-6": _mr(1.0, False, triggers=3, runs=3)},
                False,
            ),
            _pq(
                2,
                "passing one",
                True,
                {"claude-sonnet-4-6": _mr(1.0, True, triggers=3, runs=3)},
                True,
            ),
        ],
    )


class TestBuildImproverPrompt:
    def test_includes_name_and_failures(self) -> None:
        prompt = m.build_improver_prompt(
            "myskill", "current desc", "BODY TEXT", _ev_with_failures()
        )
        assert "myskill" in prompt
        assert '(should trigger) "good one"' in prompt
        assert '(should NOT trigger) "bad one"' in prompt
        assert 'trigger_rates={"claude-sonnet-4-6": 0.0}' in prompt
        assert '{"claude-sonnet-4-6": 0.75}' in prompt  # per-model accuracy line

    def test_passing_queries_are_omitted(self) -> None:
        prompt = m.build_improver_prompt(
            "myskill", "current desc", "BODY", _ev_with_failures()
        )
        assert "passing one" not in prompt

    def test_no_failures_placeholder(self) -> None:
        ev = _er(
            description="d",
            mean_accuracy=1.0,
            min_accuracy=1.0,
            per_model_accuracy={"claude-sonnet-4-6": 1.0},
            per_query=[
                _pq(
                    0,
                    "q",
                    True,
                    {"claude-sonnet-4-6": _mr(1.0, True, triggers=3, runs=3)},
                    True,
                )
            ],
        )
        prompt = m.build_improver_prompt("s", "d", "b", ev)
        assert "(none — all queries pass)" in prompt

    def test_body_excerpt_truncated_to_1500_chars(self) -> None:
        body = "BODY" * 1000  # 4000 chars
        prompt = m.build_improver_prompt("s", "d", body, _ev_with_failures())
        assert "BODY" * 375 in prompt  # first 1500 chars present
        assert "BODY" * 376 not in prompt  # but no more than 1500

    def test_lists_prior_attempts(self) -> None:
        attempts: list[ImproverAttempt] = [
            {"description": "first try", "train_results": []},
            {"description": "second try", "train_results": []},
        ]
        prompt = m.build_improver_prompt("s", "d", "b", _ev_with_failures(), attempts)
        assert 'description: "first try"' in prompt
        assert 'description: "second try"' in prompt

    def test_no_prior_attempts_shows_placeholder(self) -> None:
        prompt = m.build_improver_prompt("s", "d", "b", _ev_with_failures())
        assert "(none yet)" in prompt

    def test_prior_attempt_renders_train_results(self) -> None:
        attempts: list[ImproverAttempt] = [
            {
                "description": "prev desc",
                "train_results": [
                    _pq(
                        0,
                        "train query alpha",
                        True,
                        {"claude-sonnet-4-6": _mr(1.0, True, triggers=3, runs=3)},
                        True,
                    )
                ],
            }
        ]
        prompt = m.build_improver_prompt("s", "d", "b", _ev_with_failures(), attempts)
        assert "<attempt" in prompt
        assert "train query alpha" in prompt
        assert "triggered 3/3" in prompt

    def test_blinds_heldout_query_text(self) -> None:
        # ev is train-only and prior_attempts carry train-only results, so a held-out
        # query passed to NEITHER must be absent from the assembled prompt.
        attempts: list[ImproverAttempt] = [
            {
                "description": "prev desc",
                "train_results": [
                    _pq(
                        0,
                        "train query alpha",
                        True,
                        {"claude-sonnet-4-6": _mr(1.0, True, triggers=3, runs=3)},
                        True,
                    )
                ],
            }
        ]
        ev = _er(
            description="cur",
            mean_accuracy=0.0,
            min_accuracy=0.0,
            per_model_accuracy={"claude-sonnet-4-6": 0.0},
            per_query=[
                _pq(
                    0,
                    "train fail beta",
                    True,
                    {"claude-sonnet-4-6": _mr(0.0, False, runs=3)},
                    False,
                )
            ],
        )
        prompt = m.build_improver_prompt("s", "cur", "body", ev, attempts)
        assert "HELDOUT_SECRET_QUERY" not in prompt
        assert "train query alpha" in prompt  # prior train result present
        assert "train fail beta" in prompt  # current train failure present


# --------------------------------------------------------------------------- #
# is_better_candidate
# --------------------------------------------------------------------------- #
class TestIsBetterCandidate:
    def test_strictly_better_mean_wins(self) -> None:
        win, reason = m.is_better_candidate(0.80, 0.5, 0.70, 0.6, epsilon=0.05)
        assert win is True
        assert "improved held-out mean" in reason

    def test_tie_broken_by_higher_min(self) -> None:
        win, reason = m.is_better_candidate(0.72, 0.70, 0.70, 0.60, epsilon=0.05)
        assert win is True
        assert "weakest-model" in reason

    def test_tie_without_min_gain_loses(self) -> None:
        win, reason = m.is_better_candidate(0.72, 0.50, 0.70, 0.60, epsilon=0.05)
        assert win is False
        assert reason == "no held-out gain"

    def test_worse_mean_loses(self) -> None:
        win, _ = m.is_better_candidate(0.60, 0.90, 0.80, 0.10, epsilon=0.05)
        assert win is False

    def test_epsilon_zero_disables_tiebreak(self) -> None:
        # Even a large min advantage cannot win a tie when epsilon == 0.
        win, reason = m.is_better_candidate(0.70, 0.90, 0.70, 0.10, epsilon=0.0)
        assert win is False
        assert reason == "no held-out gain"

    def test_epsilon_zero_strict_improvement_wins(self) -> None:
        win, _ = m.is_better_candidate(0.71, 0.0, 0.70, 0.9, epsilon=0.0)
        assert win is True

    def test_over_budget_candidate_never_wins(self) -> None:
        # Higher mean, but over the char budget -> cannot ship, cannot win.
        win, reason = m.is_better_candidate(
            0.90,
            0.90,
            0.50,
            0.50,
            0.05,
            cand_chars=1100,
            best_chars=500,
            max_chars=1000,
        )
        assert win is False
        assert "over char budget" in reason

    def test_legal_candidate_replaces_over_budget_incumbent(self) -> None:
        # Incumbent over budget can't ship; a legal candidate wins even at lower mean.
        win, reason = m.is_better_candidate(
            0.50,
            0.50,
            0.90,
            0.90,
            0.05,
            cand_chars=900,
            best_chars=1100,
            max_chars=1000,
        )
        assert win is True
        assert "incumbent over budget" in reason

    def test_shorter_wins_within_epsilon(self) -> None:
        # Two legal descriptions tie within epsilon -> the shorter one wins.
        win, reason = m.is_better_candidate(
            0.70,
            0.50,
            0.70,
            0.50,
            0.05,
            cand_chars=800,
            best_chars=900,
            max_chars=1000,
        )
        assert win is True
        assert "shorter description preferred" in reason

    def test_max_chars_none_matches_five_arg_behavior(self) -> None:
        # Passing max_chars=None is byte-identical to the 5-argument call.
        args = (0.72, 0.70, 0.70, 0.60, 0.05)
        assert m.is_better_candidate(*args) == m.is_better_candidate(
            *args, cand_chars=5000, best_chars=1, max_chars=None
        )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
