import pytest

from container_execution.domain.execution import (
    ExecutionRequest, ExecutionStatus, ExecutionTarget,
)


def test_execution_request_accepts_valid_values() -> None:
    request = ExecutionRequest("a" * 32, ExecutionTarget.UNIT, "main")
    assert request.target is ExecutionTarget.UNIT


@pytest.mark.parametrize("ref", ["-bad", "../main", "feature/../main", "bad ref"])
def test_execution_request_rejects_unsafe_refs(ref: str) -> None:
    with pytest.raises(ValueError):
        ExecutionRequest("a" * 32, ExecutionTarget.UNIT, ref)


def test_status_values_are_stable() -> None:
    assert ExecutionStatus.COMPLETED.value == "completed"
