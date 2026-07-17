#!/usr/bin/env python3
"""Entry point / compatibility shim for the ``skill_optimizer`` package.

The implementation lives in the :mod:`skill_optimizer` package under ``src/``; this
module re-exports the public API so existing usage — ``python optimize_description_v2.py
--skill-path ...`` and ``import optimize_description_v2`` — keeps working unchanged,
including direct execution from a clean checkout with no install.
"""

import os
import sys

# Minimum supported runtime as a (major, minor) tuple. Held as a module-level constant
# rather than an inline ``(3, 14)`` literal ON PURPOSE: a version-upgrade linter (ruff's
# ``UP036`` with an inferred 3.14 target, formerly the ``pyupgrade --py314-plus`` hook)
# treats ``if sys.version_info < (3, 14):`` as statically dead and deletes it, which is
# exactly what silently removed this guard before. Comparing against a name the linter
# cannot resolve statically keeps the block a genuine runtime guard that survives the check.
_MINIMUM_PYTHON = (3, 14)

# Runtime-floor guard: enforce Python >=3.14 BEFORE importing the package, so a wrong
# interpreter fails as one legible stderr line (empty stdout, no traceback) instead of a
# raw import-time error from PEP 604 unions in the package's annotations. Must not import
# ``skill_optimizer`` on the failing path.
if sys.version_info < _MINIMUM_PYTHON:
    sys.stderr.write(
        "Requires Python >=3.14; use uv run --project "
        "/ABSOLUTE/PATH/TO/agent-skill-description-optimizer "
        "optimize-skill-description.\n"
    )
    raise SystemExit(1)

# src/ layout bootstrap: make ``skill_optimizer`` importable when this file is run
# directly from a checkout with no install. Harmless when the package is already
# installed (e.g. editable via ``uv sync``) — the first resolvable entry wins.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from skill_optimizer import (  # noqa: E402 - must follow the runtime-floor guard
    IMPROVER_TEMPLATE,
    MODEL_ALIASES,
    EvalConfig,
    EvalQuery,
    EvalResult,
    ModelResult,
    PerQuery,
    aggregate,
    build_improver_prompt,
    build_parser,
    call_improver,
    evaluate,
    generate_html,
    interpret_events,
    is_better_candidate,
    main,
    parse_skill_md,
    resolve_models,
    run,
    run_query_with_retry,
    run_single_query,
    stratified_split,
    subset_result,
    summarize,
    write_description,
)

__all__ = [
    "IMPROVER_TEMPLATE",
    "MODEL_ALIASES",
    "EvalConfig",
    "EvalQuery",
    "EvalResult",
    "ModelResult",
    "PerQuery",
    "aggregate",
    "build_improver_prompt",
    "build_parser",
    "call_improver",
    "evaluate",
    "generate_html",
    "interpret_events",
    "is_better_candidate",
    "main",
    "parse_skill_md",
    "resolve_models",
    "run",
    "run_query_with_retry",
    "run_single_query",
    "stratified_split",
    "subset_result",
    "summarize",
    "write_description",
]


if __name__ == "__main__":
    raise SystemExit(main())
