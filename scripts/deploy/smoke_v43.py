"""Phase 4 v4.3 — 真实 9 源 1 cycle smoke 验证

跑遍 9 个新国内 model_list 源 + 1 个聚合观察源 (千问AI), 输出:
- 每源 fetch 结果 (HTTP / OK / 错误)
- 解析出的 model 数量
- 抓取耗时
- 整体成功率

用法:
    python scripts/deploy/smoke_v43.py              # 默认 9 国内 model_list + 千问AI
    python scripts/deploy/smoke_v43.py --only deepseek  # 单厂商跑
"""
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from dotenv import dotenv_values


def _load_env_api_key(provider: str) -> str:
    """从 .env 读 API key (大写 + _API_KEY), 清洗行内注释"""
    env = dotenv_values(Path(__file__).parent.parent.parent / ".env")
    key = (env.get(f"{provider.upper()}_API_KEY", "") or "").strip()
    if " #" in key:
        key = key.split(" #", 1)[0].rstrip()
    return key


# Phase 4 v4.3.9: 9 个国内 model_list 源
# (provider, source_type, has_api, tier)
#   tier: "主力" / "观察" / "mock"
V43_SOURCES = [
    ("siliconflow", "model_list", True, "主力"),     # 主力 (有免费额度)
    ("volcengine", "model_list", True, "观察"),      # 字节方舟, 几乎无免费, 降级观察
    ("deepseek", "model_list", True, "主力"),        # 主力 (按量付费)
    ("qwen", "model_list", True, "观察"),            # 阿里云百炼, 观察免费试用
    ("glm", "model_list", True, "主力"),             # 主力
    ("stepfun", "model_list", True, "主力"),         # 主力 (1月 tokenplan)
    ("sensenova", "model_list", True, "主力"),       # 商汤日日新主力 (自家 + deepseek v4 flash 免费)
    ("hunyuan", "model_list", True, "mock"),         # 腾讯混元, 无 key, mock
    ("moonshot", "model_list", True, "主力"),        # kimi, 主力
    ("qianwenai", "pricing", False, "观察"),         # 千问AI 聚合观察, 仅 HTML
]


async def run_one_source(provider: str, source_type: str, has_api: bool, tier: str) -> dict:
    """跑单源, 返回结果 dict"""
    from intelligence.sources import get_source_by_provider
    from intelligence.sources.api_models import fetch_models_via_api, FetchApiError

    start = time.time()
    result = {
        "provider": provider,
        "source_type": source_type,
        "tier": tier,
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
        api_key = _load_env_api_key(provider) if src.api_key_from else None
        try:
            models = await fetch_models_via_api(src.api_endpoint, api_key=api_key)
            if models:
                result["status"] = "ok"
                result["models_count"] = len(models)
                result["path"] = "api"
                result["duration_ms"] = int((time.time() - start) * 1000)
                return result
        except FetchApiError as e:
            status = e.status_code
            if status == 401:
                result["error"] = "401 API key 无效 (检查 .env)"
            elif status == 403:
                result["error"] = "403 鉴权失败"
            elif status == 404:
                result["error"] = "404 endpoint 不存在"
            elif status == 429:
                result["error"] = f"429 限流: {e}"
            else:
                result["error"] = f"API {status}: {e}"
        except Exception as e:
            result["error"] = f"API 异常: {type(e).__name__}: {e}"

    # mock 厂商或无 API 源 (千问AI) 直接 fail
    result["status"] = "fail"
    result["duration_ms"] = int((time.time() - start) * 1000)
    return result


async def main(only: str = None) -> int:
    """主流程"""
    print("=== Phase 4 v4.3 — 真实 9 源 1 cycle smoke ===\n")
    print(f"目标: 9 国内 model_list + 1 千问AI 聚合观察源")
    print(f"模式: API 优先 (Phase 3.6)")
    print()

    sources = V43_SOURCES
    if only:
        sources = [s for s in V43_SOURCES if s[0] == only]
        if not sources:
            print(f"❌ 无 provider={only} 的源")
            return 1

    results = []
    for provider, st, has_api, tier in sources:
        result = await run_one_source(provider, st, has_api, tier)
        results.append(result)
        icon = {"ok": "✓", "fail": "✗", "skip": "·"}.get(result["status"], "?")
        ms = result["duration_ms"]
        n = result["models_count"]
        path = result["path"] or "-"
        line = f"  [{icon}] {provider:15s} [{tier:4s}] {path:4s} {n:>5d} models  {ms:>5d}ms"
        if result["error"]:
            line += f"  ⚠ {result['error'][:60]}"
        print(line)

    # 汇总
    print()
    print("=== 汇总 ===")
    total = len(results)
    ok = sum(1 for r in results if r["status"] == "ok")
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
            print(f"  {r['provider']:15s} [{r['tier']}]: {r['error']}")

    # 退出码
    if fail == 0:
        print("\n[OK] 全部源跑通")
        return 0
    elif ok >= 7:
        # 至少 7/9 成功 (hunyuan mock 算可接受)
        print(f"\n[WARN] {fail} 个源失败, 但 {ok} 成功 — 接受范围")
        return 0
    else:
        print(f"\n[FAIL] 失败过多 ({fail}/{total})")
        return 1


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", type=str, help="只跑指定 provider")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(only=args.only)))
