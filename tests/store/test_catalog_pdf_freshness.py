import json


def _catalog(name: str, price: float) -> list[dict]:
    return [{
        "sku": f"SKU-{name}",
        "product_name": name,
        "category": "Pijamas",
        "size": "M",
        "sizes": "M",
        "price_usd": price,
        "stock": 2,
    }]


def _patch_artifact_paths(monkeypatch, tmp_path):
    from app.catalog import pdf_generator

    monkeypatch.setattr(pdf_generator, "_CATALOG_DIR", tmp_path)
    monkeypatch.setattr(pdf_generator, "PDF_PATH", tmp_path / "catalog.pdf")
    monkeypatch.setattr(pdf_generator, "META_PATH", tmp_path / "catalog_meta.json")
    return pdf_generator


def test_pdf_from_another_store_is_not_considered_current(monkeypatch, tmp_path):
    pdf_generator = _patch_artifact_paths(monkeypatch, tmp_path)
    store_a = _catalog("Pijama A", 20)
    store_b = _catalog("Pijama B", 35)

    pdf_generator.generate_catalog_pdf(store_a)

    assert pdf_generator.is_catalog_pdf_current(store_a) is True
    assert pdf_generator.is_catalog_pdf_current(store_b) is False

    pdf_generator.ensure_catalog_pdf(store_b)
    metadata = json.loads(pdf_generator.META_PATH.read_text())
    assert metadata["catalog_fingerprint"] == pdf_generator.catalog_fingerprint(store_b)
    assert pdf_generator.is_catalog_pdf_current(store_b) is True


def test_legacy_metadata_without_fingerprint_is_rejected(monkeypatch, tmp_path):
    pdf_generator = _patch_artifact_paths(monkeypatch, tmp_path)
    pdf_generator.PDF_PATH.write_bytes(b"stale-pdf")
    pdf_generator.META_PATH.write_text(json.dumps({"generated_at": "2026-01-01", "product_count": 6}))

    assert pdf_generator.is_catalog_pdf_current(_catalog("Current", 25)) is False


def test_startup_invalidation_removes_both_generated_artifacts(monkeypatch, tmp_path):
    pdf_generator = _patch_artifact_paths(monkeypatch, tmp_path)
    pdf_generator.PDF_PATH.write_bytes(b"stale-pdf")
    pdf_generator.META_PATH.write_text("{}")

    pdf_generator.invalidate_catalog_pdf()

    assert not pdf_generator.PDF_PATH.exists()
    assert not pdf_generator.META_PATH.exists()
