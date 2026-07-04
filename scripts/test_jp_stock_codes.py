"""銘柄コード解析の簡易テスト。"""
from screener.jp_stock_code import (
    extract_jp_stock_code,
    normalize_jp_stock_code,
    normalize_stock_codes_param,
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

print(f"\n{len(CASES) - failed}/{len(CASES)} passed")
raise SystemExit(1 if failed else 0)
