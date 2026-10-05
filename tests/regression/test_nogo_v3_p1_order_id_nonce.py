"""[对抗审核 v3 P1 CONFIRMED] managed_campaign 扣费 order_id 必须唯一(带 nonce)· 判别性结构测试。

对抗审核发现:v3 精确退款按 charge_tx_id→order_id 组退,前提是 order_id 每笔唯一。confirm 路径已加
shortuuid nonce(R1-CAN-069),但 adjust/topup/brand 三路 order_id 仅到【秒】无 nonce → 同秒两笔扣费
order_id 相同 → 精确退款组查询命中两笔 → 超额退款(平台净亏)。
v3 修复:三路 order_id 全部补 shortuuid nonce。

判别性:去掉任一 order_id 的 nonce → 本测试失败。
"""
from __future__ import annotations
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SRC = (ROOT / "api" / "managed_campaign_api.py").read_text(encoding="utf-8")


def test_all_deduct_order_ids_have_nonce():
    """每个 order_id=f"...:{int(...timestamp())}..." 构造都必须带 shortuuid nonce,防同秒撞号。"""
    # 抓所有 order_id=f"..." 且含 timestamp() 的构造
    patterns = re.findall(r'order_id=f"[^"]*int\(datetime\.now\(\)\.timestamp\(\)\)[^"]*"', SRC)
    assert patterns, "未找到 order_id 时间戳构造(用例前提失效)"
    for p in patterns:
        assert "shortuuid" in p and "uuid()" in p, \
            f"🔴 order_id 构造缺 nonce → 同秒撞号致精确退款超额退款:{p}"


def test_no_bare_second_granular_order_id():
    """反向:不得存在【仅到秒、无 nonce】的 order_id 结尾(timestamp())}" 直接闭合)。"""
    bare = re.findall(r'order_id=f"[^"]*int\(datetime\.now\(\)\.timestamp\(\)\)\}"', SRC)
    assert not bare, f"🔴 发现仅到秒无 nonce 的 order_id(会被精确退款击穿):{bare}"
