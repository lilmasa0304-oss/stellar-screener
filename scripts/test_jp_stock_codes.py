"""銘柄コード解析の簡易テスト。"""
from screener.jp_stock_code import (
    extract_jp_stock_code,
    canonicalize_yahoo_ticker,
    normalize_jp_stock_code,
    normalize_stock_codes_param,
    ticker_lookup_variants,
    tracking_ticker_key,
)
from screener.jp_stock_names import resolve_jp_display_name

CASES = [
    ("285A", "285A"),
    ("285a", "285A"),
    ("285A.T", "285A"),
    ("285Ａ", "285A"),
    ("２８５A", "285A"),
    ("285Aを判断して", "285A"),
    ("7203", "7203"),
    ("堅実", None),
]

failed = 0
for raw, expected in CASES:
    got = extract_jp_stock_code(raw)
    ok = got == expected
    failed += not ok
    status = "OK" if ok else "NG"
    print(f"[{status}] {raw!r} -> {got!r} (expected {expected!r})")

param = normalize_stock_codes_param("285A,7203.T")
print(f"normalize_stock_codes_param: {param!r}")
assert param == "285A,7203", param

name_3549 = resolve_jp_display_name("3549.T", "Kusuri No Aoki Holdings Co., Ltd.")
print(f"3549.T ja name: {name_3549!r}")
assert "アオキ" in name_3549 or "クスリ" in name_3549, name_3549

name_7203 = resolve_jp_display_name("7203.T", "Toyota Motor Corporation")
print(f"7203.T ja name: {name_7203!r}")
assert "トヨタ" in name_7203, name_7203

assert canonicalize_yahoo_ticker("3465") == "3465.T"
assert canonicalize_yahoo_ticker("3465.T") == "3465.T"
assert canonicalize_yahoo_ticker("3465.t") == "3465.T"
assert tracking_ticker_key("3465") == tracking_ticker_key("3465.T") == "3465"
assert ticker_lookup_variants("3465") == ["3465.T", "3465"]
assert ticker_lookup_variants("3465.T") == ["3465.T", "3465"]
print("canonicalize/tracking key: OK")

print(f"\n{len(CASES) - failed}/{len(CASES)} passed")
raise SystemExit(1 if failed else 0)
