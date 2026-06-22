"""Shared SQLite layer for the web app and the worker.

The DB is the single source of truth. The web app inserts/cancels rows;
the worker polls for due messages, sends them, and updates their status.
"""
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data.db"
UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    target_type   TEXT NOT NULL,          -- 'number' or 'group'
    target_jid    TEXT NOT NULL,          -- e.g. '5511999999999@s.whatsapp.net' or 'xxxx@g.us'
    display_name  TEXT,                   -- human label for the queue UI
    body          TEXT,                   -- message text (may be empty if attachment-only)
    scheduled_at  INTEGER NOT NULL,       -- epoch seconds (Pi local time -> epoch)
    status        TEXT NOT NULL DEFAULT 'pending',  -- pending|sent|failed|canceled
    error         TEXT,
    created_at    INTEGER NOT NULL,
    sent_at       INTEGER
);

CREATE TABLE IF NOT EXISTS attachments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id  INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    path        TEXT NOT NULL,
    filename    TEXT NOT NULL,
    mimetype    TEXT
);

-- Cache of joined groups, refreshed by the worker so the web UI can offer a picker.
CREATE TABLE IF NOT EXISTS groups (
    jid         TEXT PRIMARY KEY,
    name        TEXT,
    updated_at  INTEGER
);
"""


@contextmanager
def connect():
    """Open a connection, commit on success, roll back on error, always close."""
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")  # lets web + worker read/write concurrently
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with connect() as conn:
        conn.executescript(SCHEMA)


def now() -> int:
    return int(time.time())


# ---------- web-app helpers ----------

def create_message(target_type, target_jid, display_name, body, scheduled_at):
    with connect() as conn:
        cur = conn.execute(
            """INSERT INTO messages
               (target_type, target_jid, display_name, body, scheduled_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (target_type, target_jid, display_name, body, scheduled_at, now()),
        )
        return cur.lastrowid


def add_attachment(message_id, path, filename, mimetype):
    with connect() as conn:
        conn.execute(
            "INSERT INTO attachments (message_id, path, filename, mimetype) VALUES (?, ?, ?, ?)",
            (message_id, str(path), filename, mimetype),
        )


def list_messages(limit=200):
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM messages ORDER BY scheduled_at DESC LIMIT ?", (limit,)
        ).fetchall()
        msgs = [dict(r) for r in rows]
        for m in msgs:
            atts = conn.execute(
                "SELECT * FROM attachments WHERE message_id = ?", (m["id"],)
            ).fetchall()
            m["attachments"] = [dict(a) for a in atts]
        return msgs


def cancel_message(message_id) -> bool:
    with connect() as conn:
        cur = conn.execute(
            "UPDATE messages SET status='canceled' WHERE id=? AND status='pending'",
            (message_id,),
        )
        return cur.rowcount > 0


def list_groups():
    with connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM groups ORDER BY name COLLATE NOCASE"
        ).fetchall()]


# ---------- worker helpers ----------

def due_messages():
    """Pending messages whose scheduled time has arrived."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM messages WHERE status='pending' AND scheduled_at <= ? ORDER BY scheduled_at",
            (now(),),
        ).fetchall()
        msgs = [dict(r) for r in rows]
        for m in msgs:
            atts = conn.execute(
                "SELECT * FROM attachments WHERE message_id = ?", (m["id"],)
            ).fetchall()
            m["attachments"] = [dict(a) for a in atts]
        return msgs


def mark_sent(message_id):
    with connect() as conn:
        conn.execute(
            "UPDATE messages SET status='sent', sent_at=?, error=NULL WHERE id=?",
            (now(), message_id),
        )


def mark_failed(message_id, error):
    with connect() as conn:
        conn.execute(
            "UPDATE messages SET status='failed', error=? WHERE id=?",
            (str(error)[:1000], message_id),
        )


def upsert_groups(groups):
    """groups: list of (jid, name)."""
    with connect() as conn:
        ts = now()
        conn.executemany(
            """INSERT INTO groups (jid, name, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(jid) DO UPDATE SET name=excluded.name, updated_at=excluded.updated_at""",
            [(j, n, ts) for j, n in groups],
        )
