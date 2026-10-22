"""Derive bounded fixes from trusted tools, never from a PR's artifact or hooks.

Invoked with Python's isolated mode from the workflow's own immutable base commit.
PR blobs are stdin data to Ruff, never files imported, installed, or executed here.
"""

import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Any, cast

DEPENDABOT_ID = 49699333
MAX_FILE_BYTES = 1_000_000
MAX_FIX_BYTES = 5_000_000
MAX_SOURCE_FILES = 1_000
SHA = re.compile(r"[0-9a-f]{40}")
SOURCE_PATH = re.compile(
    r"(?:src/skill_optimizer/|tests/)(?:[A-Za-z0-9_]+/)*[A-Za-z0-9_]+\.py"
    r"|optimize_description_v2\.py"
)
COMMIT_MESSAGE = "ci: 🤖 mechanical auto-fix"
MUTATION = """mutation($input: CreateCommitOnBranchInput!) {
  createCommitOnBranch(input: $input) { commit { oid } }
}"""


def require(condition: object, message: str) -> None:
    """Stop before writing whenever the trust contract cannot be established."""
    if not condition:
        raise ValueError(message)


class RejectRedirect(urllib.request.HTTPRedirectHandler):
    """Never forward an API credential to a redirected location."""

    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        """Fail closed even if the original endpoint returns a redirect."""
        raise ValueError("GitHub API redirects are forbidden")


class GitHub:
    """Use only GitHub's fixed API origin, with a separate write credential."""

    def __init__(self, repository: str, token: str) -> None:
        """Bind the client to the workflow repository and one credential."""
        require(
            re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)
            and all(part not in {".", ".."} for part in repository.split("/")),
            "Bad repo",
        )
        self.repository = repository
        self.token = token

    def request(self, path: str, payload: dict[str, Any] | None = None) -> Any:
        """Read repository metadata or perform the single explicit GraphQL write."""
        route = (
            "/graphql" if payload is not None else f"/repos/{self.repository}/{path}"
        )
        request = urllib.request.Request(
            "https://api.github.com" + route,
            data=None if payload is None else json.dumps(payload).encode(),
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        # The origin is constant, not a URL taken from PR metadata.
        opener = urllib.request.build_opener(RejectRedirect())
        with opener.open(request, timeout=30) as response:
            raw: object = json.load(response)
        require(
            isinstance(raw, (dict, list)),
            "GitHub API response must be an object or array",
        )
        if isinstance(raw, dict):
            require(not cast(dict[str, Any], raw).get("errors"), "GitHub API error")
        return cast(Any, raw)


def current_pr(api: GitHub, event: dict[str, Any]) -> dict[str, Any]:
    """Authenticate the current run attempt and its single live Dependabot PR."""
    original = event["workflow_run"]
    run_id = original["id"]
    require(type(run_id) is int and run_id > 0, "Invalid run ID")
    run = api.request(f"actions/runs/{run_id}")
    workflow = api.request("actions/workflows/validate.yml")
    repository = event["repository"]
    require(repository["full_name"] == api.repository, "Wrong repository")
    for candidate in (original, run):
        require(
            candidate["id"] == run_id
            and candidate["event"] == "pull_request"
            and candidate["status"] == "completed"
            and candidate["conclusion"] == "failure"
            and candidate["workflow_id"] == workflow["id"]
            and candidate["path"] == ".github/workflows/validate.yml"
            and candidate["repository"]["id"] == repository["id"]
            and candidate["head_repository"]["id"] == repository["id"]
            and candidate["actor"]["id"] == DEPENDABOT_ID
            and candidate["triggering_actor"]["id"] == DEPENDABOT_ID,
            "Untrusted Validate provenance",
        )
    require(workflow["state"] == "active", "Validate workflow is inactive")
    require(
        run["run_attempt"] == original["run_attempt"]
        and run["head_sha"] == original["head_sha"]
        and run["head_branch"] == original["head_branch"],
        "Stale Validate attempt",
    )
    require(SHA.fullmatch(run["head_sha"]), "Invalid head SHA")
    # Dependabot workflow_run metadata often has an empty pull_requests array.
    # Resolve GitHub's authoritative commit association instead, never an artifact.
    raw_association: object = api.request(
        f"commits/{run['head_sha']}/pulls?per_page=100"
    )
    require(isinstance(raw_association, list), "Invalid PR association")
    associated = cast(list[dict[str, Any]], raw_association)
    require(len(associated) < 100, "Incomplete PR association")
    candidates = [
        pr
        for pr in associated
        if pr["state"] == "open" and pr["head"]["sha"] == run["head_sha"]
    ]
    require(len(candidates) == 1, "Ambiguous PR association")
    number = candidates[0]["number"]
    require(type(number) is int and number > 0, "Invalid PR number")
    pr = api.request(f"pulls/{number}")
    require(
        pr["number"] == number
        and pr["state"] == "open"
        and not pr["draft"]
        and not pr["merged"]
        and pr["user"]["id"] == DEPENDABOT_ID
        and pr["head"]["repo"]["id"] == repository["id"]
        and pr["base"]["repo"]["id"] == repository["id"]
        and pr["base"]["ref"] == repository["default_branch"]
        and pr["head"]["ref"] == run["head_branch"]
        and pr["head"]["sha"] == run["head_sha"]
        and pr["head"]["ref"].startswith("dependabot/")
        and pr["head"]["ref"] != repository["default_branch"],
        "PR is not an eligible current Dependabot revision",
    )
    commit = api.request(f"git/commits/{run['head_sha']}")
    require(not commit["message"].startswith(COMMIT_MESSAGE), "Auto-fix loop")
    return pr


def source_tree(tree: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Select only canonical regular Python source files from a complete tree."""
    require(tree["truncated"] is False, "Incomplete source tree")
    result: dict[str, dict[str, Any]] = {}
    seen: set[str] = set()
    for item in tree["tree"]:
        path = item["path"]
        require(
            isinstance(path, str)
            and path not in seen
            and "\\" not in path
            and not any(part in {"", ".", ".."} for part in path.split("/")),
            "Malformed or duplicate tree path",
        )
        seen.add(path)
        require(item["type"] in {"blob", "tree", "commit"}, "Unknown tree object")
        if SOURCE_PATH.fullmatch(path):
            require(
                item["type"] == "blob" and item["mode"] == "100644",
                "Nonregular source file",
            )
            require(SHA.fullmatch(item["sha"]), "Invalid blob SHA")
            result[path] = item
    require(len(result) <= MAX_SOURCE_FILES, "Too many source files")
    return result


def fix_source(root: Path, path: str, content: bytes) -> bytes:
    """Run only base-locked Ruff with explicit base configuration and stdin data."""
    command = [str(root / ".venv/bin/ruff")]
    common = [
        "--no-cache",
        "--config",
        str(root / "pyproject.toml"),
        "--stdin-filename",
        path,
    ]
    # No shell, module import, filename creation, PR config discovery, or PR hooks.
    # The executable/config are from the immutable trusted workflow checkout.
    for args in (["check", "--fix-only", "--no-unsafe-fixes"], ["format"]):
        result = subprocess.run(  # noqa: S603  # nosec B603
            [*command, *args, *common, "-"],
            input=content,
            capture_output=True,
            cwd=root,
            env={"PATH": os.defpath, "HOME": str(root)},
            timeout=30,
            check=True,
        )
        content = result.stdout
    return content


def build_plan(
    api: GitHub, event: dict[str, Any], root: Path, trusted_sha: str
) -> dict[str, Any]:
    """Independently derive every output byte; no artifact or proposed patch is read."""
    require(SHA.fullmatch(trusted_sha), "Invalid trusted workflow SHA")
    pr = current_pr(api, event)
    head = pr["head"]["sha"]
    trusted = source_tree(api.request(f"git/trees/{trusted_sha}?recursive=1"))
    proposed = source_tree(api.request(f"git/trees/{head}?recursive=1"))
    # Source additions/deletions/mode changes require review, not partial automation.
    require(trusted.keys() == proposed.keys(), "Source file set or modes changed")
    additions: list[dict[str, str]] = []
    total = 0
    for path, item in sorted(proposed.items()):
        blob = api.request(f"git/blobs/{item['sha']}")
        require(
            blob["encoding"] == "base64" and blob["size"] <= MAX_FILE_BYTES,
            "Oversized or unsupported blob",
        )
        content = base64.b64decode("".join(blob["content"].split()), validate=True)
        require(len(content) == blob["size"], "Incorrect blob size")
        digest = hashlib.sha1(
            b"blob " + str(len(content)).encode() + b"\0" + content,
            usedforsecurity=False,
        ).hexdigest()
        require(digest == item["sha"], "Blob does not match its Git object ID")
        updated = fix_source(root, path, content)
        require(len(updated) <= MAX_FILE_BYTES, "Oversized formatted source")
        if updated != content:
            additions.append(
                {"path": path, "contents": base64.b64encode(updated).decode()}
            )
            total += len(updated)
    require(total <= MAX_FIX_BYTES, "Oversized fix set")
    current_pr(api, event)
    return {
        "head": head,
        "branch": pr["head"]["ref"],
        "number": pr["number"],
        "additions": additions,
    }


def apply_plan(
    api: GitHub, writer: GitHub, event: dict[str, Any], plan: dict[str, Any]
) -> str:
    """Commit a locally derived plan with an atomic expected-head comparison."""
    pr = current_pr(api, event)
    require(
        (plan["head"], plan["branch"], plan["number"])
        == (pr["head"]["sha"], pr["head"]["ref"], pr["number"]),
        "Stale fix plan",
    )
    require(plan["additions"], "Empty fix plan")
    result = writer.request(
        "",
        {
            "query": MUTATION,
            "variables": {
                "input": {
                    "branch": {
                        "repositoryNameWithOwner": api.repository,
                        "branchName": plan["branch"],
                    },
                    "expectedHeadOid": plan["head"],
                    "message": {"headline": COMMIT_MESSAGE},
                    "fileChanges": {"additions": plan["additions"]},
                }
            },
        },
    )
    return result["data"]["createCommitOnBranch"]["commit"]["oid"]


def main() -> None:
    """Prepare with read-only credentials, or publish the trusted local plan."""
    root = Path(__file__).resolve().parent.parent
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    api = GitHub(os.environ["GITHUB_REPOSITORY"], os.environ["GH_READ_TOKEN"])
    plan_path = Path(os.environ["RUNNER_TEMP"]) / "trusted-autofix-plan.json"
    if sys.argv[1:] == ["prepare"]:
        plan = build_plan(api, event, root, os.environ["TRUSTED_SHA"])
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
            output.write(f"has_fixes={str(bool(plan['additions'])).lower()}\n")
    else:
        require(sys.argv[1:] == ["apply"], "Expected prepare or apply")
        writer = GitHub(api.repository, os.environ["GH_WRITE_TOKEN"])
        oid = apply_plan(api, writer, event, json.loads(plan_path.read_text()))
        print(f"Committed trusted mechanical fixes: {oid}")


if __name__ == "__main__":
    main()
