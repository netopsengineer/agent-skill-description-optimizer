"""Bind the installed uv bootstrap executable to the audited lockfile package."""

import argparse
import re
import sys
import tomllib
from collections.abc import Sequence
from pathlib import Path

from scripts.advisory_policy import PolicyError, array, mapping, require, string


def bootstrap_requirement(project: object, lock: object) -> str:
    """Return the sole exact bootstrap requirement, rejecting divergent lock state."""
    groups = mapping(mapping(project, "project").get("dependency-groups"), "groups")
    requirements = array(groups.get("bootstrap"), "bootstrap group")
    require(len(requirements) == 1, "bootstrap must contain exactly one uv requirement")
    requirement = string(requirements[0], "bootstrap requirement")
    require(
        re.fullmatch(r"uv==[0-9]+\.[0-9]+\.[0-9]+", requirement) is not None,
        "bootstrap must pin uv==X.Y.Z exactly",
    )
    packages = [
        mapping(raw, "locked package")
        for raw in array(mapping(lock, "lock").get("package"), "lock.package")
    ]
    matches = [package for package in packages if package.get("name") == "uv"]
    require(len(matches) == 1, "uv.lock must contain exactly one uv package")
    package = matches[0]
    require(
        package.get("source") == {"registry": "https://pypi.org/simple"},
        "bootstrap must be a PyPI registry package in the scanner inventory",
    )
    require(
        requirement == f"uv=={string(package.get('version'), 'uv version')}",
        "bootstrap requirement and uv.lock version disagree",
    )
    return requirement


def main(argv: Sequence[str] | None = None) -> int:
    """Print the locked pip requirement and optionally verify the running executable."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=Path("pyproject.toml"))
    parser.add_argument("--lock", type=Path, default=Path("uv.lock"))
    parser.add_argument("--installed-version")
    args = parser.parse_args(argv)
    try:
        requirement = bootstrap_requirement(
            tomllib.loads(args.project.read_text(encoding="utf-8")),
            tomllib.loads(args.lock.read_text(encoding="utf-8")),
        )
        if args.installed_version is not None:
            require(
                args.installed_version.split()[:2]
                == ["uv", requirement.removeprefix("uv==")],
                "installed uv version differs from the audited bootstrap pin",
            )
        print(requirement)
    except (PolicyError, ValueError, OSError) as error:
        print(f"uv bootstrap check failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
