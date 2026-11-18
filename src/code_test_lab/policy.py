"""Load and validate the repository's container execution policy."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

_REQUIRED_EXECUTION_KEYS = {
    "network", "filesystem", "user", "memory", "cpus",
    "pids_limit", "tmpfs_size", "timeout_minutes",
    "drop_capabilities", "no_new_privileges",
}
_MAX_MEMORY_MIB = 4096
_MIN_DOCKER_MEMORY_MIB = 6
_MAX_TMPFS_MIB = 512
_MAX_CPUS = 4
_MAX_PIDS = 512
_MAX_TIMEOUT_MINUTES = 20
_MAX_UID_GID = 65535


class PolicyError(ValueError):
    """Raised when the isolation policy is invalid or unsafe."""


class _UniqueKeySafeLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects duplicate mapping keys."""

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
        self.flatten_mapping(node)
        mapping: dict[Any, Any] = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            try:
                duplicate = key in mapping
            except TypeError as exc:
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping", node.start_mark,
                    "found an unhashable key", key_node.start_mark,
                ) from exc
            if duplicate:
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping", node.start_mark,
                    f"found duplicate key {key!r}", key_node.start_mark,
                )
            mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping


def _size_in_mib(
    value: object, *, key: str, maximum: int, minimum: int = 1,
) -> int:
    """Parse a positive Docker size and enforce its lower and upper bounds."""
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]+[mMgG]", value):
        raise PolicyError(f"Execution setting {key!r} must be a positive MiB/GiB size")
    amount = int(value[:-1])
    multiplier = 1024 if value[-1].lower() == "g" else 1
    size_mib = amount * multiplier
    if size_mib < minimum or size_mib > maximum:
        raise PolicyError(
            f"Execution setting {key!r} must be in range {minimum}..{maximum} MiB"
        )
    return size_mib


def load_execution_policy(path: Path) -> dict[str, Any]:
    """Load and validate the policy without executing external commands."""
    try:
        document = yaml.load(  # nosec B506 - custom loader inherits SafeLoader and rejects duplicate keys.
            path.read_text(encoding="utf-8"),
            Loader=_UniqueKeySafeLoader,
        )
    except (OSError, yaml.YAMLError) as exc:
        raise PolicyError(f"Unable to load policy: {exc}") from exc

    if (
        not isinstance(document, dict)
        or isinstance(document.get("version"), bool)
        or document.get("version") != 1
    ):
        raise PolicyError("Policy must be a mapping with version: 1")

    execution = document.get("execution")
    if not isinstance(execution, dict):
        raise PolicyError("Policy execution section must be a mapping")
    missing = _REQUIRED_EXECUTION_KEYS - execution.keys()
    if missing:
        raise PolicyError(f"Policy is missing execution settings: {sorted(missing)}")
    if execution["network"] != "disabled":
        raise PolicyError("Only network: disabled is permitted")
    if execution["filesystem"] != "read-only":
        raise PolicyError("Only filesystem: read-only is permitted")

    user = execution["user"]
    if not isinstance(user, str) or not re.fullmatch(r"[0-9]+:[0-9]+", user):
        raise PolicyError("Execution user must be an ASCII numeric UID:GID pair")
    uid, gid = (int(part) for part in user.split(":"))
    if not 1 <= uid <= _MAX_UID_GID or not 1 <= gid <= _MAX_UID_GID:
        raise PolicyError("Execution UID and GID must be non-root values in range 1..65535")

    _size_in_mib(
        execution["memory"], key="memory",
        maximum=_MAX_MEMORY_MIB, minimum=_MIN_DOCKER_MEMORY_MIB,
    )
    _size_in_mib(execution["tmpfs_size"], key="tmpfs_size", maximum=_MAX_TMPFS_MIB)

    cpus = execution["cpus"]
    if (
        isinstance(cpus, bool)
        or not isinstance(cpus, (int, float))
        or not 0 < cpus <= _MAX_CPUS
        or not (float(cpus) * 1_000_000_000).is_integer()
    ):
        raise PolicyError(
            f"Execution setting 'cpus' must be in range (0, {_MAX_CPUS}] "
            "and representable as whole NanoCPUs"
        )

    for key, maximum in (
        ("pids_limit", _MAX_PIDS),
        ("timeout_minutes", _MAX_TIMEOUT_MINUTES),
    ):
        value = execution[key]
        if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= maximum:
            raise PolicyError(f"Execution setting {key!r} must be in range 1..{maximum}")
    if execution["drop_capabilities"] != "all":
        raise PolicyError("All Linux capabilities must be dropped")
    if execution["no_new_privileges"] is not True:
        raise PolicyError("no_new_privileges must be true")

    ci = document.get("ci")
    if not isinstance(ci, dict):
        raise PolicyError("Policy ci section must be a mapping")
    if ci.get("permissions") != {"contents": "read"}:
        raise PolicyError("CI permissions must be limited to contents: read")
    if ci.get("allow_pull_request_target") is not False:
        raise PolicyError("pull_request_target must be disabled")
    if ci.get("expose_repository_secrets_to_test_container") is not False:
        raise PolicyError("Repository secrets must not be exposed to test containers")
    return execution
