# Product Catalog Schema

The Store reads its live product catalog from the first worksheet of the configured Google Sheet (`PRODUCT_SHEET_ID`). The service account must have **Editor** access because checkout updates `Stock` and writes the hidden `Inventory Movements` ledger.

## Columns

Use these headers:

```text
SKU | Parent SKU | Product Name | Brand | Category | Description | Size | Price USD | Stock | Active | Image URL
```

`Brand` is optional for backward compatibility. Catalogs created before this column was introduced continue to load with an empty brand.

## Variant model

Use one row per **sellable SKU**. `Parent SKU` groups rows that are different presentations/options of the same customer-facing product.

The historical `Size` column is intentionally retained for compatibility, but it now means the product's **sellable variant/presentation value**, not only a clothing size. Examples include:

- clothing: `S`, `M`, `L`
- perfume: `30 ml`, `50 ml`, `100 ml`
- shoes: `38`, `39`, `40`
- storage: `128 GB`, `256 GB`

Values are matched case-insensitively and normalized internally, so `100 ml` is equivalent to `100 ML`.

A product that has no meaningful variant may leave `Size` blank. If there is only one unambiguous sellable SKU, checkout can resolve it without asking the customer for a presentation. If multiple sellable variants exist, the customer must choose one before checkout can finalize.

Each variant may have its own `Price USD` and `Stock`. Do not assume all rows sharing a `Parent SKU` have the same price.

## Clothing example

```text
PJ-001-S | PJ-001 | Satin Pajama | Victoria's Secret | Pajamas | Blue satin pajama | S | 28.00 | 5 | Yes | https://example.com/pj.jpg
PJ-001-M | PJ-001 | Satin Pajama | Victoria's Secret | Pajamas | Blue satin pajama | M | 28.00 | 8 | Yes | https://example.com/pj.jpg
PJ-001-L | PJ-001 | Satin Pajama | Victoria's Secret | Pajamas | Blue satin pajama | L | 28.00 | 3 | Yes | https://example.com/pj.jpg
```

The customer-facing product is `Satin Pajama`, with presentations `S`, `M`, and `L`, all at `$28.00`.

## Perfume example

```text
DIOR-SAV-50 | DIOR-SAV | Sauvage EDT | Dior | Perfumes | Fresh spicy woody fragrance | 50 ml | 85.00 | 3 | Yes | https://example.com/sauvage.jpg
DIOR-SAV-100 | DIOR-SAV | Sauvage EDT | Dior | Perfumes | Fresh spicy woody fragrance | 100 ml | 125.00 | 2 | Yes | https://example.com/sauvage.jpg
```

The customer-facing product is `Sauvage EDT` by `Dior`. The catalog preserves the exact variant prices:

- `50 ML` → `$85.00`
- `100 ML` → `$125.00`

## Product without a presentation

```text
CANDLE-001 | CANDLE-001 | Vanilla Candle | Casa | Home | Vanilla scented candle |  | 18.00 | 4 | Yes | https://example.com/candle.jpg
```

Because this is one unambiguous sellable SKU, the customer does not need to provide a `Size` value.

## Field rules

- `SKU`: required and unique per sellable row.
- `Parent SKU`: use the same value for all variants of one product. For a standalone product, it may equal `SKU` or be left blank.
- `Product Name`: customer-facing product name.
- `Brand`: optional manufacturer/brand name. It participates in catalog search.
- `Category`: customer-facing category used for search and PDF grouping.
- `Description`: free-form searchable product details. Keep specialized attributes here unless deterministic structured filtering is actually needed.
- `Size`: optional variant/presentation value as described above.
- `Price USD`: price of this exact SKU/variant.
- `Stock`: stock of this exact SKU/variant.
- `Active`: use `Yes` for sellable rows. Other values are excluded from the active catalog.
- `Image URL`: optional public image URL, Google Drive share link, or supported Google Sheets image/hyperlink formula.

## Important behavior

- Brand, category, description, product name, SKU, Parent SKU, and presentation values participate in product lookup.
- Customer-facing grouped catalog results keep variant-level prices when prices differ.
- Checkout always re-resolves the exact sellable SKU and price from the live catalog; model-supplied prices are not trusted.
- Google Sheets stock mutation, idempotency, and locking behavior are unchanged by this schema generalization.
- Instagram remains informational-only; transactional checkout and PDF catalog delivery continue through WhatsApp according to the existing channel policy.
