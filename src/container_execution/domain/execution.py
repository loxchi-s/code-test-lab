from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum


class ExecutionTarget(StrEnum):
    UNIT = "unit"
    LINT = "lint"
    SECURITY = "security"


class ExecutionStatus(StrEnum):
    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ExecutionRequest:
    execution_id: str
    target: ExecutionTarget
    ref: str

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-f0-9]{32}", self.execution_id):
            raise ValueError("execution_id must be a 32-character lowercase hex UUID")
        if not re.fullmatch(r"[A-Za-z0-9._/-]{1,128}", self.ref):
            raise ValueError("ref contains unsupported characters")
        if self.ref.startswith("-") or ".." in self.ref or self.ref.endswith("/"):
            raise ValueError("ref is not a valid safe Git ref")
