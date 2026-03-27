"""
PDF catalog generator.
Creates a formatted product catalog PDF from the in-memory product data.
Saves to app/static/catalog/catalog.pdf — served as a static file.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from fpdf import FPDF

logger = logging.getLogger(__name__)

_CATALOG_DIR = Path(__file__).resolve().parent.parent / "static" / "catalog"
PDF_PATH = _CATALOG_DIR / "catalog.pdf"
META_PATH = _CATALOG_DIR / "catalog_meta.json"

# Brand palette (RGB)
_PINK     = (208, 93, 140)
_PINK_LIGHT = (245, 220, 235)
_DARK     = (40, 40, 40)
_GRAY     = (120, 120, 120)
_ROW_ALT  = (252, 248, 251)
_WHITE    = (255, 255, 255)


# ── Public helpers ────────────────────────────────────────────

def get_pdf_metadata() -> dict:
    """Return info about the last generated PDF."""
    if META_PATH.exists():
        try:
            return json.loads(META_PATH.read_text())
        except Exception:
            pass
    return {"generated_at": None, "product_count": 0}


def generate_catalog_pdf(catalog: list[dict]) -> Path:
    """
    Build the PDF and save it.  Returns the path.
    Called by the scheduler, admin endpoints, and Telegram command.
    """
    _CATALOG_DIR.mkdir(parents=True, exist_ok=True)

    pdf = _CatalogPDF(orientation="P", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=22)
    pdf.add_page()

    _render_header(pdf)

    # Group products by category, sorted alphabetically
    categories: dict[str, list[dict]] = {}
    for p in catalog:
        cat = p.get("category") or "Otros"
        categories.setdefault(cat, []).append(p)

    for category in sorted(categories):
        _render_category(pdf, category, categories[category])

    _render_footer_note(pdf)

    pdf.output(str(PDF_PATH))

    META_PATH.write_text(json.dumps({
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "product_count": len(catalog),
    }, indent=2))

    logger.info(f"Catalog PDF generated: {len(catalog)} products → {PDF_PATH}")
    return PDF_PATH


# ── Private rendering helpers ─────────────────────────────────

def _render_header(pdf: "FPDF"):
    """Full-width pink banner at the top of the first page."""
    pdf.set_fill_color(*_PINK)
    pdf.rect(0, 0, 210, 38, "F")

    pdf.set_y(9)
    pdf.set_text_color(*_WHITE)
    pdf.set_font("Helvetica", "B", 20)
    pdf.cell(0, 11, "Victoria's Secret Collection", align="C", ln=True)

    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 7, "Catalogo de Productos Disponibles", align="C", ln=True)
    pdf.ln(14)

    pdf.set_text_color(*_DARK)


def _render_category(pdf: "FPDF", category: str, products: list[dict]):
    """Render one category with its product table."""
    # Category header bar
    pdf.set_fill_color(*_PINK)
    pdf.set_text_color(*_WHITE)
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(0, 7, f"  {category.upper()}", fill=True, ln=True)
    pdf.ln(1)

    # Column headers
    pdf.set_fill_color(*_PINK_LIGHT)
    pdf.set_text_color(*_GRAY)
    pdf.set_font("Helvetica", "B", 7)
    _row(pdf, "SKU", "Producto", "Tallas", "Precio", "Stock", fill=True)

    pdf.set_text_color(*_DARK)

    for i, p in enumerate(products):
        if i % 2 == 0:
            pdf.set_fill_color(*_ROW_ALT)
        else:
            pdf.set_fill_color(*_WHITE)

        in_stock = _parse_stock(p.get("stock", 0)) > 0
        stock_label = str(_parse_stock(p.get("stock", 0))) if in_stock else "Agotado"

        try:
            price_str = f"${float(p.get('price_usd', 0)):.2f}"
        except (ValueError, TypeError):
            price_str = f"${p.get('price_usd', 0)}"

        sku   = str(p.get("sku", ""))[:10]
        name  = str(p.get("product_name", ""))[:42]
        sizes = str(p.get("sizes", ""))[:22]

        pdf.set_font("Helvetica", "", 7)

        if not in_stock:
            pdf.set_text_color(*_GRAY)

        _row(pdf, sku, name, sizes, price_str, stock_label, fill=True)

        pdf.set_text_color(*_DARK)

    pdf.ln(5)


def _row(pdf: "FPDF", sku: str, name: str, sizes: str, price: str, stock: str, fill: bool = False):
    """Render a single table row."""
    h = 6
    pdf.cell(16,  h, sku,   fill=fill, border=0)
    pdf.cell(74,  h, name,  fill=fill, border=0)
    pdf.cell(38,  h, sizes, fill=fill, border=0)
    pdf.cell(28,  h, price, fill=fill, border=0, align="R")
    pdf.cell(30,  h, stock, fill=fill, border=0, align="C", ln=True)


def _render_footer_note(pdf: "FPDF"):
    """Small disclaimer at the very bottom of the last page."""
    pdf.set_y(-18)
    pdf.set_font("Helvetica", "I", 7)
    pdf.set_text_color(*_GRAY)
    generated = datetime.now().strftime("%d/%m/%Y %H:%M")
    pdf.cell(0, 5,
             f"Actualizado: {generated}  |  Precios incluyen delivery nacional  "
             f"|  Consulta disponibilidad antes de confirmar",
             align="C")


def _parse_stock(value) -> int:
    try:
        return int(value)
    except (ValueError, TypeError):
        return 0


class _CatalogPDF(FPDF):
    """FPDF subclass — adds a page-number footer on every page."""

    def footer(self):
        self.set_y(-10)
        self.set_font("Helvetica", "I", 7)
        self.set_text_color(*_GRAY)
        self.cell(0, 5, f"Pagina {self.page_no()}", align="C")
