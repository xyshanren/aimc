# flagship glm-5.3 经 taotoken_coding 静默降级为 glm-5.3-flash（已知行为，拍板保持现状）

> 时点：2026-09-25 调查并拍板。这是一条**已知且有意保留**的路由行为留档，不是 bug 修复记录。
> 目的：情况有变时能快速定位问题、按 §4 修复手法处理，不必重新调查。

## 1. 现象

请求日志出现 `model=glm-5.3`、渠道=`taotoken_coding` 的 200 请求（首例：2026-09-25 22:26 三条
zcode `tier:flagship` 请求，~136K tokens，¥0.00），但 taotoken_coding 价行只有
`glm-5.3-flash` 与 `deepseek-v4-flash` 两个模型，且其上游**不支持** glm-5.3 ——
"glm-5.3 通了"是显示假象。

## 2. 机制（为什么会这样）

1. `tier:flagship` 组路由本轮轮到成员 glm-5.3（`model_group_members` priority 110；
   V6.10 LRU 轮换决定何时轮到）。
2. `RouteEngine.select_best_channel` → `get_candidates` 用 `_get_fuzzy_price` 做**模糊价行
   匹配**：`glm-5.3` 前缀命中 taotoken 的 `glm-5.3-flash` 行（`_fuzzy_model_match` 的
   `price_model.startswith(model_lower)` 分支）。
3. 当时 glm_bigmodel 唯一账号 glm_01 = `manual_disabled`（2026-09-16 起有意停用待额度），
   taotoken_coding 是 glm-5.3 唯一活候选。
4. proxy 用 `_channel_model` 改写 body.model → 上游实际收到 **glm-5.3-flash**
   （`request_transcripts.request_body` 实证），上游回包 model=`glm_for_coding`
   （taotoken coding 桶别名）。
5. `usage_logs.model` 在组路由路径记 `_registry_model`（组员规范名 glm-5.3，见
   `proxy.py _resolve_actual_model`）→ 仪表盘"模型"列**看不出降级**。

**上游实证（2026-09-25，taotoken_02 key 直探 `https://api.taotoken.net/coding/v1`）**：

- `GET /models` 仅返回 `deepseek-v4-flash`、`glm-5.3-flash`；
- `POST chat model=glm-5.3` → **400** `Model glm-5.3 is not available on Coding Plan`；
- `POST chat model=glm-5.3-flash` → 200。
- glm-5.3 真身只在 glm_bigmodel（`open.bigmodel.cn/api/coding/paas/v4`）。

## 3. 拍板（2026-09-25）

**保持现状，不修。** 理由：taotoken 渠道量大管饱（订阅桶），flash 顶包阴差阳错保证了
flagship 组在大请求场景有活渠道承接。

## 4. 情况有变时的触发器（满足任一 → 回到本节修复手法）

1. taotoken 不再"管饱"（计费收紧 / 限流 / 配额缩水）—— 顶包失去意义；
2. 需要 glm-5.3 真旗舰品质（flash 输出质量成为实际问题）；
3. 恢复 glm_bigmodel glm_01 并期望 flagship 真跑 glm-5.3 —— **注意：仅恢复账号不够**。
   两渠道该档价行均 0 元，组排序键 `(price, family_bonus, member_priority, -channel.priority)`
   下 taotoken priority=9 > glm=8，仍会先选 taotoken 继续 flash 顶包；
4. 成本口径要求精确：`list_cost_estimate` 按 glm-5.3 标价记（2026-09-25 三条约 1.09 元/条），
   实际执行的 flash 标价更低，成本页轻微高估；
5. taotoken 上架真 glm-5.3 —— 只需给 taotoken_coding 补 glm-5.3 精确价行，降级自动消失
   （现状变"正确"），见手法 D。

## 5. 修复手法（按改动面从小到大）

| 手法 | 改动 | 备注 |
|------|------|------|
| **D. taotoken 上架 glm-5.3** | 给 taotoken_coding 加 glm-5.3 精确价行 | 首选（若上游真支持）； fuzzy 命中精确行，请求不再降级 |
| **A. 组路由候选禁 fuzzy** | `gateway/router.py` 组路径 `_get_fuzzy_price` → 精确匹配 | 语义最正确：glm-5.3 无活候选即跳过成员，由 deepseek-v4-pro 等承接。注意 fuzzy 的历史用途（删行后仍可路由/命名空间差异），需评估对其他模型影响 |
| **B. taotoken flash 价行定正价** | 打破 0 元 tie，让 glm_bigmodel 精确行赢 | 影响面小；副作用：flash 请求成本页开始显示费用 |
| **C. 降 taotoken_coding priority < 8** | channels.priority | 全局渠道排序变动，牵连大，不推荐 |

**排查/复跑工具**（调查用临时脚本已清理未入库；复跑时在生产机 aliy-hy 按下述重写即可）：

```bash
# 上游探测: key 从密文解密只进变量不回显 (XOR+Base64, master key 在 /opt/aimc/.env)
KEY=$(python3 - <<'EOF'
import base64, sqlite3
mk = [l.split('=',1)[1].strip() for l in open('/opt/aimc/.env') if l.startswith('AIMC_MASTER_KEY=')][0]
row = sqlite3.connect('/opt/aimc/data/aimc.db').execute(
    "SELECT api_key_ciphertext FROM accounts WHERE channel_id=8 AND account_name='taotoken_02'").fetchone()
kb = bytes.fromhex(mk)
print(bytes(b ^ kb[i % len(kb)] for i, b in enumerate(base64.b64decode(row[0]))).decode())
EOF
)
curl -sS -H "Authorization: Bearer $KEY" https://api.taotoken.net/coding/v1/models
curl -sS -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"model":"glm-5.3","messages":[{"role":"user","content":"hi"}],"max_tokens":16}' \
  https://api.taotoken.net/coding/v1/chat/completions
```

DB 侧查证：价行 / `usage_logs`（`group_id` 字段区分组路由）/ `request_transcripts` /
组成员 / 冷却事件，均为 sqlite 只读查询（库 `/opt/aimc/data/aimc.db`）。

**查证要点**：`request_transcripts.request_body` 的 `model` 字段才是发给上游的真身；
`usage_logs.model` 是组员规范名（组路由下 ≠ 上游模型）。
