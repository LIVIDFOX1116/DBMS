-- FrameCheck migration (2026-10): the users table behind Google sign-in.
--
-- Identity is keyed on Google's `sub` claim, never on the email address:
-- `sub` is the stable, immutable subject identifier for a Google account,
-- while an email can be changed by its owner or (on Workspace domains)
-- reassigned to a different person entirely. Keying on email would mean a
-- renamed account silently becomes a new user, and a reassigned address
-- silently inherits the old one's session history.
--
-- Nothing in the analysis schema references this table: the audit trail is
-- about wallets, not operators, and FrameCheck must keep working with
-- auth disabled (local dev, offline tests). Sign-in gates the API; it does
-- not become a foreign key in the pipeline.

BEGIN;

CREATE TABLE IF NOT EXISTS app_user (
    user_id       BIGSERIAL PRIMARY KEY,
    google_sub    TEXT        NOT NULL UNIQUE,
    email         TEXT        NOT NULL,
    display_name  TEXT,
    picture_url   TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_login_at TIMESTAMPTZ
);

-- Looked up on every sign-in to show "signed in as", and low cardinality
-- enough that a plain index is the right tool.
CREATE INDEX IF NOT EXISTS idx_app_user_email ON app_user (email);

COMMIT;
