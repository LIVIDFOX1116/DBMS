-- probe: SELECT to_regprocedure('sp_set_user_role(bigint,bigint,text)') IS NOT NULL AND to_regclass('v_flag_latest_audit') IS NOT NULL
--
-- FrameCheck migration (2026-11): a plain VIEW, two stored PROCEDUREs, and a
-- CHECK on alert review status. Nothing here drops or deletes anything.
--
-- 1. v_flag_latest_audit -- the newest audit row per flag, with the flag's
--    detector, wallet and run. "Latest audit per flag" (DISTINCT ON over
--    flag_audit, newest first, id as tiebreak) was written out three times
--    in the API (/audit/verdicts, /audit/overview, /audit/summary); it now
--    lives here once. flag_audit is append-only (migration 009) -- a
--    corrected verdict is a new row -- so "latest" is the only question
--    those endpoints ever ask.
-- 2. sp_set_user_role / sp_set_user_active -- role and activation changes
--    for the Users view, with the rules in the database rather than the
--    API: only an active admin may change them, and the LAST active admin
--    can be neither demoted nor deactivated (that would lock everyone out
--    of user management for good).
-- 3. alert.review_status normalised, then constrained to
--    NEW / IN_REVIEW / CONFIRMED / DISMISSED for the review workflow.
--
-- Idempotent: CREATE OR REPLACE, guarded UPDATEs, DROP CONSTRAINT IF EXISTS
-- before ADD.

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. The latest audit per flag
-- ---------------------------------------------------------------------------

CREATE OR REPLACE VIEW v_flag_latest_audit AS
SELECT DISTINCT ON (fa.flag_id)
       fa.flag_audit_id,
       fa.flag_id,
       fa.verdict,
       fa.framing_cost_wei,
       fa.framing_gas,
       fa.minimal_tx_set,
       fa.method,
       fa.backend,
       fa.runtime_ms,
       fa.created_at,
       f.run_id,
       f.wallet_id,
       f.detector
  FROM flag_audit fa
  JOIN flag f ON f.flag_id = fa.flag_id
 ORDER BY fa.flag_id, fa.created_at DESC, fa.flag_audit_id DESC;

COMMENT ON VIEW v_flag_latest_audit IS
  'Newest flag_audit row per flag (DISTINCT ON, created_at then flag_audit_id), with the flag''s run, wallet and detector. Read by /audit/verdicts, /audit/overview, /audit/summary.';

-- ---------------------------------------------------------------------------
-- 2. Role and activation changes, with the lockout rule in the database
-- ---------------------------------------------------------------------------

CREATE OR REPLACE PROCEDURE sp_set_user_role(p_actor_id BIGINT, p_target_id BIGINT, p_role TEXT)
LANGUAGE plpgsql AS $$
DECLARE
    actor  app_user;
    target app_user;
    admins BIGINT;
BEGIN
    SELECT * INTO actor FROM app_user WHERE user_id = p_actor_id;
    IF NOT FOUND OR actor.role <> 'admin' OR NOT actor.is_active THEN
        RAISE EXCEPTION 'only an active admin can change roles'
            USING ERRCODE = 'insufficient_privilege';
    END IF;

    IF p_role IS NULL OR p_role NOT IN ('admin', 'analyst', 'viewer') THEN
        RAISE EXCEPTION 'unknown role: %', coalesce(p_role, 'NULL')
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    -- Serialises admin-count checks, so two admins demoting each other at
    -- the same moment cannot both pass the "not the last one" test.
    PERFORM pg_advisory_xact_lock(hashtext('framecheck.active_admins'));

    SELECT * INTO target FROM app_user WHERE user_id = p_target_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'no such user: %', p_target_id USING ERRCODE = 'no_data_found';
    END IF;

    IF target.role = 'admin' AND target.is_active AND p_role <> 'admin' THEN
        SELECT count(*) INTO admins FROM app_user WHERE role = 'admin' AND is_active;
        IF admins <= 1 THEN
            RAISE EXCEPTION 'cannot demote the last active admin (user %)', p_target_id
                USING ERRCODE = 'restrict_violation',
                      HINT = 'Make another user an admin first.';
        END IF;
    END IF;

    UPDATE app_user SET role = p_role WHERE user_id = p_target_id;
END $$;

COMMENT ON PROCEDURE sp_set_user_role(BIGINT, BIGINT, TEXT) IS
  'Changes a user''s role. Actor must be an active admin; refuses to demote the last active admin.';

CREATE OR REPLACE PROCEDURE sp_set_user_active(p_actor_id BIGINT, p_target_id BIGINT, p_active BOOLEAN)
LANGUAGE plpgsql AS $$
DECLARE
    actor  app_user;
    target app_user;
    admins BIGINT;
BEGIN
    SELECT * INTO actor FROM app_user WHERE user_id = p_actor_id;
    IF NOT FOUND OR actor.role <> 'admin' OR NOT actor.is_active THEN
        RAISE EXCEPTION 'only an active admin can activate or deactivate users'
            USING ERRCODE = 'insufficient_privilege';
    END IF;

    IF p_active IS NULL THEN
        RAISE EXCEPTION 'is_active must be true or false' USING ERRCODE = 'invalid_parameter_value';
    END IF;

    PERFORM pg_advisory_xact_lock(hashtext('framecheck.active_admins'));

    SELECT * INTO target FROM app_user WHERE user_id = p_target_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'no such user: %', p_target_id USING ERRCODE = 'no_data_found';
    END IF;

    IF target.role = 'admin' AND target.is_active AND NOT p_active THEN
        SELECT count(*) INTO admins FROM app_user WHERE role = 'admin' AND is_active;
        IF admins <= 1 THEN
            RAISE EXCEPTION 'cannot deactivate the last active admin (user %)', p_target_id
                USING ERRCODE = 'restrict_violation',
                      HINT = 'Make another user an admin first.';
        END IF;
    END IF;

    -- Activating an account that has no way to sign in (no Google identity,
    -- no password) fails app_user_can_sign_in (migration 010) -- correctly.
    UPDATE app_user SET is_active = p_active WHERE user_id = p_target_id;
END $$;

COMMENT ON PROCEDURE sp_set_user_active(BIGINT, BIGINT, BOOLEAN) IS
  'Activates or deactivates (soft-deletes) a user. Actor must be an active admin; refuses to deactivate the last active admin.';

-- ---------------------------------------------------------------------------
-- 3. Alert review status
-- ---------------------------------------------------------------------------
-- Every alert so far is NEW. Anything else is normalised first so the CHECK
-- validates: case/whitespace folded; the old REVIEWED (an analyst looked,
-- outcome not recorded) becomes IN_REVIEW rather than claiming a CONFIRMED
-- the data never said; NULL or unknown becomes NEW.

UPDATE alert SET review_status = upper(btrim(review_status))
 WHERE review_status IS NOT NULL AND review_status <> upper(btrim(review_status));
UPDATE alert SET review_status = 'IN_REVIEW' WHERE review_status = 'REVIEWED';
UPDATE alert SET review_status = 'NEW'
 WHERE review_status IS NULL OR review_status NOT IN ('NEW', 'IN_REVIEW', 'CONFIRMED', 'DISMISSED');

ALTER TABLE alert DROP CONSTRAINT IF EXISTS alert_review_status_check;
ALTER TABLE alert ADD  CONSTRAINT alert_review_status_check
    CHECK (review_status IN ('NEW', 'IN_REVIEW', 'CONFIRMED', 'DISMISSED'));

COMMIT;
