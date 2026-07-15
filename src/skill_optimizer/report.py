"""HTML report generation, ported from skill-creator's ``generate_report.py``.

Adapted to this tool's multi-model, tri-state history: query cells are anchored by the
original positional ``index`` (dedup-safe), render ``pass`` as a tri-state
(green/red/neutral, never a red cross for an unjudged probe), carry per-model detail in
the cell ``title`` attribute, and highlight the winning row from the ``is_best`` flag
(never recomputed from a score).
"""

import html
from typing import Any

_CSS = """
        body {
            font-family: 'Lora', Georgia, serif;
            max-width: 100%;
            margin: 0 auto;
            padding: 20px;
            background: #faf9f5;
            color: #141413;
        }
        h1 { font-family: 'Poppins', sans-serif; color: #141413; }
        .explainer, .summary {
            background: white;
            padding: 15px;
            border-radius: 6px;
            margin-bottom: 20px;
            border: 1px solid #e8e6dc;
        }
        .explainer { color: #b0aea5; font-size: 0.875rem; line-height: 1.6; }
        .summary p { margin: 5px 0; }
        .best { color: #788c5d; font-weight: bold; }
        .table-container { overflow-x: auto; width: 100%; }
        table {
            border-collapse: collapse;
            background: white;
            border: 1px solid #e8e6dc;
            border-radius: 6px;
            font-size: 12px;
            min-width: 100%;
        }
        th, td {
            padding: 8px;
            text-align: left;
            border: 1px solid #e8e6dc;
            white-space: normal;
            word-wrap: break-word;
        }
        th {
            font-family: 'Poppins', sans-serif;
            background: #141413;
            color: #faf9f5;
            font-weight: 500;
        }
        th.test-col { background: #6a9bcc; }
        th.query-col { min-width: 200px; }
        td.description {
            font-family: monospace;
            font-size: 11px;
            word-wrap: break-word;
            max-width: 400px;
        }
        td.result { text-align: center; font-size: 16px; min-width: 40px; }
        td.test-result { background: #f0f6fc; }
        .pass { color: #788c5d; }
        .fail { color: #c44; }
        .unjudged { color: #b0aea5; }
        .rate { font-size: 9px; color: #b0aea5; display: block; }
        tr:hover { background: #faf9f5; }
        .score {
            display: inline-block;
            padding: 2px 6px;
            border-radius: 4px;
            font-weight: bold;
            font-size: 11px;
        }
        .score-good { background: #eef2e8; color: #788c5d; }
        .score-ok { background: #fef3c7; color: #d97706; }
        .score-bad { background: #fceaea; color: #c44; }
        .best-row { background: #f5f8f2; }
        th.positive-col { border-bottom: 3px solid #788c5d; }
        th.negative-col { border-bottom: 3px solid #c44; }
        .legend {
            font-family: 'Poppins', sans-serif;
            display: flex;
            gap: 20px;
            margin-bottom: 10px;
            font-size: 13px;
            align-items: center;
        }
        .legend-item { display: flex; align-items: center; gap: 6px; }
        .legend-swatch { width: 16px; height: 16px; border-radius: 3px; display: inline-block; }
        .swatch-positive { background: #141413; border-bottom: 3px solid #788c5d; }
        .swatch-negative { background: #141413; border-bottom: 3px solid #c44; }
        .swatch-test { background: #6a9bcc; }
        .swatch-train { background: #141413; }
"""


def _column_headers(results: list[dict[str, Any]], test: bool) -> str:
    """Render ``<th>`` column headers for a set of query results.

    Args:
        results: The ordered result entries (from ``history[0]``) to build columns for.
        test: Whether these are held-out (test) columns (adds the test styling).

    Returns:
        The concatenated header cells.
    """
    parts: list[str] = []
    for r in results:
        polarity = "positive-col" if r.get("should_trigger", True) else "negative-col"
        cls = f"test-col {polarity}" if test else polarity
        parts.append(
            f'                <th class="{cls}">{html.escape(r["query"])}</th>\n'
        )
    return "".join(parts)


def _aggregate_runs(results: list[dict[str, Any]]) -> tuple[int, int]:
    """Sum correct runs and total runs across all query results.

    Args:
        results: The result entries to aggregate.

    Returns:
        A ``(correct, total)`` tuple of run counts.
    """
    correct = 0
    total = 0
    for r in results:
        runs = r.get("runs", 0)
        triggers = r.get("triggers", 0)
        total += runs
        correct += triggers if r.get("should_trigger", True) else runs - triggers
    return correct, total


def _score_class(correct: int, total: int) -> str:
    """Map a correct/total ratio to a CSS score class.

    Args:
        correct: Correct run count.
        total: Total run count.

    Returns:
        The CSS class name.
    """
    if total > 0:
        ratio = correct / total
        if ratio >= 0.8:
            return "score-good"
        if ratio >= 0.5:
            return "score-ok"
    return "score-bad"


def _result_cell(r: dict[str, Any], test: bool) -> str:
    """Render one query result cell, tri-state and per-model annotated.

    Args:
        r: The result entry (may be empty if the column has no matching index).
        test: Whether this is a held-out (test) cell.

    Returns:
        The ``<td>`` cell HTML.
    """
    did_pass = r.get("pass")
    triggers = r.get("triggers", 0)
    runs = r.get("runs", 0)
    if did_pass is True:
        icon, css = "✓", "pass"
    elif did_pass is False:
        icon, css = "✗", "fail"
    else:
        # Unjudged (all probes errored): a neutral marker, never a red cross.
        icon, css = "–", "unjudged"
    per_model = r.get("models", {})
    title = ", ".join(
        f"{m}: {d.get('triggers', 0)}/{d.get('runs', 0)}" for m, d in per_model.items()
    )
    cls = f"result test-result {css}" if test else f"result {css}"
    return (
        f'                <td class="{cls}" title="{html.escape(title)}">{icon}'
        f'<span class="rate">{triggers}/{runs}</span></td>\n'
    )


def _row(
    h: dict[str, Any],
    train_cols: list[dict[str, Any]],
    test_cols: list[dict[str, Any]],
) -> str:
    """Render one iteration row.

    Args:
        h: The history entry for this iteration.
        train_cols: The ordered training column definitions (from ``history[0]``).
        test_cols: The ordered held-out column definitions (from ``history[0]``).

    Returns:
        The ``<tr>`` row HTML.
    """
    train_results = h.get("train_results", h.get("results", []))
    test_results = h.get("test_results", [])
    # Anchor cells by original positional index, never query text (dedup-safe).
    train_by_index = {r["index"]: r for r in train_results}
    test_by_index = {r["index"]: r for r in test_results}
    train_correct, train_runs = _aggregate_runs(train_results)
    test_correct, test_runs = _aggregate_runs(test_results)
    row_class = "best-row" if h.get("is_best") else ""
    cells = [
        f'            <tr class="{row_class}">\n',
        f"                <td>{h.get('iteration', '?')}</td>\n",
        f'                <td><span class="score {_score_class(train_correct, train_runs)}">'
        f"{train_correct}/{train_runs}</span></td>\n",
        f'                <td><span class="score {_score_class(test_correct, test_runs)}">'
        f"{test_correct}/{test_runs}</span></td>\n",
        f'                <td class="description">{html.escape(h.get("description", ""))}</td>\n',
    ]
    cells.extend(
        _result_cell(train_by_index.get(c["index"], {}), False) for c in train_cols
    )
    cells.extend(
        _result_cell(test_by_index.get(c["index"], {}), True) for c in test_cols
    )
    cells.append("            </tr>\n")
    return "".join(cells)


def generate_html(
    data: dict[str, Any], auto_refresh: bool = False, skill_name: str = ""
) -> str:
    """Generate the HTML optimization report from loop output data.

    Args:
        data: The report dict (the same superset written to stdout / ``results.json``).
        auto_refresh: When ``True``, add a 5-second meta-refresh (for the live report).
        skill_name: Skill name for the report title.

    Returns:
        The rendered HTML document.
    """
    history: list[dict[str, Any]] = data.get("history", [])
    title_prefix = html.escape(f"{skill_name} — ") if skill_name else ""
    first = history[0] if history else {}
    train_cols: list[dict[str, Any]] = first.get(
        "train_results", first.get("results", [])
    )
    test_cols: list[dict[str, Any]] = first.get("test_results", [])
    refresh_tag = (
        '    <meta http-equiv="refresh" content="5">\n' if auto_refresh else ""
    )
    best_test_score = data.get("best_test_score")
    parts: list[str] = [
        '<!DOCTYPE html>\n<html>\n<head>\n    <meta charset="utf-8">\n',
        refresh_tag,
        f"    <title>{title_prefix}Skill Description Optimization</title>\n",
        f"    <style>{_CSS}    </style>\n</head>\n<body>\n",
        f"    <h1>{title_prefix}Skill Description Optimization</h1>\n",
        '    <div class="explainer"><strong>Optimizing your skill’s '
        "description.</strong> Each row is a description attempt. Query columns show "
        "test cases: a green check means the skill triggered correctly (or correctly "
        "stayed silent), a red cross means it got it wrong, a grey dash means the probe "
        "was unjudgeable. The best-performing row is highlighted.</div>\n",
        '    <div class="summary">\n'
        f"        <p><strong>Original:</strong> {html.escape(str(data.get('original_description', 'N/A')))}</p>\n"
        f'        <p class="best"><strong>Best:</strong> {html.escape(str(data.get("best_description", "N/A")))}</p>\n'
        f"        <p><strong>Best Score:</strong> {data.get('best_score', 'N/A')} "
        f"{'(test)' if best_test_score else '(train)'}</p>\n"
        f"        <p><strong>Iterations:</strong> {data.get('iterations_run', 0)} | "
        f"<strong>Train:</strong> {data.get('train_size', '?')} | "
        f"<strong>Test:</strong> {data.get('test_size', '?')}</p>\n"
        "    </div>\n",
        '    <div class="legend">\n'
        '        <span style="font-weight:600">Query columns:</span>\n'
        '        <span class="legend-item"><span class="legend-swatch swatch-positive">'
        "</span> Should trigger</span>\n"
        '        <span class="legend-item"><span class="legend-swatch swatch-negative">'
        "</span> Should NOT trigger</span>\n"
        '        <span class="legend-item"><span class="legend-swatch swatch-train">'
        "</span> Train</span>\n"
        '        <span class="legend-item"><span class="legend-swatch swatch-test">'
        "</span> Test</span>\n"
        "    </div>\n",
        '    <div class="table-container">\n    <table>\n        <thead>\n'
        "            <tr>\n"
        "                <th>Iter</th>\n                <th>Train</th>\n"
        '                <th>Test</th>\n                <th class="query-col">'
        "Description</th>\n",
        _column_headers(train_cols, False),
        _column_headers(test_cols, True),
        "            </tr>\n        </thead>\n        <tbody>\n",
    ]
    parts.extend(_row(h, train_cols, test_cols) for h in history)
    parts.append("        </tbody>\n    </table>\n    </div>\n</body>\n</html>\n")
    return "".join(parts)
