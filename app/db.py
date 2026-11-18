"""PostgreSQL storage: organizations, users, monitors, contacts and check history."""
import secrets
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import psycopg
from psycopg.rows import dict_row

from .config import settings

# Each migration is a list of statements, applied once, in order. The database is
# new, so the first migration contains the complete schema. Never edit a migration
# that has shipped; add a new one instead.
MIGRATIONS: list[tuple[int, list[str]]] = [
    (1, [
        """CREATE TABLE users (
            id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        )""",
        """CREATE TABLE organizations (
            id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL
        )""",
        """CREATE TABLE memberships (
            id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
            role TEXT NOT NULL CHECK (role IN ('owner', 'member')),
            UNIQUE (user_id, organization_id)
        )""",
        """CREATE TABLE invites (
            id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
            token TEXT NOT NULL UNIQUE,
            role TEXT NOT NULL CHECK (role IN ('owner', 'member')),
            created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
            expires_at TEXT NOT NULL,
            used_at TEXT,
            created_at TEXT NOT NULL
        )""",
        """CREATE TABLE alert_contacts (
            id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            type TEXT NOT NULL DEFAULT 'telegram',
            chat_id TEXT,
            bind_code TEXT UNIQUE,
            verified INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )""",
        """CREATE TABLE monitors (
            id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            url TEXT NOT NULL,
            method TEXT NOT NULL DEFAULT 'GET',
            expected_status INTEGER NOT NULL DEFAULT 200,
            keyword TEXT,
            timeout_s DOUBLE PRECISION NOT NULL DEFAULT 10,
            interval_s INTEGER NOT NULL DEFAULT 60,
            verify_ssl INTEGER NOT NULL DEFAULT 1,
            enabled INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT 'unknown',
            consecutive_failures INTEGER NOT NULL DEFAULT 0,
            last_checked_at TEXT,
            last_status_change_at TEXT,
            last_alert_at TEXT,
            last_status_code INTEGER,
            last_response_ms DOUBLE PRECISION,
            last_error TEXT,
            ssl_expires_at TEXT,
            ssl_checked_at TEXT,
            created_at TEXT NOT NULL
        )""",
        """CREATE TABLE monitor_contacts (
            monitor_id INTEGER NOT NULL REFERENCES monitors(id) ON DELETE CASCADE,
            contact_id INTEGER NOT NULL REFERENCES alert_contacts(id) ON DELETE CASCADE,
            PRIMARY KEY (monitor_id, contact_id)
        )""",
        """CREATE TABLE checks (
            id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            monitor_id INTEGER NOT NULL REFERENCES monitors(id) ON DELETE CASCADE,
            checked_at TEXT NOT NULL,
            ok INTEGER NOT NULL,
            status_code INTEGER,
            response_ms DOUBLE PRECISION,
            error TEXT
        )""",
        "CREATE INDEX idx_monitors_org ON monitors(organization_id)",
        "CREATE INDEX idx_checks_monitor_time ON checks(monitor_id, checked_at)",
    ]),
]

MONITOR_FIELDS = (
    "name", "url", "method", "expected_status", "keyword", "timeout_s",
    "interval_s", "verify_ssl", "enabled",
)
STATE_FIELDS = {
    "status", "consecutive_failures", "last_checked_at", "last_status_change_at",
    "last_alert_at", "last_status_code", "last_response_ms", "last_error",
    "ssl_expires_at", "ssl_checked_at",
}
# Arbitrary constant for pg_advisory_xact_lock: serializes migrations across processes.
MIGRATION_LOCK_ID = 724001


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def ts(dt: datetime | None) -> str | None:
    """Fixed-width ISO format, so string comparison matches time order."""
    return dt.isoformat(timespec="seconds") if dt else None


def parse_ts(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def connect() -> psycopg.Connection:
    return psycopg.connect(settings.database_url, row_factory=dict_row, connect_timeout=10)


@contextmanager
def db():
    """One connection and one transaction per block: commit on success, rollback on error."""
    conn = connect()
    try:
        with conn.transaction():
            yield conn
    finally:
        conn.close()


def init_db() -> None:
    with db() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (MIGRATION_LOCK_ID,))
        conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)")
        current = conn.execute(
            "SELECT COALESCE(MAX(version), 0) AS v FROM schema_version"
        ).fetchone()["v"]
        for version, statements in MIGRATIONS:
            if version > current:
                for statement in statements:
                    conn.execute(statement)
                conn.execute("INSERT INTO schema_version (version) VALUES (%s)", (version,))


# --- organizations, users, memberships --------------------------------------

def count_users() -> int:
    with db() as c:
        return c.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]


def get_user_by_name(username: str):
    with db() as c:
        return c.execute("SELECT * FROM users WHERE username = %s", (username,)).fetchone()


def get_membership(user_id: int):
    """The user's organization and role. MVP: one organization per user."""
    with db() as c:
        row = c.execute(
            "SELECT u.id AS user_id, u.username, m.organization_id, m.role, o.name AS org_name "
            "FROM users u "
            "JOIN memberships m ON m.user_id = u.id "
            "JOIN organizations o ON o.id = m.organization_id "
            "WHERE u.id = %s ORDER BY m.id LIMIT 1",
            (user_id,),
        ).fetchone()
        return row


def create_organization_with_owner(org_name: str, username: str, password_hash: str) -> int:
    now = ts(utcnow())
    with db() as c:
        org_id = c.execute(
            "INSERT INTO organizations (name, created_at) VALUES (%s, %s) RETURNING id",
            (org_name, now),
        ).fetchone()["id"]
        user_id = c.execute(
            "INSERT INTO users (username, password_hash, created_at) VALUES (%s, %s, %s) RETURNING id",
            (username, password_hash, now),
        ).fetchone()["id"]
        c.execute(
            "INSERT INTO memberships (user_id, organization_id, role) VALUES (%s, %s, 'owner')",
            (user_id, org_id),
        )
    return org_id


def list_members(org_id: int) -> list[dict]:
    with db() as c:
        return c.execute(
            "SELECT u.username, m.role FROM memberships m "
            "JOIN users u ON u.id = m.user_id WHERE m.organization_id = %s ORDER BY u.username",
            (org_id,),
        ).fetchall()


# --- invites ----------------------------------------------------------------

def create_invite(org_id: int, token: str, role: str, created_by: int, expires_at: datetime) -> None:
    with db() as c:
        c.execute(
            "INSERT INTO invites (organization_id, token, role, created_by, expires_at, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (org_id, token, role, created_by, ts(expires_at), ts(utcnow())),
        )


def get_invite(token: str):
    """Invite with its organization name, or None if missing, used or expired."""
    with db() as c:
        row = c.execute(
            "SELECT i.*, o.name AS org_name FROM invites i "
            "JOIN organizations o ON o.id = i.organization_id WHERE i.token = %s",
            (token,),
        ).fetchone()
    if not row or row["used_at"] or parse_ts(row["expires_at"]) <= utcnow():
        return None
    return row


def list_invites(org_id: int) -> list[dict]:
    with db() as c:
        return c.execute(
            "SELECT role, created_at, expires_at, used_at FROM invites "
            "WHERE organization_id = %s ORDER BY id DESC LIMIT 50",
            (org_id,),
        ).fetchall()


def accept_invite(token: str, username: str, password_hash: str) -> tuple[bool, str]:
    now = utcnow()
    with db() as c:
        # FOR UPDATE: two parallel sign-ups with the same link cannot both succeed.
        inv = c.execute("SELECT * FROM invites WHERE token = %s FOR UPDATE", (token,)).fetchone()
        if not inv or inv["used_at"] or parse_ts(inv["expires_at"]) <= now:
            return False, "Invite is invalid or has expired"
        if c.execute("SELECT 1 FROM users WHERE username = %s", (username,)).fetchone():
            return False, "Username is already taken"
        user_id = c.execute(
            "INSERT INTO users (username, password_hash, created_at) VALUES (%s, %s, %s) RETURNING id",
            (username, password_hash, ts(now)),
        ).fetchone()["id"]
        c.execute(
            "INSERT INTO memberships (user_id, organization_id, role) VALUES (%s, %s, %s)",
            (user_id, inv["organization_id"], inv["role"]),
        )
        c.execute("UPDATE invites SET used_at = %s WHERE id = %s", (ts(now), inv["id"]))
    return True, ""


# --- alert contacts (Telegram) ----------------------------------------------

def list_contacts(org_id: int) -> list[dict]:
    with db() as c:
        return c.execute(
            "SELECT * FROM alert_contacts WHERE organization_id = %s ORDER BY name", (org_id,)
        ).fetchall()


def create_contact(org_id: int, name: str) -> None:
    with db() as c:
        c.execute(
            "INSERT INTO alert_contacts (organization_id, name, bind_code, created_at) "
            "VALUES (%s, %s, %s, %s)",
            (org_id, name, secrets.token_urlsafe(16), ts(utcnow())),
        )


def delete_contact(org_id: int, contact_id: int) -> bool:
    with db() as c:
        cur = c.execute(
            "DELETE FROM alert_contacts WHERE id = %s AND organization_id = %s",
            (contact_id, org_id),
        )
        return cur.rowcount > 0


def bind_contact(code: str, chat_id: int) -> str | None:
    """Attach a Telegram chat to the contact holding this one-time code."""
    with db() as c:
        row = c.execute(
            "SELECT id, name FROM alert_contacts WHERE bind_code = %s", (code,)
        ).fetchone()
        if not row:
            return None
        c.execute(
            "UPDATE alert_contacts SET chat_id = %s, verified = 1, bind_code = NULL WHERE id = %s",
            (str(chat_id), row["id"]),
        )
        return row["name"]


def valid_contact_ids(org_id: int, ids: list[int]) -> list[int]:
    """Keep only contacts that belong to the organization (blocks cross-tenant links)."""
    if not ids:
        return []
    marks = ", ".join(["%s"] * len(ids))
    with db() as c:
        rows = c.execute(
            f"SELECT id FROM alert_contacts WHERE organization_id = %s AND id IN ({marks})",
            [org_id, *ids],
        ).fetchall()
        return [r["id"] for r in rows]


def monitor_contact_ids(monitor_id: int) -> list[int]:
    with db() as c:
        return [r["contact_id"] for r in c.execute(
            "SELECT contact_id FROM monitor_contacts WHERE monitor_id = %s", (monitor_id,)
        ).fetchall()]


def verified_chat_ids_for_monitor(monitor_id: int) -> list[str]:
    with db() as c:
        return [r["chat_id"] for r in c.execute(
            "SELECT ac.chat_id FROM monitor_contacts mc "
            "JOIN alert_contacts ac ON ac.id = mc.contact_id "
            "WHERE mc.monitor_id = %s AND ac.verified = 1",
            (monitor_id,),
        ).fetchall()]


# --- monitors ---------------------------------------------------------------

def _set_monitor_contacts(c, monitor_id: int, contact_ids: list[int]) -> None:
    c.execute("DELETE FROM monitor_contacts WHERE monitor_id = %s", (monitor_id,))
    for cid in contact_ids:
        c.execute(
            "INSERT INTO monitor_contacts (monitor_id, contact_id) VALUES (%s, %s)",
            (monitor_id, cid),
        )


def list_monitors(org_id: int) -> list[dict]:
    with db() as c:
        return c.execute(
            "SELECT * FROM monitors WHERE organization_id = %s ORDER BY name", (org_id,)
        ).fetchall()


def enabled_monitors() -> list[dict]:
    """All organizations. Used by the background scheduler only."""
    with db() as c:
        return c.execute("SELECT * FROM monitors WHERE enabled = 1").fetchall()


def get_monitor(org_id: int, monitor_id: int) -> dict | None:
    with db() as c:
        return c.execute(
            "SELECT * FROM monitors WHERE id = %s AND organization_id = %s",
            (monitor_id, org_id),
        ).fetchone()


def create_monitor(org_id: int, data: dict, contact_ids: list[int]) -> int:
    cols = ", ".join(MONITOR_FIELDS + ("organization_id", "created_at"))
    marks = ", ".join(["%s"] * (len(MONITOR_FIELDS) + 2))
    values = [data[f] for f in MONITOR_FIELDS] + [org_id, ts(utcnow())]
    with db() as c:
        monitor_id = c.execute(
            f"INSERT INTO monitors ({cols}) VALUES ({marks}) RETURNING id", values
        ).fetchone()["id"]
        _set_monitor_contacts(c, monitor_id, contact_ids)
    return monitor_id


def update_monitor(org_id: int, monitor_id: int, data: dict, contact_ids: list[int]) -> None:
    sets = ", ".join(f"{f} = %s" for f in MONITOR_FIELDS)
    values = [data[f] for f in MONITOR_FIELDS] + [monitor_id, org_id]
    with db() as c:
        c.execute(f"UPDATE monitors SET {sets} WHERE id = %s AND organization_id = %s", values)
        _set_monitor_contacts(c, monitor_id, contact_ids)


def set_enabled(org_id: int, monitor_id: int, enabled: bool) -> None:
    with db() as c:
        c.execute(
            "UPDATE monitors SET enabled = %s WHERE id = %s AND organization_id = %s",
            (int(enabled), monitor_id, org_id),
        )


def delete_monitor(org_id: int, monitor_id: int) -> None:
    with db() as c:
        c.execute(
            "DELETE FROM monitors WHERE id = %s AND organization_id = %s",
            (monitor_id, org_id),
        )


def update_state(monitor_id: int, updates: dict) -> None:
    """Scheduler-owned status fields. Caller must have checked org access first."""
    updates = {k: v for k, v in updates.items() if k in STATE_FIELDS}
    if not updates:
        return
    sets = ", ".join(f"{k} = %s" for k in updates)
    with db() as c:
        c.execute(f"UPDATE monitors SET {sets} WHERE id = %s", [*updates.values(), monitor_id])


# --- checks -----------------------------------------------------------------

def insert_check(monitor_id: int, checked_at: str, ok: bool, status_code, response_ms, error) -> None:
    with db() as c:
        c.execute(
            "INSERT INTO checks (monitor_id, checked_at, ok, status_code, response_ms, error) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (monitor_id, checked_at, int(ok), status_code, response_ms, error),
        )


def uptime(monitor_id: int, hours: int) -> float | None:
    since = ts(utcnow() - timedelta(hours=hours))
    with db() as c:
        row = c.execute(
            "SELECT COUNT(*) AS total, COALESCE(SUM(ok), 0) AS up FROM checks "
            "WHERE monitor_id = %s AND checked_at >= %s",
            (monitor_id, since),
        ).fetchone()
    return round(100.0 * int(row["up"]) / row["total"], 2) if row["total"] else None


def recent_checks(monitor_id: int, limit: int = 50) -> list[dict]:
    with db() as c:
        return c.execute(
            "SELECT * FROM checks WHERE monitor_id = %s ORDER BY checked_at DESC LIMIT %s",
            (monitor_id, limit),
        ).fetchall()


def prune_checks(days: int) -> None:
    cutoff = ts(utcnow() - timedelta(days=days))
    with db() as c:
        c.execute("DELETE FROM checks WHERE checked_at < %s", (cutoff,))
