-- 009_framecheck_audit_trail.sql
--
-- Feature 2: make the audit trail the DATABASE's guarantee, not the
-- application's convention.
--
-- Why this is the feature. FrameCheck's entire output is an audit: rows in
-- flag_audit saying a flag either could or could not have been manufactured
-- by an outsider, and at what cost. That verdict is only worth something if
-- it can be shown not to have been changed after the fact -- and until this
-- migration, nothing enforced that. flag_audit was append-only because the
-- Python that writes it happens never to issue an UPDATE. One statement from
-- any client with table access would rewrite a verdict and leave no trace.
--
-- What "tamper-evident" means here, precisely. Not "nobody can change it" --
-- a superuser can always change anything, and pretending otherwise would be
-- the dishonest version of this feature. It means: no change can happen
-- WITHOUT LEAVING EVIDENCE. Three mechanisms, in order of strength:
--
--   1. UPDATE on flag_audit is refused outright. There is no legitimate
--      update: a corrected verdict is a new audit row, and the API already
--      reports the latest one per flag.
--   2. Every audit row is hash-chained to the one before it. Editing a row
--      by any means -- direct SQL, a restored backup, a hand-edited dump --
--      breaks the chain at that row and at every row after it, and
--      flag_audit_chain_verify() names the first row that does not fit.
--   3. DELETE is refused by default, and permitted only when a session says
--      so explicitly (framecheck.allow_audit_delete), in which case the
--      deletion is recorded in flag_audit_tombstone with who did it and why.
--      This project's own tooling needs that door -- benchmark_audit.py
--      removes the rows it just created, run_experiment.py clears a run
--      before re-running it -- and a door with a log on it is worth more
--      than a wall somebody has to tunnel under.
--
-- And because a verdict is a claim about a flag AT A MOMENT, `flag` becomes
-- system-versioned: every before-image lands in flag_history with the period
-- it was valid for, and flag_as_of(timestamptz) answers "what did this flag
-- look like when the audit was signed" as a query rather than as Python
-- reconstructing history from logs.
--
-- Idempotent, like every migration here: safe to run on a database that
-- already has it.

-- ---------------------------------------------------------------------------
-- 1. The hash chain on flag_audit
-- ---------------------------------------------------------------------------

ALTER TABLE flag_audit ADD COLUMN IF NOT EXISTS prev_hash BYTEA;
ALTER TABLE flag_audit ADD COLUMN IF NOT EXISTS row_hash  BYTEA;

COMMENT ON COLUMN flag_audit.prev_hash IS
  'row_hash of the preceding audit row; the genesis row links to a fixed seed';
COMMENT ON COLUMN flag_audit.row_hash IS
  'sha256(prev_hash || canonical payload) -- see flag_audit_payload()';

-- The canonical byte string a row's hash is taken over.
--
-- One function, used by BOTH the insert trigger and the verifier, because
-- two separate serializations would drift and the chain would start failing
-- on rows nobody had touched. Columns are listed explicitly rather than
-- taken from to_jsonb(row): a column added by a later migration must not
-- silently change the hash of every row already written.
--
-- prev_hash and row_hash are themselves excluded, for the obvious reason.
-- flag_audit_id is included, so a row cannot be renumbered or reordered
-- without breaking its own hash.
CREATE OR REPLACE FUNCTION flag_audit_payload(a flag_audit) RETURNS BYTEA
LANGUAGE sql IMMUTABLE AS $$
    SELECT convert_to(concat_ws('|',
        a.flag_audit_id::text,
        a.flag_id::text,
        a.verdict,
        coalesce(a.framing_cost_wei::text, ''),
        coalesce(a.framing_gas::text, ''),
        coalesce(a.minimal_tx_set::text, ''),
        a.method,
        coalesce(a.backend, ''),
        coalesce(a.runtime_ms::text, ''),
        to_char(a.created_at, 'YYYY-MM-DD HH24:MI:SS.US')
    ), 'UTF8')
$$;

CREATE OR REPLACE FUNCTION flag_audit_chain_seed() RETURNS BYTEA
LANGUAGE sql IMMUTABLE AS $$
    SELECT sha256(convert_to('framecheck-audit-chain-v1', 'UTF8'))
$$;

CREATE OR REPLACE FUNCTION flag_audit_chain_link() RETURNS TRIGGER
LANGUAGE plpgsql AS $$
DECLARE
    prev BYTEA;
BEGIN
    -- Serialize appends for the rest of the transaction. Without this, two
    -- concurrent inserts both read the same tail row and the chain forks:
    -- two rows claiming the same predecessor, and a verifier that cannot say
    -- which one is the real successor. The audit engine writes in batches
    -- from one process today, so this costs nothing now and is the
    -- difference between a chain and a guess the moment it does not.
    PERFORM pg_advisory_xact_lock(hashtext('framecheck.flag_audit_chain'));

    SELECT row_hash INTO prev
      FROM flag_audit
     WHERE row_hash IS NOT NULL
     ORDER BY flag_audit_id DESC
     LIMIT 1;

    NEW.prev_hash := coalesce(prev, flag_audit_chain_seed());
    NEW.row_hash  := sha256(NEW.prev_hash || flag_audit_payload(NEW));
    RETURN NEW;
END $$;

-- Backfill before the guard trigger exists, since the backfill is an UPDATE
-- and the guard is about to make those impossible. Rows written before this
-- migration get hashes in id order, so an existing database ends up with a
-- chain that verifies rather than one that is permanently "broken at row 1".
--
-- What this does NOT claim: a hash added today proves nothing about what
-- happened to a row yesterday. It establishes the baseline from which
-- tampering becomes detectable.
DO $$
DECLARE
    r    flag_audit;
    prev BYTEA;
BEGIN
    SELECT row_hash INTO prev
      FROM flag_audit
     WHERE row_hash IS NOT NULL
     ORDER BY flag_audit_id DESC
     LIMIT 1;
    prev := coalesce(prev, flag_audit_chain_seed());

    FOR r IN SELECT * FROM flag_audit WHERE row_hash IS NULL ORDER BY flag_audit_id LOOP
        r.prev_hash := prev;
        r.row_hash  := sha256(prev || flag_audit_payload(r));
        UPDATE flag_audit
           SET prev_hash = r.prev_hash, row_hash = r.row_hash
         WHERE flag_audit_id = r.flag_audit_id;
        prev := r.row_hash;
    END LOOP;
END $$;

DROP TRIGGER IF EXISTS flag_audit_chain ON flag_audit;
CREATE TRIGGER flag_audit_chain
    BEFORE INSERT ON flag_audit
    FOR EACH ROW EXECUTE FUNCTION flag_audit_chain_link();

-- ---------------------------------------------------------------------------
-- 2. Append-only, with a logged door rather than a wall
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS flag_audit_tombstone (
    tombstone_id   BIGSERIAL PRIMARY KEY,
    flag_audit_id  BIGINT NOT NULL,     -- no FK: the row it names is gone
    flag_id        BIGINT NOT NULL,
    verdict        VARCHAR(12),
    row_hash       BYTEA,               -- what the chain expected to find here
    deleted_by     TEXT NOT NULL,
    deleted_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    reason         TEXT NOT NULL
);

COMMENT ON TABLE flag_audit_tombstone IS
  'Every audit row ever deleted, with who deleted it and why. A gap in the '
  'chain with no tombstone is tampering; a gap with one is housekeeping.';

CREATE INDEX IF NOT EXISTS idx_flag_audit_tombstone_audit
    ON flag_audit_tombstone (flag_audit_id);

CREATE OR REPLACE FUNCTION flag_audit_guard() RETURNS TRIGGER
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION
            'flag_audit is append-only: audit row % cannot be edited', OLD.flag_audit_id
            USING ERRCODE = 'restrict_violation',
                  HINT = 'A corrected verdict is a NEW audit row for the same flag. '
                         'The chain keeps both and the API reports the latest.';
    END IF;

    IF coalesce(current_setting('framecheck.allow_audit_delete', true), 'off') <> 'on' THEN
        RAISE EXCEPTION
            'flag_audit is append-only: audit row % cannot be deleted', OLD.flag_audit_id
            USING ERRCODE = 'restrict_violation',
                  HINT = 'Tooling that must remove rows it created sets '
                         'framecheck.allow_audit_delete = on (and '
                         'framecheck.audit_delete_reason) for its transaction. '
                         'The deletion is tombstoned either way.';
    END IF;

    INSERT INTO flag_audit_tombstone
        (flag_audit_id, flag_id, verdict, row_hash, deleted_by, reason)
    VALUES
        (OLD.flag_audit_id, OLD.flag_id, OLD.verdict, OLD.row_hash, current_user,
         coalesce(current_setting('framecheck.audit_delete_reason', true), 'not stated'));

    RETURN OLD;
END $$;

DROP TRIGGER IF EXISTS flag_audit_append_only ON flag_audit;
CREATE TRIGGER flag_audit_append_only
    BEFORE UPDATE OR DELETE ON flag_audit
    FOR EACH ROW EXECUTE FUNCTION flag_audit_guard();

-- ---------------------------------------------------------------------------
-- 3. Chain verification
-- ---------------------------------------------------------------------------

-- Walks the chain and reports the FIRST row that does not fit, rather than a
-- bare boolean: "broken" is useless to someone holding a report, while
-- "audit row 214 does not match its hash" is actionable.
CREATE OR REPLACE FUNCTION flag_audit_chain_verify()
RETURNS TABLE (
    rows_checked       BIGINT,
    intact             BOOLEAN,
    first_bad_audit_id BIGINT,
    reason             TEXT,
    tombstones         BIGINT
)
LANGUAGE plpgsql STABLE AS $$
DECLARE
    r        flag_audit;
    prev     BYTEA;
    n        BIGINT := 0;
    bad      BIGINT := NULL;
    why      TEXT   := NULL;
    expected BYTEA;
BEGIN
    prev := flag_audit_chain_seed();

    FOR r IN SELECT * FROM flag_audit ORDER BY flag_audit_id LOOP
        n := n + 1;

        IF r.row_hash IS NULL THEN
            bad := r.flag_audit_id;
            why := 'row has no hash: it was inserted with the chain trigger disabled';
            EXIT;
        END IF;

        IF r.prev_hash IS DISTINCT FROM prev THEN
            bad := r.flag_audit_id;
            why := 'row does not link to the one before it: a row was removed, '
                   'reordered, or inserted out of band';
            EXIT;
        END IF;

        expected := sha256(prev || flag_audit_payload(r));
        IF expected IS DISTINCT FROM r.row_hash THEN
            bad := r.flag_audit_id;
            why := 'row contents do not match its own hash: the verdict was edited '
                   'after it was recorded';
            EXIT;
        END IF;

        prev := r.row_hash;
    END LOOP;

    RETURN QUERY
        SELECT n, bad IS NULL, bad, why, (SELECT count(*) FROM flag_audit_tombstone);
END $$;

COMMENT ON FUNCTION flag_audit_chain_verify() IS
  'Recomputes the whole audit hash chain and names the first row that does not fit.';

-- ---------------------------------------------------------------------------
-- 4. System-versioned flags
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS flag_history (
    flag_history_id BIGSERIAL PRIMARY KEY,
    -- Deliberately NOT a foreign key to flag: history has to outlive the row
    -- it describes, and scripts/run_experiment.py deletes flags wholesale
    -- when it re-runs a snapshot. A FK here would either block that or
    -- cascade the history away, and either defeats the point.
    flag_id      BIGINT  NOT NULL,
    change_op    CHAR(1) NOT NULL CHECK (change_op IN ('U', 'D')),
    changed_by   TEXT    NOT NULL,
    before_image JSONB   NOT NULL,
    -- The period this version was the truth: [became current, stopped being).
    -- A range rather than two nullable timestamps, so "valid at" is one
    -- indexable containment test and an inverted period cannot be stored.
    sys_period   TSTZRANGE NOT NULL CHECK (NOT isempty(sys_period))
);

COMMENT ON TABLE flag_history IS
  'Before-images of every changed or deleted flag row, with the period each '
  'version was valid for. Queried through flag_as_of().';
COMMENT ON COLUMN flag_history.before_image IS
  'to_jsonb(OLD) -- generic on purpose, so a column added by a later '
  'migration is captured without touching this table';

CREATE INDEX IF NOT EXISTS idx_flag_history_flag
    ON flag_history (flag_id, sys_period);

-- Non-overlap, declaratively where the server allows it.
--
-- Two versions of one flag claiming the same instant is a contradiction, and
-- the right place to say so is a constraint, not a comment. EXCLUDE USING
-- gist needs btree_gist for the bigint equality operator class, which is
-- contrib: present in every mainstream install (and on this one, 1.7), but
-- not guaranteed on a user's machine -- and this schema now ships inside a
-- desktop app. So: the exclusion constraint when the extension is available,
-- and a unique index on (flag_id, lower(sys_period)) otherwise, which still
-- makes two versions starting at the same instant unstorable.
DO $$
BEGIN
    BEGIN
        CREATE EXTENSION IF NOT EXISTS btree_gist;
    EXCEPTION WHEN OTHERS THEN
        RAISE NOTICE 'btree_gist unavailable (%); falling back to a unique index', SQLERRM;
    END;

    IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'btree_gist') THEN
        IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'flag_history_no_overlap') THEN
            ALTER TABLE flag_history
                ADD CONSTRAINT flag_history_no_overlap
                EXCLUDE USING gist (flag_id WITH =, sys_period WITH &&);
        END IF;
    ELSE
        CREATE UNIQUE INDEX IF NOT EXISTS flag_history_one_version_per_start
            ON flag_history (flag_id, lower(sys_period));
    END IF;
END $$;

CREATE OR REPLACE FUNCTION flag_version_capture() RETURNS TRIGGER
LANGUAGE plpgsql AS $$
DECLARE
    valid_from TIMESTAMPTZ;
    valid_to   TIMESTAMPTZ;
BEGIN
    -- This version became current when the previous one ended, or at the
    -- row's own creation if this is its first change.
    SELECT max(upper(sys_period)) INTO valid_from
      FROM flag_history WHERE flag_id = OLD.flag_id;
    valid_from := coalesce(valid_from, OLD.created_at::timestamptz);

    -- clock_timestamp(), not now(): two changes inside one transaction share
    -- a transaction timestamp, which would produce an empty period and lose
    -- a version. The microsecond nudge covers a clock that did not move
    -- between two changes.
    valid_to := clock_timestamp();
    IF valid_to <= valid_from THEN
        valid_to := valid_from + interval '1 microsecond';
    END IF;

    INSERT INTO flag_history (flag_id, change_op, changed_by, before_image, sys_period)
    VALUES (OLD.flag_id,
            CASE WHEN TG_OP = 'DELETE' THEN 'D' ELSE 'U' END,
            current_user,
            to_jsonb(OLD),
            tstzrange(valid_from, valid_to, '[)'));

    RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
END $$;

DROP TRIGGER IF EXISTS flag_versioning ON flag;
CREATE TRIGGER flag_versioning
    AFTER UPDATE OR DELETE ON flag
    FOR EACH ROW EXECUTE FUNCTION flag_version_capture();

-- ---------------------------------------------------------------------------
-- 5. Time travel, as a query
-- ---------------------------------------------------------------------------

-- Every flag as it stood at `at_time`: current rows whose version covers that
-- instant, plus the historical versions that do.
--
-- Returns SETOF flag, so callers select from it exactly as they select from
-- the table -- the point of putting this in the database instead of
-- reassembling history in Python.
CREATE OR REPLACE FUNCTION flag_as_of(at_time TIMESTAMPTZ)
RETURNS SETOF flag
LANGUAGE sql STABLE AS $$
    -- Still-current rows: they existed by then, and no later version
    -- supersedes them for that instant.
    SELECT f.*
      FROM flag f
     WHERE f.created_at::timestamptz <= at_time
       AND NOT EXISTS (
            SELECT 1 FROM flag_history h
             WHERE h.flag_id = f.flag_id AND h.sys_period @> at_time)
    UNION ALL
    -- Superseded or deleted rows, restored from their before-image.
    SELECT (jsonb_populate_record(NULL::flag, h.before_image)).*
      FROM flag_history h
     WHERE h.sys_period @> at_time
$$;

COMMENT ON FUNCTION flag_as_of(TIMESTAMPTZ) IS
  'The flag table as it stood at a given instant: SELECT * FROM flag_as_of(ts).';

-- One flag's full version history, oldest first, with the current row last.
CREATE OR REPLACE FUNCTION flag_versions(target_flag_id BIGINT)
RETURNS TABLE (
    version      INT,
    valid_from   TIMESTAMPTZ,
    valid_to     TIMESTAMPTZ,
    is_current   BOOLEAN,
    change_op    CHAR(1),
    changed_by   TEXT,
    row_image    JSONB
)
LANGUAGE sql STABLE AS $$
    WITH past AS (
        SELECT lower(h.sys_period) AS valid_from,
               upper(h.sys_period) AS valid_to,
               false               AS is_current,
               h.change_op,
               h.changed_by,
               h.before_image      AS row_image
          FROM flag_history h
         WHERE h.flag_id = target_flag_id
    ),
    present AS (
        SELECT coalesce((SELECT max(valid_to) FROM past), f.created_at::timestamptz),
               NULL::timestamptz,
               true,
               NULL::char(1),
               NULL::text,
               to_jsonb(f)
          FROM flag f
         WHERE f.flag_id = target_flag_id
    ),
    everything AS (SELECT * FROM past UNION ALL SELECT * FROM present)
    SELECT (row_number() OVER (ORDER BY valid_from))::int,
           valid_from, valid_to, is_current, change_op, changed_by, row_image
      FROM everything
     ORDER BY valid_from
$$;
