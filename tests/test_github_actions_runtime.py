import pytest

from container_execution.adapters.github_actions.runtime import (
    GitHubActionsError,
    GitHubActionsRuntime,
)
from container_execution.domain.execution import (
    ExecutionRequest,
    ExecutionStatus,
    ExecutionTarget,
)


def make_runtime() -> GitHubActionsRuntime:
    return GitHubActionsRuntime("owner/repo", "test-token", "container-execute.yml")


async def test_dispatch_sends_only_fixed_workflow_inputs(monkeypatch) -> None:
    runtime = make_runtime()
    captured = {}

    def fake_request(method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
        captured.update(method=method, path=path, payload=payload)
        return 204, {}

    monkeypatch.setattr(runtime, "_request", fake_request)
    request = ExecutionRequest("a" * 32, ExecutionTarget.UNIT, "main")

    await runtime.dispatch(request)

    assert captured == {
        "method": "POST",
        "path": "actions/workflows/container-execute.yml/dispatches",
        "payload": {
            "ref": "main",
            "inputs": {"execution_id": "a" * 32, "target": "unit"},
        },
    }


async def test_status_matches_run_name_from_display_title(monkeypatch) -> None:
    execution_id = "a" * 32
    runtime = make_runtime()

    def fake_request(method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
        assert method == "GET"
        assert "branch=main" in path
        return 200, {
            "workflow_runs": [
                {
                    "id": 42,
                    "name": "Container Execution",
                    "display_title": f"Container execution {execution_id}",
                    "status": "completed",
                    "conclusion": "success",
                }
            ]
        }

    monkeypatch.setattr(runtime, "_request", fake_request)

    assert await runtime.status(execution_id, "main") is ExecutionStatus.COMPLETED


async def test_status_does_not_match_workflow_name_as_execution_id(monkeypatch) -> None:
    execution_id = "b" * 32
    runtime = make_runtime()

    def fake_request(method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
        return 200, {
            "workflow_runs": [
                {
                    "id": 43,
                    "name": f"Container execution {execution_id}",
                    "display_title": "Unrelated run title",
                    "status": "completed",
                    "conclusion": "success",
                }
            ]
        }

    monkeypatch.setattr(runtime, "_request", fake_request)

    assert await runtime.status(execution_id, "main") is ExecutionStatus.QUEUED


@pytest.mark.parametrize(
    ("run_status", "conclusion", "expected"),
    [
        ("queued", None, ExecutionStatus.IN_PROGRESS),
        ("in_progress", None, ExecutionStatus.IN_PROGRESS),
        ("completed", "success", ExecutionStatus.COMPLETED),
        ("completed", "cancelled", ExecutionStatus.CANCELLED),
        ("completed", "failure", ExecutionStatus.FAILED),
        ("completed", "timed_out", ExecutionStatus.FAILED),
    ],
)
async def test_status_maps_github_run_states(
    monkeypatch, run_status: str, conclusion: str | None, expected: ExecutionStatus
) -> None:
    execution_id = "c" * 32
    runtime = make_runtime()

    def fake_request(method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
        return 200, {
            "workflow_runs": [
                {
                    "id": 44,
                    "display_title": f"Container execution {execution_id}",
                    "status": run_status,
                    "conclusion": conclusion,
                }
            ]
        }

    monkeypatch.setattr(runtime, "_request", fake_request)

    assert await runtime.status(execution_id, "main") is expected


async def test_cancel_targets_the_correlated_run(monkeypatch) -> None:
    runtime = make_runtime()
    calls = []

    def fake_request(method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
        calls.append((method, path, payload))
        if method == "GET":
            return 200, {
                "workflow_runs": [
                    {
                        "id": 987,
                        "display_title": f"Container execution {'d' * 32}",
                    }
                ]
            }
        return 202, {}

    monkeypatch.setattr(runtime, "_request", fake_request)

    await runtime.cancel("d" * 32, "main")

    assert calls[-1] == ("POST", "actions/runs/987/cancel", None)


async def test_cancel_fails_when_execution_does_not_exist(monkeypatch) -> None:
    runtime = make_runtime()

    def fake_request(method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
        return 200, {"workflow_runs": []}

    monkeypatch.setattr(runtime, "_request", fake_request)

    with pytest.raises(GitHubActionsError, match="Execution run was not found"):
        await runtime.cancel("e" * 32, "main")


@pytest.mark.parametrize(
    ("repository", "token", "workflow"),
    [
        ("owner/repo/extra", "token", "container-execute.yml"),
        ("owner/repo?query=x", "token", "container-execute.yml"),
        ("owner/repo", "", "container-execute.yml"),
        ("owner/repo", "token", "../workflow.yml"),
        ("owner/repo", "token", "subdir/workflow.yml"),
    ],
)
def test_runtime_rejects_unsafe_configuration(
    repository: str, token: str, workflow: str
) -> None:
    with pytest.raises(ValueError):
        GitHubActionsRuntime(repository, token, workflow)


@pytest.mark.parametrize(
    ("execution_id", "ref"),
    [
        ("not-an-id", "main"),
        ("f" * 32, "../main"),
        ("f" * 32, "-main"),
        ("f" * 32, "main..branch"),
    ],
)
async def test_status_rejects_unsafe_lookup_inputs(
    execution_id: str, ref: str
) -> None:
    runtime = make_runtime()

    with pytest.raises(ValueError):
        await runtime.status(execution_id, ref)


async def test_lookup_url_encodes_branch_query(monkeypatch) -> None:
    runtime = make_runtime()
    captured = {}

    def fake_request(method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
        captured["path"] = path
        return 200, {"workflow_runs": []}

    monkeypatch.setattr(runtime, "_request", fake_request)

    await runtime.status("f" * 32, "feature/test-branch")

    assert "branch=feature%2Ftest-branch" in captured["path"]
    assert "event=workflow_dispatch" in captured["path"]


async def test_cancel_rejects_non_integer_run_id(monkeypatch) -> None:
    runtime = make_runtime()

    def fake_request(method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
        return 200, {
            "workflow_runs": [
                {
                    "id": "987/cancel-other-run",
                    "display_title": f"Container execution {'e' * 32}",
                }
            ]
        }

    monkeypatch.setattr(runtime, "_request", fake_request)

    with pytest.raises(GitHubActionsError, match="invalid workflow run identifier"):
        await runtime.cancel("e" * 32, "main")


def test_runtime_accepts_workflow_filename_and_rejects_path_segments() -> None:
    GitHubActionsRuntime("owner/repo", "token", "container-execute.yml")

    with pytest.raises(ValueError, match="workflow filename"):
        GitHubActionsRuntime("owner/repo", "token", "../container-execute.yml")


async def test_cancel_rejects_invalid_run_identifier_before_request(monkeypatch) -> None:
    runtime = make_runtime()
    calls = []

    def fake_request(method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
        calls.append((method, path))
        return 200, {
            "workflow_runs": [
                {
                    "id": True,
                    "display_title": f"Container execution {'a' * 32}",
                }
            ]
        }

    monkeypatch.setattr(runtime, "_request", fake_request)

    with pytest.raises(GitHubActionsError, match="invalid workflow run identifier"):
        await runtime.cancel("a" * 32, "main")

    assert len(calls) == 1
    assert calls[0][0] == "GET"
