"""Replace fetch() with authFetch() in dashboard/app.js (P1-7)."""
import re
import sys

path = sys.argv[1]
content = open(path).read()

# 替换所有 dashboard API fetch 调用为 authFetch
# 注意保留 line 20 (authFetch 内部) 和 line 32 (auth status 自检)
# 策略: 匹配 `fetch('/dashboard/api/...` 或 `fetch(`/dashboard/api/...`, opts)`
# 但不匹配 `fetch(url, opts)` (authFetch 内部) 或 `fetch('/dashboard/api/auth/status')` (自检)

# 简单的替换: 把 `/dashboard/api/` 开头的 fetch 改成 authFetch
pattern = re.compile(r"await fetch\((['\"`]/dashboard/api/)")
new_content = pattern.sub(r"await authFetch(\1", content)

# 统计替换
n_diff = content.count("await fetch(") - new_content.count("await fetch(")
print(f"替换前 fetch() 调用数: {content.count('await fetch(')}")
print(f"替换后 fetch() 调用数: {new_content.count('await fetch(')}")
print(f"authFetch() 调用数: {new_content.count('await authFetch(')}")

open(path, "w").write(new_content)
print("OK")
