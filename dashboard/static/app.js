/**
 * AIMC Dashboard 前端逻辑
 * 使用 htmx 进行局部刷新，Chart.js 绘制用量图表
 * P1-7 (2026-08-14): 加 dashboard auth (跟 api_key 分开)
 */

// ============================================================
// P1-7: dashboard 鉴权 (跟 api_key 分开)
// ============================================================

const DASHBOARD_TOKEN_KEY = 'aimc_dashboard_token';

// fetch wrapper: 自动加 Authorization header (如果 localStorage 里有 token)
async function authFetch(url, opts = {}) {
    const token = localStorage.getItem(DASHBOARD_TOKEN_KEY);
    if (token) {
        opts.headers = opts.headers || {};
        opts.headers['Authorization'] = 'Bearer ' + token;
    }
    const resp = await fetch(url, opts);
    // 401 = token 失效/缺失, 跳 login
    if (resp.status === 401 && !url.includes('/dashboard/login') && !url.includes('/dashboard/api/auth/')) {
        localStorage.removeItem(DASHBOARD_TOKEN_KEY);
        location.href = '/dashboard/login?next=' + encodeURIComponent(location.pathname);
    }
    return resp;
}

// 启动时检查鉴权状态 (auth_enabled 时缺 token 跳 login)
(async function checkAuthOnLoad() {
    try {
        const resp = await fetch('/dashboard/api/auth/status');
        const data = await resp.json();
        if (data.auth_enabled) {
            // 鉴权启用: 检查 localStorage 是否有 token
            const token = localStorage.getItem(DASHBOARD_TOKEN_KEY);
            if (!token) {
                location.href = '/dashboard/login?next=' + encodeURIComponent(location.pathname);
                return;
            }
            // 验证 token 有效性
            const verifyResp = await authFetch('/dashboard/api/auth/status');
            if (!verifyResp.ok) {
                localStorage.removeItem(DASHBOARD_TOKEN_KEY);
                location.href = '/dashboard/login?next=' + encodeURIComponent(location.pathname);
            }
        }
        // auth_enabled=false: legacy, 啥也不做
    } catch (e) {
        console.warn('Dashboard auth check failed (non-fatal):', e);
    }
})();

// HTML 转义，防止 XSS
function esc(str) {
    if (!str) return '';
    const div = document.createElement('div');
    div.textContent = String(str);
    return div.innerHTML;
}

// P1-7: 给 htmx 全局加 Authorization header (dashboard token)
document.body.addEventListener('htmx:configRequest', function(event) {
    const token = localStorage.getItem(DASHBOARD_TOKEN_KEY);
    if (token) {
        event.detail.headers['Authorization'] = 'Bearer ' + token;
    }
});

// P1-7: htmx 收到 401 时跳 login
document.body.addEventListener('htmx:responseError', function(event) {
    if (event.detail.xhr.status === 401) {
        const path = event.detail.pathInfo?.requestPath || event.detail.path || '';
        if (!path.includes('/dashboard/login') && !path.includes('/dashboard/api/auth/')) {
            localStorage.removeItem(DASHBOARD_TOKEN_KEY);
            location.href = '/dashboard/login?next=' + encodeURIComponent(location.pathname);
        }
    }
});

// htmx 响应渲染：渠道列表
document.body.addEventListener('htmx:afterSwap', function(event) {
    const target = event.detail.target;

    if (target.id === 'channels-list') {
        renderChannels(target);
    } else if (target.id === 'promotions-list') {
        renderPromotions(target);
    } else if (target.id === 'prices-list') {
        renderPrices(target);
    } else if (target.id === 'registry-models-list') {
        renderRegistryModels(target);
    } else if (target.id === 'registry-groups-list') {
        renderRegistryGroups(target);
    } else if (target.id === 'group-routing-list') {
        renderGroupRouting(target);
    } else if (target.id === 'pending-review-list') {
        renderPendingReview(target);
    } else if (target.id === 'cross-source-list') {
        renderCrossSource(target);
    }
});

function renderChannels(container) {
    try {
        const data = JSON.parse(container.textContent);
        if (!Array.isArray(data) || data.length === 0) {
            container.innerHTML = '<p class="loading">暂无渠道数据</p>';
            return;
        }
        let html = '<table><thead><tr><th>渠道</th><th>厂商</th><th>状态</th><th>账号</th><th>优先级</th></tr></thead><tbody>';
        data.forEach(ch => {
            const statusClass = `status-${esc(ch.status)}`;
            html += `<tr>
                <td>${esc(ch.name)}</td>
                <td>${esc(ch.provider)}</td>
                <td class="${statusClass}">${esc(ch.status)}</td>
                <td>${esc(ch.active_accounts)}/${esc(ch.accounts)}</td>
                <td>${esc(ch.priority)}</td>
            </tr>`;
        });
        html += '</tbody></table>';
        container.innerHTML = html;
    } catch (e) {
        // 非 JSON 响应，保持原样
    }
}

function renderPromotions(container) {
    try {
        const data = JSON.parse(container.textContent);
        if (!Array.isArray(data) || data.length === 0) {
            container.innerHTML = '<p class="loading">暂无优惠情报</p>';
            return;
        }
        let html = '';
        data.forEach(p => {
            html += `<div class="promo-item">
                <span class="provider">${esc(p.provider)}</span>
                <span class="discount">${esc(p.discount_desc)}</span>
                <div class="title">${esc(p.title)}</div>
                <div class="meta">截止: ${esc(p.end_date) || '未知'} | 置信度: ${esc((p.confidence || 0).toFixed(2))}</div>
            </div>`;
        });
        container.innerHTML = html;
    } catch (e) {
        // 非 JSON 响应
    }
}

function renderPrices(container) {
    try {
        const data = JSON.parse(container.textContent);
        if (!Array.isArray(data) || data.length === 0) {
            container.innerHTML = '<p class="loading">暂无价格数据</p>';
            return;
        }
        let html = '<table><thead><tr><th>模型</th><th>渠道</th><th>类型</th><th>时段</th><th>输入价</th><th>输出价</th></tr></thead><tbody>';
        data.forEach(p => {
            html += `<tr>
                <td>${esc(p.model)}</td>
                <td>${esc(p.channel_name)}</td>
                <td>${esc(p.price_type)}</td>
                <td>${esc(p.time_range)}</td>
                <td class="price-low">${esc(p.input_price ?? '-')}</td>
                <td>${esc(p.output_price ?? '-')}</td>
            </tr>`;
        });
        html += '</tbody></table>';
        container.innerHTML = html;
    } catch (e) {
        // 非 JSON 响应
    }
}

// Phase 2.9: model_registry + model_group 渲染
function renderRegistryModels(container) {
    try {
        const data = JSON.parse(container.textContent);
        if (!data || !data.models || data.models.length === 0) {
            container.innerHTML = '<p class="loading">暂无 model registry 数据</p>';
            return;
        }
        let html = `<p class="meta">共 ${esc(data.count)} 个 model (按 family + tier 排序)</p>`;
        html += '<table><thead><tr><th>model_id</th><th>family</th><th>tier</th><th>capabilities</th><th>ctx</th><th>open</th><th>状态</th></tr></thead><tbody>';
        let prevFamily = null;
        for (const m of data.models) {
            if (m.family !== prevFamily) {
                // family 分组 (可选高亮, 这里仅逻辑分组, 视觉一致)
                prevFamily = m.family;
            }
            const caps = m.capabilities ? JSON.parse(m.capabilities) : [];
            const ctx = m.context_length ? (m.context_length >= 1024 ? Math.round(m.context_length / 1024) + 'k' : m.context_length) : '?';
            html += `<tr>
                <td><code>${esc(m.model_id)}</code></td>
                <td>${esc(m.family)}</td>
                <td>${esc(m.tier)}</td>
                <td class="caps">${esc(caps.join(', '))}</td>
                <td>${esc(ctx)}</td>
                <td>${m.is_open_source ? '✓' : '·'}</td>
                <td>${esc(m.status)}</td>
            </tr>`;
        }
        html += '</tbody></table>';
        container.innerHTML = html;
    } catch (e) {
        console.error('renderRegistryModels failed:', e);
        container.innerHTML = '<p class="loading">渲染失败: ' + esc(e.message) + '</p>';
    }
}

function renderRegistryGroups(container) {
    try {
        const data = JSON.parse(container.textContent);
        if (!data || !data.groups || data.groups.length === 0) {
            container.innerHTML = '<p class="loading">暂无 model group 数据</p>';
            return;
        }
        let html = `<p class="meta">共 ${esc(data.group_count)} 个 group, ${esc(data.member_count)} 个 member</p>`;
        for (const g of data.groups) {
            const caps = g.capabilities ? JSON.parse(g.capabilities) : [];
            const tier = g.tier || '*';
            const fallback = g.fallback_group || '(无降级)';
            const ctxMin = g.min_context || 0;
            html += `<div class="group-card">
                <h3><code>${esc(g.group_id)}</code> <span class="tier">${esc(tier)}</span></h3>
                <p class="desc">${esc(g.display_name || '')} | strategy=${esc(g.strategy)} | fallback=${esc(fallback)}</p>
                ${caps.length ? `<p class="caps">要求 capabilities: ${esc(caps.join(', '))}</p>` : ''}
                ${ctxMin ? `<p class="meta">min_context=${esc(ctxMin)}</p>` : ''}
                <details><summary>members (${g.members.length})</summary><ul>`;
            for (const m of g.members) {
                html += `<li><code>${esc(m.model_id)}</code> <span class="meta">priority=${esc(m.priority)}</span></li>`;
            }
            html += '</ul></details></div>';
        }
        container.innerHTML = html;
    } catch (e) {
        console.error('renderRegistryGroups failed:', e);
        container.innerHTML = '<p class="loading">渲染失败: ' + esc(e.message) + '</p>';
    }
}

// Phase 3.A: Group 路由命中率渲染 (Dashboard 唯一路径)
function renderGroupRouting(container) {
    try {
        const data = JSON.parse(container.textContent);

        // 1) 顶部 summary 填充
        const summary = data.summary || {};
        const grTotal = document.getElementById('gr-total');
        const grPct = document.getElementById('gr-grouped-pct');
        const grExact = document.getElementById('gr-exact');
        if (grTotal) grTotal.textContent = summary.total_requests ?? 0;
        if (grPct) grPct.textContent = summary.group_routed_pct ?? 0;
        if (grExact) grExact.textContent = summary.exact_model_requests ?? 0;

        const groups = data.groups || [];
        const unmatched = data.unmatched || {};

        if (groups.length === 0 && unmatched.request_count === 0) {
            container.innerHTML = '<p class="loading">近 7 天无组路由请求 · 所有请求都是精确模型名</p>';
            return;
        }

        let html = '';

        // 2) 每个 group 一个卡片
        for (const g of groups) {
            const dist = g.model_distribution || [];
            const channels = g.channels_used || [];
            html += `<div class="gr-group-card">
                <h4><code>${esc(g.group_id)}</code>
                    <span class="meta">${esc(g.request_count)} 次 · 估 ${esc(g.total_cost ?? 0)} 元</span>
                </h4>`;
            if (dist.length === 0) {
                html += '<p class="meta">无命中明细</p>';
            } else {
                html += '<table><thead><tr><th>实际 model</th><th>次数</th><th>占比</th><th>分布</th></tr></thead><tbody>';
                for (const m of dist) {
                    const pct = m.percentage || 0;
                    html += `<tr>
                        <td><code>${esc(m.model)}</code></td>
                        <td>${esc(m.count)}</td>
                        <td>${esc(pct.toFixed(1))}%</td>
                        <td><div class="bar-track"><div class="bar-fill" style="width:${esc(pct)}%"></div></div></td>
                    </tr>`;
                }
                html += '</tbody></table>';
            }
            if (channels.length > 0) {
                html += `<p class="meta">渠道: ${channels.map(c => `<span class="chip">${esc(c)}</span>`).join(' ')}</p>`;
            }
            html += '</div>';
        }

        // 3) unmatched (精确路由)
        if (unmatched.request_count > 0) {
            const top = unmatched.top_models || [];
            html += `<div class="gr-group-card unmatched">
                <h4>精确模型路由 <span class="meta">${esc(unmatched.request_count)} 次 (group_id 为 NULL)</span></h4>`;
            if (top.length > 0) {
                html += '<table><thead><tr><th>model</th><th>次数</th></tr></thead><tbody>';
                for (const m of top) {
                    html += `<tr><td><code>${esc(m.model)}</code></td><td>${esc(m.count)}</td></tr>`;
                }
                html += '</tbody></table>';
            }
            html += '</div>';
        }

        container.innerHTML = html;
    } catch (e) {
        console.error('renderGroupRouting failed:', e);
        container.innerHTML = '<p class="loading">渲染失败: ' + esc(e.message) + '</p>';
    }
}

// Phase 4 E1: 待确认能力变更队列
async function approvePendingReview(logId, btn) {
    if (!confirm(`确定批准 log #${logId} ? Tier 会自动写入 ModelRegistry`)) return;
    try {
        const resp = await fetch(`/dashboard/api/pending-review/${logId}/approve`, { method: 'POST' });
        const data = await resp.json();
        if (resp.ok) {
            const card = btn.closest('.pr-card');
            if (card) card.remove();
            const summary = document.getElementById('pr-summary');
            if (summary) {
                const n = Math.max(0, parseInt(summary.textContent.replace(/\D/g, '') || '0', 10) - 1);
                summary.textContent = `${n} 条待确认`;
            }
        } else {
            alert('批准失败: ' + (data.detail || resp.statusText));
        }
    } catch (e) {
        alert('网络错误: ' + e.message);
    }
}

async function rejectPendingReview(logId, btn) {
    if (!confirm(`确定拒绝 log #${logId} ? 不会修改 ModelRegistry`)) return;
    try {
        const resp = await fetch(`/dashboard/api/pending-review/${logId}/reject`, { method: 'POST' });
        const data = await resp.json();
        if (resp.ok) {
            const card = btn.closest('.pr-card');
            if (card) card.remove();
            const summary = document.getElementById('pr-summary');
            if (summary) {
                const n = Math.max(0, parseInt(summary.textContent.replace(/\D/g, '') || '0', 10) - 1);
                summary.textContent = `${n} 条待确认`;
            }
        } else {
            alert('拒绝失败: ' + (data.detail || resp.statusText));
        }
    } catch (e) {
        alert('网络错误: ' + e.message);
    }
}

function renderPendingReview(container) {
    try {
        const data = JSON.parse(container.textContent);
        const items = data.items || [];
        if (items.length === 0) {
            container.innerHTML = '<p class="loading">✓ 当前无待确认能力变更</p>';
            return;
        }
        let html = `<p class="meta">共 <strong id="pr-summary">${esc(data.count)} 条待确认</strong>，按时间倒序</p>`;
        for (const item of items) {
            const conf = item.confidence || 0;
            const confLabel = item.confidence_level || 'low';
            const confClass = `conf-${confLabel}`;
            const created = item.created_at ? new Date(item.created_at).toLocaleString('zh-CN') : '?';
            html += `<div class="pr-card">
                <div class="pr-row">
                    <code class="pr-model">${esc(item.model_id)}</code>
                    <span class="pr-field">${esc(item.field_changed)}</span>
                    <span class="pr-old">${esc(item.old_value ?? '(空)')}</span>
                    <span class="pr-arrow">→</span>
                    <span class="pr-new">${esc(item.new_value ?? '(空)')}</span>
                    <span class="pr-conf ${confClass}">${esc(conf.toFixed(2))} · ${esc(confLabel)}</span>
                    <span class="pr-time">${esc(created)}</span>
                </div>
                <div class="pr-actions">
                    <button class="btn-approve" onclick="approvePendingReview(${item.id}, this)">批准</button>
                    <button class="btn-reject" onclick="rejectPendingReview(${item.id}, this)">拒绝</button>
                </div>
            </div>`;
        }
        container.innerHTML = html;
    } catch (e) {
        console.error('renderPendingReview failed:', e);
        container.innerHTML = '<p class="loading">渲染失败: ' + esc(e.message) + '</p>';
    }
}

// Phase 4 E3: 跨源分歧
async function resolveCrossSource(discrepancyId, btn, ignored) {
    const action = ignored ? '忽略' : '拍板';
    if (!confirm(`确定${action} discrepancy #${discrepancyId} ?`)) return;
    let resolvedValue = '';
    if (!ignored) {
        resolvedValue = prompt('请输入最终值 (capabilities 请用 JSON 数组)', 'T3');
        if (resolvedValue === null) return;
    } else {
        resolvedValue = '(ignored)';
    }
    const note = prompt('备注 (可空)', '');
    try {
        const resp = await fetch(`/dashboard/api/cross-source/discrepancies/${discrepancyId}/resolve`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                resolved_value: resolvedValue,
                resolved_note: note || null,
                ignored: ignored,
            }),
        });
        const data = await resp.json();
        if (resp.ok) {
            const card = btn.closest('.cs-card');
            if (card) card.remove();
            const summary = document.getElementById('cs-summary');
            if (summary) {
                const n = Math.max(0, parseInt(summary.textContent.replace(/\D/g, '') || '0', 10) - 1);
                summary.textContent = `${n} 条待处理`;
            }
        } else {
            alert('操作失败: ' + (data.detail || resp.statusText));
        }
    } catch (e) {
        alert('网络错误: ' + e.message);
    }
}

async function runCrossSourceDetection(btn) {
    btn.disabled = true;
    const result = document.getElementById('detection-result');
    result.textContent = '检测中...';
    try {
        const resp = await fetch('/dashboard/api/cross-source/run-detection', { method: 'POST' });
        const data = await resp.json();
        if (resp.ok) {
            result.textContent = `本次: ${data.total_models} 个 model · ${data.with_discrepancy} 个分歧 · ${data.recorded} 条入库`;
            // 触发刷新列表
            htmx.trigger('#cross-source-list', 'load');
        } else {
            result.textContent = '失败: ' + (data.detail || resp.statusText);
        }
    } catch (e) {
        result.textContent = '网络错误: ' + e.message;
    } finally {
        btn.disabled = false;
    }
}

function renderCrossSource(container) {
    try {
        const data = JSON.parse(container.textContent);
        const items = data.items || [];
        if (items.length === 0) {
            container.innerHTML = '<p class="loading">✓ 当前无跨源分歧</p>';
            return;
        }
        let html = `<p class="meta">共 <strong id="cs-summary">${esc(data.count)} 条待处理</strong>，按分歧程度排序</p>`;
        for (const item of items) {
            const score = item.discrepancy_score || 0;
            const scoreLabel = score >= 0.9 ? '高' : score >= 0.6 ? '中' : '低';
            const scoreClass = score >= 0.9 ? 'conf-high' : score >= 0.6 ? 'conf-medium' : 'conf-low';
            const sources = item.sources || [];
            // 按 value 分组显示
            const byValue = {};
            for (const s of sources) {
                const v = String(s.value ?? '(空)');
                if (!byValue[v]) byValue[v] = [];
                byValue[v].push(s);
            }
            let sourcesHtml = '';
            for (const [v, entries] of Object.entries(byValue)) {
                const chips = entries.map(e =>
                    `<span class="chip">${esc(e.source)}<span class="conf-mini">${e.confidence != null ? '·' + esc(e.confidence.toFixed(2)) : ''}</span></span>`
                ).join(' ');
                sourcesHtml += `<div class="cs-value-row"><code>${esc(v)}</code>${chips}</div>`;
            }
            html += `<div class="cs-card">
                <div class="cs-row">
                    <code class="cs-model">${esc(item.model_id)}</code>
                    <span class="cs-field">${esc(item.field_changed)}</span>
                    <span class="pr-conf ${scoreClass}">分歧 ${esc(score.toFixed(2))} · ${esc(scoreLabel)}</span>
                </div>
                <div class="cs-sources">${sourcesHtml}</div>
                <div class="pr-actions">
                    <button class="btn-approve" onclick="resolveCrossSource(${item.id}, this, false)">拍板</button>
                    <button class="btn-reject" onclick="resolveCrossSource(${item.id}, this, true)">忽略</button>
                </div>
            </div>`;
        }
        container.innerHTML = html;
    } catch (e) {
        console.error('renderCrossSource failed:', e);
        container.innerHTML = '<p class="loading">渲染失败: ' + esc(e.message) + '</p>';
    }
}

// 阶段1 (2026-08-14): prompt cache 节省成本统计
async function loadCacheStats() {
    try {
        const resp = await authFetch('/dashboard/api/cache/stats?days=7');
        if (!resp.ok) return;
        const data = await resp.json();

        // 顶部 summary
        const savedEl = document.getElementById('cache-saved');
        const tokensEl = document.getElementById('cache-tokens');
        const pctEl = document.getElementById('cache-hit-pct');
        if (savedEl) savedEl.textContent = '¥' + (data.saved_cost_cny || 0).toFixed(4);
        if (tokensEl) tokensEl.textContent = (data.total_cached_tokens || 0).toLocaleString();
        if (pctEl) pctEl.textContent = (data.hit_rate_pct || 0).toFixed(1);

        // 按渠道
        const chList = document.getElementById('cache-channel-list');
        if (chList && data.by_channel && data.by_channel.length > 0) {
            let html = '<table class="cache-table"><thead><tr><th>渠道</th><th>Input</th><th>Cached</th><th>命中率</th><th>节省</th></tr></thead><tbody>';
            for (const c of data.by_channel) {
                html += `<tr><td>${esc(c.channel_name)}</td><td>${c.input_tokens.toLocaleString()}</td><td>${c.cached_tokens.toLocaleString()}</td><td>${c.hit_rate_pct.toFixed(1)}%</td><td>¥${c.saved_cost_cny.toFixed(4)}</td></tr>`;
            }
            html += '</tbody></table>';
            chList.innerHTML = html;
        } else if (chList) {
            chList.innerHTML = '<div style="color:#6a737d;font-size:13px;">暂无 cache 数据</div>';
        }

        // 按 model (TOP 10)
        const mList = document.getElementById('cache-model-list');
        if (mList && data.by_model && data.by_model.length > 0) {
            let html = '<table class="cache-table"><thead><tr><th>Model</th><th>Input</th><th>Cached</th><th>命中率</th><th>节省</th></tr></thead><tbody>';
            for (const m of data.by_model) {
                html += `<tr><td>${esc(m.model)}</td><td>${m.input_tokens.toLocaleString()}</td><td>${m.cached_tokens.toLocaleString()}</td><td>${m.hit_rate_pct.toFixed(1)}%</td><td>¥${m.saved_cost_cny.toFixed(4)}</td></tr>`;
            }
            html += '</tbody></table>';
            mList.innerHTML = html;
        } else if (mList) {
            mList.innerHTML = '<div style="color:#6a737d;font-size:13px;">暂无 cache 数据</div>';
        }
    } catch (e) {
        console.warn('Cache stats load failed:', e);
    }
}

// 用量统计图表
async function loadUsageChart() {
    try {
        const resp = await fetch('/dashboard/api/usage/summary?days=7');
        const data = await resp.json();

        if (!data || data.length === 0) return;

        const ctx = document.getElementById('usage-chart');
        if (!ctx) return;

        new Chart(ctx, {
            type: 'bar',
            data: {
                labels: data.map(d => d.date),
                datasets: [{
                    label: '请求数',
                    data: data.map(d => d.request_count),
                    backgroundColor: '#1d9bf0',
                    borderRadius: 4,
                }, {
                    label: '输出Tokens (K)',
                    data: data.map(d => (d.total_output_tokens / 1000).toFixed(1)),
                    backgroundColor: '#00ba7c',
                    borderRadius: 4,
                }]
            },
            options: {
                responsive: true,
                plugins: {
                    legend: { labels: { color: '#71767b' } }
                },
                scales: {
                    x: { ticks: { color: '#71767b' }, grid: { color: '#2f3336' } },
                    y: { ticks: { color: '#71767b' }, grid: { color: '#2f3336' } }
                }
            }
        });
    } catch (e) {
        console.log('用量图表加载失败:', e);
    }
}

// 页面加载完成后初始化图表
document.addEventListener('DOMContentLoaded', loadUsageChart);
document.addEventListener('DOMContentLoaded', loadCacheStats);  // 阶段1 (2026-08-14)
document.addEventListener('DOMContentLoaded', loadSuggestions);  // 方案A (2026-08-14)

// 方案A (2026-08-14): 模型分组建议
async function loadSuggestions() {
    try {
        // 拉 pending 建议 + 统计
        const [pendingResp, approvedResp, appliedResp] = await Promise.all([
            authFetch('/dashboard/api/suggestions?status=pending'),
            authFetch('/dashboard/api/suggestions?status=approved'),
            authFetch('/dashboard/api/suggestions?status=applied'),
        ]);
        const pending = pendingResp.ok ? await pendingResp.json() : {model: [], group: []};
        const approved = approvedResp.ok ? await approvedResp.json() : {model: [], group: []};
        const applied = appliedResp.ok ? await appliedResp.json() : {model: [], group: []};

        document.getElementById('sg-pending').textContent = pending.model.length + pending.group.length;
        document.getElementById('sg-approved').textContent = approved.model.length + approved.group.length;
        document.getElementById('sg-applied').textContent = applied.model.length + applied.group.length;

        const list = document.getElementById('suggestions-list');
        if (!list) return;

        if (pending.model.length === 0 && pending.group.length === 0) {
            list.innerHTML = '<div style="color:#6a737d;padding:12px;">暂无待审建议</div>';
            return;
        }

        let html = '<table class="suggestions-table"><thead><tr><th>类型</th><th>对象</th><th>变更</th><th>置信度</th><th>理由</th><th>操作</th></tr></thead><tbody>';
        for (const m of pending.model) {
            html += '<tr>';
            html += '<td>🟢 model</td>';
            html += `<td>${esc(m.model_id)}</td>`;
            html += `<td>${esc(m.current_value || '(空)')} → <strong>${esc(m.suggested_value)}</strong></td>`;
            html += `<td><span class="confidence-badge conf-${Math.floor((m.confidence || 0) * 10)}">${((m.confidence || 0) * 100).toFixed(0)}%</span></td>`;
            html += `<td><small>${esc(m.reason || '')}</small></td>`;
            html += `<td>`;
            html += `<button class="btn btn-sm btn-success" onclick="approveSuggestion('model', ${m.id})">批准</button> `;
            html += `<button class="btn btn-sm btn-danger" onclick="rejectSuggestion('model', ${m.id})">拒绝</button>`;
            html += `</td>`;
            html += '</tr>';
        }
        for (const g of pending.group) {
            html += '<tr>';
            html += '<td>🟡 group</td>';
            html += `<td>${esc(g.group_id)}</td>`;
            html += `<td>${esc(g.change_type)} ${esc(g.target_groups || '')}</td>`;
            html += `<td><span class="confidence-badge conf-${Math.floor((g.confidence || 0) * 10)}">${((g.confidence || 0) * 100).toFixed(0)}%</span></td>`;
            html += `<td><small>${esc(g.reason || '')}</small></td>`;
            html += `<td>`;
            html += `<button class="btn btn-sm btn-success" onclick="approveSuggestion('group', ${g.id})">批准</button> `;
            html += `<button class="btn btn-sm btn-danger" onclick="rejectSuggestion('group', ${g.id})">拒绝</button>`;
            html += `</td>`;
            html += '</tr>';
        }
        html += '</tbody></table>';
        list.innerHTML = html;
    } catch (e) {
        console.warn('Suggestions load failed:', e);
    }
}

async function approveSuggestion(type, id) {
    if (!confirm(`批准此建议? (类型=${type}, id=${id})\n批准后下次 "一键应用" 会真正改 db`)) return;
    const resp = await authFetch(`/dashboard/api/suggestions/${type}/${id}/approve`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({reviewed_by: 'dashboard_user'}),
    });
    if (resp.ok) {
        alert('已批准');
        loadSuggestions();
    } else {
        alert('失败: ' + resp.statusText);
    }
}

async function rejectSuggestion(type, id) {
    if (!confirm(`拒绝此建议? (类型=${type}, id=${id})`)) return;
    const resp = await authFetch(`/dashboard/api/suggestions/${type}/${id}/reject`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({reviewed_by: 'dashboard_user'}),
    });
    if (resp.ok) {
        alert('已拒绝');
        loadSuggestions();
    } else {
        alert('失败: ' + resp.statusText);
    }
}

async function applyAllSuggestions() {
    if (!confirm('一键应用所有已批准建议?\n\n此操作会直接改 db (tier / group_member), 不可逆\n\n建议先 review 列表再 apply')) return;
    const resp = await authFetch('/dashboard/api/suggestions/apply-all', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({min_confidence: 0.0, reviewed_by: 'dashboard_user'}),
    });
    if (resp.ok) {
        const data = await resp.json();
        alert(`应用完成:\nmodel: ${data.applied_model}\ngroup: ${data.applied_group}\nerrors: ${data.errors.length}`);
        loadSuggestions();
    } else {
        alert('失败: ' + resp.statusText);
    }
}

// ============================================================
// V4.5: 周报查看页 (与 dashboard/static/index.html #weekly-report-section 配套)
// ============================================================

async function loadReportsList() {
    // 加载 reports/ 下所有周报元信息, 填充 select options + meta
    const sel = document.getElementById('report-select');
    const meta = document.getElementById('report-meta');
    if (!sel || !meta) return;
    try {
        const resp = await fetch('/dashboard/api/reports');
        const data = await resp.json();

        // 清空, 重建 options (保留 "最新" 默认项)
        sel.innerHTML = '<option value="latest">最新</option>';
        (data.reports || []).forEach(r => {
            const opt = document.createElement('option');
            opt.value = r.date;
            opt.textContent = r.date;
            sel.appendChild(opt);
        });

        if (data.latest && data.latest.modified_at) {
            const m = new Date(data.latest.modified_at * 1000);
            meta.textContent = `最新报告生成于 ${m.toLocaleString('zh-CN')} · 共 ${data.count} 期`;
        } else {
            meta.textContent = `未生成过周报 — 请先跑 python scripts/generate_weekly_report.py`;
        }
    } catch (e) {
        meta.textContent = `加载周报列表失败: ${e.message}`;
    }
}

function loadWeeklyReport(value) {
    // select 切换时调 — value="latest" 拿 latest, 否则拿指定日期
    const url = value === 'latest' || !value
        ? '/dashboard/api/reports/latest'
        : '/dashboard/api/reports/' + value;
    const content = document.getElementById('weekly-report-content');
    if (!content) return;
    content.innerHTML = '<p class="loading">加载中...</p>';
    fetch(url)
        .then(r => {
            if (!r.ok) throw new Error(`HTTP ${r.status}`);
            return r.text();
        })
        .then(html => {
            content.innerHTML = html;
        })
        .catch(e => {
            content.innerHTML = `<p class="error">加载周报失败: ${esc(e.message)}</p>`;
        });
}

document.addEventListener('DOMContentLoaded', loadReportsList);

// ============================================================
// V4.6: Dashboard 账号管理 (列表 / 加 / 启停 / 轮换 / 过期扫描)
// 与 dashboard/routes.py /api/accounts/* 配套
// ============================================================

async function loadActiveChannels() {
    // 填充加账号表单的 provider select
    const sel = document.getElementById('acct-provider');
    if (!sel) return;
    try {
        const resp = await fetch('/dashboard/api/channels/active');
        const data = await resp.json();
        sel.innerHTML = '<option value="">-- 选择 provider --</option>';
        (data.channels || []).forEach(ch => {
            const opt = document.createElement('option');
            opt.value = ch.provider;
            opt.textContent = `${ch.provider} (${ch.name})`;
            sel.appendChild(opt);
        });
    } catch (e) {
        sel.innerHTML = `<option value="">加载失败: ${esc(e.message)}</option>`;
    }
}

async function loadAccounts() {
    // 重新拉列表 — 同 hx-get 同样的 URL, 但用 fetch 强制刷新
    const container = document.getElementById('accounts-list');
    const meta = document.getElementById('accounts-meta');
    if (meta) meta.textContent = '加载中...';
    try {
        const resp = await fetch('/dashboard/api/accounts');
        const data = await resp.json();
        renderAccounts(data);
        if (meta) {
            const active = (data.accounts || []).filter(a => a.status === 'active').length;
            meta.textContent = `共 ${data.count} 个账号 · ${active} 个 active`;
        }
    } catch (e) {
        if (meta) meta.textContent = `加载失败: ${esc(e.message)}`;
        if (container) container.innerHTML = `<p class="error">加载失败: ${esc(e.message)}</p>`;
    }
}

function renderAccounts(data) {
    const container = document.getElementById('accounts-list');
    if (!container) return;
    const items = data.accounts || [];
    if (items.length === 0) {
        container.innerHTML = '<p class="loading">✓ 当前无账号 — 点上方 "+ 加新账号" 添加</p>';
        return;
    }

    let html = '<table><thead><tr><th>ID</th><th>Provider</th><th>账号名</th><th>状态</th><th>已用/总额</th><th>过期</th><th>Key</th><th>备注</th><th>操作</th></tr></thead><tbody>';
    for (const a of items) {
        const quota = a.quota_total
            ? `${a.quota_used.toLocaleString()} / ${a.quota_total.toLocaleString()} (${a.quota_pct ?? 0}%)`
            : '∅';
        const expires = a.expires_at ? a.expires_at.slice(0, 10) : '∅';
        const notes = a.notes || '';
        const status = a.status;
        const actions = [];
        if (status !== 'active') {
            actions.push(`<button class="btn-sm btn-enable" onclick="toggleAccount(${a.id}, 'active', this)">启用</button>`);
        } else {
            actions.push(`<button class="btn-sm btn-disable" onclick="toggleAccount(${a.id}, 'disabled', this)">禁用</button>`);
        }
        actions.push(`<button class="btn-sm btn-rotate" onclick="rotateAccountKey(${a.id}, this)">轮换</button>`);
        html += `<tr>
            <td>${a.id}</td>
            <td>${esc(a.provider)}</td>
            <td><code>${esc(a.account_name)}</code></td>
            <td class="status-${esc(status)}">${esc(status)}</td>
            <td>${quota}</td>
            <td>${esc(expires)}</td>
            <td><code class="key-display">${esc(a.key_display)}</code></td>
            <td class="notes">${esc(notes)}</td>
            <td class="row-actions">${actions.join(' ')}</td>
        </tr>`;
    }
    html += '</tbody></table>';
    container.innerHTML = html;
}

async function toggleAccount(accountId, newStatus, btn) {
    if (!confirm(`确定将账号 #${accountId} 切换为 ${newStatus}?`)) return;
    btn.disabled = true;
    try {
        const resp = await fetch(`/dashboard/api/accounts/${accountId}/${newStatus === 'active' ? 'enable' : 'disable'}`, {
            method: 'POST',
        });
        const data = await resp.json();
        if (resp.ok) {
            await loadAccounts();
        } else {
            alert('操作失败: ' + (data.detail || resp.statusText));
            btn.disabled = false;
        }
    } catch (e) {
        alert('网络错误: ' + e.message);
        btn.disabled = false;
    }
}

async function rotateAccountKey(accountId, btn) {
    const newKey = prompt(`为账号 #${accountId} 输入新 API Key\n(明文仅本次提交到后端, 加密入库)`);
    if (!newKey) return;
    btn.disabled = true;
    try {
        const resp = await fetch(`/dashboard/api/accounts/${accountId}/rotate`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ api_key: newKey }),
        });
        const data = await resp.json();
        if (resp.ok) {
            await loadAccounts();
        } else {
            alert('轮换失败: ' + (data.detail || resp.statusText));
            btn.disabled = false;
        }
    } catch (e) {
        alert('网络错误: ' + e.message);
        btn.disabled = false;
    }
}

async function submitNewAccount() {
    const provider = document.getElementById('acct-provider').value;
    const name = document.getElementById('acct-name').value.trim();
    const apiKey = document.getElementById('acct-api-key').value;
    const quotaTotalRaw = document.getElementById('acct-quota-total').value;
    const expiresAt = document.getElementById('acct-expires-at').value;
    const notes = document.getElementById('acct-notes').value.trim();
    const feedback = document.getElementById('acct-add-feedback');

    if (!provider) { feedback.textContent = '❌ 请选择 provider'; feedback.className = 'feedback error'; return; }
    if (!name) { feedback.textContent = '❌ 请输入账号名'; feedback.className = 'feedback error'; return; }
    if (!apiKey) { feedback.textContent = '❌ 请输入 API Key'; feedback.className = 'feedback error'; return; }

    const payload = { provider, name, api_key: apiKey };
    if (quotaTotalRaw) payload.quota_total = parseInt(quotaTotalRaw, 10);
    if (expiresAt) payload.expires_at = expiresAt;
    if (notes) payload.notes = notes;

    feedback.textContent = '提交中...';
    feedback.className = 'feedback';
    try {
        const resp = await fetch('/dashboard/api/accounts', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
        const data = await resp.json();
        if (resp.ok) {
            feedback.textContent = `✅ 账号 #${data.account.id} 创建成功 (${data.account.account_name})`;
            feedback.className = 'feedback success';
            // 清空表单
            document.getElementById('acct-name').value = '';
            document.getElementById('acct-api-key').value = '';
            document.getElementById('acct-quota-total').value = '';
            document.getElementById('acct-expires-at').value = '';
            document.getElementById('acct-notes').value = '';
            await loadAccounts();
        } else {
            feedback.textContent = '❌ ' + (data.detail || resp.statusText);
            feedback.className = 'feedback error';
        }
    } catch (e) {
        feedback.textContent = '❌ 网络错误: ' + e.message;
        feedback.className = 'feedback error';
    }
}

async function triggerExpireCheck() {
    if (!confirm('扫描并标记所有 expires_at < now 的账号为 expired?')) return;
    try {
        const resp = await fetch('/dashboard/api/accounts/expire-check', { method: 'POST' });
        const data = await resp.json();
        if (resp.ok) {
            alert(`扫描完成 — ${data.expired_count} 个账号标记为 expired`);
            await loadAccounts();
        } else {
            alert('扫描失败: ' + (data.detail || resp.statusText));
        }
    } catch (e) {
        alert('网络错误: ' + e.message);
    }
}

// htmx 渲染: accounts-list
document.body.addEventListener('htmx:afterSwap', function(event) {
    if (event.detail.target.id === 'accounts-list') {
        try {
            const data = JSON.parse(event.detail.target.textContent);
            renderAccounts(data);
            const meta = document.getElementById('accounts-meta');
            if (meta) {
                const active = (data.accounts || []).filter(a => a.status === 'active').length;
                meta.textContent = `共 ${data.count} 个账号 · ${active} 个 active`;
            }
        } catch (e) {
            // 非 JSON — 忽略
        }
    }
});

// 页面加载时拉 active channels 填表单
document.addEventListener('DOMContentLoaded', loadActiveChannels);
