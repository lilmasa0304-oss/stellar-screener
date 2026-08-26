"""SQLAlchemy ベース DB 接続（PostgreSQL / SQLite / Turso libsql）。"""

from __future__ import annotations

import logging
import os
import re
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional, Tuple
from urllib.parse import parse_qs, quote_plus, unquote_plus, urlencode, urlparse, urlunparse

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Connection, Engine, Result
from sqlalchemy.exc import SQLAlchemyError

from screener.db_path import resolve_db_path

logger = logging.getLogger(__name__)

_engine: Optional[Engine] = None
_backend: Optional[str] = None
_db_init_error: Optional[str] = None

_POSTGRES_PREFIXES = (
    "postgresql+psycopg2://",
    "postgresql://",
    "postgres://",
)


def parse_database_url(url: str) -> Dict[str, Any]:
    """正規化済み URL から接続要素を取り出す（テスト・デバッグ用）。"""
    parsed = urlparse(url)
    username = unquote_plus(parsed.username or "")
    password = unquote_plus(parsed.password or "")
    return {
        "scheme": parsed.scheme,
        "username": username,
        "password": password,
        "host": parsed.hostname,
        "port": parsed.port,
        "database": (parsed.path or "").lstrip("/") or None,
        "query": parsed.query,
    }


def mask_database_url(url: str) -> str:
    """ログ出力用に資格情報をマスクする。"""
    try:
        normalized = normalize_database_url(url)
    except Exception:
        return "database://***:***@***"
    return re.sub(r"://([^:@/]+):([^@/]+)@", r"://\1:***@", normalized)


def _encode_userinfo(username: str, password: str) -> str:
    user = quote_plus(unquote_plus(username), safe="")
    if password:
        pwd = quote_plus(unquote_plus(password), safe="")
        return f"{user}:{pwd}"
    return user


def _fix_postgres_credentials(url: str) -> str:
    """
    パスワード内の @, #, %, / 等を含む PostgreSQL URL を安全にエンコードする。

    urlparse は最初の @ を区切りとみなすため、資格情報部分は右端 @ 基準で分割する。
    """
    prefix = None
    rest = url
    for candidate in _POSTGRES_PREFIXES:
        if url.startswith(candidate):
            prefix = "postgresql+psycopg2://"
            rest = url[len(candidate) :]
            break
    if prefix is None:
        return url

    at_pos = rest.rfind("@")
    if at_pos < 0:
        return prefix + rest

    userinfo = rest[:at_pos]
    location = rest[at_pos + 1 :]
    if ":" in userinfo:
        username, password = userinfo.split(":", 1)
    else:
        username, password = userinfo, ""
    return prefix + _encode_userinfo(username, password) + "@" + location


def normalize_database_url(raw: str) -> str:
    """Supabase / Render 形式の URL を SQLAlchemy 用に正規化する。"""
    url = raw.strip()
    if url.startswith("postgres://"):
        url = "postgresql+psycopg2://" + url[len("postgres://") :]
    elif url.startswith("postgresql://") and "+psycopg2" not in url:
        url = "postgresql+psycopg2://" + url[len("postgresql://") :]

    if url.startswith("libsql://"):
        url = "sqlite+libsql://" + url[len("libsql://") :]

    if "postgresql" in url:
        url = _fix_postgres_credentials(url)
        if "sslmode=" not in url:
            parsed = urlparse(url)
            query = parse_qs(parsed.query)
            query.setdefault("sslmode", ["require"])
            url = urlunparse(
                parsed._replace(query=urlencode({k: v[0] for k, v in query.items()}))
            )
    return url


def get_db_init_error() -> Optional[str]:
    """起動時 DB 初期化エラー（あれば）。"""
    return _db_init_error


def clear_db_init_error() -> None:
    global _db_init_error
    _db_init_error = None


def resolve_database_url() -> Tuple[str, str]:
    """
    (SQLAlchemy URL, backend) を返す。

    backend: postgresql | sqlite | libsql
    """
    explicit = os.getenv("DATABASE_URL", "").strip()
    if explicit:
        try:
            url = normalize_database_url(explicit)
        except Exception as exc:
            raise ValueError(
                f"DATABASE_URL の解析に失敗しました: {exc} "
                f"(masked={mask_database_url(explicit)})"
            ) from exc
        if url.startswith("postgresql"):
            return url, "postgresql"
        if url.startswith("sqlite+libsql"):
            return url, "libsql"
        if url.startswith("sqlite"):
            return url, "sqlite"
        raise ValueError(
            f"未対応の DATABASE_URL 形式です: {explicit.split('://', 1)[0]}"
        )

    db_file = resolve_db_path()
    db_file.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{db_file.as_posix()}", "sqlite"


def get_backend() -> str:
    global _backend
    if _backend is None:
        _, _backend = resolve_database_url()
    return _backend


def reset_engine() -> None:
    """エンジンを破棄（テスト・環境変数切替用）。"""
    global _engine, _backend
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _backend = None


def get_engine() -> Engine:
    global _engine
    if _engine is not None:
        return _engine

    url, backend = resolve_database_url()
    connect_args: Dict[str, Any] = {}
    if backend == "sqlite":
        connect_args["check_same_thread"] = False

    _engine = create_engine(
        url,
        pool_pre_ping=True,
        connect_args=connect_args,
    )

    if backend == "sqlite":

        @event.listens_for(_engine, "connect")
        def _set_sqlite_pragma(dbapi_conn, _connection_record) -> None:
            cursor = dbapi_conn.cursor()
            if os.getenv("SQLITE_TEST_MODE") == "1":
                cursor.execute("PRAGMA journal_mode=DELETE")
            else:
                cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    logger.info("Database engine ready (backend=%s)", backend)
    return _engine


def is_external_database() -> bool:
    return bool(os.getenv("DATABASE_URL", "").strip())


def is_persistent_storage() -> bool:
    """再起動・再デプロイ後もデータが残るか。"""
    if is_external_database():
        return True
    backend = get_backend()
    if backend == "sqlite":
        path = resolve_db_path()
        if os.getenv("RENDER") == "true":
            return False
        if os.getenv("VERCEL") == "1" or str(path).startswith("/tmp"):
            return False
        return True
    return True


def get_storage_info() -> Dict[str, Any]:
    try:
        backend = get_backend()
        url, _ = resolve_database_url()
        safe_url = mask_database_url(url) if is_external_database() else None
        info: Dict[str, Any] = {
            "backend": backend,
            "database_url_set": is_external_database(),
            "db_persistent": is_persistent_storage(),
            "database_url_masked": safe_url,
            "db_init_error": _db_init_error,
            "platform_render": os.getenv("RENDER") == "true",
            "platform_vercel": os.getenv("VERCEL") == "1",
        }
        if backend == "sqlite":
            info["db_path"] = str(resolve_db_path().resolve())
        return info
    except Exception as exc:
        logger.exception("ストレージ情報の取得に失敗: %s", exc)
        return {
            "backend": None,
            "database_url_set": is_external_database(),
            "db_persistent": False,
            "database_url_masked": mask_database_url(os.getenv("DATABASE_URL", ""))
            if is_external_database()
            else None,
            "db_init_error": str(exc),
            "platform_render": os.getenv("RENDER") == "true",
            "platform_vercel": os.getenv("VERCEL") == "1",
        }


def initialize_database_schema() -> bool:
    """
    スキーマ初期化を試行する。失敗しても例外を外に出さない。

    Returns:
        成功時 True
    """
    global _db_init_error
    from screener.db_schema import init_schema

    explicit = os.getenv("DATABASE_URL", "").strip()
    try:
        with connect() as conn:
            init_schema(conn)
        clear_db_init_error()
        info = get_storage_info()
        logger.info(
            "DB initialized (backend=%s, persistent=%s, url=%s)",
            info.get("backend"),
            info.get("db_persistent"),
            info.get("database_url_masked") or info.get("db_path"),
        )
        return True
    except (SQLAlchemyError, ValueError, OSError) as exc:
        _db_init_error = str(exc)
        logger.error(
            "DATABASE_URL 接続・初期化に失敗しました。"
            " アプリは起動を継続しますが DB 機能は利用できません。"
            " url=%s error=%s",
            mask_database_url(explicit) if explicit else "(local sqlite)",
            exc,
        )
        logger.exception("DATABASE 初期化エラー詳細")
        return False


def _bind_sql(sql: str, params: Tuple[Any, ...] | List[Any]) -> Tuple[Any, Dict[str, Any]]:
    if "?" not in sql:
        return text(sql), {}
    parts = sql.split("?")
    bound_sql = parts[0]
    bind: Dict[str, Any] = {}
    for index, part in enumerate(parts[1:]):
        key = f"p{index}"
        bound_sql += f":{key}" + part
        bind[key] = params[index]
    return text(bound_sql), bind


def execute(conn: Connection, sql: str, params: Tuple[Any, ...] | List[Any] = ()) -> Result:
    stmt, bind = _bind_sql(sql, params)
    return conn.execute(stmt, bind)


def fetchone(conn: Connection, sql: str, params: Tuple[Any, ...] | List[Any] = ()) -> Optional[Dict[str, Any]]:
    row = execute(conn, sql, params).mappings().fetchone()
    return dict(row) if row else None


def fetchall(conn: Connection, sql: str, params: Tuple[Any, ...] | List[Any] = ()) -> List[Dict[str, Any]]:
    return [dict(row) for row in execute(conn, sql, params).mappings().fetchall()]


def insert_returning_id(
    conn: Connection,
    sql: str,
    params: Tuple[Any, ...] | List[Any],
    *,
    pk: str = "id",
) -> int:
    if get_backend() == "postgresql":
        row = fetchone(conn, f"{sql} RETURNING {pk}", params)
        if not row:
            raise RuntimeError("INSERT RETURNING did not yield a row")
        return int(row[pk])
    result = execute(conn, sql, params)
    row_id = result.lastrowid
    if row_id is None:
        raise RuntimeError("INSERT did not yield lastrowid")
    return int(row_id)


def insert_ignore(
    conn: Connection,
    table: str,
    columns: List[str],
    values: Tuple[Any, ...] | List[Any],
    conflict_columns: List[str],
) -> None:
    cols = ", ".join(columns)
    placeholders = ", ".join("?" for _ in columns)
    if get_backend() == "postgresql":
        conflict = ", ".join(conflict_columns)
        sql = (
            f"INSERT INTO {table} ({cols}) VALUES ({placeholders}) "
            f"ON CONFLICT ({conflict}) DO NOTHING"
        )
    else:
        sql = f"INSERT OR IGNORE INTO {table} ({cols}) VALUES ({placeholders})"
    execute(conn, sql, values)


@contextmanager
def connect() -> Iterator[Connection]:
    with get_engine().connect() as conn:
        trans = conn.begin()
        try:
            yield conn
            trans.commit()
        except Exception:
            trans.rollback()
            raise
