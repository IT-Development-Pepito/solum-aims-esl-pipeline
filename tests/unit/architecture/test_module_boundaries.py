"""Module boundary enforcement (FR-018, NFR-010).

AIMS is a vendor-owned external boundary (AD-002, AD-003). Domain and
application code must therefore depend on adapter *interfaces* only, never on
a transport library or a concrete adapter, so a vendor change cannot reach
business rules and rules stay testable without live SOLUM (NFR-011).

These tests read the import graph from source rather than importing modules,
so a violation is reported even when the offending module would fail to
import for another reason.
"""

import ast
from pathlib import Path

import pytest

SOURCE_ROOT = Path(__file__).resolve().parents[3] / "src" / "esl_service"

#: Transport and persistence libraries that belong only in an adapter.
INFRASTRUCTURE_LIBRARIES = (
    "sqlalchemy",
    "psycopg",
    "pyodbc",
    "httpx",
    "alembic",
    "pydantic",
    "pydantic_settings",
    "win32crypt",
    "win32security",
    "win32service",
)

#: Internal packages the pure domain must not reach into.
NON_DOMAIN_PACKAGES = (
    "esl_service.persistence",
    "esl_service.adapters",
    "esl_service.web",
    "esl_service.runtime",
    "esl_service.config",
)


def _module_name(path: Path) -> str:
    """Return the dotted module name for a source file."""

    relative = path.relative_to(SOURCE_ROOT.parent).with_suffix("")
    parts = [part for part in relative.parts if part != "__init__"]
    return ".".join(parts)


def _imports(path: Path) -> set[str]:
    """Return every module name imported by one source file."""

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
    return found


def _modules_under(package: str) -> list[Path]:
    """Return the source files of one internal package."""

    directory = SOURCE_ROOT / package
    return sorted(directory.rglob("*.py")) if directory.is_dir() else []


def _violations(paths: list[Path], forbidden: tuple[str, ...]) -> list[str]:
    """Return 'module -> import' strings for every forbidden dependency."""

    found: list[str] = []
    for path in paths:
        for imported in sorted(_imports(path)):
            if any(
                imported == item or imported.startswith(f"{item}.")
                for item in forbidden
            ):
                found.append(f"{_module_name(path)} -> {imported}")
    return found


# --- the domain is pure (FR-005, FR-018, NFR-010, NFR-011) ------------------


def test_domain_uses_no_infrastructure_library() -> None:
    """Business rules must run without a database, HTTP client, or Windows API."""

    assert _violations(_modules_under("domain"), INFRASTRUCTURE_LIBRARIES) == []


def test_domain_reaches_into_no_other_internal_package() -> None:
    """The domain depends on itself only, so rules stay independently testable."""

    assert _violations(_modules_under("domain"), NON_DOMAIN_PACKAGES) == []


# --- application ports stay free of transport (FR-018) ----------------------


def test_application_uses_no_transport_library() -> None:
    """Ports describe outcomes; they never speak a vendor protocol."""

    assert (
        _violations(
            _modules_under("application"),
            ("sqlalchemy", "psycopg", "pyodbc", "httpx", "alembic"),
        )
        == []
    )


def test_application_never_imports_a_concrete_adapter() -> None:
    """Orchestration depends on the interface, never on an implementation."""

    forbidden = ("esl_service.adapters", "esl_service.persistence", "esl_service.web")
    assert _violations(_modules_under("application"), forbidden) == []


# --- AIMS transport is confined to adapters (FR-018, AD-002) ----------------


@pytest.mark.parametrize("library", ["httpx", "pyodbc"])
def test_aims_and_sql_transport_appear_only_in_adapters(library: str) -> None:
    """Only an adapter may speak to AIMS or SQL Server directly."""

    offenders = [
        f"{_module_name(path)} -> {library}"
        for path in sorted(SOURCE_ROOT.rglob("*.py"))
        if "adapters" not in path.parts
        for imported in _imports(path)
        if imported == library or imported.startswith(f"{library}.")
    ]
    assert offenders == []


def test_no_module_writes_to_an_aims_database() -> None:
    """Direct AIMS database writes are forbidden (AD-002, FR-020)."""

    banned = ("aims_write", "AimsWriter", "aims_mutation_session")
    offenders = [
        _module_name(path)
        for path in sorted(SOURCE_ROOT.rglob("*.py"))
        for term in banned
        if term in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


# --- the boundary is discoverable ------------------------------------------


def test_every_internal_package_is_covered_by_a_rule() -> None:
    """A new top-level package must be classified, not silently unchecked."""

    known = {
        "domain",
        "application",
        "adapters",
        "persistence",
        "runtime",
        "web",
    }
    packages = {
        path.name
        for path in SOURCE_ROOT.iterdir()
        if path.is_dir() and not path.name.startswith(("_", "."))
    }
    assert packages <= known, f"unclassified package: {sorted(packages - known)}"
