"""The Alembic migration graph must always have exactly one head.

Two agents implementing different AD-016 tasks concurrently can each set
``down_revision`` to the current head, producing a forked graph. ``alembic
upgrade head`` then fails for everyone until a merge revision is authored.

The graph is read from the migration files alone, so this needs no database
connection, no ``ESL_TEST_DATABASE_URL``, and no PostgreSQL service.
"""

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
MIGRATIONS = REPOSITORY_ROOT / "alembic"


def resolve_heads(script_location: Path) -> tuple[str, ...]:
    """Return every head revision in a migration directory."""

    config = Config()
    config.set_main_option("script_location", str(script_location))
    return tuple(ScriptDirectory.from_config(config).get_heads())


def test_repository_migration_graph_has_exactly_one_head() -> None:
    """A forked migration graph blocks `alembic upgrade head` for every agent."""

    heads = resolve_heads(MIGRATIONS)
    assert len(heads) == 1, (
        "the migration graph has more than one head, so `alembic upgrade head` "
        f"cannot resolve: {', '.join(sorted(heads))}. Rebase the newer revision "
        "onto the current head instead of branching from a shared parent."
    )


def test_a_forked_graph_is_detected(tmp_path: Path) -> None:
    """The guard must actually fail a fork, not merely pass the current graph."""

    versions = tmp_path / "versions"
    versions.mkdir(parents=True)
    (tmp_path / "env.py").write_text("", encoding="utf-8")
    _write_revision(versions, revision="0001_base", down_revision=None)
    _write_revision(versions, revision="0002_left", down_revision="0001_base")
    _write_revision(versions, revision="0002_right", down_revision="0001_base")

    assert sorted(resolve_heads(tmp_path)) == ["0002_left", "0002_right"]


def _write_revision(
    versions: Path, *, revision: str, down_revision: str | None
) -> None:
    """Write a minimal revision file used only to build a test graph."""

    parent = "None" if down_revision is None else f'"{down_revision}"'
    versions.joinpath(f"{revision}.py").write_text(
        f'"""Test revision."""\n\n'
        f'revision = "{revision}"\n'
        f"down_revision = {parent}\n"
        f"branch_labels = None\n"
        f"depends_on = None\n\n\n"
        f"def upgrade() -> None:\n    pass\n\n\n"
        f"def downgrade() -> None:\n    pass\n",
        encoding="utf-8",
    )
