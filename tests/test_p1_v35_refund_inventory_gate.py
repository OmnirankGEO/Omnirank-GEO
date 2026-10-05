"""[BUG-P1] V3.5 线上退款凭空灌库存 · 静态守护

根因:_handle_v35_factory_refund 只处理 source='recharge' 线上单。线上结算走
allocate_to_customer(净0双流水:purchase_auto +N → allocate -N),代理 paid_inventory
从未真减。退款时三处(A/B/C 态)无条件 revoke_from_customer → 给代理 paid_inventory
凭空 +N(allocate_to_customer 那 -N 不会因退款回来,但 revoke 又 +N)= 凭空注入库存。
仅线下分配(allocate_to_customer_offline · 库存真减 N)退款才需回流库存。

修:入口按 EXISTS(allocate_to_customer_offline AND related_order_id=order) 算 is_offline_alloc;
   三处 revoke_from_customer 各包 if is_offline_alloc(线上净0单恒 False → 跳过)。
prod 实证(2026-06-10 SSH):仅 2 笔线上 allocate_to_customer · 0 笔 revoke · 0 笔 offline
→ bug latent 未触发 · 纯预防(无回收数据)。
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _func_src():
    src = (ROOT / "api" / "referral_api.py").read_text(encoding="utf-8")
    s = src.find("def _handle_v35_factory_refund")
    assert s >= 0, "未找到 _handle_v35_factory_refund"
    e = src.find("\ndef ", s + 10)
    return src[s: e if e > 0 else len(src)]


def test_gate_computed():
    func = _func_src()
    assert "is_offline_alloc" in func, "须计算线下分配闸 is_offline_alloc"
    assert "type = 'allocate_to_customer_offline'" in func, \
        "is_offline_alloc 须按 allocate_to_customer_offline 判定(线上 allocate_to_customer 库存净0)"


def test_all_three_revoke_gated():
    func = _func_src()
    # 每个 revoke_from_customer( 调用前必须有 if is_offline_alloc 闸
    idx = 0
    revoke_positions = []
    while True:
        p = func.find("revoke_from_customer(", idx)
        if p < 0:
            break
        revoke_positions.append(p)
        idx = p + 1
    # 注:import 行 from services.agent_inventory import revoke_from_customer 也含子串,
    # 用调用形式 "revoke_from_customer(" 过滤,但 import 行无括号 → 不计入。
    assert len(revoke_positions) == 3, f"应有 3 处 revoke_from_customer 调用,实际 {len(revoke_positions)}"
    for p in revoke_positions:
        # 向前回看 240 字符内须出现 is_offline_alloc 闸
        window = func[max(0, p - 240): p]
        assert "is_offline_alloc" in window, \
            f"revoke_from_customer 调用(偏移 {p})前 240 字符内未见 is_offline_alloc 闸 → 线上单仍会凭空注入库存"
