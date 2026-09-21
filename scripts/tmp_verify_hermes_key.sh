#!/bin/bash
# 临时验证脚本: hermes/WSL 客户端视角端到端验证 AIMC key + 组路由
#
# 用法 (Windows Git Bash):
#   MSYS_NO_PATHCONV=1 wsl bash /mnt/<盘>/path/to/aimc/scripts/tmp_verify_hermes_key.sh
#
# 覆盖三条路径, 三个都 200 即 key/鉴权/组路由全通:
#   1. GET  /v1/models              (组列表, 客户端启动探测)
#   2. POST /v1/chat/completions tier:light    (轻量档真实路由)
#   3. POST /v1/chat/completions tier:balanced (主力档真实路由)
#
# 注意: 不要把明文 key 写进 -H 'Authorization: Bearer ...' 命令行 —
#       本机安全钩子会把命令里的密钥替换成占位符, 产生假 401。
#       (2026-09-21 实测踩坑, 见 docs/user/troubleshooting.md §2)
# 默认读 /root/.hermes/.env (hermes 侧); 改路径: sed 或改成传参。
set -u
ENV_FILE="${1:-/root/.hermes/.env}"
BASE_URL="${2:-http://127.0.0.1:8080/v1}"

KEY=$(grep "^AIMC_API_KEY=" "$ENV_FILE" | cut -d= -f2-)
echo "env: $ENV_FILE  key len: ${#KEY}  base: $BASE_URL"

# 1) GET /v1/models
curl -s --max-time 8 -H "Authorization: Bearer $KEY" \
  "$BASE_URL/models" -o /tmp/m.json -w "GET /v1/models -> %{http_code}\n"
python3 - <<'PY'
import json
try:
    d = json.load(open('/tmp/m.json'))
    print("groups:", [g["id"] for g in d.get("data_groups", [])][:8])
except Exception as e:
    print("parse:", e, open('/tmp/m.json').read()[:120])
PY

# 2) POST tier:light
curl -s --max-time 60 -X POST "$BASE_URL/chat/completions" \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"model":"tier:light","messages":[{"role":"user","content":"ping"}],"max_tokens":5}' \
  -w "\nPOST tier:light -> %{http_code}\n" | tail -c 350
echo

# 3) POST tier:balanced
curl -s --max-time 60 -X POST "$BASE_URL/chat/completions" \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"model":"tier:balanced","messages":[{"role":"user","content":"ping"}],"max_tokens":5}' \
  -w "\nPOST tier:balanced -> %{http_code}\n" | tail -c 350
echo