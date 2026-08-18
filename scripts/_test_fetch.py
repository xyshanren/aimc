"""Quick test: trigger one qwen fetch to see if Chromium + healer works."""
import asyncio
from intelligence.sources import get_all_sources
from intelligence.fetcher import fetch_page_with_error


async def main():
    sources = get_all_sources()
    qwen = [s for s in sources if s.provider == "qwen"][:1]
    if not qwen:
        print("no qwen sources")
        return
    src = qwen[0]
    url = src.url  # KEY FIX: extract url string, not pass source
    print(f"fetching: {url}")
    try:
        content, err = await asyncio.wait_for(fetch_page_with_error(url), timeout=40)
        print(f"len={len(content) if content else 0}, err={err}")
    except asyncio.TimeoutError:
        print("TIMEOUT 40s")
    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}")


asyncio.run(main())