"""DATABASE_URL 正規化のユニットテスト。"""

from screener.database import mask_database_url, normalize_database_url, parse_database_url


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


if __name__ == "__main__":
    test_password_with_at_sign()
    test_password_with_hash_and_percent()
    test_already_encoded_password()
    test_postgres_scheme_alias()
    test_mask_database_url()
    print("ok")
