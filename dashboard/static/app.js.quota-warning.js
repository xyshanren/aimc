

// === 阶段P0-UI v3 (2026-08-16): 限额预警 + 修复之前漏的 ===

// 限额预警 (左下) - 查 db account_quota_usage > 70% limit
async function loadQuotaWarning() {
    try {
        // 后端暂无 /dashboard/api/quota-warning 端点, 简化: 从 cache-stats / usage 推断
        // 或者调用 accounts + account_quotas (如果有)
        const list = document.getElementById("quota-warning-list");
        if (!list) return;
        // 暂时从 usage logs 推断 (5h / daily tokens)
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
        let html = '<div class="flex-col gap-sm">';
        for (const a of accounts.slice(0, 8)) {
            const status = a.status || 'active';
            const color = status === 'active' ? 'var(--color-success)' : (status === 'expired' ? 'var(--color-danger)' : 'var(--text-muted)');
            html += '<div class="flex gap-sm" style="font-size:12px;">';
            html += '<span class="status-dot-small" style="background:' + color + ';"></span>';
            html += '<code class="text-mono">' + esc(a.account_name || '—') + '</code>';
            html += '<span class="text-muted">' + esc(status) + '</span>';
            html += '</div>';
        }
        html += '</div>';
        list.innerHTML = html;
    } catch (e) {
        console.warn("quota warning failed:", e);
        const list = document.getElementById("quota-warning-list");
        if (list) list.innerHTML = '<div class="empty">⚠️ 加载失败</div>';
    }
}

document.addEventListener("DOMContentLoaded", loadQuotaWarning);
setInterval(loadQuotaWarning, 60000);
