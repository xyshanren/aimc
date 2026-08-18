
// === 阶段P0-UI v2 (2026-08-16): 修复旧函数死引用 + 接管 htmx 自动渲染 ===

// (a) 删除旧的 DOMContentLoaded hooks - 它们指向不存在 IDs 的元素
//   - loadDataFreshness (line 507) -> df-total/df-healthy 不存在
//   - loadSuggestions (line 636) -> sg-pending/sg-approved/sg-applied 不存在
//   - loadCacheStats (line 542) -> cache-saved/cache-tokens/cache-hit-pct 不存在; cache-by-channel / cache-by-model 也不存在
//   - loadUsageChart (line 587) -> usage-chart canvas 不存在
// 替代: 我们直接 fetch + 渲染到新 IDs (kpi-* / cache-by-channel / usage-list / suggestions-list / group-routing-list / channels-list)

// (b) 新 loadUsageChart 替代 (渲染 usage-list 表格)
async function loadUsageChart() {
    try {
        const resp = await authFetch("/dashboard/api/usage/summary?days=7");
        if (!resp.ok) return;
        const data = await resp.json();
        const list = document.getElementById("usage-list");
        if (!list) return;
        if (!data || data.length === 0) {
            list.innerHTML = '<div class="empty">暂无用量数据</div>';
            return;
        }
        let html = '<table><thead><tr><th>日期</th><th>请求数</th><th class="mono">Input</th><th class="mono">Output</th><th class="mono">平均延迟</th></tr></thead><tbody>';
        for (const r of data) {
            html += `<tr><td>${esc(r.date || '—')}</td><td>${r.request_count || 0}</td><td class="mono">${(r.total_input_tokens || 0).toLocaleString()}</td><td class="mono">${(r.total_output_tokens || 0).toLocaleString()}</td><td class="mono">${r.avg_latency_ms ? r.avg_latency_ms.toFixed(0) + ' ms' : '—'}</td></tr>`;
        }
        html += '</tbody></table>';
        list.innerHTML = html;
    } catch (e) { console.warn("usage chart failed:", e); }
}

// (c) 新 loadCacheStatsList 渲染 cache-by-channel 表格 (替代 htmx 自动渲染)
async function loadCacheStatsList() {
    try {
        const resp = await authFetch("/dashboard/api/cache/stats?days=7");
        if (!resp.ok) return;
        const data = await resp.json();
        const container = document.getElementById("cache-by-channel");
        if (!container) return;
        let html = '';
        // 按渠道
        if (data.by_channel && data.by_channel.length > 0) {
            html += '<h3 style="font-size:11px;color:var(--text-muted);text-transform:uppercase;margin-bottom:8px;">按渠道</h3>';
            html += '<table><thead><tr><th>渠道</th><th class="mono">Input</th><th class="mono">Cached</th><th class="mono">命中率</th><th class="mono">节省</th></tr></thead><tbody>';
            for (const c of data.by_channel) {
                html += '<tr>';
                html += `<td>${esc(c.channel_name || '—')}</td>`;
                html += `<td class="mono">${(c.input_tokens || 0).toLocaleString()}</td>`;
                html += `<td class="mono">${(c.cached_tokens || 0).toLocaleString()}</td>`;
                html += `<td class="mono">${(c.hit_rate_pct || 0).toFixed(1)}%</td>`;
                html += `<td class="mono">¥${(c.saved_cost_cny || 0).toFixed(4)}</td>`;
                html += '</tr>';
            }
            html += '</tbody></table>';
        }
        // TOP 10 model
        if (data.by_model && data.by_model.length > 0) {
            html += '<h3 style="font-size:11px;color:var(--text-muted);text-transform:uppercase;margin:16px 0 8px;">TOP 10 Model</h3>';
            html += '<table><thead><tr><th>Model</th><th class="mono">Input</th><th class="mono">Cached</th><th class="mono">命中率</th><th class="mono">节省</th></tr></thead><tbody>';
            for (const m of data.by_model.slice(0, 10)) {
                html += '<tr>';
                html += `<td><code class="text-mono">${esc(m.model || '—')}</code></td>`;
                html += `<td class="mono">${(m.input_tokens || 0).toLocaleString()}</td>`;
                html += `<td class="mono">${(m.cached_tokens || 0).toLocaleString()}</td>`;
                html += `<td class="mono">${(m.hit_rate_pct || 0).toFixed(1)}%</td>`;
                html += `<td class="mono">¥${(m.saved_cost_cny || 0).toFixed(4)}</td>`;
                html += '</tr>';
            }
            html += '</tbody></table>';
        }
        if (!html) html = '<div class="empty">暂无 Cache 数据</div>';
        container.innerHTML = html;
    } catch (e) { console.warn("cache stats list failed:", e); }
}

// (d) 新 loadSuggestionsList 渲染 suggestions-list (替代 htmx + 死 ID)
async function loadSuggestionsList() {
    try {
        const resp = await authFetch("/dashboard/api/suggestions?status=pending");
        if (!resp.ok) return;
        const data = await resp.json();
        const list = document.getElementById("suggestions-list");
        const countEl = document.getElementById("sg-count");
        const model = data.model || [];
        const group = data.group || [];
        const total = model.length + group.length;
        if (countEl) countEl.textContent = total;
        if (!list) return;
        if (total === 0) {
            list.innerHTML = '<div class="text-success" style="padding:8px 0;font-size:12px;">✓ 没有待审建议</div>';
            return;
        }
        let html = '';
        for (const s of model) {
            html += '<div style="padding:8px 0;border-bottom:1px solid var(--border-muted);">';
            html += '<div class="flex gap-sm"><span class="text-mono" style="font-size:12px;">' + esc(s.model_id || '—') + '</span>';
            html += '<span class="text-muted" style="font-size:11px;">' + esc(s.change_type || '—') + '</span></div>';
            if (s.suggested_value) html += '<div class="text-mono" style="font-size:11px;color:var(--color-warning);">' + esc(s.suggested_value) + '</div>';
            html += '<div class="confidence-bar mt-sm"><div class="confidence-fill ' + (s.confidence >= 0.7 ? 'confidence-fill-high' : (s.confidence >= 0.4 ? 'confidence-fill-mid' : 'confidence-fill-low')) + '" style="width:' + (s.confidence * 100) + '%;"></div></div>';
            html += '<div class="flex gap-sm mt-sm">';
            html += '<button class="btn btn-success btn-sm" onclick="approveSuggestion(\'model\', ' + s.id + ')">批准</button>';
            html += '<button class="btn btn-danger btn-sm" onclick="rejectSuggestion(\'model\', ' + s.id + ')">拒绝</button>';
            html += '</div>';
            html += '</div>';
        }
        for (const s of group) {
            html += '<div style="padding:8px 0;border-bottom:1px solid var(--border-muted);">';
            html += '<div class="flex gap-sm"><span class="text-mono" style="font-size:12px;">' + esc(s.group_id || '—') + '</span>';
            html += '<span class="text-muted" style="font-size:11px;">' + esc(s.change_type || '—') + '</span></div>';
            html += '<div class="confidence-bar mt-sm"><div class="confidence-fill ' + (s.confidence >= 0.7 ? 'confidence-fill-high' : (s.confidence >= 0.4 ? 'confidence-fill-mid' : 'confidence-fill-low')) + '" style="width:' + (s.confidence * 100) + '%;"></div></div>';
            html += '<div class="flex gap-sm mt-sm">';
            html += '<button class="btn btn-success btn-sm" onclick="approveSuggestion(\'group\', ' + s.id + ')">批准</button>';
            html += '<button class="btn btn-danger btn-sm" onclick="rejectSuggestion(\'group\', ' + s.id + ')">拒绝</button>';
            html += '</div>';
            html += '</div>';
        }
        list.innerHTML = html;
    } catch (e) { console.warn("suggestions list failed:", e); }
}

// (e) htmx 接管: 当 htmx 拿回 JSON 时, 让它直接渲染 (避免我们重复实现 renderer)
// 简单方案: 给 htmx 默认 resp.target 注入 JSON→HTML 转换
// 由于 htmx 不会自动转 JSON, 我们的 loadCacheStatsList / loadUsageChart 主动接管这些 IDs

// 启动钩子
document.addEventListener("DOMContentLoaded", loadUsageChart);
document.addEventListener("DOMContentLoaded", loadCacheStatsList);
document.addEventListener("DOMContentLoaded", loadSuggestionsList);

// 60s 定时刷新 + 接管 htmx
setInterval(() => {
    loadUsageChart();
    loadCacheStatsList();
    loadSuggestionsList();
}, 60000);
