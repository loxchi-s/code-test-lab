"""Static architecture checks for package dependency direction and cycles."""

from __future__ import annotations

import ast
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[2] / "src" / "code_test_lab"

# The policy and Docker adapters are lower-level modules. CLI orchestration may depend on them,
# while the compatibility facade may re-export the public entry point.
FORBIDDEN_DEPENDENCIES = {
    "policy": {"cli", "docker", "isolation"},
    "docker": {"cli", "policy", "isolation"},
    "cli": {"isolation"},
}


def _internal_imports(path: Path) -> set[str]:
    """Return local code_test_lab module dependencies declared by one Python module."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module:
            imported = node.module.split(".", maxsplit=1)[0]
            if (PACKAGE_ROOT / f"{imported}.py").is_file():
                imports.add(imported)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("code_test_lab."):
                    imported = alias.name.removeprefix("code_test_lab.").split(".", maxsplit=1)[0]
                    if (PACKAGE_ROOT / f"{imported}.py").is_file():
                        imports.add(imported)
    return imports


def test_dependency_direction_respects_module_boundaries() -> None:
    """Core policy and Docker modules must not depend on orchestration or facade modules."""
    violations: list[str] = []
    for module, forbidden in FORBIDDEN_DEPENDENCIES.items():
        path = PACKAGE_ROOT / f"{module}.py"
        if not path.is_file():
            continue
        actual = _internal_imports(path)
        violations.extend(
            f"{module} must not import {dependency}"
            for dependency in sorted(actual & forbidden)
        )
    assert not violations, "\n".join(violations)


def test_package_has_no_circular_module_dependencies() -> None:
    """Reject direct or transitive cycles between top-level package modules."""
    graph = {
        path.stem: _internal_imports(path)
        for path in sorted(PACKAGE_ROOT.glob("*.py"))
        if path.name != "__init__.py"
    }
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(module: str, chain: tuple[str, ...]) -> None:
        if module in visiting:
            cycle = " -> ".join((*chain, module))
            raise AssertionError(f"Circular package dependency detected: {cycle}")
        if module in visited:
            return
        visiting.add(module)
        for dependency in sorted(graph.get(module, set()) & graph.keys()):
            visit(dependency, (*chain, module))
        visiting.remove(module)
        visited.add(module)

    for module in sorted(graph):
        visit(module, ())
