-- ============================================================================
-- VELOPASS RELATIONAL DATABASE SCHEMA (Migration 001)
-- All monetary amounts are stored strictly as 64-bit INTEGER in VND.
-- ============================================================================

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL
);

-- 1. USERS
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    full_name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
    phone TEXT DEFAULT '',
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'USER' CHECK (role IN ('USER', 'ADMIN', 'SUPER_ADMIN')),
    status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE', 'SUSPENDED', 'PENDING')),
    failed_login_attempts INTEGER NOT NULL DEFAULT 0 CHECK (failed_login_attempts >= 0),
    locked_until TEXT DEFAULT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_users_role_status ON users(role, status);
CREATE INDEX IF NOT EXISTS idx_users_full_name ON users(full_name);

-- 2. SESSIONS (Revocable Server-Side Auth Sessions)
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL UNIQUE,
    csrf_token TEXT NOT NULL,
    remember_me INTEGER NOT NULL DEFAULT 0 CHECK (remember_me IN (0, 1)),
    ip_address TEXT DEFAULT '',
    user_agent TEXT DEFAULT '',
    expires_at TEXT NOT NULL,
    revoked_at TEXT DEFAULT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sessions_user_id ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_expires_at ON sessions(expires_at);

-- 3. PASSWORD RESET TOKENS
CREATE TABLE IF NOT EXISTS password_reset_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL UNIQUE,
    expires_at TEXT NOT NULL,
    used_at TEXT DEFAULT NULL,
    created_at TEXT NOT NULL
);

-- 4. WALLETS
CREATE TABLE IF NOT EXISTS wallets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE RESTRICT,
    balance INTEGER NOT NULL DEFAULT 0 CHECK (balance >= 0),
    total_topup INTEGER NOT NULL DEFAULT 0 CHECK (total_topup >= 0),
    total_spent INTEGER NOT NULL DEFAULT 0 CHECK (total_spent >= 0),
    currency TEXT NOT NULL DEFAULT 'VND' CHECK (currency = 'VND'),
    status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE', 'LOCKED')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_wallets_user_id ON wallets(user_id);

-- 5. VIRTUAL CARDS
CREATE TABLE IF NOT EXISTS virtual_cards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE RESTRICT,
    card_number TEXT NOT NULL UNIQUE,
    card_version INTEGER NOT NULL DEFAULT 1 CHECK (card_version >= 1),
    status TEXT NOT NULL DEFAULT 'INSUFFICIENT_BALANCE' CHECK (
        status IN ('ACTIVE', 'INSUFFICIENT_BALANCE', 'SUSPENDED', 'PENDING')
    ),
    issued_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_virtual_cards_status ON virtual_cards(status);

-- 6. TOP-UP TRANSACTIONS
CREATE TABLE IF NOT EXISTS topup_transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    transaction_code TEXT NOT NULL UNIQUE,
    transfer_content TEXT NOT NULL UNIQUE,
    amount INTEGER NOT NULL CHECK (amount > 0),
    bank_name TEXT NOT NULL,
    bank_short_name TEXT NOT NULL DEFAULT '',
    bank_bin TEXT NOT NULL DEFAULT '',
    account_number TEXT NOT NULL,
    account_holder TEXT NOT NULL,
    payment_provider TEXT NOT NULL DEFAULT 'BANK_TRANSFER',
    bank_reference TEXT DEFAULT NULL UNIQUE,
    user_reported_at TEXT DEFAULT NULL,
    confirmed_by_admin_id INTEGER DEFAULT NULL REFERENCES users(id) ON DELETE SET NULL,
    note TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'PENDING' CHECK (
        status IN ('PENDING', 'PROCESSING', 'SUCCESS', 'FAILED', 'CANCELLED', 'EXPIRED')
    ),
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    confirmed_at TEXT DEFAULT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_topups_user_status ON topup_transactions(user_id, status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_topups_status_created ON topup_transactions(status, created_at DESC);

-- 7. PAYMENT WEBHOOK EVENTS (Idempotency & Audit against Replay Attacks)
CREATE TABLE IF NOT EXISTS payment_webhook_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_key TEXT NOT NULL UNIQUE,
    provider TEXT NOT NULL,
    bank_reference TEXT NOT NULL UNIQUE,
    transfer_content TEXT NOT NULL,
    amount INTEGER NOT NULL CHECK (amount > 0),
    topup_id INTEGER DEFAULT NULL REFERENCES topup_transactions(id) ON DELETE SET NULL,
    status TEXT NOT NULL CHECK (status IN ('PROCESSED', 'IGNORED', 'REJECTED')),
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- 8. WALLET TRANSACTIONS (Immutable Append-Only Wallet Ledger)
CREATE TABLE IF NOT EXISTS wallet_transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    transaction_code TEXT NOT NULL UNIQUE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    wallet_id INTEGER NOT NULL REFERENCES wallets(id) ON DELETE RESTRICT,
    transaction_type TEXT NOT NULL CHECK (
        transaction_type IN ('TOPUP', 'USAGE', 'PASS_PURCHASE', 'ADJUSTMENT_CREDIT', 'ADJUSTMENT_DEBIT', 'REFUND')
    ),
    amount INTEGER NOT NULL CHECK (amount > 0),
    balance_before INTEGER NOT NULL CHECK (balance_before >= 0),
    balance_after INTEGER NOT NULL CHECK (balance_after >= 0),
    reference_type TEXT NOT NULL,
    reference_id TEXT NOT NULL,
    admin_id INTEGER DEFAULT NULL REFERENCES users(id) ON DELETE SET NULL,
    description TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_wallet_tx_user_created ON wallet_transactions(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_wallet_tx_type_created ON wallet_transactions(transaction_type, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_wallet_tx_reference ON wallet_transactions(reference_type, reference_id);

-- 9. QR TOKENS (Dynamic Single-Use Hashed Tokens)
CREATE TABLE IF NOT EXISTS qr_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    card_id INTEGER NOT NULL REFERENCES virtual_cards(id) ON DELETE RESTRICT,
    card_version INTEGER NOT NULL DEFAULT 1,
    token_hash TEXT NOT NULL UNIQUE,
    expires_at TEXT NOT NULL,
    used_at TEXT DEFAULT NULL,
    status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE', 'USED', 'EXPIRED', 'REVOKED')),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_qr_tokens_card_status ON qr_tokens(card_id, status);
CREATE INDEX IF NOT EXISTS idx_qr_tokens_user_status ON qr_tokens(user_id, status);

-- Partial unique index: at most ONE ACTIVE QR token per card at any point in time!
CREATE UNIQUE INDEX IF NOT EXISTS idx_qr_tokens_single_active_per_card
ON qr_tokens(card_id) WHERE status = 'ACTIVE';

-- 10. USAGE TRANSACTIONS (Validated Fare Deductions & 0 VND Monthly Pass Trips)
CREATE TABLE IF NOT EXISTS usage_transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    transaction_code TEXT NOT NULL UNIQUE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    card_id INTEGER NOT NULL REFERENCES virtual_cards(id) ON DELETE RESTRICT,
    qr_token_id INTEGER NOT NULL UNIQUE REFERENCES qr_tokens(id) ON DELETE RESTRICT,
    admin_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    amount INTEGER NOT NULL CHECK (amount >= 0),
    payment_method TEXT NOT NULL DEFAULT 'WALLET' CHECK (payment_method IN ('WALLET', 'MONTHLY_PASS')),
    pass_id INTEGER DEFAULT NULL,
    balance_before INTEGER NOT NULL CHECK (balance_before >= 0),
    balance_after INTEGER NOT NULL CHECK (balance_after >= 0),
    idempotency_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'SUCCESS' CHECK (status IN ('SUCCESS', 'FAILED')),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_usage_tx_user_created ON usage_transactions(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_usage_tx_admin_created ON usage_transactions(admin_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_usage_tx_created ON usage_transactions(created_at DESC);

-- 11. SCAN LOGS (Tracks both SUCCESS and FAILED gate scanner events)
CREATE TABLE IF NOT EXISTS scan_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    user_id INTEGER DEFAULT NULL REFERENCES users(id) ON DELETE SET NULL,
    card_number TEXT DEFAULT '',
    usage_transaction_id INTEGER DEFAULT NULL REFERENCES usage_transactions(id) ON DELETE SET NULL,
    idempotency_key TEXT DEFAULT '',
    status TEXT NOT NULL CHECK (status IN ('SUCCESS', 'FAILED')),
    error_code TEXT DEFAULT '',
    message TEXT NOT NULL,
    amount INTEGER NOT NULL DEFAULT 0,
    balance_before INTEGER DEFAULT NULL,
    balance_after INTEGER DEFAULT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_scan_logs_created ON scan_logs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_scan_logs_status_created ON scan_logs(status, created_at DESC);

-- 12. BANK SETTINGS (Singleton Configuration Row)
CREATE TABLE IF NOT EXISTS bank_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    bank_name TEXT NOT NULL,
    bank_short_name TEXT NOT NULL DEFAULT 'VCB',
    bank_bin TEXT NOT NULL DEFAULT '970436',
    account_number TEXT NOT NULL,
    account_holder TEXT NOT NULL,
    bank_qr_url TEXT NOT NULL DEFAULT '',
    transfer_prefix TEXT NOT NULL DEFAULT 'VP',
    payment_instructions TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    updated_by_admin_id INTEGER DEFAULT NULL REFERENCES users(id) ON DELETE SET NULL,
    updated_at TEXT NOT NULL
);

-- 13. SYSTEM SETTINGS & FARE CHANGE HISTORY
CREATE TABLE IF NOT EXISTS system_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    fare_amount INTEGER NOT NULL DEFAULT 4000 CHECK (fare_amount > 0),
    qr_ttl_seconds INTEGER NOT NULL DEFAULT 90 CHECK (qr_ttl_seconds BETWEEN 15 AND 600),
    low_balance_threshold INTEGER NOT NULL DEFAULT 20000 CHECK (low_balance_threshold >= 0),
    topup_min_amount INTEGER NOT NULL DEFAULT 4000 CHECK (topup_min_amount >= 1000),
    topup_max_amount INTEGER NOT NULL DEFAULT 10000000 CHECK (topup_max_amount >= topup_min_amount),
    topup_expiry_minutes INTEGER NOT NULL DEFAULT 30 CHECK (topup_expiry_minutes BETWEEN 5 AND 1440),
    notifications_enabled INTEGER NOT NULL DEFAULT 1 CHECK (notifications_enabled IN (0, 1)),
    updated_by_admin_id INTEGER DEFAULT NULL REFERENCES users(id) ON DELETE SET NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fare_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    old_fare INTEGER NOT NULL CHECK (old_fare > 0),
    new_fare INTEGER NOT NULL CHECK (new_fare > 0),
    reason TEXT NOT NULL DEFAULT '',
    changed_by_admin_id INTEGER DEFAULT NULL REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL
);

-- 14. NOTIFICATIONS
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    type TEXT NOT NULL,
    title TEXT NOT NULL,
    message TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    is_read INTEGER NOT NULL DEFAULT 0 CHECK (is_read IN (0, 1)),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_notifications_user_unread ON notifications(user_id, is_read, created_at DESC);

-- 15. SUPPORT REQUESTS
CREATE TABLE IF NOT EXISTS support_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    subject TEXT NOT NULL,
    message TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN', 'IN_PROGRESS', 'RESOLVED')),
    admin_note TEXT NOT NULL DEFAULT '',
    resolved_by_admin_id INTEGER DEFAULT NULL REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_support_requests_status ON support_requests(status, created_at DESC);

-- 16. AUDIT LOGS (Immutable Admin Action Trail)
CREATE TABLE IF NOT EXISTS audit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    action TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    ip_address TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_logs_created ON audit_logs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_logs_admin ON audit_logs(admin_id, created_at DESC);
