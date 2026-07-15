"""Tests for the extracted aggregation math (``aggregate``)."""

from skill_optimizer import EvalQuery, aggregate


def _eval(*specs: bool) -> list[EvalQuery]:
    return [{"query": f"q{i}", "should_trigger": s} for i, s in enumerate(specs)]


class TestAggregate:
    def test_true_positive_passes(self) -> None:
        ev = aggregate(
            {(0, "m"): [True, True, False]}, _eval(True), ("m",), 0.5, "desc"
        )
        pq = ev["per_query"][0]
        assert pq["index"] == 0
        assert pq["models"]["m"]["trigger_rate"] == 0.667  # 2/3 rounded to 3dp
        assert pq["models"]["m"]["pass"] is True
        assert pq["models"]["m"]["triggers"] == 2
        assert pq["models"]["m"]["runs"] == 3
        assert pq["models"]["m"]["errors"] == 0
        assert pq["all_pass"] is True
        assert ev["per_model_accuracy"]["m"] == 1.0
        assert ev["mean_accuracy"] == 1.0
        assert ev["min_accuracy"] == 1.0
        assert ev["description"] == "desc"
        assert ev["score_valid"] is True
        assert ev["errors"] == 0
        assert ev["unjudged"] == 0

    def test_false_positive_fails(self) -> None:
        # should_trigger=False but it triggered every time -> fail.
        ev = aggregate({(0, "m"): [True]}, _eval(False), ("m",), 0.5, "d")
        pq = ev["per_query"][0]
        assert pq["models"]["m"]["trigger_rate"] == 1.0
        assert pq["models"]["m"]["pass"] is False
        assert ev["per_model_accuracy"]["m"] == 0.0

    def test_missing_data_is_unjudged(self) -> None:
        # No runs recorded for the (query, model) -> unjudged: pass=None, rate 0.0,
        # excluded from the accuracy denominator (regardless of should_trigger).
        ev_pos = aggregate({}, _eval(True), ("m",), 0.5, "d")
        pq = ev_pos["per_query"][0]
        assert pq["models"]["m"]["trigger_rate"] == 0.0
        assert pq["models"]["m"]["pass"] is None
        assert pq["models"]["m"]["runs"] == 0
        assert pq["all_pass"] is None
        assert ev_pos["per_model_accuracy"]["m"] is None
        assert ev_pos["unjudged"] == 1
        assert ev_pos["score_valid"] is False  # no model had any judged query
        ev_neg = aggregate({}, _eval(False), ("m",), 0.5, "d")
        assert ev_neg["per_query"][0]["models"]["m"]["pass"] is None

    def test_errors_are_counted_per_cell(self) -> None:
        # A cell with 2 judged runs plus 1 error is representable and stays judged.
        ev = aggregate(
            {(0, "m"): [True, True]},
            _eval(True),
            ("m",),
            0.5,
            "d",
            {(0, "m"): 1},
        )
        mr = ev["per_query"][0]["models"]["m"]
        assert mr["runs"] == 2
        assert mr["triggers"] == 2
        assert mr["errors"] == 1
        assert mr["pass"] is True
        assert ev["errors"] == 1

    def test_all_pass_false_when_one_judged_model_fails(self) -> None:
        # One judged model passes, one is unjudged -> all_pass True (judged all pass).
        ev = aggregate({(0, "m1"): [True]}, _eval(True), ("m1", "m2"), 0.5, "d")
        assert ev["per_query"][0]["all_pass"] is True
        assert ev["per_query"][0]["models"]["m2"]["pass"] is None
        assert ev["per_model_accuracy"]["m2"] is None
        # m1 has a judged query, so the overall score is valid.
        assert ev["score_valid"] is True

    def test_confusion_matrix_hand_computed(self) -> None:
        # q0 should-trigger, m triggered 2/3 -> tp=2, fn=1.
        # q1 should-NOT-trigger, m triggered 1/3 -> fp=1, tn=2.
        ev = aggregate(
            {(0, "m"): [True, True, False], (1, "m"): [True, False, False]},
            _eval(True, False),
            ("m",),
            0.5,
            "d",
        )
        c = ev["confusion"]
        assert (c["tp"], c["fn"], c["fp"], c["tn"]) == (2, 1, 1, 2)
        assert c["precision"] == round(2 / 3, 4)  # tp / (tp + fp)
        assert c["recall"] == round(2 / 3, 4)  # tp / (tp + fn)
        assert c["accuracy"] == round(4 / 6, 4)  # (tp + tn) / total
        assert ev["per_model_confusion"]["m"] == c

    def test_threshold_boundary_is_inclusive_for_should_trigger(self) -> None:
        # rate exactly == threshold counts as triggered (>=).
        ev = aggregate({(0, "m"): [True, False]}, _eval(True), ("m",), 0.5, "d")
        assert ev["per_query"][0]["models"]["m"]["trigger_rate"] == 0.5
        assert ev["per_query"][0]["models"]["m"]["pass"] is True
        # ...and a should-NOT-trigger at exactly threshold fails (0.5 < 0.5 is False).
        ev2 = aggregate({(0, "m"): [True, False]}, _eval(False), ("m",), 0.5, "d")
        assert ev2["per_query"][0]["models"]["m"]["pass"] is False

    def test_mean_and_min_across_models(self) -> None:
        # m1 correct, m2 wrong on a single should_trigger query.
        ev = aggregate(
            {(0, "m1"): [True], (0, "m2"): [False]},
            _eval(True),
            ("m1", "m2"),
            0.5,
            "d",
        )
        assert ev["per_model_accuracy"] == {"m1": 1.0, "m2": 0.0}
        assert ev["mean_accuracy"] == 0.5
        assert ev["min_accuracy"] == 0.0
        assert ev["per_query"][0]["all_pass"] is False

    def test_accuracy_rounded_to_four_dp(self) -> None:
        # 2 of 3 queries pass for model m -> 0.6667.
        ev = aggregate(
            {(0, "m"): [True], (1, "m"): [True], (2, "m"): [False]},
            _eval(True, True, True),
            ("m",),
            0.5,
            "d",
        )
        assert ev["per_model_accuracy"]["m"] == 0.6667
