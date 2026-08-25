"""DB スキーマ初期化（SQLite / PostgreSQL）。"""

from __future__ import annotations

from sqlalchemy.engine import Connection

from screener.database import execute, fetchall, fetchone, get_backend


def init_schema(conn: Connection) -> None:
    backend = get_backend()
    if backend == "postgresql":
        _init_schema_postgresql(conn)
    else:
        _init_schema_sqlite(conn)
    _migrate_signal_tracks(conn)


def _init_schema_sqlite(conn: Connection) -> None:
    execute(conn, """
        CREATE TABLE IF NOT EXISTS scan_sessions (
            scan_id          TEXT PRIMARY KEY,
            started_at       TEXT NOT NULL,
            completed_at     TEXT,
            status           TEXT NOT NULL DEFAULT 'running',
            scan_type        TEXT NOT NULL DEFAULT 'manual',
            total_tickers    INTEGER DEFAULT 0,
            processed        INTEGER DEFAULT 0,
            buy_signal_count INTEGER DEFAULT 0,
            sent_line        INTEGER DEFAULT 0,
            error_message    TEXT
        )
    """)
    execute(conn, """
        CREATE TABLE IF NOT EXISTS scan_results (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            scan_id          TEXT NOT NULL,
            ticker           TEXT NOT NULL,
            name             TEXT NOT NULL,
            current_price    REAL,
            change_percent   REAL,
            buy_signal       INTEGER DEFAULT 0,
            is_prime_entry   INTEGER DEFAULT 0,
            triggered        INTEGER DEFAULT 0,
            signals          TEXT,
            rsi              REAL,
            ma25             REAL,
            macd             REAL,
            macd_signal      REAL,
            macd_hist        REAL,
            macd_crossover   INTEGER DEFAULT 0,
            macd_pre_crossover INTEGER DEFAULT 0,
            ma25_uptrend     INTEGER DEFAULT 0,
            scanned_at       TEXT NOT NULL,
            FOREIGN KEY (scan_id) REFERENCES scan_sessions(scan_id)
        )
    """)
    execute(conn, """
        CREATE TABLE IF NOT EXISTS signal_tracks (
            track_id         INTEGER PRIMARY KEY AUTOINCREMENT,
            scan_result_id   INTEGER,
            scan_id          TEXT NOT NULL,
            ticker           TEXT NOT NULL,
            name             TEXT NOT NULL,
            signal_date      TEXT NOT NULL,
            entry_price      REAL NOT NULL,
            preset_matched   TEXT,
            risk_mode        TEXT,
            registered_at    TEXT NOT NULL,
            status           TEXT NOT NULL DEFAULT 'tracking',
            current_price    REAL,
            current_return_pct REAL,
            period_high      REAL,
            period_low       REAL,
            max_return_pct   REAL,
            min_return_pct   REAL,
            business_days_elapsed INTEGER,
            final_return_pct REAL,
            final_is_win     INTEGER,
            archived_at      TEXT,
            last_updated_at  TEXT,
            UNIQUE(scan_id, ticker, signal_date)
        )
    """)
    execute(conn, """
        CREATE TABLE IF NOT EXISTS signal_track_outcomes (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            track_id         INTEGER NOT NULL,
            horizon_days     INTEGER NOT NULL,
            horizon_label    TEXT NOT NULL,
            eval_date        TEXT,
            exit_price       REAL,
            return_pct       REAL,
            max_return_pct   REAL,
            min_return_pct   REAL,
            is_win           INTEGER,
            evaluated_at     TEXT,
            status           TEXT NOT NULL DEFAULT 'pending',
            UNIQUE(track_id, horizon_days),
            FOREIGN KEY (track_id) REFERENCES signal_tracks(track_id)
        )
    """)
    for sql in (
        "CREATE INDEX IF NOT EXISTS idx_sr_scan_id ON scan_results(scan_id)",
        "CREATE INDEX IF NOT EXISTS idx_sr_buy_signal ON scan_results(buy_signal)",
        "CREATE INDEX IF NOT EXISTS idx_ss_status ON scan_sessions(status)",
        "CREATE INDEX IF NOT EXISTS idx_st_status ON signal_tracks(status)",
        "CREATE INDEX IF NOT EXISTS idx_sto_track ON signal_track_outcomes(track_id)",
        "CREATE INDEX IF NOT EXISTS idx_sto_horizon ON signal_track_outcomes(horizon_days)",
    ):
        execute(conn, sql)


def _init_schema_postgresql(conn: Connection) -> None:
    execute(conn, """
        CREATE TABLE IF NOT EXISTS scan_sessions (
            scan_id          TEXT PRIMARY KEY,
            started_at       TEXT NOT NULL,
            completed_at     TEXT,
            status           TEXT NOT NULL DEFAULT 'running',
            scan_type        TEXT NOT NULL DEFAULT 'manual',
            total_tickers    INTEGER DEFAULT 0,
            processed        INTEGER DEFAULT 0,
            buy_signal_count INTEGER DEFAULT 0,
            sent_line        INTEGER DEFAULT 0,
            error_message    TEXT
        )
    """)
    execute(conn, """
        CREATE TABLE IF NOT EXISTS scan_results (
            id               SERIAL PRIMARY KEY,
            scan_id          TEXT NOT NULL,
            ticker           TEXT NOT NULL,
            name             TEXT NOT NULL,
            current_price    DOUBLE PRECISION,
            change_percent   DOUBLE PRECISION,
            buy_signal       INTEGER DEFAULT 0,
            is_prime_entry   INTEGER DEFAULT 0,
            triggered        INTEGER DEFAULT 0,
            signals          TEXT,
            rsi              DOUBLE PRECISION,
            ma25             DOUBLE PRECISION,
            macd             DOUBLE PRECISION,
            macd_signal      DOUBLE PRECISION,
            macd_hist        DOUBLE PRECISION,
            macd_crossover   INTEGER DEFAULT 0,
            macd_pre_crossover INTEGER DEFAULT 0,
            ma25_uptrend     INTEGER DEFAULT 0,
            scanned_at       TEXT NOT NULL,
            FOREIGN KEY (scan_id) REFERENCES scan_sessions(scan_id)
        )
    """)
    execute(conn, """
        CREATE TABLE IF NOT EXISTS signal_tracks (
            track_id         SERIAL PRIMARY KEY,
            scan_result_id   INTEGER,
            scan_id          TEXT NOT NULL,
            ticker           TEXT NOT NULL,
            name             TEXT NOT NULL,
            signal_date      TEXT NOT NULL,
            entry_price      DOUBLE PRECISION NOT NULL,
            preset_matched   TEXT,
            risk_mode        TEXT,
            registered_at    TEXT NOT NULL,
            status           TEXT NOT NULL DEFAULT 'tracking',
            current_price    DOUBLE PRECISION,
            current_return_pct DOUBLE PRECISION,
            period_high      DOUBLE PRECISION,
            period_low       DOUBLE PRECISION,
            max_return_pct   DOUBLE PRECISION,
            min_return_pct   DOUBLE PRECISION,
            business_days_elapsed INTEGER,
            final_return_pct DOUBLE PRECISION,
            final_is_win     INTEGER,
            archived_at      TEXT,
            last_updated_at  TEXT,
            UNIQUE(scan_id, ticker, signal_date)
        )
    """)
    execute(conn, """
        CREATE TABLE IF NOT EXISTS signal_track_outcomes (
            id               SERIAL PRIMARY KEY,
            track_id         INTEGER NOT NULL,
            horizon_days     INTEGER NOT NULL,
            horizon_label    TEXT NOT NULL,
            eval_date        TEXT,
            exit_price       DOUBLE PRECISION,
            return_pct       DOUBLE PRECISION,
            max_return_pct   DOUBLE PRECISION,
            min_return_pct   DOUBLE PRECISION,
            is_win           INTEGER,
            evaluated_at     TEXT,
            status           TEXT NOT NULL DEFAULT 'pending',
            UNIQUE(track_id, horizon_days),
            FOREIGN KEY (track_id) REFERENCES signal_tracks(track_id)
        )
    """)
    for sql in (
        "CREATE INDEX IF NOT EXISTS idx_sr_scan_id ON scan_results(scan_id)",
        "CREATE INDEX IF NOT EXISTS idx_sr_buy_signal ON scan_results(buy_signal)",
        "CREATE INDEX IF NOT EXISTS idx_ss_status ON scan_sessions(status)",
        "CREATE INDEX IF NOT EXISTS idx_st_status ON signal_tracks(status)",
        "CREATE INDEX IF NOT EXISTS idx_sto_track ON signal_track_outcomes(track_id)",
        "CREATE INDEX IF NOT EXISTS idx_sto_horizon ON signal_track_outcomes(horizon_days)",
    ):
        execute(conn, sql)


def _column_exists(conn: Connection, table: str, column: str) -> bool:
    backend = get_backend()
    if backend == "postgresql":
        row = fetchone(
            conn,
            """
            SELECT 1 AS ok FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = ? AND column_name = ?
            """,
            (table, column),
        )
        return row is not None
    rows = fetchall(conn, f"PRAGMA table_info({table})")
    return any(r.get("name") == column for r in rows)


def _migrate_signal_tracks(conn: Connection) -> None:
    columns = (
        ("current_price", "REAL"),
        ("current_return_pct", "REAL"),
        ("period_high", "REAL"),
        ("period_low", "REAL"),
        ("max_return_pct", "REAL"),
        ("min_return_pct", "REAL"),
        ("business_days_elapsed", "INTEGER"),
        ("final_return_pct", "REAL"),
        ("final_is_win", "INTEGER"),
        ("archived_at", "TEXT"),
        ("last_updated_at", "TEXT"),
    )
    pg_type = {
        "REAL": "DOUBLE PRECISION",
        "INTEGER": "INTEGER",
        "TEXT": "TEXT",
    }
    for name, col_type in columns:
        if not _column_exists(conn, "signal_tracks", name):
            ddl_type = pg_type[col_type] if get_backend() == "postgresql" else col_type
            execute(conn, f"ALTER TABLE signal_tracks ADD COLUMN {name} {ddl_type}")

    if not _column_exists(conn, "signal_track_outcomes", "min_return_pct"):
        ddl = "DOUBLE PRECISION" if get_backend() == "postgresql" else "REAL"
        execute(conn, f"ALTER TABLE signal_track_outcomes ADD COLUMN min_return_pct {ddl}")

    execute(conn, "UPDATE signal_tracks SET status='archived' WHERE status='completed'")

    for horizon, label in ((5, "5日目（1週間）"), (10, "10日目（2週間）")):
        if get_backend() == "postgresql":
            execute(
                conn,
                """
                INSERT INTO signal_track_outcomes (track_id, horizon_days, horizon_label, status)
                SELECT track_id, ?, ?, 'pending' FROM signal_tracks
                ON CONFLICT (track_id, horizon_days) DO NOTHING
                """,
                (horizon, label),
            )
        else:
            execute(
                conn,
                """
                INSERT OR IGNORE INTO signal_track_outcomes
                (track_id, horizon_days, horizon_label, status)
                SELECT track_id, ?, ?, 'pending' FROM signal_tracks
                """,
                (horizon, label),
            )
