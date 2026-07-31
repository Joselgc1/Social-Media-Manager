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


def test_instagram_mapping_form_submits_multiple_product_skus():
    template = Path("store/app/templates/dashboard.html").read_text()
    dashboard = Path("store/app/static/js/dashboard.js").read_text()

    assert 'id="instagram-product-skus"' in template
    assert 'multiple size="7"' in template
    assert "product_skus: productSkus" in dashboard
    assert "selectedOptions" in dashboard


def test_instagram_mapping_edit_restores_and_clears_all_selections():
    dashboard = Path("store/app/static/js/dashboard.js").read_text()

    assert "new Set(mapping.product_skus || [])" in dashboard
    assert "option.selected = selectedSkus.has(option.value)" in dashboard
    assert "option.selected = false" in dashboard
    assert "renderSelectedInstagramProducts();" in dashboard


def test_instagram_mapping_form_can_remove_one_selected_product():
    dashboard = Path("store/app/static/js/dashboard.js").read_text()

    assert "removeInstagramProduct(selectedIndex)" in dashboard
    assert "[...select.selectedOptions][selectedIndex]" in dashboard
    assert "if (option) option.selected = false" in dashboard
