"""DATABASE_URL 正規化のユニットテスト。"""

from screener.database import (
    _uses_transaction_pooler,
    mask_database_url,
    normalize_database_url,
    parse_database_url,
)


def test_password_with_at_sign():
    raw = "postgresql://postgres:pa@ss@db.example.com:5432/postgres"
    url = normalize_database_url(raw)
    assert "@db.example.com" in url
    assert "pa%40ss" in url or "pa%40ss" in url.lower()


def test_password_with_hash_and_percent():
    raw = "postgresql://postgres:p#ss%word@db.example.com:5432/postgres"
    url = normalize_database_url(raw)
    parsed = parse_database_url(url)
    assert parsed["host"] == "db.example.com"
    assert parsed["password"] == "p#ss%word"


def test_already_encoded_password():
    raw = "postgresql://postgres:pa%40ss%23word@db.example.com:5432/postgres"
    url = normalize_database_url(raw)
    parsed = parse_database_url(url)
    assert parsed["password"] == "pa@ss#word"


def test_postgres_scheme_alias():
    url = normalize_database_url("postgres://user:pass@host/db")
    assert url.startswith("postgresql+psycopg2://")


def test_mask_database_url():
    masked = mask_database_url("postgresql://postgres:secret@host:5432/postgres")
    assert "secret" not in masked
    assert "***" in masked


def test_supabase_transaction_pooler_detection():
    url = normalize_database_url(
        "postgresql://postgres.abc:pass@aws-0-ap-northeast-1.pooler.supabase.com:6543/postgres"
    )
    assert _uses_transaction_pooler(url) is True
    assert "pgbouncer=true" in url


def test_supabase_direct_url_upgraded_for_github_actions():
    import os

    old = os.environ.get("GITHUB_ACTIONS")
    os.environ["GITHUB_ACTIONS"] = "true"
    try:
        url = normalize_database_url(
            "postgresql://postgres:secret@db.myref.supabase.co:5432/postgres"
        )
        assert "pooler.supabase.com" in url
        assert "postgres.myref" in url
        assert ":5432/" in url
    finally:
        if old is None:
            os.environ.pop("GITHUB_ACTIONS", None)
        else:
            os.environ["GITHUB_ACTIONS"] = old


def test_supabase_transaction_pooler_downgraded_in_github_actions():
    import os

    old = os.environ.get("GITHUB_ACTIONS")
    os.environ["GITHUB_ACTIONS"] = "true"
    try:
        url = normalize_database_url(
            "postgresql://postgres.myref:secret@aws-0-ap-northeast-1.pooler.supabase.com:6543/postgres"
        )
        assert ":5432/" in url
        assert "pgbouncer=true" not in url
    finally:
        if old is None:
            os.environ.pop("GITHUB_ACTIONS", None)
        else:
            os.environ["GITHUB_ACTIONS"] = old


if __name__ == "__main__":
    test_password_with_at_sign()
    test_password_with_hash_and_percent()
    test_already_encoded_password()
    test_postgres_scheme_alias()
    test_mask_database_url()
    test_supabase_transaction_pooler_detection()
    test_supabase_direct_url_upgraded_for_github_actions()
    test_supabase_transaction_pooler_downgraded_in_github_actions()
    print("ok")
