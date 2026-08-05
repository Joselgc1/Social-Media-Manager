"""
PDF catalog generator.
Creates a formatted product catalog PDF from the in-memory product data.
Saves to app/static/catalog/catalog.pdf — served as a static file.
"""

import hashlib
import json
import logging
import os
import tempfile
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock

from fpdf import FPDF, XPos, YPos

from app.catalog.sheets import group_catalog_products
from app.config import get_config

logger = logging.getLogger(__name__)

_CATALOG_DIR = Path(__file__).resolve().parent.parent / "static" / "catalog"
PDF_PATH = _CATALOG_DIR / "catalog.pdf"
META_PATH = _CATALOG_DIR / "catalog_meta.json"
_GENERATION_LOCK = Lock()

# Brand palette (RGB)
_PINK = (208, 93, 140)
_PINK_LIGHT = (245, 220, 235)
_DARK = (40, 40, 40)
_GRAY = (120, 120, 120)
_ROW_ALT = (252, 248, 251)
_WHITE = (255, 255, 255)


def get_pdf_metadata() -> dict:
    """Return info about the last generated PDF."""
    if META_PATH.exists():
        try:
            return json.loads(META_PATH.read_text())
        except Exception:
            pass
    return {"generated_at": None, "product_count": 0, "catalog_fingerprint": None}


def catalog_fingerprint(catalog: list[dict]) -> str:
    """Return a deterministic fingerprint of customer-visible catalog content."""
    public_rows = _build_public_catalog_rows(catalog)
    serialized = json.dumps(public_rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def is_catalog_pdf_current(catalog: list[dict]) -> bool:
    """Return whether both generated artifacts match the current catalog."""
    if not PDF_PATH.is_file() or not META_PATH.is_file() or not catalog:
        return False
    metadata = get_pdf_metadata()
    return metadata.get("catalog_fingerprint") == catalog_fingerprint(catalog)


def ensure_catalog_pdf(catalog: list[dict]) -> Path:
    """Generate the PDF only when the current artifact does not match the catalog."""
    if not catalog:
        raise ValueError("Catalog is empty, cannot generate PDF.")
    with _GENERATION_LOCK:
        if is_catalog_pdf_current(catalog):
            return PDF_PATH
        return _generate_catalog_pdf(catalog)


def invalidate_catalog_pdf() -> None:
    """Remove generated artifacts before loading a deployment's catalog."""
    with _GENERATION_LOCK:
        for path in (PDF_PATH, META_PATH):
            with suppress(FileNotFoundError):
                path.unlink()


def generate_catalog_pdf(catalog: list[dict]) -> Path:
    """Build the PDF and save it."""
    if not catalog:
        raise ValueError("Catalog is empty, cannot generate PDF.")
    with _GENERATION_LOCK:
        return _generate_catalog_pdf(catalog)


def _generate_catalog_pdf(catalog: list[dict]) -> Path:
    """Build and atomically publish a PDF and its matching metadata."""
    _CATALOG_DIR.mkdir(parents=True, exist_ok=True)

    pdf = _CatalogPDF(orientation="P", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=22)
    pdf.add_page()

    _render_header(pdf)
    public_catalog = _build_public_catalog_rows(catalog)

    categories: dict[str, list[dict]] = {}
    for product in public_catalog:
        category = product.get("category") or "Otros"
        categories.setdefault(category, []).append(product)

    for category in sorted(categories):
        _render_category(pdf, category, categories[category])

    _render_footer_note(pdf)

    metadata = {
        "generated_at": datetime.now(UTC).isoformat(),
        "product_count": len(group_catalog_products(catalog)),
        "catalog_fingerprint": catalog_fingerprint(catalog),
    }

    pdf_fd, pdf_temp_name = tempfile.mkstemp(prefix=".catalog-", suffix=".pdf", dir=_CATALOG_DIR)
    meta_fd, meta_temp_name = tempfile.mkstemp(prefix=".catalog-meta-", suffix=".json", dir=_CATALOG_DIR)
    os.close(pdf_fd)
    os.close(meta_fd)
    pdf_temp = Path(pdf_temp_name)
    meta_temp = Path(meta_temp_name)
    try:
        pdf.output(str(pdf_temp))
        meta_temp.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
        pdf_temp.replace(PDF_PATH)
        meta_temp.replace(META_PATH)
    finally:
        pdf_temp.unlink(missing_ok=True)
        meta_temp.unlink(missing_ok=True)

    logger.info("Catalog PDF generated: %s grouped products -> %s", metadata["product_count"], PDF_PATH)
    return PDF_PATH


def _render_header(pdf: FPDF) -> None:
    """Render the configured store name in the catalog banner."""
    pdf.set_fill_color(*_PINK)
    pdf.rect(0, 0, 210, 38, "F")

    pdf.set_y(9)
    pdf.set_text_color(*_WHITE)
    pdf.set_font("Helvetica", "B", 20)
    store_name = str(get_config().store_name or "Tienda").strip() or "Tienda"
    pdf.cell(0, 11, store_name[:60], align="C", new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 7, "Catalogo de Productos Disponibles", align="C", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(14)
    pdf.set_text_color(*_DARK)


def _render_category(pdf: FPDF, category: str, products: list[dict]) -> None:
    """Render one category with generic retail variant columns."""
    pdf.set_fill_color(*_PINK)
    pdf.set_text_color(*_WHITE)
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(0, 7, f"  {category.upper()}", fill=True, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(1)

    pdf.set_fill_color(*_PINK_LIGHT)
    pdf.set_text_color(*_GRAY)
    pdf.set_font("Helvetica", "B", 7)
    _row(pdf, "Producto", "Marca", "Presentacion", "Precio", fill=True)
    pdf.set_text_color(*_DARK)

    for index, product in enumerate(products):
        pdf.set_fill_color(*(_ROW_ALT if index % 2 == 0 else _WHITE))
        price_str = _price_text(product.get("price_usd"))
        name = str(product.get("product_name", ""))[:40]
        brand = str(product.get("brand", ""))[:24]
        presentation = str(product.get("presentation", ""))[:26]
        pdf.set_font("Helvetica", "", 7)
        _row(pdf, name, brand, presentation, price_str, fill=True)
        pdf.set_text_color(*_DARK)

    pdf.ln(5)


def _row(pdf: FPDF, name: str, brand: str, presentation: str, price: str, fill: bool = False) -> None:
    """Render a single generic catalog table row."""
    height = 6
    pdf.cell(80, height, name, fill=fill, border=0)
    pdf.cell(42, height, brand, fill=fill, border=0)
    pdf.cell(48, height, presentation, fill=fill, border=0)
    pdf.cell(28, height, price, fill=fill, border=0, align="R")
    pdf.ln()


def _price_text(value) -> str:
    try:
        return f"${float(value):.2f}"
    except (TypeError, ValueError):
        return ""


def _render_footer_note(pdf: FPDF) -> None:
    """Small disclaimer at the very bottom of the last page."""
    pdf.set_y(-18)
    pdf.set_font("Helvetica", "I", 7)
    pdf.set_text_color(*_GRAY)
    generated = datetime.now().strftime("%d/%m/%Y %H:%M")
    pdf.cell(
        0,
        5,
        f"Actualizado: {generated}  |  Entrega a domicilio en Valencia metropolitana y agencias MRW/Zoom  "
        "|  Consulta disponibilidad antes de confirmar",
        align="C",
    )


def _build_public_catalog_rows(catalog: list[dict]) -> list[dict]:
    """Return stock-free customer-facing PDF rows with exact variant pricing."""
    public_rows: list[dict] = []
    for product in group_catalog_products(catalog):
        base = {
            "product_name": product.get("product_name", ""),
            "brand": product.get("brand", ""),
            "category": product.get("category", ""),
            "description": product.get("description", ""),
            "image_url": product.get("image_url", ""),
        }
        if product.get("has_variant_prices"):
            for variant in product.get("variants", []):
                if not variant.get("in_stock"):
                    continue
                public_rows.append({
                    **base,
                    "presentation": variant.get("size", ""),
                    "price_usd": variant.get("price_usd", 0),
                })
        else:
            public_rows.append({
                **base,
                "presentation": product.get("presentations", ""),
                "price_usd": product.get("price_usd", 0),
            })
    return public_rows


class _CatalogPDF(FPDF):
    """FPDF subclass that adds a page-number footer on every page."""

    def footer(self):
        self.set_y(-10)
        self.set_font("Helvetica", "I", 7)
        self.set_text_color(*_GRAY)
        self.cell(0, 5, f"Pagina {self.page_no()}", align="C")
