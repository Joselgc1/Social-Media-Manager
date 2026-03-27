-- Migration 003: Add shipping_address column to orders
-- Run this in the Supabase SQL Editor

ALTER TABLE orders ADD COLUMN IF NOT EXISTS shipping_address TEXT;
