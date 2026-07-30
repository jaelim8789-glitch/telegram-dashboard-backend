-- Migration v003: Add AI credit columns to tenants table
-- Run: psql -U user -d dbname -f migrations/v003_add_ai_credits.sql

ALTER TABLE tenants
  ADD COLUMN IF NOT EXISTS ai_credits_remaining INTEGER NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS ai_credits_reset_tokens INTEGER NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS ai_last_refill_at TIMESTAMP;
