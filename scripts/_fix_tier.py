"""Fix TT0/TT1/TT3 bug - remove redundant T prefix from tier display."""
import sys
path = sys.argv[1]
with open(path, "r", encoding="utf-8") as f:
    content = f.read()

# Line 193 fixed already (TT -> T removed)
# Fix line 240: tier: T + tier -> tier (already in db value)
old = "html += '<span class=\"tier\">T' + esc(tier) + '</span>';"
new = "html += '<span class=\"tier\">' + esc(tier) + '</span>';"
if old in content:
    content = content.replace(old, new, 1)
    print("line 240 fixed")
else:
    print("line 240 pattern not found, might already be fixed")

with open(path, "w", encoding="utf-8") as f:
    f.write(content)