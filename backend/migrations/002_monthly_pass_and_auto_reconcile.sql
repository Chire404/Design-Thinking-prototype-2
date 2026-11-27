-- ============================================================================
-- VELOPASS PROTOTYPE 2 SCHEMA UPGRADE (Migration 002)
-- Adds:
--   1. Monthly Pass Plans (`pass_plans`) & User Subscriptions (`user_passes`)
--   2. Automated Bank Account Reconciliation (`bank_sync_settings`, `bank_statement_entries`)
--   3. Upgrades `wallet_transactions` & `usage_transactions` for Monthly Pass support
-- ============================================================================

PRAGMA foreign_keys = ON;

-- 1. PASS PLANS (Configurable Transit Subscription Packages)
CREATE TABLE IF NOT EXISTS pass_plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE COLLATE NOCASE,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    price INTEGER NOT NULL CHECK (price > 0),
    duration_days INTEGER NOT NULL CHECK (duration_days > 0),
    max_trips INTEGER DEFAULT NULL CHECK (max_trips IS NULL OR max_trips > 0),
    badge_text TEXT NOT NULL DEFAULT 'UNLIMITED',
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pass_plans_active ON pass_plans(is_active, price);

-- 2. USER PASSES (Purchased Monthly / Weekly Transit Passes)
CREATE TABLE IF NOT EXISTS user_passes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pass_code TEXT NOT NULL UNIQUE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    card_id INTEGER NOT NULL REFERENCES virtual_cards(id) ON DELETE RESTRICT,
    plan_id INTEGER NOT NULL REFERENCES pass_plans(id) ON DELETE RESTRICT,
    plan_code TEXT NOT NULL,
    plan_name TEXT NOT NULL,
    price_paid INTEGER NOT NULL CHECK (price_paid >= 0),
    duration_days INTEGER NOT NULL CHECK (duration_days > 0),
    max_trips INTEGER DEFAULT NULL,
    trips_used INTEGER NOT NULL DEFAULT 0 CHECK (trips_used >= 0),
    auto_renew INTEGER NOT NULL DEFAULT 0 CHECK (auto_renew IN (0, 1)),
    starts_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE', 'EXPIRED', 'CANCELLED')),
    wallet_tx_code TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_user_passes_user_status ON user_passes(user_id, status, expires_at DESC);
CREATE INDEX IF NOT EXISTS idx_user_passes_card_active ON user_passes(card_id, status, starts_at, expires_at);

-- 3. BANK SYNC & AUTO-RECONCILIATION SETTINGS (Singleton Row)
CREATE TABLE IF NOT EXISTS bank_sync_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    auto_reconcile_enabled INTEGER NOT NULL DEFAULT 1 CHECK (auto_reconcile_enabled IN (0, 1)),
    background_poll_enabled INTEGER NOT NULL DEFAULT 0 CHECK (background_poll_enabled IN (0, 1)),
    provider TEXT NOT NULL DEFAULT 'SEPAY_API' CHECK (
        provider IN ('SEPAY_API', 'CASSO_API', 'CUSTOM_JSON_API', 'MANUAL_IMPORT_ONLY')
    ),
    api_url TEXT NOT NULL DEFAULT '',
    api_token TEXT NOT NULL DEFAULT '',
    poll_interval_seconds INTEGER NOT NULL DEFAULT 20 CHECK (poll_interval_seconds BETWEEN 10 AND 3600),
    strict_account_match INTEGER NOT NULL DEFAULT 1 CHECK (strict_account_match IN (0, 1)),
    allow_expired_grace_minutes INTEGER NOT NULL DEFAULT 120 CHECK (allow_expired_grace_minutes >= 0),
    last_synced_at TEXT DEFAULT NULL,
    last_sync_status TEXT NOT NULL DEFAULT 'IDLE',
    last_sync_message TEXT NOT NULL DEFAULT 'Chưa kết nối API ngân hàng trực tuyến — sẵn sàng đối soát file sao kê & duyệt song song.',
    updated_by_admin_id INTEGER DEFAULT NULL REFERENCES users(id) ON DELETE SET NULL,
    updated_at TEXT NOT NULL
);

-- 4. BANK STATEMENT ENTRIES (Ingested Bank Account Transactions & Auto-Match Results)
CREATE TABLE IF NOT EXISTS bank_statement_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bank_tx_id TEXT NOT NULL UNIQUE,
    source TEXT NOT NULL CHECK (source IN ('API_POLL', 'STATEMENT_IMPORT', 'WEBHOOK')),
    account_number TEXT NOT NULL DEFAULT '',
    amount_in INTEGER NOT NULL CHECK (amount_in >= 0),
    description TEXT NOT NULL,
    extracted_code TEXT NOT NULL DEFAULT '',
    tx_time TEXT NOT NULL,
    match_status TEXT NOT NULL CHECK (
        match_status IN (
            'MATCHED_AUTO_APPROVED',
            'ALREADY_SETTLED',
            'AMOUNT_MISMATCH',
            'ACCOUNT_MISMATCH',
            'UNMATCHED'
        )
    ),
    matched_topup_id INTEGER DEFAULT NULL REFERENCES topup_transactions(id) ON DELETE SET NULL,
    reconciled_note TEXT NOT NULL DEFAULT '',
    raw_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_bank_entries_status ON bank_statement_entries(match_status, id DESC);
CREATE INDEX IF NOT EXISTS idx_bank_entries_topup ON bank_statement_entries(matched_topup_id);
