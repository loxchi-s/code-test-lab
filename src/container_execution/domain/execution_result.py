from __future__ import annotations

import re
from dataclasses import dataclass

from container_execution.domain.execution import ExecutionStatus

_REF_PATTERN = re.compile(r"[A-Za-z0-9._/-]{1,128}")


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    execution_id: str
    status: ExecutionStatus
    source_ref: str
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    commit_sha: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.execution_id, str):
            raise TypeError("execution_id must be a string")
        if not re.fullmatch(r"[a-f0-9]{32}", self.execution_id):
            raise ValueError("execution_id must be a 32-character lowercase hex UUID")
        if not isinstance(self.status, ExecutionStatus):
            raise TypeError("status must be an ExecutionStatus")
        if not isinstance(self.source_ref, str):
            raise TypeError("source_ref must be a string")
        if (
            not _REF_PATTERN.fullmatch(self.source_ref)
            or self.source_ref.startswith("-")
            or ".." in self.source_ref
            or self.source_ref.endswith("/")
        ):
            raise ValueError("source_ref is not a valid safe Git ref")
        if self.exit_code is not None:
            if isinstance(self.exit_code, bool) or not isinstance(self.exit_code, int):
                raise TypeError("exit_code must be an integer")
            if not 0 <= self.exit_code <= 255:
                raise ValueError("exit_code must be between 0 and 255")
        if not isinstance(self.stdout, str) or not isinstance(self.stderr, str):
            raise TypeError("stdout and stderr must be strings")
        if not isinstance(self.commit_sha, str):
            raise TypeError("commit_sha must be a string")
        if self.commit_sha and not re.fullmatch(r"[a-f0-9]{40}", self.commit_sha):
            raise ValueError("commit_sha must be a 40-character lowercase hexadecimal SHA")
