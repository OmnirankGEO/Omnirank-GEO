"""站内 DLP-A(规格 §14.1 · §19.1 #11 · 裁定 P0-D)。

🔴 P0-D 的原话:「扫描器失效与安全在结果上同形」。所以本文件的重心不是
「干净的 payload 能过」,而是:

1. **负样本必中** —— 一组毒串注进 fixture,必须判红;
2. **毒串不从被测模块读** —— 负样本与被测逻辑共用同一份真值,等于两边一起错
   还一起绿。下面的 ``_POISON`` 全是**硬编码字面量**,一个都不来自
   ``services.xiaobang_facade_dlp`` 的常量;
3. **扫描器活性自证** —— 规则表非空;把规则表清空后,同一批毒串必须**变绿**
   (证明是扫描器在抓,而不是别的什么东西碰巧抓到)。
"""

from __future__ import annotations

import pytest

from services import xiaobang_facade_dlp as dlp

# 🔴 硬编码毒串。**禁止**改成从 dlp 模块的常量拼出来 —— 那样负样本会随被测
#    逻辑一起漂,扫描器漏了什么,负样本就跟着漏什么。
_POISON: tuple[tuple[str, dict], ...] = (
    ("供应商名(正文)", {"preview": {"note": "本次由 deepseek 生成"}}),
    ("供应商名(中文)", {"preview": {"note": "走的是豆包通道"}}),
    ("采购成本数字", {"preview": {"note": "采购成本:¥0.29/次"}}),
    ("元换算公式", {"preview": {"tip": "1 元 = 130 积分"}}),
    ("上游单号(键名)", {"meta": {"upstream_order_id": "MJH-88123"}}),
    ("上游单号(中文)", {"preview": {"note": "供应商订单已提交,等回执"}}),
    ("上游凭据", {"meta": {"h": "Authorization: Bearer aaaabbbbccccdddd1111"}}),
    ("原始 provider 错误", {"error": {"detail": "ConnectError: upstream returned 502"}}),
    ("内部 adapter 名", {"op": {"adapters": "publish_image_note_execute"}}),
    ("内网地址", {"meta": {"url": "http://10.0.0.7/internal/dispatch"}}),
    ("HTTP method 字段", {"op": {"http_method": "POST"}}),
    ("STS 凭据", {"meta": {"upload": "access_key_id=AKxxxx"}}),
)

_CLEAN = {
    "intent_id": "xint_AbCdEf1234567890",
    "intent_state": "prepared",
    "operation_id": "publish_center",
    "side_effect": "external",
    "confirmation_policy": {"mode": "required_user_click"},
    # 🔴 [R3-P8 ①] 这份正向对照原来用的是 `object_label` / `destination_label` /
    #    `external_effect` —— **生产端 prepare 一个都不写**。也就是说这条
    #    「干净 payload 能过」证明的是一个不存在的 DTO 很干净。
    #    改成 producer 真写的键(`_frozen_form_prefill` / `_display_object_items`
    #    的产物),这条才真的在证明现役出参干净。
    #    判据:test_no_fixture_injects_a_preview_key_the_producer_never_writes。
    "preview": {
        "customer_label": "QZQZ木作美学定制",
        "object_items": [
            {"resource_kind": "brand", "ref": 152, "label": "QZQZ木作美学定制"},
            {"resource_kind": "article", "ref": 4201, "label": None},
        ],
        "form_prefill": {"brand_id": 152, "article_id": 4201},
        # 出门形状用 `pending_checks`(人话),不是库里存的 `pending_manifest_inputs`
        # 与 `scope` —— 那两个是裸 ASCII 枚举,DLP 判泄漏(R3-P7 ③ 的存量修复)。
        "pending_checks": ["投放账号资格还没核"],
    },
    "compute_quote": {"unit": "算力", "amount": 9360, "pricing_version": "2026-08-18"},
    "status_url": "/api/xiaobang/operations/publish_center/executions/xexe_Ab12/status",
    "recommendation_reasons": [
        "这篇内容适合该渠道受众",
        "最近 90 天你的 3 次发布都正常完成",
    ],
}


def test_clean_payload_passes():
    """正向对照。没有它,「毒串都被抓到」也可能只是因为扫描器恒红。"""
    dlp.assert_facade_clean(_CLEAN, where="test")


@pytest.mark.parametrize("label,payload", _POISON, ids=[p[0] for p in _POISON])
def test_every_poison_sample_is_caught(label, payload):
    with pytest.raises((dlp.FacadeDlpLeak, dlp.InternalTermLeak)):
        dlp.assert_facade_clean(payload, where="test")


def test_scanner_rule_tables_are_not_empty():
    """活性自证:空规则表的扫描器与「真的很干净」完全同形。"""
    inventory = dlp.dlp_term_inventory()
    assert inventory["inherited_terms"] > 0
    assert inventory["upstream_patterns"] > 0
    assert inventory["raw_error_patterns"] > 0
    assert inventory["cost_patterns"] > 0
    assert inventory["internal_route_fields"] > 0


def test_emptying_the_rule_tables_makes_the_poison_pass(monkeypatch):
    """🔴 证明「是这套规则在抓」,不是别的东西碰巧抓到。

    清空本模块自己的四类规则后,只依赖继承闸的那几条毒串仍会红(这是对的),
    但**依赖本模块规则**的那几条必须变绿 —— 如果它们照样红,说明本模块
    的规则表根本没参与判定,那这套 DLP 就是个装饰。
    """
    own_rule_samples = [
        {"meta": {"upstream_order_id": "MJH-88123"}},
        {"error": {"detail": "ConnectError: upstream returned 502"}},
        {"meta": {"url": "http://10.0.0.7/internal/dispatch"}},
        {"op": {"http_method": "POST"}},
    ]
    for payload in own_rule_samples:
        assert dlp.scan_facade_payload(payload), payload

    monkeypatch.setattr(dlp, "_COMPILED_UPSTREAM", ())
    monkeypatch.setattr(dlp, "_COMPILED_RAW_ERROR", ())
    monkeypatch.setattr(dlp, "_COMPILED_COST", ())
    monkeypatch.setattr(dlp, "_HTTP_METHOD_FIELD", frozenset())
    for payload in own_rule_samples:
        assert dlp.scan_facade_payload(payload) == [], payload


def test_upstream_identifier_in_the_key_name_is_caught_not_only_in_the_value():
    """回归锁:第一版只扫了值,``{"upstream_order_id": "MJH-88123"}`` 整条逃掉。

    泄漏可以只存在于**键名**里(值本身长得像任何一串编号)。这条锁把那次
    真实的漏检钉死,防止下次重构又只扫值。
    """
    problems = dlp.scan_facade_payload({"meta": {"upstream_order_id": "X1"}})
    assert problems and "upstream_order" in problems[0]


def test_machine_keys_are_scoped_to_this_callsite_not_the_shared_whitelist():
    """五阶段的机读键白名单不许写进全仓共享表。

    往共享白名单里加 ``state`` 这种通用键 = 给**所有**出参开同一个洞。
    """
    from services import gap_operation_labels as labels

    assert "state" not in labels._MACHINE_FIELD_KEYS
    assert "intent_id" not in labels._MACHINE_FIELD_KEYS
    assert "state" in dlp.FIVE_PHASE_MACHINE_KEYS
    # 反向对照:不带本包白名单时,同一个 payload 会被判红。
    with pytest.raises(labels.InternalTermLeak):
        labels.assert_no_internal_leak({"intent_state": "approval_pending"})
    labels.assert_no_internal_leak(
        {"intent_state": "approval_pending"},
        extra_machine_keys=dlp.FIVE_PHASE_MACHINE_KEYS,
    )
