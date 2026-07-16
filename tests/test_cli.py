"""Tests for the CLI layer: skill-creator-compatible flags, stdout JSON, early-exit.

``evaluate`` and ``call_improver`` are stubbed so these run without ``claude -p``.
"""

import json
import logging
import tempfile
from pathlib import Path
from typing import Any, cast

import pytest

import skill_optimizer.cli as cli_module
from skill_optimizer import EvalQuery, EvalResult, aggregate, stratified_split
from skill_optimizer.cli import (
    _LoopInputs,  # pyright: ignore[reportPrivateUsage]
    _load_eval_set,  # pyright: ignore[reportPrivateUsage]
    _optimize,  # pyright: ignore[reportPrivateUsage]
    _validate_iterations,  # pyright: ignore[reportPrivateUsage]
    build_parser,
    main,
    run,
)
from skill_optimizer.improver import ImproverFatalProcessError, ImproverRetryableError
from skill_optimizer.models import ConfusionMatrix, EvalConfig, PerQuery


def _cm() -> ConfusionMatrix:
    return {
        "tp": 0,
        "fp": 0,
        "tn": 0,
        "fn": 0,
        "precision": 1.0,
        "recall": 1.0,
        "accuracy": 0.0,
    }


def _eval_result(
    eval_set: list[EvalQuery], models: tuple[str, ...], all_pass: bool
) -> EvalResult:
    # One per_query entry per eval-set query (so subset_result's length assertion
    # holds), keyed by the real config models (so subset_result can look each up).
    rate = 1.0 if all_pass else 0.0
    per_query: list[PerQuery] = [
        {
            "index": i,
            "query": q["query"],
            "should_trigger": q["should_trigger"],
            "models": {
                mdl: {
                    "trigger_rate": rate,
                    "pass": all_pass,
                    "triggers": 3 if all_pass else 0,
                    "runs": 3,
                    "errors": 0,
                }
                for mdl in models
            },
            "all_pass": all_pass,
        }
        for i, q in enumerate(eval_set)
    ]
    return {
        "description": "d",
        "per_model_accuracy": {mdl: rate for mdl in models},
        "mean_accuracy": rate,
        "min_accuracy": rate,
        "per_query": per_query,
        "errors": 0,
        "unjudged": 0,
        "score_valid": True,
        "confusion": _cm(),
        "per_model_confusion": {mdl: _cm() for mdl in models},
    }


def _unjudged_eval_result(
    eval_set: list[EvalQuery], models: tuple[str, ...]
) -> EvalResult:
    # Every cell unjudged (all probes errored): score_valid=False, so it must never be
    # selected. subset_result recomputes the same tri-state from these copied entries.
    per_query: list[PerQuery] = [
        {
            "index": i,
            "query": q["query"],
            "should_trigger": q["should_trigger"],
            "models": {
                mdl: {
                    "trigger_rate": 0.0,
                    "pass": None,
                    "triggers": 0,
                    "runs": 0,
                    "errors": 1,
                }
                for mdl in models
            },
            "all_pass": None,
        }
        for i, q in enumerate(eval_set)
    ]
    return {
        "description": "d",
        "per_model_accuracy": {mdl: None for mdl in models},
        "mean_accuracy": 0.0,
        "min_accuracy": 0.0,
        "per_query": per_query,
        "errors": len(eval_set) * len(models),
        "unjudged": len(eval_set) * len(models),
        "score_valid": False,
        "confusion": _cm(),
        "per_model_confusion": {mdl: _cm() for mdl in models},
    }


def _stub_eval_all_pass(
    eval_set: list[EvalQuery], name: Any, description: Any, config: Any, **__: Any
) -> EvalResult:
    return _eval_result(eval_set, config.models, True)


def _no_improver(*_: Any, **__: Any) -> dict[str, Any]:
    raise AssertionError("improver should not be called")


def _setup(tmp_path: Path) -> tuple[Path, Path]:
    skill = tmp_path / "myskill"
    skill.mkdir()
    (skill / "SKILL.md").write_text(
        "---\nname: myskill\ndescription: original desc\n---\nBody\n"
    )
    eval_file = tmp_path / "evals.json"
    # >=2 queries per class so the stratified train split is non-empty (one held out
    # per class leaves at least one training query per class).
    eval_file.write_text(
        json.dumps(
            [
                {"query": "alpha", "should_trigger": True},
                {"query": "gamma", "should_trigger": True},
                {"query": "beta", "should_trigger": False},
                {"query": "delta", "should_trigger": False},
            ]
        )
    )
    return skill, eval_file


def test_skill_creator_aliases_map_to_canonical_dests() -> None:
    args = build_parser().parse_args(
        [
            "--skill-path",
            "s",
            "--eval-set",
            "e",
            "--max-iterations",
            "7",
            "--holdout",
            "0.4",
            "--runs-per-query",
            "5",
            "--num-workers",
            "9",
            "--trigger-threshold",
            "0.6",
            "--model",
            "haiku",
            "--verbose",
        ]
    )
    assert args.iterations == 7
    assert args.test_frac == 0.4
    assert args.repeats == 5
    assert args.workers == 9
    assert args.threshold == 0.6
    assert args.model == "haiku"
    assert args.verbose is True
    assert args.out is None  # --out is optional


def test_max_desc_chars_default_and_override() -> None:
    base = ["--skill-path", "s", "--eval-set", "e"]
    assert build_parser().parse_args(base).max_desc_chars == 1024
    assert (
        build_parser().parse_args([*base, "--max-desc-chars", "500"]).max_desc_chars
        == 500
    )


def test_model_shorthand_sets_eval_and_improver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    skill, eval_file = _setup(tmp_path)
    monkeypatch.setattr("skill_optimizer.cli.evaluate", _stub_eval_all_pass)
    monkeypatch.setattr("skill_optimizer.cli.call_improver", _no_improver)
    args = build_parser().parse_args(
        [
            "--skill-path",
            str(skill),
            "--eval-set",
            str(eval_file),
            "--out",
            str(tmp_path / "out"),
            "--model",
            "haiku",
        ]
    )
    run(args)
    out = json.loads(capsys.readouterr().out)
    assert out["models"] == ["claude-haiku-4-5-20251001"]
    assert out["improver"]["model"] == "claude-haiku-4-5-20251001"


def test_early_exit_when_all_train_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    skill, eval_file = _setup(tmp_path)
    monkeypatch.setattr("skill_optimizer.cli.evaluate", _stub_eval_all_pass)
    monkeypatch.setattr("skill_optimizer.cli.call_improver", _no_improver)
    args = build_parser().parse_args(
        [
            "--skill-path",
            str(skill),
            "--eval-set",
            str(eval_file),
            "--out",
            str(tmp_path / "out"),
            "--iterations",
            "3",
        ]
    )
    run(args)  # _no_improver raises if the loop fails to exit early
    out = json.loads(capsys.readouterr().out)
    assert out["best_description"] == "original desc"


def test_iterates_and_prints_best_description(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    skill, eval_file = _setup(tmp_path)

    def fake_eval(
        eval_set: list[EvalQuery],
        name: Any,
        description: str,
        config: Any,
        *,
        verbose: bool = True,
    ) -> EvalResult:
        # Only the improved description passes -> it should win on held-out.
        return _eval_result(eval_set, config.models, description == "improved desc")

    def fake_improver(*_: Any, **__: Any) -> dict[str, Any]:
        return {"description": "improved desc", "rationale": "better"}

    monkeypatch.setattr("skill_optimizer.cli.evaluate", fake_eval)
    monkeypatch.setattr("skill_optimizer.cli.call_improver", fake_improver)
    args = build_parser().parse_args(
        [
            "--skill-path",
            str(skill),
            "--eval-set",
            str(eval_file),
            "--out",
            str(tmp_path / "out"),
            "--iterations",
            "1",
        ]
    )
    run(args)
    out = json.loads(capsys.readouterr().out)
    assert out["best_description"] == "improved desc"


def test_unjudged_candidate_is_not_selected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    skill, eval_file = _setup(tmp_path)

    def fake_eval(
        eval_set: list[EvalQuery],
        name: Any,
        description: str,
        config: Any,
        *,
        verbose: bool = True,
    ) -> EvalResult:
        # Baseline fails (judged), candidate is entirely unjudged (score_valid=False):
        # the candidate must be skipped, so best stays the baseline.
        if description == "improved desc":
            return _unjudged_eval_result(eval_set, config.models)
        return _eval_result(eval_set, config.models, False)

    def fake_improver(*_: Any, **__: Any) -> dict[str, Any]:
        return {"description": "improved desc", "rationale": "r"}

    monkeypatch.setattr("skill_optimizer.cli.evaluate", fake_eval)
    monkeypatch.setattr("skill_optimizer.cli.call_improver", fake_improver)
    args = build_parser().parse_args(
        [
            "--skill-path",
            str(skill),
            "--eval-set",
            str(eval_file),
            "--out",
            str(tmp_path / "out"),
            "--iterations",
            "1",
        ]
    )
    run(args)
    out = json.loads(capsys.readouterr().out)
    assert out["best_description"] == "original desc"


def test_write_refused_when_best_over_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    skill, eval_file = _setup(tmp_path)
    original = (skill / "SKILL.md").read_text()

    def fake_eval(
        eval_set: list[EvalQuery],
        name: Any,
        description: str,
        config: Any,
        *,
        verbose: bool = True,
    ) -> EvalResult:
        # Baseline fails so the loop runs; the candidate would win on score but is
        # over the tiny char budget, so best stays the (also over-budget) baseline.
        return _eval_result(eval_set, config.models, description == "improved desc")

    def fake_improver(*_: Any, **__: Any) -> dict[str, Any]:
        return {"description": "improved desc", "rationale": "r"}

    monkeypatch.setattr("skill_optimizer.cli.evaluate", fake_eval)
    monkeypatch.setattr("skill_optimizer.cli.call_improver", fake_improver)
    args = build_parser().parse_args(
        [
            "--skill-path",
            str(skill),
            "--eval-set",
            str(eval_file),
            "--out",
            str(tmp_path / "out"),
            "--iterations",
            "1",
            "--max-desc-chars",
            "5",
            "--write",
        ]
    )
    run(args)
    out = json.loads(capsys.readouterr().out)
    # Over-budget candidate can't win -> best stays baseline, which is itself over the
    # 5-char budget, so --write is refused and SKILL.md is left untouched.
    assert out["best_description"] == "original desc"
    assert (skill / "SKILL.md").read_text() == original
    assert not (skill / "SKILL.md.bak").exists()


def test_improver_prompt_omits_heldout_queries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from skill_optimizer import stratified_split

    skill = tmp_path / "myskill"
    skill.mkdir()
    (skill / "SKILL.md").write_text(
        "---\nname: myskill\ndescription: original desc\n---\nBody\n"
    )
    queries: list[EvalQuery] = [
        {"query": "TRAINPOS_one", "should_trigger": True},
        {"query": "TRAINPOS_two", "should_trigger": True},
        {"query": "TRAINNEG_one", "should_trigger": False},
        {"query": "TRAINNEG_two", "should_trigger": False},
    ]
    eval_file = tmp_path / "evals.json"
    eval_file.write_text(json.dumps(queries))
    _, test_idx = stratified_split(queries, 0.35)

    captured: dict[str, str] = {}

    def fake_improver(prompt: str, *_: Any, **__: Any) -> dict[str, Any]:
        captured["prompt"] = prompt
        return {"description": "improved desc", "rationale": "r"}

    def fake_eval(
        eval_set: list[EvalQuery],
        name: Any,
        description: str,
        config: Any,
        *,
        verbose: bool = True,
    ) -> EvalResult:
        return _eval_result(eval_set, config.models, False)  # always fail -> loop runs

    monkeypatch.setattr("skill_optimizer.cli.evaluate", fake_eval)
    monkeypatch.setattr("skill_optimizer.cli.call_improver", fake_improver)
    args = build_parser().parse_args(
        [
            "--skill-path",
            str(skill),
            "--eval-set",
            str(eval_file),
            "--out",
            str(tmp_path / "out"),
            "--iterations",
            "1",
        ]
    )
    run(args)
    prompt = captured["prompt"]
    # Blinding: no held-out query text may appear in the improver prompt.
    for i in test_idx:
        assert queries[i]["query"] not in prompt
    # Sanity: at least one training query IS present.
    train_idx = [i for i in range(len(queries)) if i not in test_idx]
    assert any(queries[i]["query"] in prompt for i in train_idx)


_PRIOR_STDOUT_KEYS = {
    "skill",
    "skill_path",
    "models",
    "improver",
    "baseline_description",
    "best_description",
    "best_test_mean",
    "best_test_min",
    "select_epsilon",
    "history",
}


def test_stdout_is_strict_superset_with_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    skill, eval_file = _setup(tmp_path)

    def fake_eval(
        eval_set: list[EvalQuery],
        name: Any,
        description: str,
        config: Any,
        *,
        verbose: bool = True,
    ) -> EvalResult:
        return _eval_result(eval_set, config.models, description == "improved desc")

    def fake_improver(*_: Any, **__: Any) -> dict[str, Any]:
        return {"description": "improved desc", "rationale": "better"}

    monkeypatch.setattr("skill_optimizer.cli.evaluate", fake_eval)
    monkeypatch.setattr("skill_optimizer.cli.call_improver", fake_improver)
    args = build_parser().parse_args(
        [
            "--skill-path",
            str(skill),
            "--eval-set",
            str(eval_file),
            "--out",
            str(tmp_path / "out"),
            "--iterations",
            "1",
            "--report",
            "none",
        ]
    )
    run(args)
    out = json.loads(capsys.readouterr().out)
    # "Remove nothing": every prior top-level key survives.
    assert _PRIOR_STDOUT_KEYS <= set(out)
    # Additive skill-creator envelope is present.
    for key in (
        "exit_reason",
        "final_description",
        "iterations_run",
        "improver_failed_iterations",
        "best_score",
        "best_train_score",
        "best_test_score",
        "best_chars",
        "baseline_chars",
        "original_description",
        "train_size",
        "test_size",
        "holdout",
    ):
        assert key in out, key
    assert out["improver_failed_iterations"] == []  # present even on a clean run
    assert out["best_description"] == "improved desc"
    assert out["original_description"] == out["baseline_description"]
    assert out["best_chars"] == len("improved desc")
    # Exactly one history entry is flagged best (the winner).
    assert sum(h["is_best"] for h in out["history"]) == 1
    winner = next(h for h in out["history"] if h["is_best"])
    assert winner["description"] == "improved desc"
    # Result entries carry the original positional index (dedup-safe).
    for h in out["history"]:
        for r in h["train_results"] + h["test_results"]:
            assert "index" in r


def test_report_none_writes_no_html(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    skill, eval_file = _setup(tmp_path)
    monkeypatch.setattr("skill_optimizer.cli.evaluate", _stub_eval_all_pass)
    monkeypatch.setattr("skill_optimizer.cli.call_improver", _no_improver)
    out_dir = tmp_path / "out"
    args = build_parser().parse_args(
        [
            "--skill-path",
            str(skill),
            "--eval-set",
            str(eval_file),
            "--out",
            str(out_dir),
            "--report",
            "none",
        ]
    )
    run(args)  # must not crash
    assert not list(out_dir.glob("*.html"))


def test_results_dir_creates_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    skill, eval_file = _setup(tmp_path)
    monkeypatch.setattr("skill_optimizer.cli.evaluate", _stub_eval_all_pass)
    monkeypatch.setattr("skill_optimizer.cli.call_improver", _no_improver)
    rdir = tmp_path / "results"
    args = build_parser().parse_args(
        [
            "--skill-path",
            str(skill),
            "--eval-set",
            str(eval_file),
            "--results-dir",
            str(rdir),
        ]
    )
    run(args)
    subdirs = [p for p in rdir.iterdir() if p.is_dir()]
    assert len(subdirs) == 1  # one timestamped run dir
    run_dir = subdirs[0]
    assert (run_dir / "results.json").exists()
    assert (run_dir / "report.html").exists()
    assert (run_dir / "logs").is_dir()


def test_out_and_results_dir_are_mutually_exclusive(tmp_path: Path) -> None:
    skill, eval_file = _setup(tmp_path)
    args = build_parser().parse_args(
        [
            "--skill-path",
            str(skill),
            "--eval-set",
            str(eval_file),
            "--out",
            str(tmp_path / "out"),
            "--results-dir",
            str(tmp_path / "results"),
        ]
    )
    with pytest.raises(SystemExit):
        run(args)


def test_optional_out_uses_tempdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    skill, eval_file = _setup(tmp_path)
    monkeypatch.setattr("skill_optimizer.cli.evaluate", _stub_eval_all_pass)
    monkeypatch.setattr("skill_optimizer.cli.call_improver", _no_improver)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))  # mkdtemp lands here
    args = build_parser().parse_args(
        ["--skill-path", str(skill), "--eval-set", str(eval_file)]
    )
    run(args)
    assert list(tmp_path.glob("skilldesc-*"))  # a temp run dir was created


def _full_eval_by_index(
    eval_set: list[EvalQuery], models: tuple[str, ...], passing: set[int]
) -> EvalResult:
    """Build a full EvalResult where exactly the queries at ``passing`` indices pass.

    Non-uniform by design: the uniform ``_eval_result`` helper can only produce mean
    ``0.0`` or ``1.0``, which cannot express a judged candidate that loses or ties on a
    partial held-out split. Runs through the real ``aggregate`` so every confusion
    sub-structure stays internally consistent for ``subset_result``.

    Args:
        eval_set: The full eval set.
        models: The evaluated model ids.
        passing: Positional indices of the queries that should pass.

    Returns:
        The aggregated full-set :class:`EvalResult`.
    """
    raw: dict[tuple[int, str], list[bool]] = {}
    for i, q in enumerate(eval_set):
        hit = i in passing
        # A should-trigger query passes iff it triggers; a should-NOT-trigger query
        # passes iff it stays silent -- pick booleans that realize `hit` either way.
        triggered = hit if q["should_trigger"] else not hit
        for mdl in models:
            raw[(i, mdl)] = [triggered, triggered, triggered]
    return aggregate(raw, eval_set, models, 0.5, "full")


def test_incumbent_stays_best_when_candidate_loses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    skill, eval_file = _setup(tmp_path)
    queries: list[EvalQuery] = json.loads(eval_file.read_text())
    _train_idx, test_idx = stratified_split(queries, 0.35)

    def fake_eval(
        eval_set: list[EvalQuery],
        name: Any,
        description: str,
        config: Any,
        *,
        verbose: bool = True,
    ) -> EvalResult:
        if description == "original desc":
            # Baseline: held-out queries all pass (high test mean), but the train slice
            # fails, so the loop iterates instead of exiting early.
            return _full_eval_by_index(eval_set, config.models, set(test_idx))
        # Candidate: strictly worse on held-out (nothing passes) -> it must lose.
        return _full_eval_by_index(eval_set, config.models, set())

    def fake_improver(*_: Any, **__: Any) -> dict[str, Any]:
        return {"description": "a losing candidate description", "rationale": "r"}

    monkeypatch.setattr("skill_optimizer.cli.evaluate", fake_eval)
    monkeypatch.setattr("skill_optimizer.cli.call_improver", fake_improver)
    args = build_parser().parse_args(
        [
            "--skill-path",
            str(skill),
            "--eval-set",
            str(eval_file),
            "--out",
            str(tmp_path / "out"),
            "--iterations",
            "1",
        ]
    )
    run(args)
    out = json.loads(capsys.readouterr().out)
    # The candidate was actually scored (not a vacuous early-exit).
    assert len(out["history"]) > 1
    # Exactly one entry is best, and it is the baseline (the loser did not displace it).
    assert sum(h["is_best"] for h in out["history"]) == 1
    winner = next(h for h in out["history"] if h["is_best"])
    assert winner["description"] == "original desc"
    assert winner["iteration"] == 0


def test_incumbent_stays_best_when_candidate_ties(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    skill, eval_file = _setup(tmp_path)
    queries: list[EvalQuery] = json.loads(eval_file.read_text())
    _train_idx, test_idx = stratified_split(queries, 0.35)

    def fake_eval(
        eval_set: list[EvalQuery],
        name: Any,
        description: str,
        config: Any,
        *,
        verbose: bool = True,
    ) -> EvalResult:
        # Baseline and candidate both pass exactly the held-out queries -> equal test
        # mean (a tie); the baseline's train slice still fails, so the loop iterates.
        return _full_eval_by_index(eval_set, config.models, set(test_idx))

    def fake_improver(*_: Any, **__: Any) -> dict[str, Any]:
        # Longer than the 13-char baseline so the shorter-wins tie-break cannot fire.
        return {
            "description": "a tying candidate description that is clearly longer",
            "rationale": "r",
        }

    monkeypatch.setattr("skill_optimizer.cli.evaluate", fake_eval)
    monkeypatch.setattr("skill_optimizer.cli.call_improver", fake_improver)
    args = build_parser().parse_args(
        [
            "--skill-path",
            str(skill),
            "--eval-set",
            str(eval_file),
            "--out",
            str(tmp_path / "out"),
            "--iterations",
            "1",
        ]
    )
    run(args)
    out = json.loads(capsys.readouterr().out)
    assert len(out["history"]) > 1
    # A tie with no weakest-model gain and no length advantage does not replace best.
    assert sum(h["is_best"] for h in out["history"]) == 1
    winner = next(h for h in out["history"] if h["is_best"])
    assert winner["description"] == "original desc"
    # Non-vacuity: the candidate really did tie the incumbent's held-out mean.
    cand = next(h for h in out["history"] if h["iteration"] == 1)
    assert cand["test_mean"] == winner["test_mean"]


def test_each_description_evaluated_exactly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    skill, eval_file = _setup(tmp_path)
    calls: list[str] = []
    counter = {"n": 0}

    def fake_eval(
        eval_set: list[EvalQuery],
        name: Any,
        description: str,
        config: Any,
        *,
        verbose: bool = True,
    ) -> EvalResult:
        calls.append(description)
        # Everything fails -> the loop never early-exits and runs every iteration.
        return _eval_result(eval_set, config.models, False)

    def fake_improver(*_: Any, **__: Any) -> dict[str, Any]:
        counter["n"] += 1
        # Distinct each call, and longer than baseline so it can't win on length.
        return {"description": f"candidate number {counter['n']}", "rationale": "r"}

    monkeypatch.setattr("skill_optimizer.cli.evaluate", fake_eval)
    monkeypatch.setattr("skill_optimizer.cli.call_improver", fake_improver)
    args = build_parser().parse_args(
        [
            "--skill-path",
            str(skill),
            "--eval-set",
            str(eval_file),
            "--out",
            str(tmp_path / "out"),
            "--iterations",
            "2",
        ]
    )
    run(args)
    capsys.readouterr()
    # The loop iterated (non-vacuous) and each description was evaluated exactly once --
    # one full-set eval per baseline/candidate, never the old train+test+full x~2.
    assert len(calls) > 1
    assert len(calls) == len(set(calls))
    assert calls.count("original desc") == 1


def test_explicit_report_path_with_results_dir_writes_both(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    skill, eval_file = _setup(tmp_path)
    monkeypatch.setattr("skill_optimizer.cli.evaluate", _stub_eval_all_pass)
    monkeypatch.setattr("skill_optimizer.cli.call_improver", _no_improver)
    explicit = tmp_path / "explicit_report.html"
    rdir = tmp_path / "results"
    args = build_parser().parse_args(
        [
            "--skill-path",
            str(skill),
            "--eval-set",
            str(eval_file),
            "--results-dir",
            str(rdir),
            "--report",
            str(explicit),
        ]
    )
    run(args)
    assert explicit.exists()
    run_dirs = [p for p in rdir.iterdir() if p.is_dir()]
    assert len(run_dirs) == 1  # one timestamped run dir
    report_html = run_dirs[0] / "report.html"
    assert report_html.exists()
    # Both carry the rendered report (skill name in the title), not just a placeholder.
    for path in (explicit, report_html):
        assert "myskill" in path.read_text()


def test_explicit_report_path_without_results_dir_writes_only_that(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    skill, eval_file = _setup(tmp_path)
    monkeypatch.setattr("skill_optimizer.cli.evaluate", _stub_eval_all_pass)
    monkeypatch.setattr("skill_optimizer.cli.call_improver", _no_improver)
    explicit = tmp_path / "reports" / "explicit_report.html"
    out_dir = tmp_path / "out"
    args = build_parser().parse_args(
        [
            "--skill-path",
            str(skill),
            "--eval-set",
            str(eval_file),
            "--out",
            str(out_dir),
            "--report",
            str(explicit),
        ]
    )
    run(args)
    assert explicit.exists()
    assert "myskill" in explicit.read_text()
    # No results-dir -> only the explicit report is written (out/ holds JSON only).
    assert not list(out_dir.glob("*.html"))
    assert list(explicit.parent.glob("*.html")) == [explicit]


# --------------------------------------------------------------------------- #
# M1: iteration + eval-set preflight (validation before any side effect)
# --------------------------------------------------------------------------- #
def test_validate_iterations_accepts_boundaries() -> None:
    assert _validate_iterations(0) == 0
    assert _validate_iterations(50) == 50


def test_validate_iterations_rejects_out_of_range() -> None:
    for bad in (-1, 51):
        with pytest.raises(ValueError, match=r"--iterations must be between 0 and 50"):
            _validate_iterations(bad)


def _write_eval(tmp_path: Path, content: str) -> Path:
    eval_file = tmp_path / "evals.json"
    eval_file.write_text(content)
    return eval_file


def test_load_eval_set_bare_list(tmp_path: Path) -> None:
    items = [{"query": "a", "should_trigger": True}]
    assert _load_eval_set(_write_eval(tmp_path, json.dumps(items))) == items


def test_load_eval_set_queries_wrapper(tmp_path: Path) -> None:
    items = [{"query": "a", "should_trigger": False}]
    path = _write_eval(tmp_path, json.dumps({"queries": items}))
    assert _load_eval_set(path) == items


def test_load_eval_set_evals_wrapper(tmp_path: Path) -> None:
    items = [{"query": "a", "should_trigger": True}]
    path = _write_eval(tmp_path, json.dumps({"evals": items}))
    assert _load_eval_set(path) == items


def test_load_eval_set_ignores_unrelated_wrapper_metadata(tmp_path: Path) -> None:
    items = [{"query": "a", "should_trigger": True}]
    path = _write_eval(
        tmp_path, json.dumps({"version": 2, "note": "meta", "queries": items})
    )
    assert _load_eval_set(path) == items


def test_load_eval_set_preserves_order_and_duplicates(tmp_path: Path) -> None:
    items = [
        {"query": "dup", "should_trigger": True},
        {"query": "dup", "should_trigger": True},
        {"query": "z", "should_trigger": False},
    ]
    assert _load_eval_set(_write_eval(tmp_path, json.dumps(items))) == items


def test_load_eval_set_preserves_extra_item_keys(tmp_path: Path) -> None:
    items = [{"query": "a", "should_trigger": True, "note": "keep", "weight": 3}]
    loaded = _load_eval_set(_write_eval(tmp_path, json.dumps(items)))
    first = cast("dict[str, Any]", loaded[0])
    assert first["note"] == "keep"
    assert first["weight"] == 3


_INVALID_EVAL_CASES: list[tuple[str, str, str]] = [
    ("invalid_json", "{not valid json", "Invalid eval set: invalid JSON"),
    (
        "root_int",
        json.dumps(42),
        "Invalid eval set: root must be a list or wrapper object",
    ),
    (
        "root_str",
        json.dumps("nope"),
        "Invalid eval set: root must be a list or wrapper object",
    ),
    (
        "wrapper_neither",
        json.dumps({"foo": []}),
        "Invalid eval set: wrapper must contain exactly one of 'queries' or 'evals'",
    ),
    (
        "wrapper_both",
        json.dumps({"queries": [{"query": "a", "should_trigger": True}], "evals": []}),
        "Invalid eval set: wrapper must contain exactly one of 'queries' or 'evals'",
    ),
    (
        "queries_not_list",
        json.dumps({"queries": "x"}),
        "Invalid eval set: 'queries' must be a list",
    ),
    (
        "evals_not_list",
        json.dumps({"evals": {}}),
        "Invalid eval set: 'evals' must be a list",
    ),
    ("empty_bare", json.dumps([]), "Invalid eval set: must contain at least one query"),
    (
        "empty_wrapped",
        json.dumps({"evals": []}),
        "Invalid eval set: must contain at least one query",
    ),
    ("item_not_object", json.dumps([42]), "Invalid eval set: item 0 must be an object"),
    (
        "query_missing",
        json.dumps([{"should_trigger": True}]),
        "Invalid eval set: item 0 field 'query' must be a string",
    ),
    (
        "query_not_str",
        json.dumps([{"query": 5, "should_trigger": True}]),
        "Invalid eval set: item 0 field 'query' must be a string",
    ),
    (
        "should_trigger_missing",
        json.dumps([{"query": "a", "should_trigger": True}, {"query": "b"}]),
        "Invalid eval set: item 1 field 'should_trigger' must be a boolean",
    ),
    (
        "should_trigger_not_bool",
        json.dumps([{"query": "a", "should_trigger": 1}]),
        "Invalid eval set: item 0 field 'should_trigger' must be a boolean",
    ),
]


@pytest.mark.parametrize(
    ("case_id", "content", "message"),
    _INVALID_EVAL_CASES,
    ids=[case[0] for case in _INVALID_EVAL_CASES],
)
def test_load_eval_set_invalid_messages(
    tmp_path: Path, case_id: str, content: str, message: str
) -> None:
    with pytest.raises(ValueError) as excinfo:
        _load_eval_set(_write_eval(tmp_path, content))
    assert str(excinfo.value) == message


def test_load_eval_set_missing_file_is_friendly(tmp_path: Path) -> None:
    # A read error (missing/unreadable file) maps to the same ``Invalid eval set: ...``
    # contract as bad content, not a raw OSError traceback, so a stdout-parsing caller
    # fails legibly. The OS reason text varies, so match only the stable prefix + path.
    missing = tmp_path / "does-not-exist.json"
    with pytest.raises(ValueError) as excinfo:
        _load_eval_set(missing)
    assert str(excinfo.value).startswith(f"Invalid eval set: cannot read {missing}:")


def test_load_eval_set_deeply_nested_json_is_friendly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A pathologically nested eval file overflows json's C-stack recursion guard,
    # surfacing as RecursionError (a RuntimeError, not JSONDecodeError). It must map to
    # the same friendly "Invalid eval set: invalid JSON" as any other malformed JSON,
    # never escape as a mid-run traceback (the precondition contract). RecursionError is
    # injected deterministically here; a real overflow depth is platform-dependent.
    eval_path = tmp_path / "eval.json"
    eval_path.write_text("[]")

    def _raise_recursion(*_: Any, **__: Any) -> Any:
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(json, "loads", _raise_recursion)
    with pytest.raises(ValueError) as excinfo:
        _load_eval_set(eval_path)
    assert str(excinfo.value) == "Invalid eval set: invalid JSON"


def test_report_paths_sanitizes_malicious_skill_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The auto-report filename embeds the attacker-controlled skill name. A hostile
    # ``name:`` must not traverse out of the temp dir: the sanitized token keeps the
    # report file as a single component directly inside ``gettempdir()``.
    from types import SimpleNamespace

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    args = SimpleNamespace(results_dir=None, out=None, report="auto")
    _out, results_dir, live = cli_module._report_paths(  # pyright: ignore[reportPrivateUsage]
        cast("Any", args), "../../../../etc/cron.d/evil", "2026-07-15_000000"
    )
    assert results_dir is None
    assert live is not None
    assert live.parent == Path(tempfile.gettempdir())
    assert ".." not in live.name
    assert "/" not in live.name


# ---- Nine-cell destination x report no-side-effect matrix -------------------- #
_DESTINATIONS = ("none", "out", "results-dir")
_REPORTS = ("auto", "none", "explicit")
_CELLS = [(dest, rep) for dest in _DESTINATIONS for rep in _REPORTS]

_PREFLIGHT_BOUNDARIES = (
    "parse_skill_md",
    "_validate_iterations",
    "_load_eval_set",
    "stratified_split",
)
_FORBIDDEN_BOUNDARIES = (
    "_resolve_config",
    "_report_paths",
    "_write_placeholder_and_open",
    "evaluate",
    "call_improver",
)


def _make_skill(tmp_path: Path) -> Path:
    skill = tmp_path / "myskill"
    skill.mkdir()
    (skill / "SKILL.md").write_text(
        "---\nname: myskill\ndescription: original desc\n---\nBody\n"
    )
    return skill


def _cell_argv(
    skill: Path,
    eval_file: Path,
    tmp_path: Path,
    destination: str,
    report: str,
    iterations: str = "1",
) -> tuple[list[str], list[Path]]:
    """Build argv for one destination/report cell and the sentinel paths to check."""
    argv = [
        "--skill-path",
        str(skill),
        "--eval-set",
        str(eval_file),
        "--iterations",
        iterations,
    ]
    sentinels: list[Path] = []
    if destination == "out":
        out = tmp_path / "out_dir"
        argv += ["--out", str(out)]
        sentinels.append(out)
    elif destination == "results-dir":
        rdir = tmp_path / "results_dir"
        argv += ["--results-dir", str(rdir)]
        sentinels.append(rdir)
    if report == "none":
        argv += ["--report", "none"]
    elif report == "explicit":
        explicit = tmp_path / "explicit_report.html"
        argv += ["--report", str(explicit)]
        sentinels.append(explicit)
    return argv, sentinels


def _run_rejection(
    monkeypatch: pytest.MonkeyPatch, argv: list[str]
) -> tuple[list[str], dict[str, int], str]:
    """Run ``run`` expecting SystemExit, recording the boundary trace + fs activity.

    Preflight boundaries delegate to the real implementation (so validation fires);
    downstream boundaries raise if reached (proving no config/report/browser/eval side
    effect). ``tempfile.mkdtemp`` / ``Path.mkdir`` / ``Path.write_text`` are counted.
    """
    trace: list[str] = []
    counters = {"mkdtemp": 0, "mkdir": 0, "write_text": 0}

    def _make_preflight(name: str) -> Any:
        real: Any = getattr(cli_module, name)

        def wrapper(*a: Any, **k: Any) -> Any:
            trace.append(name)
            return real(*a, **k)

        return wrapper

    def _make_forbidden(name: str) -> Any:
        def wrapper(*_: Any, **__: Any) -> Any:
            trace.append(name)
            raise AssertionError(f"{name} must not run on invalid input")

        return wrapper

    for name in _PREFLIGHT_BOUNDARIES:
        monkeypatch.setattr(cli_module, name, _make_preflight(name))
    for name in _FORBIDDEN_BOUNDARIES:
        monkeypatch.setattr(cli_module, name, _make_forbidden(name))

    orig_mkdtemp: Any = tempfile.mkdtemp
    orig_mkdir: Any = Path.mkdir
    orig_write_text: Any = Path.write_text

    def spy_mkdtemp(*a: Any, **k: Any) -> Any:
        counters["mkdtemp"] += 1
        return orig_mkdtemp(*a, **k)

    def spy_mkdir(self: Any, *a: Any, **k: Any) -> Any:
        counters["mkdir"] += 1
        return orig_mkdir(self, *a, **k)

    def spy_write_text(self: Any, *a: Any, **k: Any) -> Any:
        counters["write_text"] += 1
        return orig_write_text(self, *a, **k)

    monkeypatch.setattr(cli_module.tempfile, "mkdtemp", spy_mkdtemp)
    monkeypatch.setattr(Path, "mkdir", spy_mkdir)
    monkeypatch.setattr(Path, "write_text", spy_write_text)

    args = build_parser().parse_args(argv)
    with pytest.raises(SystemExit) as excinfo:
        run(args)
    return trace, counters, str(excinfo.value)


_INVALID_HOLDOUT_CASES: list[tuple[str, str, str]] = [
    (
        "nan",
        "nan",
        "Invalid holdout split: holdout must be finite and satisfy 0 <= holdout < 1",
    ),
    (
        "inf",
        "inf",
        "Invalid holdout split: holdout must be finite and satisfy 0 <= holdout < 1",
    ),
    (
        "negative",
        "-0.1",
        "Invalid holdout split: holdout must be finite and satisfy 0 <= holdout < 1",
    ),
    (
        "one",
        "1.0",
        "Invalid holdout split: holdout must be finite and satisfy 0 <= holdout < 1",
    ),
    (
        "two",
        "2.0",
        "Invalid holdout split: holdout must be finite and satisfy 0 <= holdout < 1",
    ),
    (
        "unsafe_high",
        "0.75",
        "Invalid holdout split: positive class cannot retain a training query at "
        "this holdout",
    ),
]


def test_matrix_case_counts() -> None:
    # 2 invalid iteration values * 9 cells == 18; 14 eval families * 9 cells == 126;
    # 6 invalid holdout values * 9 cells == 54.
    assert len(_CELLS) == 9
    assert len(_INVALID_EVAL_CASES) == 14
    assert len(_INVALID_HOLDOUT_CASES) == 6


@pytest.mark.parametrize("iterations", ["-1", "51"])
@pytest.mark.parametrize(("destination", "report"), _CELLS)
def test_invalid_iterations_no_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    iterations: str,
    destination: str,
    report: str,
) -> None:
    skill = _make_skill(tmp_path)
    eval_file = _write_eval(
        tmp_path,
        json.dumps(
            [
                {"query": "a", "should_trigger": True},
                {"query": "b", "should_trigger": True},
                {"query": "c", "should_trigger": False},
                {"query": "d", "should_trigger": False},
            ]
        ),
    )
    argv, sentinels = _cell_argv(
        skill, eval_file, tmp_path, destination, report, iterations
    )
    trace, counters, message = _run_rejection(monkeypatch, argv)
    assert trace == ["parse_skill_md", "_validate_iterations"]
    assert message == "--iterations must be between 0 and 50"
    assert counters == {"mkdtemp": 0, "mkdir": 0, "write_text": 0}
    assert all(not path.exists() for path in sentinels)


@pytest.mark.parametrize(
    ("case_id", "content", "message"),
    _INVALID_EVAL_CASES,
    ids=[case[0] for case in _INVALID_EVAL_CASES],
)
@pytest.mark.parametrize(("destination", "report"), _CELLS)
def test_invalid_eval_set_no_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case_id: str,
    content: str,
    message: str,
    destination: str,
    report: str,
) -> None:
    skill = _make_skill(tmp_path)
    eval_file = _write_eval(tmp_path, content)
    argv, sentinels = _cell_argv(skill, eval_file, tmp_path, destination, report)
    trace, counters, exit_message = _run_rejection(monkeypatch, argv)
    assert trace == ["parse_skill_md", "_validate_iterations", "_load_eval_set"]
    assert exit_message == message
    assert counters == {"mkdtemp": 0, "mkdir": 0, "write_text": 0}
    assert all(not path.exists() for path in sentinels)


@pytest.mark.parametrize(
    ("case_id", "value", "message"),
    _INVALID_HOLDOUT_CASES,
    ids=[case[0] for case in _INVALID_HOLDOUT_CASES],
)
@pytest.mark.parametrize(("destination", "report"), _CELLS)
def test_invalid_holdout_no_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case_id: str,
    value: str,
    message: str,
    destination: str,
    report: str,
) -> None:
    skill = _make_skill(tmp_path)
    eval_file = _write_eval(
        tmp_path,
        json.dumps(
            [
                {"query": "a", "should_trigger": True},
                {"query": "b", "should_trigger": True},
                {"query": "c", "should_trigger": False},
                {"query": "d", "should_trigger": False},
            ]
        ),
    )
    argv, sentinels = _cell_argv(skill, eval_file, tmp_path, destination, report)
    argv += ["--holdout", value]
    trace, counters, exit_message = _run_rejection(monkeypatch, argv)
    assert trace == [
        "parse_skill_md",
        "_validate_iterations",
        "_load_eval_set",
        "stratified_split",
    ]
    assert exit_message == message
    assert counters == {"mkdtemp": 0, "mkdir": 0, "write_text": 0}
    assert all(not path.exists() for path in sentinels)


_NO_HOLDOUT_NULL_HISTORY_FIELDS = (
    "test_mean",
    "test_min",
    "test_passed",
    "test_failed",
    "test_total",
    "test_unjudged",
    "test_score",
)


def test_no_holdout_nulls_test_fields_and_selects_on_train(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    skill, eval_file = _setup(tmp_path)

    def fake_eval(
        eval_set: list[EvalQuery],
        name: Any,
        description: str,
        config: Any,
        *,
        verbose: bool = True,
    ) -> EvalResult:
        # Baseline fails, candidate passes -> candidate wins on the TRAIN score.
        return _eval_result(eval_set, config.models, description == "improved desc")

    def fake_improver(*_: Any, **__: Any) -> dict[str, Any]:
        return {"description": "improved desc", "rationale": "better"}

    monkeypatch.setattr("skill_optimizer.cli.evaluate", fake_eval)
    monkeypatch.setattr("skill_optimizer.cli.call_improver", fake_improver)
    args = build_parser().parse_args(
        [
            "--skill-path",
            str(skill),
            "--eval-set",
            str(eval_file),
            "--out",
            str(tmp_path / "out"),
            "--iterations",
            "1",
            "--holdout",
            "0",
            "--report",
            "none",
        ]
    )
    run(args)
    out = json.loads(capsys.readouterr().out)
    # A train-improving candidate wins with the holdout disabled.
    assert out["best_description"] == "improved desc"
    # Held-out measurement fields are JSON null; structural values are retained.
    assert out["best_test_mean"] is None
    assert out["best_test_min"] is None
    assert out["best_test_score"] is None
    assert out["test_size"] == 0
    assert out["holdout"] == 0.0
    # best_score is train-derived and real (train data remains populated).
    assert out["best_score"] == out["best_train_score"]
    assert out["best_train_score"] != "n/a"
    for h in out["history"]:
        for field in _NO_HOLDOUT_NULL_HISTORY_FIELDS:
            assert h[field] is None, field
        assert h["test_results"] == []
        assert h["train_results"]  # train view is real
        assert h["train_total"] is not None


def test_no_holdout_report_renders_without_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    skill, eval_file = _setup(tmp_path)
    monkeypatch.setattr("skill_optimizer.cli.evaluate", _stub_eval_all_pass)
    monkeypatch.setattr("skill_optimizer.cli.call_improver", _no_improver)
    report = tmp_path / "r.html"
    args = build_parser().parse_args(
        [
            "--skill-path",
            str(skill),
            "--eval-set",
            str(eval_file),
            "--out",
            str(tmp_path / "out"),
            "--holdout",
            "0",
            "--report",
            str(report),
        ]
    )
    run(args)  # read-only report.py must render the no-holdout payload
    assert report.exists()
    assert "myskill" in report.read_text()


def test_valid_control_trace_reaches_evaluate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    skill, eval_file = _setup(tmp_path)
    trace: list[str] = []

    def _make_traced(name: str) -> Any:
        real: Any = getattr(cli_module, name)

        def wrapper(*a: Any, **k: Any) -> Any:
            trace.append(name)
            return real(*a, **k)

        return wrapper

    for name in (
        "parse_skill_md",
        "_validate_iterations",
        "_load_eval_set",
        "stratified_split",
        "_resolve_config",
        "_report_paths",
        "_write_placeholder_and_open",
    ):
        monkeypatch.setattr(cli_module, name, _make_traced(name))

    def traced_eval(
        eval_set: list[EvalQuery],
        name: Any,
        description: Any,
        config: Any,
        **__: Any,
    ) -> EvalResult:
        trace.append("evaluate")
        return _eval_result(eval_set, config.models, True)  # all pass -> early exit

    monkeypatch.setattr(cli_module, "evaluate", traced_eval)
    monkeypatch.setattr(cli_module, "call_improver", _no_improver)
    args = build_parser().parse_args(
        [
            "--skill-path",
            str(skill),
            "--eval-set",
            str(eval_file),
            "--out",
            str(tmp_path / "out"),
            "--iterations",
            "1",
            "--report",
            str(tmp_path / "r.html"),
        ]
    )
    run(args)
    capsys.readouterr()
    # Exact run()-level preflight order up to the baseline evaluate. (_optimize then
    # re-validates iterations as its own first action, so a later _validate_iterations
    # entry is expected and not part of this prefix.)
    assert trace[:8] == [
        "parse_skill_md",
        "_validate_iterations",
        "_load_eval_set",
        "stratified_split",
        "_resolve_config",
        "_report_paths",
        "_write_placeholder_and_open",
        "evaluate",
    ]


def test_run_survives_raising_browser_opener(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    skill, eval_file = _setup(tmp_path)
    monkeypatch.setattr("skill_optimizer.cli.evaluate", _stub_eval_all_pass)
    monkeypatch.setattr("skill_optimizer.cli.call_improver", _no_improver)
    called = {"raised": False}

    def raiser(*_: Any, **__: Any) -> bool:
        called["raised"] = True
        raise RuntimeError("no browser in this environment")

    # Overrides the autouse _no_browser fixture (which ran first): a real raise here
    # must be swallowed by the production handler, not abort the run.
    monkeypatch.setattr("skill_optimizer.cli.webbrowser.open", raiser, raising=False)
    args = build_parser().parse_args(
        [
            "--skill-path",
            str(skill),
            "--eval-set",
            str(eval_file),
            "--out",
            str(tmp_path / "out"),
            "--report",
            str(tmp_path / "r.html"),
        ]
    )
    run(args)  # must NOT raise despite webbrowser.open raising
    out = json.loads(capsys.readouterr().out)
    assert out["best_description"] == "original desc"
    assert called["raised"] is True  # the opener really was invoked and raised


# --------------------------------------------------------------------------- #
# M3: typed retry, bounded launches, live state, and the public failure ledger.
# --------------------------------------------------------------------------- #
def _fake_eval_all_fail(
    eval_set: list[EvalQuery], name: Any, description: Any, config: Any, **__: Any
) -> EvalResult:
    return _eval_result(eval_set, config.models, False)


def _scripted_call_improver(
    outcomes: list[tuple[Any, ...]],
) -> tuple[Any, dict[str, Any]]:
    """Build a fake ``call_improver`` that consumes one scripted outcome per call.

    Each outcome is ``("ok", desc)`` (return a proposal), ``("retry", kind, message)``
    (raise a typed retryable error), or ``("fatal", exc)`` (raise ``exc``).
    """
    state: dict[str, Any] = {"n": 0, "log_paths": []}

    def fake(
        prompt: str, model: str, effort: Any, timeout: int, **kwargs: Any
    ) -> dict[str, Any]:
        index = cast("int", state["n"])
        state["n"] = index + 1
        cast("list[Any]", state["log_paths"]).append(kwargs.get("log_path"))
        outcome = outcomes[index]
        kind = cast("str", outcome[0])
        if kind == "ok":
            return {"description": cast("str", outcome[1]), "rationale": "r"}
        if kind == "retry":
            raise ImproverRetryableError(
                cast("str", outcome[1]), cast("str", outcome[2])
            )
        raise cast("Exception", outcome[1])

    return fake, state


def _run_with_scripted_improver(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outcomes: list[tuple[Any, ...]],
    iterations: int,
) -> dict[str, Any]:
    skill, eval_file = _setup(tmp_path)
    fake, state = _scripted_call_improver(outcomes)
    monkeypatch.setattr("skill_optimizer.cli.evaluate", _fake_eval_all_fail)
    monkeypatch.setattr("skill_optimizer.cli.call_improver", fake)
    args = build_parser().parse_args(
        [
            "--skill-path",
            str(skill),
            "--eval-set",
            str(eval_file),
            "--out",
            str(tmp_path / "out"),
            "--iterations",
            str(iterations),
            "--report",
            "none",
        ]
    )
    run(args)
    return state


def test_first_attempt_success_one_call_empty_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    state = _run_with_scripted_improver(tmp_path, monkeypatch, [("ok", "cand1")], 1)
    out = json.loads(capsys.readouterr().out)
    assert state["n"] == 1  # one outer call, no retry
    assert state["log_paths"][0].name == "iter1_improve.json"
    assert out["improver_failed_iterations"] == []
    assert out["iterations_run"] == 1


def test_retryable_then_success_two_calls_distinct_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)
    state = _run_with_scripted_improver(
        tmp_path,
        monkeypatch,
        [("retry", "timeout", "Improver timed out"), ("ok", "cand1")],
        1,
    )
    out = json.loads(capsys.readouterr().out)
    assert state["n"] == 2  # two outer calls
    assert [p.name for p in state["log_paths"]] == [
        "iter1_improve.json",
        "iter1_improve_retry.json",
    ]
    assert out["improver_failed_iterations"] == []  # success on attempt 2
    assert "Improver retryable attempt 1 failed (timeout)." in caplog.text


def test_retryable_twice_records_and_continues(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)
    state = _run_with_scripted_improver(
        tmp_path,
        monkeypatch,
        [
            ("retry", "timeout", "Improver timed out"),
            ("retry", "invalid_output", "Improver returned no JSON"),
            ("ok", "cand2"),
        ],
        2,
    )
    out = json.loads(capsys.readouterr().out)
    assert state["n"] == 3  # slot 1: two calls (exhausted); slot 2: one call
    assert out["iterations_run"] == 2  # both slots entered
    ledger = out["improver_failed_iterations"]
    assert len(ledger) == 1
    record = ledger[0]
    assert set(record) == {"iteration", "attempt_count", "errors"}
    assert record["iteration"] == 1
    assert record["attempt_count"] == 2
    assert [e["attempt"] for e in record["errors"]] == [1, 2]
    assert record["errors"][0] == {
        "attempt": 1,
        "kind": "timeout",
        "message": "Improver timed out",
    }
    assert record["errors"][1] == {
        "attempt": 2,
        "kind": "invalid_output",
        "message": "Improver returned no JSON",
    }
    assert "Improver retry attempts exhausted for iteration 1; continuing." in (
        caplog.text
    )


def test_first_attempt_fatal_propagates_without_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    skill, eval_file = _setup(tmp_path)
    fake, state = _scripted_call_improver(
        [("fatal", ImproverFatalProcessError("Improver process exited with status 1"))]
    )
    monkeypatch.setattr("skill_optimizer.cli.evaluate", _fake_eval_all_fail)
    monkeypatch.setattr("skill_optimizer.cli.call_improver", fake)
    args = build_parser().parse_args(
        [
            "--skill-path",
            str(skill),
            "--eval-set",
            str(eval_file),
            "--out",
            str(tmp_path / "out"),
            "--iterations",
            "1",
            "--report",
            "none",
        ]
    )
    with pytest.raises(ImproverFatalProcessError, match="status 1"):
        run(args)
    assert state["n"] == 1  # one call; no continuation, no success envelope


def test_main_presents_fatal_improver_as_clean_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    # run() propagates ImproverFatalProcessError by design (pinned above); the CLI
    # boundary main() must instead present it as a clean exit: code 1, empty stdout (no
    # success envelope), and one legible log line — never a raised traceback — so a
    # stdout-parsing caller fails legibly rather than on a JSONDecodeError.
    skill, eval_file = _setup(tmp_path)
    fake, state = _scripted_call_improver(
        [("fatal", ImproverFatalProcessError("Improver process exited with status 7"))]
    )
    monkeypatch.setattr("skill_optimizer.cli.evaluate", _fake_eval_all_fail)
    monkeypatch.setattr("skill_optimizer.cli.call_improver", fake)
    caplog.set_level(logging.ERROR)
    code = cli_module.main(
        [
            "--skill-path",
            str(skill),
            "--eval-set",
            str(eval_file),
            "--out",
            str(tmp_path / "out"),
            "--iterations",
            "1",
            "--report",
            "none",
        ]
    )
    assert code == 1  # returned cleanly, did not raise a traceback
    assert capsys.readouterr().out == ""  # no success envelope on stdout
    assert state["n"] == 1  # the fatal call was reached (single attempt, no retry)
    assert "Improver failed fatally" in caplog.text
    assert "status 7" in caplog.text


def test_claude_available_detects_missing_present_and_nonexecutable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The detection helper backs the CLI preflight: a missing path is unavailable, an
    # existing executable file is available, and a present-but-non-executable file is
    # unavailable (mirrors what the subprocess spawn would find).
    from skill_optimizer._process import claude_available

    monkeypatch.setenv("SKILL_OPTIMIZER_CLAUDE_BIN", str(tmp_path / "nope"))
    assert claude_available() is False

    stub = tmp_path / "claude"
    stub.write_text("#!/bin/sh\n")
    stub.chmod(0o755)
    monkeypatch.setenv("SKILL_OPTIMIZER_CLAUDE_BIN", str(stub))
    assert claude_available() is True

    plain = tmp_path / "claude.txt"
    plain.write_text("not executable")
    monkeypatch.setenv("SKILL_OPTIMIZER_CLAUDE_BIN", str(plain))
    assert claude_available() is False


def test_run_exits_cleanly_when_claude_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A missing/unrunnable claude must fail preflight as one legible setup line — before
    # any evaluation, artifact, or stdout envelope — not as a mid-run traceback.
    skill, eval_file = _setup(tmp_path)
    # Override the autouse pass-through to simulate an absent CLI.
    monkeypatch.setattr("skill_optimizer.cli.claude_available", lambda: False)

    def _boom(*_: Any, **__: Any) -> Any:
        raise AssertionError("evaluate must not run when claude is unavailable")

    monkeypatch.setattr("skill_optimizer.cli.evaluate", _boom)
    args = build_parser().parse_args(
        [
            "--skill-path",
            str(skill),
            "--eval-set",
            str(eval_file),
            "--report",
            "none",
        ]
    )
    with pytest.raises(SystemExit) as excinfo:
        run(args)
    assert "claude CLI not found" in str(excinfo.value)
    assert capsys.readouterr().out == ""  # no envelope; artifact-free


def test_defaults_align_with_skill_creator() -> None:
    # Familiar behavior when only the documented flags are passed: match run_loop's
    # workers/holdout/max-iterations defaults. --timeout stays at 90 (deliberately above
    # official's 30: with the retry/tri-state it avoids false no-triggers).
    args = build_parser().parse_args(["--skill-path", "s", "--eval-set", "e"])
    assert args.iterations == 5
    assert args.test_frac == 0.4
    assert args.workers == 10
    assert args.timeout == 90
    assert args.repeats == 3
    assert args.threshold == 0.5
    assert args.seed == 42
    assert args.dry_run is False


def test_dry_run_emits_plan_json_and_spends_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # --dry-run validates config and prints a machine-readable plan to stdout with a
    # budgetable call estimate, without evaluating or writing any artifacts.
    skill, eval_file = _setup(tmp_path)

    def _boom(*_: Any, **__: Any) -> Any:
        raise AssertionError("dry-run must not evaluate")

    monkeypatch.setattr("skill_optimizer.cli.evaluate", _boom)
    monkeypatch.setattr("skill_optimizer.cli.call_improver", _no_improver)
    args = build_parser().parse_args(
        [
            "--skill-path",
            str(skill),
            "--eval-set",
            str(eval_file),
            "--model",
            "sonnet",
            "--repeats",
            "2",
            "--iterations",
            "3",
            "--holdout",
            "0.4",
            "--seed",
            "7",
            "--dry-run",
        ]
    )
    run(args)
    out = json.loads(capsys.readouterr().out)
    assert out["dry_run"] is True
    assert "best_description" not in out  # not a run envelope
    assert out["queries"] == 4
    assert out["train_size"] == 2
    assert out["test_size"] == 2
    assert out["seed"] == 7
    assert out["repeats"] == 2
    assert out["iterations"] == 3
    # 4 queries x 1 model x 2 repeats x (3 iterations + 1 baseline) = 32 eval calls.
    assert out["estimated_eval_calls"] == 32
    assert out["estimated_improver_calls"] == 3
    assert out["estimated_claude_calls"] == 35


def test_report_echoes_seed_and_estimate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A real run records the seed and the (upper-bound) estimate in the output JSON, so
    # the run is reproducible and its cost is auditable from its own record. The estimate
    # is computed from the requested iteration budget, not the early-exit actual.
    skill, eval_file = _setup(tmp_path)
    monkeypatch.setattr("skill_optimizer.cli.evaluate", _stub_eval_all_pass)
    monkeypatch.setattr("skill_optimizer.cli.call_improver", _no_improver)
    args = build_parser().parse_args(
        [
            "--skill-path",
            str(skill),
            "--eval-set",
            str(eval_file),
            "--model",
            "sonnet",
            "--repeats",
            "2",
            "--iterations",
            "4",
            "--holdout",
            "0.4",
            "--seed",
            "13",
            "--report",
            "none",
            "--out",
            str(tmp_path / "out"),
        ]
    )
    run(args)
    out = json.loads(capsys.readouterr().out)
    assert out["seed"] == 13
    # Upper bound: 4 x 1 x 2 x (4 + 1) eval calls + 4 improver calls, despite early exit.
    assert out["estimated_claude_calls"] == 44


def test_seed_is_threaded_into_split(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The CLI must pass --seed through to the stratified split (not just echo it).
    skill, eval_file = _setup(tmp_path)
    captured: dict[str, int] = {}
    real_split = cli_module.stratified_split

    def spy(qs: Any, frac: float, seed: int = 42) -> Any:
        captured["seed"] = seed
        return real_split(qs, frac, seed=seed)

    monkeypatch.setattr("skill_optimizer.cli.stratified_split", spy)
    monkeypatch.setattr("skill_optimizer.cli.evaluate", _stub_eval_all_pass)
    monkeypatch.setattr("skill_optimizer.cli.call_improver", _no_improver)
    args = build_parser().parse_args(
        [
            "--skill-path",
            str(skill),
            "--eval-set",
            str(eval_file),
            "--model",
            "sonnet",
            "--seed",
            "99",
            "--report",
            "none",
            "--out",
            str(tmp_path / "out"),
        ]
    )
    run(args)
    assert captured["seed"] == 99


@pytest.mark.parametrize(
    "fatal",
    [
        FileNotFoundError("claude"),
        PermissionError("denied"),
        ValueError("arbitrary"),
        RuntimeError("boom"),
        ImproverFatalProcessError("Improver process exited with status 2"),
    ],
)
def test_retryable_then_fatal_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fatal: Exception
) -> None:
    skill, eval_file = _setup(tmp_path)
    fake, state = _scripted_call_improver(
        [("retry", "timeout", "Improver timed out"), ("fatal", fatal)]
    )
    monkeypatch.setattr("skill_optimizer.cli.evaluate", _fake_eval_all_fail)
    monkeypatch.setattr("skill_optimizer.cli.call_improver", fake)
    args = build_parser().parse_args(
        [
            "--skill-path",
            str(skill),
            "--eval-set",
            str(eval_file),
            "--out",
            str(tmp_path / "out"),
            "--iterations",
            "2",
            "--report",
            "none",
        ]
    )
    with pytest.raises(type(fatal)):
        run(args)
    assert state["n"] == 2  # exactly two calls: no third, no continuation


def test_three_slot_success_exhaustion_success_preserves_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    out = _run_and_capture(
        tmp_path,
        monkeypatch,
        capsys,
        [
            ("ok", "cand1"),
            ("retry", "timeout", "Improver timed out"),
            ("retry", "timeout", "Improver timed out"),
            ("ok", "cand3"),
        ],
        3,
    )
    # The exhausted slot 2 added no history row: baseline + cand1 + cand3 only.
    descriptions = [h["description"] for h in out["history"]]
    assert descriptions == ["original desc", "cand1", "cand3"]
    assert out["final_description"] == "cand3"
    assert out["iterations_run"] == 3
    assert [r["iteration"] for r in out["improver_failed_iterations"]] == [2]


def _run_and_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    outcomes: list[tuple[Any, ...]],
    iterations: int,
) -> dict[str, Any]:
    _run_with_scripted_improver(tmp_path, monkeypatch, outcomes, iterations)
    return json.loads(capsys.readouterr().out)


def test_zero_iterations_enters_no_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    state = _run_with_scripted_improver(tmp_path, monkeypatch, [], 0)
    out = json.loads(capsys.readouterr().out)
    assert state["n"] == 0  # improver never called
    assert out["iterations_run"] == 0
    assert out["improver_failed_iterations"] == []


def test_live_snapshot_after_exhaustion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    skill, eval_file = _setup(tmp_path)
    fake, _state = _scripted_call_improver(
        [
            ("ok", "cand1"),
            ("retry", "timeout", "Improver timed out"),
            ("retry", "timeout", "Improver timed out"),
        ]
    )
    monkeypatch.setattr("skill_optimizer.cli.evaluate", _fake_eval_all_fail)
    monkeypatch.setattr("skill_optimizer.cli.call_improver", fake)
    snapshots: list[tuple[dict[str, Any], bool]] = []

    def spy_write_html(
        path: Path, report: dict[str, Any], name: str, refresh: bool
    ) -> None:
        snapshots.append((report, refresh))

    monkeypatch.setattr("skill_optimizer.cli._write_html", spy_write_html)
    args = build_parser().parse_args(
        [
            "--skill-path",
            str(skill),
            "--eval-set",
            str(eval_file),
            "--out",
            str(tmp_path / "out"),
            "--iterations",
            "2",
            "--report",
            str(tmp_path / "r.html"),
        ]
    )
    run(args)
    live = [data for data, refresh in snapshots if refresh]
    assert len(live) == 2  # after slot 1 success, after slot 2 exhaustion
    after_success, after_exhaustion = live
    assert after_success["iterations_run"] == 1
    assert after_success["improver_failed_iterations"] == []
    assert len(after_success["history"]) == 2  # baseline + cand1
    # Exhaustion: history UNCHANGED, iterations_run incremented, one new record.
    assert len(after_exhaustion["history"]) == 2
    assert after_exhaustion["iterations_run"] == 2
    assert len(after_exhaustion["improver_failed_iterations"]) == 1


def test_direct_slot_51_rejected_before_work(tmp_path: Path) -> None:
    inputs = _LoopInputs(
        name="s",
        body="b",
        config=EvalConfig(
            models=("m",), repeats=1, timeout=1, workers=1, threshold=0.5
        ),
        improver_model="m",
        effort=None,
        iterations=51,
        timeout=1,
        select_epsilon=0.05,
        out=tmp_path,
    )
    queries: list[EvalQuery] = [
        {"query": "a", "should_trigger": True},
        {"query": "b", "should_trigger": True},
        {"query": "c", "should_trigger": False},
        {"query": "d", "should_trigger": False},
    ]
    base_full = _eval_result(queries, ("m",), False)
    with pytest.raises(ValueError, match="--iterations must be between 0 and 50"):
        _optimize(inputs, [0, 2], [1, 3], queries, "desc", base_full)


def test_50_slot_worst_case_hits_200_children_and_no_more(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    children = {"n": 0}

    def fake_run(*_: Any, **__: Any) -> Any:
        from types import SimpleNamespace

        children["n"] += 1
        # Always over the tiny budget, both initial and shorten -> length_limit retry.
        return SimpleNamespace(
            stdout='{"description": "xxxxxxxx"}', returncode=0, stderr=""
        )

    monkeypatch.setattr("skill_optimizer.improver.subprocess.run", fake_run)
    monkeypatch.setattr("skill_optimizer.cli.evaluate", _fake_eval_all_fail)
    skill, eval_file = _setup(tmp_path)
    args = build_parser().parse_args(
        [
            "--skill-path",
            str(skill),
            "--eval-set",
            str(eval_file),
            "--out",
            str(tmp_path / "out"),
            "--iterations",
            "50",
            "--max-desc-chars",
            "5",
            "--report",
            "none",
        ]
    )
    run(args)
    out = json.loads(capsys.readouterr().out)
    # 50 slots * 2 outer attempts * 2 children (initial + shorten) == 200, and no more.
    assert children["n"] == 200
    assert out["iterations_run"] == 50
    assert len(out["improver_failed_iterations"]) == 50
    assert all(
        r["errors"][0]["kind"] == "length_limit"
        for r in out["improver_failed_iterations"]
    )


_SENTINEL = "SENTINELLEAKXYZ"


def test_adversarial_sentinels_absent_from_public_surfaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fake_run(*_: Any, **__: Any) -> Any:
        from types import SimpleNamespace

        # Raw stdout/stderr carry the sentinel; the response is unparseable so the
        # slot exhausts (invalid_output x2). Diagnostic material stays in transcripts.
        return SimpleNamespace(
            stdout=f"prose {_SENTINEL} not json",
            returncode=0,
            stderr=f"stderr {_SENTINEL}",
        )

    monkeypatch.setattr("skill_optimizer.improver.subprocess.run", fake_run)
    monkeypatch.setattr("skill_optimizer.cli.evaluate", _fake_eval_all_fail)
    skill, eval_file = _setup(tmp_path)
    rdir = tmp_path / "results"
    args = build_parser().parse_args(
        [
            "--skill-path",
            str(skill),
            "--eval-set",
            str(eval_file),
            "--results-dir",
            str(rdir),
            "--iterations",
            "1",
            "--report",
            "none",
        ]
    )
    run(args)
    captured = capsys.readouterr()
    assert _SENTINEL not in captured.out  # stdout report JSON
    out = json.loads(captured.out)
    # The public failure ledger uses only fixed templates, never raw diagnostics.
    assert out["improver_failed_iterations"][0]["errors"][0]["message"] == (
        "Improver returned no JSON"
    )
    run_dir = next(p for p in rdir.iterdir() if p.is_dir())
    assert _SENTINEL not in (run_dir / "results.json").read_text()
    assert _SENTINEL not in (run_dir / "logs" / "report.json").read_text()
    # It IS retained privately in the per-attempt transcript.
    assert _SENTINEL in (run_dir / "logs" / "iter1_improve.json").read_text()


def test_verbose_help_names_detailed_summaries() -> None:
    help_text = build_parser().format_help()
    assert "per-model" in help_text
    assert "confusion-matrix" in help_text
    assert "per-query" in help_text


# --------------------------------------------------------------------------- #
# run() / main() branch coverage: preflight exit, description override, verbose
# summaries, --write outcomes, and the report-writer OSError swallows.
# --------------------------------------------------------------------------- #
def test_run_missing_skill_md_exits(tmp_path: Path) -> None:
    args = build_parser().parse_args(
        ["--skill-path", str(tmp_path / "nope"), "--eval-set", str(tmp_path / "e.json")]
    )
    with pytest.raises(SystemExit, match="No SKILL.md at"):
        run(args)


def test_description_override_replaces_frontmatter_desc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # --description overrides the SKILL.md frontmatter description; with an all-pass
    # early exit the winning (and only) description is the CLI-provided one.
    skill, eval_file = _setup(tmp_path)
    monkeypatch.setattr("skill_optimizer.cli.evaluate", _stub_eval_all_pass)
    monkeypatch.setattr("skill_optimizer.cli.call_improver", _no_improver)
    args = build_parser().parse_args(
        [
            "--skill-path",
            str(skill),
            "--eval-set",
            str(eval_file),
            "--out",
            str(tmp_path / "out"),
            "--description",
            "cli-provided desc",
        ]
    )
    run(args)
    out = json.loads(capsys.readouterr().out)
    assert out["best_description"] == "cli-provided desc"


def test_verbose_emits_detailed_summaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # --verbose selects summarize_verbose for both the baseline and each iteration.
    skill, eval_file = _setup(tmp_path)

    def fake_eval(
        eval_set: list[EvalQuery],
        name: Any,
        description: str,
        config: Any,
        *,
        verbose: bool = True,
    ) -> EvalResult:
        return _eval_result(eval_set, config.models, description == "improved desc")

    def fake_improver(*_: Any, **__: Any) -> dict[str, Any]:
        return {"description": "improved desc", "rationale": "better"}

    monkeypatch.setattr("skill_optimizer.cli.evaluate", fake_eval)
    monkeypatch.setattr("skill_optimizer.cli.call_improver", fake_improver)
    args = build_parser().parse_args(
        [
            "--skill-path",
            str(skill),
            "--eval-set",
            str(eval_file),
            "--out",
            str(tmp_path / "out"),
            "--iterations",
            "1",
            "--verbose",
        ]
    )
    run(args)
    assert json.loads(capsys.readouterr().out)["best_description"] == "improved desc"


def test_write_persists_improved_description(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # --write with a winning candidate rewrites SKILL.md and backs up the original.
    skill, eval_file = _setup(tmp_path)

    def fake_eval(
        eval_set: list[EvalQuery],
        name: Any,
        description: str,
        config: Any,
        *,
        verbose: bool = True,
    ) -> EvalResult:
        return _eval_result(eval_set, config.models, description == "improved desc")

    def fake_improver(*_: Any, **__: Any) -> dict[str, Any]:
        return {"description": "improved desc", "rationale": "r"}

    monkeypatch.setattr("skill_optimizer.cli.evaluate", fake_eval)
    monkeypatch.setattr("skill_optimizer.cli.call_improver", fake_improver)
    args = build_parser().parse_args(
        [
            "--skill-path",
            str(skill),
            "--eval-set",
            str(eval_file),
            "--out",
            str(tmp_path / "out"),
            "--iterations",
            "1",
            "--write",
        ]
    )
    run(args)
    _ = capsys.readouterr()
    assert "improved desc" in (skill / "SKILL.md").read_text()
    assert (skill / "SKILL.md.bak").exists()


def test_write_reports_no_change_when_best_is_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # --write with an all-pass baseline (best == baseline) writes nothing and logs the
    # explicit no-change path.
    skill, eval_file = _setup(tmp_path)
    monkeypatch.setattr("skill_optimizer.cli.evaluate", _stub_eval_all_pass)
    monkeypatch.setattr("skill_optimizer.cli.call_improver", _no_improver)
    original = (skill / "SKILL.md").read_text()
    args = build_parser().parse_args(
        [
            "--skill-path",
            str(skill),
            "--eval-set",
            str(eval_file),
            "--out",
            str(tmp_path / "out"),
            "--write",
        ]
    )
    with caplog.at_level(logging.INFO, logger="skill_optimizer.cli"):
        run(args)
    assert (skill / "SKILL.md").read_text() == original
    assert not (skill / "SKILL.md.bak").exists()
    assert any("No change to write" in r.getMessage() for r in caplog.records)


def test_main_returns_zero_on_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    skill, eval_file = _setup(tmp_path)
    monkeypatch.setattr("skill_optimizer.cli.evaluate", _stub_eval_all_pass)
    monkeypatch.setattr("skill_optimizer.cli.call_improver", _no_improver)
    rc = main(
        [
            "--skill-path",
            str(skill),
            "--eval-set",
            str(eval_file),
            "--out",
            str(tmp_path / "out"),
        ]
    )
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["best_description"] == "original desc"


def test_write_placeholder_and_open_swallows_oserror(tmp_path: Path) -> None:
    # The report parent cannot be created (it sits under a regular file), so mkdir raises
    # OSError, which is swallowed -- a headless/unwritable environment never aborts.
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    target = blocker / "sub" / "report.html"
    cli_module._write_placeholder_and_open(target)  # pyright: ignore[reportPrivateUsage]
    assert not target.exists()


def test_write_placeholder_opens_absolute_file_uri(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "report directory" / "report.html"
    opened: list[str] = []

    def capture_open(url: str) -> bool:
        opened.append(url)
        return True

    monkeypatch.setattr(
        "skill_optimizer.cli.webbrowser.open",
        capture_open,
        raising=False,
    )

    cli_module._write_placeholder_and_open(target)  # pyright: ignore[reportPrivateUsage]

    assert target.exists()
    assert opened == [target.resolve().as_uri()]


def test_write_html_swallows_oserror(tmp_path: Path) -> None:
    # Same unwritable-parent condition for the HTML report writer: OSError is swallowed.
    blocker = tmp_path / "blocker2"
    blocker.write_text("x")
    target = blocker / "sub" / "report.html"
    cli_module._write_html(  # pyright: ignore[reportPrivateUsage]
        target, {"history": []}, "skill", False
    )
    assert not target.exists()
