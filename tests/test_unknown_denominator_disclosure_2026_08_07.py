"""WO_UNKNOWN_DENOMINATOR_DISCLOSURE 2026-08-07 · 判据锁(乙案:维持排除 + 明示)。

生产取证基线(2026-08-07 只读实查):

    诊断  实跑格数  ai_total_tests  detected  ai_mention_rate   每格状态
    561     32          16             4         25.0%     YES 4 / NO 12 / PENDING 16
    553     32          30             4         13.3%     YES 4 / NO 26 / PENDING 2
    563     32          32            17         53.1%     YES 17 / NO 15 / PENDING 0

    待确认格落库 status='error'(三单共 18 格实查)→ 旧版公开报告把它算进 errors
    → dataStatus 显示「部分失败」。**什么都没失败**,而真正该说的
    「这几格没进出现率分母」一个字没说。

    全量影响面(313 份诊断复算,不是工单原文的近 60 份):
      含待确认的诊断 35/313 = 11.2% · 待确认格 201/9880 = 2.0%

🔴 Owner 四条边界,每条都在下面有锁:
  ① 分母语义一字不动 → `test_denominator_semantics_are_untouched`
  ② C 端只改文案与数量,**不给确认入口** → `test_public_report_has_no_confirm_entry`
  ③ C 端人话,禁内部术语 → `test_public_disclosure_uses_plain_language`
  ④ 563(零待确认)明示块必须不渲染 → `test_zero_pending_renders_nothing`
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from services.diagnosis_identity_review import is_identity_pending_cell

ROOT = Path(__file__).resolve().parent.parent
PLATFORM_TSX = ROOT / "frontend/src/features/publicReportPremium/components/sections/PlatformPerformance.tsx"
CELL_TSX = ROOT / "frontend/src/pages/Diagnosis/components/BrandVerdictCell.tsx"

#: 用户面文案里绝不许出现的工程词(WO §3.1-4)
FORBIDDEN_JARGON = (
    "PENDING_IDENTITY", "ai_total_tests", "UNKNOWN", "invalid_matched_text",
    "identityPendingSamples", "detection_reason", "brand_verdict",
)


def _cell(*, verdict: str, reason: str | None, answer: str, status: str = "success") -> dict:
    cell = {
        "brand_verdict": verdict,
        "brand_detected": verdict == "YES",
        "status": status,
        "full_response": answer,
        "answer_summary": answer[:60],
    }
    if reason:
        cell["detection_reason"] = reason
    return cell


PENDING = _cell(verdict="UNKNOWN", reason="invalid_matched_text",
                answer="贵阳夜宵推荐：阿强龙虾、大嘴龙虾。", status="error")
REAL_ERROR = _cell(verdict="UNKNOWN", reason="provider_not_sent",
                   answer="查询失败", status="error")
YES = _cell(verdict="YES", reason="trusted_exact_alias", answer="推荐阿强小龙虾。")
NO = _cell(verdict="NO", reason="no_identity_candidate", answer="推荐大嘴龙虾。")


# ══════════════════════════════════════════════════════════════════════
# 计数 SSOT
# ══════════════════════════════════════════════════════════════════════

def test_pending_predicate_matches_the_denominator_ssot():
    """【必须命中】待确认判据与分母同源 —— 说的 N 必须等于扣掉的 N。"""
    assert is_identity_pending_cell(PENDING) is True


@pytest.mark.parametrize("cell,label", [
    (REAL_ERROR, "真·引擎失败"),
    (YES, "已判命中"),
    (NO, "已判未提到"),
    (_cell(verdict="UNKNOWN", reason=None, answer="历史格没有 detection_reason", status="error"),
     "历史格(缺 reason)"),
])
def test_pending_predicate_does_not_over_claim(cell, label):
    """【必须不命中 · 反向对照】只有"等人确认"才算待确认。

    没有这一组,把判据写成 `verdict == "UNKNOWN"` 或干脆 `return True`
    也能让上一条绿 —— 那会把真失败也说成"待确认",比不说更糟。
    历史格缺 reason 时 fail-closed(宁可少说,不虚报)。
    """
    assert is_identity_pending_cell(cell) is False, label


# ══════════════════════════════════════════════════════════════════════
# 边界① 分母语义一字不动
# ══════════════════════════════════════════════════════════════════════

def _platform_rows(cells: list[dict]) -> list[dict]:
    from services.public_report_presentation import _evidence_and_platforms

    modules = {"3_raw": {"tests": [
        {
            "question": f"贵阳小龙虾夜宵店哪家好吃？{index}",
            "results": {"通义千问": cell},
        }
        for index, cell in enumerate(cells)
    ]}}
    _evidence, platforms, _extra = _evidence_and_platforms(
        modules, "2026-08-07T00:00:00+08:00", brand_name="阿强小龙虾"
    )
    return list(platforms.get("data") or [])


def test_denominator_semantics_are_untouched():
    """🔴【必须不命中 · 边界①】待确认格**仍然**不进任何分母。

    这是本单最容易做错的一格:把待确认算进分母 = 改口径,那半边还挂在 Owner。
    锁的是"只加了披露"。
    """
    rows = _platform_rows([YES, NO, NO, PENDING, PENDING])
    assert rows, "前提:装配出了平台行"
    row = rows[0]
    # 3 格已知(1 YES + 2 NO)进分母,2 格待确认不进
    assert row["validSamples"] == 3, f"分母被改动了:{row}"
    assert row["identityPendingSamples"] == 2


def test_mention_rate_is_byte_identical_before_and_after():
    """🔴【反向对照 · WO §3.2-6】同一份数据,出现率与改动前逐字相同。

    A/B 做法:待确认格在改动前后都走 `_is_valid_answer=False` 这条路
    —— 改动只把它从 errors 挪进 identity_pending,**没碰任何进分母的分支**。
    所以"只含已知格"的样本与"已知格+待确认格"的样本,出现率必须**一模一样**。
    这条比直接断言一个硬编码百分比更硬:它证明待确认格对费率**零影响**。
    """
    known_only = _platform_rows([YES, NO, NO])[0]
    with_pending = _platform_rows([YES, NO, NO, PENDING, PENDING])[0]
    for key in ("validSamples", "detectionRatePct", "mentionRatePct",
                "recommendRatePct", "competitiveSamples"):
        assert known_only[key] == with_pending[key], (
            f"{key} 因为多了待确认格而变了 —— 口径被改动了(边界①)"
        )


def test_real_errors_are_still_reported_as_failures():
    """【反向对照】真失败仍然是「部分失败」—— 不许被一起改口。"""
    rows = _platform_rows([YES, NO, REAL_ERROR])
    assert rows[0]["dataStatus"] == "部分失败"
    assert rows[0]["identityPendingSamples"] is None


def test_pending_is_no_longer_called_a_failure():
    """【必须命中】待确认不再被说成「部分失败」。"""
    rows = _platform_rows([YES, NO, PENDING])
    assert rows[0]["dataStatus"] == "部分待确认", rows[0]


# ══════════════════════════════════════════════════════════════════════
# 边界④ N=0 不渲染(反向对照的支点)
# ══════════════════════════════════════════════════════════════════════

def test_zero_pending_emits_null_not_zero():
    """🔴【必须不命中 · 边界④ · 563 场景】零待确认时后端给 None 不给 0。

    没有这条,把字段写成恒 `0` 也能让上面全绿,而前端 `?? 0` 之类的写法
    会把它渲染成「0 次待确认」—— 563 那种报告里多一句废话。
    """
    rows = _platform_rows([YES, NO, NO])
    assert rows[0]["identityPendingSamples"] is None
    assert rows[0]["dataStatus"] != "部分待确认"


def test_public_disclosure_block_is_conditional():
    """🔴【必须不命中 · 边界④】C 端明示块的渲染条件必须是 `> 0`。"""
    src = PLATFORM_TSX.read_text(encoding="utf-8")
    assert "pendingDisclosureTotal > 0 &&" in src, (
        "明示块必须条件渲染 —— 无条件渲染会让 563 多出一句废话,"
        "而且会让 §3.1 的正向断言变成恒真"
    )


def test_agent_disclosure_block_is_conditional():
    """🔴【必须不命中 · 边界④】代理端明示与「去确认」按钮同样只在 N>0 时出现。"""
    src = CELL_TSX.read_text(encoding="utf-8")
    assert src.count("pendingTotal > 0 &&") >= 3, (
        "徽章 / 分母明示 / 去确认按钮三处都必须挂 pendingTotal > 0"
    )


# ══════════════════════════════════════════════════════════════════════
# 边界② C 端不给确认入口 · 边界③ 人话
# ══════════════════════════════════════════════════════════════════════

def test_public_report_has_no_confirm_entry():
    """🔴【必须不命中 · 边界②】C 端(token 面)不许出现确认入口。

    确认是服务商的活。把操作口递给链接接收方 = 让客户去改自己报告的分数。
    """
    src = PLATFORM_TSX.read_text(encoding="utf-8")
    for forbidden in ("brand-cells/decision", "decide", "confirm_yes",
                      "add_alias", "去确认", "onDecide"):
        assert forbidden not in src, f"C 端出现了确认入口痕迹:{forbidden}"


def test_public_disclosure_uses_plain_language():
    """🔴【必须命中 · 边界③】C 端文案是人话,且说清"没计入出现率"。"""
    src = PLATFORM_TSX.read_text(encoding="utf-8")
    block = src.split("identity-pending-disclosure", 1)[1][:600]
    assert "当前按「未提到」计入" in block, "必须说清这一层的真实口径(Owner (A)(a))"
    assert "未计入本次出现率" not in block, (
        "🔴 在这一层它们**是进分母的** —— 写「未计入」就是假话"
    )
    assert "人工确认" in block or "需人工" in block, "必须说清为什么"
    # [复审要求] 两个数一起给:只说"另有 M 待确认"读者不知道 M 相对多大,
    # 给了分母才判得动这份报告的可信度。
    assert "已统计" in block, "必须同时给出已统计的样本数(分母)"


@pytest.mark.parametrize("path", [PLATFORM_TSX, CELL_TSX])
def test_no_engineering_jargon_in_user_facing_text(path):
    """🔴【必须不命中 · WO §3.1-4】渲染出的文案里搜不到工程词。

    判据只扫**渲染出来的中文/英文文案**,不扫注释与标识符 ——
    否则本文件自己的注释就会把锁弄红(本仓踩过:锚点撞自己写的注释)。
    """
    src = path.read_text(encoding="utf-8")
    # 剥掉块注释与行注释(标识符与注释不算用户面文案)
    stripped = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    stripped = re.sub(r"^\s*//.*$", "", stripped, flags=re.M)
    # 只看 JSX 文本节点里的中文串 + 明显的用户面字符串
    rendered = " ".join(re.findall(r"[>}]([^<>{}]*[一-鿿][^<>{}]*)[<{]", stripped))
    for jargon in FORBIDDEN_JARGON:
        assert jargon not in rendered, f"用户面文案出现了工程词 {jargon}"


def test_jargon_scanner_has_discriminating_power():
    """🔴【反向对照】上面那条扫描不是恒真 —— 造一段带工程词的 JSX 必须被抓到。"""
    fake = '<p>另有 3 格 PENDING_IDENTITY 未计入</p>'
    rendered = " ".join(re.findall(r"[>}]([^<>{}]*[一-鿿][^<>{}]*)[<{]", fake))
    assert any(j in rendered for j in FORBIDDEN_JARGON), "扫描口径抓不到工程词 = 恒真"


# ══════════════════════════════════════════════════════════════════════
# §1.2 一键去确认 · §1.3 确认后原子重算 + 零扣费
# ══════════════════════════════════════════════════════════════════════

def test_confirm_entry_reuses_the_existing_chain():
    """【必须命中 · §1.2】去确认按钮滚到既有待确认格,不新建交互。"""
    src = CELL_TSX.read_text(encoding="utf-8")
    assert 'data-identity-pending' in src, "待确认格要有落点"
    assert 'data-testid="goto-identity-pending"' in src, "明示处要有去确认入口"
    assert "scrollIntoView" in src, "复用同页既有确认 UI,不新开页面/弹层"


def test_agent_side_states_the_denominator_impact():
    """【必须命中 · §1.1】代理端必须说清分母是多少、扣了多少。

    旧版只说"有 N 格待确认" —— 没说这 N 格不算进出现率,而那才是要紧的一句。
    """
    src = CELL_TSX.read_text(encoding="utf-8")
    assert "已确认" in src and "出现率按" in src and "未计入" in src, (
        "代理端明示必须同时给出分母与被扣掉的格数"
    )


def test_decide_brand_cell_writes_no_billing_rows():
    """🔴【必须不命中 · §3.1-3 / §1.3】确认动作零扣费。

    静态判据:确认链路整份源码里不许出现任何计费 sink。
    (用户已经为这格付过钱了,确认是把已付的样本收回统计,不是新消费。)
    """
    src = (ROOT / "services/diagnosis_identity_decision.py").read_text(encoding="utf-8")
    # 🔴 先剥 docstring:该文件的文档里**写着**"零扣费""不碰计费表",
    #    不剥就会被自己的说明文字撞上 —— 本仓踩过"锚点撞自己写的注释"。
    stripped = re.sub(r'"""[\s\S]*?"""', "", src)
    stripped = re.sub(r"^\s*#.*$", "", stripped, flags=re.M)
    for sink in ("point_transactions", "freeze_points", "deduct_points",
                 "commit_charge", "wallet_db"):
        assert sink not in stripped, f"确认链路里出现了计费 sink:{sink}"


def test_billing_sink_scanner_has_discriminating_power():
    """🔴【反向对照】上面那条扫描不是恒真 —— 造一段带计费 sink 的代码必须被抓到。"""
    fake = 'cur.execute("INSERT INTO point_transactions ...")'
    stripped = re.sub(r'"""[\s\S]*?"""', "", fake)
    assert "point_transactions" in stripped, "扫描口径抓不到计费 sink = 恒真"


def test_atomic_recompute_is_still_wired():
    """【必须命中 · §1.3】确认后分母/评分一次算完(不许有中间态)。"""
    src = (ROOT / "services/diagnosis_identity_decision.py").read_text(encoding="utf-8")
    # 同一函数体内:重算聚合 → 漏斗 → 回写诊断记录,三步都在
    body = src.split("def decide_brand_cell", 1)[1]
    for step in ("_aggregates(", "_funnel_from_dimension_stats(",
                 "UPDATE diagnosis_records"):
        assert step in body, f"原子重算链缺了 {step}"
    assert "expected_version" in body, "乐观锁必须在(防重放把分母加两次)"


# ══════════════════════════════════════════════════════════════════════
# DTO 映射层(复审点名纳入范围)
# ══════════════════════════════════════════════════════════════════════

def test_dto_mapping_layer_accepts_the_new_field():
    """🔴【必须命中 · 复审】映射层必须校验新字段,否则整块明示静默消失。

    ``validPlatform`` 是布尔校验:任一字段不合格,整段平台数据被降级 ——
    **不会有任何报错**。少写这一行的后果不是"字段丢了",是"这一节没了",
    而且线上看不出来。
    """
    src = (ROOT / "frontend/src/features/publicReportPremium/transport/mapDto.ts").read_text(
        encoding="utf-8"
    )
    assert "isOptionalCount(value.identityPendingSamples)" in src, (
        "mapDto.validPlatform 必须校验 identityPendingSamples"
    )


def test_dto_contract_type_declares_the_new_field_as_optional_nullable():
    """【反向对照】契约声明必须是可选可空 —— 0 时后端给 null,声明成必填会全线报错。"""
    src = (ROOT / "frontend/src/features/publicReportPremium/contract/types.ts").read_text(
        encoding="utf-8"
    )
    assert "readonly identityPendingSamples?: number | null;" in src


def test_portal_token_surface_stays_read_only():
    """🔴【必须不命中 · 边界② · 2026-08-07 Review 装配时按 Owner refine 改写】

    旧口径「整个 publicReportPremium 目录零确认能力」已被 Owner 2026-08-07 拍板 refine
    (裁定 REVIEW_VERDICT_*,门户页可以有校准入口,但只对已登录且归属的服务商出流):
      ① 确认能力只许存在于 IdentityCalibration.tsx 这一个文件;
      ② 该文件渲染必须由 `calibration` 数据在不在门控(服务端裁剪 = 唯一身份口径,
         匿名响应根本不含 calibration —— 那条不变量由服务端「不构造」锁盯着);
      ③ 目录里其余文件仍然零确认能力(匿名客户面维持只读)。
    """
    feature_dir = ROOT / "frontend/src/features/publicReportPremium"
    allowed = "IdentityCalibration.tsx"
    hits = []
    for path in feature_dir.rglob("*.ts*"):
        if path.name == allowed:
            continue
        text = path.read_text(encoding="utf-8")
        for forbidden in ("brand-cells/decision", "confirm_yes", "add_alias"):
            if forbidden in text:
                hits.append(f"{path.name}:{forbidden}")
    assert hits == [], f"确认能力泄漏到 IdentityCalibration 之外:{hits}"

    calib = feature_dir / "components/sections/IdentityCalibration.tsx"
    assert calib.exists(), "IdentityCalibration.tsx 不在——校准入口整块丢失"
    calib_text = calib.read_text(encoding="utf-8")
    assert "calibration" in calib_text and "pendingCount" in calib_text
    # 渲染门控 = calibration 数据在不在;文件里不许自己判身份(身份只在服务端裁)
    assert "!calibration" in calib_text, "calibration 判空门控没了 —— 渲染条件被改掉"
