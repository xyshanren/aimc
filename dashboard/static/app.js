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
            // Phase 4 v4.3.1 (2026-08-13): 9 家厂商 announcement source 已补齐
            // (deepseek/glm/原有 + kimi/minimax/qwen/hunyuan/sensenova/stepfun/moonshot 7 家新增)
            // 数据为空说明 scheduler 尚未完成首次采集, 或该厂商无新优惠, 不是 "source 未接"
            container.innerHTML = `
                <p class="loading">暂无优惠情报</p>
                <p class="meta">9 家厂商 announcement source 已配置 (intelligence/sources/*AnnouncementSource);
                    数据为空说明 scheduler 尚未完成首次采集 (默认 30 分钟一次),
                    或该厂商当前无新优惠发布.
                    <br>手动触发: <code>POST /dashboard/api/sources/run-now?provider=&lt;name&gt;</code></p>
            `;
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
        // Phase 4 v4.3.1 (2026-08-13): 按 price_type 分组渲染
        // - normal: 默认
        // - cache_hit: 上下文缓存命中 (0元)
        // - free: 完全免费
        // - peak/offpeak: 峰谷时段
        const TYPE_ORDER = ['free', 'cache_hit', 'normal', 'peak', 'offpeak'];
        const TYPE_LABEL = {
            'free': '免费',
            'cache_hit': '缓存命中',
            'normal': '常规',
            'peak': '峰时',
            'offpeak': '谷时',
        };
        const grouped = {};
        for (const p of data) {
            const t = p.price_type || 'normal';
            if (!grouped[t]) grouped[t] = [];
            grouped[t].push(p);
        }
        let html = '';
        for (const type of TYPE_ORDER) {
            const items = grouped[type];
            if (!items || items.length === 0) continue;
            const label = TYPE_LABEL[type] || type;
            html += `<h3 class="price-section-title"><span class="price-type-badge price-type-${esc(type)}">${esc(label)}</span> <span class="meta">${items.length} 条</span></h3>`;
            html += '<table><thead><tr><th>模型</th><th>渠道</th><th>时段</th><th>输入价</th><th>输出价</th></tr></thead><tbody>';
            for (const p of items) {
                const priceLowClass = type === 'free' ? 'price-zero' : (p.input_price === 0 ? 'price-zero' : '');
                html += `<tr>
                    <td><code>${esc(p.model)}</code></td>
                    <td>${esc(p.channel_name)}</td>
                    <td>${esc(p.time_range)}</td>
                    <td class="${priceLowClass}">${esc(p.input_price ?? '-')}</td>
                    <td class="${priceLowClass}">${esc(p.output_price ?? '-')}</td>
                </tr>`;
            }
            html += '</tbody></table>';
        }
        container.innerHTML = html || '<p class="loading">暂无价格数据</p>';
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
        html += '<table><thead><tr><th>model_id</th><th>family</th><th>tier</th><th>capabilities</th><th>ctx</th><th title="是否开源权重">开源</th><th>状态</th></tr></thead><tbody>';
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
            // Phase 2a+ fix (2026-08-13): 每个 member 加 registry status 徽章
            // 让 UI 能看到 "在组里但 registry 标 deprecated / 不在 registry" 的成员
            for (const m of g.members) {
                const status = m.status || 'missing';
                const tier = m.tier || '';
                const tierMark = tier ? ` <span class="meta">(${esc(tier)})</span>` : '';
                let statusBadge;
                if (status === 'active') {
                    statusBadge = '<span class="status-badge status-active">active</span>';
                } else if (status === 'deprecated') {
                    statusBadge = '<span class="status-badge status-deprecated">deprecated</span>';
                } else if (status === 'preview') {
                    statusBadge = '<span class="status-badge status-preview">preview</span>';
                } else {
                    statusBadge = '<span class="status-badge status-missing">未注册</span>';
                }
                html += `<li>
                    <code>${esc(m.model_id)}</code>${tierMark}
                    <span class="meta">priority=${esc(m.priority)}</span>
                    ${statusBadge}
                </li>`;
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
