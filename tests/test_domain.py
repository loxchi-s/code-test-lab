import pytest

from container_execution.domain.execution import (
    ExecutionRequest,
    ExecutionStatus,
    ExecutionTarget,
)
from container_execution.domain.execution_result import ExecutionResult


def test_execution_request_accepts_valid_values() -> None:
    request = ExecutionRequest("a" * 32, ExecutionTarget.UNIT, "main")
    assert request.target is ExecutionTarget.UNIT


@pytest.mark.parametrize("ref", ["-bad", "../main", "feature/../main", "bad ref"])
def test_execution_request_rejects_unsafe_refs(ref: str) -> None:
    with pytest.raises(ValueError):
        ExecutionRequest("a" * 32, ExecutionTarget.UNIT, ref)


def test_status_values_are_stable() -> None:
    assert ExecutionStatus.COMPLETED.value == "completed"


def test_execution_result_accepts_valid_output() -> None:
    result = ExecutionResult(
        "b" * 32,
        ExecutionStatus.COMPLETED,
        "feature/test",
        exit_code=0,
        stdout="2 passed",
        stderr="",
        commit_sha="a" * 40,
    )
    assert result.exit_code == 0
    assert result.source_ref == "feature/test"


@pytest.mark.parametrize("exit_code", [True, -1, 256, "0"])
def test_execution_result_rejects_invalid_exit_code(exit_code) -> None:
    with pytest.raises(ValueError, match="exit_code"):
        ExecutionResult("b" * 32, ExecutionStatus.FAILED, "main", exit_code=exit_code)
