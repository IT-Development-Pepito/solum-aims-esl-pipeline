"""Source and AIMS settings carry no secret (#78, NFR-009, AD-007, AD-017).

Every password is read from the DPAPI bundle by name. Configuration holds only
the non-secret parts -- host, database, username, driver, TLS trust -- and the
startup gate refuses a state-store URL that still embeds a password, because
that was the one pre-existing exception to the rule.
"""

from esl_service.config import (
    SECRET_BEARING_SETTINGS,
    Settings,
    sanitized_configuration_snapshot,
    validate_startup_configuration,
)


def valid_values(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "environment": "development",
        "database_url": "postgresql+psycopg://esl_pipeline_dev@localhost:5432/esl_pipeline_dev",
        "internal_host": "127.0.0.1",
    }
    values.update(overrides)
    return values


# --- the fields exist, default to unconfigured, and carry no password -------


def test_source_and_aims_fields_default_to_unconfigured() -> None:
    """A fresh configuration names no source; nothing is assumed."""

    settings = Settings.model_validate(valid_values())

    assert settings.source_sql_host == ""
    assert settings.source_sql_username == ""
    assert settings.source_pepito_ho_host == ""
    assert settings.aims_host == ""
    assert settings.aims_portal_username == ""
    assert settings.aims_core_username == ""


def test_shared_sql_server_client_settings_have_safe_defaults() -> None:
    """One driver covers every SQL Server target, confirmed by the source owner."""

    settings = Settings.model_validate(valid_values())

    assert settings.source_sql_driver == "ODBC Driver 18 for SQL Server"
    assert settings.source_sql_trust_server_certificate is True
    assert settings.aims_port == 5432


def test_database_names_default_to_the_verified_ones() -> None:
    """Warehouse and legacy baseline share the ESL instance; names are VERIFIED."""

    settings = Settings.model_validate(valid_values())

    assert settings.source_warehouse_database == "DBWH_8555"
    assert settings.legacy_baseline_database == "ESL"
    assert settings.source_pepito_ho_database == "PEPITO_HO"


def test_no_settings_field_is_a_password() -> None:
    """The model must not even offer a place to put one."""

    assert not [name for name in Settings.model_fields if "password" in name.lower()]


# --- the snapshot shows where, never what -----------------------------------


def test_snapshot_records_host_database_and_username_for_every_source() -> None:
    """Audit can answer 'which server, as whom' for a run (FR-002, FR-025)."""

    settings = Settings.model_validate(
        valid_values(
            source_sql_host="sql.internal",
            source_sql_username="esl_reader",
            source_pepito_ho_host="ho.internal",
            aims_host="aims.internal",
            aims_portal_database="AIMS_PORTAL_DB",
            aims_portal_username="esl_aims_reader",
        )
    )

    snapshot = sanitized_configuration_snapshot(settings)

    assert snapshot["source_sql_host"] == "sql.internal"
    assert snapshot["source_sql_username"] == "esl_reader"
    assert snapshot["source_pepito_ho_host"] == "ho.internal"
    assert snapshot["aims_host"] == "aims.internal"
    assert snapshot["aims_portal_username"] == "esl_aims_reader"


def test_snapshot_contains_no_password_anywhere() -> None:
    snapshot = sanitized_configuration_snapshot(Settings.model_validate(valid_values()))

    assert not [key for key in snapshot if "password" in key.lower()]
    assert "database_url" in SECRET_BEARING_SETTINGS


# --- the startup gate closes the one pre-existing exception -----------------


def test_startup_refuses_a_state_store_url_that_embeds_a_password() -> None:
    """ESL_DATABASE_URL predates AD-007; the gate now holds it to the same rule."""

    settings, problems = validate_startup_configuration(
        valid_values(database_url="postgresql+psycopg://user:sup3r@localhost/esl")
    )

    assert settings is None
    assert any(problem.key == "database_url" for problem in problems)
    assert all("sup3r" not in problem.message for problem in problems)


def test_startup_accepts_a_password_free_state_store_url() -> None:
    settings, problems = validate_startup_configuration(valid_values())

    assert settings is not None
    assert problems == ()


def test_the_model_itself_stays_permissive_for_fixtures() -> None:
    """Unit fixtures build Settings directly; only the startup gate enforces."""

    settings = Settings.model_validate(
        valid_values(database_url="postgresql+psycopg://user:pw@localhost/esl")
    )

    assert settings.database_url.endswith("/esl")
