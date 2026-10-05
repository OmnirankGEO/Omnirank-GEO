"""判据 · WO-A ③:DLP-A 扫描器四个洞,每类一条**正样本**。

## 为什么每类都必须有正样本

加宽 pattern 而不配正样本 = 加宽了没人验:pattern 写错一个字、
或者压根没被 :func:`scan_facade_payload` 调用,结果与「真的很干净」完全同形。
这一份的每条正样本都是**基线实测逃逸**的那一串,不是想象出来的:

| 类 | 基线判绿的负样本 | 逃掉的原因 |
|----|------------------|-----------|
| 供应商名 | ``{"explanation": "这条走快易播发布"}`` | 继承来的 ``_SUPPLIER_TERMS`` 只有模型/LLM 厂商,一个渠道供应商都没有 |
| 采购价 | ``{"explanation": "采购价 12 元"}`` | ``cny_cost_word`` 要求「采购」后**紧跟数字**,真实写法中间隔着「价」 |
| 上游单号 | ``{"order_sn": "MJH-88123"}`` | ASCII 那条只认 ``<prefix>_order_id``,而现役字段叫 ``order_sn`` |
| provider_route | ``{"detail": "provider_route=kyb_v2"}`` | 基线只比对**键名**,写在值里就流出去了 |

🔴 毒串**硬编码在本文件里**,不从被测模块的常量读 ——
负样本与被测逻辑共用同一份真值 = 两边一起错还一起绿(模块 docstring 自己写着这条)。
"""

from __future__ import annotations

import pytest

from services.xiaobang_facade_dlp import (
    FacadeDlpLeak,
    assert_facade_clean,
    dlp_term_inventory,
    scan_facade_payload,
)

#: 四类各一条正样本(基线实测逃逸)+ **该由哪条规则判红**。
#:
#: 🔴 第三列不是装饰:第一版只断言「有命中」,于是 ``provider_route=kyb_v2``
#:    这个样本被**供应商名**那条顺手判红了 —— 值里那条规则整个删掉判据照样绿
#:    (变异 N14 存活实测)。样本必须能把要验的那条规则单独驱动起来。
POISON_BY_CLASS: tuple[tuple[str, dict, str], ...] = (
    ("供应商名", {"explanation": "这条走快易播发布"}, "渠道供应商身份"),
    ("供应商名-机读键", {"status_url": "https://ops.internal/kyb/order/1"}, "渠道供应商身份"),
    ("采购价", {"explanation": "采购价 12 元"}, "procurement_price"),
    ("上游单号-键", {"order_sn": "MJH-88123"}, "上游标识/凭据形态"),
    ("上游单号-中文", {"explanation": "外部单号 KYB88123 已回填"}, "上游标识/凭据形态"),
    ("provider_route-值", {"detail": "provider_route=alpha_v2"}, "值里出现内部路由字段名"),
)

#: 反向对照:干净 DTO 必须保持绿。少了它,「一律判红」也能让上面全过。
CLEAN_SAMPLES: tuple[dict, ...] = (
    {"label": "发布到媒体", "explanation": "预计 3 天内上线,完成后这里会变成已发布"},
    {"intent_id": "int_20260820_0001", "state": "prepared",
     "primary_label": "确认发布", "quote_note": "本次消耗 130 算力"},
    {"explanation": "这台设备主频 100mhz"},   # mhz 前面有数字 → 不是渠道名
)


@pytest.mark.parametrize("name,payload,reason", POISON_BY_CLASS,
                         ids=[n for n, _, _ in POISON_BY_CLASS])
def test_each_leak_class_is_caught_by_its_own_rule(name, payload, reason):
    problems = scan_facade_payload(payload)
    assert problems, "{0} 这一类仍然逃得掉:{1}".format(name, payload)
    assert any(reason in p for p in problems), (
        "命中了,但不是该命中的那条规则({0} 应由 {1} 判红):{2}".format(name, reason, problems))


@pytest.mark.parametrize("name,payload,reason", POISON_BY_CLASS,
                         ids=[n for n, _, _ in POISON_BY_CLASS])
def test_the_gate_actually_raises_on_each_class(name, payload, reason):
    """扫描器命中 ≠ 出口闸会抛。闸是唯一真出口,判据要打到闸上。"""
    with pytest.raises((FacadeDlpLeak, Exception)) as exc:
        assert_facade_clean(payload, where="wo_a_probe")
    assert exc.value is not None


@pytest.mark.parametrize("payload", CLEAN_SAMPLES)
def test_clean_payloads_stay_green(payload):
    assert scan_facade_payload(payload) == []
    assert_facade_clean(payload, where="wo_a_probe")


def test_inventory_proves_the_new_rules_are_not_an_empty_table():
    """活性自证:空规则表的扫描器与「真的很干净」结果完全一样。"""
    inventory = dlp_term_inventory()
    assert inventory["channel_supplier_patterns"] > 0, inventory
    # 原有四类同样不许塌:这份清单本身就是分母。
    for key in ("inherited_terms", "upstream_patterns", "raw_error_patterns",
                "cost_patterns", "internal_route_fields"):
        assert inventory[key] > 0, (key, inventory)


def test_route_field_names_come_from_one_table_not_two():
    """键名判与值判必须共用同一份字段表 —— 抄两份必然漂。"""
    from services.xiaobang_facade_dlp import _HTTP_METHOD_FIELD, _INTERNAL_ROUTE_IN_VALUE

    for field in _HTTP_METHOD_FIELD:
        assert _INTERNAL_ROUTE_IN_VALUE.search("x {0}=1".format(field)), field
