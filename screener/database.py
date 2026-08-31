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
from sqlalchemy.pool import NullPool

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


def _extract_supabase_project_ref(url: str) -> Optional[str]:
    """Supabase project ref を URL または環境変数から取得する。"""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host.startswith("db.") and host.endswith(".supabase.co"):
        parts = host.split(".")
        if len(parts) >= 2 and parts[1]:
            return parts[1]

    username = unquote_plus(parsed.username or "")
    if username.startswith("postgres.") and len(username) > len("postgres."):
        return username.split(".", 1)[1]

    env_ref = os.getenv("SUPABASE_PROJECT_REF", "").strip()
    return env_ref or None


def is_supabase_url_rewrite_enabled() -> bool:
    """
    Supabase URL 自動変換を有効にするか。

    DATABASE_URL_RAW=1 または SUPABASE_SKIP_URL_REWRITE=1 で無効化（URL をそのまま使用）。
    """
    for key in ("DATABASE_URL_RAW", "SUPABASE_SKIP_URL_REWRITE"):
        if os.getenv(key, "").strip().lower() in ("1", "true", "yes"):
            return False
    return True


def get_database_url_mode() -> str:
    """接続 URL モード（ログ用）: raw | rewrite"""
    return "raw" if not is_supabase_url_rewrite_enabled() else "rewrite"


def _should_use_supabase_pooler() -> bool:
    if not is_supabase_url_rewrite_enabled():
        return False
    if os.getenv("SUPABASE_USE_POOLER", "").lower() in ("1", "true", "yes"):
        return True
    return os.getenv("GITHUB_ACTIONS") == "true"


def _normalize_supabase_url(url: str) -> str:
    """
    Supabase 接続 URL を用途に合わせて補正する。

    - GitHub Actions 等 IPv4 環境: db.*.supabase.co 直接接続 → session pooler (5432)
    - Supavisor: username `postgres` → `postgres.<project-ref>`
    - Transaction pooler (6543): pgbouncer=true を付与
    """
    if not is_supabase_url_rewrite_enabled():
        return url

    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if not host or "supabase.co" not in host:
        return url

    ref = _extract_supabase_project_ref(url)
    region = os.getenv("SUPABASE_POOLER_REGION", "ap-northeast-1").strip()
    username = unquote_plus(parsed.username or "")
    password = parsed.password or ""
    port = parsed.port or 5432
    database = (parsed.path or "/postgres").lstrip("/") or "postgres"
    query = parse_qs(parsed.query)
    changed = False

    pooler_host = f"aws-0-{region}.pooler.supabase.com"

    if (
        host.startswith("db.")
        and host.endswith(".supabase.co")
        and _should_use_supabase_pooler()
        and ref
    ):
        pool_user = username if username.startswith("postgres.") else f"postgres.{ref}"
        userinfo = _encode_userinfo(pool_user, password)
        netloc = f"{userinfo}@{pooler_host}:5432"
        logger.info(
            "Supabase direct URL を session pooler に変換 (%s -> %s:5432)",
            host,
            pooler_host,
        )
        parsed = parsed._replace(netloc=netloc, path=f"/{database}")
        query.pop("pgbouncer", None)
        changed = True
    elif (
        "pooler.supabase.com" in host
        and port == 6543
        and _should_use_supabase_pooler()
        and ref
    ):
        pool_user = username if username.startswith("postgres.") else f"postgres.{ref}"
        userinfo = _encode_userinfo(pool_user, password)
        netloc = f"{userinfo}@{host}:5432"
        logger.info(
            "Supabase transaction pooler (6543) を session pooler (5432) に変換"
        )
        parsed = parsed._replace(netloc=netloc)
        query.pop("pgbouncer", None)
        changed = True
    elif "pooler.supabase.com" in host and username == "postgres" and ref:
        userinfo = _encode_userinfo(f"postgres.{ref}", password)
        netloc = f"{userinfo}@{host}:{port}"
        logger.info("Supabase pooler username を postgres.%s に修正", ref)
        parsed = parsed._replace(netloc=netloc)
        changed = True

    if not changed:
        return url

    query_str = urlencode({k: v[0] for k, v in query.items()}) if query else ""
    return urlunparse(parsed._replace(query=query_str))


def normalize_database_url(raw: str) -> str:
    """Supabase / Render 形式の URL を SQLAlchemy 用に正規化する。"""
    url = raw.strip().strip('"').strip("'")
    if url.startswith("postgres://"):
        url = "postgresql+psycopg2://" + url[len("postgres://") :]
    elif url.startswith("postgresql://") and "+psycopg2" not in url:
        url = "postgresql+psycopg2://" + url[len("postgresql://") :]

    if url.startswith("libsql://"):
        url = "sqlite+libsql://" + url[len("libsql://") :]

    if "postgresql" in url:
        url = _fix_postgres_credentials(url)
        if is_supabase_url_rewrite_enabled():
            url = _normalize_supabase_url(url)
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        query.setdefault("sslmode", ["require"])
        if _uses_transaction_pooler(url):
            query.setdefault("pgbouncer", ["true"])
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
    explicit = os.getenv("DATABASE_URL", "").strip().strip('"').strip("'")
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


def _uses_transaction_pooler(url: str) -> bool:
    """Supabase PgBouncer transaction mode (port 6543) 等を検出する。"""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    port = parsed.port
    if port == 6543:
        return True
    if "pooler.supabase.com" in host and port in (None, 6543):
        return True
    query = parse_qs(parsed.query)
    pgbouncer = (query.get("pgbouncer") or query.get("pool_mode") or [""])[0].lower()
    return pgbouncer in ("true", "transaction")


def get_engine() -> Engine:
    global _engine
    if _engine is not None:
        return _engine

    url, backend = resolve_database_url()
    connect_args: Dict[str, Any] = {}
    if backend == "sqlite":
        connect_args["check_same_thread"] = False
    elif backend == "postgresql" and _uses_transaction_pooler(url):
        # Transaction pooler は prepared statements 非対応
        connect_args["prepare_threshold"] = None

    engine_kwargs: Dict[str, Any] = {
        "pool_pre_ping": True,
        "connect_args": connect_args,
    }
    if backend == "postgresql" and _uses_transaction_pooler(url):
        engine_kwargs["poolclass"] = NullPool

    _engine = create_engine(
        url,
        **engine_kwargs,
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
    return bool(os.getenv("DATABASE_URL", "").strip().strip('"').strip("'"))


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

    explicit = os.getenv("DATABASE_URL", "").strip().strip('"').strip("'")
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
