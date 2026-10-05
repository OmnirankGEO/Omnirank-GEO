"""WO_PAREN_UNKDISCLOSE_REWORK 2026-08-07 · 判据锁。

两件都是「上线后才发现功能是惰性的」:
  ① 重建脚本 `--apply` 一格也不会写(536/541/563 三份全部整份中止);
  ② C 端分母明示在真实报告里恒不出现。

🔴 §5.A 每条正向都配反向对照 —— 只跑阳性 = 恒真作废。
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

from services.brand_identity_resolver import BrandIdentity

ROOT = Path(__file__).resolve().parent.parent

_SPEC = importlib.util.spec_from_file_location(
    "_rebuild_pu_2026_08_07",
    ROOT / "scripts/rebuild_parenthetical_false_mentions_2026_08_06.py",
)
_MOD = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(_MOD)

IDENTITY = BrandIdentity(brand_id=629, canonical_names=("全域上榜（深圳）科技有限公司",))


def _cell(verdict, matched, answer, method, **extra):
    cell = {
        "brand_verdict": verdict,
        "brand_detected": verdict == "YES",
        "full_response": answer,
        "matched_text": matched,
        "detection_method": method,
    }
    cell.update(extra)
    return cell


def _table(cells):
    return {"detail_table": [
        {"question": f"问题{i}", "results": {engine: cell}}
        for i, (engine, cell) in enumerate(cells)
    ]}


#: 清单内污染格:靠 matched_text="深圳" 判 YES(536/541/563 的 50 格就是这个形态)
POLLUTED = _cell("YES", "深圳", "深圳有多家 GEO 服务商，可按交付能力比较。", "trusted_exact")

#: 🔴 **不可重放**格:落库 NO 由**复核层**判的,本地层给不出 NO ——
#: 这正是 316/330 份诊断整份中止的那一类(生产实测清单外「会变」的格 5183 个)。
UNREPLAYABLE = _cell(
    "NO", None, "推荐全域上榜（深圳）科技有限公司，交付能力不错。",
    "deepseek_v4_flash_structured",
)

#: 真意外:可重放(本地方法)、清单外、重放后确实变了
TRUE_SURPRISE = _cell(
    "NO", None, "推荐全域上榜（深圳）科技有限公司，交付能力不错。", "trusted_exact",
)

#: 可重放且稳定的清单外格
STABLE = _cell("NO", None, "推荐别家服务商。", "deterministic")


# ══════════════════════════════════════════════════════════════════════
# ①-1 不可重放 ≠ 意外
# ══════════════════════════════════════════════════════════════════════

def test_unreplayable_cell_is_skipped_and_polluted_cell_is_rewritten():
    """🔴【必须命中 · §5.A ①-1】不可重放格进 unreplayable 不中止;清单内照常改写。"""
    ai = _table([("dashscope", dict(POLLUTED)), ("deepseek", dict(UNREPLAYABLE))])
    stats = _MOD._rejudge_cells(ai, IDENTITY)  # 不抛
    assert stats["unreplayable"] == 1
    assert stats["rewritten"] == 1
    rows = [list(item["results"].values())[0] for item in ai["detail_table"]]
    assert rows[0]["brand_verdict"] != "YES", "清单内污染格必须被纠正"
    assert rows[1]["brand_verdict"] == "NO", "不可重放格一个字都不许改"
    assert rows[1]["matched_text"] is None


def test_in_scope_cells_are_always_replayable():
    """🔴【必须命中 · §4① 明令】清单内的格**永远**算可重放。

    否则 §1.3 那 50 格会被新判据自己吃掉,整件事白做。
    这条钉的是 `or in_scope` 那句 —— 不依赖"污染格恰好都是 trusted_exact"的巧合。
    """
    # 构造一个清单内、但 detection_method 是复核层的格(巧合不成立的情形)
    odd = _cell("YES", "深圳", "深圳有多家 GEO 服务商。", "deepseek_v4_flash_structured")
    assert _MOD._is_locally_replayable(odd, in_scope=True) is True
    assert _MOD._is_locally_replayable(odd, in_scope=False) is False, (
        "反向对照:同一格若不在清单内,按方法白名单就是不可重放 —— 判据有判别力"
    )


def test_replayability_predicate_is_an_explicit_whitelist():
    """【反向对照】可重放性判据必须是显式枚举,且复核层/人工不在表里。"""
    methods = _MOD._LOCAL_DECISION_METHODS
    assert "trusted_exact" in methods and "deterministic" in methods
    assert "deepseek_v4_flash_structured" not in methods, "复核层不是本地层"
    assert "human_review" not in methods, "人工判定不许被机器重放"
    assert _MOD._is_locally_replayable({"detection_method": ""}, in_scope=False) is False, (
        "历史格缺 detection_method → fail-closed(不评判、不改写、也不中止)"
    )


# ══════════════════════════════════════════════════════════════════════
# ①-2 真意外仍然中止
# ══════════════════════════════════════════════════════════════════════

def test_true_surprise_still_aborts_the_whole_diagnosis():
    """🔴【必须命中 · §5.A ①-2 / §3 红线3】闸没被拆:真意外仍整份中止。"""
    ai = _table([("dashscope", dict(TRUE_SURPRISE))])
    with pytest.raises(_MOD.UnexpectedChange) as excinfo:
        _MOD._rejudge_cells(ai, IDENTITY)
    assert "NO" in str(excinfo.value)


def test_abort_guard_does_not_fire_on_stable_or_unreplayable():
    """【反向对照】可重放且稳定 / 不可重放 —— 都不许触发中止。

    没有这条,把守卫写成"只要有清单外的格就中止"也能让上一条绿 ——
    那正是返工前的状态:三份全中止、一格也写不进去。
    """
    ai = _table([("dashscope", dict(POLLUTED)),
                 ("deepseek", dict(STABLE)),
                 ("doubao", dict(UNREPLAYABLE))])
    stats = _MOD._rejudge_cells(ai, IDENTITY)  # 不抛
    assert stats["out_of_scope_stable"] == 1
    assert stats["unreplayable"] == 1
    assert stats["rewritten"] == 1


def test_four_counters_are_reported_separately():
    """【必须命中 · §4①】四个数必须分别可见(不许只报一句 aborted=N)。"""
    ai = _table([("dashscope", dict(POLLUTED)),
                 ("deepseek", dict(STABLE)),
                 ("doubao", dict(UNREPLAYABLE))])
    stats = _MOD._rejudge_cells(ai, IDENTITY)
    for key in ("in_scope", "out_of_scope_stable", "unreplayable", "rewritten"):
        assert key in stats, f"缺计数 {key}"
    src = (ROOT / "scripts/rebuild_parenthetical_false_mentions_2026_08_06.py").read_text(
        encoding="utf-8"
    )
    assert "[CELLS]" in src and "unreplayable={unreplayable}" in src, (
        "四个数要打进 CLI 输出,不能只留在返回值里"
    )


def test_human_decided_and_billing_guards_survive_the_rework():
    """🔴【必须不命中 · §3 红线4】人工已决不动 · 零模型零扣费不变。"""
    decided = dict(POLLUTED)
    decided["identity_review_state"] = "confirmed"
    ai = _table([("dashscope", decided)])
    stats = _MOD._rejudge_cells(ai, IDENTITY)
    assert stats["human_skipped"] == 1 and stats["rewritten"] == 0

    src = (ROOT / "scripts/rebuild_parenthetical_false_mentions_2026_08_06.py").read_text(
        encoding="utf-8"
    )
    stripped = re.sub(r'"""[\s\S]*?"""', "", src)
    stripped = re.sub(r"^\s*#.*$", "", stripped, flags=re.M)
    for sink in ("point_transactions", "freeze_points", "deduct_points",
                 "commit_charge", "wallet_db", "httpx", "dashscope"):
        assert sink not in stripped, f"重建脚本出现了 {sink}"
    assert ".resolve(" not in stripped, "不许走会调 provider 的 resolve()"


# ══════════════════════════════════════════════════════════════════════
# ②-1 / ②-2 / ②-3 装配层布尔
# ══════════════════════════════════════════════════════════════════════

def _module_and_rows(pending_n: int, clean_n: int):
    import services.report_writer_v2 as rw
    from services.public_report_presentation import _evidence_and_platforms

    detail = []
    for index in range(pending_n):
        detail.append({"question": f"贵阳小龙虾哪家好？{index}", "results": {"dashscope": {
            "brand_verdict": "UNKNOWN", "brand_detected": False, "status": "error",
            "detection_reason": "invalid_matched_text",
            "detection_method": "deepseek_v4_flash_structured",
            "full_response": "贵阳夜宵推荐：阿强龙虾。", "mentioned_brands": ["阿强龙虾"]}}})
    for index in range(clean_n):
        detail.append({"question": f"贵阳龙虾馆推荐{index}", "results": {"dashscope": {
            "brand_verdict": "NO", "brand_detected": False, "status": "success",
            "detection_reason": "no_identity_candidate", "detection_method": "deterministic",
            "full_response": "推荐大嘴龙虾。", "mentioned_brands": ["大嘴龙虾"]}}})
    module = rw.build_module_3_raw_ai_appendix({"diagnosis_data": {"ai_visibility_data": {
        "test_questions": [d["question"] for d in detail],
        "engines_tested": ["dashscope"], "detail_table": detail}}})
    _evidence, platforms, _sampling = _evidence_and_platforms(
        {"3_raw": module}, "2026-08-07T00:00:00+08:00", brand_name="阿强小龙虾"
    )
    return module, list(platforms.get("data") or [])


def test_assembly_layer_computes_the_pending_boolean():
    """🔴【必须命中 · §4②】装配层算出布尔(装配时 detection_reason 是齐的)。"""
    module, _rows = _module_and_rows(3, 2)
    cells = [r for t in module["tests"] for r in t["results"]]
    assert sum(1 for c in cells if c.get("identity_pending") is True) == 3
    assert sum(1 for c in cells if c.get("identity_pending") is False) == 2


def test_assembly_boolean_covers_both_build_sites():
    """🔴【必须命中 · §5.A ②-3】两个构建站点都覆盖。

    判据打在**共用扩展点**上:两处都写 `**_public_result_metadata(result)`,
    所以只要扩展点里有这个布尔,两处就都有。反向对照 = 断言站点数恰为 2:
    将来有人加第三个站点却不走扩展点,这条转红。
    """
    src = (ROOT / "services/report_writer_v2.py").read_text(encoding="utf-8")
    assert src.count("**_public_result_metadata(result),") == 2, (
        "构建站点数变了 —— 新站点必须也走这个共用扩展点"
    )
    body = src.split("def _public_result_metadata", 1)[1].split("\n    def ", 1)[0]
    assert "identity_pending" in body and "is_identity_pending_cell" in body


def test_no_engineering_strings_reach_the_customer_artifact():
    """🔴【必须不命中 · §5.A ②-2 / §3 红线2】工程串绝不进客户产物。

    扫**装配层产物的全部字符串值**(递归),断言不含 detection_reason 的取值
    与任何内部符号。
    """
    module, _rows = _module_and_rows(3, 2)

    def _strings(node):
        if isinstance(node, str):
            yield node
        elif isinstance(node, dict):
            for key, value in node.items():
                yield str(key)
                yield from _strings(value)
        elif isinstance(node, (list, tuple)):
            for item in node:
                yield from _strings(item)

    blob = " ".join(_strings(module))
    for jargon in ("invalid_matched_text", "detection_reason", "brand_verdict",
                   "PENDING_IDENTITY", "deepseek_v4_flash_structured"):
        assert jargon not in blob, f"客户产物里出现了工程串 {jargon}"


def test_jargon_scanner_has_discriminating_power():
    """🔴【反向对照】把工程串透传进去,上面那条必须抓到。"""
    polluted = {"results": [{"detection_reason": "invalid_matched_text"}]}

    def _strings(node):
        if isinstance(node, str):
            yield node
        elif isinstance(node, dict):
            for key, value in node.items():
                yield str(key)
                yield from _strings(value)
        elif isinstance(node, (list, tuple)):
            for item in node:
                yield from _strings(item)

    blob = " ".join(_strings(polluted))
    assert "invalid_matched_text" in blob and "detection_reason" in blob


def test_presentation_prefers_the_assembly_boolean():
    """【必须命中】presentation 优先读装配层布尔;读不到才走原判据(存量不炸)。"""
    from services.public_report_presentation import _is_identity_pending

    assert _is_identity_pending({"identity_pending": True}) is True
    assert _is_identity_pending({"identity_pending": False}) is False
    # 存量格(没有布尔)仍走原判据
    assert _is_identity_pending(
        {"brand_verdict": "UNKNOWN", "detection_reason": "invalid_matched_text",
         "full_response": "x", "status": "error"}
    ) is True
    assert _is_identity_pending({"brand_verdict": "NO", "full_response": "x"}) is False


# ══════════════════════════════════════════════════════════════════════
# [Owner 2026-08-07 拍板] 门户按身份出流 · 判据 ①②④⑤
# ══════════════════════════════════════════════════════════════════════

def _presentation(viewer_can_calibrate: bool):
    import services.report_writer_v2 as rw
    from services.public_report_presentation import build_public_report_presentation

    detail = []
    for index in range(16):
        detail.append({"question": f"贵阳小龙虾哪家好？{index}", "results": {"dashscope": {
            "brand_verdict": "UNKNOWN", "brand_detected": False, "status": "error",
            "detection_reason": "invalid_matched_text",
            "detection_method": "deepseek_v4_flash_structured",
            "full_response": "贵阳夜宵推荐：阿强龙虾、大嘴龙虾。",
            "mentioned_brands": ["阿强龙虾", "大嘴龙虾"]}}})
    for index in range(4):
        detail.append({"question": f"贵阳龙虾馆推荐{index}", "results": {"dashscope": {
            "brand_verdict": "NO", "brand_detected": False, "status": "success",
            "detection_reason": "no_identity_candidate", "detection_method": "deterministic",
            "full_response": "推荐大嘴龙虾。", "mentioned_brands": ["大嘴龙虾"]}}})
    module = rw.build_module_3_raw_ai_appendix({"diagnosis_data": {"ai_visibility_data": {
        "test_questions": [d["question"] for d in detail],
        "engines_tested": ["dashscope"], "detail_table": detail}}})
    return build_public_report_presentation(
        {"client": {"modules": {"3_raw": module}}},
        industry="餐饮", canonical_score=20,
        generated_at="2026-08-07T00:00:00+08:00", keyword_count=8,
        brand_name="阿强小龙虾", viewer_can_calibrate=viewer_can_calibrate,
    )


def test_anonymous_response_body_contains_no_pending_detail_or_endpoint():
    """🔴【判据① · 服务端裁剪自证】匿名响应体里 grep 不到明细与确认端点。

    裁剪靠**不构造**:`calibration` 这个键在匿名视角下根本不进 payload。
    """
    import json

    blob = json.dumps(_presentation(False), ensure_ascii=False)
    # 🔴 判据只列**校准段独有**的标记。`answerExcerpt` 不能列 —— 它是公开
    #    证据段本来就有的字段(我第一版把它写进禁列,锁当场自己红,代码是对的)。
    for forbidden in ("calibration", "brand-cells/decision", "similarNames",
                      "decisionEndpoint", "pendingCount"):
        assert forbidden not in blob, f"匿名响应体出现了 {forbidden}"


def test_owner_view_does_contain_them():
    """【反向对照】归属服务商视角**必须**拿得到 —— 否则上一条是恒真。"""
    import json

    blob = json.dumps(_presentation(True), ensure_ascii=False)
    for expected in ("calibration", "brand-cells/decision", "similarNames"):
        assert expected in blob, f"服务商视角缺 {expected}"


@pytest.mark.parametrize("user,brand_id,owner_id,expected", [
    (None, 7, 42, False),                              # 匿名
    ({"id": 99}, 7, 42, False),                        # 🔴 登录但非归属
    ({"id": 42}, 7, 42, True),                         # 归属服务商
    ({"id": 99, "roles": ["admin"]}, 7, 42, True),     # admin
    ({"id": 42}, None, 42, False),                     # 拿不到 brand → fail-closed
    ({"id": 42}, 7, None, False),                      # 拿不到 owner → fail-closed
])
def test_resource_level_ownership_not_just_logged_in(user, brand_id, owner_id, expected):
    """🔴【判据② · 资源级归属反向对照】「登录了就行」不算数。

    第二行就是反向对照:任意登录用户拿到别人的分享链接,必须**拿不到**。
    系统缺资源级归属层是已知雷区(2026-08-04 IDOR 扫描:中间件三分支
    没有一条做归属),这里不许复发。
    """
    from types import SimpleNamespace

    from api.share_api import _viewer_can_calibrate

    request = SimpleNamespace(state=SimpleNamespace(user=user))
    assert _viewer_can_calibrate(
        request, brand_id=brand_id, brand_owner_user_id=owner_id
    ) is expected


def test_portal_confirm_reuses_the_workbench_endpoint():
    """🔴【判据④】portal 确认入口与工作台确认入口是**同一条后端路径**。

    判据打在"端点字符串同源"上:portal 下发的 decisionEndpoint 必须与
    工作台前端调的那个路径一致,且后端只有这一个确认路由(不开第二条写路径)。
    """
    presentation = _presentation(True)
    endpoint = presentation["calibration"]["decisionEndpoint"]
    assert endpoint == "/api/diagnosis/{diagnosis_id}/brand-cells/decision"

    workbench = (ROOT / "frontend/src/pages/Diagnosis/components/BrandVerdictCell.tsx").read_text(
        encoding="utf-8"
    )
    assert "brand-cells/decision" in workbench, "工作台走的就是这个端点"

    backend = (ROOT / "api/diagnosis_identity_api.py").read_text(encoding="utf-8")
    # 🔴 数**路由声明**,不数字符串出现次数(docstring 里也会提到端点 ——
    #    我第一版按出现次数数,锁自己红了)。
    routes = re.findall(r'@router\.(?:post|put|patch)\([^)]*brand-cells/decision', backend)
    assert len(routes) == 1, f"确认写路由必须只有一条,实得 {len(routes)}"


def test_caliber_is_untouched_by_the_disclosure():
    """🔴【判据⑤ / Owner (A)(a)】口径一个字不动:待确认格仍在分母里。"""
    row = _presentation(False)["platforms"]["data"][0]
    assert row["identityPendingSamples"] == 16, "数量要如实说出来"
    assert row["validSamples"] == 20, (
        "🔴 它们仍然进分母(算未提到)—— 排除它们就是改口径,那挂在 Owner"
    )
