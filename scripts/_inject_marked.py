"""Inject marked.min.js script tag before app.js in index.html."""
import re
import sys

path = sys.argv[1]
with open(path, "r", encoding="utf-8") as f:
    content = f.read()

# Insert before "src=\"/static/app.js\""
new_content = content.replace(
    '<script src="/static/app.js"></script>',
    '<script src="/static/vendor/marked.min.js"></script>\n<script src="/static/app.js"></script>',
)
if new_content != content:
    with open(path, "w", encoding="utf-8") as f:
        f.write(new_content)
    print("injected marked.min.js before app.js")
else:
    print("marked.min.js already present or pattern not found")