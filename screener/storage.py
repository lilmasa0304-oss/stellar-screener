"""
永続化ストレージ（PostgreSQL / SQLite）。

DATABASE_URL 設定時は Supabase PostgreSQL 等の外部 DB へ接続。
未設定時はローカル SQLite（data/screener.db）を使用する。
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from screener.database import (
    connect,
    execute,
    fetchall,
    fetchone,
    get_backend,
    get_db_init_error,
    get_storage_info,
    initialize_database_schema,
    insert_ignore,
    insert_returning_id,
    is_persistent_storage,
    reset_engine,
)
from screener.db_path import resolve_db_path

logger = logging.getLogger(__name__)

DB_PATH: Path = resolve_db_path()
_storage_warned_ephemeral = False


def refresh_db_path() -> Path:
    """環境変数変更後に DB パスを再解決する（主にテスト用）。"""
    global DB_PATH
    reset_engine()
    DB_PATH = resolve_db_path()
    return DB_PATH


def _warn_if_ephemeral() -> None:
    global _storage_warned_ephemeral
    if _storage_warned_ephemeral:
        return
    if os.getenv("RENDER") == "true" and not is_persistent_storage():
        logger.warning(
            "Render 上で DATABASE_URL が未設定です（エフェメラル SQLite: %s）。"
            "検証リストを永続化するには Supabase 等の DATABASE_URL を設定してください。",
            DB_PATH.resolve(),
        )
        _storage_warned_ephemeral = True


def init_db() -> bool:
    """DB とテーブルを初期化する（初回起動時に呼び出す）。失敗時は False。"""
    _warn_if_ephemeral()
    return initialize_database_schema()


# ─────────────────────────────────────────────────────────────────────────────
# セッション操作
# ─────────────────────────────────────────────────────────────────────────────

def create_session(scan_id: str, scan_type: str, total_tickers: int) -> None:
    now = datetime.utcnow().isoformat()
    with connect() as conn:
        execute(
            conn,
            """INSERT INTO scan_sessions
               (scan_id, started_at, status, scan_type, total_tickers)
               VALUES (?, ?, 'running', ?, ?)""",
            (scan_id, now, scan_type, total_tickers),
        )


def update_session_progress(scan_id: str, processed: int) -> None:
    with connect() as conn:
        execute(
            conn,
            "UPDATE scan_sessions SET processed=? WHERE scan_id=?",
            (processed, scan_id),
        )


def complete_session(
    scan_id: str,
    buy_signal_count: int,
    sent_line: bool,
    error_message: Optional[str] = None,
) -> None:
    now = datetime.utcnow().isoformat()
    status = "failed" if error_message else "completed"
    with connect() as conn:
        execute(
            conn,
            """UPDATE scan_sessions
               SET completed_at=?, status=?, buy_signal_count=?, sent_line=?, error_message=?
               WHERE scan_id=?""",
            (now, status, buy_signal_count, int(sent_line), error_message, scan_id),
        )


def get_session(scan_id: str) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        return fetchone(conn, "SELECT * FROM scan_sessions WHERE scan_id=?", (scan_id,))


def get_latest_session(scan_type: Optional[str] = None) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        if scan_type:
            return fetchone(
                conn,
                "SELECT * FROM scan_sessions WHERE scan_type=? ORDER BY started_at DESC LIMIT 1",
                (scan_type,),
            )
        return fetchone(
            conn,
            "SELECT * FROM scan_sessions ORDER BY started_at DESC LIMIT 1",
        )


def list_sessions(limit: int = 20) -> List[Dict[str, Any]]:
    with connect() as conn:
        return fetchall(
            conn,
            "SELECT * FROM scan_sessions ORDER BY started_at DESC LIMIT ?",
            (limit,),
        )


# ─────────────────────────────────────────────────────────────────────────────
# 銘柄結果操作
# ─────────────────────────────────────────────────────────────────────────────

def save_result(scan_id: str, ev: Dict[str, Any]) -> int:
    now = datetime.utcnow().isoformat()
    with connect() as conn:
        return insert_returning_id(
            conn,
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
            pk="id",
        )


def _normalize_scan_result(row: Dict[str, Any]) -> Dict[str, Any]:
    data = dict(row)
    data["signals"] = json.loads(data["signals"]) if data.get("signals") else []
    data["buy_signal"] = bool(data.get("buy_signal"))
    data["is_prime_entry"] = bool(data.get("is_prime_entry"))
    data["triggered"] = bool(data.get("triggered"))
    data["macd_crossover"] = bool(data.get("macd_crossover"))
    data["macd_pre_crossover"] = bool(data.get("macd_pre_crossover"))
    data["ma25_uptrend"] = bool(data.get("ma25_uptrend"))
    return data


def get_results(scan_id: str, buy_signal_only: bool = False) -> List[Dict[str, Any]]:
    with connect() as conn:
        if buy_signal_only:
            rows = fetchall(
                conn,
                "SELECT * FROM scan_results WHERE scan_id=? AND buy_signal=1 ORDER BY rsi",
                (scan_id,),
            )
        else:
            rows = fetchall(
                conn,
                "SELECT * FROM scan_results WHERE scan_id=? ORDER BY buy_signal DESC, rsi",
                (scan_id,),
            )
    return [_normalize_scan_result(r) for r in rows]


def get_history_buy_signals(limit: int = 50) -> List[Dict[str, Any]]:
    with connect() as conn:
        rows = fetchall(
            conn,
            """SELECT r.*, s.started_at as session_started_at, s.scan_type
               FROM scan_results r
               JOIN scan_sessions s ON r.scan_id = s.scan_id
               WHERE r.buy_signal = 1
               ORDER BY r.scanned_at DESC
               LIMIT ?""",
            (limit,),
        )
    results = []
    for row in rows:
        data = _normalize_scan_result(row)
        results.append(data)
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
    now = datetime.utcnow().isoformat()
    with connect() as conn:
        if get_backend() == "postgresql":
            row = fetchone(
                conn,
                """INSERT INTO signal_tracks
                   (scan_result_id, scan_id, ticker, name, signal_date,
                    entry_price, preset_matched, risk_mode, registered_at, status)
                   VALUES (?,?,?,?,?,?,?,?,?, 'tracking')
                   ON CONFLICT (scan_id, ticker, signal_date) DO NOTHING
                   RETURNING track_id""",
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
            if row:
                track_id = int(row["track_id"])
            else:
                existing = fetchone(
                    conn,
                    """SELECT track_id FROM signal_tracks
                       WHERE scan_id=? AND ticker=? AND signal_date=?""",
                    (scan_id, ticker, signal_date),
                )
                track_id = int(existing["track_id"]) if existing else None
        else:
            try:
                track_id = insert_returning_id(
                    conn,
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
                    pk="track_id",
                )
            except Exception as exc:
                from sqlalchemy.exc import IntegrityError

                if not isinstance(exc, IntegrityError):
                    raise
                existing = fetchone(
                    conn,
                    """SELECT track_id FROM signal_tracks
                       WHERE scan_id=? AND ticker=? AND signal_date=?""",
                    (scan_id, ticker, signal_date),
                )
                track_id = int(existing["track_id"]) if existing else None

        if track_id is None:
            return None

        for horizon, label in ((3, "3日目"), (5, "5日目（1週間）"), (10, "10日目（2週間）")):
            insert_ignore(
                conn,
                "signal_track_outcomes",
                ["track_id", "horizon_days", "horizon_label", "status"],
                (track_id, horizon, label, "pending"),
                ["track_id", "horizon_days"],
            )
        return track_id


def list_active_signal_tracks(limit: int = 100) -> List[Dict[str, Any]]:
    with connect() as conn:
        return fetchall(
            conn,
            """SELECT * FROM signal_tracks
               WHERE status='tracking'
               ORDER BY registered_at DESC
               LIMIT ?""",
            (limit,),
        )


def mark_track_completed(track_id: int) -> None:
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
    with connect() as conn:
        execute(
            conn,
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
    with connect() as conn:
        execute(
            conn,
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
    with connect() as conn:
        rows = fetchall(
            conn,
            f"""SELECT * FROM signal_tracks
                {where}
                ORDER BY registered_at DESC
                LIMIT ?""",
            params,
        )
    results = []
    for row in rows:
        data = dict(row)
        if data.get("final_is_win") is not None:
            data["final_is_win"] = bool(data["final_is_win"])
        results.append(data)
    return results


def get_track_outcome(track_id: int, horizon_days: int) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        row = fetchone(
            conn,
            """SELECT * FROM signal_track_outcomes
               WHERE track_id=? AND horizon_days=?""",
            (track_id, horizon_days),
        )
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
    with connect() as conn:
        if get_backend() == "postgresql":
            execute(
                conn,
                """INSERT INTO signal_track_outcomes
                   (track_id, horizon_days, horizon_label, eval_date, exit_price,
                    return_pct, max_return_pct, min_return_pct, is_win, evaluated_at, status)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT (track_id, horizon_days) DO UPDATE SET
                     horizon_label=EXCLUDED.horizon_label,
                     eval_date=EXCLUDED.eval_date,
                     exit_price=EXCLUDED.exit_price,
                     return_pct=EXCLUDED.return_pct,
                     max_return_pct=EXCLUDED.max_return_pct,
                     min_return_pct=EXCLUDED.min_return_pct,
                     is_win=EXCLUDED.is_win,
                     evaluated_at=EXCLUDED.evaluated_at,
                     status=EXCLUDED.status""",
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
        else:
            execute(
                conn,
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
    with connect() as conn:
        rows = fetchall(
            conn,
            f"""SELECT o.*, t.ticker, t.name, t.signal_date, t.risk_mode, t.preset_matched
                FROM signal_track_outcomes o
                JOIN signal_tracks t ON t.track_id = o.track_id
                {where}
                ORDER BY t.registered_at DESC, o.horizon_days ASC
                LIMIT ?""",
            params,
        )
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
    with connect() as conn:
        row = fetchone(
            conn,
            f"SELECT COUNT(*) AS cnt FROM signal_tracks t {where}",
            params,
        )
    return int(row["cnt"]) if row else 0
