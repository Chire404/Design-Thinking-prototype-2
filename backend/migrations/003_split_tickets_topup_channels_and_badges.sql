-- ============================================================================
-- VELOPASS PROTOTYPE 2 UPGRADE (Migration 003)
-- 1. Separates QR Ticket Modes: `SINGLE` (Vé lượt) vs `MONTHLY` (Vé tháng)
-- 2. Splits Top-up Requests: `AUTO` (Bank transfer auto-reconcile) vs `MANUAL` (Cash, counter, e-wallet...)
--    + Direct Top-up for Monthly Pass (`purpose = 'MONTHLY_PASS'`, `target_plan_id`)
-- 3. Adds explicit `approval_mode` ('AUTO' | 'MANUAL') on `topup_transactions` & `wallet_transactions`
--    so Admin Transactions clearly display TỰ ĐỘNG vs THỦ CÔNG badges!
-- ============================================================================

PRAGMA foreign_keys = ON;

-- 1. QR Tokens: explicit ticket_mode ('SINGLE' = Vé lượt, 'MONTHLY' = Vé tháng)
-- Drop single-active-per-card index so User can hold 1 active SINGLE QR and 1 active MONTHLY QR independently!
DROP INDEX IF EXISTS idx_qr_tokens_single_active_per_card;

ALTER TABLE qr_tokens ADD COLUMN ticket_mode TEXT NOT NULL DEFAULT 'SINGLE' CHECK (
    ticket_mode IN ('SINGLE', 'MONTHLY')
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_qr_tokens_active_per_card_mode
ON qr_tokens(card_id, ticket_mode) WHERE status = 'ACTIVE';

-- 2. Top-up Transactions: split AUTO vs MANUAL request channels + Monthly Pass direct top-up target
ALTER TABLE topup_transactions ADD COLUMN topup_channel TEXT NOT NULL DEFAULT 'AUTO' CHECK (
    topup_channel IN ('AUTO', 'MANUAL')
);
ALTER TABLE topup_transactions ADD COLUMN payment_method_detail TEXT NOT NULL DEFAULT 'BANK_TRANSFER';
ALTER TABLE topup_transactions ADD COLUMN purpose TEXT NOT NULL DEFAULT 'WALLET' CHECK (
    purpose IN ('WALLET', 'MONTHLY_PASS')
);
ALTER TABLE topup_transactions ADD COLUMN target_plan_id INTEGER DEFAULT NULL REFERENCES pass_plans(id) ON DELETE SET NULL;
ALTER TABLE topup_transactions ADD COLUMN approval_mode TEXT DEFAULT NULL CHECK (
    approval_mode IS NULL OR approval_mode IN ('AUTO', 'MANUAL')
);

CREATE INDEX IF NOT EXISTS idx_topups_channel_status ON topup_transactions(topup_channel, status, id DESC);

-- 3. Wallet Transactions: explicit approval_mode ('AUTO' | 'MANUAL') for Admin Transaction Badges
ALTER TABLE wallet_transactions ADD COLUMN approval_mode TEXT NOT NULL DEFAULT 'AUTO' CHECK (
    approval_mode IN ('AUTO', 'MANUAL')
);

CREATE INDEX IF NOT EXISTS idx_wallet_tx_approval_mode ON wallet_transactions(approval_mode, id DESC);
