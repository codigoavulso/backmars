PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    email_verified_at TEXT,
    session_version INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    project_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_projects_user_updated
ON projects (user_id, updated_at DESC);

-- Failed sign-in attempts, kept per client address so a burst of guesses from
-- one source can be turned away. Deliberately not keyed on the username: that
-- would let anyone lock a user out of their own account.
CREATE TABLE IF NOT EXISTS login_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_address TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_login_attempts_address_created
ON login_attempts (client_address, created_at DESC);

-- Successful registration attempts are capped separately from failed logins.
-- This protects the password hasher and transactional mail quota without
-- allowing guesses against one username to lock that account.
CREATE TABLE IF NOT EXISTS registration_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_address TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_registration_attempts_address_created
ON registration_attempts (client_address, created_at DESC);

-- Short-lived, one-use account codes.  Only an HMAC is stored, so copying the
-- database does not reveal a usable six-digit reset or verification code.
CREATE TABLE IF NOT EXISTS account_codes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    purpose TEXT NOT NULL CHECK (purpose IN ('password_reset', 'email_verification')),
    code_hash TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    consumed_at TEXT,
    request_address TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_account_codes_user_purpose_created
ON account_codes (user_id, purpose, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_account_codes_address_purpose_created
ON account_codes (request_address, purpose, created_at DESC);

-- Public library metadata is mutable, but the slug is permanent and the row
-- is never deleted.  Archiving therefore cannot let a different user inherit
-- a URL that may still appear in old worksheets.
CREATE TABLE IF NOT EXISTS publications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id INTEGER NOT NULL,
    slug TEXT NOT NULL COLLATE NOCASE UNIQUE,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    instructions TEXT NOT NULL DEFAULT '',
    tags_json TEXT NOT NULL DEFAULT '[]',
    language TEXT NOT NULL DEFAULT 'en',
    difficulty TEXT NOT NULL DEFAULT 'beginner',
    kind TEXT NOT NULL DEFAULT 'example',
    license TEXT NOT NULL DEFAULT 'unspecified',
    status TEXT NOT NULL DEFAULT 'published'
        CHECK (status IN ('published', 'unlisted', 'archived', 'removed')),
    latest_revision INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (owner_id) REFERENCES users(id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_publications_status_updated
ON publications (status, updated_at DESC, id DESC);

CREATE INDEX IF NOT EXISTS idx_publications_owner_updated
ON publications (owner_id, updated_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS publication_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    publication_id INTEGER NOT NULL,
    revision INTEGER NOT NULL,
    project_json TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    webmars_version TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    FOREIGN KEY (publication_id) REFERENCES publications(id) ON DELETE RESTRICT,
    UNIQUE (publication_id, revision)
);

CREATE INDEX IF NOT EXISTS idx_publication_revisions_publication
ON publication_revisions (publication_id, revision DESC);
