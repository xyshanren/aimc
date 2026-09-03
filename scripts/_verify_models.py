"""Verify /v1/models returns groups by default."""
import json
import sys
import urllib.request
from urllib.parse import urlparse


def fetch(url):
    # V6.1 (Mimosa 清理): SSRF 防护 — 本脚本只允许打本机回环目标
    parsed = urlparse(url)
    if parsed.scheme != "http" or (parsed.hostname or "").lower() not in ("localhost", "127.0.0.1"):
        raise ValueError(f"blocked non-loopback url: {url!r}")
    with urllib.request.urlopen(url) as r:
        return json.loads(r.read())


# Default (only groups)
data = fetch("http://localhost:8080/v1/models")
items = data.get("data", [])
groups = [m for m in items if m.get("aimc_metadata", {}).get("type") == "group"]
models = [m for m in items if m.get("aimc_metadata", {}).get("type") == "model"]
print(f"=== /v1/models (default) ===")
print(f"total: {len(items)} (groups={len(groups)}, models={len(models)})")
print(f"include_models flag: {data.get('include_models')}")
print("first 5 group ids:")
for g in groups[:5]:
    print(f"  - {g['id']}")

# include_models=true
data2 = fetch("http://localhost:8080/v1/models?include_models=true")
items2 = data2.get("data", [])
groups2 = [m for m in items2 if m.get("aimc_metadata", {}).get("type") == "group"]
models2 = [m for m in items2 if m.get("aimc_metadata", {}).get("type") == "model"]
print(f"\n=== /v1/models?include_models=true ===")
print(f"total: {len(items2)} (groups={len(groups2)}, models={len(models2)})")
print(f"include_models flag: {data2.get('include_models')}")
print("first 5 model ids:")
for m in models2[:5]:
    print(f"  - {m['id']}")