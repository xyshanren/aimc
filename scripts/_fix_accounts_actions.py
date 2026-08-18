"""Fix the broken toggle/rotate buttons in renderAccounts."""
import sys

path = sys.argv[1]
with open(path, "r", encoding="utf-8") as f:
    content = f.read()

# The broken lines use HTML onclick with mixed quote nesting.
# Replace with data-id attribute + delegated event handler.
old = """        if (status !== 'active') {
            actions.push('<button class="btn-sm btn-enable" onclick="toggleAccount(' + a.id + \\", 'active', this)">启用</button>');
        } else {
            actions.push('<button class="btn-sm btn-disable" onclick="toggleAccount(' + a.id + \\", 'disabled', this)">禁用</button>');
        }
        actions.push('<button class="btn-sm btn-rotate" onclick="rotateAccountKey(' + a.id + ', this)">轮换</button>');"""

# Use data-id and dispatch via custom function names stored in data-action
new = """        if (status !== "active") {
            actions.push('<button class="btn-sm btn-enable" data-action="enable" data-id="' + a.id + '">启用</button>');
        } else {
            actions.push('<button class="btn-sm btn-disable" data-action="disable" data-id="' + a.id + '">禁用</button>');
        }
        actions.push('<button class="btn-sm btn-rotate" data-action="rotate" data-id="' + a.id + '">轮换</button>');"""

if old in content:
    content = content.replace(old, new, 1)
    print("replaced broken button lines")
else:
    print("PATTERN NOT FOUND")
    sys.exit(1)

with open(path, "w", encoding="utf-8") as f:
    f.write(content)