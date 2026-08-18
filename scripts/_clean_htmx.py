import re
path = "/root/dsh-workspace/aimc-work/aimc-wt/dashboard/static/index.html"
content = open(path).read()
for id_to_clean in ["usage-list", "cache-by-channel", "suggestions-list"]:
    pattern = r'(<div id="' + id_to_clean + r'"[^>]*?) hx-get="[^"]+" hx-trigger="[^"]+" hx-swap="[^"]+"'
    new = re.sub(pattern, lambda m: m.group(1), content)
    if new != content:
        content = new
        print("cleaned", id_to_clean)
open(path, "w").write(content)
print("done")
