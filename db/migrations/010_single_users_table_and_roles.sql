-- probe: SELECT to_regclass('analyst') IS NULL AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'app_user' AND column_name = 'role')
--
-- FrameCheck migration (2026-11): ONE users table, with roles.
--
-- Until now there were two: `analyst` (schema.sql -- username, email, a
-- bcrypt-sized password_hash CHAR(60), a role; one seeded `system` row that
-- every pipeline run points at) and `app_user` (migration 006 -- the people
-- who sign in with Google, with no role at all). Authorisation needs a role
-- on the row a person actually signs in as, and two tables of people is one
-- too many to keep consistent. So `app_user` becomes the Users table:
--
--   1. it gains role (admin | analyst | viewer), username, password_hash
--      (bcrypt, for local accounts), organisation and is_active;
--      google_sub becomes nullable, because a local account has none;
--   2. every analyst row is copied in (matched on username), the `system`
--      service account as an INACTIVE admin -- it owns pipeline runs, nobody
--      signs in as it;
--   3. the three foreign keys that pointed at analyst -- analysis_run.analyst_id,
--      wallet_label.tagged_by, alert.reviewed_by -- are widened to BIGINT,
--      remapped analyst_id -> user_id, and repointed at app_user(user_id).
--      The column names stay, so nothing reading them changes;
--   4. analyst is dropped (last block, on its own).
--
-- Idempotent like every migration here. Step 2-3's data move runs only while
-- analysis_run's foreign key still references analyst, so running this file
-- a second time can never remap ids that have already been remapped.
--
-- Take a backup first (scripts/backup_db.ps1, docs/BACKUP_RESTORE.md):
-- step 4 drops a table.

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. app_user becomes the Users table
-- ---------------------------------------------------------------------------

ALTER TABLE app_user ADD COLUMN IF NOT EXISTS role          VARCHAR(20) NOT NULL DEFAULT 'viewer';
ALTER TABLE app_user ADD COLUMN IF NOT EXISTS username      VARCHAR(40);
ALTER TABLE app_user ADD COLUMN IF NOT EXISTS password_hash VARCHAR(255);
ALTER TABLE app_user ADD COLUMN IF NOT EXISTS organisation  VARCHAR(80);
ALTER TABLE app_user ADD COLUMN IF NOT EXISTS is_active     BOOLEAN NOT NULL DEFAULT TRUE;

-- A local account has no Google identity.
ALTER TABLE app_user ALTER COLUMN google_sub DROP NOT NULL;

ALTER TABLE app_user DROP CONSTRAINT IF EXISTS app_user_role_check;
ALTER TABLE app_user ADD  CONSTRAINT app_user_role_check CHECK (role IN ('admin', 'analyst', 'viewer'));

ALTER TABLE app_user DROP CONSTRAINT IF EXISTS app_user_username_key;
ALTER TABLE app_user ADD  CONSTRAINT app_user_username_key UNIQUE (username);

-- Every ACTIVE user must have a way to sign in: Google, or a password. An
-- inactive row (the `system` service account, a deactivated person) needs
-- neither.
ALTER TABLE app_user DROP CONSTRAINT IF EXISTS app_user_can_sign_in;
ALTER TABLE app_user ADD  CONSTRAINT app_user_can_sign_in
    CHECK (google_sub IS NOT NULL OR password_hash IS NOT NULL OR NOT is_active);

COMMENT ON COLUMN app_user.role IS 'admin | analyst | viewer -- read on every request, never trusted from the session token';
COMMENT ON COLUMN app_user.password_hash IS 'bcrypt (cost 12) for local accounts; NULL for Google-only accounts. Never returned by the API';

-- ---------------------------------------------------------------------------
-- 2 + 3. Fold analyst into app_user and repoint its three foreign keys
-- ---------------------------------------------------------------------------
-- Only while the old foreign key still exists: after the first successful
-- run it is gone, and this block does nothing.

DO $$
BEGIN
    IF to_regclass('analyst') IS NOT NULL AND EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'analysis_run_analyst_id_fkey'
           AND confrelid = to_regclass('analyst')
    ) THEN
        INSERT INTO app_user (username, email, password_hash, role, organisation, is_active, created_at)
        SELECT a.username,
               a.email,
               NULL,                              -- analyst.password_hash was a placeholder, never a usable hash
               CASE WHEN a.role IN ('admin', 'analyst', 'viewer') THEN a.role ELSE 'analyst' END,
               a.organisation,
               FALSE,                             -- no analyst row could ever sign in; activating one is a deliberate act
               a.created_at::timestamptz
          FROM analyst a
        ON CONFLICT (username) DO NOTHING;

        CREATE TEMP TABLE analyst_user_map ON COMMIT DROP AS
            SELECT a.analyst_id, u.user_id
              FROM analyst a JOIN app_user u ON u.username = a.username;

        ALTER TABLE analysis_run DROP CONSTRAINT IF EXISTS analysis_run_analyst_id_fkey;
        ALTER TABLE wallet_label DROP CONSTRAINT IF EXISTS wallet_label_tagged_by_fkey;
        ALTER TABLE alert        DROP CONSTRAINT IF EXISTS alert_reviewed_by_fkey;

        ALTER TABLE analysis_run ALTER COLUMN analyst_id  TYPE BIGINT;
        ALTER TABLE wallet_label ALTER COLUMN tagged_by   TYPE BIGINT;
        ALTER TABLE alert        ALTER COLUMN reviewed_by TYPE BIGINT;

        UPDATE analysis_run r SET analyst_id  = m.user_id FROM analyst_user_map m WHERE r.analyst_id  = m.analyst_id;
        UPDATE wallet_label w SET tagged_by   = m.user_id FROM analyst_user_map m WHERE w.tagged_by   = m.analyst_id;
        UPDATE alert        a SET reviewed_by = m.user_id FROM analyst_user_map m WHERE a.reviewed_by = m.analyst_id;
    END IF;
END $$;

-- The final shape, stated plainly (and harmless to repeat).
ALTER TABLE analysis_run ALTER COLUMN analyst_id  TYPE BIGINT;
ALTER TABLE wallet_label ALTER COLUMN tagged_by   TYPE BIGINT;
ALTER TABLE alert        ALTER COLUMN reviewed_by TYPE BIGINT;

ALTER TABLE analysis_run DROP CONSTRAINT IF EXISTS analysis_run_analyst_id_fkey;
ALTER TABLE analysis_run ADD  CONSTRAINT analysis_run_analyst_id_fkey FOREIGN KEY (analyst_id) REFERENCES app_user (user_id);
ALTER TABLE wallet_label DROP CONSTRAINT IF EXISTS wallet_label_tagged_by_fkey;
ALTER TABLE wallet_label ADD  CONSTRAINT wallet_label_tagged_by_fkey FOREIGN KEY (tagged_by) REFERENCES app_user (user_id);
ALTER TABLE alert        DROP CONSTRAINT IF EXISTS alert_reviewed_by_fkey;
ALTER TABLE alert        ADD  CONSTRAINT alert_reviewed_by_fkey FOREIGN KEY (reviewed_by) REFERENCES app_user (user_id);

COMMENT ON COLUMN analysis_run.analyst_id IS 'references app_user (user_id) -- the user who ran it; pipeline runs belong to the inactive `system` account';
COMMENT ON COLUMN wallet_label.tagged_by  IS 'references app_user (user_id)';
COMMENT ON COLUMN alert.reviewed_by      IS 'references app_user (user_id)';

COMMIT;

-- ---------------------------------------------------------------------------
-- 4. Drop analyst  (DESTRUCTIVE -- back up first)
-- ---------------------------------------------------------------------------
-- Nothing references it any more (step 3), and every row it held now lives in
-- app_user. Kept in its own transaction so the fold above stands even if this
-- is refused.

BEGIN;
DROP TABLE IF EXISTS analyst;
COMMIT;
