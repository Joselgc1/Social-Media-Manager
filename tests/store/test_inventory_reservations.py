import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _sheet(records, *, default_active=True):
    if default_active:
        records = [{"Active": "yes", **record} for record in records]
    worksheet = MagicMock()
    worksheet.title = "Products"
    worksheet.get_all_records.return_value = records
    worksheet.row_values.return_value = ["SKU", "Product name", "Stock"]
    ledger = MagicMock()
    ledger.title = "_inventory_mutations"
    ledger.row_values.return_value = [
        "Operation ID",
        "Type",
        "SKU",
        "Quantity",
        "Stock Before",
        "Stock After",
        "Created At",
    ]
    ledger.get_all_records.return_value = []
    spreadsheet = MagicMock()
    spreadsheet.sheet1 = worksheet
    spreadsheet.worksheet.return_value = ledger
    client = MagicMock()
    client.open_by_key.return_value = spreadsheet
    return client, worksheet


def _transaction():
    transaction = MagicMock()
    transaction.__aenter__ = AsyncMock(return_value=None)
    transaction.__aexit__ = AsyncMock(return_value=None)
    return transaction


def _database(fetch_one=None, execute=None):
    database = MagicMock()
    connection = MagicMock()
    connection.__aenter__ = AsyncMock(return_value=connection)
    connection.__aexit__ = AsyncMock(return_value=None)
    connection.transaction.side_effect = _transaction
    connection.fetch_one = fetch_one or AsyncMock()
    connection.execute = execute or AsyncMock()
    database.connection.return_value = connection
    return database


def _catalog():
    return [{
        "sku": "SKU-S",
        "parent_sku": "SKU",
        "product_name": "Pijama",
        "size": "S",
        "sizes": "S",
        "price_usd": 28,
        "stock": 2,
    }]


def _items():
    return [{"sku": "SKU-S", "product_name": "Pijama", "size": "S", "quantity": 1}]


def _order_settings():
    return {
        "payment_methods": [
            {"id": "pm-zelle", "name": "Zelle", "information": "pagos@example.com"},
        ],
    }


def test_sheet_inventory_uses_one_batch_after_validating_every_item():
    from app.catalog import sheets

    client, worksheet = _sheet([
        {"SKU": "SKU-S", "Stock": 3},
        {"SKU": "SKU-M", "Stock": 2},
    ])
    with (
        patch.object(sheets, "_get_gspread_client", return_value=client),
        patch.object(sheets, "get_config", return_value=SimpleNamespace(product_sheet_id="sheet")),
        patch.object(sheets, "refresh_catalog"),
    ):
        result = sheets.deduct_stock([
            {"sku": "SKU-S", "quantity": 2},
            {"sku": "SKU-M", "quantity": 1},
        ])

    assert result == {"SKU-S": 1, "SKU-M": 1}
    worksheet.batch_update.assert_called_once_with(
        [
            {"range": "C2", "values": [[1]]},
            {"range": "C3", "values": [[1]]},
        ],
        raw=True,
    )
    worksheet.update_cell.assert_not_called()


def test_gspread_client_has_explicit_timeout(monkeypatch):
    from app.catalog import sheets

    client = MagicMock()
    authorize = MagicMock(return_value=client)
    credentials = MagicMock()
    monkeypatch.setattr(sheets, "get_config", lambda: SimpleNamespace(google_sheets_credentials_b64="e30="))
    monkeypatch.setattr(sheets.Credentials, "from_service_account_info", MagicMock(return_value=credentials))
    monkeypatch.setattr(sheets.gspread, "authorize", authorize)

    assert sheets._get_gspread_client() is client
    authorize.assert_called_once_with(credentials)
    client.set_timeout.assert_called_once_with((5, 30))


def test_sheet_inventory_operation_id_updates_stock_and_ledger_atomically():
    from app.catalog import sheets

    client, worksheet = _sheet([{"SKU": "SKU-S", "Stock": 3}])
    spreadsheet = client.open_by_key.return_value
    with (
        patch.object(sheets, "_get_gspread_client", return_value=client),
        patch.object(sheets, "get_config", return_value=SimpleNamespace(product_sheet_id="sheet")),
        patch.object(sheets, "refresh_catalog"),
    ):
        result = sheets.deduct_stock(
            [{"sku": "SKU-S", "quantity": 2}],
            operation_id="order:order-1:reserve",
        )

    assert result == {"SKU-S": 1}
    worksheet.batch_update.assert_not_called()
    spreadsheet.values_batch_update.assert_called_once()
    body = spreadsheet.values_batch_update.call_args.args[0]
    assert body["valueInputOption"] == "RAW"
    assert body["data"][0] == {"range": "'Products'!C2", "values": [[1]]}
    assert body["data"][1]["range"] == "'_inventory_mutations'!A2:G2"
    assert body["data"][1]["values"][0][:6] == [
        "order:order-1:reserve",
        "deduct",
        "SKU-S",
        2,
        3,
        1,
    ]


def test_sheet_inventory_operation_id_is_idempotent_from_ledger():
    from app.catalog import sheets

    client, worksheet = _sheet([{"SKU": "SKU-S", "Stock": 3}])
    spreadsheet = client.open_by_key.return_value
    ledger = spreadsheet.worksheet.return_value
    ledger.get_all_records.return_value = [{
        "Operation ID": "order:order-1:reserve",
        "Type": "deduct",
        "SKU": "SKU-S",
        "Quantity": 2,
        "Stock Before": 3,
        "Stock After": 1,
        "Created At": "2026-01-01T00:00:00+00:00",
    }]

    with (
        patch.object(sheets, "_get_gspread_client", return_value=client),
        patch.object(sheets, "get_config", return_value=SimpleNamespace(product_sheet_id="sheet")),
        patch.object(sheets, "refresh_catalog"),
    ):
        result = sheets.deduct_stock(
            [{"sku": "SKU-S", "quantity": 2}],
            operation_id="order:order-1:reserve",
        )

    assert result == {"SKU-S": 1}
    worksheet.get_all_records.assert_not_called()
    worksheet.batch_update.assert_not_called()
    spreadsheet.values_batch_update.assert_not_called()


def test_inventory_ledger_grows_before_writing_past_its_grid():
    from app.catalog import sheets

    client, worksheet = _sheet([{"SKU": "SKU-S", "Stock": 3}])
    spreadsheet = client.open_by_key.return_value
    ledger = spreadsheet.worksheet.return_value
    ledger.row_count = 2
    ledger.get_all_records.return_value = [{
        "Operation ID": "order:old:reserve",
        "Type": "deduct",
        "SKU": "SKU-S",
        "Quantity": 1,
        "Stock Before": 4,
        "Stock After": 3,
        "Created At": "2026-01-01T00:00:00+00:00",
    }]
    with (
        patch.object(sheets, "_get_gspread_client", return_value=client),
        patch.object(sheets, "get_config", return_value=SimpleNamespace(product_sheet_id="capacity-sheet")),
        patch.object(sheets, "refresh_catalog"),
    ):
        sheets.deduct_stock([{"sku": "SKU-S", "quantity": 1}], operation_id="order:new:reserve")

    ledger.add_rows.assert_called_once_with(1000)
    body = spreadsheet.values_batch_update.call_args.args[0]
    assert body["data"][1]["range"] == "'_inventory_mutations'!A3:G3"


def test_inventory_ledger_is_not_reloaded_after_cached_mutation():
    from app.catalog import sheets

    client, worksheet = _sheet([{"SKU": "SKU-S", "Stock": 4}])
    spreadsheet = client.open_by_key.return_value
    ledger = spreadsheet.worksheet.return_value
    ledger.row_count = 1000
    with (
        patch.object(sheets, "_get_gspread_client", return_value=client),
        patch.object(sheets, "get_config", return_value=SimpleNamespace(product_sheet_id="cache-sheet")),
        patch.object(sheets, "refresh_catalog"),
    ):
        sheets.deduct_stock([{"sku": "SKU-S", "quantity": 1}], operation_id="order:first:reserve")
        sheets.deduct_stock([{"sku": "SKU-S", "quantity": 1}], operation_id="order:second:reserve")

    ledger.get_all_records.assert_called_once()


def test_inventory_operation_verification_forces_a_fresh_ledger_read():
    from app.catalog import sheets

    client, _worksheet = _sheet([{"SKU": "SKU-S", "Stock": 3}])
    spreadsheet = client.open_by_key.return_value
    ledger = spreadsheet.worksheet.return_value
    ledger.get_all_records.side_effect = [[], [{
        "Operation ID": "order:committed:reserve",
        "Type": "deduct",
        "SKU": "SKU-S",
        "Quantity": 1,
        "Stock Before": 4,
        "Stock After": 3,
        "Created At": "2026-01-01T00:00:00+00:00",
    }]]
    with (
        patch.object(sheets, "_get_gspread_client", return_value=client),
        patch.object(sheets, "get_config", return_value=SimpleNamespace(product_sheet_id="fresh-ledger-sheet")),
    ):
        assert sheets.inventory_operation_exists("order:committed:reserve") is False
        assert sheets.inventory_operation_applied(
            "order:committed:reserve", [{"sku": "SKU-S", "quantity": 1}], direction=-1
        ) is True

    assert ledger.get_all_records.call_count == 2


@pytest.mark.parametrize(
    "items",
    [
        [{"sku": "MISSING", "quantity": 1}],
        [{"sku": "SKU-S", "quantity": 3}],
    ],
)
def test_sheet_inventory_failure_never_partially_updates(items):
    from app.catalog import sheets

    client, worksheet = _sheet([{"SKU": "SKU-S", "Stock": 2}])
    with (
        patch.object(sheets, "_get_gspread_client", return_value=client),
        patch.object(sheets, "get_config", return_value=SimpleNamespace(product_sheet_id="sheet")),
        pytest.raises(sheets.InventoryUpdateError),
    ):
        sheets.deduct_stock(items)

    worksheet.batch_update.assert_not_called()


@pytest.mark.parametrize(
    ("record", "message"),
    [
        ({"SKU": "SKU-S", "Stock": 2, "Active": "no", "Price USD": 28}, "not active"),
        ({"SKU": "SKU-S", "Stock": 2, "Active": "yes", "Price USD": 30}, "price changed"),
    ],
)
def test_sheet_inventory_deduction_validates_current_active_flag_and_price(record, message):
    from app.catalog import sheets

    client, worksheet = _sheet([record], default_active=False)
    with (
        patch.object(sheets, "_get_gspread_client", return_value=client),
        patch.object(sheets, "get_config", return_value=SimpleNamespace(product_sheet_id="sheet")),
        pytest.raises(sheets.InventoryUpdateError, match=message),
    ):
        sheets.deduct_stock([{"sku": "SKU-S", "quantity": 1, "unit_price": 28}])

    worksheet.batch_update.assert_not_called()


@pytest.mark.parametrize(
    ("record", "message"),
    [
        ({"SKU": "SKU-S", "Stock": 2, "Price USD": 28}, "not active"),
        ({"SKU": "SKU-S", "Stock": 2, "Active": "yes", "Price USD": "invalid"}, "price changed or is invalid"),
    ],
)
def test_sheet_inventory_deduction_fails_closed_for_missing_active_or_malformed_price(record, message):
    from app.catalog import sheets

    client, worksheet = _sheet([record], default_active=False)
    with (
        patch.object(sheets, "_get_gspread_client", return_value=client),
        patch.object(sheets, "get_config", return_value=SimpleNamespace(product_sheet_id="sheet")),
        pytest.raises(sheets.InventoryUpdateError, match=message),
    ):
        sheets.deduct_stock([{"sku": "SKU-S", "quantity": 1, "unit_price": 28}])

    worksheet.batch_update.assert_not_called()


@pytest.mark.asyncio
async def test_order_persists_pending_reservation_before_sheet_mutation():
    from app.crm import orders

    events = []
    fetch_one = AsyncMock(side_effect=[
        None,
        None,
        {"id": "order-1", "payment_status": "pending", "inventory_status": "reservation_pending"},
        None,
    ])

    async def execute(query, values=None):
        if "INSERT INTO orders" in query:
            events.append("insert")
            return "order-1"
        if "inventory_status = 'reserved'" in query:
            events.append("finalize")
        return None

    execute_mock = AsyncMock(side_effect=execute)
    deduct = MagicMock(side_effect=lambda items, operation_id=None: events.append("deduct"))

    with (
        patch.object(orders, "get_cached_catalog", return_value=_catalog()),
        patch.object(orders, "ensure_fresh_catalog", AsyncMock(return_value=_catalog())),
        patch.object(orders.db, "get_settings", AsyncMock(return_value=_order_settings())),
        patch.object(orders.db, "fetch_one", AsyncMock(return_value={"id": "customer-1"})),
        patch.object(orders.db, "get_db", return_value=_database(fetch_one=fetch_one, execute=execute_mock)),
        patch.object(orders, "deduct_stock", deduct),
    ):
        result = await orders.create_order(
            "customer-1", _items(), "Zelle", "Caracas", "Av. Principal", "mrw"
        )

    assert events == ["insert", "deduct", "finalize"]
    assert result["created_new"] is True
    assert "inventory_status" in execute_mock.await_args_list[0].args[0]
    deduct.assert_called_once()
    assert deduct.call_args.kwargs["operation_id"] == "order:order-1:reserve"


@pytest.mark.asyncio
async def test_failed_order_insert_never_mutates_inventory():
    from app.crm import orders

    deduct = MagicMock()
    restore = MagicMock()
    with (
        patch.object(orders, "get_cached_catalog", return_value=_catalog()),
        patch.object(orders, "ensure_fresh_catalog", AsyncMock(return_value=_catalog())),
        patch.object(orders.db, "get_settings", AsyncMock(return_value=_order_settings())),
        patch.object(orders.db, "fetch_one", AsyncMock(return_value={"id": "customer-1"})),
        patch.object(
            orders.db,
            "get_db",
            return_value=_database(
                fetch_one=AsyncMock(side_effect=[None, None, None]),
                execute=AsyncMock(side_effect=RuntimeError("insert failed")),
            ),
        ),
        patch.object(orders, "deduct_stock", deduct),
        patch.object(orders, "restore_stock", restore),
        pytest.raises(RuntimeError, match="insert failed"),
    ):
        await orders.create_order(
            "customer-1", _items(), "Zelle", "Caracas", "Av. Principal", "mrw"
        )

    deduct.assert_not_called()
    restore.assert_not_called()


@pytest.mark.asyncio
async def test_failed_reservation_finalize_does_not_compensate_stock():
    from app.crm import orders

    async def execute(query, values=None):
        if "INSERT INTO orders" in query:
            return "order-1"
        if "inventory_status = 'reserved'" in query:
            raise RuntimeError("finalize failed")
        return None

    deduct = MagicMock()
    restore = MagicMock()
    with (
        patch.object(orders, "get_cached_catalog", return_value=_catalog()),
        patch.object(orders, "ensure_fresh_catalog", AsyncMock(return_value=_catalog())),
        patch.object(orders.db, "get_settings", AsyncMock(return_value=_order_settings())),
        patch.object(orders.db, "fetch_one", AsyncMock(return_value={"id": "customer-1"})),
        patch.object(
            orders.db,
            "get_db",
            return_value=_database(
                fetch_one=AsyncMock(side_effect=[
                    None,
                    None,
                    {"id": "order-1", "payment_status": "pending", "inventory_status": "reservation_pending"},
                    None,
                ]),
                execute=AsyncMock(side_effect=execute),
            ),
        ),
        patch.object(orders, "deduct_stock", deduct),
        patch.object(orders, "restore_stock", restore),
        pytest.raises(RuntimeError, match="finalize failed"),
    ):
        await orders.create_order(
            "customer-1", _items(), "Zelle", "Caracas", "Av. Principal", "mrw"
        )

    deduct.assert_called_once()
    restore.assert_not_called()


@pytest.mark.asyncio
async def test_reported_sheet_error_with_matching_ledger_still_finalizes_order():
    from app.crm import orders

    events = []

    async def execute(query, values=None):
        if "INSERT INTO orders" in query:
            events.append("insert")
            return "order-1"
        if "inventory_status = 'reserved'" in query:
            events.append("finalize")
        return None

    deduct = MagicMock(side_effect=RuntimeError("timeout"))
    applied = MagicMock(return_value=True)
    with (
        patch.object(orders, "get_cached_catalog", return_value=_catalog()),
        patch.object(orders, "ensure_fresh_catalog", AsyncMock(return_value=_catalog())),
        patch.object(orders.db, "get_settings", AsyncMock(return_value=_order_settings())),
        patch.object(orders.db, "fetch_one", AsyncMock(return_value={"id": "customer-1"})),
        patch.object(
            orders.db,
            "get_db",
            return_value=_database(
                fetch_one=AsyncMock(side_effect=[
                    None,
                    None,
                    {"id": "order-1", "payment_status": "pending", "inventory_status": "reservation_pending"},
                    None,
                ]),
                execute=AsyncMock(side_effect=execute),
            ),
        ),
        patch.object(orders, "deduct_stock", deduct),
        patch.object(orders, "inventory_operation_applied", applied),
    ):
        result = await orders.create_order(
            "customer-1", _items(), "Zelle", "Caracas", "Av. Principal", "mrw"
        )

    assert result["order_id"] == "order-1"
    assert events == ["insert", "finalize"]
    applied.assert_called_once()


@pytest.mark.asyncio
async def test_failed_sheet_reservation_marks_order_failed_without_restore():
    from app.crm import orders

    execute_events = []

    async def execute(query, values=None):
        if "INSERT INTO orders" in query:
            return "order-1"
        execute_events.append((query, values))
        return None

    restore = MagicMock()
    with (
        patch.object(orders, "get_cached_catalog", return_value=_catalog()),
        patch.object(orders, "ensure_fresh_catalog", AsyncMock(return_value=_catalog())),
        patch.object(orders.db, "get_settings", AsyncMock(return_value=_order_settings())),
        patch.object(orders.db, "fetch_one", AsyncMock(return_value={"id": "customer-1"})),
        patch.object(
            orders.db,
            "get_db",
            return_value=_database(
                fetch_one=AsyncMock(side_effect=[None, None, None]),
                execute=AsyncMock(side_effect=execute),
            ),
        ),
        patch.object(orders, "deduct_stock", MagicMock(side_effect=RuntimeError("sheets failed"))),
        patch.object(orders, "inventory_operation_applied", MagicMock(return_value=False)),
        patch.object(orders, "restore_stock", restore),
        pytest.raises(RuntimeError, match="sheets failed"),
    ):
        await orders.create_order(
            "customer-1", _items(), "Zelle", "Caracas", "Av. Principal", "mrw"
        )

    restore.assert_not_called()
    assert any("inventory_status = :failed_status" in query for query, _ in execute_events)


@pytest.mark.asyncio
async def test_unverifiable_sheet_reservation_stays_pending_for_safe_reconciliation():
    from app.crm import orders

    execute_events = []

    async def execute(query, values=None):
        if "INSERT INTO orders" in query:
            return "order-1"
        execute_events.append((query, values))
        return None

    with (
        patch.object(orders, "get_cached_catalog", return_value=_catalog()),
        patch.object(orders, "ensure_fresh_catalog", AsyncMock(return_value=_catalog())),
        patch.object(orders.db, "get_settings", AsyncMock(return_value=_order_settings())),
        patch.object(orders.db, "fetch_one", AsyncMock(return_value={"id": "customer-1"})),
        patch.object(
            orders.db,
            "get_db",
            return_value=_database(
                fetch_one=AsyncMock(side_effect=[None, None]),
                execute=AsyncMock(side_effect=execute),
            ),
        ),
        patch.object(orders, "deduct_stock", MagicMock(side_effect=RuntimeError("timeout"))),
        patch.object(orders, "inventory_operation_applied", MagicMock(side_effect=RuntimeError("ledger unavailable"))),
        pytest.raises(orders.InventoryReservationUncertainError),
    ):
        await orders.create_order(
            "customer-1", _items(), "Zelle", "Caracas", "Av. Principal", "mrw"
        )

    assert not any("inventory_status = :failed_status" in query for query, _ in execute_events)


@pytest.mark.asyncio
async def test_cancelled_sheet_mutation_holds_advisory_lock_until_thread_finishes():
    from app.crm import orders

    started = threading.Event()
    finish = threading.Event()
    lock_events = []

    async def fetch_one(query, values=None):
        lock_events.append("unlock" if "pg_advisory_unlock" in query else "lock")
        return None

    def blocking_deduct(items, operation_id=None):
        started.set()
        finish.wait(timeout=2)

    async def reserve_inventory():
        async with orders._inventory_mutation_connection():
            await orders._deduct_order_inventory("order-1", _items())

    with (
        patch.object(orders.db, "get_db", return_value=_database(fetch_one=AsyncMock(side_effect=fetch_one))),
        patch.object(orders, "deduct_stock", blocking_deduct),
    ):
        task = asyncio.create_task(reserve_inventory())
        await asyncio.to_thread(started.wait, 1)
        task.cancel()
        await asyncio.sleep(0)
        assert lock_events == ["lock"]

        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert lock_events == ["lock", "unlock"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("items", "city", "address", "method", "message"),
    [
        ([], "Caracas", "Av. Principal", "mrw", "at least one item"),
        (_items(), " ", "Av. Principal", "mrw", "shipping city"),
        (_items(), "Caracas", " ", "mrw", "shipping address"),
        (_items(), "Caracas", "Av. Principal", "pickup", "MRW or Zoom"),
    ],
)
async def test_order_rejects_malformed_required_fields_before_side_effects(
    items, city, address, method, message
):
    from app.crm import orders

    with pytest.raises(ValueError, match=message):
        await orders.create_order("customer-1", items, "Zelle", city, address, method)


@pytest.mark.asyncio
async def test_order_rejects_unconfigured_payment_method_before_inventory_mutation():
    from app.crm import orders

    deduct = MagicMock()
    with (
        patch.object(orders.db, "get_settings", AsyncMock(return_value=_order_settings())),
        patch.object(orders, "deduct_stock", deduct),
        pytest.raises(ValueError, match="not configured"),
    ):
        await orders.create_order(
            "customer-1", _items(), "Inventado", "Caracas", "Av. Principal", "mrw"
        )

    deduct.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("inventory_status", "expected_restored"),
    [("reserved", 1), ("legacy_unknown", 0), ("released", 0)],
)
async def test_delete_restores_only_confirmed_reservations(inventory_status, expected_restored):
    from app.crm import orders

    first_row = {
        "id": "order-1",
        "items": _items(),
        "inventory_status": inventory_status,
    }
    final_row = {
        "id": "order-1",
        "customer_id": None,
        "total": 28,
        "customer_totals_applied": False,
    }
    restore = MagicMock()
    execute = AsyncMock()
    with (
        patch.object(
            orders.db,
            "get_db",
            return_value=_database(
                fetch_one=AsyncMock(side_effect=[None, first_row, final_row, None]),
                execute=execute,
            ),
        ),
        patch.object(orders, "restore_stock", restore),
    ):
        result = await orders.delete_order("order-1")

    assert result["restored_items"] == expected_restored
    assert restore.call_count == expected_restored
    if expected_restored:
        assert restore.call_args.kwargs["operation_id"] == "order:order-1:release"
        assert "inventory_status = :release_pending" in execute.await_args_list[0].args[0]


@pytest.mark.asyncio
async def test_expired_pending_reservation_is_restored_and_rejected():
    from app.crm import orders

    first_row = {
        "id": "order-1",
        "items": _items(),
        "inventory_status": "reserved",
        "payment_status": "pending",
    }
    final_row = {"id": "order-1", "payment_status": "pending"}
    execute = AsyncMock()
    restore = MagicMock()
    with (
        patch.object(
            orders.db,
            "get_db",
            return_value=_database(
                fetch_one=AsyncMock(side_effect=[None, first_row, final_row, None]),
                execute=execute,
            ),
        ),
        patch.object(orders, "restore_stock", restore),
    ):
        released = await orders._release_inventory_reservation("order-1")

    assert released is True
    restore.assert_called_once()
    assert restore.call_args.kwargs["operation_id"] == "order:order-1:release"
    assert "inventory_status = :release_pending" in execute.await_args_list[0].args[0]
    assert "inventory_status = :released" in execute.await_args_list[-1].args[0]
    assert "THEN 'rejected'" in execute.await_args_list[-1].args[0]
