"""P0-UI v6: Fix empty model + week report styling."""
import re
import sys

path = sys.argv[1]
with open(path, "r", encoding="utf-8") as f:
    content = f.read()

# Fix 1: renderGroupRouting - empty model fallback
old1 = """                html += `<tr>
                        <td><code>${esc(m.model)}</code></td>
                        <td>${esc(m.count)}</td>
                        <td>${esc(pct.toFixed(1))}%</td>
                        <td><div class="bar-track"><div class="bar-fill" style="width:${esc(pct)}%"></div></div></td>
                    </tr>`;"""
new1 = """                const modelName = m.model || "(未知)";
                html += `<tr>
                        <td><code class="${m.model ? 'text-mono' : 'text-muted'}" style="font-size:11px;">${esc(modelName)}</code></td>
                        <td>${esc(m.count)}</td>
                        <td>${esc(pct.toFixed(1))}%</td>
                        <td><div class="bar-track"><div class="bar-fill" style="width:${esc(pct)}%"></div></div></td>
                    </tr>`;"""
content = content.replace(old1, new1, 1)

# Fix 2: empty model in unmatched
old2 = """                html += '<table><thead><tr><th>model</th><th>次数</th></tr></thead><tbody>';
                for (const m of top) {
                    html += `<tr><td><code>${esc(m.model)}</code></td><td>${esc(m.count)}</td></tr>`;"""
new2 = """                html += '<table><thead><tr><th>model</th><th>次数</th></tr></thead><tbody>';
                for (const m of top) {
                    const mName = m.model || "(未知)";
                    html += `<tr><td><code class="${m.model ? 'text-mono' : 'text-muted'}" style="font-size:11px;">${esc(mName)}</code></td><td>${esc(m.count)}</td></tr>`;"""
content = content.replace(old2, new2, 1)

with open(path, "w", encoding="utf-8") as f:
    f.write(content)
print("OK")