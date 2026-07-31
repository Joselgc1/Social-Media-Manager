from pathlib import Path


def test_archived_instagram_mappings_render_restore_action():
    dashboard = Path("store/app/static/js/dashboard.js").read_text()

    assert "restoreInstagramMapping" in dashboard
    assert "Restaurar" in dashboard
    assert "body: JSON.stringify({status: 'active'})" in dashboard
    assert "archiveInstagramMapping" in dashboard
    assert "Archivar" in dashboard


def test_mapping_list_renders_when_product_selector_request_fails():
    dashboard = Path("store/app/static/js/dashboard.js").read_text()

    assert "Promise.allSettled" in dashboard
    assert "if (mappingsResult.status === 'rejected')" in dashboard
    assert "productsResult.status === 'fulfilled'" in dashboard
    assert "Catálogo temporalmente no disponible" in dashboard


def test_mapping_list_renders_authoritative_post_identifiers():
    dashboard = Path("store/app/static/js/dashboard.js").read_text()

    assert "URL normalizada" in dashboard
    assert "Shortcode" in dashboard
    assert "Meta media ID" in dashboard
    assert "mapping.normalized_url" in dashboard
    assert "mapping.media_id" in dashboard
