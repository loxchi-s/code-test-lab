from __future__ import annotations

from typing import Protocol

from container_execution.domain.execution import ExecutionRequest, ExecutionStatus


class ExecutionRuntime(Protocol):
    async def dispatch(self, request: ExecutionRequest) -> None: ...
    async def status(self, execution_id: str, ref: str) -> ExecutionStatus: ...
    async def cancel(self, execution_id: str, ref: str) -> None: ...
