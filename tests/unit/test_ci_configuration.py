"""Requirement-traceable checks for GitHub Actions database verification (#115)."""

import tomllib
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def test_database_verify_job_is_self_contained_and_exercises_state_integration() -> None:
    """NFR-016: Linux CI migrates an ephemeral PostgreSQL state store."""

    workflow = (REPOSITORY_ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )

    database_job = workflow.split("  database-verify:\n", maxsplit=1)[1].split(
        "\n  verify:\n", maxsplit=1
    )[0]

    assert "runs-on: ubuntu-latest" in database_job
    assert "image: postgres:16" in database_job
    assert "POSTGRES_HOST_AUTH_METHOD: trust" in database_job
    assert "ESL_DATABASE_URL: postgresql+psycopg://esl_ci@localhost:5432/esl_ci" in database_job
    assert "ESL_TEST_DATABASE_URL: postgresql+psycopg://esl_ci@localhost:5432/esl_ci" in database_job
    assert "python -m alembic upgrade head" in database_job
    assert "python -m pytest -v tests/integration tests/unit/persistence/test_migration_graph.py" in database_job
    assert "POSTGRES_PASSWORD" not in database_job
    assert "secrets." not in database_job


def test_pywin32_is_installed_only_on_windows() -> None:
    """NFR-016: the Linux CI job can install the project dependencies."""

    project = tomllib.loads(
        (REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )

    assert 'pywin32; sys_platform == "win32"' in project["project"]["dependencies"]
