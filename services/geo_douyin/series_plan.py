"""杂志级连续组图 · 七页职责框架与 N 张降档表(规范 §3 / §8.1)

规范全文:`GEO图文排行榜通用母版_2026-08-02/12-3比4杂志级连续组图-v2/00-杂志级连续组图生成规范.md`

## 这个模块回答一个问题:客户选了 N 张,这 N 张分别干什么

七页叙事是**完整形态**,不是默认。产品默认 **4 张**(套餐含 4,每加一张按价目表加价)。
所以必须有降档表 —— 否则默认套餐根本用不了这套规范。

🔴 **张数控制的三层**(规范 §8.1a,Owner 定死):
  1. **代码层** —— `card_count` 是结构化参数(资金语义),**不交给任何模型**;
  2. **规划 prompt 层** —— 把本模块产出的降档表逐字嵌进业务 AI 的 prompt,
     要求"把叙事规划成恰好 N 张";
  3. **校验层** —— 代码硬校验 `len(cards) == N`,多裁 / 少重试 / 仍不齐**明确失败**。
     生图模型只收"第 i/N 张 + 给定文字",对张数零话语权。

🔴 收缩时叙事**必须仍闭环**:封面提出的问题,收口必须回答。
   所以任何 N >= 2 都同时保留 cover 与 closing,不许砍成"有头无尾"。
"""
from __future__ import annotations

from typing import Dict, List, Optional

from services.geo_douyin.config import CARD_COUNT_MAX, CARD_COUNT_MIN

# 角色 → (用户可见职责名, 版式要求)。版式取自规范 §3 的「推荐版式」列。
# 🔴 `layout_role` 是给生图提示词用的 —— 规范 §5 的骨架引用了 {layout_role},
#    而 §4 的合同里原本**没有这个字段**(§8.4 指出的自相矛盾)。补在这里,
#    合同由代码生成,模型不需要也不允许自己想版式。
ROLE_SPEC: Dict[str, Dict[str, str]] = {
    "cover":    {"label": "封面问题", "layout_role": "全幅场景照 + 大标题色块"},
    # 🔴 榜单形态专用职责(2026-08-07 P1-4)。杂志七页阶梯是**通用叙事**,
    #    里面没有"这一张讲哪一家"这个概念 —— 而母版规范写的是
    #    「3-5 ranked rows; each row = rank, company, best-fit scenario, one reason」。
    #    缺这一层的后果不是排版难看,是**没人知道哪张卡该讲某一家** →
    #    只能反过来去正文里猜公司名,而猜名那条路已经被实测判死。
    "entity":   {"label": "上榜企业", "layout_role": "企业卡:名次 + 名称 + 适配场景 + 一条依据"},
    "demand":   {"label": "需求定义", "layout_role": "决策漏斗 / 路径图"},
    "compare":  {"label": "比较口径", "layout_role": "对比矩阵 / 三对象并列"},
    "verify":   {"label": "核验细节", "layout_role": "细节照片拼贴 + 局部标注"},
    "cost":     {"label": "成本结构", "layout_role": "费用拆分清单 + 实物对象"},
    "delivery": {"label": "履约验证", "layout_role": "现场大图 + 三个检查锚点"},
    "closing":  {"label": "决策收束", "layout_role": "三步行动路径 + 完成场景"},
}

# 规范 §8.1 的降档表。N=4 是默认套餐。
# 🔴 N=1/2/3 规范没写 —— 这三档是本次**补的**,规则是"先保闭环再加深度":
#    2 张起就必须 cover + closing 同时在(封面提问、收口必答)。
#    补的部分标注出来,免得下一个人以为它也是规范原文。
_LADDER: Dict[int, List[str]] = {
    1: ["cover"],                                            # 补:单图=问题与结论合一
    2: ["cover", "closing"],                                 # 补
    3: ["cover", "compare", "closing"],                      # 补
    4: ["cover", "compare", "cost", "closing"],              # 规范:默认
    5: ["cover", "compare", "verify", "cost", "closing"],    # 规范:+ 核验细节
    6: ["cover", "demand", "compare", "verify", "cost", "closing"],          # 规范:+ 需求定义
    7: ["cover", "demand", "compare", "verify", "cost", "delivery", "closing"],  # 规范:完整七页
}


#: 榜单形态下,封面与收尾各占一张 —— 剩下的才是企业卡。
RANKING_FIXED_CARDS: int = 2


def entity_slots_for(card_count: int) -> int:
    """这么多张图,最多能装下几家企业。**唯一实现** —— 三处算三遍必漂。"""
    n = max(CARD_COUNT_MIN, min(int(card_count or 4), CARD_COUNT_MAX))
    return max(0, n - RANKING_FIXED_CARDS)


def plan_ranking_roles(card_count: int, entity_names: List[str]) -> List[dict]:
    """榜单形态的职责表:封面 + 每家一张企业卡 + 收尾。

    🔴 **公司名由代码写进职责表**,不是模型选的。这是 P1-4 的落点:
       「这一张讲哪一家」变成**构造出来的事实**,模型只负责写这一张的文案。
       于是"省略 entity_ref 就绕过检查"这条路结构上不存在 ——
       `entity_ref` 根本不由模型填。
    """
    names = [str(x).strip() for x in (entity_names or []) if str(x or "").strip()]
    k = min(len(names), entity_slots_for(card_count))
    roles = ["cover"] + ["entity"] * k + ["closing"]
    out: List[dict] = []
    ei = 0
    for idx, role in enumerate(roles, 1):
        spec = ROLE_SPEC[role]
        item = {"index": idx, "role": role, "label": spec["label"],
                "layout_role": spec["layout_role"], "depth_index": 1}
        if role == "entity":
            item["entity_index"] = ei          # 0-based,对应冻结名单下标
            item["entity_name"] = names[ei]
            item["label"] = f"第 {ei + 1} 家 · {names[ei]}"
            ei += 1
        out.append(item)
    return out


#: 画像进 prompt 的截断长度。够模型判断"这家主打什么",又不至于让它整段搬运。
PROFILE_CONTEXT_CHARS: int = 120


def _scrub_ad_law(text: str) -> str:
    """把画像里的绝对化用语**在进 prompt 之前**剥掉。

    🔴 生产实测:394 条画像里 27 条含「第一/最大/最高/领先/龙头/首创/国家级」。
       与其把这些词递给模型再指望它别写,不如根本不递过去 —— 少一次赌。
    🔴 词表走**签发目录同源**(`services/marketing/guards.legal_pack`),
       本模块不自建第二份:图文链上一包刚因为"三份词表各不相同、漏检率 95%"返过工。
       目录取不到就原样返回 —— 剥不掉不该挡住生成(主防仍在 prompt 约束那一层)。
    """
    t = str(text or "")
    if not t:
        return ""
    try:
        from services.marketing.guards import legal_pack
        terms = [str(w) for w in (legal_pack().get("ad_law") or ()) if str(w or "")]
    except Exception:   # noqa: BLE001
        return t
    for w in terms:
        if w and w in t:
            t = t.replace(w, "")
    return t.strip()


def ranking_roles_prompt_block(card_count: int, entity_names: List[str],
                               statements: List[str],
                               profiles: Optional[Dict[str, str]] = None) -> str:
    """榜单职责表进 prompt。**每一张写死讲哪一家、名次那句话逐字给出。**

    `profiles` = `safe_merge_key(名字) → 画像`(2026-08-08 A 层)。
    🔴 它是**选材背景**,不是可引用事实:名字有 verify_source 链接核验,
       能力描述没有,是我们自己联网后 LLM 综述的。所以这里
       ①截断、②明写"不许逐字抄"、③配 R9 闸做**真检查**(光写在 prompt 里
       只是愿望,不是判据 —— 本仓这条教训已经吃过多次)。
    """
    from services.geo_douyin.ranking_payload import safe_merge_key

    plan = plan_ranking_roles(card_count, entity_names)
    pmap = {str(k): v for k, v in (profiles or {}).items()
            if isinstance(v, dict) and str(v.get("text") or "").strip()}
    n = len(plan)
    lines = [f"这一组**恰好 {n} 张**,每张讲什么已经定死,不许多也不许少:"]
    used_profile = False
    used_citable = False
    for item in plan:
        if item["role"] == "cover":
            lines.append(f"  第 {item['index']}/{n} 张 · 封面问题 · 版式:{item['layout_role']}")
        elif item["role"] == "closing":
            lines.append(f"  第 {item['index']}/{n} 张 · 决策收束 · 版式:{item['layout_role']}")
        else:
            i = item["entity_index"]
            said = statements[i] if i < len(statements) else ""
            lines.append(
                f"  第 {item['index']}/{n} 张 · **只讲「{item['entity_name']}」这一家** · "
                f"名次表述逐字照抄:「{said}」 · 版式:{item['layout_role']}")
            ent = pmap.get(safe_merge_key(str(item["entity_name"])))
            bg = _scrub_ad_law(str((ent or {}).get("text") or ""))
            if bg:
                used_profile = True
                if (ent or {}).get("citable"):
                    used_citable = True
                    lines.append(f"      公开资料(可以用里面的**事实**,但要用自己的话说):"
                                 f"{bg[:PROFILE_CONTEXT_CHARS]}")
                else:
                    lines.append(f"      背景(只用来定这一张的差异化角度):"
                                 f"{bg[:PROFILE_CONTEXT_CHARS]}")
    lines.append("🔴 企业卡只许讲指定的那一家,**不许换成别家、不许多讲一家**。")
    lines.append("🔴 封面提出的问题,最后一张**必须**回答上 —— 不许有头无尾。")
    if used_profile:
        lines.append(
            "🔴 上面的「背景」是内部选材参考,**一个字都不许照抄进卡面**;"
            "它只用来决定这一张从哪个角度写(各家角度要**互不重复**)。"
            "背景里的数字、专利名、资质、市占率一律**不许**写进卡面 —— "
            "那些没有逐条核验,写上去就是替别人家做没有依据的宣称。")
    if used_citable:
        lines.append(
            "🔴 标「公开资料」的那几家不同:里面的事实**可以用**,但要用自己的话重写,"
            "**不许整段照抄**(照抄会被系统标出来让用户重做这一张)。")
    return "\n".join(lines)


def plan_roles(card_count: int) -> List[dict]:
    """N 张分别是什么角色。返回顺序即卡序,第 1 张恒为 cover。

    8-9 张:规范 §8.1 明确「**不新增职责**,只把核验细节 / 成本结构拆开展开」——
    所以这里是把 verify / cost 复制一份,而不是塞进新角色。
    复制出来的那张带 `depth_index`,让业务 AI 知道"这是同一职责的第 2 张,要更深"。
    """
    n = max(CARD_COUNT_MIN, min(int(card_count or 4), CARD_COUNT_MAX))
    if n <= 7:
        roles = list(_LADDER[n])
    else:
        roles = list(_LADDER[7])
        # 8 → 多一张 verify;9 → 再多一张 cost。插在原职责后面保持叙事顺序。
        extra = ["verify", "cost"][: n - 7]
        for role in extra:
            roles.insert(roles.index(role) + 1, role)

    out: List[dict] = []
    seen: Dict[str, int] = {}
    for idx, role in enumerate(roles, 1):
        seen[role] = seen.get(role, 0) + 1
        spec = ROLE_SPEC[role]
        out.append({
            "index": idx,
            "role": role,
            "label": spec["label"],
            "layout_role": spec["layout_role"],
            "depth_index": seen[role],   # 同职责的第几张(8-9 张时才会 >1)
        })
    return out


def roles_prompt_block(card_count: int) -> str:
    """给业务 AI 的降档表文字。**逐字嵌进规划 prompt**(§8.1a 第 2 层)。"""
    plan = plan_roles(card_count)
    n = len(plan)
    lines = [f"这一组**恰好 {n} 张**,每张的职责已经定死,不许多也不许少:"]
    for item in plan:
        deep = "(同一职责的第 2 张,要比上一张更深入,不要重复)" if item["depth_index"] > 1 else ""
        lines.append(f"  第 {item['index']}/{n} 张 · {item['label']}"
                     f" · 版式:{item['layout_role']}{deep}")
    lines.append("🔴 封面提出的问题,最后一张**必须**回答上 —— 不许有头无尾。")
    lines.append("🔴 每张要承接上一张、推动下一张,禁止各说各话。")
    return "\n".join(lines)


def content_role_plan(card_count: int) -> List[dict]:
    """去掉封面与收尾之后的**内容卡**计划(生成侧按这个逐张填内容)。"""
    return [r for r in plan_roles(card_count) if r["role"] not in ("cover", "closing")]


# 规范 §8.9:样张里的行业内容**全是虚构示例**(深胡桃木场景 / 板材·五金·工期 /
# 深圳全屋定制文案)。可复用的只有结构,行业词一律逐客户从其知识库生成。
# 样张词出现在**非对应行业**客户的产出里 = 串味,验收直接判负。
_SAMPLE_TAINT: Dict[str, List[str]] = {
    # 样张所属行业 → 该样张的专属词
    "home_improvement": ["深胡桃木", "胡桃木", "板材", "五金", "封边", "柜体",
                         "门板", "全屋定制", "整装", "收口"],
    "food": ["坤沙", "碎沙", "翻沙", "基酒", "酱香"],
}


def sample_taint_words(industry_key: str) -> List[str]:
    """这个客户产出里**不该出现**的样张专属词。

    🔴 判定必须**行业条件化**:客户本身就是家装行业时,"板材/五金"是他真正的
       业务词,不是串味。只有跨行业出现才算 —— 一刀切会把合规产出误判成违约。
    """
    industry = str(industry_key or "").strip()
    out: List[str] = []
    for owner_industry, words in _SAMPLE_TAINT.items():
        if industry == owner_industry:
            continue          # 客户就是这个行业 → 这些词本来就该出现
        out.extend(words)
    return out


def detect_sample_taint(text: str, industry_key: str) -> List[str]:
    """产出里有没有混进样张的行业词。返回命中的词(最多 5 个)。"""
    body = str(text or "")
    if not body:
        return []
    hits = [w for w in sample_taint_words(industry_key) if w in body]
    return hits[:5]


def cover_aux_labels(card_count: int) -> List[str]:
    """封面底部的导航词(规范 §8.4 的 `aux_labels`)。

    🔴 为什么必须由代码给:样张封面底部那四个词("需求/参数/工艺/交付")
       **不在冻结文案的任何字段里** —— 按原合同它就是模型自加文字,
       于是验收第 5 条"逐字一致"恒 fail。这里把它变成**代码生成的冻结文案**:
       取后续各张的职责名,天然与这一组的实际内容对齐,而且逐字可验。
    """
    labels = [r["label"] for r in plan_roles(card_count)
              if r["role"] not in ("cover",)]
    # 去重保序(8-9 张会有重复职责),最多 4 个 —— 再多封面底部排不下
    out: List[str] = []
    for label in labels:
        if label not in out:
            out.append(label)
    return out[:4]
