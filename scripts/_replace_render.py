"""P0-UI v5: Replace renderRegistryGroups + renderRegistryModels + renderGroupRouting.

Run on VM, replacing existing implementations with improved versions.
"""
import re
import sys

path = sys.argv[1]
with open(path, "r", encoding="utf-8") as f:
    content = f.read()

new_code = open(sys.argv[2]).read()

# Find function boundaries: start at "function renderRegistryGroups"
# end at next "function " declaration or 3+ blank lines
def replace_function(content, func_name, new_def):
    """Replace a top-level function definition."""
    pattern = re.compile(
        rf"^function {func_name}\([^)]*\)\s*\{{",
        re.MULTILINE,
    )
    m = pattern.search(content)
    if not m:
        print(f"  {func_name}: not found")
        return content, False
    start = m.start()
    # Find matching close brace - track brace depth
    body_start = m.end() - 1  # position of {
    depth = 0
    i = body_start
    while i < len(content):
        c = content[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                i += 1  # include closing brace
                break
        i += 1
    # Skip trailing whitespace/newlines (max 2)
    while i < len(content) and content[i] in " \n":
        if content[i:i + 2] == "\n\n":
            i += 1
            break
        i += 1
    new_content = content[:start] + new_def + "\n\n" + content[i:]
    print(f"  {func_name}: replaced ({i - start} chars)")
    return new_content, True


# We need to update renderRegistryGroups and renderRegistryModels
# Keep renderGroupRouting as-is (already good, just missing CSS)
content, ok1 = replace_function(content, "renderRegistryGroups", new_code.split("// 替换 renderRegistryModels")[0])
content, ok2 = replace_function(content, "renderRegistryModels", new_code.split("// 替换 renderRegistryModels")[1])

with open(path, "w", encoding="utf-8") as f:
    f.write(content)
print(f"\nDONE: renderRegistryGroups={ok1}, renderRegistryModels={ok2}")