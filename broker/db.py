"""Database layer: connect(), init_db(), transaction().

Plain standard-library sqlite3, no ORM, so every query is visible SQL.
"""

import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
SCHEMA_VERSION = 2


def connect(path, check_same_thread: bool = True) -> sqlite3.Connection:
    """Open the database with the settings every connection must have.

    check_same_thread=False is only for a connection that moves between
    threads but is still used by one caller at a time (see api.get_conn).
    """
    # isolation_level=None: Python's sqlite3 would otherwise open transactions
    # implicitly before INSERT/UPDATE. We want the only transactions to be the
    # ones transaction() starts, so it is always clear what is atomic.
    conn = sqlite3.connect(
        path, isolation_level=None, check_same_thread=check_same_thread
    )

    # Rows can be read by column name (row["status"]) as well as by index.
    conn.row_factory = sqlite3.Row

    # SQLite does not enforce REFERENCES unless this is on, and it is a
    # per-connection setting, so it must be set every time.
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL: readers don't block the writer, and a crash mid-write never
    # corrupts committed data.
    conn.execute("PRAGMA journal_mode = WAL")
    # NORMAL is safe against an app crash in WAL mode; only a power loss or an
    # OS crash can lose the last few commits. Faster than FULL.
    conn.execute("PRAGMA synchronous = NORMAL")
    # If another connection holds the write lock, wait up to 5 s instead of
    # failing at once with "database is locked".
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection):
    """Run a block atomically: everything commits, or nothing does.

    Every state change that touches more than one row (e.g. placing a job on a
    node) must happen in ONE transaction, so a crash never leaves a job
    "running" on a node that doesn't exist.

    BEGIN IMMEDIATE takes the write lock at the start rather than at the first
    write, so a check-then-write inside the block (e.g. "does the node still
    have free GPUs?") cannot race another writer.

    Not re-entrant: SQLite has no nested BEGIN, so don't call this inside itself.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise  # the caller still sees the original error
    else:
        conn.execute("COMMIT")


def init_db(conn: sqlite3.Connection, start_ts: int = 0) -> None:
    """Create or upgrade the schema. Safe to call on every start (idempotent).

    start_ts is the simulated time to start at, used only when the clock row
    doesn't exist yet. On a restart the stored clock wins, so time resumes
    where it stopped.
    """
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        raise RuntimeError(
            f"database schema v{version} is newer than this code (v{SCHEMA_VERSION})"
        )
    if version < SCHEMA_VERSION and _table_exists(conn, "jobs"):
        _migrate_v1_to_v2(conn)

    # executescript() commits any open transaction before it runs, so the DDL
    # cannot be inside transaction(). That is acceptable: every statement is
    # IF NOT EXISTS, so if a crash interrupts it, the next init_db() finishes it.
    conn.executescript(SCHEMA_PATH.read_text())

    with transaction(conn):
        # PRAGMA values cannot be bound as ? parameters; the value is our own
        # integer constant, not user input.
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        # OR IGNORE: insert only if the row is missing; never reset the clock.
        conn.execute(
            "INSERT OR IGNORE INTO sim_clock (id, now_ts) VALUES (1, ?)",
            (start_ts,),
        )


def _table_exists(conn, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


NODE_COLUMNS = (
    "id, node_type, gpus_total, interruptible, price_per_hour_micros, team, "
    "state, launched_at, ready_at, idle_since, terminated_at"
)


def _migrate_v1_to_v2(conn: sqlite3.Connection) -> None:
    """v1 -> v2: add jobs.created_seq; new nodes.end_reason values.

    SQLite can't change a CHECK constraint in place, so `nodes` is rebuilt:
    create the v2 table, copy the rows (mapping old reasons), drop the old one,
    rename. All in one transaction: a crash leaves either v1 or v2, never half.
    Foreign keys must be off for the drop (jobs references nodes), and that
    PRAGMA has no effect inside a transaction, so it is set around it.
    """
    nodes_ddl = re.search(
        r"CREATE TABLE IF NOT EXISTS nodes \(.*?\n\);",
        SCHEMA_PATH.read_text(),
        re.DOTALL,
    ).group(0)
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        with transaction(conn):
            conn.execute("ALTER TABLE jobs ADD COLUMN created_seq INTEGER")
            # rowid is insertion order, i.e. arrival order, the v1 tie-break.
            conn.execute("UPDATE jobs SET created_seq = rowid")
            conn.execute(nodes_ddl.replace("IF NOT EXISTS nodes (", "nodes_v2 (", 1))
            # v1 used idle_reaped for both reaping and eviction; they can't be
            # told apart any more, so both become idle_timeout.
            conn.execute(
                f"""INSERT INTO nodes_v2 ({NODE_COLUMNS}, end_reason)
                    SELECT {NODE_COLUMNS},
                           CASE end_reason WHEN 'idle_reaped' THEN 'idle_timeout'
                                           WHEN 'interrupted' THEN 'provider_reclaimed'
                                           ELSE end_reason END
                    FROM nodes"""
            )
            conn.execute("DROP TABLE nodes")
            conn.execute("ALTER TABLE nodes_v2 RENAME TO nodes")
            broken = conn.execute("PRAGMA foreign_key_check").fetchall()
            if broken:
                raise RuntimeError(f"migration broke foreign keys: {broken}")
            conn.execute("PRAGMA user_version = 2")
    finally:
        conn.execute("PRAGMA foreign_keys = ON")
