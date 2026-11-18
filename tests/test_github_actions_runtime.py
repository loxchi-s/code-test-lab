from container_execution.adapters.github_actions.runtime import GitHubActionsRuntime
from container_execution.domain.execution import ExecutionStatus


async def test_status_matches_run_name_from_display_title(monkeypatch) -> None:
    execution_id = "a" * 32
    runtime = GitHubActionsRuntime("owner/repo", "token", "container-execute.yml")

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
    runtime = GitHubActionsRuntime("owner/repo", "token", "container-execute.yml")

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
