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
        """)
    logger.info(f"DB initialized at {DB_PATH.resolve()}")


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

def save_result(scan_id: str, ev: Dict[str, Any]) -> None:
    """1銘柄の評価結果を保存する。"""
    now = datetime.utcnow().isoformat()
    with _get_conn() as conn:
        conn.execute(
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
