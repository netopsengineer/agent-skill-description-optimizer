"""Exercise the trusted auto-fixer with hostile PR data and no network credentials."""

import base64
import copy
import hashlib
import io
import json
import os
import runpy
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from email.message import Message
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from scripts import autofix

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github/workflows/auto-fix.yml"
HEAD = "a" * 40
TRUSTED = "b" * 40
REPOSITORY = "owner/optimizer"
SOURCE = "src/skill_optimizer/cli.py"


def _blob(content: bytes) -> tuple[str, dict[str, Any]]:
    digest = hashlib.sha1(
        b"blob " + str(len(content)).encode() + b"\0" + content,
        usedforsecurity=False,
    ).hexdigest()
    return digest, {
        "encoding": "base64",
        "size": len(content),
        "content": base64.b64encode(content).decode(),
    }


def _entry(path: str = SOURCE, content: bytes = b"value=1\n") -> dict[str, Any]:
    return {"path": path, "type": "blob", "mode": "100644", "sha": _blob(content)[0]}


class FakeGitHub(autofix.GitHub):
    """Offline API whose explicit route allowlist detects artifact access and writes."""

    def __init__(self) -> None:
        super().__init__(REPOSITORY, "test-read-token")
        self.calls: list[tuple[str, dict[str, Any] | None]] = []
        self.event: dict[str, Any] = {
            "repository": {
                "id": 700,
                "full_name": REPOSITORY,
                "default_branch": "main",
            },
            "workflow_run": {
                "id": 123,
                "event": "pull_request",
                "status": "completed",
                "conclusion": "failure",
                "workflow_id": 456,
                "path": ".github/workflows/validate.yml",
                "repository": {"id": 700},
                "head_repository": {"id": 700},
                "actor": {"id": autofix.DEPENDABOT_ID, "login": "dependabot[bot]"},
                "triggering_actor": {
                    "id": autofix.DEPENDABOT_ID,
                    "login": "dependabot[bot]",
                },
                "run_attempt": 2,
                "head_sha": HEAD,
                "head_branch": "dependabot/pip/ruff-1",
                "pull_requests": [{"number": 42}],
            },
        }
        self.run = copy.deepcopy(self.event["workflow_run"])
        self.pr: dict[str, Any] = {
            "number": 42,
            "state": "open",
            "draft": False,
            "merged": False,
            "user": {"id": autofix.DEPENDABOT_ID, "login": "dependabot[bot]"},
            "head": {"repo": {"id": 700}, "ref": self.run["head_branch"], "sha": HEAD},
            "base": {"repo": {"id": 700}, "ref": "main"},
        }
        self.associations: Any = [copy.deepcopy(self.pr)]
        self.workflow: dict[str, Any] = {"id": 456, "state": "active"}
        self.commit: dict[str, Any] = {"message": "build(deps): bump ruff"}
        self.trusted_tree: dict[str, Any] = {"truncated": False, "tree": [_entry()]}
        self.proposed_tree = copy.deepcopy(self.trusted_tree)
        digest, blob = _blob(b"value=1\n")
        self.blobs: dict[str, dict[str, Any]] = {digest: blob}
        self.result: dict[str, Any] = {
            "data": {"createCommitOnBranch": {"commit": {"oid": "c" * 40}}}
        }

    def request(self, path: str, payload: dict[str, Any] | None = None) -> Any:
        self.calls.append((path, copy.deepcopy(payload)))
        if payload is not None:
            assert path == ""
            return self.result
        routes: dict[str, Any] = {
            "actions/runs/123": self.run,
            "actions/workflows/validate.yml": self.workflow,
            "pulls/42": self.pr,
            f"commits/{HEAD}/pulls?per_page=100": self.associations,
            f"git/commits/{HEAD}": self.commit,
            f"git/trees/{TRUSTED}?recursive=1": self.trusted_tree,
            f"git/trees/{HEAD}?recursive=1": self.proposed_tree,
            **{f"git/blobs/{sha}": blob for sha, blob in self.blobs.items()},
        }
        assert path in routes, f"Unexpected API route: {path}"
        return copy.deepcopy(routes[path])


def _set_path(document: dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    target = document
    for part in parts[:-1]:
        target = target[part]
    target[parts[-1]] = value


@pytest.fixture
def api() -> FakeGitHub:
    return FakeGitHub()


def test_current_bot_revision_uses_stable_ids_not_display_names(
    api: FakeGitHub,
) -> None:
    for source in (api.run, api.event["workflow_run"]):
        source["actor"]["login"] = "renamed-account"
        source["triggering_actor"]["login"] = "renamed-account"
    api.pr["user"]["login"] = "renamed-account"
    assert autofix.current_pr(api, api.event) == api.pr
    assert [path for path, _ in api.calls] == [
        "actions/runs/123",
        "actions/workflows/validate.yml",
        f"commits/{HEAD}/pulls?per_page=100",
        "pulls/42",
        f"git/commits/{HEAD}",
    ]


@pytest.mark.parametrize("run_id", [0, -1, True, False, "123", 123.0, None])
def test_run_identifier_is_a_positive_json_integer(
    api: FakeGitHub, run_id: Any
) -> None:
    api.event["workflow_run"]["id"] = run_id
    with pytest.raises(ValueError, match="Invalid run ID"):
        autofix.current_pr(api, api.event)
    assert api.calls == []


@pytest.mark.parametrize("source", ["event", "fresh"])
@pytest.mark.parametrize(
    "path,value",
    [
        ("event", "push"),
        ("status", "in_progress"),
        ("conclusion", "success"),
        ("workflow_id", 999),
        ("path", ".github/workflows/untrusted.yml"),
        ("repository.id", 701),
        ("head_repository.id", 701),
        ("actor.id", 900),
        ("triggering_actor.id", 900),
    ],
)
def test_original_and_fresh_run_must_have_trusted_provenance(
    api: FakeGitHub, source: str, path: str, value: Any
) -> None:
    _set_path(api.event["workflow_run"] if source == "event" else api.run, path, value)
    with pytest.raises(ValueError, match="Untrusted Validate provenance"):
        autofix.current_pr(api, api.event)


@pytest.mark.parametrize(
    "scenario,message",
    [
        ("run_id", "Untrusted Validate provenance"),
        ("repository", "Wrong repository"),
        ("inactive", "Validate workflow is inactive"),
        ("attempt", "Stale Validate attempt"),
        ("head", "Stale Validate attempt"),
        ("branch", "Stale Validate attempt"),
        ("invalid_sha", "Invalid head SHA"),
        ("no_pr", "Ambiguous PR association"),
        ("multiple_prs", "Ambiguous PR association"),
        ("loop", "Auto-fix loop"),
    ],
)
def test_stale_ambiguous_or_replayed_run_is_rejected(
    api: FakeGitHub, scenario: str, message: str
) -> None:
    if scenario == "run_id":
        api.run["id"] = 124
    elif scenario == "repository":
        api.event["repository"]["full_name"] = "attacker/optimizer"
    elif scenario == "inactive":
        api.workflow["state"] = "disabled_manually"
    elif scenario == "attempt":
        api.run["run_attempt"] += 1
    elif scenario == "head":
        api.run["head_sha"] = "d" * 40
    elif scenario == "branch":
        api.run["head_branch"] = "dependabot/other"
    elif scenario == "invalid_sha":
        api.run["head_sha"] = api.event["workflow_run"]["head_sha"] = "not-a-sha"
    elif scenario == "no_pr":
        api.associations = []
    elif scenario == "multiple_prs":
        api.associations.append({**api.associations[0], "number": 43})
    else:
        api.commit["message"] = autofix.COMMIT_MESSAGE + "\n\nRepeated trigger"
    with pytest.raises(ValueError, match=message):
        autofix.current_pr(api, api.event)


@pytest.mark.parametrize("number", [0, -1, True, False, "42", 42.0, None])
def test_pr_number_is_a_positive_json_integer(api: FakeGitHub, number: Any) -> None:
    api.associations[0]["number"] = number
    with pytest.raises(ValueError, match="Invalid PR number"):
        autofix.current_pr(api, api.event)


@pytest.mark.parametrize(
    "path,value",
    [
        ("number", 43),
        ("state", "closed"),
        ("draft", True),
        ("merged", True),
        ("user.id", 900),
        ("head.repo.id", 701),
        ("base.repo.id", 701),
        ("base.ref", "release"),
        ("head.ref", "dependabot/other"),
        ("head.sha", "d" * 40),
    ],
)
def test_pr_must_remain_open_current_and_same_repository(
    api: FakeGitHub, path: str, value: Any
) -> None:
    _set_path(api.pr, path, value)
    with pytest.raises(ValueError, match="eligible current Dependabot"):
        autofix.current_pr(api, api.event)


@pytest.mark.parametrize("branch", ["feature/pretend-bot", "dependabot/default"])
def test_non_bot_or_default_branch_is_never_writable(
    api: FakeGitHub, branch: str
) -> None:
    api.pr["head"]["ref"] = branch
    api.run["head_branch"] = api.event["workflow_run"]["head_branch"] = branch
    if branch == "dependabot/default":
        api.event["repository"]["default_branch"] = branch
        api.pr["base"]["ref"] = branch
    with pytest.raises(ValueError, match="eligible current Dependabot"):
        autofix.current_pr(api, api.event)


@pytest.mark.parametrize(
    "path",
    [
        "uv.lock",
        "pyproject.toml",
        "README.md",
        ".pre-commit-config.yaml",
        ".github/workflows/auto-fix.yml",
        ".github/workflows/validate.yml",
        ".github/advisory-exceptions.json",
        ".github/osv-scanner-empty.toml",
        "ci/security-scanner/Dockerfile",
        "scripts/autofix.py",
        "scripts/advisory_policy.py",
        "sitecustomize.py",
        "json.py",
        ".venv/bin/ruff",
        "src/skill_optimizer/evil.py/child.py",
        "src/skill_optimizer/evil.py\n",
        "src/skill_optimizer/evil\x00.py",
        "src/skill_optimizer/évil.py",
        "tests/.hidden.py",
        "tests/evil-name.py",
    ],
)
def test_controls_and_noncanonical_paths_are_never_formatted(
    api: FakeGitHub, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    api.proposed_tree["tree"].append(_entry(path))
    formatter = Mock(return_value=b"value = 1\n")
    monkeypatch.setattr(autofix, "fix_source", formatter)
    plan = autofix.build_plan(api, api.event, ROOT, TRUSTED)
    assert [item["path"] for item in plan["additions"]] == [SOURCE]
    formatter.assert_called_once_with(ROOT, SOURCE, b"value=1\n")
    assert not any(
        payload is not None or "artifacts" in path for path, payload in api.calls
    )


@pytest.mark.parametrize(
    "path",
    [SOURCE, "tests/test_cli.py", "tests/unit/test_1.py", "optimize_description_v2.py"],
)
def test_canonical_regular_python_paths_are_selected(path: str) -> None:
    entry = _entry(path)
    assert autofix.source_tree({"truncated": False, "tree": [entry]}) == {path: entry}


@pytest.mark.parametrize(
    "mode,kind",
    [("120000", "blob"), ("100755", "blob"), ("160000", "commit"), ("040000", "tree")],
)
def test_symlink_executable_submodule_and_directory_are_not_source(
    mode: str, kind: str
) -> None:
    entry = {**_entry(), "mode": mode, "type": kind}
    with pytest.raises(ValueError, match="Nonregular source file"):
        autofix.source_tree({"truncated": False, "tree": [entry]})


@pytest.mark.parametrize(
    "change", ["added", "deleted", "symlink", "executable", "submodule"]
)
def test_source_set_and_mode_changes_require_review(
    api: FakeGitHub, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    if change == "added":
        api.proposed_tree["tree"].append(_entry("tests/new_test.py"))
    elif change == "deleted":
        api.proposed_tree["tree"] = []
    elif change == "symlink":
        api.proposed_tree["tree"][0]["mode"] = "120000"
    elif change == "executable":
        api.proposed_tree["tree"][0]["mode"] = "100755"
    else:
        api.proposed_tree["tree"][0].update(type="commit", mode="160000")
    formatter = Mock()
    monkeypatch.setattr(autofix, "fix_source", formatter)
    with pytest.raises(
        ValueError, match=r"Source file set or modes changed|Nonregular source file"
    ):
        autofix.build_plan(api, api.event, ROOT, TRUSTED)
    formatter.assert_not_called()
    assert not any(path.startswith("git/blobs/") for path, _ in api.calls)


@pytest.mark.parametrize("truncated", [True, None, 0, "false"])
def test_truncated_or_nonboolean_tree_is_rejected(truncated: Any) -> None:
    with pytest.raises(ValueError, match="Incomplete source tree"):
        autofix.source_tree({"truncated": truncated, "tree": []})


def test_source_difference_is_mechanically_derived_not_copied_from_artifact(
    api: FakeGitHub, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    content = b"value=2\n"
    digest, blob = _blob(content)
    api.proposed_tree["tree"][0]["sha"] = digest
    api.blobs[digest] = blob
    (tmp_path / "fixes.json").write_text(
        json.dumps([{"path": ".github/workflows/validate.yml", "content": "poison"}]),
        encoding="utf-8",
    )
    formatter = Mock(return_value=b"value = 2\n")
    monkeypatch.setattr(autofix, "fix_source", formatter)
    plan = autofix.build_plan(api, api.event, tmp_path, TRUSTED)
    assert plan == {
        "head": HEAD,
        "branch": api.pr["head"]["ref"],
        "number": 42,
        "additions": [
            {"path": SOURCE, "contents": base64.b64encode(b"value = 2\n").decode()}
        ],
    }
    formatter.assert_called_once_with(tmp_path, SOURCE, content)
    assert [path for path, _ in api.calls].count("actions/runs/123") == 2
    assert not any(
        payload is not None or "artifacts" in path for path, payload in api.calls
    )


@pytest.mark.parametrize(
    "trusted", ["", "../main", "c" * 39, "G" * 40, "c" * 40 + "\n"]
)
def test_trusted_commit_is_an_immutable_canonical_sha(
    api: FakeGitHub, trusted: str
) -> None:
    with pytest.raises(ValueError, match="Invalid trusted workflow SHA"):
        autofix.build_plan(api, api.event, ROOT, trusted)
    assert api.calls == []


def test_blob_identifier_must_be_a_canonical_sha(api: FakeGitHub) -> None:
    api.proposed_tree["tree"][0]["sha"] = "../../malicious"
    with pytest.raises(ValueError, match="Invalid blob SHA"):
        autofix.build_plan(api, api.event, ROOT, TRUSTED)


@pytest.mark.parametrize(
    "change,value,message",
    [
        ("encoding", "utf-8", "Oversized or unsupported blob"),
        ("size", autofix.MAX_FILE_BYTES + 1, "Oversized or unsupported blob"),
        ("size", -1, "Incorrect blob size"),
        ("size", 1, "Incorrect blob size"),
        ("content", "not!base64", ""),
        ("content", base64.b64encode(b"value=2\n").decode(), "Git object ID"),
    ],
)
def test_corrupt_unsupported_or_oversized_blob_is_never_formatted(
    api: FakeGitHub,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
    value: Any,
    message: str,
) -> None:
    api.blobs[_blob(b"value=1\n")[0]][change] = value
    formatter = Mock()
    monkeypatch.setattr(autofix, "fix_source", formatter)
    with pytest.raises(ValueError, match=message or None):
        autofix.build_plan(api, api.event, ROOT, TRUSTED)
    formatter.assert_not_called()


def test_base64_line_wrapping_is_accepted_and_identical_bytes_produce_no_writes(
    api: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    blob = api.blobs[_blob(b"value=1\n")[0]]
    blob["content"] = "\n".join(blob["content"]) + "\n"
    monkeypatch.setattr(autofix, "fix_source", Mock(return_value=b"value=1\n"))
    plan = autofix.build_plan(api, api.event, ROOT, TRUSTED)
    assert plan["additions"] == []
    writer = FakeGitHub()
    with pytest.raises(ValueError, match="Empty fix plan"):
        autofix.apply_plan(api, writer, api.event, plan)
    assert writer.calls == []


@pytest.mark.parametrize("limit", ["file", "total"])
def test_output_byte_budgets_are_enforced(
    api: FakeGitHub, monkeypatch: pytest.MonkeyPatch, limit: str
) -> None:
    monkeypatch.setattr(autofix, "fix_source", Mock(return_value=b"value = 1\n"))
    monkeypatch.setattr(
        autofix, "MAX_FILE_BYTES" if limit == "file" else "MAX_FIX_BYTES", 8
    )
    with pytest.raises(
        ValueError,
        match="Oversized formatted source" if limit == "file" else "Oversized fix set",
    ):
        autofix.build_plan(api, api.event, ROOT, TRUSTED)


def test_empty_source_tree_is_a_valid_noop(api: FakeGitHub) -> None:
    api.trusted_tree["tree"] = api.proposed_tree["tree"] = []
    assert autofix.build_plan(api, api.event, ROOT, TRUSTED)["additions"] == []


def test_plan_fails_if_head_changes_during_formatting(
    api: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    def advance_head(root: Path, path: str, content: bytes) -> bytes:
        api.pr["head"]["sha"] = "d" * 40
        return content

    monkeypatch.setattr(autofix, "fix_source", advance_head)
    with pytest.raises(ValueError, match="eligible current Dependabot"):
        autofix.build_plan(api, api.event, ROOT, TRUSTED)
    assert not any(payload is not None for _, payload in api.calls)


def _plan(api: FakeGitHub) -> dict[str, Any]:
    return {
        "head": HEAD,
        "branch": api.pr["head"]["ref"],
        "number": 42,
        "additions": [{"path": SOURCE, "contents": "dmFsdWUgPSAxCg=="}],
    }


def test_publish_uses_one_atomic_commit_with_expected_head(api: FakeGitHub) -> None:
    writer = FakeGitHub()
    plan = _plan(api)
    assert autofix.apply_plan(api, writer, api.event, plan) == "c" * 40
    assert writer.calls == [
        (
            "",
            {
                "query": autofix.MUTATION,
                "variables": {
                    "input": {
                        "branch": {
                            "repositoryNameWithOwner": REPOSITORY,
                            "branchName": plan["branch"],
                        },
                        "expectedHeadOid": HEAD,
                        "message": {"headline": autofix.COMMIT_MESSAGE},
                        "fileChanges": {"additions": plan["additions"]},
                    }
                },
            },
        )
    ]
    assert not any(payload is not None for _, payload in api.calls)


@pytest.mark.parametrize(
    "key,value", [("head", "d" * 40), ("branch", "dependabot/other"), ("number", 43)]
)
def test_stale_plan_never_reaches_writer(api: FakeGitHub, key: str, value: Any) -> None:
    plan = _plan(api)
    plan[key] = value
    writer = FakeGitHub()
    with pytest.raises(ValueError, match="Stale fix plan"):
        autofix.apply_plan(api, writer, api.event, plan)
    assert writer.calls == []


def test_compare_and_swap_rejection_never_retries_or_force_pushes(
    api: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer = FakeGitHub()
    request = Mock(side_effect=ValueError("GitHub API error"))
    monkeypatch.setattr(writer, "request", request)
    with pytest.raises(ValueError, match="GitHub API error"):
        autofix.apply_plan(api, writer, api.event, _plan(api))
    assert request.call_count == 1
    assert request.call_args.args[1]["variables"]["input"]["expectedHeadOid"] == HEAD


def test_ruff_command_is_base_owned_stdin_only_and_has_no_token_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("GH_READ_TOKEN", "never-forward-this")
    monkeypatch.setenv("GH_WRITE_TOKEN", "never-forward-this-either")
    monkeypatch.setenv("PYTHONPATH", "/attacker")
    process = Mock(
        side_effect=[
            subprocess.CompletedProcess([], 0, b"intermediate", b""),
            subprocess.CompletedProcess([], 0, b"formatted", b""),
        ]
    )
    monkeypatch.setattr(autofix.subprocess, "run", process)
    assert autofix.fix_source(tmp_path, SOURCE, b"untrusted source") == b"formatted"
    common = [
        "--no-cache",
        "--config",
        str(tmp_path / "pyproject.toml"),
        "--stdin-filename",
        SOURCE,
        "-",
    ]
    assert process.call_args_list[0].args[0] == [
        str(tmp_path / ".venv/bin/ruff"),
        "check",
        "--fix-only",
        "--no-unsafe-fixes",
        *common,
    ]
    assert process.call_args_list[1].args[0] == [
        str(tmp_path / ".venv/bin/ruff"),
        "format",
        *common,
    ]
    for call, expected_input in zip(
        process.call_args_list, [b"untrusted source", b"intermediate"], strict=True
    ):
        assert call.kwargs == {
            "input": expected_input,
            "capture_output": True,
            "cwd": tmp_path,
            "env": {"PATH": os.defpath, "HOME": str(tmp_path)},
            "timeout": 30,
            "check": True,
        }


@pytest.mark.parametrize(
    "error",
    [subprocess.CalledProcessError(1, "ruff"), subprocess.TimeoutExpired("ruff", 30)],
)
def test_ruff_failure_or_timeout_cannot_create_a_plan(
    api: FakeGitHub, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    process = Mock(side_effect=error)
    monkeypatch.setattr(autofix.subprocess, "run", process)
    with pytest.raises(type(error)):
        autofix.build_plan(api, api.event, ROOT, TRUSTED)
    assert process.call_count == 1
    assert not any(payload is not None for _, payload in api.calls)


def test_real_ruff_does_not_execute_pr_code_hooks_shadow_imports_or_config(
    api: FakeGitHub, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trusted = tmp_path / "trusted"
    (trusted / ".venv/bin").mkdir(parents=True)
    (trusted / ".venv/bin/ruff").symlink_to(ROOT / ".venv/bin/ruff")
    shutil.copyfile(ROOT / "pyproject.toml", trusted / "pyproject.toml")
    hostile = tmp_path / "hostile"
    hostile.mkdir()
    sentinel = tmp_path / "PR_CODE_EXECUTED"
    malicious = f"from pathlib import Path\nPath({str(sentinel)!r}).write_text('executed')\nvalues=[1,2]\n".encode()
    digest, blob = _blob(malicious)
    api.blobs[digest] = blob
    api.proposed_tree["tree"][0]["sha"] = digest
    for path in (
        "pyproject.toml",
        ".pre-commit-config.yaml",
        "sitecustomize.py",
        "json.py",
        "scripts/autofix.py",
        ".venv/bin/ruff",
    ):
        api.proposed_tree["tree"].append(_entry(path, malicious))
    (hostile / "sitecustomize.py").write_bytes(malicious)
    (hostile / "json.py").write_bytes(malicious)
    (hostile / "pyproject.toml").write_text("THIS IS NOT VALID TOML", encoding="utf-8")
    monkeypatch.setenv("PYTHONPATH", str(hostile))
    monkeypatch.chdir(hostile)
    plan = autofix.build_plan(api, api.event, trusted, TRUSTED)
    assert [item["path"] for item in plan["additions"]] == [SOURCE]
    output = base64.b64decode(plan["additions"][0]["contents"])
    assert b"values = [1, 2]" in output
    assert str(sentinel).encode() in output
    assert not sentinel.exists()
    assert not (trusted / SOURCE).exists()
    assert (hostile / "pyproject.toml").read_text() == "THIS IS NOT VALID TOML"


@pytest.mark.parametrize(
    "repository",
    [
        "",
        "owner",
        "owner/..",
        "./repo",
        "owner/repo/extra",
        "../repo",
        "https://evil.test/repo",
        "owner/repo\n",
        "owner/rep?x",
    ],
)
def test_api_repository_cannot_select_an_external_origin(repository: str) -> None:
    with pytest.raises(ValueError, match="Bad repo"):
        autofix.GitHub(repository, "secret")


@pytest.mark.parametrize("payload", [None, {"query": "query { viewer { login } }"}])
@pytest.mark.parametrize("response", [{"ok": True}, [], {"errors": []}])
def test_api_uses_fixed_https_origin_headers_and_json(
    monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any] | None, response: Any
) -> None:
    opener = Mock(return_value=io.BytesIO(json.dumps(response).encode()))
    monkeypatch.setattr(
        urllib.request, "build_opener", Mock(return_value=Mock(open=opener))
    )
    client = autofix.GitHub(REPOSITORY, "private-token")
    assert client.request("https://attacker.invalid/path", payload) == response
    request = opener.call_args.args[0]
    assert request.full_url == (
        "https://api.github.com/graphql"
        if payload is not None
        else f"https://api.github.com/repos/{REPOSITORY}/https://attacker.invalid/path"
    )
    assert request.get_header("Authorization") == "Bearer private-token"
    assert request.get_header("Accept") == "application/vnd.github+json"
    assert request.get_header("X-github-api-version") == "2022-11-28"
    assert request.data == (None if payload is None else json.dumps(payload).encode())
    assert opener.call_args.kwargs == {"timeout": 30}


def test_graphql_error_hides_server_supplied_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opener = Mock(
        return_value=io.BytesIO(b'{"errors": [{"message": "private-token"}]}')
    )
    monkeypatch.setattr(
        urllib.request, "build_opener", Mock(return_value=Mock(open=opener))
    )
    with pytest.raises(ValueError, match=r"^GitHub API error$"):
        autofix.GitHub(REPOSITORY, "private-token").request("", {"query": "mutation"})


@pytest.mark.parametrize(
    "error",
    [
        urllib.error.HTTPError(
            "https://api.github.com/test", 403, "Forbidden", Message(), None
        ),
        urllib.error.URLError("connection failed"),
    ],
)
def test_http_failure_is_not_retried_and_does_not_print_token(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error: Exception,
) -> None:
    opener = Mock(side_effect=error)
    monkeypatch.setattr(
        urllib.request, "build_opener", Mock(return_value=Mock(open=opener))
    )
    with pytest.raises(type(error)) as caught:
        autofix.GitHub(REPOSITORY, "private-token").request("pulls/42")
    assert "private-token" not in str(caught.value)
    assert opener.call_count == 1
    assert capsys.readouterr() == ("", "")


def _wrapper_environment(
    api: FakeGitHub, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Path:
    event = tmp_path / "event.json"
    event.write_text(json.dumps(api.event), encoding="utf-8")
    for key, value in {
        "GITHUB_EVENT_PATH": str(event),
        "GITHUB_REPOSITORY": REPOSITORY,
        "GH_READ_TOKEN": "read-only-token",
        "RUNNER_TEMP": str(tmp_path),
        "TRUSTED_SHA": TRUSTED,
        "GITHUB_OUTPUT": str(tmp_path / "github-output"),
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("GH_WRITE_TOKEN", raising=False)

    def request(
        self: autofix.GitHub, path: str, payload: dict[str, Any] | None = None
    ) -> Any:
        return api.request(path, payload)

    monkeypatch.setattr(autofix.GitHub, "request", request)
    return tmp_path / "trusted-autofix-plan.json"


@pytest.mark.parametrize("changed", [False, True])
def test_prepare_wrapper_writes_only_locally_derived_plan_without_write_token(
    api: FakeGitHub, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, changed: bool
) -> None:
    plan_path = _wrapper_environment(api, monkeypatch, tmp_path)
    plan_path.write_text('{"additions": [{"path": "evil.py"}]}', encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["autofix.py", "prepare"])
    monkeypatch.setattr(
        autofix,
        "fix_source",
        Mock(return_value=b"value = 1\n" if changed else b"value=1\n"),
    )
    autofix.main()
    plan = json.loads(plan_path.read_text())
    assert plan["head"] == HEAD
    assert bool(plan["additions"]) is changed
    assert (
        tmp_path / "github-output"
    ).read_text() == f"has_fixes={str(changed).lower()}\n"
    assert not any(payload is not None for _, payload in api.calls)
    assert "GH_WRITE_TOKEN" not in os.environ


def test_apply_wrapper_reads_local_plan_and_publishes_once(
    api: FakeGitHub,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plan_path = _wrapper_environment(api, monkeypatch, tmp_path)
    plan_path.write_text(json.dumps(_plan(api)), encoding="utf-8")
    monkeypatch.setenv("GH_WRITE_TOKEN", "write-only-token")
    monkeypatch.setattr(sys, "argv", ["autofix.py", "apply"])
    autofix.main()
    assert (
        capsys.readouterr().out == f"Committed trusted mechanical fixes: {'c' * 40}\n"
    )
    assert sum(payload is not None for _, payload in api.calls) == 1


@pytest.mark.parametrize("arguments", [[], ["publish"], ["prepare", "apply"]])
def test_wrapper_rejects_unknown_mode_before_requesting_write_token(
    api: FakeGitHub,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    arguments: list[str],
) -> None:
    _wrapper_environment(api, monkeypatch, tmp_path)
    monkeypatch.setattr(sys, "argv", ["autofix.py", *arguments])
    with pytest.raises(ValueError, match="Expected prepare or apply"):
        autofix.main()
    assert api.calls == []


def test_script_entrypoint_executes_wrapper(
    api: FakeGitHub, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _wrapper_environment(api, monkeypatch, tmp_path)
    monkeypatch.setattr(sys, "argv", ["autofix.py", "unknown"])
    with pytest.raises(ValueError, match="Expected prepare or apply"):
        runpy.run_path(str(ROOT / "scripts/autofix.py"), run_name="__main__")


def test_workflow_uses_base_code_and_no_pr_artifacts_or_execution() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert "ref: ${{ github.workflow_sha }}" in workflow
    assert "persist-credentials: false" in workflow
    assert (
        "uv sync --locked --only-group dev --no-install-project --no-cache" in workflow
    )
    assert "python -I scripts/autofix.py prepare" in workflow
    assert "python -I scripts/autofix.py apply" in workflow
    assert "download-artifact" not in workflow
    assert "upload-artifact" not in workflow
    assert "git apply" not in workflow
    assert "git push" not in workflow
    assert "prek run" not in workflow
    assert "uv run pytest" not in workflow
    assert "ref: ${{ github.event.workflow_run.head_sha }}" not in workflow
    for step in (
        "Mint a GitHub App token",
        "Revalidate and atomically commit the locally derived fixes",
    ):
        section = workflow.split(f"- name: {step}\n", 1)[1].split("      - name:", 1)[0]
        assert "if: steps.fixes.outputs.has_fixes == 'true'" in section
    assert workflow.index(
        "- name: Independently derive bounded fixes"
    ) < workflow.index("- name: Mint a GitHub App token")


def test_both_dependency_scans_are_isolated_from_test_and_hook_execution() -> None:
    root = WORKFLOW.parent
    for filename in ("validate.yml", "security-scan.yml"):
        workflow = (root / filename).read_text(encoding="utf-8")
        dependency_job = workflow.split("  deps-security:\n", 1)[1].split(
            "\n  # Conventional-Commit", 1
        )[0]
        assert "bash scripts/audit-dependencies.sh" in dependency_job
        assert "persist-credentials: false" in dependency_job
        assert "prek run" not in dependency_job
        assert "uv run pytest" not in dependency_job
    weekly = (root / "security-scan.yml").read_text(encoding="utf-8")
    full_gate = weekly.split("  full-gate:\n", 1)[1].split("  deps-security:\n", 1)[0]
    assert "prek run" in full_gate
    assert "audit-dependencies.sh" not in full_gate


@pytest.mark.parametrize(
    "path",
    [
        None,
        12,
        "",
        "/tests/file.py",
        "./tests/file.py",
        "tests//file.py",
        "tests/../file.py",
        "tests\\file.py",
    ],
)
def test_malformed_tree_paths_fail_closed(path: Any) -> None:
    entry = {**_entry(), "path": path}
    with pytest.raises(ValueError, match="Malformed or duplicate tree path"):
        autofix.source_tree({"truncated": False, "tree": [entry]})


def test_duplicate_tree_paths_cannot_hide_a_symlink_or_alternate_blob() -> None:
    entries = [_entry(), {**_entry(), "mode": "120000", "sha": "e" * 40}]
    with pytest.raises(ValueError, match="Malformed or duplicate tree path"):
        autofix.source_tree({"truncated": False, "tree": entries})


def test_unknown_tree_objects_fail_closed_even_outside_write_scope() -> None:
    with pytest.raises(ValueError, match="Unknown tree object"):
        autofix.source_tree(
            {"truncated": False, "tree": [{"path": "README.md", "type": "surprise"}]}
        )


@pytest.mark.parametrize(
    "tree",
    [
        {},
        {"truncated": False},
        {"truncated": False, "tree": [dict[str, Any]()]},
        {"truncated": False, "tree": None},
    ],
)
def test_malformed_tree_objects_never_produce_a_plan(tree: dict[str, Any]) -> None:
    with pytest.raises((KeyError, TypeError)):
        autofix.source_tree(tree)


def test_source_file_count_is_bounded_before_blob_downloads(api: FakeGitHub) -> None:
    api.proposed_tree["tree"] = [
        _entry(f"tests/test_{i}.py") for i in range(autofix.MAX_SOURCE_FILES + 1)
    ]
    with pytest.raises(ValueError, match="Too many source files"):
        autofix.build_plan(api, api.event, ROOT, TRUSTED)
    assert not any(path.startswith("git/blobs/") for path, _ in api.calls)
    assert (
        len(
            autofix.source_tree(
                {"truncated": False, "tree": api.proposed_tree["tree"][:-1]}
            )
        )
        == autofix.MAX_SOURCE_FILES
    )


def test_empty_workflow_pr_metadata_uses_authoritative_commit_association(
    api: FakeGitHub,
) -> None:
    api.run["pull_requests"] = api.event["workflow_run"]["pull_requests"] = []
    api.associations.extend(
        [
            {"number": 40, "state": "closed", "head": {"sha": HEAD}},
            {"number": 41, "state": "open", "head": {"sha": "e" * 40}},
        ]
    )
    assert autofix.current_pr(api, api.event) == api.pr


@pytest.mark.parametrize(
    "associations,message",
    [
        ({}, "Invalid PR association"),
        (None, "Invalid PR association"),
        ([dict[str, Any]()] * 100, "Incomplete PR association"),
    ],
)
def test_invalid_or_paginated_commit_associations_fail_closed(
    api: FakeGitHub, associations: Any, message: str
) -> None:
    api.associations = associations
    with pytest.raises(ValueError, match=message):
        autofix.current_pr(api, api.event)


@pytest.mark.parametrize("response", [None, True, 3, "not-an-object"])
def test_scalar_api_response_is_rejected(
    monkeypatch: pytest.MonkeyPatch, response: Any
) -> None:
    opener = Mock(open=Mock(return_value=io.BytesIO(json.dumps(response).encode())))
    monkeypatch.setattr(urllib.request, "build_opener", Mock(return_value=opener))
    with pytest.raises(
        ValueError, match="GitHub API response must be an object or array"
    ):
        autofix.GitHub(REPOSITORY, "private-token").request("pulls/42")


def test_api_redirect_is_rejected_without_retransmitting_authorization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RedirectingTransport:
        def __init__(self, handler: autofix.RejectRedirect) -> None:
            self.handler = handler

        def open(self, request: urllib.request.Request, timeout: int) -> Any:
            assert timeout == 30
            assert request.get_header("Authorization") == "Bearer private-token"
            return self.handler.redirect_request(
                request, None, 302, "Found", {}, "https://attacker.invalid/steal"
            )

    factory = Mock(side_effect=RedirectingTransport)
    monkeypatch.setattr(urllib.request, "build_opener", factory)
    with pytest.raises(
        ValueError, match="GitHub API redirects are forbidden"
    ) as caught:
        autofix.GitHub(REPOSITORY, "private-token").request("pulls/42")
    assert "private-token" not in str(caught.value)
    factory.assert_called_once()
    assert isinstance(factory.call_args.args[0], autofix.RejectRedirect)


def test_daily_reconciliation_preserves_push_manual_and_pypi_gates() -> None:
    validate = (WORKFLOW.parent / "validate.yml").read_text(encoding="utf-8")
    release = (WORKFLOW.parent / "release.yml").read_text(encoding="utf-8")
    for text, cron in ((validate, '"17 3 * * *"'), (release, '"47 3 * * *"')):
        triggers = text.split("on:\n", 1)[1].split("\npermissions:", 1)[0]
        assert "  push:\n    branches: [main]" in triggers
        assert "  workflow_dispatch:" in triggers
        assert f"  schedule:\n    - cron: {cron}" in triggers
    assert (
        "  pull_request:" in validate.split("on:\n", 1)[1].split("\npermissions:", 1)[0]
    )
    assert "if: needs.release.outputs.released == 'true'" in release
    assert "if: vars.PUBLISH_TO_PYPI == 'true'" in release
    assert "ref: ${{ needs.release.outputs.tag }}" in release


def test_autoapproval_rechecks_current_head_and_binds_review_and_merge() -> None:
    workflow = (WORKFLOW.parent / "dependabot-auto-merge.yml").read_text(
        encoding="utf-8"
    )
    assert "github.event.pull_request.user.id == 49699333" in workflow
    assert "github.event.sender.id == 49699333" in workflow
    assert "github.event.sender.id == 305056826" in workflow
    assert (
        "github.repository == github.event.pull_request.head.repo.full_name" in workflow
    )
    step = workflow.split(
        "- name: Approve and enable auto-merge for the current revision\n", 1
    )[1]
    for check in (
        '.state == "open"',
        ".draft == false",
        ".merged == false",
        ".user.id == 49699333",
        ".head.repo.full_name == $repo",
        ".base.repo.full_name == $repo",
        ".base.ref == $base",
        ".head.sha == $head",
    ):
        assert check in step
    assert step.count("if ! is_current; then") == 2
    first_check = step.index("if ! is_current; then")
    review = step.index('review_id=$(jq -nc --arg commit_id "$HEAD_SHA"')
    second_check = step.index("if ! is_current; then", first_check + 1)
    dismissal = step.index('reviews/$review_id/dismissals" --input -')
    merge = step.index(
        'gh pr merge --auto --squash --match-head-commit "$HEAD_SHA" "$PR_URL"'
    )
    assert first_check < review < second_check < dismissal < merge
    assert '{event: "APPROVE", commit_id: $commit_id,' in step
    assert "--admin" not in step
