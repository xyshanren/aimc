// === 阶段P0-UI v4 (2026-08-16): 限额预警 紧凑渲染 ===

async function loadQuotaWarning() {
    try {
        const list = document.getElementById("quota-warning-list");
        if (!list) return;
        const resp = await authFetch("/dashboard/api/accounts");
        if (!resp.ok) {
            list.innerHTML = '<div class="empty">⚠️ 后端 API 暂未提供</div>';
            return;
        }
        const data = await resp.json();
        const accounts = data.accounts || [];
        if (accounts.length === 0) {
            list.innerHTML = '<div class="empty">无账号</div>';
            return;
        }
        // 4 个 KPI: 总数 / active / expired / cooldown (从 quota_usage 推断)
        let active = 0, expired = 0, total = accounts.length;
        for (const a of accounts) {
            const s = a.status || 'active';
            if (s === 'active') active++;
            else if (s === 'expired') expired++;
        }
        const inactive = total - active - expired;

        let html = '<div class="kpi-grid" style="grid-template-columns: repeat(2, 1fr); margin-bottom: var(--space-sm);">';
        html += '<div class="kpi"><div class="kpi-value" style="font-size:16px;">' + total + '</div><div class="kpi-label">总账号</div></div>';
        html += '<div class="kpi"><div class="kpi-value kpi-value-success" style="font-size:16px;">' + active + '</div><div class="kpi-label">Active</div></div>';
        html += '<div class="kpi"><div class="kpi-value" style="font-size:16px;color:var(--color-danger);">' + expired + '</div><div class="kpi-label">Expired</div></div>';
        html += '<div class="kpi"><div class="kpi-value" style="font-size:16px;color:var(--text-muted);">' + inactive + '</div><div class="kpi-label">Inactive</div></div>';
        html += '</div>';

        // 列表: 只显示 expired/inactive 的 (active 不需要预警)
        const warned = accounts.filter(a => {
            const s = a.status || 'active';
            return s !== 'active';
        });
        if (warned.length > 0) {
            html += '<div class="text-muted" style="font-size:11px;margin-top:var(--space-sm);">⚠️ ' + warned.length + ' 个账号需要关注:</div>';
            html += '<div class="flex-col gap-sm" style="margin-top:var(--space-sm);">';
            for (const a of warned.slice(0, 5)) {
                const status = a.status || 'active';
                const color = status === 'expired' ? 'var(--color-danger)' : 'var(--text-muted)';
                html += '<div class="flex gap-sm" style="font-size:12px;">';
                html += '<span class="status-dot-small" style="background:' + color + ';"></span>';
                html += '<code class="text-mono" style="font-size:11px;">' + esc(a.account_name || '—') + '</code>';
                html += '<span class="text-muted" style="font-size:11px;">' + esc(status) + '</span>';
                html += '</div>';
            }
            html += '</div>';
        } else {
            html += '<div class="text-success" style="font-size:12px;margin-top:var(--space-sm);">✓ 所有账号 active</div>';
        }
        list.innerHTML = html;
    } catch (e) {
        console.warn("quota warning failed:", e);
        const list = document.getElementById("quota-warning-list");
        if (list) list.innerHTML = '<div class="empty">⚠️ 加载失败</div>';
    }
}

document.addEventListener("DOMContentLoaded", loadQuotaWarning);
setInterval(loadQuotaWarning, 60000);
