"""Characterization tests for SKILL.md parsing and writing.

These pin the *current* behavior of ``parse_skill_md`` and ``write_description``
in ``optimize_description_v2.py`` BEFORE any refactor. Where current behavior is a
quirk rather than an obvious intent, the test name/comment says so — the point is
to detect change, not to bless the quirk.
"""

from pathlib import Path

import pytest

import optimize_description_v2 as m


def _write(tmp_path: Path, text: str) -> Path:
    skill_md = tmp_path / "SKILL.md"
    skill_md.write_text(text)
    return skill_md


# --------------------------------------------------------------------------- #
# parse_skill_md
# --------------------------------------------------------------------------- #
class TestParseSkillMd:
    def test_inline_description(self, tmp_path: Path) -> None:
        skill_md = _write(
            tmp_path,
            "---\n"
            "name: my-skill\n"
            "description: Does a specific thing with widgets.\n"
            "---\n"
            "Body here.\n",
        )
        name, desc, body = m.parse_skill_md(skill_md)
        assert name == "my-skill"
        assert desc == "Does a specific thing with widgets."
        assert body == "Body here.\n"

    def test_folded_scalar_stops_at_next_key(self, tmp_path: Path) -> None:
        skill_md = _write(
            tmp_path,
            "---\n"
            "name: s\n"
            "description: >\n"
            "  First line\n"
            "  second line\n"
            "extra: 1\n"
            "---\n"
            "B\n",
        )
        name, desc, _ = m.parse_skill_md(skill_md)
        assert name == "s"
        # Folded into one whitespace-collapsed line; capture stops at `extra:`.
        assert desc == "First line second line"

    def test_block_scalar(self, tmp_path: Path) -> None:
        skill_md = _write(
            tmp_path,
            "---\ndescription: |\n  Line one.\n  Line two.\n---\n",
        )
        name, desc, _ = m.parse_skill_md(skill_md)
        assert name == ""  # no name key
        assert desc == "Line one. Line two."

    def test_blank_line_inside_block_does_not_break_capture(
        self, tmp_path: Path
    ) -> None:
        skill_md = _write(
            tmp_path,
            "---\ndescription: |\n  Para one.\n\n  Para two.\nkey: v\n---\n",
        )
        _, desc, _ = m.parse_skill_md(skill_md)
        # The blank line is captured (as empty) but does not terminate capture;
        # `key: v` does.
        assert desc == "Para one. Para two."

    def test_internal_whitespace_is_collapsed(self, tmp_path: Path) -> None:
        skill_md = _write(tmp_path, "---\ndescription:   lots    of   spaces\n---\n")
        _, desc, _ = m.parse_skill_md(skill_md)
        assert desc == "lots of spaces"

    def test_quotes_are_not_stripped_from_description(self, tmp_path: Path) -> None:
        # QUIRK: name quotes are stripped, description quotes are not.
        skill_md = _write(
            tmp_path,
            "---\nname: 'quoted-name'\ndescription: \"quoted value\"\n---\n",
        )
        name, desc, _ = m.parse_skill_md(skill_md)
        assert name == "quoted-name"
        assert desc == '"quoted value"'

    def test_missing_frontmatter_raises(self, tmp_path: Path) -> None:
        skill_md = _write(tmp_path, "no frontmatter here\n")
        with pytest.raises(ValueError, match="frontmatter"):
            m.parse_skill_md(skill_md)

    def test_inline_with_continuation_lines_folds(self, tmp_path: Path) -> None:
        skill_md = _write(
            tmp_path,
            "---\ndescription: foo\n  bar\n---\n",
        )
        _, desc, _ = m.parse_skill_md(skill_md)
        assert desc == "foo bar"


# --------------------------------------------------------------------------- #
# write_description
# --------------------------------------------------------------------------- #
class TestWriteDescription:
    def test_round_trip_single_line(self, tmp_path: Path) -> None:
        original = "---\nname: s\ndescription: old description\n---\nBody stays.\n"
        skill_md = _write(tmp_path, original)
        m.write_description(skill_md, "Brand new description.")
        name, desc, body = m.parse_skill_md(skill_md)
        assert name == "s"
        assert desc == "Brand new description."
        assert body == "Body stays.\n"

    def test_backup_file_holds_original(self, tmp_path: Path) -> None:
        original = "---\ndescription: old\n---\nB\n"
        skill_md = _write(tmp_path, original)
        m.write_description(skill_md, "new")
        bak = tmp_path / "SKILL.md.bak"
        assert bak.exists()
        assert bak.read_text() == original

    def test_description_is_moved_to_end_of_frontmatter(self, tmp_path: Path) -> None:
        # QUIRK: the new description is always appended after the other keys,
        # regardless of where the old one was.
        skill_md = _write(
            tmp_path,
            "---\nname: s\ndescription: old\nextra: 1\n---\nB\n",
        )
        m.write_description(skill_md, "NEW")
        text = skill_md.read_text()
        assert text == "---\nname: s\nextra: 1\ndescription: |\n  NEW\n---\nB\n"

    def test_multiline_description_folds_and_reparses_collapsed(
        self, tmp_path: Path
    ) -> None:
        skill_md = _write(tmp_path, "---\ndescription: old\n---\nB\n")
        m.write_description(skill_md, "A\nB")
        # Written as a block scalar with each line indented...
        assert "description: |\n  A\n  B\n" in skill_md.read_text()
        # ...and parsing collapses the newline to a space.
        _, desc, _ = m.parse_skill_md(skill_md)
        assert desc == "A B"

    def test_missing_frontmatter_raises_but_backup_already_written(
        self, tmp_path: Path
    ) -> None:
        # QUIRK: the backup is written before the frontmatter check, so a failed
        # write still leaves a .bak behind.
        skill_md = _write(tmp_path, "no frontmatter\n")
        with pytest.raises(ValueError, match="frontmatter"):
            m.write_description(skill_md, "new")
        assert (tmp_path / "SKILL.md.bak").exists()
