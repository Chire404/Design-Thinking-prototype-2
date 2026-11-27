-- ============================================================================
-- VELOPASS PROTOTYPE 2 UPGRADE (Migration 004)
-- 1. Adds `vehicle_plate` and Priority Passenger fields to `users`
-- 2. Adds `priority_requests` table with mandatory proof image upload (`proof_image_data`)
-- 3. Adds `requires_priority` flag to `pass_plans` & locks `MONTHLY_STUDENT` to approved priority users!
-- ============================================================================

PRAGMA foreign_keys = ON;

ALTER TABLE users ADD COLUMN vehicle_plate TEXT NOT NULL DEFAULT '';
ALTER TABLE users ADD COLUMN priority_type TEXT NOT NULL DEFAULT 'NONE' CHECK (
    priority_type IN ('NONE', 'STUDENT', 'SENIOR', 'DISABLED', 'CONTRIBUTOR', 'STAFF')
);
ALTER TABLE users ADD COLUMN priority_status TEXT NOT NULL DEFAULT 'NONE' CHECK (
    priority_status IN ('NONE', 'PENDING', 'APPROVED', 'REJECTED')
);
ALTER TABLE users ADD COLUMN priority_card_code TEXT NOT NULL DEFAULT '';
ALTER TABLE users ADD COLUMN priority_org TEXT NOT NULL DEFAULT '';
ALTER TABLE users ADD COLUMN priority_expires_at TEXT DEFAULT NULL;

CREATE INDEX IF NOT EXISTS idx_users_vehicle_plate ON users(vehicle_plate);
CREATE INDEX IF NOT EXISTS idx_users_priority_status ON users(priority_status);

ALTER TABLE pass_plans ADD COLUMN requires_priority INTEGER NOT NULL DEFAULT 0 CHECK (
    requires_priority IN (0, 1)
);

UPDATE pass_plans SET requires_priority = 1 WHERE code = 'MONTHLY_STUDENT';

CREATE TABLE IF NOT EXISTS priority_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    priority_type TEXT NOT NULL CHECK (
        priority_type IN ('STUDENT', 'SENIOR', 'DISABLED', 'CONTRIBUTOR', 'STAFF')
    ),
    id_card_code TEXT NOT NULL,
    organization_name TEXT NOT NULL,
    proof_note TEXT NOT NULL DEFAULT '',
    proof_image_data TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING' CHECK (
        status IN ('PENDING', 'APPROVED', 'REJECTED')
    ),
    admin_note TEXT NOT NULL DEFAULT '',
    reviewed_by_admin_id INTEGER DEFAULT NULL REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    reviewed_at TEXT DEFAULT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_priority_req_status ON priority_requests(status, id DESC);
CREATE INDEX IF NOT EXISTS idx_priority_req_user ON priority_requests(user_id, id DESC);
