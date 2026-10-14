#!/usr/bin/env bash
# 双远程推送配置 (Gitee + GitHub 同步)
#
# 用法:
#   bash scripts/deploy/setup-dual-remote.sh <github-ssh-url> [gitee-ssh-url]
# 例:
#   bash scripts/deploy/setup-dual-remote.sh git@github.com:XiaoYRecluse/aimc.git
#
# 原理: 在 origin 上配置两条 pushurl, `git push origin master` 一次推两仓;
# fetch 仍走原 URL (默认 Gitee)。幂等 — 重复执行不会产生重复 pushurl。
set -euo pipefail

GITHUB_URL=${1:?用法: setup-dual-remote.sh <github-ssh-url> [gitee-ssh-url]}
GITEE_URL=${2:-$(git remote get-url origin)}

have_pushurl() {
  git remote get-url --all --push origin 2>/dev/null | grep -qx "$1"
}

# 第一次 set-url --add --push 会"替换"隐式 pushurl (fetch url),
# 所以两条都要显式 add。
if ! have_pushurl "$GITHUB_URL"; then
  git remote set-url --add --push origin "$GITHUB_URL"
fi
if ! have_pushurl "$GITEE_URL"; then
  git remote set-url --add --push origin "$GITEE_URL"
fi

echo "=== 当前远程配置 ==="
git remote -v
echo
echo "之后: git push origin master 一次推 Gitee + GitHub。"
echo "注意: 推送非原子 — 一个仓失败不影响另一个, 失败方需单独补推。"
