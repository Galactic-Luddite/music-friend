"""Static dependency-direction checks for the Music Friend package."""

from __future__ import annotations

import ast
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

ALLOWED_INTERNAL_IMPORTS = {
    "music_friend.errors": frozenset(),
    "music_friend.domain": frozenset(),
    "music_friend.providers": frozenset({"music_friend.domain", "music_friend.errors"}),
    "music_friend.store": frozenset({"music_friend.domain", "music_friend.errors"}),
    "music_friend.tools": frozenset(
        {
            "music_friend.domain",
            "music_friend.errors",
            "music_friend.providers",
            "music_friend.store",
        }
    ),
    "music_friend.mcp": frozenset(
        {
            "music_friend.domain",
            "music_friend.errors",
            "music_friend.providers",
            "music_friend.store",
            "music_friend.tools",
        }
    ),
    "music_friend.runtimes": frozenset(
        {
            "music_friend.domain",
            "music_friend.mcp",
            "music_friend.providers",
            "music_friend.store",
            "music_friend.tools",
        }
    ),
}

HERE = Path(__file__).parent
REPOSITORY_ROOT = HERE.parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"


@dataclass(frozen=True, slots=True)
class Violation:
    importing_file: str
    imported_module: str
    edge: str


def find_violations(
    path: Path,
    module_name: str,
    *,
    scan_root: Path = REPOSITORY_ROOT,
) -> tuple[Violation, ...]:
    """Return dependency violations without importing the parsed module."""
    importing_file = _display_path(path, scan_root)
    importing_layer = _layer_for(module_name)
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=importing_file)
    except SyntaxError as error:
        imported_module = "<syntax-error>"
        importing_node = importing_layer or module_name
        return (
            Violation(
                importing_file,
                imported_module,
                f"{importing_node} -> {imported_module} (line {error.lineno})",
            ),
        )
    if importing_layer is None:
        return ()

    violations: list[Violation] = []
    for imported_module in _internal_imports(tree, module_name, path.name == "__init__.py"):
        imported_layer = _layer_for(imported_module)
        if imported_layer is None or imported_layer == importing_layer:
            continue
        if imported_layer not in ALLOWED_INTERNAL_IMPORTS[importing_layer]:
            violations.append(
                Violation(
                    importing_file,
                    imported_module,
                    f"{importing_layer} -> {imported_layer}",
                )
            )
    return tuple(violations)


def _display_path(path: Path, scan_root: Path) -> str:
    try:
        return path.relative_to(scan_root).as_posix()
    except ValueError as error:
        raise ValueError("path must be within scan_root") from error


def _layer_for(module_name: str) -> str | None:
    for layer in ALLOWED_INTERNAL_IMPORTS:
        if module_name == layer or module_name.startswith(f"{layer}."):
            return layer
    return None


def _internal_imports(tree: ast.AST, module_name: str, is_package: bool) -> tuple[str, ...]:
    imported: list[str] = []
    package = module_name if is_package else module_name.rpartition(".")[0]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names if _is_internal(alias.name))
        elif isinstance(node, ast.ImportFrom):
            base = _resolve_from_base(package, node.level, node.module)
            if _layer_for(base) is not None:
                imported.append(base)
            elif base == "music_friend":
                imported.extend(
                    candidate
                    for alias in node.names
                    if _is_internal(candidate := f"{base}.{alias.name}")
                )
    return tuple(imported)


def _resolve_from_base(package: str, level: int, imported_module: str | None) -> str:
    if level == 0:
        return imported_module or ""
    parts = package.split(".") if package else []
    ascents = level - 1
    if ascents >= len(parts):
        base = ""
    else:
        base = ".".join(parts[: len(parts) - ascents])
    if imported_module:
        return f"{base}.{imported_module}" if base else imported_module
    return base


def _is_internal(module_name: str) -> bool:
    return module_name == "music_friend" or module_name.startswith("music_friend.")


def _tracked_source_paths() -> tuple[Path, ...]:
    result = subprocess.run(
        ["git", "ls-files", "-z", "--", "src/music_friend"],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
    )
    relative_paths = (
        Path(raw_path.decode("utf-8")) for raw_path in result.stdout.split(b"\0") if raw_path
    )
    return tuple(REPOSITORY_ROOT / path for path in relative_paths if path.suffix == ".py")


FORBIDDEN_CASES = (
    (
        "domain_imports_provider.py.txt",
        "music_friend.domain.fixture",
        "music_friend.providers",
        "music_friend.domain -> music_friend.providers",
    ),
    (
        "domain_imports_store.py.txt",
        "music_friend.domain.fixture",
        "music_friend.store.portable",
        "music_friend.domain -> music_friend.store",
    ),
    (
        "provider_imports_mcp.py.txt",
        "music_friend.providers.fixture",
        "music_friend.mcp",
        "music_friend.providers -> music_friend.mcp",
    ),
    (
        "store_imports_runtime.py.txt",
        "music_friend.store.fixture",
        "music_friend.runtimes",
        "music_friend.store -> music_friend.runtimes",
    ),
    (
        "mcp_imports_runtime.py.txt",
        "music_friend.mcp.fixture",
        "music_friend.runtimes",
        "music_friend.mcp -> music_friend.runtimes",
    ),
)

ARCHITECTURE_LAYERS = (
    "music_friend.errors",
    "music_friend.domain",
    "music_friend.providers",
    "music_friend.store",
    "music_friend.tools",
    "music_friend.mcp",
    "music_friend.runtimes",
)
EXPECTED_ALLOWED_INTER_LAYER_EDGES = frozenset(
    {
        ("music_friend.providers", "music_friend.domain"),
        ("music_friend.providers", "music_friend.errors"),
        ("music_friend.store", "music_friend.domain"),
        ("music_friend.store", "music_friend.errors"),
        ("music_friend.tools", "music_friend.domain"),
        ("music_friend.tools", "music_friend.errors"),
        ("music_friend.tools", "music_friend.providers"),
        ("music_friend.tools", "music_friend.store"),
        ("music_friend.mcp", "music_friend.domain"),
        ("music_friend.mcp", "music_friend.errors"),
        ("music_friend.mcp", "music_friend.providers"),
        ("music_friend.mcp", "music_friend.store"),
        ("music_friend.mcp", "music_friend.tools"),
        ("music_friend.runtimes", "music_friend.mcp"),
        ("music_friend.runtimes", "music_friend.providers"),
        ("music_friend.runtimes", "music_friend.domain"),
        ("music_friend.runtimes", "music_friend.store"),
        ("music_friend.runtimes", "music_friend.tools"),
    }
)
INTER_LAYER_CASES = tuple(
    (importing_layer, imported_layer)
    for importing_layer in ARCHITECTURE_LAYERS
    for imported_layer in ARCHITECTURE_LAYERS
    if importing_layer != imported_layer
)


def test_declared_graph_has_exactly_the_seven_approved_layers() -> None:
    assert tuple(ALLOWED_INTERNAL_IMPORTS) == ARCHITECTURE_LAYERS


@pytest.mark.parametrize(("filename", "module_name", "imported_module", "edge"), FORBIDDEN_CASES)
def test_forbidden_fixture_reports_exactly_its_intended_edge(
    filename: str,
    module_name: str,
    imported_module: str,
    edge: str,
) -> None:
    path = HERE / "fixtures" / "forbidden" / filename

    assert find_violations(path, module_name) == (
        Violation(
            f"tests/architecture/fixtures/forbidden/{filename}",
            imported_module,
            edge,
        ),
    )


@pytest.mark.parametrize(
    ("importing_layer", "imported_layer"),
    INTER_LAYER_CASES,
    ids=lambda layer: layer.removeprefix("music_friend."),
)
def test_every_ordered_inter_layer_edge_matches_independent_oracle(
    tmp_path: Path,
    importing_layer: str,
    imported_layer: str,
) -> None:
    path = tmp_path / "candidate.py"
    path.write_text(f"import {imported_layer}.candidate\n", encoding="utf-8")
    expected: tuple[Violation, ...] = ()
    if (importing_layer, imported_layer) not in EXPECTED_ALLOWED_INTER_LAYER_EDGES:
        expected = (
            Violation(
                "candidate.py",
                f"{imported_layer}.candidate",
                f"{importing_layer} -> {imported_layer}",
            ),
        )

    assert find_violations(path, f"{importing_layer}.candidate", scan_root=tmp_path) == expected


def test_tools_cannot_import_mcp(tmp_path: Path) -> None:
    path = tmp_path / "tools_module.py"
    path.write_text("import music_friend.mcp.server\n", encoding="utf-8")

    assert find_violations(path, "music_friend.tools.module", scan_root=tmp_path) == (
        Violation(
            "tools_module.py",
            "music_friend.mcp.server",
            "music_friend.tools -> music_friend.mcp",
        ),
    )


def test_mcp_can_import_tools(tmp_path: Path) -> None:
    path = tmp_path / "mcp_module.py"
    path.write_text("import music_friend.tools.catalog\n", encoding="utf-8")

    assert find_violations(path, "music_friend.mcp.module", scan_root=tmp_path) == ()


def test_allowed_fixture_has_no_dependency_violations() -> None:
    path = HERE / "fixtures" / "allowed" / "domain_ok.py.txt"

    assert find_violations(path, "music_friend.domain.fixture") == ()


def test_syntax_error_fails_closed_with_file_module_and_edge(tmp_path: Path) -> None:
    path = tmp_path / "broken.py"
    path.write_text("from music_friend.domain import (\n", encoding="utf-8")

    assert find_violations(path, "music_friend.providers.broken", scan_root=tmp_path) == (
        Violation(
            "broken.py",
            "<syntax-error>",
            "music_friend.providers -> <syntax-error> (line 1)",
        ),
    )


def test_root_package_syntax_error_also_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "__init__.py"
    path.write_text("def broken(\n", encoding="utf-8")

    assert find_violations(path, "music_friend", scan_root=tmp_path) == (
        Violation(
            "__init__.py",
            "<syntax-error>",
            "music_friend -> <syntax-error> (line 1)",
        ),
    )


def test_package_init_resolves_relative_import_from_the_package(tmp_path: Path) -> None:
    path = tmp_path / "__init__.py"
    path.write_text("from .. import providers\n", encoding="utf-8")

    assert find_violations(path, "music_friend.domain", scan_root=tmp_path) == (
        Violation(
            "__init__.py",
            "music_friend.providers",
            "music_friend.domain -> music_friend.providers",
        ),
    )


def test_absent_future_layer_is_checked_without_resolving_it(tmp_path: Path) -> None:
    path = tmp_path / "local.py"
    path.write_text("from music_friend.mcp import serve\n", encoding="utf-8")

    assert find_violations(path, "music_friend.runtimes.local", scan_root=tmp_path) == ()


def test_duplicate_package_basenames_have_distinct_syntax_diagnostics(
    tmp_path: Path,
) -> None:
    first = tmp_path / "domain" / "__init__.py"
    second = tmp_path / "providers" / "__init__.py"
    first.parent.mkdir()
    second.parent.mkdir()
    first.write_text("def broken(\n", encoding="utf-8")
    second.write_text("def broken(\n", encoding="utf-8")

    assert find_violations(first, "music_friend.domain", scan_root=tmp_path) == (
        Violation(
            "domain/__init__.py",
            "<syntax-error>",
            "music_friend.domain -> <syntax-error> (line 1)",
        ),
    )
    assert find_violations(second, "music_friend.providers", scan_root=tmp_path) == (
        Violation(
            "providers/__init__.py",
            "<syntax-error>",
            "music_friend.providers -> <syntax-error> (line 1)",
        ),
    )


def test_scanning_source_does_not_import_project_modules() -> None:
    before = frozenset(
        name for name in sys.modules if name == "music_friend" or name.startswith("music_friend.")
    )

    for path in _tracked_source_paths():
        find_violations(path, _module_name_for_source(path))

    after = frozenset(
        name for name in sys.modules if name == "music_friend" or name.startswith("music_friend.")
    )
    assert after == before


def test_real_source_tree_obeys_dependency_direction() -> None:
    paths = _tracked_source_paths()
    relative_paths = {path.relative_to(SOURCE_ROOT).as_posix() for path in paths}
    assert {
        "music_friend/domain/models.py",
        "music_friend/providers/base.py",
        "music_friend/store/catalog.py",
        "music_friend/mcp/read_server.py",
    }.issubset(relative_paths)

    violations = tuple(
        violation
        for path in paths
        for violation in find_violations(path, _module_name_for_source(path))
    )
    assert violations == ()


def _module_name_for_source(path: Path) -> str:
    relative = path.relative_to(SOURCE_ROOT).with_suffix("")
    parts = relative.parts
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)
