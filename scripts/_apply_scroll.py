"""P0-UI v7 D1: 表格限高滚动 - 在 TOP 10 / Model Registry / Accounts 应用 scroll-y 类。"""
import sys

path = sys.argv[1]
with open(path, "r", encoding="utf-8") as f:
    content = f.read()

# 1. renderCacheStatsList: TOP 10 model 表加 scroll-y-260
old1 = """        // TOP 10 model
        if (data.by_model && data.by_model.length > 0) {
            html += '<h3 style="font-size:11px;color:var(--text-muted);text-transform:uppercase;margin:16px 0 8px;">TOP 10 Model</h3>';
            html += '<table><thead><tr><th>Model</th><th class="mono">Input</th><th class="mono">Cached</th><th class="mono">命中率</th><th class="mono">节省</th></tr></thead><tbody>';"""
new1 = """        // TOP 10 model
        if (data.by_model && data.by_model.length > 0) {
            html += '<h3 style="font-size:11px;color:var(--text-muted);text-transform:uppercase;margin:16px 0 8px;">TOP 10 Model</h3>';
            html += '<div class="scroll-y-260"><table><thead><tr><th>Model</th><th class="mono">Input</th><th class="mono">Cached</th><th class="mono">命中率</th><th class="mono">节省</th></tr></thead><tbody>';"""
content = content.replace(old1, new1, 1)

# 关闭 + tbody 后面要 div 包裹
old2 = """                html += '</tbody></table>';
        }
        if (!html) html = '<div class="empty">暂无 Cache 数据</div>';"""
new2 = """                html += '</tbody></table></div>';
        }
        if (!html) html = '<div class="empty">暂无 Cache 数据</div>';"""
content = content.replace(old2, new2, 1)

# 2. renderRegistryModels: 已 max-height: 480px, 改为 scroll-y-320
old3 = """        html += '<div style="max-height:480px;overflow-y:auto;border:1px solid var(--border-default);border-radius:var(--radius-md);">';"""
new3 = """        html += '<div class="scroll-y-320">';"""
content = content.replace(old3, new3, 1)

# 3. renderAccounts: 表格加 scroll-y-260
old4 = """    let html = explanation + '<table><thead><tr><th>ID</th><th>账号名</th><th>状态</th><th>已用/总额</th><th>过期</th><th>Key</th><th>备注</th><th>操作</th></tr></thead><tbody>';"""
new4 = """    let html = explanation + '<div class="scroll-y-300"><table><thead><tr><th>ID</th><th>账号名</th><th>状态</th><th>已用/总额</th><th>过期</th><th>Key</th><th>备注</th><th>操作</th></tr></thead><tbody>';"""
content = content.replace(old4, new4, 1)

# 关闭
old5 = """        html += '</tbody></table>';
    container.innerHTML = html;"""
new5 = """        html += '</tbody></table></div>';
    container.innerHTML = html;"""
content = content.replace(old5, new5, 1)

# 4. Group routing: 长 group 加 scroll-y-260
old6 = """            html += '<table><thead><tr><th>实际 model</th><th>次数</th><th>占比</th><th>分布</th></tr></thead><tbody>';
                for (const m of dist) {
                    const modelName = m.model || "(未知)";
                    html += `<tr>
                        <td><code class="${m.model ? 'text-mono' : 'text-muted'}" style="font-size:11px;">${esc(modelName)}</code></td>
                        <td>${esc(m.count)}</td>
                        <td>${esc(pct.toFixed(1))}%</td>
                        <td><div class="bar-track"><div class="bar-fill" style="width:${esc(pct)}%"></div></div></td>
                    </tr>`;
                }
                html += '</tbody></table>';"""
new6 = """                html += '<div class="scroll-y-260"><table><thead><tr><th>实际 model</th><th>次数</th><th>占比</th><th>分布</th></tr></thead><tbody>';
                for (const m of dist) {
                    const modelName = m.model || "(未知)";
                    html += `<tr>
                        <td><code class="${m.model ? 'text-mono' : 'text-muted'}" style="font-size:11px;">${esc(modelName)}</code></td>
                        <td>${esc(m.count)}</td>
                        <td>${esc(pct.toFixed(1))}%</td>
                        <td><div class="bar-track"><div class="bar-fill" style="width:${esc(pct)}%"></div></div></td>
                    </tr>`;
                }
                html += '</tbody></table></div>';"""
content = content.replace(old6, new6, 1)

with open(path, "w", encoding="utf-8") as f:
    f.write(content)
print("OK: applied scroll-y-260/300/320 to 4 tables")