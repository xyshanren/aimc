"""Test full chain: _run_source_task + handle_fetch_error (simulates real scheduler)."""
import asyncio
from intelligence.scheduler import _run_source_task
from intelligence.sources import get_all_sources


async def main():
    sources = get_all_sources()
    qwen = [s for s in sources if s.provider == "qwen"][:1]
    if not qwen:
        print("no qwen sources")
        return
    src = qwen[0]
    print(f"running _run_source_task for {src.url}")
    try:
        await asyncio.wait_for(_run_source_task(src), timeout=45)
        print("DONE (full chain ran)")
    except asyncio.TimeoutError:
        print("TIMEOUT")
    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}")


asyncio.run(main())