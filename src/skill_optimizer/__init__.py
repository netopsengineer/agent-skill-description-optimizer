"""Project-agnostic skill *description* optimizer — no ANTHROPIC_API_KEY required.

The skill-creator's own ``run_eval.py`` already evaluates triggering with ``claude
-p`` (no key). The only piece that needs an API key is the improver, which calls the
``anthropic`` SDK. This package replaces that piece: both evaluation and improvement
go through ``claude -p``, so the whole loop runs with the same auth the CLI uses.

For ANY single skill it: reads the current ``description`` from ``SKILL.md``;
evaluates how reliably it triggers across one or more models via ``claude -p``; asks
an improver model to rewrite it from the failures; and selects the best description
by held-out score, optionally writing it back with ``--write``.
"""

from importlib import metadata

from skill_optimizer.cli import build_parser, main, run
from skill_optimizer.evaluation import (
    aggregate,
    evaluate,
    run_query_with_retry,
    run_single_query,
    subset_result,
)
from skill_optimizer.improver import (
    IMPROVER_TEMPLATE,
    build_improver_prompt,
    call_improver,
)
from skill_optimizer.interpreter import interpret_events
from skill_optimizer.models import (
    MODEL_ALIASES,
    EvalConfig,
    EvalQuery,
    EvalResult,
    ModelResult,
    PerQuery,
)
from skill_optimizer.report import generate_html
from skill_optimizer.selection import (
    is_better_candidate,
    resolve_models,
    stratified_split,
    summarize,
)
from skill_optimizer.skill_md import parse_skill_md, write_description

try:
    __version__ = metadata.version("agent-skill-description-optimizer")
except metadata.PackageNotFoundError:  # source checkout without an install
    __version__ = "0.0.0+unknown"

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
