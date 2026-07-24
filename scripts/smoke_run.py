"""单 source 跑一次 smoke：fetch → clean → diff → parse

绕过 scheduler + healer，直接看单次采集的中间产物。
试用阶段调试用，**不写 DB**（避免污染数据）。

用法：
    python scripts/smoke_run.py deepseek
    python scripts/smoke_run.py deepseek --source-type announcement
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


async def smoke_run(provider: str, source_type: str = None):
    from db.init_db import init_db
    from intelligence.sources import get_source_by_provider
    from intelligence.fetcher import fetch_page_with_error
    from intelligence.cleaner import clean_html
    from intelligence.differ import compute_hash, compute_similarity
    from db.engine import async_session_factory
    from db import crud
    from sqlalchemy import select
    from db.models import IntelSource

    await init_db()

    sources = get_source_by_provider(provider)
    if source_type:
        sources = [s for s in sources if s.source_type == source_type]

    if not sources:
        print(f"❌ 无 provider={provider} source_type={source_type} 的 source")
        return

    print(f"=== Smoke run: {provider} ({len(sources)} source) ===\n")

    for src in sources:
        print(f"--- {src.provider}/{src.source_type} ---")
        print(f"  URL: {src.url}")
        print(f"  poll_interval: {src.poll_interval}s")

        # 1. fetch
        print("  [1/4] fetch...", end="", flush=True)
        content, error = await fetch_page_with_error(src.url)
        if not content:
            print(f" ❌ FAILED: {error}")
            continue
        print(f" ✅ {len(content)} chars")

        # 2. clean
        print("  [2/4] clean HTML → Markdown...", end="", flush=True)
        try:
            cleaned = clean_html(content)
            print(f" ✅ {len(cleaned)} chars (压缩 {100*(1-len(cleaned)/len(content)):.1f}%)")
        except Exception as e:
            print(f" ❌ {e}")
            cleaned = content

        # 3. diff
        print("  [3/4] diff vs last content...", end="", flush=True)
        new_hash = compute_hash(content)
        async with async_session_factory() as session:
            db_src = await crud.get_intel_source_by_provider_url(session, src.provider, src.url)
            old_hash = db_src.last_hash if db_src else None
            old_content = db_src.last_content if db_src else None

        if not old_hash:
            print(f" 🆕 首次（无历史）")
        elif old_hash == new_hash:
            print(f" ⏭️  无变化（hash 相同）")
        else:
            similarity = compute_similarity(content, old_content or "")
            print(f" 🔄 有变化（相似度 {similarity:.2%}）")

        # 4. parse (调 AI，省钱可注释)
        print("  [4/4] parse (AI)...", end="", flush=True)
        if not old_hash or old_hash != new_hash:
            try:
                from intelligence.parser import parse_content
                result = await parse_content(cleaned, src.provider, src.url)
                flag = "🟡 标黄" if result.confidence < 0.6 else "🟢 正常"
                print(f" ✅ is_promotion={result.is_promotion} conf={result.confidence:.2f} {flag}")
                if result.title:
                    print(f"      title: {result.title[:80]}")
                if result.discount_desc:
                    print(f"      discount: {result.discount_desc}")
            except Exception as e:
                print(f" ❌ {e}")
        else:
            print(" ⏭️  skipped (无变化)")

        print()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python scripts/smoke_run.py <provider> [source_type]")
        print("  e.g.: python scripts/smoke_run.py deepseek")
        print("        python scripts/smoke_run.py deepseek announcement")
        sys.exit(1)

    provider = sys.argv[1]
    source_type = sys.argv[2] if len(sys.argv) > 2 else None
    asyncio.run(smoke_run(provider, source_type))
