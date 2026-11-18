from __future__ import annotations

import os

from mcp.server.fastmcp import FastMCP

from container_execution.adapters.github_actions.runtime import GitHubActionsRuntime
from container_execution.application.service import ContainerExecutionService
from container_execution.domain.execution import ExecutionTarget

mcp = FastMCP("code-test-lab-container")
_service: ContainerExecutionService | None = None


def service() -> ContainerExecutionService:
    global _service
    if _service is None:
        _service = ContainerExecutionService(GitHubActionsRuntime.from_environment())
    return _service


@mcp.tool()
async def container_run(target: str, ref: str = "main") -> dict[str, str]:
    """Dispatch an allowlisted test target to the code-test-lab GitHub Actions runner."""
    try:
        selected = ExecutionTarget(target)
    except ValueError as exc:
        raise ValueError("target must be one of: unit, lint, security") from exc
    request = await service().run(selected, ref)
    return {"execution_id": request.execution_id, "status": "queued", "ref": ref}


@mcp.tool()
async def container_status(execution_id: str, ref: str = "main") -> dict[str, str]:
    """Get the current GitHub Actions execution status."""
    status = await service().status(execution_id, ref)
    return {"execution_id": execution_id, "status": status.value}


@mcp.tool()
async def container_cancel(execution_id: str, ref: str = "main") -> dict[str, str]:
    """Cancel a running GitHub Actions execution."""
    await service().cancel(execution_id, ref)
    return {"execution_id": execution_id, "status": "cancellation_requested"}


def main() -> None:
    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    mcp.run(transport=transport)


if __name__ == "__main__":
    main()
