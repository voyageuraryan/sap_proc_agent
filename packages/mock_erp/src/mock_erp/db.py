import sqlite3
from pathlib import Path

SCHEMA = """
    CREATE TABLE IF NOT EXISTS proposals(
        sequence_id INTEGER PRIMARY KEY AUTOINCREMENT,
        proposal_id TEXT NOT NULL UNIQUE,
        status TEXT NOT NULL,
        scenario_id TEXT,
        invoice_number TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        payload_hash TEXT NOT NULL,
        agent_reasoning TEXT NOT NULL,
        proposed_by TEXT NOT NULL,
        proposed_at TEXT NOT NULL,
        approved_by TEXT,
        approved_at TEXT,
        approved_hash TEXT,
        rejected_by TEXT,
        rejected_at TEXT,
        rejection_reason TEXT,
        applied_at TEXT
    );
    
    CREATE TABLE IF NOT EXISTS amendments (
        proposal_id TEXT PRIMARY KEY,
        invoice_number TEXT NOT NULL,
        correction_type TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        applied_at TEXT NOT NULL,
        FOREIGN KEY (proposal_id) REFERENCES proposals(proposal_id)
    );
    
    CREATE INDEX IF NOT EXISTS idx_amendments_invoice_number
    ON amendments(invoice_number);
"""


def open_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    conn.row_factory = sqlite3.Row

    return conn
