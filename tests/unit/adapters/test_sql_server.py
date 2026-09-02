"""Shared SQL Server transport rules for every source tier (#91, #93, AD-020).

A live read on 2026-09-02 showed snapshot isolation OFF on all three source
databases (PEPITO_HO, DBWH_8555, ESL), so a SNAPSHOT-only engine could read
nothing (SQL Server error 3952). The owner decided: READ COMMITTED by
default, stricter than the legacy procedure's NOLOCK, recorded in provenance,
and SNAPSHOT only when configuration asks for it after a DBA enables it.
"""

from typing import cast

import pytest
from sqlalchemy import Engine
from sqlalchemy.engine import URL

from esl_service.adapters import sql_server as sql_server_module
from esl_service.adapters.sql_server import (
    DEFAULT_ISOLATION_LEVEL,
    SUPPORTED_ISOLATION_LEVELS,
    build_read_only_url,
    create_read_only_engine,
)

URL_WITH_SECRET = URL.create(
    "mssql+pyodbc",
    username="reader",
    password="top-secret",
    host="sql.internal",
    database="PEPITO_HO",
    query={"driver": "ODBC Driver 18 for SQL Server"},
)


def capture(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    captured: dict[str, object] = {}

    def fake_create_engine(url: URL, **kwargs: object) -> Engine:
        captured["url"] = url
        captured.update(kwargs)
        return cast(Engine, object())

    monkeypatch.setattr(sql_server_module, "create_engine", fake_create_engine)
    return captured


def test_the_default_isolation_is_read_committed(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = capture(monkeypatch)

    create_read_only_engine(URL_WITH_SECRET)

    assert DEFAULT_ISOLATION_LEVEL == "READ COMMITTED"
    assert captured["isolation_level"] == "READ COMMITTED"


def test_snapshot_is_used_only_when_asked_for(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = capture(monkeypatch)

    create_read_only_engine(URL_WITH_SECRET, isolation_level="SNAPSHOT")

    assert captured["isolation_level"] == "SNAPSHOT"


def test_an_unsupported_isolation_level_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    capture(monkeypatch)

    with pytest.raises(ValueError, match="isolation"):
        create_read_only_engine(URL_WITH_SECRET, isolation_level="READ UNCOMMITTED")

    assert SUPPORTED_ISOLATION_LEVELS == ("READ COMMITTED", "SNAPSHOT")


def test_the_engine_requests_read_intent_and_a_bounded_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = capture(monkeypatch)

    create_read_only_engine(URL_WITH_SECRET)

    url = cast(URL, captured["url"])
    assert url.query["ApplicationIntent"] == "ReadOnly"
    assert captured["connect_args"] == {"timeout": 10}
    assert captured["pool_pre_ping"] is True


def test_the_read_only_url_never_renders_the_password() -> None:
    assert "top-secret" not in repr(build_read_only_url(URL_WITH_SECRET))
