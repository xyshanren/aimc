# siliconflow 规范名价行压住 raw id 行 → glm-5.3-flash 全 400 (2026-10-26)

## 现象

tier:vision 的 glm-5.3-flash 流量打到 siliconflow_aggregator (ch3) 全部
400 `{"code":20012,"message":"Model does not exist. Please check it carefully."}`,
失败链显示 `#3 siliconflow_aggregator`, 渠道被 MODEL_UNSUPPORTED 分类
(needs_human + 1800s 冷却) 反复拿下。usage_logs 近 20 天该渠道×该模型
**零成功全 400**。

## 根因

- ch3 同模型有两套价行: raw 行 `zai-org/GLM-5.3-Flash` (10-13 api 同步) 与
  **手工规范名行 `glm-5.3-flash` (10-14 23:58 manual, raw_model_id=NULL)**。
- 路由取价 `_get_current_price` 精确字符串匹配 → 只命中规范名行; 存在精确行时
  raw 行的 fuzzy 兜底不跑。
- `_channel_model = raw_model_id or model` (gateway/router.py) → NULL 回退到
  规范名 → 与请求同名 → proxy `_replace_body_model` 跳过改写 → 规范名裸发上游。
- siliconflow 上游严格按 raw id (org/name) 寻址: 直探实证
  `zai-org/GLM-5.3-Flash` 200 / `glm-5.3-flash` 400 20012。上游模型与 key 均正常。

回归时间点 = 10-14 手工补规范名行的瞬间; 之前仅 raw 行时 fuzzy 命中会正确改写。

## 修复 (三件套)

1. **生产数据**: ch3 全部 14 条规范名行 (deepseek/qwen/glm/paddleocr) 按
   同渠道 raw 兄弟行回填 raw_model_id。路由缓存有 db mtime 监听 (~60s 内自动
   刷新), 免重启生效。e2e: `qwen3-8b` 经网关 200, 上游收到 `Qwen/Qwen3-8B`。
2. **护栏 (本提交)**: 价格管理三端点支持 raw_model_id 字段;
   `_RAW_ID_STRICT_PROVIDERS = {"siliconflow"}` 渠道上规范名行缺 raw_model_id →
   有 raw 兄弟行自动回填, 无则 422 拒绝。GET /api/prices 补 raw_model_id 字段。
   扩充 strict 集合须有"规范名裸发上游即 400"的生产证据 (modelscope/minimax
   实测接受规范名, 不入集合)。
3. **附带发现**: .env 从未有过生效的 SILICONFLOW_API_KEY (只有注释行), 而每日
   model_list 同步 (refresh_siliconflow_models.load_sf_key) 只读 .env → 同步
   一直在坏。已把库内解密 key 写入 .env 并验证 /models 拉通 (98 个)。

## 经验

- 聚合渠道 (上游要 raw id) 上补规范名价行, **必须**同时带 raw_model_id, 否则
  规范名行的精确命中会压死 raw 行的 fuzzy 改写 — 报错还是上游的
  "Model does not exist", 与 key/退役无关, 极易误判成 SF_RETIRED_MODEL_IDS 一类。
- 同类排查入口: request_transcripts.request_body 的 model 字段是规范名还是
  raw id, 一眼定位改写是否发生。
- 探活 GET /models 200 与推理可用性无关 (列表不作数), 但本例中 raw id 确实
  仍在列表 — 400 是名字翻译断链, 不是退役。
