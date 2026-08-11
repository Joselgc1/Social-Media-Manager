from pathlib import Path


def test_archived_instagram_mappings_render_restore_action():
    dashboard = Path("store/app/static/js/dashboard.js").read_text()

    assert "restoreInstagramMapping" in dashboard
    assert "Restaurar" in dashboard
    assert "body: JSON.stringify({status: 'active'})" in dashboard
    assert "archiveInstagramMapping" in dashboard
    assert "Archivar" in dashboard


def test_archived_instagram_mappings_are_hidden_by_default_with_lifecycle_filter():
    template = Path("store/app/templates/dashboard.html").read_text()
    dashboard = Path("store/app/static/js/dashboard.js").read_text()

    assert 'id="instagram-filter-lifecycle"' in template
    assert '<option value="current">No archivados</option>' in template
    assert '<option value="archived">Archivados</option>' in template
    assert "lifecycleValue === 'current' && lifecycle === 'archived'" in dashboard


def test_mapping_list_renders_when_product_selector_request_fails():
    dashboard = Path("store/app/static/js/dashboard.js").read_text()

    assert "Promise.allSettled" in dashboard
    assert "if (mappingsResult.status === 'rejected')" in dashboard
    assert "productsResult.status === 'fulfilled'" in dashboard
    assert "Catálogo temporalmente no disponible" in dashboard


def test_mapping_list_renders_authoritative_post_identifiers():
    dashboard = Path("store/app/static/js/dashboard.js").read_text()

    assert "mapping.normalized_url" in dashboard
    assert "mapping.media_id" in dashboard
    assert "mapping.shortcode" in dashboard
    assert "ID de Meta pendiente" in dashboard


def test_mapping_list_uses_client_style_table_filters_and_mobile_cards():
    template = Path("store/app/templates/dashboard.html").read_text()
    dashboard = Path("store/app/static/js/dashboard.js").read_text()

    assert 'id="instagram-filter-toggle"' in template
    assert 'id="instagram-filter-search"' in template
    assert 'id="instagram-filter-type"' in template
    assert 'id="instagram-filter-mapping"' in template
    assert "function getVisibleInstagramMappings()" in dashboard
    assert 'class="w-full customers-table instagram-mappings-table"' in dashboard
    assert "isMobileViewport()" in dashboard
    assert 'class="card mobile-data-card"' in dashboard
    assert "instagram-actions-cell" in dashboard


def test_instagram_mapping_form_submits_multiple_product_skus():
    template = Path("store/app/templates/dashboard.html").read_text()
    dashboard = Path("store/app/static/js/dashboard.js").read_text()

    assert 'id="instagram-product-skus"' in template
    assert 'id="instagram-product-skus" class="hidden" multiple' in template
    assert "product_skus: productSkus" in dashboard
    assert "selectedOptions" in dashboard


def test_instagram_product_picker_shows_name_brand_and_sku():
    dashboard = Path("store/app/static/js/dashboard.js").read_text()

    assert "function instagramProductLabel(product)" in dashboard
    assert "[product.name, product.brand, product.sku]" in dashboard
    assert ".join(' - ')" in dashboard
    assert "escapeHtml(instagramProductLabel(product))" in dashboard


def test_customer_table_rows_have_subtle_dividers():
    styles = Path("store/app/static/css/dashboard.css").read_text()

    assert ".customers-table tbody tr + tr td" in styles
    assert "border-top: 1px solid #f1f5f9" in styles
    assert ".dark .customers-table tbody tr + tr td" in styles


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


def test_story_content_renders_preview_and_mapping_status():
    dashboard = Path("store/app/static/js/dashboard.js").read_text()

    assert "mapping.preview_url" in dashboard
    assert "view.mapped ? 'Mapeado' : 'Sin productos'" in dashboard
    assert "lifecycleStatus === 'expired'" in dashboard


def test_story_edit_assigns_products_without_submitting_permalink():
    template = Path("store/app/templates/dashboard.html").read_text()
    dashboard = Path("store/app/static/js/dashboard.js").read_text()

    assert 'id="instagram-url-help"' in template
    assert "const editingStory = mapping?.content_type === 'story'" in dashboard
    assert "if (!editingStory) body.post_url = postUrl" in dashboard
    assert "postUrlInput.disabled = editingStory" in dashboard


def test_story_mapping_form_accepts_manual_story_urls_and_reports_disabled_discovery():
    template = Path("store/app/templates/dashboard.html").read_text()
    dashboard = Path("store/app/static/js/dashboard.js").read_text()

    assert "URL del post, Reel o Historia" in template
    assert "instagram.com/stories/usuario/123456789/" in template
    assert "meta-instagram-context/status" in dashboard
    assert "!_instagramContextStatus.story_enabled" in dashboard
    assert "El descubrimiento automático de Historias está desactivado" in dashboard
    assert "mapping.status === 'active' && !mapping.is_expired" in dashboard
