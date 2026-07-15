"""Tests for the ported HTML report generator (``generate_html``)."""

from typing import Any

from skill_optimizer import generate_html


def _result(
    index: int,
    query: str,
    should_trigger: bool,
    triggers: int,
    runs: int,
    passed: bool | None,
    models: dict[str, dict[str, int]],
) -> dict[str, Any]:
    return {
        "index": index,
        "query": query,
        "should_trigger": should_trigger,
        "triggers": triggers,
        "runs": runs,
        "errors": 0,
        "pass": passed,
        "models": models,
    }


def _entry(
    iteration: int,
    description: str,
    is_best: bool,
    train_results: list[dict[str, Any]],
    test_results: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "iteration": iteration,
        "description": description,
        "is_best": is_best,
        "train_results": train_results,
        "test_results": test_results,
    }


def _data() -> dict[str, Any]:
    sonnet = {"sonnet": {"triggers": 3, "runs": 3}}
    train0 = [_result(0, "train query zero", True, 3, 3, True, sonnet)]
    test0 = [
        _result(
            1,
            "held query one",
            False,
            0,
            3,
            True,
            {"sonnet": {"triggers": 0, "runs": 3}},
        )
    ]
    return {
        "original_description": "base desc",
        "best_description": "iter1 desc",
        "best_score": "1/1",
        "best_train_score": "1/1",
        "best_test_score": "1/1",
        "iterations_run": 1,
        "train_size": 1,
        "test_size": 1,
        "history": [
            _entry(0, "base desc", False, train0, test0),
            _entry(1, "iter1 desc", True, train0, test0),
        ],
    }


def test_contains_query_headers_and_descriptions() -> None:
    html = generate_html(_data())
    assert "train query zero" in html
    assert "held query one" in html
    assert "iter1 desc" in html
    assert "base desc" in html


def test_best_row_highlighted_exactly_once() -> None:
    # The winning row is highlighted from is_best, never recomputed from a score.
    html = generate_html(_data())
    assert html.count('class="best-row"') == 1


def test_auto_refresh_tag_toggles() -> None:
    assert 'http-equiv="refresh"' in generate_html(_data(), auto_refresh=True)
    assert 'http-equiv="refresh"' not in generate_html(_data(), auto_refresh=False)


def test_skill_name_in_title() -> None:
    html = generate_html(_data(), skill_name="mypdfskill")
    assert "mypdfskill" in html


def test_unjudged_renders_neutral_not_cross() -> None:
    sonnet = {"sonnet": {"triggers": 0, "runs": 0}}
    train = [_result(0, "unjudged query", True, 0, 0, None, sonnet)]
    data: dict[str, Any] = {
        "history": [_entry(0, "d", True, train, [])],
        "train_size": 1,
        "test_size": 0,
    }
    html = generate_html(data)
    assert "–" in html  # neutral marker present
    assert "✗" not in html  # never a red cross for an unjudged probe
    assert "unjudged" in html


def test_per_model_detail_in_cell_title() -> None:
    # Q2: never aggregate-only -- per-model rates ride along in the cell title.
    two_models = {
        "sonnet": {"triggers": 2, "runs": 3},
        "opus": {"triggers": 3, "runs": 3},
    }
    train = [_result(0, "q", True, 5, 6, True, two_models)]
    data: dict[str, Any] = {"history": [_entry(0, "d", True, train, [])]}
    html = generate_html(data)
    assert "sonnet: 2/3" in html
    assert "opus: 3/3" in html


def test_duplicate_queries_get_distinct_columns() -> None:
    # Two entries with identical query text but distinct indices -> two columns,
    # each anchored by index (no query-text collapse).
    dup_a = _result(0, "dup", True, 3, 3, True, {"sonnet": {"triggers": 3, "runs": 3}})
    dup_b = _result(1, "dup", True, 1, 3, False, {"sonnet": {"triggers": 1, "runs": 3}})
    data: dict[str, Any] = {
        "history": [_entry(0, "d", True, [dup_a, dup_b], [])],
        "train_size": 2,
        "test_size": 0,
    }
    html = generate_html(data)
    # Both columns rendered: distinct rates 3/3 and 1/3 both present.
    assert "3/3" in html
    assert "1/3" in html
    assert html.count(">dup</th>") == 2  # two distinct header columns


def test_empty_history_does_not_crash() -> None:
    assert "<table" in generate_html({"history": []})
