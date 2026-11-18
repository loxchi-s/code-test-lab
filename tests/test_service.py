from container_execution.application.service import ContainerExecutionService
from container_execution.domain.execution import ExecutionStatus, ExecutionTarget


class FakeRuntime:
    def __init__(self) -> None:
        self.dispatched = None

    async def dispatch(self, request) -> None:
        self.dispatched = request

    async def status(self, execution_id: str, ref: str) -> ExecutionStatus:
        return ExecutionStatus.COMPLETED

    async def cancel(self, execution_id: str, ref: str) -> None:
        return None


async def test_service_dispatches_without_network_or_docker() -> None:
    runtime = FakeRuntime()
    service = ContainerExecutionService(runtime)
    request = await service.run(ExecutionTarget.UNIT, "main")
    assert runtime.dispatched == request
    assert len(request.execution_id) == 32
    assert await service.status(request.execution_id) is ExecutionStatus.COMPLETED
