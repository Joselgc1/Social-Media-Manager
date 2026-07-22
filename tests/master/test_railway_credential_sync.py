from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException


def _database():
    database = MagicMock()
    transaction = MagicMock()
    transaction.__aenter__ = AsyncMock(return_value=None)
    transaction.__aexit__ = AsyncMock(return_value=None)
    database.transaction.return_value = transaction
    return database


@pytest.mark.asyncio
async def test_railway_delete_variable_uses_exact_service_environment(monkeypatch):
    from app.stores import railway

    graphql = AsyncMock(return_value={"variableDelete": True})
    monkeypatch.setattr(railway, "_graphql", graphql)

    await railway.delete_variable("project-1", "service-1", "environment-1", "OLD_KEY")

    variables = graphql.await_args.args[1]
    assert variables == {
        "input": {
            "projectId": "project-1",
            "serviceId": "service-1",
            "environmentId": "environment-1",
            "name": "OLD_KEY",
        },
    }


@pytest.mark.asyncio
async def test_environment_resolution_requires_production_when_not_explicit(monkeypatch):
    from app.stores import api, railway

    monkeypatch.setattr(railway, "get_environments", AsyncMock(return_value=[{"id": "staging-1", "name": "staging"}]))

    with pytest.raises(HTTPException, match="no production environment"):
        await api._resolve_railway_environment_id("project-1")


@pytest.mark.asyncio
async def test_explicit_environment_must_belong_to_project(monkeypatch):
    from app.stores import api, railway

    monkeypatch.setattr(railway, "get_environments", AsyncMock(return_value=[{"id": "production-1", "name": "production"}]))

    with pytest.raises(HTTPException, match="does not belong"):
        await api._resolve_railway_environment_id("project-1", "other-project-env")


@pytest.mark.asyncio
async def test_delete_credential_removes_railway_variable_before_local_row(monkeypatch):
    from app.stores import api, railway

    events = []
    store = {
        "railway_service_id": "service-1",
        "railway_project_id": "project-1",
        "credential_id": "credential-1",
    }
    monkeypatch.setattr(api.db, "fetch_one", AsyncMock(return_value=store))
    monkeypatch.setattr(api.db, "execute", AsyncMock(side_effect=lambda *args, **kwargs: events.append("db")))
    monkeypatch.setattr(api, "get_config", lambda: SimpleNamespace(railway_api_token="token"))
    monkeypatch.setattr(
        railway,
        "get_environments",
        AsyncMock(return_value=[
            {"id": "production-1", "name": "production"},
            {"id": "staging-1", "name": "staging"},
        ]),
    )
    monkeypatch.setattr(
        railway,
        "get_variables",
        AsyncMock(side_effect=[{"OPENAI_API_KEY": "secret"}, {}]),
    )
    monkeypatch.setattr(
        railway,
        "delete_variable",
        AsyncMock(side_effect=lambda *args, **kwargs: events.append("railway")),
    )

    result = await api.delete_credential("store-1", "OPENAI_API_KEY")

    assert result == {"ok": True}
    assert events[0] == "railway"
    assert "db" in events[1:]
    delete_query = api.db.execute.await_args_list[0].args[0]
    assert "DELETE FROM store_credentials" in delete_query


@pytest.mark.asyncio
async def test_failed_railway_delete_preserves_local_credential(monkeypatch):
    from app.stores import api, railway

    store = {
        "railway_service_id": "service-1",
        "railway_project_id": "project-1",
        "credential_id": "credential-1",
    }
    execute = AsyncMock()
    monkeypatch.setattr(api.db, "fetch_one", AsyncMock(return_value=store))
    monkeypatch.setattr(api.db, "execute", execute)
    monkeypatch.setattr(api, "get_config", lambda: SimpleNamespace(railway_api_token="token"))
    monkeypatch.setattr(
        railway,
        "get_environments",
        AsyncMock(return_value=[{"id": "production-1", "name": "production"}]),
    )
    monkeypatch.setattr(
        railway,
        "get_variables",
        AsyncMock(return_value={"OPENAI_API_KEY": "secret"}),
    )
    monkeypatch.setattr(railway, "delete_variable", AsyncMock(side_effect=RuntimeError("Railway unavailable")))
    monkeypatch.setattr(api, "_audit", AsyncMock())

    with pytest.raises(HTTPException, match="local credential was preserved"):
        await api.delete_credential("store-1", "OPENAI_API_KEY")

    execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_database_url_credential_updates_master_connection_source(monkeypatch):
    from app.stores import api
    from app.stores.models import CredentialSet

    execute = AsyncMock()
    monkeypatch.setattr(api.db, "fetch_one", AsyncMock(return_value={"id": "store-1"}))
    monkeypatch.setattr(api.db, "execute", execute)
    monkeypatch.setattr(api.db, "get_db", lambda: _database())
    monkeypatch.setattr(api, "encrypt", lambda value: "encrypted-new-url")
    monkeypatch.setattr(api, "_audit", AsyncMock())

    await api.set_credential("store-1", CredentialSet(key="DATABASE_URL", value="postgresql://new"))

    assert execute.await_count == 2
    assert "INSERT INTO store_credentials" in execute.await_args_list[0].args[0]
    assert "UPDATE stores SET db_url_encrypted" in execute.await_args_list[1].args[0]


@pytest.mark.asyncio
async def test_deploy_forces_authoritative_master_database_url(monkeypatch):
    from app.stores import api, railway

    store = {
        "railway_service_id": "service-1",
        "railway_project_id": "project-1",
        "name": "Store",
        "db_url_encrypted": "authoritative-ciphertext",
    }
    credentials = [
        {"key": "DATABASE_URL", "value_encrypted": "stale-ciphertext"},
        {"key": "OPENAI_API_KEY", "value_encrypted": "openai-ciphertext"},
    ]
    decrypt_values = {
        "authoritative-ciphertext": "postgresql://authoritative",
        "stale-ciphertext": "postgresql://stale",
        "openai-ciphertext": "sk-test",
    }
    upsert = AsyncMock(return_value=True)
    monkeypatch.setattr(api.db, "fetch_one", AsyncMock(return_value=store))
    monkeypatch.setattr(api.db, "fetch_all", AsyncMock(return_value=credentials))
    monkeypatch.setattr(api.db, "execute", AsyncMock())
    monkeypatch.setattr(api, "decrypt", lambda value: decrypt_values[value])
    monkeypatch.setattr(api, "get_config", lambda: SimpleNamespace(railway_api_token="token"))
    monkeypatch.setattr(api, "_resolve_railway_environment_id", AsyncMock(return_value="production-1"))
    monkeypatch.setattr(api, "_audit", AsyncMock())
    monkeypatch.setattr(railway, "upsert_variables", upsert)
    monkeypatch.setattr(railway, "get_latest_deployment", AsyncMock(return_value={"id": "deployment-1"}))
    monkeypatch.setattr(railway, "redeploy_service", AsyncMock(return_value="deployment-2"))

    result = await api.deploy_credentials("store-1")

    variables = upsert.await_args.args[3]
    assert variables["DATABASE_URL"] == "postgresql://authoritative"
    assert variables["OPENAI_API_KEY"] == "sk-test"
    assert result["environment_id"] == "production-1"
