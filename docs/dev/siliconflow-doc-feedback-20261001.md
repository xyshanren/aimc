# SiliconFlow 官方反馈：Qwen3-VL-Embedding/Reranker-8B 文档格式与 API 实现不符

> 拟发送对象：SiliconFlow（硅基流动）官方支持/文档团队
> 撰写日期：2026-10-01
> 复测环境：2026-09-30 ~ 2026-10-01，模型 `Qwen/Qwen3-VL-Embedding-8B` 与 `Qwen/Qwen3-VL-Reranker-8B`，生产网关直连 `https://api.siliconflow.cn/v1`，多次复测结果一致

---

## 摘要

官方文档中 Qwen3-VL-Embedding-8B 的**多模态输入格式与实际 API 行为不符**：文档示例返回 400，实际可用的是另一套未见于文档的简式格式。另有**两个静默降级场景**（HTTP 200 但结果不可用），对接方极易踩坑。恳请修正文档或对齐实现。

## 问题一：文档格式实测 400，实际可用格式未在文档中标注

**按文档格式发起请求（返回 400）：**

```json
{"model": "Qwen/Qwen3-VL-Embedding-8B",
 "input": [{"type": "image_url",
            "image_url": {"url": "data:image/jpeg;base64,<...>"}}]}
```

```json
{"code": 20015, "message": "The parameter is invalid. Please check again.", "data": null}
```

`image/png` 与 `image/jpeg` 两种 data URI 均复现，多日多次复测一致。

**实际可用的简式格式（返回 200）：**

```json
{"model": "Qwen/Qwen3-VL-Embedding-8B",
 "input": [{"image": "data:image/jpeg;base64,<...>"}]}
```

简式格式的实测边界：

| 行为 | 结果 |
|------|------|
| `{"image": "<data URI>"}`（带 `data:<mime>;base64,` 前缀） | 200 ✓ |
| `{"image": "<纯 base64 无前缀>"}` | 400（前缀必须带） |
| `{"image": "<https URL>"}` | 400（只收 data URI，不收 URL） |
| `input` 列表多个 `{"image":...}` 条目批量 | 200 ✓ 多图正常出向量 |

`/v1/rerank` 同理：`query` 侧 `{"image": data URI}`、`documents` 侧图文混合 `{"image"}/{"text"}` 条目均实测可用，与上述简式一致。

## 问题二：两个静默降级场景（200 但结果不可用）

以下两种请求均返回 200，但**图片并没有被编码**，调用方拿到的是"看似成功"的错误结果：

1. **data URI 作为纯字符串传入**：
   `input: ["data:image/png;base64,..."]` → 200，但被按**文本**编码（`usage.image_tokens = 0`，`prompt_tokens` 因 base64 字符膨胀至 2000+）。返回的向量是 base64 字符串的文本向量，不是图片向量。
2. **同一条目 text 与 image 共存**：
   `input: [{"text": "...", "image": "<data URI>"}]` → 200，但**图片被静默忽略**，只编码文本（`image_tokens = 0`）。

建议：这两种情况要么直接返回 4xx 明确报错，要么在响应中携带可判别的标记；至少应在文档中显著标注。

## 建议

1. **修正文档**：将多模态输入示例改为实际实现的简式 `{"image": ...}` 格式，并注明边界（前缀必须带、仅收 data URI）与上述两个静默陷阱；
2. 或**对齐实现**：让 API 支持文档所写的 `image_url` 嵌套格式（与 OpenAI 多模态惯例一致）；
3. **补充 image token 计价说明**：pricing 页对两模型仅标单一输入价（¥0.70/百万 tokens），但响应中 `input_tokens` 与 `image_tokens` 分列计数。请明确 `image_tokens` 是否按同一单价计费，以及图片 → image token 的换算规则（实测 872×527 帧 ≈ 432 image_tokens，1×1 px ≈ 4）。

以上均有生产环境复测证据，如需完整请求/响应报文可随时提供。感谢！

---

*反馈方：AIMC LLM 网关运营（siliconflow_aggregator 渠道接入方）*
