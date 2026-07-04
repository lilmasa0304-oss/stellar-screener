"""ファンダメンタルズ分析の簡易テスト。"""
from screener.fundamentals import assess_fundamentals, fetch_fundamentals, format_fundamentals_lines

data = fetch_fundamentals("7203.T")
assert data.get("available"), data
assert data.get("trailing_pe") is not None
assert data.get("assessment", {}).get("grade")

lines = format_fundamentals_lines(data)
text = "\n".join(lines)
assert "【ファンダメンタルズ分析】" in text
assert "PER:" in text
print(text)
print("\nOK: fundamentals module")
