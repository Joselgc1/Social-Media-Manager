-- Add saved shipping address to customers table
-- so returning customers can reuse their last delivery address.

ALTER TABLE customers
ADD COLUMN IF NOT EXISTS last_shipping_address TEXT,
ADD COLUMN IF NOT EXISTS last_shipping_city TEXT,
ADD COLUMN IF NOT EXISTS last_shipping_method TEXT;
