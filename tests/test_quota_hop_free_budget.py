"""V6.x quota-hop 不烧预算测试 (2026-11-01)

机制: quota_exhausted 类失败是确定性失败 (渠道配额耗尽不会因等待自愈),
failover 换道不消耗 retry 预算 (_failover_next_action 返回 "free"); 普通
retryable 失败 (5xx/限流/网络) 消耗预算 ("budget"); 预算耗尽或 quota-hop
撞硬帽 → "final" 落终态 (V6.18 真因回放)。

背景: 生产 2026-11-01 实录 — minimax 周限额缺位期, modelscope 日额首次
触顶的漏出窗内, 2 跳烧完于付费兜底前, 一批客户端可见 429。

跑法: python -m pytest tests/test_quota_hop_free_budget.py -v
"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from gateway.routes import _failover_next_action


def test_quota_hop_is_free():
    """quota_exhausted 失败 → free (不消耗预算), 即使预算已耗尽"""
    assert _failover_next_action("quota_exhausted", 0, 2, 1, 7) == "free"
    assert _failover_next_action("quota_exhausted", 2, 2, 3, 7) == "free"


def test_quota_hop_hard_cap():
    """quota-hop 撞硬帽 → final (防御; 正常由候选耗尽先触发)"""
    assert _failover_next_action("quota_exhausted", 0, 2, 7, 7) == "final"


def test_other_retryable_consumes_budget():
    """5xx/限流类失败 → budget (消耗一次)"""
    assert _failover_next_action("upstream_5xx", 0, 2, 1, 7) == "budget"
    assert _failover_next_action("rate_limited", 1, 2, 2, 7) == "budget"


def test_budget_exhausted_is_final():
    """预算耗尽 (attempt == max_retries) → final, 落 V6.18 真因回放"""
    assert _failover_next_action("upstream_5xx", 2, 2, 3, 7) == "final"
    assert _failover_next_action("network_transient", 2, 2, 5, 7) == "final"


def test_budget_left_even_at_hard_cap():
    """非 quota 类失败不受硬帽约束 (预算是它的约束), 预算内仍可换道"""
    assert _failover_next_action("upstream_5xx", 1, 2, 7, 7) == "budget"
