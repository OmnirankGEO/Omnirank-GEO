"""问题计划的身份、canonical hash 与编辑语义(规格 §15.2 / REV-01,02,03,10,12)。

纯域层:不碰库、不碰 HTTP。存储在 ``question_plan_store.py``。

🔴 两个身份键**必须分清**,这是本模块存在的头号理由:

``question_identity_key``(本模块产出)
    题目的**稳定逻辑身份**。改题文它 **不变**(REV-02:「改题后 question_key 不变、
    question_revision/hash 递增,旧监测不漂移」)。它由 (plan_id, 首次出现序号) 派生,
    与题文无关。

``geo_article_target_question_snapshots.question_key``(现役 · 别碰)
    ``CHARACTER(64)`` 的**内容哈希** + 全局 UNIQUE。改题文它**就变**。

两者语义相反。同名会让「改一个字」变成「换了一道题」,旧监测当场断链。
§0.5.1-7 要求去混淆,Review-CTO 2026-08-21 批准改名 —— 落地即本模块。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any, Literal, NamedTuple

#: canonical hash 的算法版本。改任何一位入 hash 的字段都必须升版 ——
#: 否则「不同 payload 得到同一 hash」会静默发生,而幂等正是靠 hash 判同。
CANONICAL_HASH_VERSION = "defgeo-question-plan-canonical-v1"

Mode = Literal["defensive", "offensive", "hybrid"]
ModeSide = Literal["defensive", "offensive"]
BrandExposure = Literal["named", "unnamed", "comparison"]


class PlannedQuestion(NamedTuple):
    question_identity_key: str
    question_revision: int
    global_ordinal: int
    text: str
    mode_side: ModeSide
    family_key: str
    brand_exposure: BrandExposure
    origin: Literal["system", "customer", "ai_suggested"]
    classifier_version: str | None


class PlanIdentityError(ValueError):
    """计划自身不自洽。不签发,而不是修一修凑合存下去。

    🔴 [2026-09-02] 多带两个**机器可读**的字段,规则本体一个字不动:

    ``code``
        哪一条规则不过。API 层据此选一句说人话的提示 ——
        以前所有规则都映成同一个 reason_key=None 的通用句
        (「这次提交的内容有一处填得不对」),她不知道哪一处、也没有下一步,
        违反开发原则「提示二选一:要么有明细 + 有修复动作,要么不显示」。
    ``side``
        跟"哪一侧"有关的规则(hybrid 缺一侧 / 单模式混入另一侧)带上侧别,
        让按钮能直接落到那一侧。

    为什么不在 API 层按**消息文本**分类:那是裸串匹配 ——
    改一个字提示就悄悄退回通用句,而且没有任何判据会红。

    🔴 ``code`` 默认 ``None`` 而**不是** ``"plan_identity"``:漏带 code 的新规则
       应当落**通用句**(诚实地说"有一处不对"),而不是被安上一句**具体但错误**的
       「题单的编号对不上了」—— 后者会把她指向一个根本不存在的问题。
       "不知道"与"知道且是身份问题"必须分得开。
    """

    def __init__(self, message: str, *, code: str | None = None,
                 side: str | None = None):
        super().__init__(message)
        self.code = code
        self.side = side


def new_plan_id() -> str:
    return str(uuid.uuid4())


def question_identity_key(plan_id: str, seq: int) -> str:
    """稳定逻辑身份键。

    只吃 ``(plan_id, seq)`` —— **刻意不吃题文**。
    吃了题文就变回内容哈希,REV-02 当场破。
    ``seq`` 是该题在本 plan 里**首次出现**时的序号,一旦分配终身不变
    (删题后重排 ordinal 不影响它 —— REV-03)。
    """
    if not plan_id or seq < 1:
        raise PlanIdentityError(f"非法身份输入 plan_id={plan_id!r} seq={seq}")
    digest = hashlib.sha256(f"{plan_id}|{seq}".encode("utf-8")).hexdigest()
    return f"q_{digest[:32]}"


def _canonical_question(q: PlannedQuestion) -> dict[str, Any]:
    """进 hash 的题目投影。字段顺序由 ``sort_keys`` 决定,不靠字典插入序。"""
    return {
        "identity": q.question_identity_key,
        "revision": q.question_revision,
        "ordinal": q.global_ordinal,
        "text": q.text,
        "mode_side": q.mode_side,
        "family": q.family_key,
        "exposure": q.brand_exposure,
        "origin": q.origin,
        "classifier_version": q.classifier_version,
    }


def canonical_hash(
    *,
    brand_id: int,
    profile_revision_id: str,
    mode: Mode,
    question_set_version: str,
    questions: tuple[PlannedQuestion, ...],
) -> str:
    """整份计划的 canonical hash。

    🔴 **题目按 ordinal 排序后入 hash,不按传入顺序** ——
       否则「同一份题单换个提交顺序」会得到不同 hash,幂等重放直接失效。
    🔴 用 ``sort_keys=True`` + ``ensure_ascii=False`` + 显式分隔符:
       Python 字典序、Unicode 转义、空格三者任一漂移都会让 hash 分叉
       (§19 变异 168 点名 "Unicode/key-order/边界编码分叉")。
    """
    if not questions:
        raise PlanIdentityError("空题单不得签发 canonical hash —— 零计划格不能启动 run")
    payload = {
        "v": CANONICAL_HASH_VERSION,
        "brand_id": brand_id,
        "profile_revision_id": profile_revision_id,
        "mode": mode,
        "question_set_version": question_set_version,
        "questions": [_canonical_question(q) for q in sorted(questions, key=lambda x: x.global_ordinal)],
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def request_content_hash(
    *,
    brand_id: int,
    profile_revision_id: str,
    mode: Mode,
    question_set_version: str,
    questions: tuple[tuple[str, str, str, str], ...],
) -> str:
    """**请求内容**哈希 —— 幂等用。与 ``canonical_hash`` 是两件不同的东西。

    🔴 为什么必须分开(这是被判据当场抓出来的一个真缺陷):

    ``canonical_hash`` 覆盖 ``question_identity_key``,而身份键由
    ``question_identity_key(plan_id, seq)`` 派生 —— ``plan_id`` **每次请求新生成**。
    于是「同一份题单提交两次」会得到两个不同的 canonical_hash,
    幂等唯一约束 ``(tenant, client_request_id, hash)`` **永远命中不了**,
    第二次请求安静地建出第二份题单。

    这个 bug 在纯函数层面完全看不出来(两个 hash 各自都算得对),
    只有真 HTTP 判据「换 HTTP key 但同 clientRequestId 应返回同一 plan」才照得出来。

    所以幂等键只吃**客户端真正提交的内容**:品牌、档案版本、模式、题集版本,
    以及逐题的 (题文, 侧, 题族, 曝光形态) 四元组按提交顺序。
    服务端生成的任何东西(plan_id / 身份键 / 时间)一律不进。

    ``questions`` 是四元组序列 ``(text, mode_side, family_key, brand_exposure)``,
    **按客户端提交顺序**(顺序本身就是内容:换序 = 换了一份题单的 ordinal 安排)。
    """
    payload = {
        "v": CANONICAL_HASH_VERSION,
        "kind": "request-content",
        "brand_id": brand_id,
        "profile_revision_id": profile_revision_id,
        "mode": mode,
        "question_set_version": question_set_version,
        "questions": [list(q) for q in questions],
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ══════════════════════════════════════════════════════════════════════════
# 平台集:规范化与幂等 hash(返修② 第 2/3 条)
# ══════════════════════════════════════════════════════════════════════════
#: run-request 幂等 hash 的算法版本。**不**复用 ``CANONICAL_HASH_VERSION`` ——
#: 二者覆盖面不同,共用一个版本号会让「只升其中一件」这种改动无处表达。
RUN_REQUEST_HASH_VERSION = "defgeo-run-request-v1"


def normalize_platform_keys(value: Any) -> Any:
    """平台集的**唯一**规范化谓词:去空白 → 丢空串 → 去重 → 定序。

    🔴 去重是资金面的事,不是洁癖
    ----------------------------
    ``planned_cells = 题数 × 平台数``,而它直接决定报价与冻结额。
    ``["kimi","kimi"]`` 把同一格算两遍 —— 客户为一份工作付两份钱,
    真跑的时候平台就那么一个。多扣的钱没有对应的交付物。

    🔴 为什么要定序
    ----------------
    平台集是**集合**:``["kimi","doubao"]`` 与 ``["doubao","kimi"]`` 是同一件事。
    不定序 ⇒ 同一件事两个 hash ⇒ 同一把幂等键会建出第二份 preview,
    幂等重放当场失效。

    🔴 为什么**不**做大小写折叠
    ---------------------------
    折叠会把两个可能真不同的平台键合并。平台键的等价关系归下游检索注册表管,
    不归本模块拍板。规范化只做「肯定安全」的那几件。

    非 list/tuple、或元素不是字符串时**原样退回** —— 类型错误交给 Pydantic 报 422,
    不在这里吞掉,也不在这里另造一套错误。
    """
    if not isinstance(value, (list, tuple)):
        return value
    seen: set[str] = set()
    out: list[str] = []
    for raw in value:
        if not isinstance(raw, str):
            return value
        key = raw.strip()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(key)
    return sorted(out)


def run_request_hash(*, plan_content_hash: str, platform_keys: Any) -> str:
    """run-preview 的**幂等**哈希:题单内容 +(规范化后的)平台集。

    🔴 为什么必须覆盖平台集(这是一个真缺陷)
    ------------------------------------------
    幂等唯一约束是 ``(tenant, idempotency_key, canonical_request_hash)``。
    平台集不进 hash 时,「同一把幂等键 + 换了平台集」会命中旧行,
    端点把**第一次**那份 preview 当幂等重放返回 ——
    她选了 4 个平台,拿到的是 1 个平台的报价与题单,而响应里写着「已受理」。

    🔴 为什么不直接把平台集加进 ``canonical_hash``
    ------------------------------------------
    ``canonical_hash`` 是**题单本体**的身份,题单预览端点也在算它。
    往里加一个只有 run 阶段才存在的字段,会让同一份题单在两个阶段算出两个身份。
    所以这里是**复合**:拿题单 hash 当一个字段,外层再包一层。
    题单 hash 自身逐字节不变,旧行为不回归。
    """
    keys = normalize_platform_keys(list(platform_keys or []))
    if not isinstance(keys, list) or not keys:
        raise PlanIdentityError("平台集为空或形状不合法 —— 不得签发 run 幂等 hash")
    payload = {
        "v": RUN_REQUEST_HASH_VERSION,
        "plan_content_hash": str(plan_content_hash),
        "platform_keys": keys,
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def validate_plan(mode: Mode, questions: tuple[PlannedQuestion, ...]) -> None:
    """签发前的自洽体检。任一项不过 → 不签发。"""
    if not questions:
        raise PlanIdentityError("空题单", code="plan_empty")

    keys = [q.question_identity_key for q in questions]
    if len(set(keys)) != len(keys):
        raise PlanIdentityError(
            "同一 plan 内 question_identity_key 重复 —— 身份键必须唯一",
            code="plan_identity")

    # [#157] 单题字数上限:**冻结时就拦**,不留到后台派发才炸。
    #   实证(2026-09-08):96 字品牌名生成的系统题 = 102 字 ⇒ 冻进题单后
    #   在 `run_executor` 派发时抛 pydantic ValidationError,而那里**没有**
    #   HTTP 出口把它翻成 422 —— 它落进 except Exception,归还 marker、反复重试,
    #   直到 attempts 耗尽由 sweeper 退款。用户看到「一直没跑起来,最后退了钱」,
    #   没有一句话说是题太长。
    #   🔴 口径复用 `services.diagnosis_question_pricing`,不写第二份 ——
    #      两份上限漂开时,表现是「冻结放过、提交拒绝」,而两边各自看都正常。
    from services.diagnosis_question_pricing import (
        MAX_QUESTION_CHARS, question_length_violation)

    for q in questions:
        n = question_length_violation(q.text)
        if n is None:
            continue
        # 🔴 [#157 · v11] 文案**只在 copy_registry**,这里只负责填空 ——
        #    内联 f-string 会让前端只能照抄一遍,而两份漂开时的表现是
        #    「弹窗说 100 字、按钮说别的」,两边各自看都正常。
        from services.defensive_geo.copy_registry import user_label

        raise PlanIdentityError(
            user_label("reason", "question_too_long").format(
                ordinal=q.global_ordinal, chars=n, limit=MAX_QUESTION_CHARS,
                excerpt=(q.text or "")[:24]),
            code="question_too_long",
        )

    ordinals = sorted(q.global_ordinal for q in questions)
    if ordinals != list(range(1, len(questions) + 1)):
        raise PlanIdentityError(
            f"ordinal 必须是 1..N 连续无重复,实得 {ordinals} —— "
            "断档会让「删了一题」和「有一题没渲染出来」在前端长得一样",
            code="plan_identity",
        )

    # mode 与 mode side 的关系:单模式计划不许偷塞另一侧的题
    # (§19 变异 165「单模式 Core 偷塞另一侧 sample」的上游同形)。
    sides = {q.mode_side for q in questions}
    if mode == "defensive" and sides - {"defensive"}:
        raise PlanIdentityError(f"defensive 计划出现 {sorted(sides)} 侧的题",
                                code="plan_side_mismatch", side="offensive")
    if mode == "offensive" and sides - {"offensive"}:
        raise PlanIdentityError(f"offensive 计划出现 {sorted(sides)} 侧的题",
                                code="plan_side_mismatch", side="defensive")
    if mode == "hybrid" and sides != {"defensive", "offensive"}:
        raise PlanIdentityError(
            f"hybrid 计划两侧都必须有题,实得 {sorted(sides)} —— "
            "只有一侧的 hybrid 是伪装成混合的单模式",
            code="plan_hybrid_needs_both_sides",
            # 缺的是哪一侧 —— 让按钮能直接落到那一侧,而不是让她自己找。
            side=("offensive" if "offensive" not in sides else "defensive"),
        )

    for q in questions:
        if not q.text.strip():
            raise PlanIdentityError(f"{q.question_identity_key} 题文为空",
                                    code="plan_question_blank", side=q.mode_side)
        if q.question_revision < 1:
            raise PlanIdentityError(f"{q.question_identity_key} revision 必须 >= 1",
                                    code="plan_identity")


def counts(questions: tuple[PlannedQuestion, ...]) -> tuple[int, int, int]:
    """(defensive, offensive, total)。守恒由调用方与 DB CHECK 双重把关。"""
    d = sum(1 for q in questions if q.mode_side == "defensive")
    o = sum(1 for q in questions if q.mode_side == "offensive")
    return d, o, d + o


def apply_edit_revision(
    questions: tuple[PlannedQuestion, ...],
    *,
    identity_key: str,
    new_text: str,
) -> tuple[PlannedQuestion, ...]:
    """改题 = **身份不变、revision 递增**(REV-02),返回新题单。

    这个函数就是 REV-02 的可执行形式:它**结构上做不到**改身份键 ——
    身份键从旧题原样带过来,新题文只进 ``text``。
    """
    out = []
    hit = False
    for q in questions:
        if q.question_identity_key == identity_key:
            hit = True
            out.append(q._replace(text=new_text, question_revision=q.question_revision + 1))
        else:
            out.append(q)
    if not hit:
        raise PlanIdentityError(f"要改的题不在计划里:{identity_key}")
    return tuple(out)


def remove_question(
    questions: tuple[PlannedQuestion, ...], *, identity_key: str
) -> tuple[PlannedQuestion, ...]:
    """删题 —— **不重排剩余题的 ordinal**(REV-03)。

    ⚠️ 这会让 ordinal 出现断档,而 ``validate_plan`` 要求 1..N 连续。
       这不是矛盾:删题后必须由调用方**重新编号并给每题递增 revision**,
       或者说 —— 删题产生的是一份**新 revision 的计划**,不是原计划少一行。
       本函数只负责「拿掉」,连续性由 ``renumber_for_new_revision`` 负责,
       且那一步会显式记录旧 ordinal → 新 ordinal 的映射,不静默重排。
    """
    out = tuple(q for q in questions if q.question_identity_key != identity_key)
    if len(out) == len(questions):
        raise PlanIdentityError(f"要删的题不在计划里:{identity_key}")
    if not out:
        raise PlanIdentityError("不得删空题单")
    return out


def renumber_for_new_revision(
    questions: tuple[PlannedQuestion, ...],
) -> tuple[tuple[PlannedQuestion, ...], dict[str, tuple[int, int]]]:
    """为新 revision 重新编号,返回 (新题单, {身份键: (旧 ordinal, 新 ordinal)})。

    🔴 **身份键一个都不变**。变的只有 ordinal,且变化被逐题记录下来 ——
       REV-03 禁的是「删题后**静默**重排已签 ordinal」,不是禁止编号存在。
       把映射交出去,调用方才能在审计里说清「第 3 题现在是第 2 题」。
    """
    ordered = sorted(questions, key=lambda x: x.global_ordinal)
    mapping: dict[str, tuple[int, int]] = {}
    out = []
    for new_ord, q in enumerate(ordered, start=1):
        mapping[q.question_identity_key] = (q.global_ordinal, new_ord)
        out.append(q._replace(global_ordinal=new_ord))
    return tuple(out), mapping
