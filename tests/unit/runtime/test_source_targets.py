"""Connection targets are derived from settings, never from the environment (#78).

Three source tiers plus two AIMS databases, each named a fixed bundle key.
Per-store targets are the one case where a connection address comes from
table data rather than configuration, so they are validated before use.
"""

import pytest

from esl_service.config import Settings
from esl_service.runtime.connectivity import (
    AIMS_CORE_PASSWORD_KEY,
    AIMS_PORTAL_PASSWORD_KEY,
    SOURCE_SQL_PASSWORD_KEY,
    STATE_PASSWORD_KEY,
    InvalidStoreAddress,
    TargetKind,
    store_target,
    targets_from_settings,
)


def settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "environment": "development",
        "database_url": "postgresql+psycopg://esl_dev@localhost:5432/esl_pipeline_dev",
        "internal_host": "127.0.0.1",
        "source_sql_host": "sql.internal",
        "source_sql_username": "esl_reader",
        "source_pepito_ho_host": "ho.internal",
        "aims_host": "aims.internal",
        "aims_portal_database": "AIMS_PORTAL_DB",
        "aims_portal_username": "esl_aims_reader",
        "aims_core_database": "AIMS_CORE_DB",
        "aims_core_username": "esl_aims_reader",
    }
    values.update(overrides)
    return Settings.model_validate(values)


def by_name(configured: Settings) -> dict[str, object]:
    return {target.name: target for target in targets_from_settings(configured)}


# --- every tier is present, keyed to a fixed bundle name -------------------


def test_all_six_targets_are_derived_from_settings() -> None:
    names = set(by_name(settings()))

    assert names == {
        "state-store",
        "warehouse",
        "legacy-baseline",
        "pepito-ho",
        "aims-portal",
        "aims-core",
    }


def test_store_ops_app_is_deliberately_absent() -> None:
    """Its only use in the procedure is commented out; it is dead code."""

    assert not [name for name in by_name(settings()) if "ops" in name]


def test_warehouse_and_legacy_baseline_share_the_esl_instance() -> None:
    """DBWH_8555 is reached by three-part name, so it is on the same host."""

    targets = by_name(settings())
    warehouse = targets["warehouse"]
    baseline = targets["legacy-baseline"]

    assert warehouse.host == baseline.host == "sql.internal"  # type: ignore[attr-defined]
    assert warehouse.database == "DBWH_8555"  # type: ignore[attr-defined]
    assert baseline.database == "ESL"  # type: ignore[attr-defined]
    assert warehouse.kind is TargetKind.SQLSERVER  # type: ignore[attr-defined]


def test_sql_server_targets_share_one_credential_key() -> None:
    """One read-only account covers every SQL Server, confirmed by the owner."""

    targets = by_name(settings())

    for name in ("warehouse", "legacy-baseline", "pepito-ho"):
        assert targets[name].username == "esl_reader"  # type: ignore[attr-defined]
        assert targets[name].password_key == SOURCE_SQL_PASSWORD_KEY  # type: ignore[attr-defined]


def test_aims_targets_use_their_own_keys_and_the_shared_host() -> None:
    targets = by_name(settings(aims_port=5433))
    portal = targets["aims-portal"]
    core = targets["aims-core"]

    assert portal.kind is core.kind is TargetKind.POSTGRESQL  # type: ignore[attr-defined]
    assert portal.host == core.host == "aims.internal"  # type: ignore[attr-defined]
    assert portal.port == core.port == 5433  # type: ignore[attr-defined]
    assert portal.password_key == AIMS_PORTAL_PASSWORD_KEY  # type: ignore[attr-defined]
    assert core.password_key == AIMS_CORE_PASSWORD_KEY  # type: ignore[attr-defined]


def test_the_state_store_takes_its_password_from_the_bundle_not_the_url() -> None:
    """The one pre-existing exception to AD-007 is closed."""

    state = by_name(settings())["state-store"]

    assert state.password is None  # type: ignore[attr-defined]
    assert state.password_key == STATE_PASSWORD_KEY  # type: ignore[attr-defined]
    assert state.username == "esl_dev"  # type: ignore[attr-defined]


def test_an_unconfigured_tier_is_still_listed_so_the_report_shows_it() -> None:
    """Useful while access is being arranged: the gap is visible, not silent."""

    targets = by_name(settings(source_pepito_ho_host=""))

    assert "pepito-ho" in targets
    assert targets["pepito-ho"].configured() is False  # type: ignore[attr-defined]


# --- per-store addresses come from DimStore and are validated -------------


@pytest.mark.parametrize("address", ["10.20.30.41", "192.168.85.18", "store-075.internal"])
def test_a_plain_ip_or_hostname_is_accepted(address: str) -> None:
    target = store_target(settings(), store_code="075", org_ip=address, org_db="STORE075")

    assert target.host == address
    assert target.database == "STORE075"
    assert target.name == "store-075"
    assert target.password_key == SOURCE_SQL_PASSWORD_KEY
    assert target.username == "esl_reader"


@pytest.mark.parametrize(
    "address",
    ["", "  ", "10.20.30.41;DROP TABLE x", "10.20.30.41,1433", "host name", "tcp://x", "a/b", "x@y"],
)
def test_anything_that_is_not_an_address_is_refused(address: str) -> None:
    """The value originates in a table row, so it is never trusted as-is."""

    with pytest.raises(InvalidStoreAddress):
        store_target(settings(), store_code="075", org_ip=address, org_db="STORE075")


def test_a_store_database_name_must_be_a_plain_identifier() -> None:
    with pytest.raises(InvalidStoreAddress, match="database"):
        store_target(settings(), store_code="075", org_ip="10.0.0.1", org_db="db;x")


def test_a_store_target_carries_no_port() -> None:
    """DimStore.ORG_IP holds a bare IP, confirmed by the source owner."""

    target = store_target(settings(), store_code="084", org_ip="10.0.0.2", org_db="STORE084")

    assert target.port is None
