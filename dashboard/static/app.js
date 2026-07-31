/**
 * AIMC Dashboard 前端逻辑
 * 使用 htmx 进行局部刷新，Chart.js 绘制用量图表
 */

// HTML 转义，防止 XSS
function esc(str) {
    if (!str) return '';
    const div = document.createElement('div');
    div.textContent = String(str);
    return div.innerHTML;
}

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
