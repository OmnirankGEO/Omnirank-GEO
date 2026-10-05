"""[F-preblock · Master SSOT v1.8 §4.1 / 手册 §1.6 事故#1] 前端升级预拦回归锁。

生产事故(2026-07-25 Owner 亲测):后端升级墙(ACTIVE_CUSTOMER_COMMERCIAL_BINDING)
已在 WP1 拆除,但 UserManagement.tsx 残留一条**客户端预拦**——有商业归属的用户点
"调整为服务商"时 toast+return,请求根本不到后端。三轮审核未抓到,根因是文案里的
全角引号("服务关系")使精确 grep 扑空。

本锁用**不含引号的独特子串**匹配,防同类 grep 盲区复发:
- 预拦文案必须永久消失;
- 确认弹窗必须携带原子转换语义(绑定→渠道关系),不得回退为"要求当前没有绑定"。
"""
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "frontend/src/pages/Admin/UserManagement.tsx"


def _src() -> str:
    return _SRC.read_text(encoding="utf-8")


def test_no_client_side_promotion_preblock():
    src = _src()
    # 引号无关的独特子串(全角引号 grep 盲区的教训)
    assert "单独结束当前商业服务归属" not in src, (
        "前端升级预拦复活:有商业归属的用户会在客户端被拦,后端原子转换永远收不到请求"
    )


def test_promotion_dialog_carries_atomic_conversion_semantics():
    src = _src()
    assert "原子升级" in src and "渠道进货关系" in src, (
        "升级确认弹窗必须说明真实语义:旧归属同事务转换为渠道关系(§4.1/D2)"
    )
    # 旧的错误语义不得回退
    assert "要求当前没有显式商业服务绑定" not in src


def test_binding_on_provider_guard_is_kept():
    # 反向判别:另一条守卫(给服务商挂商业归属→指去渠道治理)是四类关系分离的正确
    # 语义,本次修复不得误删。
    assert "服务商上下游请使用渠道关系治理" in _src()
