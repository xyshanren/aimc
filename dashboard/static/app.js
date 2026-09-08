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

// Phase 3a (2026-09-05): htmx 移除 — 原 htmx:configRequest/responseError/afterSwap 三块删除,
// 轮询改由页面调度器驱动 (PAGE_ROUTES, 见文件末尾), JSON 卡走 loadJsonCard + 各 renderXxx。

function renderChannels(container) {
    try {
        const data = JSON.parse(container.textContent);
        if (!Array.isArray(data) || data.length === 0) {
            container.innerHTML = '<p class="loading">暂无渠道数据</p>';
            return;
        }
        let html = '<table><thead><tr><th>渠道</th><th>厂商</th><th>状态</th><th>账号</th><th>优先级</th><th>思考剥离</th><th>drop_params</th><th>操作</th></tr></thead><tbody>';
        data.forEach(ch => {
            const statusClass = `status-${esc(ch.status)}`;
            const dp = ch.drop_params ? esc(ch.drop_params) : '<span class="text-muted">—</span>';
            // V6.x: disable_thinking_pass_through — 1=请求侧剥离 thinking 字段 (默认开)
            const thinkingOn = ch.disable_thinking_pass_through !== 0 && ch.disable_thinking_pass_through !== false;
            const thinkingCell = `<button class="btn-tiny ${thinkingOn ? '' : 'btn-ghost'}" onclick="toggleChannelThinking(${esc(ch.id)}, ${thinkingOn ? 0 : 1})">${thinkingOn ? '开' : '关'}</button>`;
            html += `<tr data-channel-id="${esc(ch.id)}">
                <td>${esc(ch.name)}</td>
                <td>${esc(ch.provider)}</td>
                <td class="${statusClass}">${esc(ch.status)}</td>
                <td>${esc(ch.active_accounts)}/${esc(ch.accounts)}</td>
                <td>${esc(ch.priority)}</td>
                <td>${thinkingCell}</td>
                <td class="mono" style="font-size:11px;">${dp}</td>
                <td><button class="btn-tiny" onclick="editChannelDropParams(${esc(ch.id)}, ${JSON.stringify(ch.drop_params || '').replace(/"/g, '&quot;')})">编辑</button></td>
            </tr>`;
        });
        html += '</tbody></table>';
        html += '<div class="text-muted" style="margin-top:var(--space-xs);font-size:11px;">思考剥离: 关=透传客户端 thinking 参数给上游; drop_params: 逗号分隔的需剥离参数 (如 reasoning_effort,max_tokens); 留空=全透传; messages/model/stream 永远保留</div>';
        container.innerHTML = html;
    } catch (e) {
        // 非 JSON 响应，保持原样
    }
}

// V6.x: 思考参数剥离开关 (disable_thinking_pass_through 0/1 切换)
async function toggleChannelThinking(channelId, newVal) {
    try {
        const resp = await authFetch(`/dashboard/api/channels/${channelId}`, {
            method: 'PUT',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({disable_thinking_pass_through: newVal}),
        });
        const result = await resp.json();
        if (result.ok) {
            // 刷新渠道列表
            const list = document.getElementById('channels-list');
            if (list) {
                authFetch('/dashboard/api/channels').then(r => r.text()).then(t => {
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

async function editChannelDropParams(channelId, currentVal) {
    const newVal = prompt('drop_params (逗号分隔, 留空=全透传)\n例如: reasoning_effort,max_tokens', currentVal || '');
    if (newVal === null) return; // 用户取消
    try {
        const resp = await authFetch(`/dashboard/api/channels/${channelId}`, {
            method: 'PUT',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({drop_params: newVal.trim()}),
        });
        const result = await resp.json();
        if (result.ok) {
            // 刷新渠道列表
            const list = document.getElementById('channels-list');
            if (list) {
                authFetch('/dashboard/api/channels').then(r => r.text()).then(t => {
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

// 相对时间: ISO/datetime 字符串 → "x 分钟/小时/天 前"
function _relTime(iso) {
    if (!iso) return '';
    const t = new Date(iso.includes('T') ? iso : iso.replace(' ', 'T'));
    if (Number.isNaN(t.getTime())) return '';
    const diffMin = Math.round((Date.now() - t.getTime()) / 60000);
    if (diffMin < 1) return '刚刚';
    if (diffMin < 60) return diffMin + ' 分钟前';
    const h = Math.floor(diffMin / 60);
    if (h < 24) return h + ' 小时前';
    const d = Math.floor(h / 24);
    if (d < 30) return d + ' 天前';
    return Math.floor(d / 30) + ' 个月前';
}

function renderPromotions(container) {
    try {
        const data = JSON.parse(container.textContent);
        if (!Array.isArray(data) || data.length === 0) {
            container.innerHTML = '<p class="loading">暂无优惠情报</p>';
            return;
        }
        // 限高滚动: 最多 50 条卡片 (~90px/条) 会把页面顶得很长; 条数标注防"下面还有"的错觉
        let html = '<div class="text-muted" style="margin-bottom:var(--space-xs);font-size:11px;">共 ' + esc(data.length) + ' 条优惠 (按采集时间倒序, 滚动查看)</div>';
        html += '<div class="scroll-y-420">';
        data.forEach(p => {
            const rel = _relTime(p.created_at);
            html += `<div class="promo-item">
                <span class="provider">${esc(p.provider)}</span>
                <span class="discount">${esc(p.discount_desc)}</span>
                <div class="title">${esc(p.title)}</div>
                <div class="meta">采集: ${esc(rel) || '未知'} | 截止: ${esc(p.end_date) || '未知'} | 置信度: ${esc((p.confidence || 0).toFixed(2))}</div>
                ${p.url ? `<a class="promo-link" href="${esc(p.url)}" target="_blank" rel="noopener">查看原文 ↗</a>` : ''}
            </div>`;
        });
        html += '</div>';
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
        // 长表限高滚动 (不做翻页: 对比场景翻页打断视线), 行数标注防"下面还有"的错觉
        let html = '<div class="text-muted" style="margin-bottom:var(--space-xs);font-size:11px;">共 ' + esc(data.length) + ' 条价格记录 (滚动查看)</div>';
        html += '<div class="scroll-y-420 scroll-table"><table><thead><tr><th>模型</th><th>渠道</th><th>类型</th><th>时段</th><th>输入价</th><th>输出价</th></tr></thead><tbody>';
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
        html += '</tbody></table></div>';
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
            container.innerHTML = '<p class="loading">近 ' + fmtDays() + ' 天无组路由请求 · 所有请求都是精确模型名</p>';
            return;
        }

        let html = '';

        // 2) 每个 group 一个卡片
        for (const g of groups) {
            const dist = g.model_distribution || [];
            const channels = g.channels_used || [];
            html += `<div class="gr-group-card">
                <h4><code>${esc(g.group_id)}</code>
                    <span class="meta">${esc(g.request_count)} 次 · 估 ${fmtCNY(g.total_cost ?? 0)}</span>
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

// 指标统一 Phase 2 (2026-09-05): 组路由从纯 htmx 改为 JS loader
// - 请求带 ?days=fmtDays()，跟随全局时间范围（24h/7d/30d），原来固定后端默认 7 天
// - applyTimeRange 切换范围时会重拉；渲染仍复用 renderGroupRouting (JSON→HTML)
async function loadGroupRouting() {
    const container = document.getElementById("group-routing-list");
    if (!container) return;
    try {
        const resp = await authFetch(`/dashboard/api/stats/group_routing?days=${fmtDays()}`);
        if (!resp.ok) return;
        container.textContent = await resp.text();
        renderGroupRouting(container);
    } catch (e) { console.warn("group routing failed:", e); }
}

// Phase 4 E1: 待确认能力变更队列
async function approvePendingReview(logId, btn) {
    if (!confirm(`确定批准 log #${logId} ? Tier 会自动写入 ModelRegistry`)) return;
    try {
        const resp = await authFetch(`/dashboard/api/pending-review/${logId}/approve`, { method: 'POST' });
        const data = await resp.json();
        if (resp.ok) {
            const card = btn.closest('.pr-card');
            // V4.8.6: create 批准后, 如果仍无可用渠道, 提示用户
            if (data.field_changed === 'create' && data.channel_activated === false) {
                alert(`已批准, 但该模型仍无可用渠道 (status=inactive).\n请先在渠道管理中为该模型配置价格, 下次扫描会自动激活.`);
            }
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
        const resp = await authFetch(`/dashboard/api/pending-review/${logId}/reject`, { method: 'POST' });
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
            // V4.8.6: 渠道来源标签 (provider)
            const providerTag = item.provider
                ? `<span class="pr-provider" title="采集来源渠道">📡 ${esc(item.provider)}</span>`
                : '';
            // new_value 可能是 JSON 串 (field_changed=create/metadata), 格式化展示
            let newVal = item.new_value ?? '(空)';
            let oldVal = item.old_value ?? '(空)';
            // V4.8.6: 解析 new_value JSON, 检测 channel_available 标记
            let channelWarn = '';
            try {
                if (typeof newVal === 'string' && newVal.startsWith('{')) {
                    const obj = JSON.parse(newVal);
                    if (obj.channel_available === false) {
                        channelWarn = '<span class="pr-warn" title="当前无 active 渠道为该模型配过价格">⚠️ 无可用渠道</span>';
                    }
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
                    ${providerTag}
                    ${channelWarn}
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

// V4.8.4: 低置信度优惠审查队列
function renderPromotionReview(container) {
    try {
        const data = JSON.parse(container.textContent);
        const items = data.items || [];
        if (items.length === 0) {
            container.innerHTML = '<p class="loading">✓ 当前无待审低置信度优惠</p>';
            return;
        }
        let html = `<p class="meta">共 <strong>${esc(data.count)} 条待审</strong>，按时间倒序</p>`;
        for (const item of items) {
            const conf = item.confidence || 0;
            const confLabel = item.confidence_level || 'low';
            const created = item.created_at ? new Date(item.created_at).toLocaleString('zh-CN') : '?';
            html += `<div class="pr-card">
                <div class="pr-row">
                    <code class="pr-model">${esc(item.provider)}</code>
                    <span class="pr-display-name">${esc(item.title || '(无标题)')}</span>
                    <span class="pr-conf conf-${esc(confLabel)}">${esc(conf.toFixed(2))} · ${esc(confLabel)}</span>
                    <span class="pr-time">${esc(created)}</span>
                </div>
                ${item.summary ? `<div class="pr-summary">${esc(item.summary)}</div>` : ''}
                ${item.url ? `<div class="pr-summary"><a href="${esc(item.url)}" target="_blank">${esc(item.url)}</a></div>` : ''}
                <div class="pr-actions">
                    <button class="btn-approve" onclick="approvePromotionReview(${esc(item.id)}, this)">批准入库</button>
                    <button class="btn-reject" onclick="rejectPromotionReview(${esc(item.id)}, this)">拒绝</button>
                </div>
            </div>`;
        }
        container.innerHTML = html;
    } catch (e) {
        console.error('renderPromotionReview failed:', e);
        container.innerHTML = '<p class="loading">渲染失败: ' + esc(e.message) + '</p>';
    }
}

async function approvePromotionReview(reviewId, btn) {
    if (!confirm(`确定批准 #${reviewId} 入库? 将写入 Promotion 表`)) return;
    try {
        const resp = await authFetch(`/dashboard/api/promotion-reviews/${reviewId}/approve`, { method: 'POST' });
        const data = await resp.json();
        if (resp.ok) {
            const card = btn.closest('.pr-card');
            if (card) card.remove();
        } else {
            alert('批准失败: ' + (data.detail || resp.statusText));
        }
    } catch (e) {
        alert('网络错误: ' + e.message);
    }
}

async function rejectPromotionReview(reviewId, btn) {
    if (!confirm(`确定拒绝 #${reviewId}? 不入库, 记录保留`)) return;
    try {
        const resp = await authFetch(`/dashboard/api/promotion-reviews/${reviewId}/reject`, { method: 'POST' });
        const data = await resp.json();
        if (resp.ok) {
            const card = btn.closest('.pr-card');
            if (card) card.remove();
        } else {
            alert('拒绝失败: ' + (data.detail || resp.statusText));
        }
    } catch (e) {
        alert('网络错误: ' + e.message);
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
        const resp = await authFetch(`/dashboard/api/cross-source/discrepancies/${discrepancyId}/resolve`, {
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
        const resp = await authFetch('/dashboard/api/cross-source/run-detection', { method: 'POST' });
        const data = await resp.json();
        if (resp.ok) {
            result.textContent = `本次: ${data.total_models} 个 model · ${data.with_discrepancy} 个分歧 · ${data.recorded} 条入库`;
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

// 阶段1/2 (2026-08-14) 死代码已移除: loadDataFreshness / loadCacheStats / 旧版 loadSuggestions
// 指向不存在的 df-* / cache-* / sg-* IDs, 现役实现为 loadDataFreshnessDonut /
// loadCacheStatsList / loadSuggestionsList (见下方)。
// 方案A (2026-08-14): 模型分组建议 — 批准/拒绝/一键应用动作保留, 列表由 loadSuggestionsList 渲染
async function approveSuggestion(type, id) {
    if (!confirm(`批准此建议? (类型=${type}, id=${id})\n批准后下次 "一键应用" 会真正改 db`)) return;
    const resp = await authFetch(`/dashboard/api/suggestions/${type}/${id}/approve`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({reviewed_by: 'dashboard_user'}),
    });
    if (resp.ok) {
        alert('已批准');
        loadSuggestionsList();
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
        loadSuggestionsList();
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
        loadSuggestionsList();
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
        const resp = await authFetch('/dashboard/api/reports');
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

function renderReportHTML(content, html) {
    // V4.8.6: 服务端返回 <pre class='report-md'> 包裹的原始 markdown,
    // 剥离包裹后统一走 marked.parse (loadWeeklyReport / loadWeeklyReportLatest 共用)
    let md = html;
    if (html.includes("<pre class='report-md'>")) {
        const doc = new DOMParser().parseFromString(html, 'text/html');
        md = (doc.querySelector('pre.report-md') || {}).textContent || html;
    }
    if (typeof marked !== 'undefined' && md &&
        (md.includes('# ') || md.includes('## ') || md.includes('### ') || md.includes('**'))) {
        content.innerHTML = marked.parse(md);
    } else {
        content.innerHTML = md ? `<pre class="report-md">${esc(md)}</pre>` : '<p class="error">报告内容为空</p>';
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
    authFetch(url)
        .then(r => {
            if (!r.ok) throw new Error(`HTTP ${r.status}`);
            return r.text();
        })
        .then(html => {
            renderReportHTML(content, html);
        })
        .catch(e => {
            content.innerHTML = `<p class="error">加载周报失败: ${esc(e.message)}</p>`;
        });
}

// (loadReportsList 由情报页调度器拉起, 不再挂全局 DOMContentLoaded — Phase 3a)

// ============================================================
// V4.6: Dashboard 账号管理 (列表 / 加 / 启停 / 轮换 / 过期扫描)
// 与 dashboard/routes.py /api/accounts/* 配套
// ============================================================

async function loadActiveChannels() {
    // 填充加账号表单的 provider select
    const sel = document.getElementById('acct-provider');
    if (!sel) return;
    try {
        const resp = await authFetch('/dashboard/api/channels/active');
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
        const resp = await authFetch('/dashboard/api/accounts');
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
    // 限额预警 (原独立卡, 2026-09-05 并入): 非 active 或配额>=80% 计入"需关注"
    const QUOTA_WARN_PCT = 80;
    const needsAttention = (a) => (a.status && a.status !== 'active') || (a.quota_pct != null && a.quota_pct >= QUOTA_WARN_PCT);
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
    const warnedCount = items.filter(needsAttention).length;
    if (warnedCount > 0) {
        explanation += '<div class="mt-sm" style="font-size:12px;color:var(--color-warning);">⚠️ ' + warnedCount + ' 个账号需关注 (非 active 或配额 ≥' + QUOTA_WARN_PCT + '%，已置顶)</div>';
    }
    // 非 active 置顶 (expired/error 最前), 其余保持 id 序
    const sev = (s) => (s === 'expired' || s === 'error' ? 0 : s === 'exhausted' ? 1 : s && s !== 'active' ? 2 : 3);
    const sorted = items.slice().sort((x, y) => sev(x.status) - sev(y.status));
    let html = explanation + '<div class="scroll-y-300"><table><thead><tr><th>ID</th><th>账号名</th><th>状态</th><th>已用/总额</th><th>过期</th><th>Key</th><th>备注</th><th>操作</th></tr></thead><tbody>';
    for (const a of sorted) {
        const quota = a.quota_total
            ? a.quota_used.toLocaleString() + ' / ' + a.quota_total.toLocaleString() + ' (' + (a.quota_pct || 0) + '%)'
            : '-';
        const quotaHot = a.quota_pct != null && a.quota_pct >= QUOTA_WARN_PCT;
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
            + '<td' + (quotaHot ? ' style="color:var(--color-warning);font-weight:600;"' : '') + '>' + quota + '</td>'
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
        const resp = await authFetch(`/dashboard/api/accounts/${accountId}/${newStatus === 'active' ? 'enable' : 'disable'}`, {
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
        const resp = await authFetch(`/dashboard/api/accounts/${accountId}/rotate`, {
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
        const resp = await authFetch('/dashboard/api/accounts', {
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
        const resp = await authFetch('/dashboard/api/accounts/expire-check', { method: 'POST' });
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

// (htmx accounts-list afterSwap 已随 htmx 移除 — loadAccounts 自带渲染, Phase 3a)

// 页面加载时拉 active channels 填表单


// 阶段P0-UI (2026-08-16): dashboard redesign JS
// - loadOverviewKPIs: 顶部 4 KPI 卡片 + status bar
// - filterChannels/filterRegistry: 搜索过滤
// - loadDataFreshnessDonut: 替换旧表格为 donut + 失败列表
// - keydown listener: r / a / / / ? 快捷键

async function loadOverviewKPIs() {
    try {
        const [cacheResp, usageResp] = await Promise.all([
            authFetch(`/dashboard/api/cache/stats?days=${fmtDays()}`),
            authFetch(`/dashboard/api/usage/summary?days=${fmtDays()}`),
        ]);
        const cache = cacheResp.ok ? await cacheResp.json() : {by_channel: [], hit_rate_pct: 0, saved_cost_cny: 0};
        const usage = usageResp.ok ? await usageResp.json() : [];
        const totalInput = (usage || []).reduce((s, r) => s + (r.total_input_tokens || 0), 0);
        const totalReq = (usage || []).reduce((s, r) => s + (r.request_count || 0), 0);
        const set = (id, v) => { const e = document.getElementById(id); if (e) e.textContent = v; };
        set("kpi-tokens", fmtTokens(totalInput));
        set("kpi-requests", fmtNum(totalReq));
        set("kpi-cache-rate", fmtPct(cache.hit_rate_pct, totalInput > 0));
        set("kpi-saved", fmtCNY(cache.saved_cost_cny));
        // 指标统一 (2026-09-05): 状态栏不再展示业务金额/比率 — Cache 三指标只在 KPI 卡出现,
        // 避免与 Cache 节省明细/成本审计同屏双入口歧义; 状态栏文案由 syncRangeLabels 维护
    } catch (e) { console.warn("Overview KPIs failed:", e); }
}

function filterChannels() {
    const input = document.getElementById("search-channels");
    const q = input ? input.value : "";
    // 只过滤 tbody: thead 行不含查询词, 一并过滤会把表头藏掉; 价格表同卡, 搜索一并覆盖
    document.querySelectorAll("#channels-list tbody tr, #prices-list tbody tr").forEach(tr => {
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

// Phase 3a: 唯一启动入口 — 时间范围还原 → 页面调度器 → 顶栏 badge 常驻轮询
// (原 8 处 DOMContentLoaded + 3 处 setInterval 全部收编进 PAGE_ROUTES 调度器, 见文件末尾)
document.addEventListener("DOMContentLoaded", () => {
    initTimeRange();
    initNav();
    refreshBadges();
    setInterval(refreshBadges, 30000);
});

// === 阶段P0-UI v2 (2026-08-16): 修复旧函数死引用 + 接管 htmx 自动渲染 ===

// (a) 删除旧的 DOMContentLoaded hooks - 它们指向不存在 IDs 的元素
//   - loadDataFreshness (line 507) -> df-total/df-healthy 不存在
//   - loadSuggestions (line 636) -> sg-pending/sg-approved/sg-applied 不存在
//   - loadCacheStats (line 542) -> cache-saved/cache-tokens/cache-hit-pct 不存在; cache-by-channel / cache-by-model 也不存在
//   - loadUsageChart (line 587) -> usage-chart canvas 不存在
// 替代: 我们直接 fetch + 渲染到新 IDs (kpi-* / cache-by-channel / usage-list / suggestions-list / group-routing-list / channels-list)

// (b) 新 loadUsageChart 替代 (渲染 usage-list 表格 + 每日请求柱图)
let _usageBarChart = null;

// V6.3 F7 图-表联动: 双向 hover 高亮 — 图 hover 高亮表行, 表行 hover 高亮图元素。
// chart.setActiveElements 为 Chart.js 4.4.0 内置, 纯前端 class 切换, 无新请求。
function _chartTableLink(chart, tableEl) {
    if (!chart || !tableEl) return;
    const rows = tableEl.querySelectorAll('tbody tr');
    const highlightRow = (idx) => {
        rows.forEach((r, i) => r.classList.toggle('hl', i === idx));
    };
    chart.options.onHover = (evt, elements) => {
        const idx = elements.length ? elements[0].index : -1;
        highlightRow(idx);
        // 离开图表区域时清除表高亮 (elements 为空即未命中任何数据元素)
        if (idx === -1) chart.setActiveElements([]);
    };
    rows.forEach((row, i) => {
        row.addEventListener('mouseenter', () => {
            if (i >= chart.data.labels.length) return;
            chart.setActiveElements([{ datasetIndex: 0, index: i }]);
            chart.tooltip.setActiveElements([{ datasetIndex: 0, index: i }], {
                x: (row.getBoundingClientRect().left || 0),
                y: (row.getBoundingClientRect().top || 0),
            });
            chart.update('none');
        });
        row.addEventListener('mouseleave', () => {
            chart.setActiveElements([]);
            chart.tooltip.setActiveElements([], { x: 0, y: 0 });
            chart.update('none');
        });
    });
}

async function loadUsageChart() {
    try {
        const resp = await authFetch(`/dashboard/api/usage/summary?days=${fmtDays()}`);
        if (!resp.ok) return;
        const data = await resp.json();
        const list = document.getElementById("usage-list");
        if (!list) return;
        if (!data || data.length === 0) {
            list.innerHTML = '<div class="empty">暂无用量数据</div>';
            if (_usageBarChart) { _usageBarChart.destroy(); _usageBarChart = null; }
            return;
        }
        // 每日请求柱图 (Phase 3 数据表现形式: 时间序列用柱, 明细用表)
        const barCanvas = document.getElementById("usage-bar-chart");
        if (barCanvas) {
            if (_usageBarChart) _usageBarChart.destroy();
            _usageBarChart = new Chart(barCanvas, {
                type: 'bar',
                data: {
                    labels: data.map(d => (d.date || '').slice(5)),
                    datasets: [{
                        label: '请求数',
                        data: data.map(d => d.request_count || 0),
                        backgroundColor: _cssColor('--color-info', 0.7),
                        borderRadius: 3,
                    }]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: { legend: { display: false } },
                    scales: {
                        x: { ticks: { color: _cssVar('--text-muted') }, grid: { display: false } },
                        y: { ticks: { color: _cssVar('--text-muted') }, grid: { color: _cssVar('--border-muted') }, beginAtZero: true }
                    }
                }
            });
        }
        // V6.x: 思考列 = reasoning_tokens 合计 (NULL 行不参与 SUM, 0=上游未返回)
        let html = '<table><thead><tr><th>日期</th><th>请求数</th><th class="mono">Input</th><th class="mono">Output</th><th class="mono">思考</th><th class="mono">平均延迟</th></tr></thead><tbody>';
        for (const r of data) {
            html += `<tr><td>${esc(r.date || '—')}</td><td>${r.request_count || 0}</td><td class="mono">${fmtTokens(r.total_input_tokens || 0)}</td><td class="mono">${fmtTokens(r.total_output_tokens || 0)}</td><td class="mono">${r.total_reasoning_tokens ? fmtTokens(r.total_reasoning_tokens) : '—'}</td><td class="mono">${r.avg_latency_ms ? fmtLatency(r.avg_latency_ms) : '—'}</td></tr>`;
        }
        html += '</tbody></table>';
        list.innerHTML = html;
        // V6.3 F7: 柱图↔日表双向 hover 联动
        _chartTableLink(_usageBarChart, list);
    } catch (e) { console.warn("usage chart failed:", e); }
}

// (c) 新 loadCacheStatsList 渲染 cache-by-channel 表格 (替代 htmx 自动渲染) + 节省分布环图
let _cacheDonutChart = null;

// 图表多系列调色板 (语义色 + 辅助灰, 循环取用)
const _CHART_PALETTE = ['--chart-1', '--chart-2', '--chart-3', '--chart-4', '--chart-5', '--chart-6'];

function _paletteColor(i, alpha) {
    return _cssColor(_CHART_PALETTE[i % _CHART_PALETTE.length], alpha);
}

async function loadCacheStatsList() {
    try {
        const resp = await authFetch(`/dashboard/api/cache/stats?days=${fmtDays()}`);
        if (!resp.ok) return;
        const data = await resp.json();
        const container = document.getElementById("cache-by-channel");
        if (!container) return;
        // 节省分布环图 (Phase 3 数据表现形式: 构成占比用环图)
        const donutCanvas = document.getElementById("cache-donut-chart");
        const byChannel = data.by_channel || [];
        if (donutCanvas && byChannel.length > 0) {
            if (_cacheDonutChart) _cacheDonutChart.destroy();
            _cacheDonutChart = new Chart(donutCanvas, {
                type: 'doughnut',
                data: {
                    labels: byChannel.map(c => c.channel_name || '—'),
                    datasets: [{
                        data: byChannel.map(c => Math.round((c.saved_cost_cny || 0) * 100) / 100),
                        backgroundColor: byChannel.map((c, i) => _paletteColor(i, 0.75)),
                        borderWidth: 0,
                    }]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {
                        legend: { position: 'right', labels: { color: _cssVar('--text-secondary') } },
                        tooltip: { callbacks: { label: (ctx) => `${ctx.label}: ${fmtCNY(ctx.parsed)}` } }
                    }
                }
            });
        } else if (donutCanvas && _cacheDonutChart) {
            _cacheDonutChart.destroy();
            _cacheDonutChart = null;
        }
        let html = '';
        // 按渠道
        if (data.by_channel && data.by_channel.length > 0) {
            html += '<h3 style="font-size:11px;color:var(--text-muted);text-transform:uppercase;margin-bottom:8px;">按渠道</h3>';
            html += '<table><thead><tr><th>渠道</th><th class="mono">Input</th><th class="mono">Cached</th><th class="mono"' + mdTitle('cacheHit') + '>Cache 命中率</th><th class="mono"' + mdTitle('cacheSaved') + '>Cache 节省</th></tr></thead><tbody>';
            for (const c of data.by_channel) {
                html += '<tr>';
                html += `<td>${esc(c.channel_name || '—')}</td>`;
                html += `<td class="mono">${fmtTokens(c.input_tokens || 0)}</td>`;
                html += `<td class="mono">${fmtTokens(c.cached_tokens || 0)}</td>`;
                html += `<td class="mono">${fmtPct(c.hit_rate_pct, (c.input_tokens || 0) > 0)}</td>`;
                html += `<td class="mono">${fmtCNY(c.saved_cost_cny)}</td>`;
                html += '</tr>';
            }
            html += '</tbody></table>';
        }
        // TOP 10 model
        if (data.by_model && data.by_model.length > 0) {
            html += '<h3 style="font-size:11px;color:var(--text-muted);text-transform:uppercase;margin:16px 0 8px;">TOP 10 Model</h3>';
            html += '<div class="scroll-y-260"><table><thead><tr><th>Model</th><th class="mono">Input</th><th class="mono">Cached</th><th class="mono"' + mdTitle('cacheHit') + '>Cache 命中率</th><th class="mono"' + mdTitle('cacheSaved') + '>Cache 节省</th></tr></thead><tbody>';
            for (const m of data.by_model.slice(0, 10)) {
                html += '<tr>';
                html += `<td><code class="text-mono">${esc(m.model || '—')}</code></td>`;
                html += `<td class="mono">${fmtTokens(m.input_tokens || 0)}</td>`;
                html += `<td class="mono">${fmtTokens(m.cached_tokens || 0)}</td>`;
                html += `<td class="mono">${fmtPct(m.hit_rate_pct, (m.input_tokens || 0) > 0)}</td>`;
                html += `<td class="mono">${fmtCNY(m.saved_cost_cny)}</td>`;
                html += '</tr>';
            }
            html += '</tbody></table>';
        }
        if (!html) html = '<div class="empty">暂无 Cache 数据</div>';
        container.innerHTML = html;
        // V6.3 F7: 环图↔渠道表双向 hover 联动 (表 = container 第一个 table)
        const chTable = container.querySelector('table');
        if (chTable) _chartTableLink(_cacheDonutChart, chTable);
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

// (启动钩子与 60s 全局轮询已由 PAGE_ROUTES 页面调度器接管 — Phase 3a, 见文件末尾)


// (限额预警已并入账号管理卡 (2026-09-05): 需关注计数 + 非active置顶 + 配额>=80% 标黄,
//  见 renderAccounts; 原 loadQuotaWarning 删除, 同源 /api/accounts 不再每分钟重复拉取)
// === P0-UI v5: 周报 markdown 渲染 ===
// (原 htmx:afterSwap 拦截已随 htmx 移除; markdown 渲染并入 loadWeeklyReportLatest — Phase 3a)

async function loadWeeklyReportLatest() {
    const content = document.getElementById('weekly-report-content');
    if (!content) return;
    try {
        const resp = await authFetch('/dashboard/api/reports/latest');
        if (!resp.ok) return;
        const md = await resp.text();
        // V4.8.6 fix: 默认首载路径此前未剥离 <pre class='report-md'> 包裹,
        // marked.parse 收到整段 HTML 后 pre 块原样透出 → markdown 不渲染
        renderReportHTML(content, md);
    } catch (e) {
        console.warn('weekly report latest failed:', e);
    }
}// === P0-UI v6: 账号管理 - data-action click 委托 (避免 onclick 嵌套引号) ===


// ============================================================
// V4.8 (2026-08-19): 按人用量 + Key 管理
// ============================================================

function fmtNum(n) {
    if (n === null || n === undefined) return '-';
    return Number(n).toLocaleString();
}

// P1-2: 全看板统一货币格式
// - null/undefined/NaN → '-'；0 → ¥0.00（原各处不一致: '-' / ¥0.0000）
// - |n|<0.01 → <¥0.01，避免 toFixed(2) 把微小节省错显成 ¥0.00
function fmtCNY(n) {
    if (n === null || n === undefined || Number.isNaN(Number(n))) return '-';
    const v = Number(n);
    if (v < 0) return '-' + fmtCNY(-v);
    if (v === 0) return '¥0.00';
    if (v < 0.01) return '<¥0.01';
    return '¥' + v.toFixed(2);
}

// ============================================================
// 指标体系统一 (docs/dashboard-metrics-plan.md §2.2, 2026-09-05)
// - 所有 fmt 函数集中于此, 渲染代码禁止手拼 ¥ / % / toFixed
// - METRIC_DEFS 是指标字典: 显示名 + 口径一句话, 表头 tooltip 从这里取
// ============================================================

// 比率: 1 位小数; valid=false (分母为 0/无数据) 显 — 而非 0.0%
function fmtPct(v, valid) {
    if (valid === false || v === null || v === undefined || Number.isNaN(Number(v))) return '—';
    return Number(v).toFixed(1) + '%';
}

// Token: 大数 K/M 缩写 (LLM 领域惯用阶梯), 小数原值; 表格需要精确值时仍可用 fmtNum
function fmtTokens(n) {
    if (n === null || n === undefined || Number.isNaN(Number(n))) return '-';
    const v = Number(n);
    const neg = v < 0 ? '-' : '';
    const a = Math.abs(v);
    if (a >= 1e6) return neg + (a / 1e6).toFixed(1) + 'M';
    if (a >= 1e3) return neg + (a / 1e3).toFixed(1) + 'K';
    return String(v);
}

// 延迟: <1s 显 ms, ≥1s 显 s
function fmtLatency(ms) {
    if (ms === null || ms === undefined || Number.isNaN(Number(ms))) return '—';
    const v = Number(ms);
    if (v >= 1000) return (v / 1000).toFixed(1) + ' s';
    return v.toFixed(0) + ' ms';
}

// 指标字典: key → { label 显示名, desc 口径一句话 (表头 title tooltip) }
const METRIC_DEFS = {
    tokens:       { label: 'Token',       desc: '模型输入+输出 token 总量' },
    requests:     { label: '请求数',      desc: '计入统计的请求次数' },
    cacheHit:     { label: 'Cache 命中率', desc: 'cached_tokens / input_tokens，按当前统计窗口聚合' },
    cacheSaved:   { label: 'Cache 节省',  desc: '缓存命中相对未缓存标价省下的金额 (saved_cost_cny)，与成本审计的「标价节省」口径不同' },
    paid:         { label: '实付',        desc: '实际计费成本 (cost_yuan)' },
    listPrice:    { label: '官方标价',    desc: '按厂商公开标价估算的成本 (list_cost_yuan)' },
    listSaved:    { label: '标价节省',    desc: '官方标价 − 实付，只统计标价已知的请求；与 Cache 节省口径不同' },
    coverage:     { label: '标价覆盖率',  desc: '标价已知请求 / 总请求，覆盖率低时标价节省偏保守' },
    routeSuccess: { label: '路由成功率',  desc: '按分组路由的匹配情况 (Agent 视角)，非 Cache 命中率' },
    latency:      { label: '平均延迟',    desc: '请求端到端耗时的日均值' },
};

// 生成 <th> 的 title tooltip 属性
function mdTitle(key) {
    const def = METRIC_DEFS[key];
    return def ? ` title="${esc(def.desc)}"` : '';
}

// ============================================================
// P1-1: 全局时间范围（24h / 7d / 30d）
// - 持久化：localStorage + URL ?range= 双向（URL 优先，便于分享/刷新还原）
// - 所有 ?days=N 的 API 调用统一走 fmtDays()
// ============================================================

let _timeRange = "7"; // "1" | "7" | "30"

function rangeLabel(r) {
    return { "1": "近 24 小时", "7": "近 7 天", "30": "近 30 天" }[r] || "近 7 天";
}

// 卡片标题副标签与当前时间范围保持同步（index.html 各 card-title-sub）
function syncRangeLabels() {
    const lbl = rangeLabel(_timeRange);
    const short = { "1": "24 小时", "7": "7 天", "30": "30 天" }[_timeRange] || "7 天";
    const map = [
        ["overview-range-label", short],
        ["cost-range-label", "标价 vs 实付 · " + lbl],
        ["cache-range-label", lbl],
        ["group-routing-range-label", "Agent 视角 · " + lbl],
        ["requests-range-label", lbl],
        ["trend-range-label", lbl],
        ["top-range-label", lbl],
        ["group-overview-label", lbl],
    ];
    for (const [id, text] of map) {
        const el = document.getElementById(id);
        if (el) el.textContent = text;
    }
}

function applyTimeRange(r, fromUrl, skipRefresh) {
    if (!["1", "7", "30"].includes(r)) return;
    _timeRange = r;
    try { localStorage.setItem("aimc_time_range", r); } catch (e) { /* ignore */ }
    if (!fromUrl) {
        try {
            const url = new URL(location.href);
            url.searchParams.set("range", r);
            history.replaceState(null, "", url.toString());
        } catch (e) { /* ignore */ }
    }
    const btns = document.querySelectorAll(".range-btn");
    btns.forEach((b) => b.classList.toggle("active", b.dataset.range === r));
    syncRangeLabels();
    if (skipRefresh) return; // 初始化时由常规 DOMContentLoaded 加载，避免重复请求
    // 重新拉当前页所有随时间范围变化的数据
    if (_currentPage) showPage(_currentPage);
}

function initTimeRange() {
    // URL 优先 → localStorage → 默认 7 天
    const urlRange = new URLSearchParams(location.search).get("range");
    let stored = null;
    try { stored = localStorage.getItem("aimc_time_range"); } catch (e) { /* ignore */ }
    const r = urlRange && ["1", "7", "30"].includes(urlRange) ? urlRange : stored;
    // skipRefresh=true: 本次只还原状态，数据由紧随其后的常规 DOMContentLoaded 加载拉取
    if (r) applyTimeRange(r, !urlRange, true);
    syncRangeLabels(); // 无存储范围时也要初始化状态栏/副标签（默认 7 天）
    document.querySelectorAll(".range-btn").forEach((b) => {
        b.classList.toggle("active", b.dataset.range === _timeRange);
        b.addEventListener("click", () => applyTimeRange(b.dataset.range, false));
    });
}

function fmtDays() {
    return Number(_timeRange) || 7;
}

// (V6.1+: 原右栏"按人用量"卡已并入成本审计"按用户" tab — renderCostTable user 分支)

function toggleKeyDetail(row) {
    const detail = row.nextElementSibling;
    if (detail && detail.classList.contains('key-detail-row')) {
        const icon = row.querySelector('.expand-icon');
        if (detail.style.display === 'none') {
            detail.style.display = '';
            if (icon) icon.textContent = '▾';
        } else {
            detail.style.display = 'none';
            if (icon) icon.textContent = '▸';
        }
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
    const provider = btn.dataset.provider;
    if (action === 'enable') {
        toggleAccount(id, 'active', btn);
    } else if (action === 'disable') {
        toggleAccount(id, 'disabled', btn);
    } else if (action === 'rotate') {
        rotateAccountKey(id, btn);
    } else if (action === 'revoke') {
        revokeKey(id, btn);
    } else if (action === 'src-run-now') {
        runSourceNow(provider, btn);
    } else if (action === 'src-toggle') {
        toggleSource(provider, btn);
    }
});

// === V4.8.3: 采集源管理 ===

async function loadSources() {
    const container = document.getElementById('sources-list');
    const meta = document.getElementById('sources-meta');
    if (container) container.innerHTML = '<p class="loading">加载中…</p>';
    try {
        const resp = await authFetch('/dashboard/api/sources');
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        const data = await resp.json();
        if (meta) meta.textContent = `共 ${data.count} 个源`;

        if (!data.sources || data.sources.length === 0) {
            container.innerHTML = '<p class="text-muted">无采集源</p>';
            return;
        }

        // 按 provider 分组
        const groups = {};
        for (const s of data.sources) {
            if (!groups[s.provider]) groups[s.provider] = [];
            groups[s.provider].push(s);
        }

        let html = '<table class="data-table" style="width:100%;font-size:12px;"><thead><tr>'
            + '<th>Provider</th><th>类型</th><th>间隔</th><th>状态</th>'
            + '<th>最后检查</th><th>错误</th><th>无变化</th><th>操作</th>'
            + '</tr></thead><tbody>';
        for (const s of data.sources) {
            const intervalMin = Math.round(s.poll_interval / 60);
            const intervalStr = intervalMin >= 60
                ? (intervalMin / 60).toFixed(intervalMin % 60 === 0 ? 0 : 1) + 'h'
                : intervalMin + 'm';
            const statusClass = s.enabled
                ? (s.status === 'error' ? 'status-error' : 'status-active')
                : 'status-disabled';
            const statusText = s.enabled ? s.status : 'disabled';
            const lastCheck = s.last_check_at
                ? s.last_check_at.slice(5, 16).replace('T', ' ')
                : '-';
            const noChangeBadge = s.consecutive_no_change >= 4
                ? `<span class="badge badge-warn">${s.consecutive_no_change}</span>`
                : s.consecutive_no_change;
            const apiBadge = s.api_endpoint_configured
                ? '<span class="text-muted" title="API 结构化抽取">API</span>'
                : '<span class="text-muted" title="HTML 抓取">HTML</span>';
            const toggleBtn = s.enabled
                ? `<button class="btn-sm btn-disable" data-action="src-toggle" data-provider="${esc(s.provider)}">禁用</button>`
                : `<button class="btn-sm btn-enable" data-action="src-toggle" data-provider="${esc(s.provider)}">启用</button>`;
            const runBtn = `<button class="btn-sm" data-action="src-run-now" data-provider="${esc(s.provider)}">采集</button>`;
            html += '<tr>'
                + '<td><code>' + esc(s.provider) + '</code></td>'
                + '<td>' + esc(s.source_type) + ' ' + apiBadge + '</td>'
                + '<td>' + intervalStr + '</td>'
                + '<td class="' + statusClass + '">' + esc(statusText) + '</td>'
                + '<td class="text-muted">' + lastCheck + '</td>'
                + '<td>' + (s.error_count || 0) + '</td>'
                + '<td>' + noChangeBadge + '</td>'
                + '<td>' + runBtn + ' ' + toggleBtn + '</td>'
                + '</tr>';
        }
        html += '</tbody></table>';
        container.innerHTML = html;
    } catch (e) {
        if (container) container.innerHTML = '<p class="error">加载失败: ' + esc(e.message) + '</p>';
    }
}

async function runSourceNow(provider, btn) {
    if (btn) btn.disabled = true;
    try {
        const resp = await authFetch(
            '/dashboard/api/sources/run-now?provider=' + encodeURIComponent(provider),
            { method: 'POST' }
        );
        const data = await resp.json().catch(() => ({}));
        if (resp.ok) {
            if (btn) {
                btn.textContent = '已触发';
                // sources 卡已是 once 语义, 触发后 2s 手动重拉展示采集状态 (采集为异步, last_run 可能仍滞后)
                setTimeout(() => { btn.textContent = '采集'; btn.disabled = false; loadSources(); }, 2000);
            }
        } else {
            alert('触发失败: ' + (data.detail || resp.status));
            if (btn) btn.disabled = false;
        }
    } catch (e) {
        alert('触发失败: ' + e.message);
        if (btn) btn.disabled = false;
    }
}

async function toggleSource(provider, btn) {
    if (btn) btn.disabled = true;
    try {
        const resp = await authFetch(
            '/dashboard/api/sources/' + encodeURIComponent(provider) + '/toggle',
            { method: 'POST' }
        );
        const data = await resp.json().catch(() => ({}));
        if (resp.ok) {
            await loadSources();
        } else {
            alert('操作失败: ' + (data.detail || resp.status));
            if (btn) btn.disabled = false;
        }
    } catch (e) {
        alert('操作失败: ' + e.message);
        if (btn) btn.disabled = false;
    }
}

// === V4.9 (2026-08-25): 待处理 tab ===

async function loadPendingSummary() {
    const container = document.getElementById('pending-list');
    const meta = document.getElementById('pending-meta');
    const badge = document.getElementById('pending-total');
    if (!container) return;
    try {
        const resp = await authFetch('/dashboard/api/pending-summary');
        if (!resp.ok) {
            container.innerHTML = '<p class="error">加载失败: HTTP ' + resp.status + '</p>';
            return;
        }
        const data = await resp.json();
        const total = data.total || 0;
        if (badge) badge.textContent = total;
        if (meta) meta.textContent = total > 0 ? '共 ' + total + ' 项待处理' : '全部正常';
        const items = data.items || [];
        if (total === 0) {
            container.innerHTML = '<div class="text-success" style="padding:8px 0;font-size:12px;">✓ 当前无待处理项</div>';
            return;
        }
        let html = '<div class="flex-col gap-sm">';
        for (const item of items) {
            if (item.count === 0) continue;
            const color = item.severity === 'danger' ? 'var(--color-danger)'
                        : item.severity === 'warning' ? 'var(--color-warning)'
                        : 'var(--text-muted)';
            html += '<div class="pr-card">';
            html += '<div class="pr-row">';
            html += '<span class="status-dot-small" style="background:' + color + ';"></span>';
            html += '<strong>' + esc(item.label) + '</strong>';
            html += '<span class="badge badge-danger" style="margin-left:auto;">' + item.count + '</span>';
            html += '</div>';
            if (item.examples && item.examples.length > 0) {
                for (const ex of item.examples) {
                    const aid = ex.account_id;
                    const ch = ex.channel || '?';
                    const msg = ex.error_msg || '';
                    html += '<div style="font-size:11px;margin-top:6px;color:var(--text-secondary);">';
                    html += '· #' + aid + ' ' + esc(ch) + ': ' + esc(msg);
                    html += '</div>';
                    if (item.category && item.category.startsWith('needs_human')) {
                        html += '<div class="pending-actions">';
                        html += '<button class="btn btn-sm btn-enable" onclick="ackRecover(' + aid + ', this)">确认恢复</button>';
                        html += '<button class="btn btn-sm btn-disable" onclick="manualDisable(' + aid + ', this)">禁用</button>';
                        html += '</div>';
                    }
                }
            }
            html += '</div>';
        }
        html += '</div>';
        container.innerHTML = html;
    } catch (e) {
        container.innerHTML = '<p class="error">加载失败: ' + esc(e.message) + '</p>';
    }
}

// V6.3.1: 24h 瞬时冷却事件历史 (抓 badge 竞态的"现场")
async function loadCooldownEvents() {
    const container = document.getElementById('cooldown-events-list');
    const badge = document.getElementById('cooldown-events-count');
    if (!container) return;
    try {
        const resp = await authFetch('/dashboard/api/cooldown-events?hours=24&limit=100');
        if (!resp.ok) {
            container.innerHTML = '<p class="error">加载失败: HTTP ' + resp.status + '</p>';
            return;
        }
        const data = await resp.json();
        const events = data.events || [];
        if (badge) badge.textContent = events.length;
        if (events.length === 0) {
            container.innerHTML = '<div class="text-success" style="padding:8px 0;font-size:12px;">✓ 24h 内无瞬时冷却记录</div>';
            return;
        }
        const ERROR_LABELS = {
            rate_limited: '429 限流', upstream_5xx: '上游 5xx',
            network_transient: '网络抖动', unknown: '未知错误',
        };
        let html = '';
        for (const ev of events) {
            const ch = ev.channel_name || ('#' + ev.account_id);
            const acct = ev.account_name ? ' · ' + ev.account_name : '';
            const etype = ERROR_LABELS[ev.error_type] || ev.error_type || '?';
            const remain = ev.remaining_seconds != null ? Math.round(ev.remaining_seconds) + 's' : '?';
            const msg = ev.error_msg || '';
            html += '<div class="pr-card">';
            html += '<div class="pr-row">';
            html += '<span class="status-dot-small" style="background:var(--color-warning);"></span>';
            html += '<strong>' + esc(ch) + esc(acct) + '</strong>';
            html += '<span style="margin-left:auto;font-size:11px;color:var(--text-muted);">' + esc(ev.created_at || '') + '</span>';
            html += '</div>';
            html += '<div style="font-size:11px;margin-top:6px;color:var(--text-secondary);">';
            html += esc(etype) + ' · 观察时剩余冷却 ' + esc(remain);
            if (msg) html += ' — ' + esc(msg.length > 120 ? msg.slice(0, 120) + '…' : msg);
            html += '</div>';
            html += '</div>';
        }
        container.innerHTML = html;
    } catch (e) {
        container.innerHTML = '<p class="error">加载失败: ' + esc(e.message) + '</p>';
    }
}

async function ackRecover(accountId, btn) {
    if (!confirm('确认账号 #' + accountId + ' 问题已解决?将立即触发探活。')) return;
    const note = prompt('备注 (可选, 充值来源/操作人)');
    btn.disabled = true;
    try {
        const r = await authFetch('/dashboard/api/cooldown/' + accountId + '/ack-recover', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({note: note || ''})
        });
        if (r.ok) {
            btn.closest('.pr-card').remove();
            await loadPendingSummary();
        } else {
            const data = await r.json().catch(() => ({}));
            alert('确认失败: ' + (data.detail || r.status));
            btn.disabled = false;
        }
    } catch (e) {
        alert('操作失败: ' + e.message);
        btn.disabled = false;
    }
}

async function manualDisable(accountId, btn) {
    if (!confirm('确认禁用账号 #' + accountId + '?\n路由将永久排除该账号 (除非手动启用)。')) return;
    btn.disabled = true;
    try {
        const r = await authFetch('/dashboard/api/accounts/' + accountId + '/manual-disable', {method: 'POST'});
        if (r.ok) {
            btn.closest('.pr-card').remove();
            await loadPendingSummary();
        } else {
            alert('禁用失败: HTTP ' + r.status);
            btn.disabled = false;
        }
    } catch (e) {
        alert('操作失败: ' + e.message);
        btn.disabled = false;
    }
}

async function manualEnable(accountId, btn) {
    if (!confirm('启用账号 #' + accountId + '?')) return;
    btn.disabled = true;
    try {
        const r = await authFetch('/dashboard/api/accounts/' + accountId + '/manual-enable', {method: 'POST'});
        if (r.ok) {
            btn.closest('.pr-card').remove();
            await loadPendingSummary();
        } else {
            alert('启用失败: HTTP ' + r.status);
            btn.disabled = false;
        }
    } catch (e) {
        alert('操作失败: ' + e.message);
        btn.disabled = false;
    }
}

// (待处理卡由运维页调度器拉起; 顶栏 badge 走 refreshBadges 30s — Phase 3a)

// ============================================================
// V6.0 Phase 2 (2026-09-03): 成本审计 — 标价 vs 实付
// docs/next-major-v6-plan.md §2.3 Phase B
// - KPI: 实付 / 官方标价 / 节省 / 标价覆盖率
// - 趋势图: cost-trend (标价 vs 实付 双线)
// - 下钻: cost-by-model / cost-by-group / cost-by-user 三个 tab
// 节省口径: 只统计标价已知 (list_cost_estimate 非空) 的行, 覆盖率低时数字偏保守
// ============================================================

let _costTab = 'model';

function _costFmt(n) {
    return fmtCNY(n);
}

function _costKeyLabel(key) {
    return { model: '模型', group_id: '分组', username: '用户' }[key] || key;
}

// UI-3 (2026-09-04): 图表颜色读 CSS 变量 — 浅色主题 (style.css @media prefers-color-scheme: light)
// 下轴线/网格/数据线自动跟随, 不再硬编码深色配色. fill 需要 alpha 通道, 6 位 hex 变量手动转 rgba.
function _cssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}
function _cssColor(name, alpha) {
    const hex = _cssVar(name);
    if (/^#[0-9a-fA-F]{6}$/.test(hex)) {
        const n = parseInt(hex.slice(1), 16);
        return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${alpha})`;
    }
    return hex || '#71767b';
}

async function loadCostAudit() {
    const table = document.getElementById('cost-audit-table');
    if (!table) return;
    try {
        // Phase 3 指标重组: 趋势图收敛到概览页, 此处只拉当前 tab 明细 (KPI 取 totals)
        const fetches = [
            authFetch(`/dashboard/api/usage/cost-by-${_costTab}?days=${fmtDays()}`),
        ];
        if (_costTab === 'user') {
            fetches.push(authFetch(`/dashboard/api/stats/per-user?days=${fmtDays()}`));
        }
        const resps = await Promise.all(fetches);
        const dimResp = resps[0];
        for (const r of resps) {
            if (!r.ok) {
                table.innerHTML = '<p class="error">加载失败: HTTP ' + r.status + '</p>';
                return;
            }
        }
        const dim = await dimResp.json();

        // key 下钻: caller_id -> keys[] (仅 user tab)
        let userKeyMap = null;
        if (_costTab === 'user') {
            const perUser = await resps[1].json();
            userKeyMap = {};
            for (const item of (perUser.items || [])) {
                userKeyMap[item.caller_id === null ? 'null' : String(item.caller_id)] =
                    (item.keys || []).filter(k => k.key_id);
            }
        }

        // --- KPI ---
        const t = dim.totals || {};
        const set = (id, v) => { const e = document.getElementById(id); if (e) e.textContent = v; };
        set('kpi-cost-paid', _costFmt(t.cost_yuan));
        set('kpi-cost-list', _costFmt(t.list_cost_yuan));
        set('kpi-cost-saved', _costFmt(t.saved_yuan));
        set('kpi-cost-coverage', (t.list_coverage_pct || 0) + '%');
        const covEl = document.getElementById('kpi-cost-coverage');
        if (covEl) covEl.style.color = (t.list_coverage_pct || 0) < 50 ? 'var(--color-warning, #f0b132)' : '';

        // --- 下钻表 ---
        renderCostTable(dim, userKeyMap);
    } catch (e) {
        if (table) table.innerHTML = '<p class="error">加载失败: ' + esc(e.message) + '</p>';
    }
}

function renderCostTable(dim, userKeyMap) {
    const table = document.getElementById('cost-audit-table');
    if (!table) return;
    const items = dim.items || [];
    const key = { model: 'model', group: 'group_id', user: 'username' }[_costTab];
    if (items.length === 0) {
        table.innerHTML = '<p class="empty">近 ' + (dim.days || fmtDays()) + ' 天无用量数据</p>';
        return;
    }
    // V6.1+: 按用户 tab = 原右栏"按人用量"卡并入 (key 下钻 + 双轨成本列)
    const isUserTab = _costTab === 'user';
    let html = '<div class="scroll-y-300"><table><thead><tr>'
        + '<th>' + _costKeyLabel(key) + '</th>'
        + (isUserTab ? '<th>Input</th><th>Output</th><th' + mdTitle('reasoning') + '>思考</th>' : '')
        + '<th' + mdTitle('requests') + '>请求</th><th' + mdTitle('tokens') + '>Token</th>'
        + '<th' + mdTitle('paid') + '>实付</th><th' + mdTitle('listPrice') + '>官方标价</th>'
        + '<th' + mdTitle('listSaved') + '>标价节省</th><th' + mdTitle('coverage') + '>标价覆盖率</th>'
        + '</tr></thead><tbody>';
    for (const it of items) {
        const name = it.model || it.group_id || it.username || '-';
        const label = it.display_name ? esc(name) + ' (' + esc(it.display_name) + ')' : esc(name);
        const cov = it.request_count ? Math.round((it.list_known_requests || 0) / it.request_count * 100) : 0;
        const savedTxt = it.list_known_requests === 0
            ? '<span class="text-muted">标价未知</span>'
            : '<span class="text-success">' + _costFmt(it.saved_yuan) + '</span>';

        // user tab: key 下钻 (V6.1+ 从按人用量卡并入)
        const keys = isUserTab && userKeyMap
            ? (userKeyMap[it.caller_id === null ? 'null' : String(it.caller_id)] || [])
            : [];
        const expandable = isUserTab && keys.length > 0;

        html += '<tr class="' + (expandable ? 'expandable-row' : '') + '"'
            + (expandable ? ' onclick="toggleKeyDetail(this)"' : '') + '>'
            + '<td>' + (expandable ? '<span class="expand-icon">▸</span> ' : '')
            + '<code class="text-mono">' + label + '</code></td>'
            + (isUserTab
                ? '<td>' + fmtNum(it.input_tokens || 0) + '</td>'
                  + '<td>' + fmtNum(it.output_tokens || 0) + '</td>'
                  + '<td class="mono">' + (it.reasoning_tokens ? fmtNum(it.reasoning_tokens) : '<span class="text-muted">—</span>') + '</td>'
                : '')
            + '<td>' + fmtNum(it.request_count) + '</td>'
            + '<td>' + fmtNum((it.input_tokens || 0) + (it.output_tokens || 0)) + '</td>'
            + '<td>' + _costFmt(it.cost_yuan) + '</td>'
            + '<td>' + _costFmt(it.list_cost_yuan) + '</td>'
            + '<td>' + savedTxt + '</td>'
            + '<td>' + cov + '%</td>'
            + '</tr>';

        if (expandable) {
            html += '<tr class="key-detail-row" style="display:none;">'
                + '<td colspan="' + (isUserTab ? 10 : 7) + '">'
                + '<table class="sub-table"><thead><tr>'
                + '<th>Key Label</th><th>请求</th><th>Input</th><th>Output</th><th>Token</th><th>实付</th>'
                + '</tr></thead><tbody>';
            for (const k of keys) {
                html += '<tr>'
                    + '<td>' + esc(k.label) + '</td>'
                    + '<td>' + fmtNum(k.request_count) + '</td>'
                    + '<td>' + fmtNum(k.input_tokens) + '</td>'
                    + '<td>' + fmtNum(k.output_tokens) + '</td>'
                    + '<td>' + fmtNum(k.total_tokens) + (k.reasoning_tokens ? ' <span class="text-muted">🧠' + fmtNum(k.reasoning_tokens) + '</span>' : '') + '</td>'
                    + '<td>' + _costFmt(k.total_cost) + '</td>'
                    + '</tr>';
            }
            html += '</tbody></table></td></tr>';
        }
    }
    html += '</tbody></table></div>';
    table.innerHTML = html;
}

function switchCostTab(tab) {
    _costTab = tab;
    // tab 高亮 (btn-primary 只有当前 tab)
    for (const t of ['model', 'group', 'user']) {
        const btn = document.getElementById('cost-tab-' + t);
        if (btn) btn.classList.toggle('btn-primary', t === tab);
    }
    loadCostAudit();
}

// 初始 tab 高亮 (数据加载由成本页调度器负责, 不在此触发请求)
for (const t of ['model', 'group', 'user']) {
    const btn = document.getElementById('cost-tab-' + t);
    if (btn) btn.classList.toggle('btn-primary', t === 'model');
}

// ============================================================
// Phase 3a (2026-09-05): 页面调度器 — docs/dashboard-phase3-design.md §2.3
// - 侧边栏 6 页哈希路由, 只轮询当前页 (切页停旧定时器)
// - 原 htmx 轮询卡全部转 JS loader (loadJsonCard + 既有 renderXxx)
// - 顶栏 badge 独立 30s 常驻轮询
// ============================================================

// 通用 JSON 卡 loader: 拉接口 → textContent 暂存 → 复用既有 renderXxx 解析渲染
async function loadJsonCard(containerId, url, renderer, after) {
    const container = document.getElementById(containerId);
    if (!container) return;
    try {
        const resp = await authFetch(url);
        if (!resp.ok) return;
        container.textContent = await resp.text();
        renderer(container);
        if (after) after();
    } catch (e) { console.warn(containerId + ' failed:', e); }
}

function loadChannelsCard() { return loadJsonCard('channels-list', '/dashboard/api/channels', renderChannels, filterChannels); }
function loadPricesCard() { return loadJsonCard('prices-list', '/dashboard/api/prices', renderPrices, filterChannels); }
function loadRegistryModelsCard() { return loadJsonCard('registry-models-list', '/dashboard/api/registry/models', renderRegistryModels, filterRegistry); }
function loadRegistryGroupsCard() { return loadJsonCard('registry-groups-list', '/dashboard/api/registry/groups', renderRegistryGroups); }
function loadPromotionsCard() { return loadJsonCard('promotions-list', '/dashboard/api/promotions', renderPromotions); }
function loadPromotionReviewsCard() { return loadJsonCard('promotion-review-list', '/dashboard/api/promotion-reviews', renderPromotionReview); }
function loadPendingReviewCard() { return loadJsonCard('pending-review-list', '/dashboard/api/pending-review', renderPendingReview); }
function loadAccountsCard() { loadAccounts(); loadActiveChannels(); }
// loadWeeklyReportLatest 定义在周报 markdown 渲染区 (带 marked.parse, 此处不重复)

// ---- 概览新增卡 ----

let _overviewTrendChart = null;

// 标价 vs 实付 趋势 (概览缩略版, 配置与成本审计趋势一致)
async function loadOverviewTrend() {
    const canvas = document.getElementById('overview-trend-chart');
    if (!canvas) return;
    try {
        const resp = await authFetch(`/dashboard/api/usage/cost-trend?days=${fmtDays()}`);
        if (!resp.ok) return;
        const trend = await resp.json();
        if (_overviewTrendChart) { _overviewTrendChart.destroy(); _overviewTrendChart = null; }
        _overviewTrendChart = new Chart(canvas, {
            type: 'line',
            data: {
                labels: (trend.items || []).map(d => d.date),
                datasets: [{
                    label: '官方标价 ¥',
                    data: (trend.items || []).map(d => d.list_cost_yuan),
                    borderColor: _cssColor('--color-info', 1),
                    backgroundColor: _cssColor('--color-info', 0.15),
                    tension: 0.3,
                    fill: true,
                }, {
                    label: '实付 ¥',
                    data: (trend.items || []).map(d => d.cost_yuan),
                    borderColor: _cssColor('--color-success', 1),
                    backgroundColor: _cssColor('--color-success', 0.15),
                    tension: 0.3,
                    fill: true,
                }],
            },
            options: {
                responsive: true,
                    maintainAspectRatio: false,
                plugins: { legend: { labels: { color: _cssVar('--text-secondary') } } },
                scales: {
                    x: { ticks: { color: _cssVar('--text-muted') }, grid: { color: _cssVar('--border-muted') } },
                    y: { ticks: { color: _cssVar('--text-muted') }, grid: { color: _cssVar('--border-muted') }, beginAtZero: true }
                }
            }
        });
    } catch (e) { console.warn('overview trend failed:', e); }
}

// Top 模型 (前 5)
async function loadUsageTop() {
    const el = document.getElementById('usage-top-list');
    if (!el) return;
    try {
        const resp = await authFetch(`/dashboard/api/usage/cost-by-model?days=${fmtDays()}`);
        if (!resp.ok) return;
        const items = (await resp.json()).items || [];
        if (items.length === 0) {
            el.innerHTML = '<p class="empty">近 ' + fmtDays() + ' 天无用量数据</p>';
            return;
        }
        let html = '<table><thead><tr><th>模型</th><th' + mdTitle('requests') + '>请求</th><th' + mdTitle('paid') + '>实付</th><th' + mdTitle('listPrice') + '>标价</th></tr></thead><tbody>';
        for (const it of items.slice(0, 5)) {
            html += '<tr><td><code class="text-mono">' + esc(it.model || '—') + '</code></td>'
                + '<td>' + fmtNum(it.request_count) + '</td>'
                + '<td class="mono">' + _costFmt(it.cost_yuan) + '</td>'
                + '<td class="mono">' + _costFmt(it.list_cost_yuan) + '</td></tr>';
        }
        html += '</tbody></table>';
        el.innerHTML = html;
    } catch (e) { console.warn('usage top failed:', e); }
}

// 分组概览 (前 3 tier)
async function loadGroupOverview() {
    const el = document.getElementById('group-overview-list');
    if (!el) return;
    try {
        const resp = await authFetch(`/dashboard/api/usage/cost-by-group?days=${fmtDays()}`);
        if (!resp.ok) return;
        const items = (await resp.json()).items || [];
        if (items.length === 0) {
            el.innerHTML = '<p class="empty">近 ' + fmtDays() + ' 天无分组用量</p>';
            return;
        }
        const totalReq = items.reduce((s, it) => s + (it.request_count || 0), 0) || 1;
        let html = '<table><thead><tr><th>分组</th><th' + mdTitle('requests') + '>请求</th><th>占比</th><th' + mdTitle('paid') + '>实付</th></tr></thead><tbody>';
        for (const it of items.slice(0, 3)) {
            const pct = Math.round((it.request_count || 0) / totalReq * 100);
            html += '<tr><td><code class="text-mono">' + esc(it.group_id || '—') + '</code></td>'
                + '<td>' + fmtNum(it.request_count) + '</td>'
                + '<td><div class="bar-track"><div class="bar-fill" style="width:' + pct + '%"></div></div></td>'
                + '<td class="mono">' + _costFmt(it.cost_yuan) + '</td></tr>';
        }
        html += '</tbody></table>';
        el.innerHTML = html;
    } catch (e) { console.warn('group overview failed:', e); }
}

// 今日摘要: 过去 24h 请求 + 账号/队列异常一行速览
async function loadTodaySummary() {
    const el = document.getElementById('today-summary');
    if (!el) return;
    try {
        const [usageResp, acctResp, pendResp] = await Promise.all([
            authFetch('/dashboard/api/usage/summary?days=1'),
            authFetch('/dashboard/api/accounts'),
            authFetch('/dashboard/api/pending-summary'),
        ]);
        const usage = usageResp.ok ? await usageResp.json() : [];
        const reqs = (usage || []).reduce((s, r) => s + (r.request_count || 0), 0);
        const accounts = acctResp.ok ? ((await acctResp.json()).accounts || []) : [];
        const inactive = accounts.filter(a => (a.status || 'active') !== 'active').length;
        const pending = pendResp.ok ? ((await pendResp.json()).total || 0) : 0;
        const issues = [];
        if (inactive > 0) issues.push(inactive + ' 个账号非 active');
        if (pending > 0) issues.push(pending + ' 项待处理');
        if (issues.length === 0) {
            el.innerHTML = '<div class="text-success" style="font-size:13px;padding:4px 0;">✓ 一切正常 — 24h 内 ' + fmtNum(reqs) + ' 个请求 · 全部账号 active · 无待处理积压</div>';
        } else {
            el.innerHTML = '<div style="font-size:13px;padding:4px 0;">24h 内 ' + fmtNum(reqs) + ' 个请求 · '
                + '<span style="color:var(--color-warning);">' + issues.join(' · ') + '</span>'
                + ' — 详情见 <a href="#/ops">运维</a></div>';
        }
    } catch (e) { console.warn('today summary failed:', e); }
}

// ---- 顶栏 badge (唯一跨页常驻轮询) ----

async function refreshBadges() {
    try {
        const [pendResp, suggResp] = await Promise.all([
            // V6.3.1: badge 只数持久项 — 瞬时冷却 15~60s 自愈, 30s 轮询撞上
            // 冷却窗口就会出现 "badge 有数、进运维页已空" 的竞态假警报
            authFetch('/dashboard/api/pending-summary?exclude_transient=true'),
            authFetch('/dashboard/api/suggestions?status=pending'),
        ]);
        let pend = 0;
        if (pendResp.ok) pend = (await pendResp.json()).total || 0;
        let sugg = 0;
        if (suggResp.ok) {
            const d = await suggResp.json();
            sugg = (d.model || []).length + (d.group || []).length;
        }
        const bp = document.getElementById('badge-pending');
        if (bp) { bp.hidden = pend === 0; const b = bp.querySelector('b'); if (b) b.textContent = pend; }
        const bs = document.getElementById('badge-suggestions');
        if (bs) { bs.hidden = sugg === 0; const b = bs.querySelector('b'); if (b) b.textContent = sugg; }
    } catch (e) { /* badge 静默失败, 不干扰页面 */ }
}

// ---- 页面注册表 + 调度器 ----

const PAGE_ROUTES = {
    overview: { title: '概览', ms: 60000, loaders: [loadOverviewKPIs, loadOverviewTrend, loadUsageTop, loadGroupOverview, loadTodaySummary] },
    cost:     { title: '成本', ms: 60000, loaders: [loadCostAudit, loadCacheStatsList] },
    usage:    { title: '用量', ms: 60000, loaders: [loadUsageChart, loadGroupRouting, loadRequests], once: [loadRequestFilterOptions] },
    // channels: 渠道状态/价格会随冷却自愈与上下架变动, 保留 30s 轮询;
    // registry 模型/分组/用户 Key/采集源是配置类数据 (仅人为操作才变, 操作后已有显式刷新),
    // 周报周级生成 — 以上均只在进页时加载一次 (once).
    // 注意: 情报页三张审批卡必须留在轮询 — 批准/拒绝只做本地 card.remove(), 卡间联动全靠轮询.
    channels: { title: '渠道', ms: 30000, loaders: [loadChannelsCard, loadPricesCard], once: [loadRegistryModelsCard, loadRegistryGroupsCard] },
    intel:    { title: '情报', ms: 120000, loaders: [loadPromotionsCard, loadPromotionReviewsCard, loadPendingReviewCard], once: [loadReportsList, loadWeeklyReportLatest] },
    ops:      { title: '运维', ms: 60000, loaders: [loadDataFreshnessDonut, loadPendingSummary, loadCooldownEvents, loadSuggestionsList, loadAccountsCard, loadAutoJoinMetrics], once: [loadUserKeys, loadSources] },
};

let _currentPage = null;
let _pageTimer = null;

function showPage(name) {
    if (!PAGE_ROUTES[name]) name = 'overview';
    _currentPage = name;
    document.querySelectorAll('.page').forEach(p => p.classList.toggle('active', p.id === 'page-' + name));
    document.querySelectorAll('.nav-item').forEach(n => n.classList.toggle('active', n.dataset.page === name));
    if (location.hash !== '#/' + name) {
        try { history.replaceState(null, '', '#/' + name); } catch (e) { /* ignore */ }
    }
    if (_pageTimer) clearInterval(_pageTimer);
    window.scrollTo(0, 0); // 切页回到顶部 (长页切换不必来回滚)
    const page = PAGE_ROUTES[name];
    const runAll = () => page.loaders.forEach(fn => { try { fn(); } catch (e) { console.warn(name + ' loader failed:', e); } });
    (page.once || []).forEach(fn => { try { fn(); } catch (e) { console.warn(name + ' once-loader failed:', e); } });
    runAll();
    _pageTimer = setInterval(runAll, page.ms);
}

function initNav() {
    document.querySelectorAll('.nav-item').forEach(n => {
        n.addEventListener('click', (e) => { e.preventDefault(); showPage(n.dataset.page); });
    });
    window.addEventListener('hashchange', () => {
        const r = location.hash.replace(/^#\//, '') || 'overview';
        if (r !== _currentPage) showPage(r);
    });
    // 返回顶部按钮: 滚动超 400px 出现
    const backTop = document.getElementById('back-top');
    if (backTop) {
        window.addEventListener('scroll', () => { backTop.hidden = window.scrollY < 400; }, { passive: true });
        backTop.addEventListener('click', () => window.scrollTo({ top: 0, behavior: 'smooth' }));
    }
    showPage(location.hash.replace(/^#\//, '') || 'overview');
}

// ============================================================
// Phase 3b (2026-09-05): 请求日志 — docs/dashboard-phase3-design.md §3
// 列表: usage_logs 明细 (状态/模型/渠道/Key 过滤 + 分页); 转录: 行内按钮开抽屉,
// 后端按 model+时间窗 best-effort 匹配, 仅 admin key 可见 (中间件保证)。
// ============================================================

let _reqPage = 1, _reqStatus = 'all', _reqModel = '', _reqChannel = '', _reqKey = '', _reqDebounce = null;

function requestsFilterChanged() {
    const s = document.getElementById('req-status');
    const m = document.getElementById('req-model');
    const c = document.getElementById('req-channel');
    const k = document.getElementById('req-key');
    _reqStatus = s ? s.value : 'all';
    _reqModel = m ? m.value.trim() : '';
    _reqChannel = c ? c.value : '';
    _reqKey = k ? k.value : '';
    _reqPage = 1;
    clearTimeout(_reqDebounce);
    _reqDebounce = setTimeout(loadRequests, 300);
}

// 渠道/Key 筛选选项 — 配置类数据, 进用量页时刷一次 (PAGE_ROUTES once), 不进 60s 轮询。
// Key 显示名与后端 key_label_map 同规则: label||prefix, 同名 label 跨用户时追加 (username);
// 已吊销 key 保留在选项里 (历史请求仍可按它过滤), 标注区分。
async function loadRequestFilterOptions() {
    const cSel = document.getElementById('req-channel');
    const kSel = document.getElementById('req-key');
    if (!cSel || !kSel) return;
    try {
        const [chResp, keyResp] = await Promise.all([
            authFetch('/dashboard/api/channels'),
            authFetch('/dashboard/api/keys'),
        ]);
        if (chResp.ok) {
            const channels = await chResp.json();
            const cur = cSel.value;
            cSel.innerHTML = '<option value="">全部渠道</option>' + channels.map(ch =>
                '<option value="' + ch.id + '">' + esc(ch.name) + (ch.status !== 'active' ? ' (停用)' : '') + '</option>'
            ).join('');
            if ([...cSel.options].some(o => o.value === cur)) cSel.value = cur;
        }
        if (keyResp.ok) {
            const data = await keyResp.json();
            const entries = [];
            for (const u of (data.users || [])) {
                for (const k of (u.keys || [])) {
                    const base = k.label || k.key_prefix;
                    if (base) entries.push({ id: k.id, base, username: u.username, status: k.status });
                }
            }
            const owners = {};
            for (const e of entries) (owners[e.base] = owners[e.base] || new Set()).add(e.username);
            const cur = kSel.value;
            kSel.innerHTML = '<option value="">全部 Key</option>' + entries.map(e => {
                let text = e.base;
                if (e.username && owners[e.base].size > 1) text += ' (' + e.username + ')';
                if (e.status === 'revoked') text += ' (已吊销)';
                return '<option value="' + e.id + '">' + esc(text) + '</option>';
            }).join('');
            if ([...kSel.options].some(o => o.value === cur)) kSel.value = cur;
        }
    } catch (e) { /* 选项加载失败不阻塞日志主列表, 下拉退化为只有"全部" */ }
}

function requestsPage(delta) {
    _reqPage = Math.max(1, _reqPage + delta);
    loadRequests();
}

async function loadRequests() {
    const list = document.getElementById('requests-list');
    if (!list) return;
    try {
        const params = new URLSearchParams({
            days: String(fmtDays()), status: _reqStatus,
            page: String(_reqPage), page_size: '50',
        });
        if (_reqModel) params.set('model', _reqModel);
        if (_reqChannel) params.set('channel_id', _reqChannel);
        if (_reqKey) params.set('key_id', _reqKey);
        const resp = await authFetch(`/dashboard/api/requests?${params}`);
        if (!resp.ok) {
            list.innerHTML = '<p class="error">加载失败: HTTP ' + resp.status + '</p>';
            return;
        }
        const data = await resp.json();
        const totalPages = Math.max(1, Math.ceil(data.total / data.page_size));
        const meta = document.getElementById('req-meta');
        if (meta) meta.textContent = '共 ' + fmtNum(data.total) + ' 条 · 第 ' + data.page + '/' + totalPages + ' 页';
        const prev = document.getElementById('req-prev');
        const next = document.getElementById('req-next');
        if (prev) prev.disabled = data.page <= 1;
        if (next) next.disabled = data.page >= totalPages;
        if (!data.items.length) {
            list.innerHTML = '<p class="empty">当前过滤条件下无请求记录</p>';
            return;
        }
        let html = '<div class="scroll-y-420"><table><thead><tr><th>时间</th><th>模型</th><th>渠道</th><th>Key</th><th>状态</th><th>延迟</th><th>Token</th><th>实付</th><th></th></tr></thead><tbody>';
        for (const r of data.items) {
            const cls = r.status_code >= 500 ? 'st-error' : (r.status_code >= 400 ? 'st-warn' : 'st-ok');
            html += '<tr>'
                + '<td class="mono" style="font-size:11px;">' + esc((r.created_at || '').replace('T', ' ').slice(5, 19)) + '</td>'
                + '<td><code class="text-mono">' + esc(r.model || '—') + '</code></td>'
                + '<td>' + esc(r.channel_name || '—')
                + (r.account_name ? ' <span class="text-muted" style="font-size:11px;" title="上游服务账号">· ' + esc(r.account_name) + '</span>' : '')
                + '</td>'
                + '<td>' + esc(r.key_label || '—') + '</td>'
                + '<td><span class="status-pill ' + cls + '">' + esc(String(r.status_code)) + '</span></td>'
                + '<td class="mono">' + fmtLatency(r.latency_ms) + '</td>'
                + '<td class="mono">' + fmtTokens((r.input_tokens || 0) + (r.output_tokens || 0)) + (r.reasoning_tokens ? ' <span class="text-muted" title="其中思考 token (上游返回)">🧠' + fmtTokens(r.reasoning_tokens) + '</span>' : '') + '</td>'
                + '<td class="mono">' + fmtCNY(r.cost_yuan) + '</td>'
                + '<td><button class="btn-tiny" onclick="openRequestDrawer(' + r.id + ')">详情</button></td>'
                + '</tr>';
        }
        html += '</tbody></table></div>';
        list.innerHTML = html;
    } catch (e) {
        list.innerHTML = '<p class="error">加载失败: ' + esc(e.message) + '</p>';
    }
}

async function openRequestDrawer(id) {
    const overlay = document.getElementById('drawer-overlay');
    const drawer = document.getElementById('req-drawer');
    if (!drawer) return;
    if (overlay) overlay.hidden = false;
    drawer.hidden = false;
    const meta = document.getElementById('drawer-meta');
    const reqPre = document.getElementById('drawer-req');
    const respPre = document.getElementById('drawer-resp');
    if (meta) meta.textContent = '#' + id + ' 加载中…';
    if (reqPre) reqPre.textContent = '加载中…';
    if (respPre) respPre.textContent = '';
    try {
        const resp = await authFetch(`/dashboard/api/requests/${id}/transcript`);
        const data = await resp.json();
        if (!resp.ok) {
            if (reqPre) reqPre.textContent = '加载失败: HTTP ' + resp.status + (resp.status === 403 ? ' (仅 admin key 可看转录)' : '');
            return;
        }
        if (meta) meta.textContent = '#' + id
            + (data.account_name ? ' · 账号 ' + data.account_name : '')
            + (data.truncated ? ' · 已截断 (超 256KB)' : '')
            + (data.transcript_created_at ? ' · 转录时间 ' + data.transcript_created_at.replace('T', ' ').slice(0, 19) : '');
        if (reqPre) reqPre.textContent = data.request_body || '(无)';
        if (respPre) respPre.textContent = data.response_body || '(无)';
    } catch (e) {
        if (reqPre) reqPre.textContent = '加载失败: ' + e.message;
    }
}

function closeRequestDrawer() {
    const o = document.getElementById('drawer-overlay');
    const d = document.getElementById('req-drawer');
    if (o) o.hidden = true;
    if (d) d.hidden = true;
}

document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') closeRequestDrawer();
});

// ============================================================
// V6.3 F3 (自动化收口, 2026-09-05): 自动入组观察卡
// 纯只读埋点展示, 为 AUTO_JOIN_NON_T0 决策积累数据; 不含任何操作按钮
// ============================================================

async function loadAutoJoinMetrics() {
    const container = document.getElementById('auto-join-metrics-list');
    const meta = document.getElementById('aj-metrics-meta');
    if (!container) return;
    try {
        const resp = await authFetch('/dashboard/api/ops/auto-join-metrics?days=14');
        if (!resp.ok) {
            container.innerHTML = '<p class="error">加载失败: HTTP ' + resp.status + '</p>';
            return;
        }
        const data = await resp.json();
        if (meta) meta.textContent = '近 ' + data.days + ' 天 · AUTO_JOIN_NON_T0=' + (data.auto_join_enabled ? 'True' : 'False');

        const pr = data.pending_review || {};
        const gr = data.group_routing || {};
        const bs = data.tier_blind_spot || {};
        const rb = data.review_burden || {};
        const td = data.tier_distribution || {};

        // 积压概览行
        let html = '<div class="flex-col gap-sm">';
        html += '<div class="pr-card"><div class="pr-row"><strong>待审积压 (approved=0)</strong>'
            + '<span class="badge ' + (pr.total > 20 ? 'badge-danger' : 'badge') + '" style="margin-left:auto;">' + fmtNum(pr.total) + '</span></div>';
        if (pr.weekly && pr.weekly.length) {
            html += '<div style="font-size:11px;margin-top:6px;color:var(--text-secondary);">'
                + pr.weekly.map(w => esc(w.bucket) + ': ' + fmtNum(w.count)).join(' · ')
                + ' (新增按 created_at 归桶)</div>';
        }
        html += '</div>';

        // 审批负担 + 盲区
        html += '<div class="pr-card"><div class="pr-row"><strong>审批负担 (近 ' + data.days + ' 天)</strong>'
            + '<span class="badge" style="margin-left:auto;">' + fmtNum(rb.reviewed_count) + ' 次处理</span></div>'
            + '<div style="font-size:11px;margin-top:6px;color:var(--text-secondary);">'
            + '批准 ' + fmtNum(rb.approved_count || 0) + ' / 拒绝 ' + fmtNum(rb.rejected_count || 0)
            + (rb.note ? ' · ' + esc(rb.note) : '') + '</div></div>';

        html += '<div class="pr-card"><div class="pr-row"><strong title="metadata 行中 inferred_tier=null 占比 — 这些模型无法从模型名推断参数规模, 永远无法自动入组">infer_tier 盲区</strong>'
            + '<span class="badge ' + (bs.pct > 30 ? 'badge-danger' : 'badge') + '" style="margin-left:auto;">' + fmtPct(bs.pct, bs.metadata_total > 0) + '</span></div>'
            + '<div style="font-size:11px;margin-top:6px;color:var(--text-secondary);">'
            + fmtNum(bs.tier_null) + ' / ' + fmtNum(bs.metadata_total) + ' 条 AI 推断缺参数规模依据</div></div>';

        // tier 分布
        const tierOrder = ['T0', 'T1', 'T2', 'T3', 'T4'];
        const tierTotal = Object.values(td).reduce((a, b) => a + b, 0);
        let tierParts = [];
        for (const t of tierOrder) {
            if (td[t]) tierParts.push(t + ': ' + fmtNum(td[t]));
        }
        if (td['(null)']) tierParts.push('null: ' + fmtNum(td['(null)']));
        html += '<div class="pr-card"><div class="pr-row"><strong>推断 tier 分布 (ai_infer 全量)</strong>'
            + '<span class="badge" style="margin-left:auto;">' + fmtNum(tierTotal) + ' 条</span></div>'
            + '<div style="font-size:11px;margin-top:6px;color:var(--text-secondary);">'
            + (tierParts.length ? esc(tierParts.join(' · ')) : '暂无 tier 推断记录') + '</div></div>';

        // 组路由命中
        html += '<div class="pr-card"><div class="pr-row"><strong>组路由命中 (近 ' + data.days + ' 天请求)</strong>'
            + '<span class="badge" style="margin-left:auto;">' + fmtPct(gr.grouped_pct, gr.total_requests > 0) + '</span></div>'
            + '<div style="font-size:11px;margin-top:6px;color:var(--text-secondary);">tier: 组请求 '
            + fmtNum(gr.grouped_requests) + ' / 总 ' + fmtNum(gr.total_requests) + ' (其余为精确模型名路由)</div>';
        if (gr.items && gr.items.length) {
            html += '<div style="font-size:11px;margin-top:4px;color:var(--text-secondary);">'
                + gr.items.slice(0, 6).map(g => esc(g.group_id) + ': ' + fmtNum(g.count) + ' (' + g.pct + '%)').join(' · ')
                + '</div>';
        }
        html += '</div>';

        html += '<div style="font-size:11px;color:var(--text-muted);">观察期 ≥2 周后基于本卡数据决策是否开启 AUTO_JOIN_NON_T0 (S1-S4), 详见 docs/dev/automation-closure-plan.md</div>';
        html += '</div>';
        container.innerHTML = html;
    } catch (e) {
        container.innerHTML = '<p class="error">加载失败: ' + esc(e.message) + '</p>';
    }
}

