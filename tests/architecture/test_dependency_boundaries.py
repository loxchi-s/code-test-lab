"""Static architecture checks for package dependency direction and cycles."""

from __future__ import annotations

import ast
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[2] / "src" / "code_test_lab"
PACKAGE_NAME = "code_test_lab"

# Policy and Docker are lower-level modules; CLI orchestration may depend on them.
FORBIDDEN_DEPENDENCIES = {
    "policy": {"cli", "docker", "isolation"},
    "docker": {"cli", "policy", "isolation"},
    "cli": {"isolation"},
}


def _local_module(module: str) -> str | None:
    """Resolve a package-qualified import to a top-level local module."""
    prefix = f"{PACKAGE_NAME}."
    if module.startswith(prefix):
        candidate = module[len(prefix):].split(".", maxsplit=1)[0]
        return candidate if (PACKAGE_ROOT / f"{candidate}.py").is_file() else None
    candidate = module.split(".", maxsplit=1)[0]
    return candidate if (PACKAGE_ROOT / f"{candidate}.py").is_file() else None


def _internal_imports(path: Path) -> set[str]:
    """Return local package dependencies, including absolute and relative imports."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                if node.module == PACKAGE_NAME:
                    for alias in node.names:
                        imported = _local_module(alias.name)
                        if imported:
                            imports.add(imported)
                else:
                    imported = _local_module(node.module)
                    if imported:
                        imports.add(imported)
            elif node.level == 1:
                if node.module:
                    imported = _local_module(node.module)
                    if imported:
                        imports.add(imported)
                else:
                    for alias in node.names:
                        imported = _local_module(alias.name)
                        if imported:
                            imports.add(imported)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imported = _local_module(alias.name)
                if imported:
                    imports.add(imported)
    return imports


def test_dependency_direction_respects_module_boundaries() -> None:
    """Core policy and Docker modules must not depend on orchestration or facade modules."""
    violations: list[str] = []
    for module, forbidden in FORBIDDEN_DEPENDENCIES.items():
        path = PACKAGE_ROOT / f"{module}.py"
        if path.is_file():
            violations.extend(
                f"{module} must not import {dependency}"
                for dependency in sorted(_internal_imports(path) & forbidden)
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
            raise AssertionError(
                f"Circular package dependency detected: {' -> '.join((*chain, module))}"
            )
        if module in visited:
            return
        visiting.add(module)
        for dependency in sorted(graph.get(module, set()) & graph.keys()):
            visit(dependency, (*chain, module))
        visiting.remove(module)
        visited.add(module)

    for module in sorted(graph):
        visit(module, ())



def test_internal_imports_detects_package_root_aliases(tmp_path: Path) -> None:
    sample = tmp_path / "sample.py"
    sample.write_text(
        "from code_test_lab import docker\nfrom . import cli\n",
        encoding="utf-8",
    )
    assert _internal_imports(sample) == {"docker", "cli"}
