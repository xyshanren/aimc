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
        let html = '<table><thead><tr><th>渠道</th><th>厂商</th><th>状态</th><th>账号</th><th>优先级</th><th>drop_params</th><th>操作</th></tr></thead><tbody>';
        data.forEach(ch => {
            const statusClass = `status-${esc(ch.status)}`;
            const dp = ch.drop_params ? esc(ch.drop_params) : '<span class="text-muted">—</span>';
            html += `<tr data-channel-id="${esc(ch.id)}">
                <td>${esc(ch.name)}</td>
                <td>${esc(ch.provider)}</td>
                <td class="${statusClass}">${esc(ch.status)}</td>
                <td>${esc(ch.active_accounts)}/${esc(ch.accounts)}</td>
                <td>${esc(ch.priority)}</td>
                <td class="mono" style="font-size:11px;">${dp}</td>
                <td><button class="btn-tiny" onclick="editChannelDropParams(${esc(ch.id)}, ${JSON.stringify(ch.drop_params || '').replace(/"/g, '&quot;')})">编辑</button></td>
            </tr>`;
        });
        html += '</tbody></table>';
        html += '<div class="text-muted" style="margin-top:var(--space-xs);font-size:11px;">drop_params: 逗号分隔的需剥离参数 (如 reasoning_effort,max_tokens); 留空=全透传; messages/model/stream 永远保留</div>';
        container.innerHTML = html;
    } catch (e) {
        // 非 JSON 响应，保持原样
    }
}

async function editChannelDropParams(channelId, currentVal) {
    const newVal = prompt('drop_params (逗号分隔, 留空=全透传)\n例如: reasoning_effort,max_tokens', currentVal || '');
    if (newVal === null) return; // 用户取消
    try {
        const resp = await fetch(`/dashboard/api/channels/${channelId}`, {
            method: 'PUT',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({drop_params: newVal.trim()}),
        });
        const result = await resp.json();
        if (result.ok) {
            // 刷新渠道列表
            const list = document.getElementById('channels-list');
            if (list) {
                fetch('/dashboard/api/channels').then(r => r.text()).then(t => {
                    list.textContent = t;
                    renderChannels(list);
                });
            }
        } else {
            alert('保存失败: ' + (result.detail || JSON.stringify(result)));
        }
    } catch (e) {
        alert('保存失败: ' + e.message);
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
                ${p.url ? `<a class="promo-link" href="${esc(p.url)}" target="_blank" rel="noopener">查看原文 ↗</a>` : ''}
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

// === P0-UI v5: model_registry + model_group 渲染 ===
function renderRegistryModels(container) {
    try {
        const data = JSON.parse(container.textContent);
        if (!data || !data.models || data.models.length === 0) {
            container.innerHTML = '<div class="empty">暂无 model registry 数据</div>';
            return;
        }
        let html = '<div class="text-muted" style="margin-bottom:var(--space-sm);font-size:12px;">共 ' + esc(data.count) + ' 个 model</div>';
        html += '<div class="scroll-y-320">';
        html += '<table style="font-size:12px;"><thead><tr><th>Model</th><th>Family</th><th class="mono">Tier</th><th class="mono">Ctx</th><th>Capabilities</th><th class="mono">Status</th></tr></thead><tbody>';
        for (const m of data.models) {
            const caps = m.capabilities ? (Array.isArray(m.capabilities) ? m.capabilities : JSON.parse(m.capabilities)) : [];
            const tier = m.tier || '?';
            html += '<tr>';
            html += '<td><code class="text-mono" style="font-size:11px;">' + esc(m.model_id) + '</code></td>';
            html += '<td style="font-size:11px;">' + esc(m.family || '') + '</td>';
            html += '<td class="mono" style="font-weight:600;">' + esc(tier) + '</td>';
            html += '<td class="mono">' + (m.context_length ? Math.round(m.context_length / 1000) + 'k' : '?') + '</td>';
            html += '<td style="font-size:10px;color:var(--text-muted);">' + caps.slice(0, 3).map(esc).join(', ') + (caps.length > 3 ? '...' : '') + '</td>';
            html += '<td class="mono" style="font-size:11px;">' + esc(m.status || 'active') + '</td>';
            html += '</tr>';
        }
        html += '</tbody></table>';
        html += '</div>';
        container.innerHTML = html;
    } catch (e) {
        console.error('renderRegistryModels failed:', e);
        container.innerHTML = '<div class="empty">渲染失败: ' + esc(e.message) + '</div>';
    }
}


// === P0-UI v5 (2026-08-16): renderRegistryGroups + renderRegistryModels 优化 ===

// 替换 renderRegistryGroups: 用表格而不是 list, 每个 group 独立卡片
function renderRegistryGroups(container) {
    try {
        const data = JSON.parse(container.textContent);
        if (!data || !data.groups || data.groups.length === 0) {
            container.innerHTML = '<div class="empty">暂无 model group 数据</div>';
            return;
        }

        // 顶部 summary
        let html = '<div class="kpi-grid" style="grid-template-columns: repeat(3, 1fr); margin-bottom: var(--space-md);">';
        html += '<div class="kpi"><div class="kpi-value" style="font-size:18px;">' + esc(data.group_count) + '</div><div class="kpi-label">Groups</div></div>';
        html += '<div class="kpi"><div class="kpi-value" style="font-size:18px;">' + esc(data.member_count) + '</div><div class="kpi-label">Members</div></div>';
        const tiers = [...new Set(data.groups.map(g => g.tier).filter(Boolean))];
        html += '<div class="kpi"><div class="kpi-value" style="font-size:18px;">' + esc(tiers.length) + '</div><div class="kpi-label">Tiers</div></div>';
        html += '</div>';

        // 每个 group 一个折叠卡片
        for (const g of data.groups) {
            const caps = g.capabilities ? (Array.isArray(g.capabilities) ? g.capabilities : JSON.parse(g.capabilities)) : [];
            const tier = g.tier || '*';
            const fallback = g.fallback_group || '(无降级)';
            const ctxMin = g.min_context || 0;
            const strat = g.strategy || 'cheapest';

            html += '<div class="group-card">';
            // header: group_id + tier + summary
            html += '<div class="flex gap-md" style="align-items:center;flex-wrap:wrap;">';
            html += '<strong class="text-mono" style="font-size:14px;">' + esc(g.group_id) + '</strong>';
            html += '<span class="tier">' + esc(tier) + '</span>';
            html += '<span class="text-muted" style="font-size:11px;">' + esc(strat) + '</span>';
            if (fallback !== '(无降级)') html += '<span class="text-muted" style="font-size:11px;">fallback=' + esc(fallback) + '</span>';
            if (ctxMin) html += '<span class="text-muted" style="font-size:11px;">ctx&gt;=' + esc(ctxMin) + '</span>';
            if (caps.length) html += '<span class="text-muted" style="font-size:11px;">caps: ' + caps.map(esc).join(', ') + '</span>';
            html += '</div>';

            // members 表格
            if (g.members && g.members.length > 0) {
                html += '<table style="margin-top:var(--space-sm);"><thead><tr><th>Model</th><th class="mono">Priority</th></tr></thead><tbody>';
                for (const m of g.members) {
                    html += '<tr><td><code class="text-mono">' + esc(m.model_id) + '</code></td><td class="mono">' + esc(m.priority) + '</td></tr>';
                }
                html += '</tbody></table>';
            } else {
                html += '<div class="text-muted" style="font-size:11px;margin-top:var(--space-sm);">无 member</div>';
            }
            html += '</div>';
        }
        container.innerHTML = html;
    } catch (e) {
        console.error('renderRegistryGroups failed:', e);
        container.innerHTML = '<div class="empty">渲染失败: ' + esc(e.message) + '</div>';
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
                    const modelName = m.model || "(未知)";
                html += `<tr>
                        <td><code class="${m.model ? 'text-mono' : 'text-muted'}" style="font-size:11px;">${esc(modelName)}</code></td>
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
                    const mName = m.model || "(未知)";
                    html += `<tr><td><code class="${m.model ? 'text-mono' : 'text-muted'}" style="font-size:11px;">${esc(mName)}</code></td><td>${esc(m.count)}</td></tr>`;
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
        const FIELD_LABELS = {
            'create': '新模型入库',
            'metadata': '元信息变更',
            'tier': '档位调整',
            'status': '状态变更',
            'display_name': '显示名变更',
            'context_length': '上下文长度',
            'modality': '多模态变更',
        };
        let html = `<p class="meta">共 <strong id="pr-summary">${esc(data.count)} 条待确认</strong>，按时间倒序</p>`;
        for (const item of items) {
            const conf = item.confidence || 0;
            const confLabel = item.confidence_level || 'low';
            const confClass = `conf-${confLabel}`;
            const created = item.created_at ? new Date(item.created_at).toLocaleString('zh-CN') : '?';
            // new_value 可能是 JSON 串 (field_changed=create/metadata), 格式化展示
            let newVal = item.new_value ?? '(空)';
            let oldVal = item.old_value ?? '(空)';
            // 如果是 JSON 字符串, 解析后按 key=value 展示
            try {
                if (typeof newVal === 'string' && newVal.startsWith('{')) {
                    const obj = JSON.parse(newVal);
                    const parts = [];
                    for (const [k, v] of Object.entries(obj)) {
                        const vs = Array.isArray(v) ? v.join(', ') : String(v);
                        parts.push(`<span class="pr-kv"><span class="pr-k">${esc(k)}</span>: <span class="pr-v">${esc(vs)}</span></span>`);
                    }
                    newVal = parts.join(' ');
                }
                if (typeof oldVal === 'string' && oldVal.startsWith('{')) {
                    const obj = JSON.parse(oldVal);
                    const parts = [];
                    for (const [k, v] of Object.entries(obj)) {
                        const vs = Array.isArray(v) ? v.join(', ') : String(v);
                        parts.push(`${esc(k)}: ${esc(vs)}`);
                    }
                    oldVal = parts.join(' ');
                }
            } catch (_) { /* not JSON, show raw */ }
            html += `<div class="pr-card">
                <div class="pr-row">
                    <code class="pr-model">${esc(item.model_id)}</code>
                    ${item.display_name ? `<span class="pr-display-name">${esc(item.display_name)}</span>` : ''}
                    <span class="pr-field" title="${esc(item.field_changed)}">${esc(FIELD_LABELS[item.field_changed] || item.field_changed)}</span>
                    <span class="pr-old">${oldVal}</span>
                    <span class="pr-arrow">→</span>
                    <span class="pr-new">${newVal}</span>
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

// 阶段2 (2026-08-14): 数据新鲜度 (job_health)
async function loadDataFreshness() {
    try {
        const resp = await authFetch('/dashboard/api/data-freshness');
        if (!resp.ok) return;
        const data = await resp.json();
        const jobs = data.jobs || [];
        document.getElementById('df-total').textContent = jobs.length;
        document.getElementById('df-healthy').textContent = jobs.filter(j => j.health === 'healthy').length;
        document.getElementById('df-warning').textContent = jobs.filter(j => j.health === 'warning').length;
        document.getElementById('df-stale').textContent = jobs.filter(j => j.health === 'stale').length;

        const list = document.getElementById('data-freshness-list');
        if (jobs.length === 0) {
            list.innerHTML = '<div style="color:#6a737d;padding:12px;">暂无数据 (listener 待 apscheduler 任务执行)</div>';
            return;
        }
        let html = '<table class="cache-table"><thead><tr><th>Job</th><th>上次成功</th><th>上次失败</th><th>健康度</th><th>运行/成功/失败</th></tr></thead><tbody>';
        for (const j of jobs) {
            const healthColor = {healthy: '#28a745', warning: '#d29922', stale: '#d73a49', unknown: '#6a737d'}[j.health];
            html += '<tr>';
            html += `<td><code>${esc(j.job_id)}</code></td>`;
            html += `<td>${esc(j.last_success_ago || '从未')}</td>`;
            html += `<td>${j.last_error_ago ? `<span style="color:#d73a49;">${esc(j.last_error_ago)}</span>` : '-'}</td>`;
            html += `<td><span style="color:${healthColor};font-weight:bold;">${j.health}</span></td>`;
            html += `<td>${j.total_runs}/${j.total_successes}/${j.total_failures}</td>`;
            html += '</tr>';
        }
        html += '</tbody></table>';
        list.innerHTML = html;
    } catch (e) {
        console.warn('Data freshness load failed:', e);
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
document.addEventListener('DOMContentLoaded', loadDataFreshness);  // 阶段2 (2026-08-14)
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
    const container = document.getElementById('accounts-list');
    const meta = document.getElementById('accounts-meta');
    if (meta) meta.textContent = '加载中...';
    try {
        const resp = await fetch('/dashboard/api/accounts');
        const data = await resp.json();
        renderAccounts(data);
        if (meta) {
            const items = data.accounts || [];
            const active = items.filter(a => a.status === 'active').length;
            const byProvider = {};
            for (const a of items) {
                byProvider[a.provider] = (byProvider[a.provider] || 0) + 1;
            }
            const providerSummary = Object.entries(byProvider)
                .sort((a, b) => b[1] - a[1])
                .map(([p, n]) => p + '=' + n)
                .join(', ');
            meta.textContent = '共 ' + data.count + ' 个账号 · ' + active + ' 个 active · ' + providerSummary;
        }
    } catch (e) {
        if (meta) meta.textContent = '加载失败: ' + e.message;
        if (container) container.innerHTML = '<p class="error">加载失败: ' + e.message + '</p>';
    }
}

function renderAccounts(data) {
    const container = document.getElementById('accounts-list');
    if (!container) return;
    const items = data.accounts || [];
    if (items.length === 0) {
        container.innerHTML = '<p class="loading">当前无账号 — 点上方 "+ 加新账号" 添加</p>';
        return;
    }
    const byProvider = {};
    for (const a of items) {
        byProvider[a.provider] = (byProvider[a.provider] || 0) + 1;
    }
    const multiProvider = Object.entries(byProvider).filter((kv) => kv[1] > 1);
    let explanation = '';
    if (multiProvider.length > 0) {
        explanation = '<div class="info-banner"><strong>💡 多账号说明:</strong> ';
        explanation += multiProvider.map(function(kv){return kv[0] + ' 有 <strong>' + kv[1] + '</strong> 个账号'}).join('，');
        explanation += ' 用于分散频限 + 故障转移。<br>';
        explanation += '重复的 <code>test_01</code> 是手动测试用的多账号，可在操作列禁用不需要的。</div>';
    }
    let html = explanation + '<div class="scroll-y-300"><table><thead><tr><th>ID</th><th>账号名</th><th>状态</th><th>已用/总额</th><th>过期</th><th>Key</th><th>备注</th><th>操作</th></tr></thead><tbody>';
    for (const a of items) {
        const quota = a.quota_total
            ? a.quota_used.toLocaleString() + ' / ' + a.quota_total.toLocaleString() + ' (' + (a.quota_pct || 0) + '%)'
            : '-';
        const expires = a.expires_at ? a.expires_at.slice(0, 10) : '-';
        const notes = a.notes || '';
        const status = a.status;
        const actions = [];
        if (status !== "active") {
            actions.push('<button class="btn-sm btn-enable" data-action="enable" data-id="' + a.id + '">启用</button>');
        } else {
            actions.push('<button class="btn-sm btn-disable" data-action="disable" data-id="' + a.id + '">禁用</button>');
        }
        actions.push('<button class="btn-sm btn-rotate" data-action="rotate" data-id="' + a.id + '">轮换</button>');
        html += '<tr>'
            + '<td>' + a.id + '</td>'
            + '<td><code>' + a.provider + '/' + esc(a.account_name) + '</code></td>'
            + '<td class="status-' + status + '">' + status + '</td>'
            + '<td>' + quota + '</td>'
            + '<td>' + expires + '</td>'
            + '<td><code class="key-display">' + esc(a.key_display) + '</code></td>'
            + '<td class="notes">' + esc(notes) + '</td>'
            + '<td class="row-actions">' + actions.join(' ') + '</td>'
            + '</tr>';
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
            html += '<div class="scroll-y-260"><table><thead><tr><th>Model</th><th class="mono">Input</th><th class="mono">Cached</th><th class="mono">命中率</th><th class="mono">节省</th></tr></thead><tbody>';
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
document.addEventListener("DOMContentLoaded", loadPerUserUsage);
document.addEventListener("DOMContentLoaded", loadUserKeys);

// 60s 定时刷新 + 接管 htmx
setInterval(() => {
    loadUsageChart();
    loadCacheStatsList();
    loadSuggestionsList();
    loadPerUserUsage();
    loadUserKeys();
}, 60000);


// === 阶段P0-UI v3 (2026-08-16): 限额预警 + 修复之前漏的 ===
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
// === P0-UI v5: 周报 markdown 渲染 ===
// 周报 raw markdown → 用 marked.js 渲染成 HTML
// htmx 自动加载 + 我们需要拦截 raw markdown 转成 HTML

document.body.addEventListener('htmx:afterSwap', function(event) {
    if (event.detail.target.id === 'weekly-report-content') {
        try {
            const md = event.detail.target.textContent;
            // 检查是否含 markdown 标记
            if (md && (md.includes('# ') || md.includes('## ') || md.includes('### ') || md.includes('**'))) {
                // marked.js 全局函数
                if (typeof marked !== 'undefined') {
                    event.detail.target.innerHTML = marked.parse(md);
                }
            }
        } catch (e) {
            console.warn('weekly report render:', e);
        }
    }
});// === P0-UI v6: 账号管理 - data-action click 委托 (避免 onclick 嵌套引号) ===


// ============================================================
// V4.8 (2026-08-19): 按人用量 + Key 管理
// ============================================================

function fmtNum(n) {
    if (n === null || n === undefined) return '-';
    return Number(n).toLocaleString();
}

function fmtCost(n) {
    if (!n || n === 0) return '-';
    return '¥' + Number(n).toFixed(4);
}

async function loadPerUserUsage() {
    const container = document.getElementById('per-user-list');
    const meta = document.getElementById('per-user-meta');
    if (!container) return;
    try {
        const resp = await authFetch('/dashboard/api/stats/per-user?days=7');
        if (!resp.ok) {
            container.innerHTML = '<p class="error">加载失败: HTTP ' + resp.status + '</p>';
            return;
        }
        const data = await resp.json();
        const items = data.items || [];
        if (meta) meta.textContent = items.length + ' 个调用方';
        if (items.length === 0) {
            container.innerHTML = '<p class="empty">近 ' + data.days + ' 天无用量数据</p>';
            return;
        }
        let html = '<div class="scroll-y-300"><table><thead><tr>'
            + '<th>用户</th><th>请求数</th><th>Input</th><th>Output</th><th>总Token</th><th>估算费用</th>'
            + '</tr></thead><tbody>';
        for (const item of items) {
            const name = item.display_name
                ? esc(item.username) + ' (' + esc(item.display_name) + ')'
                : esc(item.username);
            html += '<tr>'
                + '<td>' + name + '</td>'
                + '<td>' + fmtNum(item.request_count) + '</td>'
                + '<td>' + fmtNum(item.input_tokens) + '</td>'
                + '<td>' + fmtNum(item.output_tokens) + '</td>'
                + '<td><strong>' + fmtNum(item.total_tokens) + '</strong></td>'
                + '<td>' + fmtCost(item.total_cost) + '</td>'
                + '</tr>';
        }
        html += '</tbody></table></div>';
        container.innerHTML = html;
    } catch (e) {
        container.innerHTML = '<p class="error">加载失败: ' + esc(e.message) + '</p>';
    }
}

async function loadUserKeys() {
    const container = document.getElementById('user-keys-list');
    const meta = document.getElementById('keys-meta');
    if (!container) return;
    try {
        const resp = await authFetch('/dashboard/api/keys');
        if (!resp.ok) {
            container.innerHTML = '<p class="error">加载失败: HTTP ' + resp.status + '</p>';
            return;
        }
        const data = await resp.json();
        const users = data.users || [];
        if (meta) meta.textContent = users.length + ' 个用户';
        if (users.length === 0) {
            container.innerHTML = '<p class="empty">无用户 — 用 <code>scripts/add_user_key.py</code> 创建</p>';
            return;
        }
        let html = '';
        for (const u of users) {
            const daily = u.daily_token_limit !== null ? fmtNum(u.daily_token_limit) : '不限';
            const monthly = u.monthly_token_limit !== null ? fmtNum(u.monthly_token_limit) : '不限';
            html += '<div class="user-key-block mt-sm">'
                + '<div class="user-key-header">'
                + '<strong>' + esc(u.username) + '</strong>'
                + (u.display_name ? ' (' + esc(u.display_name) + ')' : '')
                + ' <span class="chip status-' + u.status + '">' + u.status + '</span>'
                + ' <span class="chip">' + u.role + '</span>'
                + ' <span class="meta">日限:' + daily + ' 月限:' + monthly + '</span>'
                + '</div>';
            if (u.keys.length === 0) {
                html += '<p class="empty text-muted" style="font-size:12px;padding:4px 0;">无 Key</p>';
            } else {
                html += '<table><thead><tr>'
                    + '<th>前缀</th><th>Admin</th><th>状态</th><th>标签</th><th>最后使用</th><th>创建</th><th>操作</th>'
                    + '</tr></thead><tbody>';
                for (const k of u.keys) {
                    const lastUsed = k.last_used_at ? k.last_used_at.slice(0, 16).replace('T', ' ') : '-';
                    const created = k.created_at ? k.created_at.slice(0, 10) : '-';
                    const admin = k.is_admin ? '是' : '否';
                    let actions;
                    if (k.status === 'active') {
                        actions = '<button class="btn-sm btn-danger" data-action="revoke" data-id="' + k.id + '">吊销</button>';
                    } else {
                        actions = '<span class="text-muted">已吊销</span>';
                    }
                    html += '<tr>'
                        + '<td><code>' + esc(k.key_prefix) + '…</code></td>'
                        + '<td>' + admin + '</td>'
                        + '<td class="status-' + k.status + '">' + k.status + '</td>'
                        + '<td>' + esc(k.label || '-') + '</td>'
                        + '<td class="text-muted">' + lastUsed + '</td>'
                        + '<td class="text-muted">' + created + '</td>'
                        + '<td>' + actions + '</td>'
                        + '</tr>';
                }
                html += '</tbody></table>';
            }
            html += '</div>';
        }
        container.innerHTML = html;
    } catch (e) {
        container.innerHTML = '<p class="error">加载失败: ' + esc(e.message) + '</p>';
    }
}

async function revokeKey(keyId, btn) {
    if (!confirm('确定吊销 Key #' + keyId + '?\n吊销后该 Key 立即失效, 不可恢复 (需重新生成)。')) return;
    if (btn) btn.disabled = true;
    try {
        const resp = await authFetch('/dashboard/api/keys/' + keyId + '/revoke', { method: 'POST' });
        if (resp.ok) {
            await loadUserKeys();
        } else {
            const data = await resp.json().catch(() => ({}));
            alert('吊销失败: ' + (data.detail || resp.status));
            if (btn) btn.disabled = false;
        }
    } catch (e) {
        alert('吊销失败: ' + e.message);
        if (btn) btn.disabled = false;
    }
}

// === P0-UI v6: 账号管理 - data-action click 委托 (避免 onclick 嵌套引号) ===

document.addEventListener('click', (e) => {
    const btn = e.target.closest('button[data-action]');
    if (!btn) return;
    const action = btn.dataset.action;
    const id = parseInt(btn.dataset.id, 10);
    if (action === 'enable') {
        toggleAccount(id, 'active', btn);
    } else if (action === 'disable') {
        toggleAccount(id, 'disabled', btn);
    } else if (action === 'rotate') {
        rotateAccountKey(id, btn);
    } else if (action === 'revoke') {
        revokeKey(id, btn);
    }
});