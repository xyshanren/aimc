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
