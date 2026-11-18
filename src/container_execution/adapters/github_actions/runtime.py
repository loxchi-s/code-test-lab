from __future__ import annotations

import asyncio
import json
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from container_execution.domain.execution import ExecutionRequest, ExecutionStatus


class GitHubActionsError(RuntimeError):
    pass


class GitHubActionsRuntime:
    """Dispatch and observe the fixed workflow; never sends arbitrary shell commands."""

    def __init__(self, repository: str, token: str, workflow: str) -> None:
        if not repository or "/" not in repository:
            raise ValueError("GITHUB_REPOSITORY must be owner/name")
        self._repository = repository
        self._token = token
        self._workflow = workflow

    @classmethod
    def from_environment(cls) -> GitHubActionsRuntime:
        token = os.environ.get("GITHUB_TOKEN")
        repository = os.environ.get("GITHUB_REPOSITORY")
        if not token or not repository:
            raise RuntimeError("GITHUB_TOKEN and GITHUB_REPOSITORY are required")
        return cls(repository, token, os.environ.get("CONTAINER_WORKFLOW", "container-execute.yml"))

    def _request(self, method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
        url = f"https://api.github.com/repos/{self._repository}/{path.lstrip('/')}"
        body = json.dumps(payload).encode() if payload is not None else None
        request = Request(url, data=body, method=method, headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self._token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
            "User-Agent": "code-test-lab",
        })
        try:
            with urlopen(request, timeout=20) as response:
                raw = response.read()
                return response.status, json.loads(raw) if raw else {}
        except HTTPError as exc:
            detail = exc.read(2048).decode("utf-8", errors="replace")
            raise GitHubActionsError(f"GitHub API returned HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise GitHubActionsError(f"GitHub API connection failed: {exc.reason}") from exc

    async def dispatch(self, request: ExecutionRequest) -> None:
        await asyncio.to_thread(
            self._request, "POST",
            f"actions/workflows/{self._workflow}/dispatches",
            {"ref": request.ref, "inputs": {
                "execution_id": request.execution_id, "target": request.target.value,
            }},
        )

    async def _find_run(self, execution_id: str, ref: str) -> dict | None:
        _, data = await asyncio.to_thread(
            self._request, "GET",
            f"actions/workflows/{self._workflow}/runs?branch={ref}&event=workflow_dispatch&per_page=100",
        )
        expected = f"Container execution {execution_id}"
        return next(
            (run for run in data.get("workflow_runs", []) if run.get("display_title") == expected),
            None,
        )

    async def status(self, execution_id: str, ref: str) -> ExecutionStatus:
        run = await self._find_run(execution_id, ref)
        if run is None:
            return ExecutionStatus.QUEUED
        if run.get("status") != "completed":
            return ExecutionStatus.IN_PROGRESS
        conclusion = run.get("conclusion")
        return {
            "success": ExecutionStatus.COMPLETED,
            "cancelled": ExecutionStatus.CANCELLED,
        }.get(conclusion, ExecutionStatus.FAILED)

    async def cancel(self, execution_id: str, ref: str) -> None:
        run = await self._find_run(execution_id, ref)
        if run is None:
            raise GitHubActionsError("Execution run was not found")
        await asyncio.to_thread(
            self._request, "POST", f"actions/runs/{run['id']}/cancel", None,
        )
