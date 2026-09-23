import hashlib
import sqlite3
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS houses (
 id TEXT PRIMARY KEY, address TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS house_districts (house_id TEXT PRIMARY KEY REFERENCES houses(id), district TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_house_districts_district ON house_districts(district);
CREATE TABLE IF NOT EXISTS operator_districts (user_id TEXT NOT NULL REFERENCES users(id), district TEXT NOT NULL, PRIMARY KEY(user_id,district));
CREATE INDEX IF NOT EXISTS idx_operator_districts_district ON operator_districts(district);
CREATE UNIQUE INDEX IF NOT EXISTS uq_operator_district ON operator_districts(district);
CREATE TABLE IF NOT EXISTS users (
 id TEXT PRIMARY KEY, name TEXT NOT NULL,
 role TEXT NOT NULL CHECK(role IN ('resident', 'operator')),
 house_id TEXT NOT NULL REFERENCES houses(id),
 token_hash TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS tickets (
 id TEXT PRIMARY KEY, house_id TEXT NOT NULL REFERENCES houses(id),
 resident_id TEXT NOT NULL REFERENCES users(id),
 category TEXT NOT NULL, location TEXT NOT NULL, description TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN
 ('new','accepted','in_progress','resolved','confirmed','reopened')),
 priority TEXT NOT NULL DEFAULT 'normal' CHECK(priority IN ('normal','urgent','emergency')),
 version INTEGER NOT NULL DEFAULT 1,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 due_at TEXT, first_response_at TEXT, closed_at TEXT
);
CREATE TABLE IF NOT EXISTS events (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 ticket_id TEXT NOT NULL REFERENCES tickets(id),
 actor_id TEXT NOT NULL REFERENCES users(id),
 kind TEXT NOT NULL, status TEXT NOT NULL, text TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS tickets_house ON tickets(house_id, created_at);
CREATE INDEX IF NOT EXISTS tickets_resident ON tickets(resident_id, created_at);
CREATE INDEX IF NOT EXISTS events_ticket ON events(ticket_id, id);
CREATE TABLE IF NOT EXISTS ticket_attachments (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 ticket_id TEXT NOT NULL REFERENCES tickets(id),
 source TEXT NOT NULL,
 type TEXT NOT NULL,
 external_id TEXT,
 metadata_json TEXT NOT NULL DEFAULT '{}',
 created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ticket_attachments_ticket ON ticket_attachments(ticket_id, id);
CREATE TABLE IF NOT EXISTS max_updates (
 fingerprint TEXT PRIMARY KEY,
 update_type TEXT NOT NULL,
 payload_json TEXT NOT NULL,
 received_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS max_polling_state (
 bot_id INTEGER PRIMARY KEY,
 marker INTEGER
);
CREATE TABLE IF NOT EXISTS max_outbox (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 max_user_id INTEGER NOT NULL,
 text TEXT NOT NULL,
 attachments_json TEXT NOT NULL DEFAULT '[]',
 status TEXT NOT NULL DEFAULT 'pending'
   CHECK(status IN ('pending','sending','sent','failed')),
 attempts INTEGER NOT NULL DEFAULT 0,
 next_attempt_at TEXT NOT NULL,
 error TEXT,
 max_mid TEXT,
 created_at TEXT NOT NULL,
 sent_at TEXT
);
CREATE INDEX IF NOT EXISTS max_outbox_delivery
 ON max_outbox(status, next_attempt_at, id);
CREATE TABLE IF NOT EXISTS max_links (
 max_user_id INTEGER PRIMARY KEY,
 user_id TEXT NOT NULL UNIQUE REFERENCES users(id),
 linked_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS max_dialogs (
 max_user_id INTEGER PRIMARY KEY,
 state TEXT NOT NULL,
 draft_json TEXT NOT NULL DEFAULT '{}',
 updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sla_alerts (
 ticket_id TEXT PRIMARY KEY REFERENCES tickets(id),
 created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS enrollment_codes (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 code_hash TEXT NOT NULL UNIQUE,
 house_id TEXT NOT NULL REFERENCES houses(id),
 role TEXT NOT NULL CHECK(role IN ('resident','operator')),
 expires_at TEXT NOT NULL,
 max_uses INTEGER NOT NULL DEFAULT 1 CHECK(max_uses > 0),
 used_count INTEGER NOT NULL DEFAULT 0 CHECK(used_count >= 0),
 created_at TEXT NOT NULL,
 revoked_at TEXT
);
CREATE TABLE IF NOT EXISTS max_link_attempts (
 max_user_id INTEGER PRIMARY KEY,
 failed_count INTEGER NOT NULL DEFAULT 0,
 blocked_until TEXT
);
CREATE TABLE IF NOT EXISTS house_identities (
 house_id TEXT NOT NULL REFERENCES houses(id),
 provider TEXT NOT NULL,
 external_id TEXT NOT NULL,
 PRIMARY KEY(provider, external_id)
);
CREATE TABLE IF NOT EXISTS gosuslugi_link_attempts (
 state_hash TEXT PRIMARY KEY,
 max_user_id INTEGER NOT NULL,
 display_name TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('pending','completed','unmatched','expired','cancelled')),
 expires_at TEXT NOT NULL,
 created_at TEXT NOT NULL,
 completed_at TEXT
);
CREATE INDEX IF NOT EXISTS gosuslugi_link_attempts_user
 ON gosuslugi_link_attempts(max_user_id, created_at);
CREATE TABLE IF NOT EXISTS residency_verifications (
 user_id TEXT PRIMARY KEY REFERENCES users(id),
 provider TEXT NOT NULL,
 subject_hash TEXT NOT NULL,
 registration_type TEXT NOT NULL CHECK(registration_type IN ('permanent','temporary')),
 house_address TEXT NOT NULL,
 fias_house_id TEXT,
 verified_at TEXT NOT NULL,
 UNIQUE(provider, subject_hash)
);
CREATE TABLE IF NOT EXISTS incidents (
 id TEXT PRIMARY KEY,
 house_id TEXT NOT NULL REFERENCES houses(id),
 category TEXT NOT NULL,
 location TEXT NOT NULL,
 priority TEXT NOT NULL CHECK(priority IN ('normal','urgent','emergency')),
 created_by TEXT NOT NULL REFERENCES users(id),
 created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS incident_tickets (
 incident_id TEXT NOT NULL REFERENCES incidents(id),
 ticket_id TEXT NOT NULL UNIQUE REFERENCES tickets(id),
 PRIMARY KEY (incident_id, ticket_id)
);
CREATE INDEX IF NOT EXISTS incidents_house ON incidents(house_id, created_at);
"""

TICKET_MIGRATIONS = {
    'priority': "ALTER TABLE tickets ADD COLUMN priority TEXT NOT NULL DEFAULT 'normal'",
    'due_at': 'ALTER TABLE tickets ADD COLUMN due_at TEXT',
    'first_response_at': 'ALTER TABLE tickets ADD COLUMN first_response_at TEXT',
    'closed_at': 'ALTER TABLE tickets ADD COLUMN closed_at TEXT',
}


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Database:
    def __init__(self, path: str):
        self.path = path

    def initialize(self):
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            columns = {row['name'] for row in conn.execute('PRAGMA table_info(tickets)').fetchall()}
            for column, statement in TICKET_MIGRATIONS.items():
                if column not in columns:
                    conn.execute(statement)

    @contextmanager
    def connect(self, write=False):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        try:
            if write:
                conn.execute('BEGIN IMMEDIATE')
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


class AsyncDatabase:
    """Async database connection used by the API and MAX worker.

    The synchronous Database remains only for local seed/test preparation.
    """
    def __init__(self, path: str):
        self.path = path

    async def initialize(self):
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        async with self.connect() as conn:
            await conn.executescript(SCHEMA)
            cursor = await conn.execute('PRAGMA table_info(tickets)')
            columns = {row['name'] for row in await cursor.fetchall()}
            for column, statement in TICKET_MIGRATIONS.items():
                if column not in columns:
                    await conn.execute(statement)

    @asynccontextmanager
    async def connect(self, write=False):
        conn = await aiosqlite.connect(self.path, timeout=10)
        conn.row_factory = aiosqlite.Row
        await conn.execute('PRAGMA foreign_keys=ON')
        try:
            if write:
                await conn.execute('BEGIN IMMEDIATE')
            yield conn
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise
        finally:
            await conn.close()
