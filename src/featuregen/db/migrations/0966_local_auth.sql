-- src/featuregen/db/migrations/0966_local_auth.sql
-- Local username/password authentication with users -> groups -> roles + session tokens (the mode
-- until an OIDC IdP lands). A user belongs to groups; groups grant roles; roles drive read-scope.
-- A login mints a session; requests carry the token (we store only its SHA-256, never the raw token).
CREATE TABLE IF NOT EXISTS app_user (
    user_id       text        PRIMARY KEY,
    username      text        UNIQUE NOT NULL,
    password_hash text        NOT NULL,          -- pbkdf2_sha256$rounds$salt$hash
    disabled      boolean     NOT NULL DEFAULT false,
    created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS app_group (
    group_id text PRIMARY KEY,
    name     text UNIQUE NOT NULL
);
CREATE TABLE IF NOT EXISTS app_user_group (
    user_id  text NOT NULL REFERENCES app_user (user_id)  ON DELETE CASCADE,
    group_id text NOT NULL REFERENCES app_group (group_id) ON DELETE CASCADE,
    PRIMARY KEY (user_id, group_id)
);
CREATE TABLE IF NOT EXISTS app_group_role (
    group_id text NOT NULL REFERENCES app_group (group_id) ON DELETE CASCADE,
    role     text NOT NULL,
    PRIMARY KEY (group_id, role)
);
CREATE TABLE IF NOT EXISTS app_session (
    token_hash text        PRIMARY KEY,          -- SHA-256 of the raw token (never store the token)
    user_id    text        NOT NULL REFERENCES app_user (user_id) ON DELETE CASCADE,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS app_session_user_idx ON app_session (user_id);
