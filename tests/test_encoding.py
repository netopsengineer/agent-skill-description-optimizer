"""Regression tests for the text-encoding contract (PY-CORR-013).

The tool reads/writes SKILL.md, eval sets, per-iteration artifacts, JSON envelopes,
and the HTML report. Every one of those text I/O sites must pin ``encoding="utf-8"``
rather than inheriting the host locale — otherwise the HTML report (which contains
``✓``/``✗``/``–``/``—``/``’``) fails to write on a non-UTF-8 locale (Windows cp1252, a
POSIX ``C``/ascii locale) with a ``UnicodeEncodeError`` that is *not* an ``OSError`` and
so escapes the report writer's guard, and a UTF-8 ``SKILL.md`` is silently mis-decoded
and re-written under ``--write``.

Guarding strategy:

- :class:`TestEncodingContract` is the host-independent guard: it spies on every
  ``Path.read_text``/``Path.write_text`` the loop performs and asserts each tool file is
  opened as UTF-8. It fails the moment any site regresses to the locale default, even on
  a UTF-8 CI host where the functional round-trips below would still pass by luck.
- :class:`TestNonAsciiRoundTrip` / :class:`TestEvalSetEncoding` assert behavior and the
  new invalid-UTF-8 precondition path.
- :class:`TestAsciiLocaleSubprocess` is the definitive end-to-end proof: it runs the real
  code under a genuine ascii locale where the pre-fix code raised ``UnicodeEncodeError``.
"""

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

import skill_optimizer.cli as cli_module
from skill_optimizer import aggregate, generate_html, parse_skill_md, write_description
from skill_optimizer.cli import (
    _load_eval_set,  # pyright: ignore[reportPrivateUsage]
    build_parser,
    run,
)
from skill_optimizer.models import EvalQuery, EvalResult

_BASELINE_DESC = "original desc"
# A description exercising the non-ASCII bytes that break a non-UTF-8 encoder: an
# em-dash (U+2014), an en-dash (U+2013), a curly quote (U+2019), and accented Latin.
_NON_ASCII = "Résumé parser — café menus – handles piñata's touché"


def _agg(
    eval_set: list[EvalQuery], models: tuple[str, ...], description: str, *, win: bool
) -> EvalResult:
    """Build a real :class:`EvalResult` via :func:`aggregate` (no hand-rolled dict).

    Args:
        eval_set: The queries being scored.
        models: The evaluated model ids.
        description: The description these results belong to.
        win: When ``True`` every query is answered correctly; when ``False`` every
            ``should_trigger`` query is missed (so the baseline fails and the loop runs
            an improver iteration).

    Returns:
        The aggregated result.
    """
    raw: dict[tuple[int, str], list[bool]] = {}
    for i, q in enumerate(eval_set):
        triggered = q["should_trigger"] if win else False
        for m in models:
            raw[(i, m)] = [triggered, triggered, triggered]
    return aggregate(raw, eval_set, models, 0.5, description)


def _setup_skill(tmp_path: Path) -> tuple[Path, Path]:
    """Write a minimal skill dir and a 4-query eval set (2 per class).

    Args:
        tmp_path: Per-test temporary directory.

    Returns:
        ``(skill_dir, eval_file)``.
    """
    skill = tmp_path / "myskill"
    skill.mkdir()
    (skill / "SKILL.md").write_text(
        f"---\nname: myskill\ndescription: {_BASELINE_DESC}\n---\nBody\n",
        encoding="utf-8",
    )
    eval_file = tmp_path / "evals.json"
    eval_file.write_text(
        json.dumps(
            [
                {"query": "alpha", "should_trigger": True},
                {"query": "gamma", "should_trigger": True},
                {"query": "beta", "should_trigger": False},
                {"query": "delta", "should_trigger": False},
            ]
        ),
        encoding="utf-8",
    )
    return skill, eval_file


class TestEncodingContract:
    """Every tool-file text I/O the loop performs must pin ``encoding="utf-8"``."""

    def test_all_loop_file_io_specifies_utf8(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        skill, eval_file = _setup_skill(tmp_path)
        calls: list[tuple[str, str, Any]] = []
        real_read = Path.read_text
        real_write = Path.write_text

        def spy_read(self: Path, *args: Any, **kwargs: Any) -> str:
            calls.append(("read", str(self), kwargs.get("encoding")))
            return real_read(self, *args, **kwargs)

        def spy_write(self: Path, data: str, *args: Any, **kwargs: Any) -> int:
            calls.append(("write", str(self), kwargs.get("encoding")))
            return real_write(self, data, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", spy_read)
        monkeypatch.setattr(Path, "write_text", spy_write)

        def fake_eval(
            eval_set: list[EvalQuery],
            name: Any,
            description: str,
            config: Any,
            *,
            verbose: bool = True,
        ) -> EvalResult:
            # Baseline (unchanged description) fails so an iteration runs; the improver's
            # candidate wins so --write persists it.
            return _agg(
                eval_set, config.models, description, win=description != _BASELINE_DESC
            )

        def fake_improver(*_: Any, **__: Any) -> dict[str, Any]:
            return {"description": _NON_ASCII, "rationale": "r"}

        monkeypatch.setattr(cli_module, "evaluate", fake_eval)
        monkeypatch.setattr(cli_module, "call_improver", fake_improver)

        results_dir = tmp_path / "results"
        args = build_parser().parse_args(
            [
                "--skill-path",
                str(skill),
                "--eval-set",
                str(eval_file),
                "--results-dir",
                str(results_dir),
                "--iterations",
                "1",
                "--report",
                "auto",
                "--write",
            ]
        )
        run(args)

        # Only assert on files this tool owns (under tmp_path); ignore any stdlib/dep
        # Path I/O. Every one of them must have opened as UTF-8, never the host locale.
        tool_io = [(op, p, enc) for op, p, enc in calls if str(tmp_path) in p]
        offenders = [(op, p, enc) for op, p, enc in tool_io if enc != "utf-8"]
        assert not offenders, f"non-UTF-8 text I/O: {offenders}"
        # Sanity: the run actually exercised reads and writes (guard against a no-op that
        # would make the assertion vacuously true).
        assert any(op == "read" for op, _, _ in tool_io)
        assert any(op == "write" for op, _, _ in tool_io)
        # And it touched the SKILL.md, the eval set, the JSON envelope, and the report.
        touched = " ".join(p for _, p, _ in tool_io)
        assert "SKILL.md" in touched
        assert "evals.json" in touched
        assert "report.json" in touched
        assert "report.html" in touched


class TestNonAsciiRoundTrip:
    """Non-ASCII text survives read/write and lands on disk as real UTF-8 bytes."""

    def test_skill_md_round_trip_is_utf8(self, tmp_path: Path) -> None:
        skill_md = tmp_path / "SKILL.md"
        skill_md.write_text(
            f"---\nname: s\ndescription: {_BASELINE_DESC}\n---\nBody\n",
            encoding="utf-8",
        )
        write_description(skill_md, _NON_ASCII)
        _, desc, _ = parse_skill_md(skill_md)
        assert desc == _NON_ASCII
        # The bytes on disk decode as UTF-8 and carry the em-dash's UTF-8 sequence,
        # proving the writer used UTF-8 rather than a locale codec.
        raw = skill_md.read_bytes()
        assert raw.decode("utf-8")  # no UnicodeDecodeError
        assert "—".encode() in raw
        # The backup is UTF-8 too and holds the pre-write original verbatim.
        bak = (tmp_path / "SKILL.md.bak").read_text(encoding="utf-8")
        assert _BASELINE_DESC in bak

    def test_report_html_contains_glyphs_and_is_utf8(self, tmp_path: Path) -> None:
        # A history row so the ✓/✗/– result glyphs (the cp1252-unsafe ones) are emitted.
        data: dict[str, Any] = {
            "original_description": _NON_ASCII,
            "best_description": _NON_ASCII,
            "history": [
                {
                    "iteration": 0,
                    "description": _NON_ASCII,
                    "is_best": True,
                    "train_results": [
                        {
                            "index": 0,
                            "query": "q — with dash",
                            "should_trigger": True,
                            "triggers": 3,
                            "runs": 3,
                            "pass": True,
                            "models": {"m": {"triggers": 3, "runs": 3}},
                        }
                    ],
                    "test_results": [],
                }
            ],
        }
        html = generate_html(data, skill_name="skill — name")
        assert "✓" in html and "—" in html
        out = tmp_path / "report.html"
        cli_module._write_html(  # pyright: ignore[reportPrivateUsage]
            out, data, "skill — name", refresh=False
        )
        assert "✓" in out.read_text(encoding="utf-8")
        assert out.read_bytes().decode("utf-8")  # valid UTF-8 on disk


class TestEvalSetEncoding:
    """The eval-set loader reads UTF-8 and fails legibly on non-UTF-8 input."""

    def test_utf8_eval_set_loads(self, tmp_path: Path) -> None:
        eval_file = tmp_path / "e.json"
        eval_file.write_text(
            json.dumps(
                [{"query": "café — piñata", "should_trigger": True}],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        loaded = _load_eval_set(eval_file)
        assert loaded[0]["query"] == "café — piñata"

    def test_non_utf8_eval_set_fails_legibly(self, tmp_path: Path) -> None:
        eval_file = tmp_path / "e.json"
        # Latin-1 bytes for é/— are invalid standalone UTF-8, so the UTF-8 read raises
        # UnicodeDecodeError; the loader must map it to the friendly precondition message.
        eval_file.write_bytes(
            b'[{"query": "caf\xe9 \x97 pi\xf1ata", "should_trigger": true}]'
        )
        with pytest.raises(
            ValueError, match=r"Invalid eval set: .* is not valid UTF-8"
        ):
            _load_eval_set(eval_file)


class TestAsciiLocaleSubprocess:
    """End-to-end proof under a real ascii locale (where the pre-fix code raised)."""

    def test_write_paths_survive_ascii_locale(self, tmp_path: Path) -> None:
        # Force a genuine ascii filesystem encoding: disable UTF-8 mode (PEP 540) and C
        # locale coercion (PEP 538) under LC_ALL=C. This is the environment that broke
        # the unqualified write_text calls; the fixed code must round-trip cleanly.
        env = {
            **os.environ,
            "PYTHONUTF8": "0",
            "PYTHONCOERCECLOCALE": "0",
            "PYTHONWARNINGS": "ignore",
            "LC_ALL": "C",
            "LANG": "C",
        }
        probe = subprocess.run(
            [sys.executable, "-c", "import locale;print(locale.getencoding())"],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        if probe.stdout.strip().lower() not in {"ascii", "us-ascii", "ansi_x3.4-1968"}:
            pytest.skip(f"platform did not yield an ascii locale: {probe.stdout!r}")

        script = textwrap.dedent(
            f"""
            import sys
            sys.path.insert(0, {str(Path(cli_module.__file__).parents[1])!r})
            from pathlib import Path
            from skill_optimizer import (
                generate_html, parse_skill_md, write_description,
            )
            skill = Path({str(tmp_path)!r}) / "SKILL.md"
            skill.write_text(
                "---\\nname: s\\ndescription: old\\n---\\nB\\n", encoding="utf-8"
            )
            write_description(skill, {_NON_ASCII!r})
            _, desc, _ = parse_skill_md(skill)
            assert desc == {_NON_ASCII!r}, desc
            html = generate_html(
                {{"history": [], "best_description": {_NON_ASCII!r},
                  "original_description": {_NON_ASCII!r}}},
                skill_name="s — x",
            )
            (Path({str(tmp_path)!r}) / "r.html").write_text(html, encoding="utf-8")
            print("OK")
            """
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "OK"
