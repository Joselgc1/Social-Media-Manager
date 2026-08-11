from pathlib import Path


def _dashboard_source() -> str:
    return Path("store/app/static/js/dashboard.js").read_text()


def test_payment_methods_use_editable_settings_table_with_final_action_column():
    source = _dashboard_source()

    assert "settingsRecordTable(" in source
    assert "['#', 'Nombre', 'Información para el cliente', 'Acciones']" in source
    assert 'id="payment-methods-rows"' not in source
    assert "'payment-methods-rows'" in source
    assert '<tr class="payment-method-card"' in source
    assert 'data-label="Acciones" class="settings-record-actions"' in source
    assert "removePaymentMethod(this)" in source


def test_local_delivery_cities_and_zones_use_editable_settings_tables():
    source = _dashboard_source()

    assert "['Ciudad o municipio', 'Alias', 'Acciones']" in source
    assert "['Ciudad', 'Zona', 'Tarifa USD', 'Acciones']" in source
    assert "'home-delivery-cities-rows'" in source
    assert "'home-delivery-zones-rows'" in source
    assert '<tr class="shipping-rate-card">' in source
    assert 'data-label="Tarifa USD"' in source
    assert "removeHomeDeliveryCity(this)" in source
    assert "removeShippingRateCard(this)" in source


def test_agency_pickup_rates_use_editable_settings_table():
    source = _dashboard_source()

    assert "['Ciudad de destino', 'MRW USD', 'Zoom USD', 'Acciones']" in source
    assert "'courier-destination-rates-rows'" in source
    assert 'data-label="MRW USD"' in source
    assert 'data-label="Zoom USD"' in source
    assert "shipping-courier-table" in source


def test_settings_tables_become_labelled_cards_on_mobile():
    styles = Path("store/app/static/css/dashboard.css").read_text()

    assert ".settings-record-table tbody tr" in styles
    assert ".settings-record-table thead { display: none; }" in styles
    assert "content: attr(data-label)" in styles
    assert ".settings-record-table tbody tr + tr td { border-top: 0; }" in styles


def test_settings_cards_stack_fields_on_narrow_phones():
    styles = Path("store/app/static/css/dashboard.css").read_text()

    narrow_phone_styles = styles.split("@media (max-width: 479px)", 1)[1]
    assert ".settings-record-table td" in narrow_phone_styles
    assert "grid-template-columns: minmax(0, 1fr);" in narrow_phone_styles
    assert ".settings-record-table .settings-record-actions" in narrow_phone_styles
