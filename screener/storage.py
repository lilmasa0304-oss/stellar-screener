"""
SQLite ベースの永続化ストレージ。

スキャン結果をファイル（data/screener.db）に保存するため、
クラウド上でサーバーが再起動してもデータが消えません。

テーブル:
  scan_sessions  — スキャン実行ごとのメタ情報（開始/完了時刻・件数・ステータス）
  scan_results   — 各銘柄の評価結果
"""

import json
import sqlite3
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# SQLite ファイルのパス（環境変数で上書き可能）
import os
DB_PATH = Path(os.getenv("DB_PATH", "data/screener.db"))


# ─────────────────────────────────────────────────────────────────────────────
def _get_conn() -> sqlite3.Connection:
    """スレッドセーフな DB 接続を返す。"""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """DB とテーブルを初期化する（初回起動時に呼び出す）。"""
    with _get_conn() as conn:
        conn.executescript("""
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
            );

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
            );

            CREATE INDEX IF NOT EXISTS idx_sr_scan_id
                ON scan_results(scan_id);
            CREATE INDEX IF NOT EXISTS idx_sr_buy_signal
                ON scan_results(buy_signal);
            CREATE INDEX IF NOT EXISTS idx_ss_status
                ON scan_sessions(status);

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
                UNIQUE(scan_id, ticker, signal_date)
            );

            CREATE TABLE IF NOT EXISTS signal_track_outcomes (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                track_id         INTEGER NOT NULL,
                horizon_days     INTEGER NOT NULL,
                horizon_label    TEXT NOT NULL,
                eval_date        TEXT,
                exit_price       REAL,
                return_pct       REAL,
                max_return_pct   REAL,
                is_win           INTEGER,
                evaluated_at     TEXT,
                status           TEXT NOT NULL DEFAULT 'pending',
                UNIQUE(track_id, horizon_days),
                FOREIGN KEY (track_id) REFERENCES signal_tracks(track_id)
            );

            CREATE INDEX IF NOT EXISTS idx_st_status
                ON signal_tracks(status);
            CREATE INDEX IF NOT EXISTS idx_sto_track
                ON signal_track_outcomes(track_id);
            CREATE INDEX IF NOT EXISTS idx_sto_horizon
                ON signal_track_outcomes(horizon_days);
        """)
        _migrate_signal_tracks(conn)
    logger.info(f"DB initialized at {DB_PATH.resolve()}")


def _migrate_signal_tracks(conn: sqlite3.Connection) -> None:
    """既存 DB へ signal_tracks のライブ snapshot 列を追加する。"""
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
    for name, col_type in columns:
        try:
            conn.execute(f"ALTER TABLE signal_tracks ADD COLUMN {name} {col_type}")
        except sqlite3.OperationalError:
            pass
    try:
        conn.execute(
            "ALTER TABLE signal_track_outcomes ADD COLUMN min_return_pct REAL"
        )
    except sqlite3.OperationalError:
        pass
    conn.execute(
        "UPDATE signal_tracks SET status='archived' WHERE status='completed'"
    )
    for horizon, label in ((5, "5日目（1週間）"), (10, "10日目（2週間）")):
        conn.execute(
            """INSERT OR IGNORE INTO signal_track_outcomes
               (track_id, horizon_days, horizon_label, status)
               SELECT track_id, ?, ?, 'pending' FROM signal_tracks""",
            (horizon, label),
        )


# ─────────────────────────────────────────────────────────────────────────────
# セッション操作
# ─────────────────────────────────────────────────────────────────────────────

def create_session(scan_id: str, scan_type: str, total_tickers: int) -> None:
    """新しいスキャンセッションを作成する。"""
    now = datetime.utcnow().isoformat()
    with _get_conn() as conn:
        conn.execute(
            """INSERT INTO scan_sessions
               (scan_id, started_at, status, scan_type, total_tickers)
               VALUES (?, ?, 'running', ?, ?)""",
            (scan_id, now, scan_type, total_tickers),
        )


def update_session_progress(scan_id: str, processed: int) -> None:
    """処理済み件数を更新する。"""
    with _get_conn() as conn:
        conn.execute(
            "UPDATE scan_sessions SET processed=? WHERE scan_id=?",
            (processed, scan_id),
        )


def complete_session(
    scan_id: str,
    buy_signal_count: int,
    sent_line: bool,
    error_message: Optional[str] = None,
) -> None:
    """セッションを完了状態にする。"""
    now    = datetime.utcnow().isoformat()
    status = "failed" if error_message else "completed"
    with _get_conn() as conn:
        conn.execute(
            """UPDATE scan_sessions
               SET completed_at=?, status=?, buy_signal_count=?, sent_line=?, error_message=?
               WHERE scan_id=?""",
            (now, status, buy_signal_count, int(sent_line), error_message, scan_id),
        )


def get_session(scan_id: str) -> Optional[Dict[str, Any]]:
    """セッション情報を取得する。"""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM scan_sessions WHERE scan_id=?", (scan_id,)
        ).fetchone()
    return dict(row) if row else None


def get_latest_session(scan_type: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """最新のセッション情報を返す。"""
    with _get_conn() as conn:
        if scan_type:
            row = conn.execute(
                "SELECT * FROM scan_sessions WHERE scan_type=? ORDER BY started_at DESC LIMIT 1",
                (scan_type,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM scan_sessions ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
    return dict(row) if row else None


def list_sessions(limit: int = 20) -> List[Dict[str, Any]]:
    """過去のスキャンセッション一覧を返す（新しい順）。"""
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM scan_sessions ORDER BY started_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


# ─────────────────────────────────────────────────────────────────────────────
# 銘柄結果操作
# ─────────────────────────────────────────────────────────────────────────────

def save_result(scan_id: str, ev: Dict[str, Any]) -> int:
    """1銘柄の評価結果を保存する。"""
    now = datetime.utcnow().isoformat()
    with _get_conn() as conn:
        cursor = conn.execute(
            """INSERT INTO scan_results
               (scan_id, ticker, name, current_price, change_percent,
                buy_signal, is_prime_entry, triggered, signals,
                rsi, ma25, macd, macd_signal, macd_hist,
                macd_crossover, macd_pre_crossover, ma25_uptrend, scanned_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                scan_id,
                ev["ticker"],
                ev["name"],
                ev.get("current_price"),
                ev.get("change_percent"),
                int(ev.get("buy_signal", False)),
                int(ev.get("is_prime_entry", False)),
                int(ev.get("triggered", False)),
                json.dumps(ev.get("signals", []), ensure_ascii=False),
                ev.get("rsi"),
                ev.get("ma25"),
                ev.get("macd"),
                ev.get("macd_signal"),
                ev.get("macd_hist"),
                int(ev.get("macd_crossover", False)),
                int(ev.get("macd_pre_crossover", False)),
                int(ev.get("ma25_uptrend", False)),
                now,
            ),
        )
        return int(cursor.lastrowid)


def get_results(scan_id: str, buy_signal_only: bool = False) -> List[Dict[str, Any]]:
    """指定セッションの銘柄結果を取得する。"""
    with _get_conn() as conn:
        if buy_signal_only:
            rows = conn.execute(
                "SELECT * FROM scan_results WHERE scan_id=? AND buy_signal=1 ORDER BY rsi",
                (scan_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM scan_results WHERE scan_id=? ORDER BY buy_signal DESC, rsi",
                (scan_id,),
            ).fetchall()
    results = []
    for r in rows:
        d = dict(r)
        d["signals"] = json.loads(d["signals"]) if d["signals"] else []
        d["buy_signal"]         = bool(d["buy_signal"])
        d["is_prime_entry"]     = bool(d["is_prime_entry"])
        d["triggered"]          = bool(d["triggered"])
        d["macd_crossover"]     = bool(d["macd_crossover"])
        d["macd_pre_crossover"] = bool(d["macd_pre_crossover"])
        d["ma25_uptrend"]       = bool(d["ma25_uptrend"])
        results.append(d)
    return results


def get_history_buy_signals(limit: int = 50) -> List[Dict[str, Any]]:
    """過去のスキャンで BUY SIGNAL が出た銘柄を新しい順に返す。"""
    with _get_conn() as conn:
        rows = conn.execute(
            """SELECT r.*, s.started_at as session_started_at, s.scan_type
               FROM scan_results r
               JOIN scan_sessions s ON r.scan_id = s.scan_id
               WHERE r.buy_signal = 1
               ORDER BY r.scanned_at DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
    results = []
    for r in rows:
        d = dict(r)
        d["signals"] = json.loads(d["signals"]) if d["signals"] else []
        d["buy_signal"]         = bool(d["buy_signal"])
        d["ma25_uptrend"]       = bool(d["ma25_uptrend"])
        d["macd_crossover"]     = bool(d["macd_crossover"])
        d["macd_pre_crossover"] = bool(d["macd_pre_crossover"])
        results.append(d)
    return results


def register_signal_track(
    *,
    scan_id: str,
    ticker: str,
    name: str,
    signal_date: str,
    entry_price: float,
    preset_matched: Optional[str] = None,
    risk_mode: Optional[str] = None,
    scan_result_id: Optional[int] = None,
) -> Optional[int]:
    """BUY SIGNAL を追跡テーブルへ登録する（重複は無視）。"""
    now = datetime.utcnow().isoformat()
    with _get_conn() as conn:
        try:
            cursor = conn.execute(
                """INSERT INTO signal_tracks
                   (scan_result_id, scan_id, ticker, name, signal_date,
                    entry_price, preset_matched, risk_mode, registered_at, status)
                   VALUES (?,?,?,?,?,?,?,?,?, 'tracking')""",
                (
                    scan_result_id,
                    scan_id,
                    ticker,
                    name,
                    signal_date,
                    entry_price,
                    preset_matched,
                    risk_mode,
                    now,
                ),
            )
            track_id = int(cursor.lastrowid)
        except sqlite3.IntegrityError:
            row = conn.execute(
                """SELECT track_id FROM signal_tracks
                   WHERE scan_id=? AND ticker=? AND signal_date=?""",
                (scan_id, ticker, signal_date),
            ).fetchone()
            track_id = int(row["track_id"]) if row else None
            if track_id is None:
                return None

        for horizon, label in ((3, "3日目"), (5, "5日目（1週間）"), (10, "10日目（2週間）")):
            conn.execute(
                """INSERT OR IGNORE INTO signal_track_outcomes
                   (track_id, horizon_days, horizon_label, status)
                   VALUES (?,?,?, 'pending')""",
                (track_id, horizon, label),
            )
        return track_id


def list_active_signal_tracks(limit: int = 100) -> List[Dict[str, Any]]:
    with _get_conn() as conn:
        rows = conn.execute(
            """SELECT * FROM signal_tracks
               WHERE status='tracking'
               ORDER BY registered_at DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def mark_track_completed(track_id: int) -> None:
    """後方互換: 追跡完了。"""
    mark_track_archived(track_id)


def mark_track_archived(
    track_id: int,
    *,
    final_return_pct: Optional[float] = None,
    final_is_win: Optional[bool] = None,
    max_return_pct: Optional[float] = None,
    min_return_pct: Optional[float] = None,
) -> None:
    now = datetime.utcnow().isoformat()
    with _get_conn() as conn:
        conn.execute(
            """UPDATE signal_tracks
               SET status='archived',
                   archived_at=?,
                   final_return_pct=COALESCE(?, final_return_pct),
                   final_is_win=COALESCE(?, final_is_win),
                   max_return_pct=COALESCE(?, max_return_pct),
                   min_return_pct=COALESCE(?, min_return_pct),
                   last_updated_at=?
               WHERE track_id=?""",
            (
                now,
                final_return_pct,
                int(final_is_win) if final_is_win is not None else None,
                max_return_pct,
                min_return_pct,
                now,
                track_id,
            ),
        )


def update_track_snapshot(
    track_id: int,
    *,
    current_price: Optional[float] = None,
    current_return_pct: Optional[float] = None,
    period_high: Optional[float] = None,
    period_low: Optional[float] = None,
    max_return_pct: Optional[float] = None,
    min_return_pct: Optional[float] = None,
    business_days_elapsed: Optional[int] = None,
) -> None:
    now = datetime.utcnow().isoformat()
    with _get_conn() as conn:
        conn.execute(
            """UPDATE signal_tracks
               SET current_price=?,
                   current_return_pct=?,
                   period_high=?,
                   period_low=?,
                   max_return_pct=?,
                   min_return_pct=?,
                   business_days_elapsed=?,
                   last_updated_at=?
               WHERE track_id=?""",
            (
                current_price,
                current_return_pct,
                period_high,
                period_low,
                max_return_pct,
                min_return_pct,
                business_days_elapsed,
                now,
                track_id,
            ),
        )


def list_signal_tracks(
    *,
    status: Optional[str] = None,
    risk_mode: Optional[str] = None,
    limit: int = 200,
) -> List[Dict[str, Any]]:
    clauses: List[str] = []
    params: List[Any] = []
    if status:
        clauses.append("status = ?")
        params.append(status)
    if risk_mode:
        clauses.append("risk_mode = ?")
        params.append(risk_mode)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    with _get_conn() as conn:
        rows = conn.execute(
            f"""SELECT * FROM signal_tracks
                {where}
                ORDER BY registered_at DESC
                LIMIT ?""",
            params,
        ).fetchall()
    results = []
    for row in rows:
        data = dict(row)
        if data.get("final_is_win") is not None:
            data["final_is_win"] = bool(data["final_is_win"])
        results.append(data)
    return results


def get_track_outcome(track_id: int, horizon_days: int) -> Optional[Dict[str, Any]]:
    with _get_conn() as conn:
        row = conn.execute(
            """SELECT * FROM signal_track_outcomes
               WHERE track_id=? AND horizon_days=?""",
            (track_id, horizon_days),
        ).fetchone()
    if not row:
        return None
    data = dict(row)
    if data.get("is_win") is not None:
        data["is_win"] = bool(data["is_win"])
    return data


def upsert_track_outcome(
    *,
    track_id: int,
    horizon_days: int,
    horizon_label: str,
    eval_date: Optional[str] = None,
    exit_price: Optional[float] = None,
    return_pct: Optional[float] = None,
    max_return_pct: Optional[float] = None,
    min_return_pct: Optional[float] = None,
    is_win: Optional[bool] = None,
    status: str,
) -> None:
    now = datetime.utcnow().isoformat()
    with _get_conn() as conn:
        conn.execute(
            """INSERT INTO signal_track_outcomes
               (track_id, horizon_days, horizon_label, eval_date, exit_price,
                return_pct, max_return_pct, min_return_pct, is_win, evaluated_at, status)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(track_id, horizon_days) DO UPDATE SET
                 horizon_label=excluded.horizon_label,
                 eval_date=excluded.eval_date,
                 exit_price=excluded.exit_price,
                 return_pct=excluded.return_pct,
                 max_return_pct=excluded.max_return_pct,
                 min_return_pct=excluded.min_return_pct,
                 is_win=excluded.is_win,
                 evaluated_at=excluded.evaluated_at,
                 status=excluded.status""",
            (
                track_id,
                horizon_days,
                horizon_label,
                eval_date,
                exit_price,
                return_pct,
                max_return_pct,
                min_return_pct,
                int(is_win) if is_win is not None else None,
                now if status == "complete" else None,
                status,
            ),
        )


def _track_filters_sql(
    risk_mode: Optional[str],
    preset_matched: Optional[str],
) -> tuple[str, List[Any]]:
    clauses: List[str] = []
    params: List[Any] = []
    if risk_mode:
        clauses.append("t.risk_mode = ?")
        params.append(risk_mode)
    if preset_matched:
        clauses.append("t.preset_matched = ?")
        params.append(preset_matched)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return where, params


def list_track_outcomes(
    *,
    risk_mode: Optional[str] = None,
    preset_matched: Optional[str] = None,
    limit: int = 5000,
) -> List[Dict[str, Any]]:
    where, params = _track_filters_sql(risk_mode, preset_matched)
    params.append(limit)
    with _get_conn() as conn:
        rows = conn.execute(
            f"""SELECT o.*, t.ticker, t.name, t.signal_date, t.risk_mode, t.preset_matched
                FROM signal_track_outcomes o
                JOIN signal_tracks t ON t.track_id = o.track_id
                {where}
                ORDER BY t.registered_at DESC, o.horizon_days ASC
                LIMIT ?""",
            params,
        ).fetchall()
    results = []
    for row in rows:
        data = dict(row)
        if data.get("is_win") is not None:
            data["is_win"] = bool(data["is_win"])
        results.append(data)
    return results


def count_signal_tracks(
    *,
    risk_mode: Optional[str] = None,
    preset_matched: Optional[str] = None,
) -> int:
    where, params = _track_filters_sql(risk_mode, preset_matched)
    with _get_conn() as conn:
        row = conn.execute(
            f"SELECT COUNT(*) AS cnt FROM signal_tracks t {where}",
            params,
        ).fetchone()
    return int(row["cnt"]) if row else 0
