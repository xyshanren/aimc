# Dashboard 前端设计原则

> 受众：改 dashboard 前端的开发者
> 前提：[setup.md](setup.md)（本地搭起来）
> 设计参考：DeepSeek Chat / Claude / ChatGPT LLM UI 风格

## 1. 设计原则

### 1.1 信息层级

- **3 列宽屏布局**（≥1280px）：左 = 健康度 / 中 = 数据 / 右 = 操作
- **2 列**（≥768px）：隐藏右栏
- **单列**（<768px）：隐藏左栏，垂直堆叠
- 卡片用 `border-radius: 8px` + `border: 1px solid var(--border-default)`

### 1.2 配色

- **深色优先**（LLM 工程师多夜间使用）+ 跟随 `prefers-color-scheme` 自动浅色
- 状态色：`success (#3fb950) / warning (#d29922) / danger (#f85149) / info (#58a6ff)`
- 字体：sans 主体 + monospace 用于 model 名/价格/token（JetBrains Mono 优先）

### 1.3 交互

- **零依赖**：不引入 Tailwind/React/Vue/Chart.js 之外的库
- htmx 自动加载 + JS 函数接管渲染（避免 htmx 默认 innerHTML 渲染 JSON）
- 快捷键：`r` 刷新 / `a` 应用建议 / `/` 搜索 / `?` 切换提示
- 60s 定时刷新 dashboard 数据（不要实时推送，个人项目无需 WebSocket）

## 2. CSS 变量（`dashboard/static/style.css`）

```css
/* 背景 (深色) */
--bg-base: #0d1117;
--bg-surface: #161b22;
--bg-elevated: #21262d;
--bg-hover: #1f2937;

/* 文字 */
--text-primary: #e6edf3;
--text-secondary: #8b949e;
--text-muted: #6e7681;

/* 间距 */
--space-xs: 4px;
--space-sm: 8px;
--space-md: 12px;
--space-lg: 16px;
--space-xl: 24px;

/* 圆角 */
--radius-sm: 4px;
--radius-md: 8px;
--radius-lg: 12px;
```

浅色模式自动覆盖（`@media (prefers-color-scheme: light)`）。

## 3. 组件清单

### 3.1 KPI 卡片

```html
<div class="kpi">
    <div class="kpi-value">54,570</div>
    <div class="kpi-label">总 TOKEN</div>
</div>
```

- 父容器 `.kpi-grid` 默认 4 列；中间列窄时降级为 2 列
- 数字 22px font-mono

### 3.2 Donut 饼图

```html
<div class="donut">
    <div class="donut-bg" id="freshness-donut"></div>
    <div class="donut-text">
        <div class="donut-value">100%</div>
        <div class="donut-label">10 / 10 健康</div>
    </div>
</div>
```

通过 CSS 变量 `--healthy-deg` / `--warning-deg` 控制 conic-gradient 比例。

### 3.3 折叠 details

```html
<details class="card-folding">
    <summary>折叠标题</summary>
    <div class="card-body">内容</div>
</details>
```

CSS 用 `border-right + border-bottom` 画三角（避免 Unicode 字符渲染问题），关闭 → 旋转 90°。

### 3.4 长表格限高

```html
<div class="scroll-y-260">
    <table>...</table>
</div>
```

深色滚动条样式见 `style.css`。

### 3.5 状态指示

```html
<span class="status-dot-small status-healthy"></span>  <!-- 绿 -->
<span class="status-dot-small status-warning"></span>  <!-- 黄 -->
<span class="status-dot-small status-stale"></span>    <!-- 红 -->
```

## 4. 禁止事项（避免重蹈覆辙）

### 4.1 ❌ HTML `onclick` 属性嵌套引号

```html
<!-- ❌ 错 -->
<button onclick="toggleAccount(1, "active", this)">启用</button>

<!-- ✅ 对: 用 data-action + delegated listener -->
<button data-action="enable" data-id="1">启用</button>

// app.js
document.addEventListener('click', (e) => {
    const btn = e.target.closest('button[data-action]');
    if (!btn) return;
    const fn = {enable: toggleAccount, ...}[btn.dataset.action];
    fn(parseInt(btn.dataset.id, 10), 'active', btn);
});
```

### 4.2 ❌ Unicode 字符做图标

```css
/* ❌ 错 - 某些字体渲染为 ? */
content: '▾';
content: '◆';
content: '►';
```

```css
/* ✅ 对 - 用 CSS border 画三角（任何字体可渲染） */
content: '';
border-right: 2px solid var(--text-muted);
border-bottom: 2px solid var(--text-muted);
transform: rotate(-45deg);
```

或者用**内联 SVG**（data URI）：

```css
content: url("data:image/svg+xml;utf8,<svg ...>");
```

### 4.3 ❌ 假设 data 字段非空

```js
// ❌ 错 - null 时崩
m.model.toLowerCase()  // if m.model is null → TypeError
```

```js
// ✅ 对 - null 兜底
const modelName = m.model || "(未知)";
// 或更严格: modelName = m.model ? m.model : "(未知)";
```

### 4.4 ❌ 假设 htmx 默认渲染 HTML

```html
<!-- htmx 默认 innerHTML 写回响应, JSON 会显示原始字符串 -->
<div id="cache-by-channel" hx-get="/dashboard/api/cache/stats"></div>
```

```html
<!-- ✅ 对 - JS 接管渲染, 或后端返回 HTML 片段 -->
<div id="cache-by-channel"></div>
```

```js
// app.js
async function loadCacheStatsList() {
    const resp = await authFetch("/dashboard/api/cache/stats?days=7");
    const data = await resp.json();
    document.getElementById("cache-by-channel").innerHTML = renderTable(data);
}
```

### 4.5 ❌ 删除旧函数 DOMContentLoaded hook 但保留引用

```js
// ❌ 错 - loadDataFreshness 引用 df-total (新 HTML 里没这个 ID)
document.addEventListener('DOMContentLoaded', loadDataFreshness);
```

```js
// ✅ 对 - 删除 hook 同时更新 HTML IDs, 或保留向后兼容代码
document.getElementById('df-total') && document.getElementById('df-total').textContent = total;
// 或直接删除 hook
```

## 5. JS 模块边界

### 5.1 启动钩子

```js
// DOMContentLoaded: 一次加载
document.addEventListener('DOMContentLoaded', loadOverviewKPIs);
document.addEventListener('DOMContentLoaded', loadCacheStatsList);

// 定时刷新: 60s
setInterval(() => {
    loadCacheStatsList();
    loadSuggestionsList();
}, 60000);
```

### 5.2 authFetch (统一鉴权)

```js
async function authFetch(url, opts = {}) {
    const headers = { ...(opts.headers || {}) };
    const token = readCookie('aimc_dashboard_token');
    if (token) headers['Authorization'] = 'Bearer ' + token;
    return fetch(url, { ...opts, headers });
}
```

## 6. 修改 checklist

改 dashboard 前端前必查：

- [ ] **CSS 变量**：新颜色用 `var(--color-xxx)`，不用硬编码 `#xxx`
- [ ] **class 名语义化**：用 `.cache-row` / `.account-form` 而不是 `.box1` / `.row-2`
- [ ] **JS 错误处理**：所有 `await` 必须 `try/catch` 或显式 `.catch()`
- [ ] **数据字段兜底**：`data.field || "(未知)"` 而不是 `data.field`
- [ ] **响应式**：新增卡片在 3 列 / 2 列 / 单列 都测试
- [ ] **快捷键冲突**：新快捷键不要跟 `r/a// /?` 重叠
- [ ] **不要修改后端**：纯前端改动不应改 `dashboard/routes.py`
- [ ] **74 tests 仍跑通**：前端改完 `python -m pytest tests/` 验证后端没坏

## 7. 已知 UI 模式（参考 DeepSeek/Claude）

| 元素 | 模式 |
|------|------|
| 状态栏 (sticky) | 顶部 + 实时路由信息 + 状态点 ● |
| 主区卡片 | 网格 4 列 KPI + sparkline + 折线图 |
| 折叠 details | 折叠次要卡片（不抢占屏幕）|
| 快捷键 | 顶部 footer 显示 + 实际生效 |
| 表格 | alternating rows + monospace 数字 + sticky header |

## 8. 调试

```bash
# 看后端 API 数据
curl -s http://localhost:8080/dashboard/api/cache/stats?days=7 | python3 -m json.tool

# 浏览器控制台查看 JS 错误
F12 → Console

# 跑测试确认后端没破
python -m pytest tests/ -v

# 检查特定模块
python -m pytest tests/test_quota_check.py -v
```

## 9. 相关文件

- `dashboard/static/index.html` — 主入口
- `dashboard/static/style.css` — 全部样式 (CSS 变量 + 组件)
- `dashboard/static/app.js` — 全部前端逻辑
- `dashboard/static/vendor/` — htmx + chart.js + marked.min.js (本地化)
- `dashboard/routes.py` — 后端 API (Django/FastAPI 风格 FastAPI)

## 10. 改动记录

| 版本 | 改动 | commit |
|------|------|--------|
| v1 | 初次重写 (深色 + 3 列 + KPI) | 4a28b44 |
| v2 | 修复旧 JS 死引用 | 5c46df3 |
| v3 | 修复 style.css 实际未更新 | 30783e6 |
| v4 | KPI 4 列 + 限额预警紧凑 | 1d30544 |
| v5 | 表格样式 + 周报 markdown | e65eb0a |
| v6 | 折叠图标 + 空 model + 加账号表单 | c871e7b |
| v7 | 限高 + 按钮反馈 + 文档 | (本次) |