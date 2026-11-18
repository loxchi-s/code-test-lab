from __future__ import annotations

import asyncio
import json
import os
import re
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from container_execution.domain.execution import ExecutionRequest, ExecutionStatus


_REPOSITORY_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}")
_WORKFLOW_PATTERN = re.compile(r"[A-Za-z0-9_.-]{1,128}\.ya?ml")
_EXECUTION_ID_PATTERN = re.compile(r"[a-f0-9]{32}")
_REF_PATTERN = re.compile(r"[A-Za-z0-9._/-]{1,128}")


class GitHubActionsError(RuntimeError):
    pass


class GitHubActionsRuntime:
    """Dispatch and observe the fixed workflow; never sends arbitrary shell commands."""

    def __init__(self, repository: str, token: str, workflow: str) -> None:
        if not _REPOSITORY_PATTERN.fullmatch(repository):
            raise ValueError("GITHUB_REPOSITORY must be a valid owner/name")
        if not token or not token.strip():
            raise ValueError("GitHub token must not be empty")
        if not _WORKFLOW_PATTERN.fullmatch(workflow):
            raise ValueError("CONTAINER_WORKFLOW must be a workflow filename")
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

    @staticmethod
    def _validate_lookup(execution_id: str, ref: str) -> None:
        if not _EXECUTION_ID_PATTERN.fullmatch(execution_id):
            raise ValueError("execution_id must be a 32-character lowercase hex UUID")
        if (
            not _REF_PATTERN.fullmatch(ref)
            or ref.startswith("-")
            or ".." in ref
            or ref.endswith("/")
        ):
            raise ValueError("ref is not a valid safe Git ref")

    def _request(self, method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
        url = f"https://api.github.com/repos/{self._repository}/{path.lstrip('/')}"
        body = json.dumps(payload).encode() if payload is not None else None
        request = Request(
            url,
            data=body,
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self._token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "Content-Type": "application/json",
                "User-Agent": "code-test-lab",
            },
        )
        try:
            with urlopen(request, timeout=20) as response:
                raw = response.read()
                return response.status, json.loads(raw) if raw else {}
        except HTTPError as exc:
            # Do not echo response bodies: API error payloads are untrusted and may contain secrets.
            raise GitHubActionsError(f"GitHub API returned HTTP {exc.code}") from exc
        except (URLError, TimeoutError) as exc:
            reason = exc.reason if isinstance(exc, URLError) else "request timed out"
            raise GitHubActionsError(f"GitHub API connection failed: {reason}") from exc
        except json.JSONDecodeError as exc:
            raise GitHubActionsError("GitHub API returned invalid JSON") from exc

    async def dispatch(self, request: ExecutionRequest) -> None:
        workflow = quote(self._workflow, safe="")
        await asyncio.to_thread(
            self._request,
            "POST",
            f"actions/workflows/{workflow}/dispatches",
            {
                "ref": request.ref,
                "inputs": {
                    "execution_id": request.execution_id,
                    "target": request.target.value,
                },
            },
        )

    async def _find_run(self, execution_id: str, ref: str) -> dict | None:
        self._validate_lookup(execution_id, ref)
        workflow = quote(self._workflow, safe="")
        query = urlencode(
            {"branch": ref, "event": "workflow_dispatch", "per_page": 100}
        )
        _, data = await asyncio.to_thread(
            self._request,
            "GET",
            f"actions/workflows/{workflow}/runs?{query}",
        )
        expected = f"Container execution {execution_id}"
        return next(
            (
                run
                for run in data.get("workflow_runs", [])
                if run.get("display_title") == expected
            ),
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
        run_id = run.get("id")
        if not isinstance(run_id, int) or isinstance(run_id, bool) or run_id <= 0:
            raise GitHubActionsError("GitHub returned an invalid workflow run identifier")
        await asyncio.to_thread(
            self._request, "POST", f"actions/runs/{run_id}/cancel", None
        )
