

// 阶段P0-UI (2026-08-16): dashboard redesign JS
// - loadOverviewKPIs: 顶部 4 KPI 卡片 + status bar
// - toggleCard: 折叠卡片
// - filterChannels/filterRegistry: 搜索过滤
// - loadDataFreshnessDonut: 替换旧表格为 donut + 失败列表
// - keydown listener: r / a / / / ? 快捷键

async function loadOverviewKPIs() {
    try {
        const [cacheResp, usageResp] = await Promise.all([
            authFetch("/dashboard/api/cache/stats?days=7"),
            authFetch("/dashboard/api/usage/summary?days=7"),
        ]);
        const cache = cacheResp.ok ? await cacheResp.json() : {by_channel: [], hit_rate_pct: 0, saved_cost_cny: 0};
        const usage = usageResp.ok ? await usageResp.json() : [];
        const totalInput = (usage || []).reduce((s, r) => s + (r.total_input_tokens || 0), 0);
        const totalReq = (usage || []).reduce((s, r) => s + (r.request_count || 0), 0);
        const set = (id, v) => { const e = document.getElementById(id); if (e) e.textContent = v; };
        set("kpi-tokens", (totalInput || 0).toLocaleString());
        set("kpi-requests", (totalReq || 0).toLocaleString());
        set("kpi-cache-rate", (cache.hit_rate_pct || 0).toFixed(1));
        set("kpi-saved", "¥" + (cache.saved_cost_cny || 0).toFixed(4));
        const sr = document.getElementById("status-route");
        if (sr) {
            const ch = (cache.by_channel && cache.by_channel[0]) ? cache.by_channel[0].channel_name : "—";
            sr.textContent = "cache: " + ch + " · ¥" + (cache.saved_cost_cny || 0).toFixed(4) + " saved · " + (cache.hit_rate_pct || 0) + "% hit";
        }
    } catch (e) { console.warn("Overview KPIs failed:", e); }
}

function toggleCard(t) {
    const c = t.closest(".card");
    if (c) c.classList.toggle("card-collapsed");
}

function filterChannels() {
    const input = document.getElementById("search-channels");
    const q = input ? input.value : "";
    document.querySelectorAll("#channels-list tr").forEach(tr => {
        tr.style.display = (!q || tr.textContent.toLowerCase().includes(q.toLowerCase())) ? "" : "none";
    });
}

function filterRegistry() {
    const input = document.getElementById("search-registry");
    const q = input ? input.value : "";
    document.querySelectorAll("#registry-models-list tr").forEach(tr => {
        tr.style.display = (!q || tr.textContent.toLowerCase().includes(q.toLowerCase())) ? "" : "none";
    });
}

async function loadDataFreshnessDonut() {
    try {
        const resp = await authFetch("/dashboard/api/data-freshness");
        if (!resp.ok) return;
        const data = await resp.json();
        const jobs = data.jobs || [];
        const total = jobs.length;
        const healthy = jobs.filter(j => j.health === "healthy").length;
        const warning = jobs.filter(j => j.health === "warning").length;
        const stale = jobs.filter(j => j.health === "stale").length;

        const donut = document.getElementById("freshness-donut");
        if (donut && total > 0) {
            const hDeg = (healthy / total) * 360;
            const wDeg = (warning / total) * 360;
            donut.style.setProperty("--healthy-deg", hDeg + "deg");
            donut.style.setProperty("--warning-deg", wDeg + "deg");
        }
        const set = (id, v) => { const e = document.getElementById(id); if (e) e.textContent = v; };
        set("freshness-pct", total ? Math.round(healthy / total * 100) + "%" : "--%");
        set("freshness-total", total ? (healthy + " / " + total + " 健康") : "0 / 0 健康");

        const failedList = document.getElementById("freshness-failed");
        if (failedList) {
            const failed = jobs.filter(j => j.health !== "healthy");
            if (failed.length === 0) {
                failedList.innerHTML = '<div class="text-success" style="padding:8px 0;font-size:12px;">✓ 所有 job 健康</div>';
            } else {
                let html = '<div class="flex-col gap-sm">';
                for (let i = 0; i < Math.min(failed.length, 5); i++) {
                    const j = failed[i];
                    const color = {warning: "var(--color-warning)", stale: "var(--color-danger)", unknown: "var(--text-muted)"}[j.health];
                    html += '<div class="flex gap-sm" style="font-size:12px;">';
                    html += '<span class="status-dot-small" style="background:' + color + ';"></span>';
                    html += '<code class="text-mono">' + esc(j.job_id.slice(0, 40)) + '</code>';
                    html += '<span class="text-muted">' + esc(j.last_success_ago || "从未") + '</span>';
                    if (j.last_error_msg) {
                        html += '<span class="text-danger" title="' + esc(j.last_error_msg) + '">!</span>';
                    }
                    html += '</div>';
                }
                html += '</div>';
                failedList.innerHTML = html;
            }
        }

        // 向后兼容 (旧的 df-* ID 也保持填充)
        set("df-total", total);
        set("df-healthy", healthy);
        set("df-warning", warning);
        set("df-stale", stale);
    } catch (e) { console.warn("freshness donut failed:", e); }
}

document.addEventListener("keydown", (e) => {
    const tag = e.target.tagName;
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
    if (e.ctrlKey || e.metaKey) return;
    if (e.key === "r") { e.preventDefault(); location.reload(); }
    else if (e.key === "a") { e.preventDefault(); if (typeof applyAllSuggestions === "function") applyAllSuggestions(); }
    else if (e.key === "/") { e.preventDefault(); const s = document.getElementById("search-channels"); if (s) s.focus(); }
    else if (e.key === "?") { e.preventDefault(); const el = document.getElementById("shortcut-hint"); if (el) el.style.opacity = (el.style.opacity === "1" ? "0.3" : "1"); }
});

document.addEventListener("DOMContentLoaded", loadOverviewKPIs);
document.addEventListener("DOMContentLoaded", loadDataFreshnessDonut);
