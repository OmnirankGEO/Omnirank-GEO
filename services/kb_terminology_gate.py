"""KB 术语门 · **运行时**实现(R3-P7 ①②)。

## 为什么从 tests/ 搬到 services/

R3-P6 把四把分类器放在 ``tests/xiaobang_vnext_2026_08_18/terminology_domains.py``。
那意味着它**只在 CI 里存在** —— 而 KB 的写入路径在生产里天天开着:
管理员在帮助中心改一条 FAQ,``_trigger_xiaobang_faq_reindex()`` 就会把新文案
灌进 ``kb_chunks``,小榜下一句就照着念。判据拦不住运行时发生的事。

所以分类器搬到运行时模块,判据侧改成**re-export 同一份实现**(不是抄一份):
两份实现必然各自漂移,而漂移的方向一定是"线上那份更松"。

## 门开在哪(单一收口点)

``db/kb_db.py`` 的两个 writer —— ``insert_chunk`` 与
``replace_chunks_transactionally`` —— 是 **admin 触发重建**与**全量重建**
唯一共同的收口点(writer 调用方 census 见
``tests/.../kb_index_sources.py``:全仓只有 ``tools/xiaobang_kb_indexer.py``
与 ``tools/xiaobang_system_kb.py`` 两个调用方,它们都从这两个函数下去)。
门开在收口点 = **fail-closed**:任何新增的写入路径都自动被管住,
不需要新增路径的人记得来加校验。

API 侧(``api/faq_api.py``)另有一道**同源**的前置校验,目的不同:
让管理员在**提交那一刻**就拿到「哪个词、改成什么」,而不是提交成功、
后台重建静默失败、下次问小榜才发现。两道都用本模块,不各写一套。

## 报错必须带改法

拒绝而不给改法,下一个人只会把门关掉。所以 :class:`KbTerminologyViolation`
的 message 逐条给出 **原句 + 命中的词 + 建议写法**。
"""

from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path

_LOGGER = logging.getLogger("GEO-KbTerminology")

#: 仓内签发物。测试断言它存在且内容指纹与签发原件一致。
#: 🔴 按**模块位置**解析,不按进程 cwd —— 运行时(容器 /app)与判据(仓库根)
#:    的 cwd 不同,写相对路径会让运行时门在容器里永远找不到裁定书。
_REPO_ROOT = Path(__file__).resolve().parents[1]
RULING_PATH = _REPO_ROOT / "docs/xiaobang/vnext/RULING_TERMINOLOGY_EDU_2026-08-18.md"
RULING_SHA256 = "a05b6832480cb285e1b6558440c2cf34b29f06bd10a980c4509c9adff64c534c"

#: 错误合同里下发给调用方的规则标识(R3-P8 ⑤)。
#: 报「你违反了规则」而不说**哪条规则的哪个版本**,收到的人无从核对。
RULING_RULE_ID = "RULING_TERMINOLOGY_EDU_2026-08-18"
RULING_VERSION = "2026-08-19-erratum-1"

#: 子句切分。语义判据必须在**子句**内判,跨子句共现会大量误报
#: (例:「要提现去收益结算页,要开算力去库存页」两件事各自成立)。
_CLAUSE = re.compile(r"[^。;；,，、\n]+")

# ── ② 算力池命名:这些组合词永远是错的,不看上下文 ──────────────────────
POOL_NAMES: tuple[str, ...] = (
    "工具额度", "发布额度", "赠送额度", "充值额度", "佣金额度",
    "额度包", "额度钱包", "额度池", "额度账户", "主额度", "出厂额度", "到账额度",
)

# ── ① 计价单位语境:消耗/扣减/余额 ───────────────────────────────────────
PRICING_ANCHORS: tuple[str, ...] = (
    "消耗", "扣", "余额", "剩余", "可用", "到账", "充值", "返利",
    "划拨", "撤回", "不足", "结余",
)

# ── ③ 容量/配额豁免:裁定说「不推荐但不算错义」,所以**不报红** ──────────
QUOTA_EXEMPT: tuple[str, ...] = (
    "上限", "名额", "配额", "封顶", "硬顶", "频控",
    "每月", "每日", "单次", "月度", "本月", "本周期", "月卡", "套餐",
)

# ── ④ 非算力域锚点:出现即交给资金语义判据,①② 锁不插手 ─────────────────
CASH_ANCHORS: tuple[str, ...] = (
    "收益", "提现", "打款", "佣金", "毛利", "税", "保险", "营收",
    "货款", "人民币", "银行卡", "手续费", "本金", "提取",
)

#: 🔴 **绝不能**成为算力池的钱名词。它们后面直接跟「算力」就是错义。
#:    注意 `佣金` / `提现` **不在**此列:本系统的佣金本来就以算力入账
#:    (原文「佣金积分…满 ¥100(13000 积分)可提现」),「佣金算力」是合法池名。
CASH_POSSESSORS: tuple[str, ...] = (
    "收益", "金额", "营收", "货款", "毛利", "税额", "保险", "手续费", "本金",
)

#: 钱名词与「算力」之间只隔量词/结构助词 = 它在**领属**这个量 → 错义。
#: 直接相连(0 字)按复合词处理,交给 :data:`HARD_FORBIDDEN_COMPOUNDS` 单独判。
_POSSESSION = re.compile(
    "(" + "|".join(CASH_POSSESSORS) + ")"
    r"(的|之)?(剩余|可用|未用|结余|总|本笔|同一笔|待|已|当前|全部)+算力"
)

#: 直接相连也一定错的复合词。
HARD_FORBIDDEN_COMPOUNDS: tuple[str, ...] = (
    "收益算力", "金额算力", "保险算力", "毛利算力", "税额算力",
    "货款算力", "营收算力", "手续费算力", "本金算力",
)

#: 媒体/文章**条数**域:它本来就不是算力,写成算力同样是错义。
_COUNT_MISUSE = re.compile(r"(算力\s*\d+\s*[条篇]|\d+\s*[条篇]算力|交付算力|总算力\s*[条篇])")


def ruling_fingerprint_ok() -> bool:
    if not RULING_PATH.exists():
        return False
    return hashlib.sha256(RULING_PATH.read_bytes()).hexdigest() == RULING_SHA256


def clauses(text: str) -> list[str]:
    return [c.strip() for c in _CLAUSE.findall(text) if c.strip()]


def domain12_violations(text: str) -> list[str]:
    """① 计价单位 / ② 池名 里残留的「额度」。

    ③ 配额语境与 ④ 现金语境**不报** —— 前者裁定说不算错,后者归资金语义判据。
    """
    out: list[str] = []
    for clause in clauses(text):
        hit_pool = [p for p in POOL_NAMES if p in clause]
        if hit_pool:
            out.append("②池名 {0} → {1}".format(hit_pool, clause[:70]))
            continue
        if "额度" not in clause:
            continue
        if any(q in clause for q in QUOTA_EXEMPT):
            continue          # ③ 不推荐但不算错义
        if any(c in clause for c in CASH_ANCHORS):
            continue          # ④ 交给 cash_semantic_violations
        if any(a in clause for a in PRICING_ANCHORS):
            out.append("①计价单位 → {0}".format(clause[:70]))
    return out


def cash_semantic_violations(text: str) -> list[str]:
    """④ 非算力域被写成了「算力」—— 资金语义错误。

    禁词锁抓不到这一类:「剩余算力」四个字本身完全合法,错的是它站在
    「同一笔收益」后面。所以判据打的是**领属关系**,不是词表。
    """
    out: list[str] = []
    for clause in clauses(text):
        for compound in HARD_FORBIDDEN_COMPOUNDS:
            if compound in clause:
                out.append("④钱被写成算力({0}) → {1}".format(compound, clause[:70]))
        match = _POSSESSION.search(clause)
        if match:
            out.append("④钱领属算力({0}) → {1}".format(match.group(0), clause[:70]))
        count = _COUNT_MISUSE.search(clause)
        if count:
            out.append("④条数被写成算力({0}) → {1}".format(count.group(0), clause[:70]))
    return out


# ── 已废制度 / 具体比例(R3 · Review 2026-08-18 指令)────────────────────────
#: 🔴 「18%/3% 多级推荐分成」已于 2026-05-30 废止
#:    (在码实证:`frontend/src/pages/Agent/ProfitDashboard.tsx:4`
#:     「[2026-05-30 D1] 代理 18%/3% 推荐佣金已废(纯工厂模式取代)」;
#:     CLAUDE.md 资金节同述;08_billing §邀请只记录获客来源)。
#:    生产小榜此前一直在照 KB 讲解这套已死制度 —— 这是**存量病**,不是本包引入的。
#:
#: 锁的是**带比率的制度描述**,不是「直推/间推」这两个词本身:
#: UI 对历史行仍会渲染「直推收益 / 间推收益」标签,KB 必须能解释那个标签,
#: 否则用户看见标签而助手说"没这回事",助手就成了错的那一方。
_NOT_CLAUSE_END = r"[^。;；\n]"
_DEAD_SCHEME_PATTERNS: tuple[tuple[str, str], ...] = (
    ("多级分成比率", r"(直推|间推)" + _NOT_CLAUSE_END + r"{0,8}\d+\s*%"),
    ("分成制度比率", r"(推荐分成|分销|返佣)" + _NOT_CLAUSE_END + r"{0,10}\d+\s*%"),
    ("元→算力换算", r"[¥￥]\s*\d[\d,]*\s*[（(]\s*\d[\d,]*\s*算力"),
    ("元→算力换算2", r"\d[\d,]*\s*算力\s*[）)]\s*(可提现|到账|折算)"),
    # 🔴 两个语序都要打。R3-P5 的锁只写了「手续费 5%」这一种,而 FAQ seed 里
    #    真正的写法是「扣 5% 手续费」—— 数字在前,锁恒绿。窄口径 = 静默放行。
    ("手续费率", r"(手续费\s*\d+(\.\d+)?\s*%)|(\d+(\.\d+)?\s*%\s*手续费)"),
    # 「梯度赠送(约 10%~30%)」「送 10%–30% 算力」都属写死赠送比例:
    # 08_billing 明写比例是后台可调系数,文档背了数字就会在系数改动后继续讲旧口径。
    ("赠送加成率", r"(多送|加送|额外送|阶梯赠送|梯度赠送|赠送比例|赠送)"
                   + _NOT_CLAUSE_END + r"{0,8}\d+\s*%"),
    # R3-P2 ③:referral 的「15% 返利」同属写死比率。SSOT 已裁(08_billing:
    # 比例是后台可调系数,文档不背数字),Owner 可否决 —— 否决就删本行。
    ("返利比率", r"(返利|返点|回赠)" + _NOT_CLAUSE_END + r"{0,8}\d+\s*%"),
    ("比率修饰返利", r"\d+\s*%" + _NOT_CLAUSE_END + r"{0,4}(返利|赠送算力|算力返利)"),
    # 算力量 与 人民币单价 同子句出现 = 隐式泄露算力→元倍率。
    # 实证:FAQ seed「整体 39000 积分 · 平均每词 ¥3.9」—— 上面那条
    # 「元→算力换算」要求 ¥ 在前、括号里跟算力,这种「算力在前、¥ 在后」的
    # 写法它一个都打不到。
    ("算力单价混写", r"\d[\d,]*\s*算力" + _NOT_CLAUSE_END + r"{0,12}[¥￥]\s*\d"),
    ("算力单价混写2", r"[¥￥]\s*\d[\d.,]*\s*/\s*\d*\s*算力"),
)
_COMPILED_DEAD = tuple((n, re.compile(p)) for n, p in _DEAD_SCHEME_PATTERNS)


#: 🔴 **比率区间/校验语境豁免**。
#:    `agent-pricing.md` 里的「返利比例(0~100%)」「返利比例填了超过 100%」讲的是
#:    **服务商自己填的那个输入框的取值范围**,不是平台背了一个固定比率 ——
#:    第一版的返利锁把它判红了。锁太宽会逼人把合法说明删掉,
#:    最后门被关掉;所以这里显式豁免"区间/校验"语境。
_RATE_RANGE_EXEMPT = re.compile(
    r"(\d+\s*[~～至]\s*\d+\s*%)|(之间)|(范围)|(超过\s*100\s*%)|(须在)|(上限)|(不超过)"
)


def dead_scheme_violations(text: str) -> list[str]:
    """已废制度描述 / 具体资金比率。

    08_billing:「所有比例/倍率都是系统系数,后台动态可调 —— 文档不背具体数字,
    以生产配置为准」。KB 背了数字,就会在数字改动后**继续对用户讲旧口径**,
    而且没人会收到通知。
    """
    out: list[str] = []
    for clause in clauses(text):
        ranged = _RATE_RANGE_EXEMPT.search(clause) is not None
        for name, pattern in _COMPILED_DEAD:
            # 只有"比率"类规则吃区间豁免;换算/已废制度类不吃
            # (「¥100(13000 算力)」写成区间也还是泄倍率)。
            if ranged and name in ("返利比率", "比率修饰返利", "赠送加成率", "手续费率"):
                continue
            hit = pattern.search(clause)
            if hit:
                out.append("已废制度/具体比率({0}:{1}) → {2}".format(
                    name, hit.group(0)[:24], clause[:70]))
    return out


# ── KB 索引口径的残留门(R3-P6 · WO 2026-08-19 verify ④)─────────────────
#: 旧入口名。它跟语义域无关,是硬错:页面上根本没有这个入口。
STALE_ENTRY_NAMES: tuple[str, ...] = ("写作大厅",)

#: 旧计价单位。DP-B4 明令,本仓不存在合法语义域,按裸词禁。
LEGACY_UNIT = "积分"


def kb_index_residue(text: str) -> list[str]:
    """**进小榜索引的那批源**的残留门。判据 = 线上口径,不是语义域。

    与 :func:`domain12_violations` / :func:`cash_semantic_violations` /
    :func:`dead_scheme_violations` 三把锁**不同层**:那三把按裁定分域管全站
    用户可见文案;这一把只管**会被灌进 kb_chunks 的内容源**,验收线是
    「线上 kb_chunks 含『积分』=0 且含『额度』=0」(WO 2026-08-19 · 34 班
    verify ④ 挂的账)。

    ## 为什么 KB 里连 ③ 域的「额度」也要清

    裁定 ③ 说 ``额度`` 在上限/名额语境「不推荐但**不算错义**」——所以
    :func:`domain12_violations` 对它不报红,那条不变。但 ③ 同时给了**推荐写法**
    「算力上限 / 名额」,而现役组织中心 UI 已经整屏说「算力上限」
    (``OrganizationCenter.tsx``:``额度``=0 / ``算力``=39,34 班并车实测)。
    KB 讲一个屏幕上不存在的词,用户就照着找不到那个入口 —— 这是 **UI parity**
    问题,不是语义域问题,所以它是**另一把锁**,而不是把 ①② 锁改宽。

    ## 🔴 ④ 域必须豁免

    裁定对 ④「禁止替换」。因此本门对含现金锚点的子句放行 ——
    绝不能靠把「保险额度」改成「保险算力」来把计数凑成 0。
    那种改法会被 :func:`cash_semantic_violations` 当场判红:
    两把锁方向相反,必须配对使用(判据见 ``test_terminology_gate``)。
    """
    out: list[str] = []
    for clause in clauses(text):
        if LEGACY_UNIT in clause:
            out.append("旧计价单位「{0}」 → {1}".format(LEGACY_UNIT, clause[:70]))
        for entry in STALE_ENTRY_NAMES:
            if entry in clause:
                out.append("旧入口名「{0}」 → {1}".format(entry, clause[:70]))
        # 🔴 [R3-P8 ④] **② 域池名判定必须先于 ④ 域现金锚豁免**。
        #    实测漏放行:「佣金额度不足」里 `佣金` 是现金锚点,而 `佣金额度` 本身是
        #    ② 域死池名 —— 按「④ 出现即豁免」的读法它被放行了,而它其实该改。
        #    池名是**我们自己给算力池起的名字**,含不含现金词都不改变「它是算力池」;
        #    ④ 域保护的是「别把别人的钱说成算力」。两者只在「池名恰好含现金词」
        #    这一种写法上撞车,而那种写法本身就该改。
        #    裁定依据:签发物「勘误 · ②④ 域优先级」(Review-CTO 2026-08-19)。
        hit_pool = [name for name in POOL_NAMES if name in clause]
        if hit_pool:
            out.append("②池名 {0}(先于 ④ 豁免) → {1}".format(hit_pool, clause[:70]))
        # 🔴 [R3-P9 ⑤] ③ 域**裸词**(不是池名、不含现金锚点的「额度」)
        #    已从本门移出 → :func:`kb_index_advisories`(warn 不 block)。
        #    原因见该函数 docstring:签发裁定说 ③「不推荐但**不算错义**」,
        #    门比签发规则严 ⇒ 门与规则打架 ⇒ 输的一定是门。
    return out


def kb_index_advisories(text: str) -> list[str]:
    """③ 域裸词的 **advisory**(提醒,不拦)。

    ## 为什么降级(R3-P9 ⑤ · Review-CTO 裁定)

    R3-P6/P7 把「上限/名额语境里的裸『额度』」也当硬门拦。那超出了签发裁定:
    裁定 ③ 的原话是「**不推荐但不算错义**」。门比它所依据的规则更严,会有两个后果:

    * 写文案的人被一条「规则里查不到」的红拦住 —— 他去查裁定,发现裁定说这不算错,
      于是下一步不是改文案,是**把门关掉**;
    * 门和签发物各说各话之后,「以哪个为准」变成每次都要重新吵一遍的事。

    所以按 Review 裁定:降为 advisory。**如果确实认为 KB 域该更严,
    正确做法是提请把裁定 ③ 升一个规则版本,而不是让门私自加严。**

    ② 域池名(``工具额度`` / ``佣金额度`` 等)不受影响 —— 它们在
    :func:`kb_index_residue` 里**照样是硬门**,因为那是我们自己给算力池起错的名字,
    裁定对它的定性是「永远是错的,不看上下文」。
    ④ 域现金锚点同样不动(裁定禁止替换)。
    """
    out: list[str] = []
    for clause in clauses(text):
        if any(name in clause for name in POOL_NAMES):
            continue                      # ② 域已由硬门收走,不重复提醒
        if "额度" in clause and not any(c in clause for c in CASH_ANCHORS):
            out.append("(建议)③域:「额度」在上限/名额语境建议写「算力上限」/「名额」 → {0}"
                       .format(clause[:70]))
    return out


def all_violations(text: str) -> list[str]:
    return (domain12_violations(text)
            + cash_semantic_violations(text)
            + dead_scheme_violations(text))


# ══════════════════════════════════════════════════════════════════════════
# 运行时门(R3-P7 ①)
# ══════════════════════════════════════════════════════════════════════════

class KbTerminologyViolation(ValueError):
    """KB 写入被术语门拒绝。

    携带 ``rule_id`` / ``version``(R3-P8 ⑤):报「你违反了规则」而不说**哪条规则的
    哪个版本**,收到的人无从核对 —— 尤其是这条规则本身刚出过勘误。
    """

    def __init__(self, where: str, problems: list[str], fixes: list[str]):
        self.where = where
        self.problems = list(problems)
        self.fixes = list(fixes)
        self.rule_id = RULING_RULE_ID
        self.version = RULING_VERSION
        super().__init__(
            "[{0}] KB 术语门拒绝写入,命中 {1} 处:\n  - {2}\n改法:\n  - {3}".format(
                where, len(problems), "\n  - ".join(problems[:10]),
                "\n  - ".join(fixes[:10]) or "见上方命中说明",
            )
        )


#: 命中类型 → 改法。**拒绝而不给改法,下一个人只会把门关掉**,
#: 所以这张表按命中前缀逐条给出「改成什么」。
_FIX_HINTS: tuple[tuple[str, str], ...] = (
    ("旧计价单位", "把「积分」改成「算力」(DP-B4:用户侧唯一计价单位 = 算力)"),
    ("旧入口名", "把「写作大厅」改成现役入口名(页面上没有这个入口了)"),
    ("KB 索引口径",
     "把「额度」改成「算力」(消耗/余额语境)或「算力上限」/「名额」(上限语境);"
     "现役组织中心 UI 用的就是「算力上限」。**现金语境(收益/提现/手续费)保持原样,"
     "改成算力属资金语义错误**"),
    ("②池名", "算力池按现行模型命名:「充值算力」/「赠送算力」"),
    ("①计价单位", "消耗/扣减/余额语境一律用「算力」"),
    ("④钱被写成算力", "🔴 改回钱的说法(剩余金额/剩余收益)—— 这里是钱不是算力"),
    ("④钱领属算力", "🔴 同上:钱名词后面不能跟算力"),
    ("④条数被写成算力", "条数就写条数,不要写成算力"),
    ("已废制度/具体比率",
     "去掉写死的比率/换算数字,改成「以页面显示为准」"
     "(08_billing:比例是后台可调系数,文档不背数字)"),
)


def violation_contract(exc: "KbTerminologyViolation", *, message: str,
                       code: str = "KB_TERMINOLOGY_REJECTED") -> dict:
    """错误合同(R3-P8 ⑤)。

    `rule_id` + `version` 指向签发物本身,调用方可以拿它去核对;
    `problems` 指得出是哪句话,`fixes` 说改成什么。三样缺一样,收到的人就只能猜。
    """
    return {
        "code": code,
        "message": message,
        "rule_id": exc.rule_id,
        "version": exc.version,
        "ruling_path": str(RULING_PATH.name),
        "problems": exc.problems[:10],
        "fixes": exc.fixes[:10],
    }


def kb_write_violations(text: str) -> list[str]:
    """KB **写入路径**用的全量判据 = 四把锁并起来。

    与判据侧同一份实现 —— 判据 import 本模块,不另抄一份
    (两份必然各自漂移,而漂移方向一定是线上那份更松)。
    """
    return (kb_index_residue(text)
            + cash_semantic_violations(text)
            + dead_scheme_violations(text))


def fixes_for(problems: list[str]) -> list[str]:
    out: list[str] = []
    for problem in problems:
        for prefix, hint in _FIX_HINTS:
            if problem.startswith(prefix) and hint not in out:
                out.append(hint)
                break
    return out


def _warn_advisories(text: str, *, where: str) -> None:
    """[R3-P9 ⑤] advisory 只写日志,**不拦**。

    降级不等于删掉:降成 advisory 之后仍然要留下痕迹,否则「不推荐」就等于
    「无所谓」,KB 会慢慢漂回去。
    """
    notes = kb_index_advisories(text or "")
    if notes:
        _LOGGER.warning("[kb-terminology] %s 有 %d 条建议(不拦):%s",
                        where, len(notes), notes[:5])


def assert_kb_text_clean(text: str, *, where: str) -> None:
    """单段文本过门。命中即抛,**不清洗后放行**。

    清洗会把「谁把它放进去的」变成静默问题 —— 与
    ``services/xiaobang_facade_dlp.assert_facade_clean`` 同一条纪律。
    """
    _warn_advisories(text, where=where)
    problems = kb_write_violations(text or "")
    if problems:
        raise KbTerminologyViolation(where, problems, fixes_for(problems))


def assert_chunk_rows_clean(rows, *, where: str) -> None:
    """批量写入过门。逐行扫 ``source_title`` + ``content``。

    🔴 **在任何一行落库之前**全部扫完再决定 —— 边扫边写会留下半份 release,
    而 ``replace_chunks_transactionally`` 是 clear-then-insert,
    半份 = 用户当场问不出答案。
    """
    problems: list[str] = []
    # 🔴 [WO-A ⑤ · 2026-08-20] ``fixes_for`` 按**命中前缀**匹配改法,而这里给
    #    ``problems`` 加了 ``<slug>: `` 前缀 ⇒ 一条都匹配不上 ⇒ **批量写径的异常
    #    从来没带过改法**(单段那条 ``assert_kb_text_clean`` 有,因为它不加前缀)。
    #    拒绝而不给改法,下一个人只会把门关掉。所以另留一份**未加前缀**的原始命中
    #    专门喂 ``fixes_for``;展示给人的 ``problems`` 仍带 slug —— 得指得出是哪一条。
    raw_hits: list[str] = []
    for row in rows:
        slug = str((row or {}).get("source_slug") or "?")
        blob = "{0}\n{1}".format(
            (row or {}).get("source_title") or "", (row or {}).get("content") or "")
        _warn_advisories(blob, where="{0}/{1}".format(where, slug))
        for hit in kb_write_violations(blob):
            raw_hits.append(hit)
            problems.append("{0}: {1}".format(slug, hit))
    if problems:
        raise KbTerminologyViolation(where, problems, fixes_for(raw_hits))


# ══════════════════════════════════════════════════════════════════════════
# corpus lint 扩展面(包 C② · WORKORDER_XIAOBANG_SOLUTION_FIRST_AI_2026-08-20)
#
# 🔴 为什么这几条**不进** `all_violations`:
#    `all_violations` 挂在**运行时写入门**上(`api/faq_api.py:542` /
#    `db/faq_db.py:498` 的 `assert_kb_text_clean`),是 fail-closed 的 ——
#    往那里加规则 = 线上管理员写 FAQ 可能被当场拒。
#    本节这几条是**语料 lint 面**(离线扫全库、给人看),由
#    `tools/xiaobang_kb_lint.py` 消费。规则住在同一个模块里(工单 §7.4
#    「禁止第二套 corpus lint」),但**执行点不同**,风险面也不同。
#    要把某一条升成运行时硬门,请单独出 RFC 并给出它在存量语料上的红/绿分母。
#
# 🔴 Review 2026-08-21 的实施要求:**打语境,不打裸词表**。
#    我在开工 census 里逐处判读过 25 处「DeepSeek/Kimi/豆包」——**0 处真泄漏**:
#      · 20 处是「四大 AI 引擎(通义、DeepSeek、Kimi、豆包)同步检测」——
#        那是**我们卖的东西**(被检测的目标引擎),不是内部供应商;
#      · 1 处是禁令原文本身(`agents/xiaobang_presets.py:15`);
#    裸词表规则会把这 25 处全判红,逼人删掉产品说明。所以下面的供应商规则
#    只在「**我们自己**用什么模型」这个语境里才判。
# ══════════════════════════════════════════════════════════════════════════

#: 供应商名(仅用于**语境**判断,不单独构成违规)
_SUPPLIER_NAMES = ("deepseek", "kimi", "moonshot", "豆包", "doubao",
                   "通义", "qwen", "千问", "元宝", "gpt", "claude")

#: 「我们自己用的模型」语境 —— 只有这种说法才是供应商穿透。
_OUR_MODEL_CONTEXT = re.compile(
    r"(我们|平台|系统|小榜|本产品|底层|后台|内部)"
    r"[^。;；\n]{0,12}"
    r"(用的是|使用的是|基于|采用|调用的是|驱动|接的是|跑在)"
)
#: 反向:「目标/被检测/被推荐的引擎」语境 —— 合法,必须豁免。
_TARGET_ENGINE_CONTEXT = re.compile(
    r"(AI\s*引擎|AI\s*搜索|搜索引擎|被引用|被推荐|收录|检测|监测|问了|回答里|榜单|覆盖)"
)
#: 反向:禁令原文本身(「不要提 X」)—— 提到 ≠ 泄漏。
_PROHIBITION_CONTEXT = re.compile(r"(不要|禁止|不许|别)[^。;；\n]{0,10}(提|说|透露|暴露)")


def supplier_leak_violations(text: str) -> list[str]:
    """内部模型供应商穿透 —— **按语境判**,不按词表判。

    判红:「我们底层用的是 DeepSeek」。
    不判红:「四大 AI 引擎(通义、DeepSeek、Kimi、豆包)同步检测」(产品本身);
            「不要在答案里提 DeepSeek / 通义 / 豆包」(禁令原文)。
    """
    out: list[str] = []
    for clause in clauses(text):
        low = clause.lower()
        if not any(name in low for name in _SUPPLIER_NAMES):
            continue
        if _PROHIBITION_CONTEXT.search(clause):
            continue
        if _TARGET_ENGINE_CONTEXT.search(clause):
            continue
        if _OUR_MODEL_CONTEXT.search(clause):
            out.append("供应商穿透(自述底层模型) → {0}".format(clause[:70]))
    return out


# ── 写死动态功能价 · v2(2026-08-21 微返修单 B-⑤)──────────────────────────
#
# 🔴 **v1 为什么恒绿** —— 分母没漏,漏的是规则。
#
#    v1 要求子句里同时出现一个 `_FEATURE_ACTION` 手写功能动作词。两处塌方:
#
#    ① **词表漏项**:语料真正的写法是表项键 —— `GEO 文章生成` / `文章补发·重写`
#       / `品牌深度行业解析` / `客户资料 AI 补齐` / `品牌信息 AI 填充` / `AI填写`。
#       词表里有 `生成文章` 没有 `文章生成`、有 `改写` 没有 `重写`、有 `分析`
#       没有 `解析`、有 `补全` 没有 `补齐`,`填充`/`填写` 一个都没有。
#    ② **子句切分把主语切走**:`扣 650 算力` / `每题加 100 算力` / `它单独扣 40 算力`
#       自成一个子句,功能名留在**上一个**子句里 —— 就算词表全对也命中不了。
#
#    这正是「手写分母漏掉的那一项不会让任何判据变红」。所以 v2 **不再打词表**,
#    改成打形态:子句里出现一个**字面价格数字**就是在背价格,除非落进豁免语境。
#    `_FEATURE_ACTION` 随之退役(全仓 census:只有本函数用过它)。
#
# 🔴 v1 还漏了三种**价格数字不带「算力」二字**的形态,它们连第一道
#    `_PRICE_NUMBER` 都过不去,是真正的「390/篇」类恒绿:
#      · `扣 650 分`(旧单位「分」)
#      · `生成 N 篇按 N×390 扣`(乘法算价)
#      · `诊断 650 / 选题 80 / 写文 390 / 改写 260 …`(功能名 + 裸数字枚举)

#: 形态①「N 算力」—— 最常见的一种,v1 就有。
_PRICE_NUMBER = re.compile(r"\d[\d,]*\s*算力")
#: 形态②「扣/加 N 分」—— 旧单位。`分钟/分析/分类` 不是钱。
_PRICE_OLD_UNIT = re.compile(r"(扣|加|收|消耗|花|付|另加|需)\s*\d[\d,]*\s*分(?![钟析类之数])")
#: 形态③「N×390 扣」—— 把单价写进算式。
_PRICE_ARITH = re.compile(r"[×✕xX*]\s*\d[\d,]*\s*(扣|算力)")
#: 形态④「功能名 裸数字 / 功能名 裸数字 / …」—— 截图指引里的价目枚举。
#:   要求**至少两组**,单独一个「XX 3 个」不算。
_PRICE_BARE_ENUM = re.compile(r"(?:[^\s/，,：:（）()]{1,12}\s+\d[\d,]*\s*/\s*){2,}")

_PRICE_SHAPES: tuple[tuple[str, "re.Pattern[str]"], ...] = (
    ("算力数", _PRICE_NUMBER),
    ("旧单位分", _PRICE_OLD_UNIT),
    ("乘法算价", _PRICE_ARITH),
    ("裸数字枚举", _PRICE_BARE_ENUM),
)

#: 反向豁免① 讲**单位含义 / 余额 / 充值入口**,不是在背价格。
#:   🔴 v1 这条里还有 `示例|例如|比如` —— 实测它豁免掉的是 `writing.md` 的
#:      「比如选 5 篇扣 1950 算力」这种**真价格**(举例说明也是在背价格),
#:      不是占位符。占位符写的是 `N 算力`/`M 算力`,本来就不带数字、压根命中不了。
#:      所以这三个词退出豁免。
_PRICE_EXEMPT = re.compile(r"(余额|剩余|不足|够不够|单位|是什么|怎么充|充值)")

#: 反向豁免② **入账方向**(赠送/到账/福利包)不在「功能消耗目录」这条轴上。
#:   `wallet-recharge.md` 的体验包(¥0 / 免费 / 3888 算力)是充值赠送额,
#:   归**现金价目表**那条轴 —— 本规则不认领(交付单里列为「未认领轴」,不是没看见)。
_PRICE_GRANT = re.compile(r"(到账|赠送|福利|体验包|注册送|首充送)")

#: 反向豁免③ **阈值/区间**(≥N 算力弹二次确认、N 算力起)是行为门槛,不是目录价。
#:   🔴 v1 把 `起`/`至少`/`上限` 当**裸词**放进豁免表 —— `起` 在「发**起**前确认页」
#:      里恒命中,于是任何带「发起」的子句里的价格被静默放行。窄修不了,
#:      只能把阈值词**锚到那个数字上**:是它在限定这个数,才算阈值。
#:   🔴 中间那段**不许出现计费动词**:`至少扣 100 算力` 是在说一个**底价**,
#:      不是阈值 —— 第一版写成 `[^。;；\n]{0,8}?` 时它被当阈值放过了。
_PRICE_THRESHOLD = re.compile(
    r"(≥|>=|≤|<=|大于|不低于|不高于|超过|满|上限|封顶|最多|最少|至少)"
    r"[^。;；\n扣加收付花费耗]{0,8}?\d[\d,]*\s*算力"
    r"|\d[\d,]*\s*算力\s*(起|以上|以内|封顶|上限)"
)


def hardcoded_feature_price_violations(text: str) -> list[str]:
    """把**动态**功能目录价写死进静态知识。

    08_billing:功能消耗目录(`feature_pricing`)是后台可调的,文档不背数字。
    背了就会在调价后继续对用户讲旧价 —— 而且没有任何人会收到通知
    (这正是 35 班之前烂掉的那个机制)。

    命中信息里带**形态标签**,判据据此点名「是哪一条形态抓到的」;
    只断言「有命中」的正样本会被别的形态顺手判绿/判红,等于没在守。
    """
    out: list[str] = []
    for clause in clauses(text):
        if _PRICE_EXEMPT.search(clause):
            continue
        if _PRICE_GRANT.search(clause):
            continue
        if _PRICE_THRESHOLD.search(clause):
            continue
        for shape, pattern in _PRICE_SHAPES:
            hit = pattern.search(clause)
            if hit:
                out.append("写死动态功能价·{0}({1}) → {2}".format(
                    shape, hit.group(0).strip(), clause[:70]))
                break
    return out


#: 文档里出现的站内路由
_INTERNAL_ROUTE = re.compile(r"(?<![\w/])/[a-z][a-z0-9\-]*(?:/[a-z0-9\-:]+)*")
#: 这些前缀不是前端路由,别拿去跟 App.tsx 比
_NON_APP_ROUTE_PREFIXES = ("/api/", "/static/", "/assets/", "/docs/", "/help/docs/")

#: 🔴 **markdown 图片/链接目标不是路由。**
#:    第一版规则在现役语料上判红了 `api/help_docs_content.json` 里的
#:    `![个人设置页](/help-screenshots/%E4%B8%AA...png)` —— 那是**图片资源路径**,
#:    不是前端路由。锁把资源当路由判红,会逼人删掉正文里的截图引用。
#:    (顺带查实:`/help-screenshots/` 全仓无目录、无静态挂载、无 nginx 规则,
#:     ⇒ 那些图确实是**坏图链**——真缺陷,但属另一类,记在交付单里,不由本规则背。)
_MARKDOWN_TARGET = re.compile(r"\]\(\s*$")
_ASSET_TAIL = re.compile(r"^[^\s)\"']*\.(png|jpe?g|gif|svg|webp|ico|css|js|pdf)", re.I)


def dead_route_violations(text: str, known_routes=None) -> list[str]:
    """文档里写了**前端根本没有**的路由。

    `known_routes` 不传时从 `services.gap_operation_map.declared_frontend_routes()`
    取真值(机械解析 `frontend/src/App.tsx`),**不手抄第二份路由真值**
    —— 工单 §7.4 明令。
    """
    if known_routes is None:
        from services.gap_operation_map import declared_frontend_routes
        known_routes = declared_frontend_routes()
    known = {str(r).split("?", 1)[0].rstrip("/") or "/" for r in known_routes}
    if not known:
        # 🔴 零分母只能记「没验」,不能记「没有死路由」。
        return []
    body = str(text or "")
    out: list[str] = []
    seen: set[str] = set()
    for match in _INTERNAL_ROUTE.finditer(body):
        raw = match.group(0)
        if raw in seen:
            continue
        if any(raw.startswith(p) for p in _NON_APP_ROUTE_PREFIXES):
            continue
        # markdown 图片/链接目标 `](/xxx...)` → 资源,不是路由
        if _MARKDOWN_TARGET.search(body[max(0, match.start() - 4):match.start()]):
            continue
        # 后面紧跟资源扩展名(含被 URL 编码打断的路径)→ 资源,不是路由
        if _ASSET_TAIL.match(body[match.end():match.end() + 200]):
            continue
        seen.add(raw)
        norm = raw.rstrip("/") or "/"
        if norm in known:
            continue
        # 动态段:`/pricing/12` 这类,拿它的静态前缀再比一次
        head = "/" + norm.strip("/").split("/", 1)[0]
        if head in known:
            continue
        out.append("死路由(App.tsx 里查无此页) → {0}".format(raw))
    return out


#: 「答不上来」的说法
_FAILURE_PHRASE = re.compile(
    r"(没找到|找不到|无法回答|回答不了|不清楚|没有准确答案|暂时不支持|不支持)"
)
#: 「去看文档」的说法 —— 单独出现不算错,**只在没有别的出口时**才算。
_DOC_PUNT = re.compile(r"(帮助中心|帮助文档|使用说明|去翻文档|看文档|联系客服)")
#: 真正的出口:检查/重试/手动/换个说法/转人工。
_REAL_EXIT = re.compile(
    r"(重试|再试|刷新|检查|确认一下|换个说法|告诉我|提供|补充|反馈给|转人工|"
    r"提交.{0,4}(问题|工单)|工作人员|会有人|点下方)"
)


#: 🔴 这条规则**按句子**判,不按子句判。
#:    子句切分器(`clauses`)会把「我没找到准确答案**,**你可以去帮助中心翻文档」
#:    切成两半:一半只有「没找到」、一半只有「帮助中心」,两半各自都不违规 ——
#:    判据当场抓到这个恒绿(第一版就是这么写的)。
#:    「失败」和「只给文档」本来就是**跨子句**的一句话,分母必须是句子。
_SENTENCE = re.compile(r"[^。!！?？\n]+")


def exitless_failure_violations(text: str) -> list[str]:
    """「答不上来 + 只让你去看文档」且**没有任何可执行出口**的文案。

    工单 §5.3:禁止以「去帮助中心」作为唯一答复或唯一动作。
    """
    out: list[str] = []
    for sentence in _SENTENCE.findall(str(text or "")):
        if not _FAILURE_PHRASE.search(sentence):
            continue
        if not _DOC_PUNT.search(sentence):
            continue
        if _REAL_EXIT.search(sentence):
            continue
        out.append("失败文案无出口(只让去看文档) → {0}".format(sentence.strip()[:70]))
    return out


def corpus_lint_violations(text: str, *, known_routes=None) -> list[str]:
    """包 C② 的 lint 面合集。**不进运行时写入门**(见本节顶部说明)。"""
    return (supplier_leak_violations(text)
            + hardcoded_feature_price_violations(text)
            + dead_route_violations(text, known_routes=known_routes)
            + exitless_failure_violations(text))
