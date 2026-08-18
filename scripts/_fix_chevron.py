"""Replace corrupted chevron in index.html."""
import re
import sys

path = sys.argv[1]
with open(path, "r", encoding="utf-8") as f:
    content = f.read()

# ▾ = BLACK DOWN-POINTING SMALL TRIANGLE (U+25BE) — may render as ?
# Replace with simpler "v" (ASCII letter) so all fonts render correctly
# In CSS, when .card-collapsed is added, we rotate it -90deg to point right
# So "v" open + rotated "v" = "<" (visually consistent with collapsed state)
new = content.replace("▾", "v")
count = content.count("▾")
with open(path, "w", encoding="utf-8") as f:
    f.write(new)
print(f"replaced {count} occurrences of corrupted chevron '▾' with 'v'")