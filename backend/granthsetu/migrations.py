"""Versioned, forward-only schema migrations.

Each migration is applied once, inside a transaction, and recorded in
`schema_migrations`. On PostgreSQL an advisory lock stops two API replicas
from migrating at the same time. Nothing here drops tables or data.

SQL is written for PostgreSQL; `_sqlite()` translates the few type names that
differ so the same migrations run on the SQLite dev database.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

log = logging.getLogger("granthsetu.migrations")

# ---------------------------------------------------------------------------
# 0001: the schema that existed before versioned migrations (all idempotent,
# so it is safe on databases created by the old init_schema()).
M0001 = [
    """CREATE TABLE IF NOT EXISTS resources (
        id BIGSERIAL PRIMARY KEY,
        key TEXT NOT NULL UNIQUE,
        topic_key TEXT,
        title TEXT NOT NULL,
        source TEXT NOT NULL,
        kind TEXT NOT NULL,
        url TEXT NOT NULL,
        lang TEXT NOT NULL,
        licence TEXT NOT NULL,
        licence_url TEXT,
        attribution TEXT,
        author TEXT,
        subject TEXT,
        access TEXT NOT NULL DEFAULT 'open',
        fetched_at TEXT NOT NULL
    )""",
    "@pg ALTER TABLE resources ADD COLUMN IF NOT EXISTS access TEXT NOT NULL DEFAULT 'open'",
    "CREATE INDEX IF NOT EXISTS ix_resources_access ON resources(access)",
    "CREATE INDEX IF NOT EXISTS ix_resources_lang ON resources(lang)",
    "CREATE INDEX IF NOT EXISTS ix_resources_topic ON resources(topic_key)",
    "CREATE INDEX IF NOT EXISTS ix_resources_fetched ON resources(fetched_at DESC)",
    # passages are sharded by language (one partition per language) on PostgreSQL
    """@pg CREATE TABLE IF NOT EXISTS passages (
        id BIGSERIAL,
        resource_id BIGINT NOT NULL,
        lang TEXT NOT NULL,
        chunk_no INT NOT NULL,
        text TEXT NOT NULL,
        embedding BYTEA,
        embed_model TEXT,
        PRIMARY KEY (id, lang)
    ) PARTITION BY LIST (lang)""",
    "@pg CREATE TABLE IF NOT EXISTS passages_en PARTITION OF passages FOR VALUES IN ('en')",
    "@pg CREATE TABLE IF NOT EXISTS passages_hi PARTITION OF passages FOR VALUES IN ('hi')",
    "@pg CREATE TABLE IF NOT EXISTS passages_kn PARTITION OF passages FOR VALUES IN ('kn')",
    "@pg CREATE TABLE IF NOT EXISTS passages_other PARTITION OF passages DEFAULT",
    """@sqlite CREATE TABLE IF NOT EXISTS passages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        resource_id INTEGER NOT NULL,
        lang TEXT NOT NULL,
        chunk_no INT NOT NULL,
        text TEXT NOT NULL,
        embedding BLOB,
        embed_model TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS ix_passages_resource ON passages(resource_id)",
    """CREATE TABLE IF NOT EXISTS gaps (
        id BIGSERIAL PRIMARY KEY,
        lang TEXT NOT NULL,
        subject TEXT NOT NULL,
        topic TEXT NOT NULL,
        hits INT NOT NULL DEFAULT 1,
        status TEXT NOT NULL DEFAULT 'open',
        job_id TEXT,
        added INT NOT NULL DEFAULT 0,
        last_seen TEXT NOT NULL,
        UNIQUE (lang, topic)
    )""",
    "CREATE INDEX IF NOT EXISTS ix_gaps_status ON gaps(status, hits DESC)",
    """CREATE TABLE IF NOT EXISTS eval_runs (
        id BIGSERIAL PRIMARY KEY,
        created_at TEXT NOT NULL,
        config TEXT NOT NULL,
        summary TEXT NOT NULL,
        details TEXT NOT NULL
    )""",
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
]

# ---------------------------------------------------------------------------
# 0002: digital-library platform: identity, rights, workflow, files, reader,
# processing jobs, outbox, audit. `resources` is the books table (reused, not
# duplicated); `passages` are the book chunks.
M0002 = [
    # identity -------------------------------------------------------------
    """CREATE TABLE users (
        id BIGSERIAL PRIMARY KEY,
        email TEXT NOT NULL UNIQUE,
        display_name TEXT NOT NULL,
        password_hash TEXT NOT NULL,
        is_active BOOLEAN NOT NULL DEFAULT TRUE,
        created_at TIMESTAMPTZ NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL,
        last_login_at TIMESTAMPTZ
    )""",
    "CREATE TABLE roles (name TEXT PRIMARY KEY, description TEXT NOT NULL)",
    "INSERT INTO roles(name, description) VALUES "
    "('reader', 'Registered reader'), ('student', 'Student (membership-based access)'), "
    "('contributor', 'May register resources and submit them for review'), "
    "('librarian', 'Manages, reviews and publishes resources'), "
    "('rights_manager', 'Verifies rights and manages access policies and entitlements'), "
    "('institution_admin', 'Manages members of their own institution'), "
    "('platform_admin', 'Full administration including user roles')",
    """CREATE TABLE user_roles (
        user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        role TEXT NOT NULL REFERENCES roles(name),
        granted_by BIGINT REFERENCES users(id),
        granted_at TIMESTAMPTZ NOT NULL,
        PRIMARY KEY (user_id, role)
    )""",
    """CREATE TABLE sessions (
        id BIGSERIAL PRIMARY KEY,
        token_hash TEXT NOT NULL UNIQUE,
        user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        created_at TIMESTAMPTZ NOT NULL,
        expires_at TIMESTAMPTZ NOT NULL,
        last_seen_at TIMESTAMPTZ NOT NULL
    )""",
    "CREATE INDEX ix_sessions_user ON sessions(user_id)",
    """CREATE TABLE institutions (
        id BIGSERIAL PRIMARY KEY,
        name TEXT NOT NULL UNIQUE,
        created_at TIMESTAMPTZ NOT NULL
    )""",
    """CREATE TABLE institution_memberships (
        institution_id BIGINT NOT NULL REFERENCES institutions(id) ON DELETE CASCADE,
        user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        member_role TEXT NOT NULL DEFAULT 'member' CHECK (member_role IN ('member', 'admin')),
        expires_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL,
        PRIMARY KEY (institution_id, user_id)
    )""",
    "CREATE INDEX ix_inst_members_user ON institution_memberships(user_id)",
    """CREATE TABLE user_groups (
        id BIGSERIAL PRIMARY KEY,
        name TEXT NOT NULL UNIQUE,
        institution_id BIGINT REFERENCES institutions(id) ON DELETE SET NULL,
        created_at TIMESTAMPTZ NOT NULL
    )""",
    """CREATE TABLE group_memberships (
        group_id BIGINT NOT NULL REFERENCES user_groups(id) ON DELETE CASCADE,
        user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        expires_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL,
        PRIMARY KEY (group_id, user_id)
    )""",
    "CREATE INDEX ix_group_members_user ON group_memberships(user_id)",
    # books = resources (extended) ------------------------------------------
    "ALTER TABLE resources ADD COLUMN subtitle TEXT",
    "ALTER TABLE resources ADD COLUMN isbn TEXT",
    "ALTER TABLE resources ADD COLUMN publisher TEXT",
    "ALTER TABLE resources ADD COLUMN pub_year INT",
    "ALTER TABLE resources ADD COLUMN edition TEXT",
    "ALTER TABLE resources ADD COLUMN description TEXT",
    "ALTER TABLE resources ADD COLUMN categories TEXT",
    "ALTER TABLE resources ADD COLUMN provider_id TEXT",
    "ALTER TABLE resources ADD COLUMN rights_holder TEXT",
    # rights basis: public_domain | open_licence | institutional_licence | rights_holder_permission
    #               | external_provider | catalogue_only | unverified
    "ALTER TABLE resources ADD COLUMN rights_basis TEXT NOT NULL DEFAULT 'open_licence'",
    # rights_status: unverified | verified | rejected
    "ALTER TABLE resources ADD COLUMN rights_status TEXT NOT NULL DEFAULT 'verified'",
    "ALTER TABLE resources ADD COLUMN rights_notes TEXT",
    # what the licence/permission allows GranthSetu to do
    "ALTER TABLE resources ADD COLUMN allow_display BOOLEAN NOT NULL DEFAULT TRUE",
    "ALTER TABLE resources ADD COLUMN allow_download BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE resources ADD COLUMN allow_ai BOOLEAN NOT NULL DEFAULT TRUE",
    "ALTER TABLE resources ADD COLUMN policy TEXT NOT NULL DEFAULT 'OPEN_ACCESS'",
    "ALTER TABLE resources ADD COLUMN catalogue TEXT NOT NULL DEFAULT 'discoverable'",
    "ALTER TABLE resources ADD COLUMN institution_id BIGINT REFERENCES institutions(id)",
    "ALTER TABLE resources ADD COLUMN group_id BIGINT REFERENCES user_groups(id)",
    "ALTER TABLE resources ADD COLUMN status TEXT NOT NULL DEFAULT 'PUBLISHED'",
    "ALTER TABLE resources ADD COLUMN origin TEXT NOT NULL DEFAULT 'harvest'",
    "ALTER TABLE resources ADD COLUMN created_by BIGINT REFERENCES users(id)",
    "ALTER TABLE resources ADD COLUMN created_at TIMESTAMPTZ",
    "ALTER TABLE resources ADD COLUMN updated_at TIMESTAMPTZ",
    "ALTER TABLE resources ADD COLUMN published_at TIMESTAMPTZ",
    "CREATE INDEX ix_resources_status_policy ON resources(status, policy)",
    "CREATE INDEX ix_resources_origin ON resources(origin, status)",
    "CREATE INDEX ix_resources_created_by ON resources(created_by)",
    # Harvested items that are behind a login or a price become external-provider records.
    "UPDATE resources SET policy='EXTERNAL_PROVIDER_ACCESS', rights_basis='external_provider', allow_display=FALSE, "
    "allow_ai=FALSE WHERE access IN ('authorised', 'paid')",
    # chunks: kind = content (full text, needs read access) | metadata (title/abstract, needs discovery only)
    "ALTER TABLE passages ADD COLUMN kind TEXT NOT NULL DEFAULT 'content'",
    "ALTER TABLE passages ADD COLUMN page_no INT",
    "ALTER TABLE passages ADD COLUMN chapter_no INT",
    "UPDATE passages SET kind='metadata' WHERE resource_id IN (SELECT id FROM resources WHERE access IN ('authorised', 'paid'))",
    """CREATE TABLE book_files (
        id BIGSERIAL PRIMARY KEY,
        resource_id BIGINT NOT NULL REFERENCES resources(id) ON DELETE CASCADE,
        storage_key TEXT NOT NULL UNIQUE,
        filename TEXT NOT NULL,
        mime TEXT NOT NULL,
        size_bytes BIGINT NOT NULL,
        sha256 TEXT NOT NULL,
        scan_status TEXT NOT NULL,
        is_current BOOLEAN NOT NULL DEFAULT TRUE,
        uploaded_by BIGINT REFERENCES users(id),
        uploaded_at TIMESTAMPTZ NOT NULL
    )""",
    "CREATE INDEX ix_book_files_resource ON book_files(resource_id, is_current)",
    "CREATE INDEX ix_book_files_sha ON book_files(sha256)",
    """CREATE TABLE book_chapters (
        id BIGSERIAL PRIMARY KEY,
        resource_id BIGINT NOT NULL REFERENCES resources(id) ON DELETE CASCADE,
        chapter_no INT NOT NULL,
        title TEXT NOT NULL,
        start_page INT NOT NULL,
        end_page INT NOT NULL,
        UNIQUE (resource_id, chapter_no)
    )""",
    """CREATE TABLE book_pages (
        resource_id BIGINT NOT NULL REFERENCES resources(id) ON DELETE CASCADE,
        page_no INT NOT NULL,
        chapter_no INT,
        text TEXT NOT NULL,
        ocr BOOLEAN NOT NULL DEFAULT FALSE,
        PRIMARY KEY (resource_id, page_no)
    )""",
    # rights and access -----------------------------------------------------
    """CREATE TABLE entitlements (
        id BIGSERIAL PRIMARY KEY,
        resource_id BIGINT NOT NULL REFERENCES resources(id) ON DELETE CASCADE,
        subject_type TEXT NOT NULL CHECK (subject_type IN ('user', 'group', 'institution')),
        subject_id BIGINT NOT NULL,
        reason TEXT,
        granted_by BIGINT REFERENCES users(id),
        created_at TIMESTAMPTZ NOT NULL,
        expires_at TIMESTAMPTZ,
        revoked_at TIMESTAMPTZ,
        revoked_by BIGINT REFERENCES users(id)
    )""",
    "CREATE INDEX ix_entitlements_subject ON entitlements(subject_type, subject_id)",
    "CREATE INDEX ix_entitlements_resource ON entitlements(resource_id)",
    # content workflow --------------------------------------------------------
    """CREATE TABLE book_reviews (
        id BIGSERIAL PRIMARY KEY,
        resource_id BIGINT NOT NULL REFERENCES resources(id) ON DELETE CASCADE,
        reviewer_id BIGINT REFERENCES users(id),
        decision TEXT NOT NULL,
        notes TEXT,
        created_at TIMESTAMPTZ NOT NULL
    )""",
    "CREATE INDEX ix_reviews_resource ON book_reviews(resource_id)",
    """CREATE TABLE publication_events (
        id BIGSERIAL PRIMARY KEY,
        resource_id BIGINT NOT NULL REFERENCES resources(id) ON DELETE CASCADE,
        from_status TEXT,
        to_status TEXT NOT NULL,
        action TEXT NOT NULL,
        actor_id BIGINT REFERENCES users(id),
        reason TEXT,
        created_at TIMESTAMPTZ NOT NULL
    )""",
    "CREATE INDEX ix_pubevents_resource ON publication_events(resource_id, id)",
    # processing ---------------------------------------------------------------
    """CREATE TABLE processing_jobs (
        id BIGSERIAL PRIMARY KEY,
        resource_id BIGINT NOT NULL REFERENCES resources(id) ON DELETE CASCADE,
        kind TEXT NOT NULL,
        file_id BIGINT REFERENCES book_files(id),
        status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
        attempts INT NOT NULL DEFAULT 0,
        max_attempts INT NOT NULL DEFAULT 3,
        stage TEXT,
        last_error TEXT,
        result TEXT,
        requested_by BIGINT REFERENCES users(id),
        created_at TIMESTAMPTZ NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL,
        started_at TIMESTAMPTZ,
        finished_at TIMESTAMPTZ
    )""",
    "CREATE INDEX ix_jobs_status ON processing_jobs(status, updated_at)",
    "CREATE INDEX ix_jobs_resource ON processing_jobs(resource_id, id)",
    # at most one active job per resource: duplicates are impossible, not just unlikely
    "CREATE UNIQUE INDEX ux_jobs_active ON processing_jobs(resource_id) WHERE status IN ('queued', 'running')",
    """CREATE TABLE processing_errors (
        id BIGSERIAL PRIMARY KEY,
        job_id BIGINT REFERENCES processing_jobs(id) ON DELETE CASCADE,
        resource_id BIGINT NOT NULL REFERENCES resources(id) ON DELETE CASCADE,
        stage TEXT NOT NULL,
        message TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL
    )""",
    """CREATE TABLE outbox (
        id BIGSERIAL PRIMARY KEY,
        topic TEXT NOT NULL,
        payload TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL,
        dispatched_at TIMESTAMPTZ,
        attempts INT NOT NULL DEFAULT 0,
        last_error TEXT
    )""",
    "CREATE INDEX ix_outbox_pending ON outbox(dispatched_at, id)",
    # user experience -----------------------------------------------------------
    """CREATE TABLE reading_progress (
        user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        resource_id BIGINT NOT NULL REFERENCES resources(id) ON DELETE CASCADE,
        page_no INT NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL,
        PRIMARY KEY (user_id, resource_id)
    )""",
    """CREATE TABLE bookmarks (
        id BIGSERIAL PRIMARY KEY,
        user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        resource_id BIGINT NOT NULL REFERENCES resources(id) ON DELETE CASCADE,
        page_no INT NOT NULL,
        note TEXT,
        created_at TIMESTAMPTZ NOT NULL,
        UNIQUE (user_id, resource_id, page_no)
    )""",
    # operations -------------------------------------------------------------------
    """CREATE TABLE audit_logs (
        id BIGSERIAL PRIMARY KEY,
        actor_id BIGINT REFERENCES users(id) ON DELETE SET NULL,
        action TEXT NOT NULL,
        target_type TEXT,
        target_id TEXT,
        outcome TEXT NOT NULL,
        detail TEXT,
        created_at TIMESTAMPTZ NOT NULL
    )""",
    "CREATE INDEX ix_audit_created ON audit_logs(created_at DESC)",
    "CREATE INDEX ix_audit_target ON audit_logs(target_type, target_id)",
]

MIGRATIONS: list[tuple[int, str, list[str]]] = [
    (1, "baseline", M0001),
    (2, "digital_library_platform", M0002),
]


def _sqlite(stmt: str) -> str:
    s = stmt.replace("BIGSERIAL PRIMARY KEY", "INTEGER PRIMARY KEY AUTOINCREMENT")
    s = s.replace("TIMESTAMPTZ", "TEXT").replace("BYTEA", "BLOB")
    return s


def _for(kind: str, stmts: list[str]) -> list[str]:
    out = []
    for s in stmts:
        m = re.match(r"@(pg|sqlite)\s+", s)
        if m:
            if (m.group(1) == "pg") != (kind == "postgres"):
                continue
            s = s[m.end():]
        out.append(s if kind == "postgres" else _sqlite(s))
    return out


def applied(db) -> list[int]:
    db.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INT PRIMARY KEY, name TEXT NOT NULL, "
               "applied_at TEXT NOT NULL)")
    return [int(r["version"]) for r in db.query("SELECT version FROM schema_migrations ORDER BY version", primary=True)]


def migrate(db) -> list[int]:
    """Apply pending migrations. Returns the versions applied in this call."""
    done: list[int] = []
    if db.kind == "sqlite":
        cols = [r["name"] for r in db.execute("PRAGMA table_info(resources)")]
        if cols and "access" not in cols:  # databases created before the access column
            db.execute("ALTER TABLE resources ADD COLUMN access TEXT NOT NULL DEFAULT 'open'")
        have = set(applied(db))
        for version, name, stmts in MIGRATIONS:
            if version in have:
                continue
            db.transaction([(s, ()) for s in _for("sqlite", stmts)] + [
                ("INSERT INTO schema_migrations(version, name, applied_at) VALUES (%s, %s, %s)",
                 (version, name, datetime.now(timezone.utc).isoformat()))])
            done.append(version)
            log.info("applied migration %04d_%s", version, name)
        return done

    applied(db)
    with db._primary.connection() as conn:  # one connection holds the advisory lock throughout
        conn.execute("SELECT pg_advisory_lock(727274)")
        try:
            have = {int(r[0]) for r in conn.execute("SELECT version FROM schema_migrations").fetchall()}
            conn.commit()
            for version, name, stmts in MIGRATIONS:
                if version in have:
                    continue
                with conn.transaction():
                    for s in _for("postgres", stmts):
                        conn.execute(s)
                    conn.execute("INSERT INTO schema_migrations(version, name, applied_at) VALUES (%s, %s, %s)",
                                 (version, name, datetime.now(timezone.utc).isoformat()))
                done.append(version)
                log.info("applied migration %04d_%s", version, name)
        finally:
            conn.execute("SELECT pg_advisory_unlock(727274)")
            conn.commit()
    return done
