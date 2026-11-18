from __future__ import annotations

from uuid import uuid4

from container_execution.domain.execution import (
    ExecutionRequest,
    ExecutionStatus,
    ExecutionTarget,
)
from container_execution.domain.execution_result import ExecutionResult
from container_execution.ports.execution_runtime import ExecutionRuntime


class ContainerExecutionService:
    def __init__(self, runtime: ExecutionRuntime) -> None:
        self._runtime = runtime

    async def run(self, target: ExecutionTarget, ref: str = "main") -> ExecutionRequest:
        request = ExecutionRequest(uuid4().hex, target, ref)
        await self._runtime.dispatch(request)
        return request

    async def status(self, execution_id: str, ref: str = "main") -> ExecutionStatus:
        return await self._runtime.status(execution_id, ref)

    async def result(self, execution_id: str, ref: str = "main") -> ExecutionResult | None:
        return await self._runtime.result(execution_id, ref)

    async def cancel(self, execution_id: str, ref: str = "main") -> None:
        await self._runtime.cancel(execution_id, ref)
