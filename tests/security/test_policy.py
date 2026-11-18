from container_execution.domain.execution import ExecutionTarget


def test_only_declared_execution_targets_exist() -> None:
    assert {item.value for item in ExecutionTarget} == {"unit", "lint", "security"}
