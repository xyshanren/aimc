"""Remove htmx auto-fetch from IDs we now render in JS."""
import re
import sys

path = sys.argv[1]
content = open(path).read()

# Remove htmx attrs from specific IDs
for id_to_clean in ["usage-list", "cache-by-channel"]:
    # Match the div opening, then the htmx attrs we want to strip
    pattern = (
        r'(<div id="' + id_to_clean + r'"[^>]*?)'
        r' hx-get="[^"]+"'
        r' hx-trigger="[^"]+"'
        r' hx-swap="[^"]+"'
    )
    new_content = re.sub(pattern, lambda m: m.group(1), content)
    if new_content != content:
        content = new_content
        print(f"cleaned htmx from #{id_to_clean}")

open(path, "w").write(content)
print("done")
