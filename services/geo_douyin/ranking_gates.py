"""榜单闸 R1-R8 · 按治理 SSOT §11.1 四级裁决分级

## 🔴 分级不是我拍的,是治理 SSOT §11.1 明列的

**H0(不可绕过)** 只包括:Owner 签发版本化目录明确禁止的肯定式宣传 / 跨租户越权 /
身份不一致 / 资金守恒失败 / malformed payload / 演示副作用 /
**真实结果不存在却试图 settle 或发布**。

**A1(提示 + 局部修复 + 人工继续)** 明列包含:证据不足 / 无源数字 /
**排名依据不完整** / 评分风格篇幅结构 / 企业材料属于自述 / 不确定推断 /
可能需要补资料 / 一般事实核验 / 标题与购买关键词对齐。

→ 工单 v2 把 R3/R5/R6/R8 全写成硬拦,**四条全错**,v3 整节推翻重写。

| 闸 | 级别 | 语义 |
|---|---|---|
| **R1** 实体不在冻结名单 | **A1** | 结构化比对;名单外 → **单卡重做 / 换版式**,不拦整单 |
| R2 家数一致 | A1 | 承诺 N ≠ 实际 N → 提示 + 一键改写,不拒 |
| R3 词条清洗 | 清洗 | 见 `ranking_payload.clean_phrases`,**清洗后继续生成** |
| R4 客户自我排除 | 数据处理 | 见 `topic_distiller.load_competitors` |
| R5 证据时效 | A1·降级 | 超窗 → **切不依赖时效的版式** + 提示补齐,不拒 |
| ~~R6~~ 行业门控 | **已删** | 并入 `ranking_router` 的**路由默认版式**,不是关闭 |
| R7 caveat 语义 | A1 | 榜单型下 caveat 只允许"使用注意事项",不许成为诋毁 |
| R8 举证链完整 | A1·降级 | `source` 五要素不齐 → 降级切版式 + 提示,不拒生图 |

## 🔴 2026-08-06 二次返工 · 本模块两处推翻重写

### ① R1 从「中文正则猜公司名 + H0」改成「结构化比对 + A1」

上一版用 `[一-龥]{2,20}?(?:有限公司|集团|科技|电梯|装饰|…)` 去正文里**猜**公司名。
Codex 实测四句普通话全被判成虚构公司、`blocking=True`:

    「选择时要看电梯」「对比装饰」「这类科技」「智能电梯」

猜名这条路判死 —— 它的失败方向是**误报**,而误报的终点是「判据恒红 → 人麻木」。

改法:比对**结构化实体槽**(卡片的 `entity` 字段,由代码按职责表逐张填,
不是从自由文本里抠出来的)与冻结名单的 `entity_id` / `display_name` 集合。
集合成员判定是确定性的,没有猜的余地。

### ② 「禁虚构」的真正防线在源头,检测层只是兜底

候选**只能**来自 `ranking_source.fetch_ranking_candidates`(读库),
写作侧 prompt 又把白名单逐字冻死。检测层再拦一次是兜底,
所以它按 A1 走:名单外 → 这一张自动重做 / 换个不点名的版式,
**不拦整单**。这同时对齐治理 SSOT D8⑤(禁虚构 = prompt 指导非阻断项)
与 Owner 顶层铁律(能绕过就绕过 —— 自动重做这张卡就是绕过)。

### ③ `blocking` 字段整个删除

它写进 `generation_meta.ranking_gates` 后**全仓没有任何代码消费**:
既产生"这条被拦了"的错误元数据,又不真正拦任何东西。
🔴 修法不是"把 blocking 执行起来" —— 那是往错误方向接线。
闸的输出和函数一样,**要有消费方**,否则就是死函数的闸版。
现在 findings 的消费方是 `generation_meta.ranking_gates` → 前端 A1 提示区
(局部修复合同:`card_indices` + `actions`)。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Final, Iterable, Optional, Sequence

# ── 四级裁决(治理 SSOT §11.1)──────────────────────────────────────────
LEVEL_H0: Final = "H0"      # 不可绕过
LEVEL_A1: Final = "A1"      # 提示 + 局部修复 + 人工继续
LEVEL_CLEAN: Final = "clean"  # 清洗,不产生裁决


#: 序列化后**允许**出现在 `generation_meta.ranking_gates` 里的键。
#: 🔴 这不是文档,是判据:锁按这份清单逐键核 `to_dict()`,并要求每一个键
#:    都能在前端 grep 出真渲染点。多一个没人读的键就红。
SERIALIZED_KEYS: Final = ("gate", "message", "card_indices")


@dataclass
class GateFinding:
    """一条 A1 **局部修复合同**。

    ## 🔴 2026-08-07:序列化面砍到只剩三个字段

    这是「闸输出必须有消费方」家族的**第四次**。前三次是 `blocking`(删)、
    `degrade_to`(见下)、`actions`(删)。这一次的判据换了写法:
    **不再逐个 case-by-case 判断某字段该不该留,而是给序列化面一份白名单
    (`SERIALIZED_KEYS`),并要求名单上每个键都能在前端 grep 出真渲染点。**

    被删的三个,各有各的问题:

    | 字段 | 为什么删 |
    |---|---|
    | `actions` | 五条 finding 的出口(redraw_card/switch_form/add_entity/keep)序列化后**零渲染方**。接线的话得先定"R1 触发的重做免不免费"——既有单张重抽链 2026-08-03 起收 100 算力,而 action 上写着 `recharge: False`,接上去这个字段当场变成假声明。资金语义不该在一个补丁里拍。 |
    | `hits` | 没有消费方,而且它承载的信息**本来就该在 `message` 里**(读的人要的是人话不是数组)。已把 R7/R8 的具体命中折进 message。 |
    | `degrade_to` | 不只是没人读 —— **它配的文案在撒谎**。闸跑在生成之后,form 由 `route_template` 定,`degrade_to` 从来没有人执行;而 R5/R8 的 message 写着「**已改用**…版式」。什么都没切。文案已改成真话。 |

    `level` / `reason` 留在 dataclass(锁在读,是 §11.1 分类与两种 R1 失败模式的
    判别码),但**不序列化**:五条 finding 的 `level` 恒为 A1,序列化一个常量
    等于零信息 —— 和 `redraw_free: True`、`blocking: False` 同形。

    用户拿到的可执行出口在**既有路径**上:「看看」→ 详情页 → 单张重抽。
    `card_indices` 告诉他重做哪几张,这才是这条 finding 真正多给的东西。
    """
    gate: str
    level: str
    reason: str
    message: str
    #: 局部修复的**作用域**:这些下标的卡需要重做(1-based,与 `cards[].idx` 同源)。
    #: 空 = 整条内容级提示,没有具体到某一张。
    card_indices: tuple[int, ...] = ()

    def to_dict(self) -> dict:
        return {
            "gate": self.gate,
            "message": self.message,
            "card_indices": list(self.card_indices),
        }


# ===========================================================================
# R1 · 实体必须在冻结名单里(结构化比对 · A1 局部修复)
# ===========================================================================

def frozen_name_keys(allowed: Iterable[str], *, client_brand: str = "") -> set[str]:
    """把冻结名单(`entity_id` / `display_name` / 客户名)压成**合并键集合**。

    用 `safe_merge_key` 而不是原名:`深圳市恒通电梯有限公司` 与 `恒通电梯`
    是同一主体的两种写法,卡面上写哪一种都不该被判成名单外。
    """
    from services.geo_douyin.ranking_payload import safe_merge_key

    keys = {safe_merge_key(a) for a in (allowed or []) if str(a or "").strip()}
    if str(client_brand or "").strip():
        keys.add(safe_merge_key(client_brand))
    keys.discard("")
    return keys


def _display_norms(allowed: Iterable[str], *, client_brand: str = "") -> set[str]:
    from services.geo_douyin.ranking_payload import _norm

    out = {_norm(str(a)) for a in (allowed or []) if str(a or "").strip()}
    if str(client_brand or "").strip():
        out.add(_norm(str(client_brand)))
    out.discard("")
    return out


#: 卡片上"这张讲的是哪一家"的**声明字段**。榜单形态下由写作侧按白名单逐字填,
#: 不讲具体某一家的结构卡(比较口径 / 成本结构 / 核验细节…)留空。
ENTITY_REF_FIELD: Final = "entity_ref"

#: 前缀容错的最短长度。**2 是有依据的**:「通力」「日立」这类 2 字品牌真实存在,
#: 设成 4 会把它们误判成名单外;设成 1 则单字就能蹭过。
_MIN_PREFIX_CHARS: Final = 2


def _slot_ref(slot: Any) -> str:
    if isinstance(slot, dict):
        return str(slot.get(ENTITY_REF_FIELD) or "").strip()
    return str(slot or "").strip()


def _slot_display(slot: Any) -> str:
    if isinstance(slot, dict):
        return str(slot.get("entity") or slot.get("headline") or "").strip()
    return ""


def ref_in_frozen_list(name: str, keys: set[str], display_norms: set[str]) -> bool:
    """这个**声明**是不是名单里的。只做集合判定,不猜"像不像公司名"。

    两条命中路径:
      ① 合并键相等 —— 覆盖 `深圳市恒通电梯有限公司` ↔ `恒通电梯` 这类同主体异写;
      ② 归一化后是某个名单名的**前缀** —— 覆盖 `clip_text` 的截断
         (`clip_text` 是纯前缀截断、不加省略号,所以前缀判定是**精确**的,
          不是模糊匹配)。
    """
    from services.geo_douyin.ranking_payload import _norm, safe_merge_key

    raw = str(name or "").strip()
    if not raw:
        return True          # 空 = 没声明,不由本闸管
    if safe_merge_key(raw) in keys:
        return True
    n = _norm(raw)
    if not n:
        return True
    # 🔴 前缀容错要有下限:没有它,`ref="深"` 也能蹭过
    #    (「深」是「深圳市恒通电梯有限公司」的前缀)。
    #    下限取 2 而不是更大 —— 「通力」是 2 字的真实品牌,设 4 会误杀。
    if len(n) < _MIN_PREFIX_CHARS:
        return False
    return any(d.startswith(n) for d in display_norms)


def _display_consistent(display: str, ref: str) -> bool:
    """卡面印出来的名字与它声明的那一家**是不是同一个**。

    `entity` 被 `clip_text(…, 12)` 截过,所以判据是"归一化后互为前缀",
    不是逐字相等 —— 前缀判定对纯前缀截断是精确的。
    """
    from services.geo_douyin.ranking_payload import _norm, safe_merge_key

    d, r = _norm(display), _norm(ref)
    if not d or not r:
        return True
    if d.startswith(r) or r.startswith(d):
        return True
    # 同主体异写(带/不带行政前缀、法定后缀)也算一致
    return bool(safe_merge_key(display)) and safe_merge_key(display) == safe_merge_key(ref)


def r1_entities_in_frozen_list(entity_slots: Sequence[Any],
                               allowed: Iterable[str], *,
                               client_brand: str = "") -> Optional[GateFinding]:
    """**声明式**实体槽 vs 冻结名单 → A1 单卡修复合同。

    判的是每张卡的 `entity_ref` 声明("这张讲的是哪一家"),不是自由文本 ——
    这是与上一版的根本差别,见模块 docstring ①。

    🔴 为什么必须是**声明**而不是"每张卡的 entity 都得是公司名":
       `series_plan` 的职责阶梯是杂志七页(需求定义 / 比较口径 / 核验细节 /
       成本结构 / 履约验证),**多数内容卡根本不讲某一家**。
       要求每张卡的 `entity` 都落在名单里,会把「比较口径」这种结构卡全判成
       名单外 —— 那正是上一版误报的同一个形状,只是换了个地方犯。
       所以:**没声明的卡不判**,声明了的卡才判。

    两类命中(都不涉及猜):
      ① 声明了一个名单里没有的名字 → `entity_not_in_frozen_list`;
      ② 声明了 A、卡面却印了 B → `entity_display_mismatch`(冒名顶替)。

    ⚠️ **残留**:模型完全不声明、却在 `entity` 里写一个编的公司名 —— 本闸不判。
       这是刻意的:判它就得回到"猜哪个字符串像公司名",而那条路已经被实测判死。
       禁虚构的主防线在**源头**(候选只能来自 `ranking_source` 读库 +
       写作侧白名单逐字冻结),本闸只是兜底。

    返回的 finding 带 `card_indices`(1-based),消费方据此只重做这几张。
    """
    keys = frozen_name_keys(allowed, client_brand=client_brand)
    norms = _display_norms(allowed, client_brand=client_brand)
    if not keys and not norms:
        # 名单为空 = 上游没给冻结件,此时任何判定都没有依据。**不报**。
        return None

    bad_names: list[str] = []
    bad_idx: list[int] = []
    mismatched = False
    for i, slot in enumerate(entity_slots or [], start=1):
        ref = _slot_ref(slot)
        if not ref:
            continue          # 结构卡:没声明讲哪一家 → 不由本闸管
        if not ref_in_frozen_list(ref, keys, norms):
            if ref not in bad_names:
                bad_names.append(ref)
            bad_idx.append(i)
            continue
        display = _slot_display(slot)
        if display and not _display_consistent(display, ref):
            mismatched = True
            tag = f"{display}(声明的是 {ref})"
            if tag not in bad_names:
                bad_names.append(tag)
            bad_idx.append(i)
    if not bad_idx:
        return None

    idx = tuple(sorted(set(bad_idx)))
    return GateFinding(
        gate="R1", level=LEVEL_A1,
        reason="entity_display_mismatch" if mismatched else "entity_not_in_frozen_list",
        card_indices=idx,
        # 出口指向**既有路径**(「看看」→ 详情页 → 单张重抽),不新造一条。
        message=(f"有 {len(idx)} 张卡讲的公司不在这次的名单里("
                 + "、".join(bad_names[:3]) +
                 ")。名单外的名字没有来源可查 —— 点「看看」进详情页把这几张单独重做一下。"),
    )


# ===========================================================================
# R2 · 家数一致(A1)
# ===========================================================================

_COUNT_RE: Final = re.compile(r"([0-9]{1,2})\s*(?:家|款|个)")


def promised_entity_count(text: str) -> Optional[int]:
    m = _COUNT_RE.search(str(text or ""))
    return int(m.group(1)) if m else None


def r2_count_matches(caption: str, actual: int) -> Optional[GateFinding]:
    """标题承诺 N 家、正文/图里实际 M 家 → 提示 + 一键改写,**不拒**。"""
    promised = promised_entity_count(caption)
    if promised is None or promised == int(actual or 0):
        return None
    return GateFinding(
        gate="R2", level=LEVEL_A1, reason="entity_count_mismatch",
        message=(f"文案里说「{promised}家」,实际做了 {actual} 家 —— "
                 "数字对不上读者会当场发现,把文案里那个数改一下。"),
    )


# ===========================================================================
# R5 · 证据时效(A1 · 降级切版式)
# ===========================================================================

def r5_evidence_freshness(observed_days_ago: Optional[int], *,
                          window_days: int) -> Optional[GateFinding]:
    """依据过旧 → 提示,不拒绝(永不中断)。

    🔴 2026-08-07 订正文案:原文写的是「**已改用**不依赖时效的场景推荐版式」——
       **这句是假的**。闸跑在生成之后,form 由 `route_template` 定,
       本闸从来没有切过任何版式(`degrade_to` 也从来没有人执行,已删)。
       告诉用户"我们已经改了"而实际什么都没改,比不提示更坏。
    """
    if observed_days_ago is None or observed_days_ago <= int(window_days):
        return None
    return GateFinding(
        gate="R5", level=LEVEL_A1, reason="evidence_stale",
        message=(f"这批名次是 {observed_days_ago} 天前的,超过 {window_days} 天了 —— "
                 "名次可能已经变了。想要新的,先跑一次监测再生成一次。"),
    )


# ===========================================================================
# R7 · caveat 不得成为诋毁(A1)
# ===========================================================================

#: 榜单型下 caveat 只允许「使用注意事项 / 适用边界」语义。
#: `card_templates.py:133-134` 本来就给了合法出口(「编不出真实短板就写使用注意事项」),
#: R7 是把那个出口在榜单型下变成**唯一**出口。
_DISPARAGE_RE: Final = re.compile(
    r"(差|烂|坑|骗|忽悠|不行|垃圾|劣质|偷工减料|没实力|不专业|翻车|跑路|失信)"
)


def r7_caveat_not_disparaging(caveats: Sequence[str], *,
                              is_ranking_form: bool) -> Optional[GateFinding]:
    if not is_ranking_form:
        return None
    hits = [c for c in (caveats or []) if _DISPARAGE_RE.search(str(c or ""))]
    if not hits:
        return None
    return GateFinding(
        gate="R7", level=LEVEL_A1, reason="caveat_disparaging",
        # 具体是哪一句折进 message —— 原来它在 `hits` 里,而 `hits` 没有渲染方,
        # 于是用户看到"有负面评价"却不知道是哪一句。
        message=("榜单里给被点名的公司写了负面评价(比如「"
                 + str(hits[0])[:20] + "」)—— 这属于商业诋毁。"
                 "改成「适用边界/使用注意事项」就行,不用删。"),
    )


# ===========================================================================
# R8 · 举证链完整(A1 · 降级)
# ===========================================================================

PROVENANCE_KEYS: Final = ("engine", "recommendation_rank", "extractor_version",
                          "llm_model", "observed_at")


def r8_provenance_complete(items: Sequence[Any]) -> Optional[GateFinding]:
    """`source` 五要素不齐 → 提示,不拒生图。

    🔴 2026-08-07 订正文案:原文写的是「**已改用**不给名次的版式」——
       同样是假的,本闸切不了版式。真实发生的事是
       `rank_statement` **逐条**降级(缺四元组就说「被 AI 搜索提到过」),
       所以正确的说法是"这几家没写名次",不是"我们换了版式"。
    """
    missing: list[str] = []
    for it in (items or []):
        src = (it.get("source") if isinstance(it, dict) else getattr(it, "source", None)) or {}
        lack = [k for k in PROVENANCE_KEYS if not src.get(k)]
        if lack:
            name = (it.get("display_name") if isinstance(it, dict)
                    else getattr(it, "display_name", "")) or "?"
            missing.append(str(name))
    if not missing:
        return None
    return GateFinding(
        gate="R8", level=LEVEL_A1, reason="provenance_incomplete",
        message=(f"有 {len(missing)} 家(" + "、".join(missing[:3]) +
                 ")查不到「哪个引擎、什么时候、第几名」,这几家就没写名次。"
                 "补一批候选之后可以再生成一次。"),
    )


# ===========================================================================
# R9 · 画像只许当选材背景,不许整段搬上卡面(2026-08-08 A 层)
# ===========================================================================

#: 卡面与画像的最长逐字重合上限(汉字计)。超过就是在搬运,不是在借鉴。
#: 取 16:一句完整的宣称(「拥有五大智能化生产基地」)约 11-14 字,
#: 16 能放过"全屋定制""智能制造"这类**行业通用词组**的自然重合。
VERBATIM_MAX_CHARS: Final = 16


def _slot_text(slot: Any) -> str:
    """槽里所有会落到卡面上的字。

    🔴 故意**不**写死字段名(`headline`/`body`/`points`…):卡的字段随版式变,
       写死就会漏掉新字段 —— 而漏掉的那个字段正好是搬运发生的地方时,
       这条闸就成了摆设。拼全部字符串,宁可多judge也不漏。
    """
    if not isinstance(slot, dict):
        return str(slot or "")
    buf: list[str] = []

    def _walk(v: Any) -> None:
        if isinstance(v, str):
            buf.append(v)
        elif isinstance(v, (list, tuple)):
            for x in v:
                _walk(x)
        elif isinstance(v, dict):
            for x in v.values():
                _walk(x)

    _walk(slot)
    return "\n".join(buf)


def _longest_common_span(a: str, b: str) -> int:
    """最长公共子串长度(滚动数组,两边都是短文本)。"""
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    best = 0
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        ai = a[i - 1]
        for j in range(1, len(b) + 1):
            if ai == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best = cur[j]
        prev = cur
    return best


def r9_no_verbatim_profile(entity_slots: Sequence[Any],
                           profiles: Optional[dict] = None) -> Optional[GateFinding]:
    """竞品画像被整段抄上卡面 → A1 单卡重做。

    🔴 这条闸是 A 层安全论证的**落点**。画像是我们自己联网后 LLM 综述的,
       名字有 `verify_source` 链接核验、能力描述没有;进 prompt 当背景没问题,
       原句上卡就等于替第三方公司做没有逐条依据的宣称(数字/专利/资质/市占率)。
       prompt 里那句"不许照抄"只是**要求**,不是判据 —— 没有本闸就无从知道
       模型到底照没照抄。本仓这条教训已经吃过多次:判据要打在接线上。
    """
    # 🔴 B 层(2026-08-08):**只判不可引用的那几家**。
    #    可引用的(逐条 name_verified 且主体成立)按合同就是允许把里面的事实写进卡面,
    #    再拿"逐字重合"判它就成了自相矛盾的闸 —— 那才是合规表演。
    #    可引用那几家的约束改由 prompt 承担(用自己的话重写),与广告法同一档:
    #    prompt 主防 + 提示级,不硬拦。
    pmap = {str(k): str((v or {}).get("text") or "")
            for k, v in (profiles or {}).items()
            if isinstance(v, dict) and not v.get("citable")
            and str((v or {}).get("text") or "").strip()}
    if not pmap:
        return None
    from services.geo_douyin.ranking_payload import safe_merge_key

    hits: list[str] = []
    idx: list[int] = []
    for i, slot in enumerate(entity_slots or [], start=1):
        ref = _slot_ref(slot)
        bg = pmap.get(safe_merge_key(ref)) if ref else ""
        if not bg:
            continue
        text = _slot_text(slot)
        if _longest_common_span(text, bg) >= VERBATIM_MAX_CHARS:
            idx.append(i)
            if ref not in hits:
                hits.append(ref)
    if not idx:
        return None
    return GateFinding(
        gate="R9", level=LEVEL_A1, reason="profile_copied_verbatim",
        card_indices=tuple(idx),
        message=("有 " + str(len(idx)) + " 张卡(" + "、".join(hits[:3]) +
                 ")把同行资料原样抄了上去 —— 那份资料是检索综述,里面的数字和"
                 "资质没有逐条核验,替别人家这么说不合适。"
                 "点「看看」进详情页把这几张单独重做一下,换成我们自己的判断。"),
    )


# ===========================================================================
# 汇总
# ===========================================================================

def run_gates(*, caption: str = "", allowed_names: Iterable[str] = (),
              client_brand: str = "", actual_count: int = 0,
              caveats: Sequence[str] = (), is_ranking_form: bool = False,
              items: Sequence[Any] = (), observed_days_ago: Optional[int] = None,
              window_days: int = 90,
              entity_slots: Sequence[Any] = (),
              entity_profiles: Optional[dict] = None) -> list[GateFinding]:
    """跑全部闸。**永不抛异常**;返回的全部是 A1 局部修复合同,**没有一条阻断**。

    `entity_slots` = 结构化实体槽(内容卡列表或名字列表)。R1 只看它,
    **不再从 `caption` 里猜公司名**。
    """
    out: list[GateFinding] = []
    for f in (
        r1_entities_in_frozen_list(entity_slots, allowed_names,
                                   client_brand=client_brand),
        r2_count_matches(caption, actual_count),
        r5_evidence_freshness(observed_days_ago, window_days=window_days),
        r7_caveat_not_disparaging(caveats, is_ranking_form=is_ranking_form),
        r8_provenance_complete(items),
        r9_no_verbatim_profile(entity_slots, entity_profiles),
    ):
        if f is not None:
            out.append(f)
    return out


# 🔴 `repair_scope()` 2026-08-07 **删除**。
#
#    我上一轮把它写进交付说明,称它是 `card_indices` 的"真消费入口" ——
#    **当时没有 grep 过它的调用方**。全仓唯一的调用者是它自己的锁。
#    判据写了、话说了,唯独没跑那一条 grep。
#
#    `card_indices` 真正的消费方是前端:作品卡上的 A1 提示会渲染
#    「(第 N 张)」,用户据此进详情页单张重抽。那是既有付费路径,
#    不需要后端再包一层没人调的聚合函数。
