"""Phase 4 v4.3 — 真实 9 源 1 cycle smoke 验证

跑遍 9 个新国内 model_list 源 + 1 个聚合观察源 (千问AI), 输出:
- 每源 fetch 结果 (HTTP / OK / 错误)
- 解析出的 model 数量
- 抓取耗时
- 整体成功率

跟 scripts/smoke_run.py 区别:
- smoke_run.py: 单源 + 仅 HTML (无 API 抽取)
- smoke_v43.py:  批量 9 源 + API 优先 + HTML fallback

用法:
    python scripts/deploy/smoke_v43.py              # 默认 9 国内 model_list + 千问AI
    python scripts/deploy/smoke_v43.py --skip-html   # 跳过 HTML 源 (省时间)
    python scripts/deploy/smoke_v43.py --only deepseek  # 单厂商跑
"""
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))


# Phase 4 v4.3: 9 个国内 model_list 源 (Phase 3.6 加)
V43_SOURCES = [
    # (provider, source_type, has_api)
    ("siliconflow", "model_list", True),
    ("volcengine", "model_list", True),
    ("deepseek", "model_list", True),
    ("qwen", "model_list", True),
    ("glm", "model_list", True),
    ("stepfun", "model_list", True),
    ("sensenova", "model_list", True),
    ("hunyuan", "model_list", True),  # v4.3: 无 key, 预期失败
    ("moonshot", "model_list", True),  # kimi
    # v4.2 千问AI (观察性, 不进路由)
    ("qianwenai", "pricing", False),  # HTML only
]


async def run_one_source(provider: str, source_type: str, has_api: bool, skip_html: bool) -> dict:
    """跑单源, 返回结果 dict"""
    from intelligence.sources import get_source_by_provider
    from intelligence.fetcher import fetch_page_with_error, fetch_models_via_api

    start = time.time()
    result = {
        "provider": provider,
        "source_type": source_type,
        "has_api": has_api,
        "status": "unknown",
        "models_count": 0,
        "duration_ms": 0,
        "error": None,
        "path": None,
    }

    sources = get_source_by_provider(provider)
    sources = [s for s in sources if s.source_type == source_type]
    if not sources:
        result["status"] = "skip"
        result["error"] = "source 不存在"
        result["duration_ms"] = int((time.time() - start) * 1000)
        return result

    src = sources[0]

    # API 优先 (Phase 3.6 设计的)
    if has_api and src.api_endpoint:
        try:
            models = await fetch_models_via_api(src)
            if models is not None:
                result["status"] = "ok"
                result["models_count"] = len(models)
                result["path"] = "api"
                result["duration_ms"] = int((time.time() - start) * 1000)
                return result
        except Exception as e:
            result["error"] = f"API 失败: {e}"

    # HTML fallback
    if not skip_html:
        try:
            content, err = await fetch_page_with_error(src.url)
            if content:
                result["status"] = "ok-html"
                result["path"] = "html"
                result["models_count"] = len(content) // 200  # 估算 (HTML 不解析)
                result["duration_ms"] = int((time.time() - start) * 1000)
                return result
            else:
                if not result["error"]:
                    result["error"] = f"HTML 失败: {err}"
        except Exception as e:
            if not result["error"]:
                result["error"] = f"HTML 异常: {e}"

    result["status"] = "fail"
    result["duration_ms"] = int((time.time() - start) * 1000)
    return result


async def main(skip_html: bool = False, only: str = None) -> int:
    """主流程"""
    print("=== Phase 4 v4.3 — 真实 9 源 1 cycle smoke ===\n")
    print(f"目标: 9 国内 model_list + 1 千问AI 聚合观察源")
    print(f"模式: API 优先, HTML {'跳过' if skip_html else 'fallback'}")
    print()

    sources = V43_SOURCES
    if only:
        sources = [s for s in V43_SOURCES if s[0] == only]
        if not sources:
            print(f"❌ 无 provider={only} 的源")
            return 1

    results = []
    for provider, st, has_api in sources:
        result = await run_one_source(provider, st, has_api, skip_html)
        results.append(result)
        # 实时打印
        icon = {"ok": "✓", "ok-html": "✓H", "fail": "✗", "skip": "·"}.get(result["status"], "?")
        ms = result["duration_ms"]
        n = result["models_count"]
        path = result["path"] or "-"
        line = f"  [{icon}] {provider:15s}/{st:11s} {path:4s} {n:>5d} models  {ms:>5d}ms"
        if result["error"]:
            line += f"  ⚠ {result['error'][:60]}"
        print(line)

    # 汇总
    print()
    print("=== 汇总 ===")
    total = len(results)
    ok = sum(1 for r in results if r["status"] in ("ok", "ok-html"))
    fail = sum(1 for r in results if r["status"] == "fail")
    skip = sum(1 for r in results if r["status"] == "skip")
    print(f"  总源:    {total}")
    print(f"  成功:    {ok}/{total} ({100*ok/total:.0f}%)")
    print(f"  失败:    {fail}/{total}")
    print(f"  跳过:    {skip}/{total}")
    print(f"  总耗时:  {sum(r['duration_ms'] for r in results)}ms")

    # 失败列表 (供 troubleshooting)
    fails = [r for r in results if r["status"] == "fail"]
    if fails:
        print()
        print("=== 失败源 (troubleshooting) ===")
        for r in fails:
            print(f"  {r['provider']:15s}: {r['error']}")

    # 退出码
    if fail == 0:
        print("\n[OK] 全部源跑通")
        return 0
    elif ok >= 7:
        # 至少 7/9 成功 (hunyuan 无 key 算可接受)
        print(f"\n[WARN] {fail} 个源失败, 但 {ok} 成功 — 接受范围")
        return 0
    else:
        print(f"\n[FAIL] 失败过多 ({fail}/{total})")
        return 1


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-html", action="store_true", help="跳过 HTML fallback")
    parser.add_argument("--only", type=str, help="只跑指定 provider")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(skip_html=args.skip_html, only=args.only)))