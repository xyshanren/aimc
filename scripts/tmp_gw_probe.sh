#!/bin/bash
# 临时探测: WSL 默认网关 + Windows 主机服务 (Ollama 11434) 可达性
#
# 背景 (2026-09-21): WSL2 NAT 下 quickstart 探测 Windows 侧 Ollama 失败 —
#   localhost / host.docker.internal / resolv.conf nameserver(lo 上的 DNS 代理) 全不通,
#   但默认网关 IP 可达 Windows 本机服务。
#
# 用法 (Windows Git Bash):
#   MSYS_NO_PATHCONV=1 wsl bash /mnt/<盘>/path/to/aimc/scripts/tmp_gw_probe.sh
#
# 输出默认网关后, 若 11434 返回 200, 该 IP 即可填:
#   HERMES_OLLAMA_HOST=http://<gw-ip>:11434   → /root/.hermes/.env (hermes quickstart 优先读取)
# 注意网关 IP 随 WSL 重启可能漂移, 掉线后重跑本脚本取新值。
GW=$(ip route | awk '/^default/ {print $3}' | head -1)
echo "default gw: ${GW:-NONE}"
ip -4 addr show eth0 | grep inet
if [ -n "$GW" ]; then
  curl -s -o /dev/null -w "gw:11434 -> %{http_code}\n" --max-time 3 "http://${GW}:11434/api/tags" || echo "gw:11434 -> unreachable"
fi