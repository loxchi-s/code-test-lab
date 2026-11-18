from __future__ import annotations

import asyncio
import io
import json
import os
import re
import zipfile
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from container_execution.domain.execution import ExecutionRequest, ExecutionStatus
from container_execution.domain.execution_result import ExecutionResult

_REPOSITORY_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}")
_WORKFLOW_PATTERN = re.compile(r"[A-Za-z0-9_.-]{1,128}\\.ya?ml")
_EXECUTION_ID_PATTERN = re.compile(r"[a-f0-9]{32}")
_REF_PATTERN = re.compile(r"[A-Za-z0-9._/-]{1,128}")
_MAX_ARTIFACT_BYTES = 8 * 1024 * 1024
_MAX_LOG_BYTES = 1024 * 1024


class GitHubActionsError(RuntimeError):
    pass


class GitHubActionsRuntime:
    """Dispatch trusted workflows and retrieve bounded result artifacts."""

    def __init__(
        self,
        repository: str,
        token: str,
        workflow: str,
        workflow_ref: str = "main",
    ) -> None:
        if not _REPOSITORY_PATTERN.fullmatch(repository):
            raise ValueError("GITHUB_REPOSITORY must be a valid owner/name")
        if not token or not token.strip():
            raise ValueError("GitHub token must not be empty")
        if not _WORKFLOW_PATTERN.fullmatch(workflow):
            raise ValueError("CONTAINER_WORKFLOW must be a workflow filename")
        self._validate_ref(workflow_ref)
        self._repository = repository
        self._token = token
        self._workflow = workflow
        self._workflow_ref = workflow_ref

    @staticmethod
    def _validate_ref(ref: str) -> None:
        if (
            not isinstance(ref, str)
            or not _REF_PATTERN.fullmatch(ref)
            or ref.startswith("-")
            or ".." in ref
            or ref.endswith("/")
        ):
            raise ValueError("ref is not a valid safe Git ref")

    @classmethod
    def from_environment(cls) -> GitHubActionsRuntime:
        token = os.environ.get("GITHUB_TOKEN")
        repository = os.environ.get("GITHUB_REPOSITORY")
        if not token or not repository:
            raise RuntimeError("GITHUB_TOKEN and GITHUB_REPOSITORY are required")
        return cls(
            repository,
            token,
            os.environ.get("CONTAINER_WORKFLOW", "container-execute.yml"),
            os.environ.get("CONTAINER_WORKFLOW_REF", "main"),
        )

    @staticmethod
    def _validate_lookup(execution_id: str, ref: str) -> None:
        if not _EXECUTION_ID_PATTERN.fullmatch(execution_id):
            raise ValueError("execution_id must be a 32-character lowercase hex UUID")
        GitHubActionsRuntime._validate_ref(ref)

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
            raise GitHubActionsError(f"GitHub API returned HTTP {exc.code}") from exc
        except (URLError, TimeoutError) as exc:
            reason = exc.reason if isinstance(exc, URLError) else "request timed out"
            raise GitHubActionsError(f"GitHub API connection failed: {reason}") from exc
        except json.JSONDecodeError as exc:
            raise GitHubActionsError("GitHub API returned invalid JSON") from exc

    def _download_artifact(self, artifact_id: int) -> bytes:
        url = (
            f"https://api.github.com/repos/{self._repository}"
            f"/actions/artifacts/{artifact_id}/zip"
        )
        request = Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self._token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "code-test-lab",
            },
        )
        try:
            with urlopen(request, timeout=30) as response:
                raw = response.read(_MAX_ARTIFACT_BYTES + 1)
        except HTTPError as exc:
            raise GitHubActionsError(f"GitHub artifact download returned HTTP {exc.code}") from exc
        except (URLError, TimeoutError) as exc:
            raise GitHubActionsError("GitHub artifact download failed") from exc
        if len(raw) > _MAX_ARTIFACT_BYTES:
            raise GitHubActionsError("Execution artifact exceeds the 8 MiB limit")
        return raw

    async def dispatch(self, request: ExecutionRequest) -> None:
        # Always execute the trusted workflow from the configured base ref.
        # The requested source ref is checked out separately as untrusted data.
        workflow = quote(self._workflow, safe="")
        await asyncio.to_thread(
            self._request,
            "POST",
            f"actions/workflows/{workflow}/dispatches",
            {
                "ref": self._workflow_ref,
                "inputs": {
                    "execution_id": request.execution_id,
                    "target": request.target.value,
                    "source_ref": request.ref,
                },
            },
        )

    async def _find_run(self, execution_id: str, ref: str) -> dict | None:
        self._validate_lookup(execution_id, ref)
        workflow = quote(self._workflow, safe="")
        query = urlencode(
            {"branch": self._workflow_ref, "event": "workflow_dispatch", "per_page": 100}
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

    @staticmethod
    def _status_for_run(run: dict) -> ExecutionStatus:
        if run.get("status") != "completed":
            return ExecutionStatus.IN_PROGRESS
        conclusion = run.get("conclusion")
        return {
            "success": ExecutionStatus.COMPLETED,
            "cancelled": ExecutionStatus.CANCELLED,
        }.get(conclusion, ExecutionStatus.FAILED)

    async def status(self, execution_id: str, ref: str) -> ExecutionStatus:
        run = await self._find_run(execution_id, ref)
        if run is None:
            return ExecutionStatus.QUEUED
        return self._status_for_run(run)

    async def result(self, execution_id: str, ref: str) -> ExecutionResult | None:
        run = await self._find_run(execution_id, ref)
        if run is None:
            return None
        status = self._status_for_run(run)
        if status is ExecutionStatus.IN_PROGRESS:
            return ExecutionResult(execution_id, status, ref)
        run_id = run.get("id")
        if not isinstance(run_id, int) or isinstance(run_id, bool) or run_id <= 0:
            raise GitHubActionsError("GitHub returned an invalid workflow run identifier")

        _, artifact_data = await asyncio.to_thread(
            self._request,
            "GET",
            f"actions/runs/{run_id}/artifacts?per_page=100",
        )
        expected_name = f"execution-{execution_id}"
        artifact = next(
            (
                item
                for item in artifact_data.get("artifacts", [])
                if item.get("name") == expected_name and not item.get("expired", False)
            ),
            None,
        )
        if artifact is None:
            # The workflow may have completed while its artifact is still being uploaded.
            return None
        artifact_id = artifact.get("id")
        if not isinstance(artifact_id, int) or isinstance(artifact_id, bool) or artifact_id <= 0:
            raise GitHubActionsError("GitHub returned an invalid artifact identifier")

        raw = await asyncio.to_thread(self._download_artifact, artifact_id)
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                names = {item.filename.rsplit("/", 1)[-1]: item for item in archive.infolist()}
                required = {"metadata.txt", "stdout.log", "stderr.log"}
                if not required.issubset(names):
                    raise GitHubActionsError("Execution artifact is missing required result files")
                values = {}
                for name in required:
                    member = names[name]
                    if member.file_size > _MAX_LOG_BYTES:
                        raise GitHubActionsError(f"Execution artifact file {name} exceeds 1 MiB")
                    values[name] = archive.read(member).decode("utf-8", errors="replace")
        except (zipfile.BadZipFile, OSError, KeyError) as exc:
            raise GitHubActionsError("GitHub returned an invalid execution artifact") from exc

        metadata = {}
        for line in values["metadata.txt"].splitlines():
            key, separator, value = line.partition("=")
            if separator:
                metadata[key] = value
        try:
            exit_code = int(metadata["exit_code"])
        except (KeyError, ValueError) as exc:
            raise GitHubActionsError("Execution artifact has invalid exit_code metadata") from exc
        if not 0 <= exit_code <= 255:
            raise GitHubActionsError("Execution artifact exit_code is outside the valid range")
        return ExecutionResult(
            execution_id=execution_id,
            status=status,
            source_ref=metadata.get("source_ref", ref),
            exit_code=exit_code,
            stdout=values["stdout.log"],
            stderr=values["stderr.log"],
            commit_sha=metadata.get("commit_sha", ""),
        )

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
