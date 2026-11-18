"""Load and validate the repository's container execution policy."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import yaml
from yaml.tokens import (
    BlockEndToken,
    BlockMappingStartToken,
    BlockSequenceStartToken,
    FlowMappingEndToken,
    FlowMappingStartToken,
    FlowSequenceEndToken,
    FlowSequenceStartToken,
)

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
_MAX_POLICY_BYTES = 64 * 1024
_MAX_YAML_NODES = 10_000
_MAX_YAML_DEPTH = 32


class PolicyError(ValueError):
    """Raised when the isolation policy is invalid or unsafe."""


class _UniqueKeySafeLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects duplicate mapping keys."""

    def _validate_expansion_budget(self, node: yaml.Node) -> None:
        """Bound alias/merge expansion work before PyYAML copies merged entries."""
        active: set[int] = set()
        node_visits = 0
        mapping_entries = 0

        def visit(current: yaml.Node, depth: int) -> None:
            nonlocal node_visits, mapping_entries
            node_visits += 1
            if node_visits > _MAX_YAML_NODES or depth > _MAX_YAML_DEPTH:
                raise PolicyError("Policy YAML exceeds the structural expansion limits")
            identity = id(current)
            if identity in active:
                raise PolicyError("Policy YAML contains a recursive alias")
            active.add(identity)
            try:
                if isinstance(current, yaml.MappingNode):
                    mapping_entries += len(current.value)
                    if mapping_entries > _MAX_YAML_NODES:
                        raise PolicyError("Policy YAML exceeds the mapping expansion budget")
                    for key_node, value_node in current.value:
                        visit(key_node, depth + 1)
                        visit(value_node, depth + 1)
                elif isinstance(current, yaml.SequenceNode):
                    for child in current.value:
                        visit(child, depth + 1)
            finally:
                active.remove(identity)

        visit(node, 0)

    def _validate_mapping_tree(
        self, node: yaml.Node, visited: set[int] | None = None,
    ) -> None:
        """Reject duplicate keys in every source mapping, including merge operands."""
        if visited is None:
            visited = set()
        if id(node) in visited:
            return
        visited.add(id(node))

        if isinstance(node, yaml.MappingNode):
            direct_keys: set[Any] = set()
            seen_merge = False
            for key_node, value_node in node.value:
                if key_node.tag == "tag:yaml.org,2002:merge":
                    if seen_merge:
                        raise yaml.constructor.ConstructorError(
                            "while constructing a mapping", node.start_mark,
                            "found duplicate merge key '<<'", key_node.start_mark,
                        )
                    seen_merge = True
                    merge_nodes = (
                        value_node.value
                        if isinstance(value_node, yaml.SequenceNode)
                        else [value_node]
                    )
                    for merge_node in merge_nodes:
                        self._validate_mapping_tree(merge_node, visited)
                else:
                    key = self.construct_object(key_node, deep=True)
                    try:
                        duplicate = key in direct_keys
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
                    direct_keys.add(key)
                self._validate_mapping_tree(value_node, visited)
        elif isinstance(node, yaml.SequenceNode):
            for child in node.value:
                self._validate_mapping_tree(child, visited)

    def construct_document(self, node: yaml.Node) -> Any:
        """Validate the untouched source graph before PyYAML flattens merge nodes."""
        self._validate_expansion_budget(node)
        self._validate_mapping_tree(node)
        return super().construct_document(node)

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
        self.flatten_mapping(node)
        return super().construct_mapping(node, deep=deep)


def _validate_yaml_token_depth(policy_text: str) -> None:
    """Reject excessive collection nesting before PyYAML composes recursive nodes."""
    collection_starts = (
        BlockMappingStartToken,
        BlockSequenceStartToken,
        FlowMappingStartToken,
        FlowSequenceStartToken,
    )
    collection_ends = (BlockEndToken, FlowMappingEndToken, FlowSequenceEndToken)
    depth = 0
    for token in yaml.scan(policy_text):
        if isinstance(token, collection_starts):
            depth += 1
            if depth > _MAX_YAML_DEPTH:
                raise PolicyError("Policy YAML exceeds the structural expansion limits")
        elif isinstance(token, collection_ends):
            depth -= 1
            if depth < 0:
                raise PolicyError("Policy YAML contains invalid collection structure")


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
        with path.open("rb") as stream:
            policy_bytes = stream.read(_MAX_POLICY_BYTES + 1)
        if len(policy_bytes) > _MAX_POLICY_BYTES:
            raise PolicyError(f"Policy file exceeds {_MAX_POLICY_BYTES} bytes")
        try:
            policy_text = policy_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PolicyError("Policy file must be valid UTF-8") from exc
        _validate_yaml_token_depth(policy_text)
        document = yaml.load(  # nosec B506 - custom loader inherits SafeLoader and rejects duplicate keys.
            policy_text,
            Loader=_UniqueKeySafeLoader,
        )
    except PolicyError:
        raise
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
    if isinstance(cpus, bool) or not isinstance(cpus, (int, float)):
        raise PolicyError(
            f"Execution setting 'cpus' must be numeric and in range [0.01, {_MAX_CPUS}]"
        )
    try:
        cpu_value = Decimal(str(cpus))
    except (InvalidOperation, ValueError) as exc:
        raise PolicyError("Execution setting 'cpus' must be a finite decimal number") from exc
    if not cpu_value.is_finite():
        raise PolicyError("Execution setting 'cpus' must be a finite decimal number")
    nano_cpus = cpu_value * Decimal(1_000_000_000)
    if (
        cpu_value < Decimal("0.01")
        or cpu_value > Decimal(_MAX_CPUS)
        or nano_cpus != nano_cpus.to_integral_value()
    ):
        raise PolicyError(
            f"Execution setting 'cpus' must be in range [0.01, {_MAX_CPUS}] "
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
