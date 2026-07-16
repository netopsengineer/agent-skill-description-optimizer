"""Parsing and writing of a skill's ``SKILL.md`` frontmatter."""

import re
from pathlib import Path

# YAML block/folded scalar indicators that carry no inline text on the ``key:`` line.
_BLOCK_SCALAR_INDICATORS = frozenset({"|", ">", "|-", ">-", "|+", ">+", ""})

# Characters kept verbatim when a skill name is reduced to a filesystem path token.
# Every other character — path separators, whitespace, shell/format metacharacters —
# collapses to a single ``_``. A shared skill's ``name:`` is attacker-controlled, so it
# must never reach a filename verbatim.
_UNSAFE_NAME_CHARS = re.compile(r"[^A-Za-z0-9._-]+")

# Cap on the path-token length so a pathologically long ``name:`` cannot produce an
# over-long filename that the OS rejects with ``ENAMETOOLONG`` mid-run.
_MAX_NAME_TOKEN_CHARS = 64


def safe_name_token(name: str, *, fallback: str = "skill") -> str:
    """Reduce a skill name to a filesystem-safe token for use as a path component.

    A shared ``SKILL.md``'s ``name:`` frontmatter is attacker-controlled, so it must
    never reach a filename verbatim: an absolute path, a ``../`` sequence, or a bare path
    separator would let a downstream write escape its intended directory. This collapses
    every run of characters outside ``[A-Za-z0-9._-]`` to a single ``_``, strips leading
    and trailing separators/dots (so a name that is only dots or separators — e.g. ``..``
    or ``/`` — can never yield ``.``, ``..``, or an empty component), caps the length,
    and falls back to a fixed token when nothing usable remains.

    Args:
        name: The raw, possibly attacker-controlled skill name.
        fallback: Token returned when ``name`` has no filesystem-safe characters.

    Returns:
        A token drawn only from ``[A-Za-z0-9._-]``, never empty, never ``.`` or ``..``,
        and never containing a path separator.
    """
    token = _UNSAFE_NAME_CHARS.sub("_", name).strip("._-")[:_MAX_NAME_TOKEN_CHARS]
    return token.strip("._-") or fallback


def parse_skill_md(skill_md: Path) -> tuple[str, str, str]:
    """Extract the name, description, and body from a ``SKILL.md`` file.

    Handles inline, folded (``>``), and block (``|``) scalar description styles, and
    collapses internal whitespace in the description to single spaces.

    Args:
        skill_md: Path to the ``SKILL.md`` file.

    Returns:
        A ``(name, description, body)`` tuple. ``name`` is empty when absent.

    Raises:
        ValueError: If the file has no ``---`` delimited YAML frontmatter.
    """
    text = skill_md.read_text()
    match = re.search(r"^---\n(.*?)\n---\n?(.*)$", text, re.DOTALL)
    if not match:
        raise ValueError(f"No YAML frontmatter in {skill_md}")
    frontmatter, body = match[1], match[2]

    name = ""
    if name_match := re.search(r"^name:\s*(.+)$", frontmatter, re.MULTILINE):
        name = name_match[1].strip().strip("'\"")

    desc_parts: list[str] = []
    capturing = False
    for line in frontmatter.splitlines():
        if not capturing:
            if desc_match := re.match(r"^description:\s*(.*)$", line):
                capturing = True
                rest = desc_match[1].strip()
                if rest not in _BLOCK_SCALAR_INDICATORS:
                    desc_parts.append(rest)
            continue
        # Continuation: indented or blank lines belong to the description; the first
        # non-indented, non-blank line is the next top-level key and ends capture.
        if re.match(r"^\s+\S", line) or line.strip() == "":
            desc_parts.append(line.strip())
        else:
            break

    description = re.sub(r"\s+", " ", " ".join(p for p in desc_parts if p)).strip()
    return name, description, body


def write_description(skill_md: Path, new_description: str) -> None:
    """Rewrite the description in ``SKILL.md`` as a literal block scalar (``|``).

    The original file is backed up to ``<name>.bak`` first. The new description is
    appended after the remaining frontmatter keys (so key order may change).

    Args:
        skill_md: Path to the ``SKILL.md`` file to modify.
        new_description: The replacement description text.

    Raises:
        ValueError: If the file has no ``---`` delimited YAML frontmatter. Note the
            backup is written before this check, so a failed write still leaves a
            ``.bak`` behind.
    """
    text = skill_md.read_text()
    skill_md.with_suffix(f"{skill_md.suffix}.bak").write_text(text)
    match = re.search(r"^---\n(.*?)\n---", text, re.DOTALL)
    if not match:
        raise ValueError(f"No frontmatter in {skill_md}")
    frontmatter = match[1]
    # Drop the existing description block (``description:`` up to the next top-level
    # key or end of frontmatter).
    fm_without_desc = re.sub(
        r"^description:.*?(?=^\w[\w-]*:|\Z)",
        "",
        frontmatter + "\n",
        flags=re.DOTALL | re.MULTILINE,
    ).rstrip("\n")
    folded = "\n  ".join(new_description.split("\n"))
    new_frontmatter = (
        fm_without_desc + "\n" if fm_without_desc else ""
    ) + f"description: |\n  {folded}"
    new_text = text[: match.start(1)] + new_frontmatter + text[match.end(1) :]
    skill_md.write_text(new_text)
