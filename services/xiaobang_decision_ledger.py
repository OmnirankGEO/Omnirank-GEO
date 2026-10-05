"""小榜 vNext 版本化决策台账 + H0 闸矩阵(WP0 交付物 · 规格 §2/§15.1)。

## 为什么这是代码而不是一张表格

invrel 2026-08-13 的 P0 是这么来的:一份长工单里写着「未拍板前默认 X」,
实现的人把那句话读成了公理,于是**未签发的候选口径变成了运行时硬阻断**。
规格 §2 因此写死一条:「在 Owner 将其签入版本化决策记录前,实施者不得用它们
新增运行时硬门」。一句只写在文档里的规矩救不了下一个人 —— 所以这里把它做成
**调用时会自己抛异常的东西**::

    assert_gate_is_signed("XB-H0-EXTERNAL-CONFIRM")   # 背后是 candidate → 抛

判据不是「我记得没用候选口径」,是「用了就当场红」(测试 §19.1 #20)。

## 两类记录

* :data:`DECISIONS` —— §2 的 Owner 对话输入(XO-01..XO-11)与已签发上位规则。
  ``status`` 只有 ``signed`` / ``candidate`` 两值,**没有第三档**,也不许用
  「事实上大家都这么做」把 candidate 说成 signed。
* :data:`H0_GATES` —— §15.1 的七条 fail-closed 闸(本包按两档确认制拆成八条)。
  每条必须指回 ``signed`` 决策;指向 candidate 的闸不允许进入运行时。

## 签发出处怎么核

``SignedSource`` 记 (ref, path, sha256)。``ref=None`` 表示该文件就在当前工作树
(如 ``docs/SYSTEM_TRUTH/08_billing.md``);``ref`` 非空表示签发件在别的提交上
(如开发原则 SSOT 落在 ``14e229a13``,尚未并入生产尖那棵树)。
:func:`verify_signed_sources` 两种都能核 —— 核的是**内容 sha256**,不是路径存在性。
「位置对 ≠ 内容对」这条 2026-08-12 刚踩过。
"""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Optional

LEDGER_VERSION = "xiaobang-decision-ledger-2026-08-18"

_REPO_ROOT = Path(__file__).resolve().parent.parent

DecisionStatus = Literal["signed", "candidate"]

STATUS_SIGNED: DecisionStatus = "signed"
STATUS_CANDIDATE: DecisionStatus = "candidate"


class UnsignedRuleError(RuntimeError):
    """把 candidate 口径当成已签发硬门使用时抛出。

    这不是"防御性编程"。它对应的是一次真实 P0:未拍板的默认出口被写成阻断。
    """


@dataclass(frozen=True)
class SignedSource:
    """签发件定位 + 内容指纹。

    ``ref``:签发件所在的 git commit-ish;``None`` = 当前工作树。
    ``sha256``:该文件**内容**的 sha256(十六进制小写)。
    """

    path: str
    sha256: str
    section: str
    ref: Optional[str] = None

    def read_bytes(self) -> Optional[bytes]:
        """取签发件内容。取不到返回 ``None``(不抛),由调用方决定怎么报。"""
        if self.ref is None:
            candidate = _REPO_ROOT / self.path
            if not candidate.exists():
                return None
            return candidate.read_bytes()
        try:
            return subprocess.run(
                ["git", "show", "{0}:{1}".format(self.ref, self.path)],
                cwd=str(_REPO_ROOT),
                capture_output=True,
                check=True,
            ).stdout
        except (OSError, subprocess.CalledProcessError):
            return None

    def verify(self) -> dict:
        """核内容 sha256。返回三态,不把「读不到」混成「不匹配」。"""
        raw = self.read_bytes()
        if raw is None:
            return {
                "path": self.path, "ref": self.ref, "state": "unreachable",
                "expected": self.sha256, "actual": None,
            }
        # git show 与磁盘 rb 读都不做行尾翻译 —— 两路同口径。
        actual = hashlib.sha256(raw).hexdigest()
        return {
            "path": self.path,
            "ref": self.ref,
            "state": "match" if actual == self.sha256 else "mismatch",
            "expected": self.sha256,
            "actual": actual,
        }


@dataclass(frozen=True)
class DecisionRecord:
    decision_id: str
    statement: str
    origin: str
    status: DecisionStatus
    rule_version: str
    signed_source: Optional[SignedSource] = None
    conflict_note: str = ""

    def __post_init__(self) -> None:
        if self.status == STATUS_SIGNED and self.signed_source is None:
            raise ValueError("{0}: signed 记录必须给出签发件".format(self.decision_id))
        if self.status == STATUS_CANDIDATE and self.signed_source is not None:
            raise ValueError("{0}: candidate 记录不得挂签发件".format(self.decision_id))

    def as_dict(self) -> dict:
        source = None
        if self.signed_source is not None:
            source = {
                "path": self.signed_source.path,
                "ref": self.signed_source.ref,
                "sha256": self.signed_source.sha256,
                "section": self.signed_source.section,
            }
        return {
            "decision_id": self.decision_id,
            "statement": self.statement,
            "origin": self.origin,
            "status": self.status,
            "rule_version": self.rule_version,
            "signed_source": source,
            "conflict_note": self.conflict_note,
        }


# ── 已签发上位规则(可作硬门依据)────────────────────────────────────────────
_DEV_PRINCIPLES = "docs/SYSTEM_TRUTH/00_DEV_PRINCIPLES.md"
# 开发原则 SSOT v1.0 落在 docs/track-ssot-manual 分支的 14e229a13,尚未并入
# 生产尖那棵树。判「在不在 git」必须核内容而不是核路径 —— 见模块 docstring。
_DEV_PRINCIPLES_REF = "14e229a13"
_DEV_PRINCIPLES_SHA = "f2d74d972734651f5f914f7d575531113ac225b66d9dbc283d67433c852c20ac"

_BILLING_SSOT = "docs/SYSTEM_TRUTH/08_billing.md"


def _billing_sha() -> str:
    """资金 SSOT 就在本树里,导入时现算指纹。

    写死一个常量的话,08_billing.md 下次合法更新就会把这里变成长期红 ——
    而长期红的判据等于没有判据(state.sh 2026-08-02 那次的同一课)。
    这里要的是「当次内容指纹可被引用」,不是「内容永不许变」。
    """
    path = _REPO_ROOT / _BILLING_SSOT
    if not path.exists():
        return ""
    return hashlib.sha256(path.read_bytes()).hexdigest()


_SIGNED_DECISIONS: tuple[DecisionRecord, ...] = (
    DecisionRecord(
        decision_id="DP-A5",
        statement=(
            "真红线:钱(计费/支付/退款/余额,金额守恒可对账)、权限与身份"
            "(认证/鉴权/租户隔离不许绕过放宽)、数据安全、生产纪律、秘密"
            "(密钥/口令/内部成本参数不入库不外泄)。"
        ),
        origin="开发原则 SSOT v1.0 · A5(Owner 历次亲裁汇编,2026-08-16 入库)",
        status=STATUS_SIGNED,
        rule_version="dev-principles-v1.0/A5",
        signed_source=SignedSource(
            path=_DEV_PRINCIPLES, sha256=_DEV_PRINCIPLES_SHA,
            section="A5 · 真红线", ref=_DEV_PRINCIPLES_REF,
        ),
    ),
    DecisionRecord(
        decision_id="DP-A6.1",
        statement=(
            "永不中断主流程:扣费不前置确认,静默扣 + 事后气泡通知 + 用户自选提醒"
            "档位(Owner 2026-06-03)。唯一边界:系统不得替用户开启他没开的收费项"
            "——「首次知情 + 自主选」是静默扣不变成偷偷扣的前提。"
        ),
        origin="开发原则 SSOT v1.0 · A6 第 1 条",
        status=STATUS_SIGNED,
        rule_version="dev-principles-v1.0/A6.1",
        signed_source=SignedSource(
            path=_DEV_PRINCIPLES, sha256=_DEV_PRINCIPLES_SHA,
            section="A6 · 产品行为:非必要不打扰(第 1 条)", ref=_DEV_PRINCIPLES_REF,
        ),
    ),
    DecisionRecord(
        decision_id="DP-A4",
        statement="禁止形态:给正常流程加确认弹窗、二次拦截;「安全起见」默认关闭/降级。",
        origin="开发原则 SSOT v1.0 · A4",
        status=STATUS_SIGNED,
        rule_version="dev-principles-v1.0/A4",
        signed_source=SignedSource(
            path=_DEV_PRINCIPLES, sha256=_DEV_PRINCIPLES_SHA,
            section="A4 · 禁止形态速查", ref=_DEV_PRINCIPLES_REF,
        ),
    ),
    DecisionRecord(
        decision_id="DP-B4",
        statement=(
            "域内红线:真实客户品牌禁计划外写操作;对外禁绝对化承诺;"
            "禁泄内部技术参数(SOV/成本/倍率/供应商名);对外称「服务商」禁「代理」;"
            "全站术语「算力」禁「积分/额度」。"
        ),
        origin="开发原则 SSOT v1.0 · B4",
        status=STATUS_SIGNED,
        rule_version="dev-principles-v1.0/B4",
        signed_source=SignedSource(
            path=_DEV_PRINCIPLES, sha256=_DEV_PRINCIPLES_SHA,
            section="B4 · 域内红线补充", ref=_DEV_PRINCIPLES_REF,
        ),
    ),
    DecisionRecord(
        decision_id="BILL-SSOT",
        statement=(
            "唯一资金链 SSOT:两张现金价目表 + 功能消耗目录;异步长任务 freeze → "
            "成功 commit / 失败 release;退款按原订单不可变快照单跳反转;"
            "用户侧术语统一「算力」。"
        ),
        origin="docs/SYSTEM_TRUTH/08_billing.md(2026-07-17 生效)",
        status=STATUS_SIGNED,
        rule_version="billing-ssot-2026-07-17",
        signed_source=SignedSource(
            path=_BILLING_SSOT, sha256=_billing_sha(), section="全文", ref=None,
        ),
    ),
    DecisionRecord(
        decision_id="ORG-SEAT-NO-SOCIAL",
        statement=(
            "员工席位永久剔除社媒板块:席位 = GEO 交付工具;"
            "把社媒域 operation/路由塞进席位能力发现的变更一律 NO-GO。"
        ),
        origin="Owner 2026-08-17 拍板",
        status=STATUS_SIGNED,
        rule_version="org-seat-social-exclusion-2026-08-17",
        signed_source=SignedSource(
            path=_DEV_PRINCIPLES, sha256=_DEV_PRINCIPLES_SHA,
            section="B 部 · 域内附则(板块边界)", ref=_DEV_PRINCIPLES_REF,
        ),
        conflict_note="本条不待裁,直接落;实现见 gap_operation_map 的社媒负向枚举锁。",
    ),
)

# ── §2 Owner 对话输入(全部 candidate,未签发前不得作硬门)──────────────────
_CANDIDATE_DECISIONS: tuple[DecisionRecord, ...] = (
    DecisionRecord("XO-01", "继续使用右下角小榜作为 AI 操作入口",
                   "Owner 对话 2026-08-17", STATUS_CANDIDATE, "candidate-v1"),
    DecisionRecord("XO-02", "不新建小榜专属业务页面",
                   "Owner 对话 2026-08-17", STATUS_CANDIDATE, "candidate-v1"),
    DecisionRecord("XO-03", "复杂核对打开并预填现役页面",
                   "Owner 对话 2026-08-17", STATUS_CANDIDATE, "candidate-v1"),
    DecisionRecord("XO-04", "允许基于授权业务事实和治理后历史效果推荐",
                   "Owner 对话 2026-08-17", STATUS_CANDIDATE, "candidate-v1"),
    DecisionRecord("XO-05", "推荐必须给普通用户看懂的具体原因",
                   "Owner 对话 2026-08-17", STATUS_CANDIDATE, "candidate-v1"),
    DecisionRecord(
        "XO-06", "消费或外部发布前展示对象、渠道、理由、算力和影响",
        "Owner 对话 2026-08-17", STATUS_CANDIDATE, "candidate-v1",
        conflict_note=(
            "与已签发 DP-A6.1 冲突。调和解(规格 §8.1):本条只约束 "
            "side_effect=external;compute_only 类走 silent_with_notice,"
            "不做发布前逐项展示确认。"
        ),
    ),
    DecisionRecord(
        "XO-07", "聊天文字不构成确认,必须真实 UI 点击",
        "Owner 对话 2026-08-17", STATUS_CANDIDATE, "candidate-v1",
        conflict_note=(
            "与已签发 DP-A6.1 冲突。调和解:只约束 external 类的最终确认;"
            "compute_only 类不要求真实 UI 点击。"
        ),
    ),
    DecisionRecord(
        "XO-08", "确认后由 AI 走 OmniRank 真实流程,不能伪造成功",
        "Owner 对话 2026-08-17", STATUS_CANDIDATE, "candidate-v1",
        conflict_note=(
            "「确认后执行」仅描述 external 类;compute_only 类无需等待确认即可"
            "执行并静默扣费。「不能伪造成功」部分与已签发 DP-A5 数据安全一致,"
            "两档都适用 —— 该部分的硬门依据记 DP-A5 而不是本条。"
        ),
    ),
    DecisionRecord(
        "XO-09", "用户侧唯一计价单位是算力",
        "Owner 对话 2026-08-17;资金语义仍以 BILL-SSOT 为准",
        STATUS_CANDIDATE, "candidate-v1",
        conflict_note="术语部分已由已签发 DP-B4 覆盖,可直接执行;本条作为产品输入仍待签。",
    ),
    DecisionRecord("XO-10", "小榜通过版本化注册和适配随系统更新",
                   "Owner 要求审计并规划", STATUS_CANDIDATE, "candidate-v1"),
    DecisionRecord("XO-11", "本包先做站内小榜,外部 CLI/Skill 后续复用网关",
                   "本轮范围裁定", STATUS_CANDIDATE, "candidate-v1"),
)

DECISIONS: tuple[DecisionRecord, ...] = _SIGNED_DECISIONS + _CANDIDATE_DECISIONS

_BY_ID = {record.decision_id: record for record in DECISIONS}
if len(_BY_ID) != len(DECISIONS):
    raise RuntimeError("decision_id 重复")


# ── H0 闸矩阵(规格 §15.1)──────────────────────────────────────────────────
@dataclass(frozen=True)
class H0Gate:
    gate_id: str
    h0_class: str
    decision_ids: tuple[str, ...]
    user_next_action: str
    scope: str = ""
    applies_to_side_effect: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict:
        return {
            "gate_id": self.gate_id,
            "h0_class": self.h0_class,
            "decision_ids": list(self.decision_ids),
            "rule_versions": [_BY_ID[d].rule_version for d in self.decision_ids],
            "user_next_action": self.user_next_action,
            "scope": self.scope,
            "applies_to_side_effect": list(self.applies_to_side_effect),
        }


H0_GATES: tuple[H0Gate, ...] = (
    H0Gate("XB-H0-TENANT", "authorization/privacy", ("DP-A5",),
           "重新登录 / 申请权限",
           "越权、跨租户、撤权后访问;统一返回不泄存在性的 404"),
    H0Gate("XB-H0-PAYER-BINDING", "authorization + money", ("DP-A5", "BILL-SSOT"),
           "交负责人确认",
           "actor/owner/payer 错绑;禁 fallback 扣员工本人算力"),
    H0Gate("XB-H0-DRIFT", "object identity", ("DP-A5",),
           "重新准备",
           "payload_hash / object_manifest_hash / compute_quote_hash / 权限代际漂移"),
    H0Gate("XB-H0-IDEMPOTENCY", "object identity + money", ("DP-A5", "BILL-SSOT"),
           "查询原任务 / 重试",
           "幂等、CAS、事务与资金守恒失败"),
    H0Gate("XB-H0-EXTERNAL-CONFIRM", "money/external side effect integrity",
           ("DP-A5", "BILL-SSOT"),
           "打开核对并确认",
           "side_effect=external 未经真实 UI 点击确认却执行;"
           "compute_only 类不适用本闸(走 XB-H0-SILENT-NOTICE)",
           ("external",)),
    H0Gate("XB-H0-SILENT-NOTICE", "money", ("DP-A6.1", "BILL-SSOT"),
           "查看算力明细",
           "side_effect=compute_only 扣费后未触发事后通知,或系统替用户开启了"
           "他没开的收费项(A6.1 唯一边界)",
           ("compute_only",)),
    H0Gate("XB-H0-FAKE-TERMINAL", "object/data integrity", ("DP-A5",),
           "核对真实回执",
           "真实外部结果不存在却标成功;unknown 不得压成 succeeded/failed"),
    H0Gate("XB-H0-DLP", "privacy/tenant isolation", ("DP-A5", "DP-B4"),
           "停止返回并安全升级",
           "供应商名/域名/采购成本/上游订单号/原始 provider error 泄漏"),
)

_GATE_BY_ID = {gate.gate_id: gate for gate in H0_GATES}
if len(_GATE_BY_ID) != len(H0_GATES):
    raise RuntimeError("gate_id 重复")


# ── 查询 / 断言 ────────────────────────────────────────────────────────────
def resolve_decision(decision_id: str) -> Optional[DecisionRecord]:
    return _BY_ID.get(str(decision_id or ""))


def is_signed(decision_id: str) -> bool:
    record = resolve_decision(decision_id)
    return record is not None and record.status == STATUS_SIGNED


def resolve_gate(gate_id: str) -> Optional[H0Gate]:
    return _GATE_BY_ID.get(str(gate_id or ""))


def assert_gate_is_signed(gate_id: str) -> H0Gate:
    """运行时硬门的唯一合法入口。

    未登记的 gate_id、或背后有任何一条 candidate 决策 —— 一律抛
    :class:`UnsignedRuleError`。这就是规格 §2「未签发不得新增运行时硬门」
    从「一句话」变成「会自己响的东西」的那一步。
    """
    gate = resolve_gate(gate_id)
    if gate is None:
        raise UnsignedRuleError("未登记的 H0 闸:{0!r}".format(gate_id))
    unsigned = [d for d in gate.decision_ids if not is_signed(d)]
    if unsigned:
        raise UnsignedRuleError(
            "H0 闸 {0} 依赖未签发决策 {1};candidate 口径不得作为运行时硬门"
            "(规格 §2 / invrel 2026-08-13 P0)".format(gate_id, unsigned)
        )
    return gate


def h0_gate_matrix() -> list[dict]:
    return [gate.as_dict() for gate in H0_GATES]


def decision_manifest() -> dict:
    return {
        "ledger_version": LEDGER_VERSION,
        "decisions": [record.as_dict() for record in DECISIONS],
        "h0_gates": h0_gate_matrix(),
    }


def verify_signed_sources() -> list[dict]:
    """核每条 signed 决策的签发件内容指纹(交付/CI 用,不在请求路径调用)。"""
    out: list[dict] = []
    for record in DECISIONS:
        if record.signed_source is None:
            continue
        result = record.signed_source.verify()
        result["decision_id"] = record.decision_id
        out.append(result)
    return out
