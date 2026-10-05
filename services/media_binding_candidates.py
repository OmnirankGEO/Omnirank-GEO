"""Shadow media inventory binding candidates for GEO placement flywheel.

This module deliberately keeps live placement untouched.  It only prepares
admin-reviewable candidates and reuses the media entity matching guardrails.
"""

from __future__ import annotations

import re
from typing import Any

# 🔴 [平台域口径 2026-08-15] 仓内**同名两版** `normalize_domain` 并存,是上一轮判错的直接成因:
#     services.media_entity_flywheel.normalize_domain   host 级,**不剥子域** c.m.163.com -> c.m.163.com
#     services.citation_domain_weights.normalize_domain 注册域,**剥子域**   c.m.163.com -> 163.com
#                                                       (且已处理 .com.cn 等多段公共后缀)
#   消歧方案 = **两个都显式 `as` 重命名**,本模块内不再出现裸的 `normalize_domain`。
#   为什么不是「统一到一处」:两者语义都对、且都在用 ——
#     · host 级是**实体/库存匹配键**(`domain_exact` 靠它,换成注册域会让 blog.csdn.net 的库存
#       突然匹配上 csdn.net 实体,是另一个量级的行为变更,不在本单范围);
#     · 注册域是**「这个域名代表的是不是一个人人可发的平台」**的判据。
#   合并会丢掉其中一个语义;裸名字才是事故源,所以消除的是裸名字,不是函数。
from services.citation_domain_weights import normalize_domain as registrable_domain
from services.media_entity_flywheel import (
    canonicalize_media_name,
    match_inventory_to_entity,
    normalize_domain as host_domain,
)


# [T6/T6b 2026-07-03] 一键通过 / 自动通过 的共享阈值(与前端 isRecommendedBinding 同源,防口径漂移)。
#   一键通过「建议通过」集 = status=candidate + can_approve + match_confidence>=0.90 + 无风险。
#   T6b 自动通过 = 仅 domain_exact 精确域名 + conf>=0.95 + 无风险 + 可采购(can_approve 已含无风险+可采购)。
#   name_alias(0.86 名字子串匹配)= 错绑高发区,永不自动通过,留一键批次/逐条人工。
RECOMMENDED_BINDING_MIN_CONFIDENCE = 0.90
AUTO_APPROVE_MIN_CONFIDENCE = 0.95
AUTO_APPROVE_MATCH_METHOD = "domain_exact"


def is_recommended_binding(candidate: dict[str, Any]) -> bool:
    """「建议通过」判定 = 一键通过选集标准(与前端 isRecommendedBinding 同源)。"""
    return (
        str(candidate.get("status") or "candidate") == "candidate"
        and bool(candidate.get("can_approve"))
        and float(candidate.get("match_confidence") or 0) >= RECOMMENDED_BINDING_MIN_CONFIDENCE
        and not candidate.get("risk_flags")
    )


def is_auto_approvable(candidate: dict[str, Any]) -> bool:
    """T6b 自动通过判定:仅 domain_exact + conf>=0.95 + 无风险 + can_approve(含可采购)。"""
    return (
        candidate.get("match_method") == AUTO_APPROVE_MATCH_METHOD
        and float(candidate.get("match_confidence") or 0) >= AUTO_APPROVE_MIN_CONFIDENCE
        and not candidate.get("risk_flags")
        and bool(candidate.get("can_approve"))
    )


#: 🔴 [平台域口径 2026-08-15 · Owner 拍板 ①③] 本集合的元素一律是**注册域**(registrable domain),
#: 不是 host。`csdn.net` 一条覆盖 blog./download./wenku.csdn.net 等全部子域。
#:
#: 改口径前的实测(生产,2026-08-15):判定是「host 级 + 精确相等」,于是名单里的**裸域一条都没生效**
#: (zhihu.com 挂 578 条候选命中 0、163.com 573 条命中 0、sohu.com 220 条命中 0),
#: 而同平台子域几乎全逃过(zhuanlan.zhihu.com / c.m.163.com / news.qq.com / blog.csdn.net …);
#: `qq.com` 这条在实体表里精确命中 0 个实体,是条**死规则**。
#:
#: ⚠️ 因此原名单里两条 host 级写法**必须折成注册域**,否则改判定后它们自己会变成死规则:
#:     baijiahao.baidu.com → baidu.com   ·   mp.weixin.qq.com → qq.com
#:
#: 🔴 收录判据已于 2026-08-16 换代(Owner 拍板)。旧判据「同一注册域下 ≥5 个不同媒体名」
#: **把广告投放位当成了媒体**(china.com 的「中华网快讯焦点图 / 首发 / 首页文字链」按名字算 ≥5,
#: 按主体只有一个)。现判据 = `services.media_name_stem`:**除主导主体外还站着 >=2 个独立发布者**。
#: A 桶那 33 项当初大多选对了,但是靠对的答案蒙对的、不是靠对的方法 —— 已用新尺子全量复检
#: (`scripts/platform_domain_stem_recheck.py`),5 项降级见 `STEM_DEMOTED_NON_PLATFORM`。
#:
#: 分桶(生产实测 314 个注册域,其中 62 个样本足够可判定):
#:   · A 桶(真平台)→ 本集合;
#:   · B 桶(主流媒体主站,多频道但**同一发布主体**,如新华社「新华网体育/财经」)→ **不收**,
#:     判成共享平台等于要求每条额外提供名称证据,增加噪声无收益。B 桶清单见
#:     `B_BUCKET_MAINSTREAM_PORTALS`,待 Owner 逐条点名后才可能上移,**不许悄悄并进来**;
#:   · 148 个单一媒体域(媒体名=1)**一律不收**。
SHARED_PLATFORM_DOMAINS = {
    # —— 原有 11 项(两条 host 级写法已折成注册域)——
    "163.com",
    "baidu.com",          # 原 baijiahao.baidu.com
    "bilibili.com",
    "douyin.com",
    "iesdouyin.com",
    "qq.com",             # 原 mp.weixin.qq.com 折入
    "sohu.com",
    "toutiao.com",
    "weibo.com",
    "zhihu.com",
    # —— A 桶新增:真平台,每个「媒体名」是独立账号(Owner 2026-08-15 拍板 ③)——
    "smzdm.com",          # 什么值得买
    "sina.com.cn",        # 财经号 / 看点
    "csdn.net",           # 博客
    "ifeng.com",          # 凤凰号
    "autohome.com.cn",    # 车家号
    "sina.cn",
    "douban.com",
    "dongchedi.com",
    "meipian.cn",         # 美篇
    "xueqiu.com",         # 雪球
    "taobao.com",         # 江湖
    "360kuai.com",
    "eastmoney.com",      # 财富号
    "xcar.com.cn",        # 爱卡号
    "jianshu.com",        # 简书
    "ctrip.com",
    "pcauto.com.cn",
    "mafengwo.cn",
    "weibo.cn",
    "chooseauto.com.cn",
    "qctt.cn",
    "yoojia.com",
    "zcool.com.cn",
    "digitaling.com",
    "52hrtt.com",
}

#: [补充单 P0-2 2026-08-16] 词干复检从 A 桶降下来的域 —— **和 B 桶同等对待**(都不要求名称证据)。
#:
#: 为什么单列而不是并进 `B_BUCKET_MAINSTREAM_PORTALS`:那 20 项是 Owner 正在过目的清单,
#: 把新降级的混进去会让 Owner 看到的「B 桶 20 项」变成 25 项,清单身份就糊了。两者语义相同、
#: 都不进 `SHARED_PLATFORM_DOMAINS`,三个集合互不相交由断言钉住。
#:
#: 降级依据(生产快照 2026-08-16 · 逐条媒体名样例见交付单):这些域下的「多个媒体名」是
#: **同一主体的版位与地方版**,不是独立账号 ——
#:   36kr.com            36kr长文大首页文字链 / 36氪APP客户端频道推荐 / 36氪官方全文发布
#:   chexun.com          车讯网上海 / 车讯网主站 / 车讯网北京 / 车讯网南昌 / 车讯网成都
#:   chinaventure.com.cn 投中网大首页 / 投中网频道首页 / 投中网首发（融资）
#:   iyiou.com           亿欧网 / 亿欧网大首页快讯推荐 / 亿欧网官方资讯（带图）
#:   yiche.com           易车网主站 / 易车网佛山 / 易车网南京 / 易车网台州(⚠️ 见交付单保留意见)
#:   itouchtv.cn         广东台粤TV客户端 / 广东台触电新闻 / 粤TV客户端（粤眼）/ 触电新闻X
#:                       —— P0-4 抽样人肉判读发现的假阳性,阈值 2→3 后判据自己也判非平台了
#:
#: 🔴 [2026-08-16 根治尝试] `jia.com` 是**手工降级**,判据至今仍判它平台。为什么是手工:
#:   它和 `smzdm.com` 的名字形态**完全一样**(前缀各异 + 尾部同一个括号词),
#:   区别只在现实事实 —— `(齐家网)` 是发布主体、`(什么值得买)` 是托管平台。
#:   任何「把尾部括号当主体标记」的规则,修好 jia 的同时必然把 smzdm 判错
#:   (smzdm 645 条候选 vs jia 14 条)。**名字里没有能区分这两者的信息,不是判据没写好。**
#:
#:   试过换一个不从名字推的信号:供应商分表 `media_source`(mhz_wemedia=自媒体号 / mhz_media=媒体)。
#:   在 37 域普查标注集上「wemedia 条数 >= 3」按域 97.3% > 词干 94.6%、加权 99.80% > 99.73%,
#:   **指标上赢了**;但那点优势**全部来自 zhengguannews.cn,而它已在 B 桶、线上不受影响** ——
#:   只在有行为后果的集合(A 桶 ∩ 已判读 34 域)上重评,两者**打平**(都只错 jia.com 这一个)。
#:   指标赢、行为零收益,却要引入供应商专属依赖 + 快易播覆盖缺口 ⇒ 不采纳。详见交付单。
#:
#:   ⚠️ 必须说明的反证:供应商自己把 jia.com 的库存 **10/14 归到了自媒体号表**,
#:   即媒介盒子的录入口径认为齐家网上多数是自媒体号 —— 这与「齐家网是单一主体」的人工标注**相反**。
#:   本条降级依据的是 Review/Owner 的人工判读,不是供应商数据。若 Owner 认为供应商口径更可信,
#:   把 jia.com 从本集合移回 A 桶即可(移回后判据本身仍判它平台,不需要改任何代码)。
STEM_DEMOTED_NON_PLATFORM = {
    "36kr.com",
    "itouchtv.cn",
    "jia.com",
    "chexun.com",
    "chinaventure.com.cn",
    "iyiou.com",
    "yiche.com",
}

#: B 桶 · 主流媒体主站(多频道 · **同一发布主体**)—— **故意不在** `SHARED_PLATFORM_DOMAINS` 里。
#: 放在这里不是为了被使用,而是为了让「有没有被悄悄并进名单」成为**可断言的事实**:
#: 测试直接遍历本集合断言 `is_shared_platform_domain(d) is False`(见
#: `tests/platform_domain_match_2026_08_15/`)。Owner 若认为其中某些应归 A 桶,逐条点名上移。
B_BUCKET_MAINSTREAM_PORTALS = {
    "news.cn",            # 新华社
    "peopleapp.com",      # 人民日报
    "gmw.cn",             # 光明网
    "cnr.cn",             # 央广
    "huanqiu.com",        # 环球网
    "bjd.com.cn",         # 北京日报
    "xhby.net",           # 新华报业
    "cjn.cn",             # 长江网
    "voc.com.cn",         # 华声在线
    "tidenews.com.cn",    # 潮新闻
    "dzwww.com",          # 大众网
    "ce.cn",              # 中国经济网
    "cet.com.cn",
    "china.com",
    "xnnews.com.cn",
    "zhengguannews.cn",
    "xfrb.com.cn",
    "3news.cn",
    "dzmg.cn",
    "cinic.org.cn",
}


def is_shared_platform_domain(domain: str) -> bool:
    """域名是否属于「人人可开号的共享平台」。

    🔴 [2026-08-15] 两级归一化,缺一不可:
      1. `host_domain` —— 先按 host 级归一(它负责拆 URL、剥 www./m./wap./amp.、
         并**解开 r.jina.ai 包装**;注册域那版不认 jina 前缀,直接喂会得到 `r.jina.ai`);
      2. `registrable_domain` —— 再折到注册域(`blog.csdn.net` → `csdn.net`,
         `aikahao.xcar.com.cn` → `xcar.com.cn`)。
    集合本身也已是注册域(见 SHARED_PLATFORM_DOMAINS 注释),所以这里是精确相等而非后缀匹配 ——
    后缀匹配会把 `notcsdn.net` 这类同尾不同域也吃进来。
    """
    return registrable_domain(host_domain(domain)) in SHARED_PLATFORM_DOMAINS


#: [裸 URL 回落 2026-08-16] 「这个媒体名其实是个 URL / 裸域名 / 邮箱,不是名字」。
#: 判据刻意收得很紧:**含中日韩字符一律不算**(中文媒体名里带点的很多),
#: 且必须整串匹配 —— 只认三种形态:http(s) 链接 / user@host.tld / host.tld。
#: 为什么不能用「host_domain(name) 非空」来判:`host_domain` 对**任何**字符串都返回非空
#: (`host_domain("中华网快讯")` → `"中华网快讯"`),拿它当 URL 探测器等于恒真。
_CJK_RE = re.compile(r"[一-鿿぀-ヿ가-힯]")
_URL_LIKE_RE = re.compile(
    r"^(?:https?://\S+"                                    # http(s) 链接
    r"|[A-Za-z0-9][\w.\-]*@[\w.\-]+\.[A-Za-z]{2,}"         # user@host.tld
    r"|[A-Za-z0-9][\w\-]*(?:\.[\w\-]+)+)$"                 # host.tld
)


def is_url_like_name(name: Any) -> bool:
    """媒体名本身是不是一个 URL / 裸域名 / 邮箱(⇒ 它不是「名字」)。"""
    text = str(name or "").strip()
    if not text or _CJK_RE.search(text):
        return False
    return bool(_URL_LIKE_RE.match(text))


def domain_from_name(name: Any) -> str:
    """媒体名是 URL 形态时,把域名提出来;否则返回空串。

    🔴 这是**回落**,不是新判据:提出来的域名走的还是
    `registrable_domain(host_domain(...))` 那条管道、比的还是同一个
    `SHARED_PLATFORM_DOMAINS`。它只是把原本够不着的输入接进既有管道。
    """
    return str(name or "").strip() if is_url_like_name(name) else ""


def _inventory_id(row: dict[str, Any]) -> int:
    try:
        return int(row.get("inventory_id") or row.get("media_id") or row.get("id") or 0)
    except (TypeError, ValueError):
        return 0


def _display_name(row: dict[str, Any]) -> str:
    return (
        row.get("media_name")
        or row.get("platform_name")
        or row.get("toutiao_name")
        or row.get("name")
        or ""
    )


def normalize_inventory_row(row: dict[str, Any]) -> dict[str, Any]:
    """Normalize media-box inventory row to the matching contract."""
    media_source = row.get("media_source") or row.get("_type") or "mhz_media"
    price_yuan = row.get("price_yuan")
    if price_yuan in (None, ""):
        price_yuan = row.get("our_price_yuan")
    if price_yuan in (None, ""):
        price_yuan = row.get("price")
    price_points = row.get("price_points")
    if price_points in (None, ""):
        price_points = row.get("our_price_points")
    return {
        **row,
        "media_source": media_source,
        "inventory_id": _inventory_id(row),
        "media_name": _display_name(row),
        # [裸 URL 回落 2026-08-16] 末尾多一档 `domain_from_name`:生产实测 36 条候选
        #   entrance_link 为空、而 media_name 本身就是平台 URL(toutiao ×24 / 163 ×12)——
        #   原来这三档全空 ⇒ 拿不到域 ⇒ domain_exact 打不出来 ⇒ 共享平台风险标(只作用于
        #   domain_exact)永远不触发。回落后它们走的还是同一条管道、同一个名单。
        "domain": host_domain(row.get("domain") or row.get("url") or row.get("entrance_link")
                              or domain_from_name(_display_name(row)) or ""),
        "url": row.get("url") or row.get("entrance_link") or "",
        "price_yuan": price_yuan or 0,
        "price_points": price_points or 0,
        "is_active": row.get("is_active", True) is not False,
    }


def _entity_names(entity: dict[str, Any]) -> set[str]:
    names = {canonicalize_media_name(entity.get("canonical_name") or "")}
    names.update(canonicalize_media_name(x) for x in entity.get("aliases") or [])
    return {name for name in names if len(name) >= 2}


def _has_name_evidence(entity: dict[str, Any], row: dict[str, Any]) -> bool:
    """库存行的名字能不能给这个实体提供**名称证据**。

    🔴 [裸 URL 回落 2026-08-16] URL / 裸域名 / 邮箱形态的「名字」**不算名称证据**。
    这不是新判据,是修「什么东西算名字」:生产实测这 36 条的实体 canonical_name
    **就是域名本身**(`toutiao.com`),而 media_name 是
    `https://www.toutiao.com/c/user/token/...` —— 子串一比就命中,
    于是「URL 里含域名」被当成了名称证据,把共享平台风险标抵消掉。
    URL 不是名字,它不能证明这个账号属于谁。
    """
    raw_name = _display_name(row)
    if is_url_like_name(raw_name):
        return False
    row_name = canonicalize_media_name(raw_name)
    if not row_name:
        return False
    return any(name in row_name or row_name in name for name in _entity_names(entity))


def _candidate_key(entity: dict[str, Any], match: dict[str, Any]) -> str:
    return "|".join([
        str(entity.get("entity_key") or ""),
        str(entity.get("industry_key") or entity.get("industry") or "general"),
        str(match.get("media_source") or "mhz_media"),
        str(match.get("inventory_id") or 0),
    ])


def build_binding_candidates(
    entity: dict[str, Any],
    inventory_rows: list[dict[str, Any]],
    *,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Build admin-reviewable binding candidates without trusting raw payloads."""
    normalized_inventory = [normalize_inventory_row(row) for row in inventory_rows]
    matches = match_inventory_to_entity(entity, normalized_inventory)
    # host 级:这是**实体/库存匹配键**(domain_exact 靠它),语义不动;
    # 折注册域只发生在 is_shared_platform_domain 内部。
    entity_domain = host_domain(entity.get("domain") or "")
    candidates: list[dict[str, Any]] = []
    for match in matches:
        name_evidence = _has_name_evidence(entity, match)
        risk_flags: list[str] = []
        if (
            match.get("match_method") == "domain_exact"
            and is_shared_platform_domain(entity_domain or match.get("domain") or "")
            and not name_evidence
        ):
            risk_flags.append("共享平台域名需要名称证据")
        # 🔴 [裸 URL 回落 2026-08-16 · 工单 §2.2] 提取失败的默认必须**保守**:
        #   实体域、库存域(含从媒体名回落提取的)全都拿不到 ⇒ 根本无从判断它挂在谁名下,
        #   这时**不许静默放行**,走「需人工核对」。与快易播回落同一条纪律:拿不准就要证据。
        #   生产实测这类候选当前 0 条(实体域 + entrance_link 双空 = 0 行),所以本条零行为影响,
        #   是为**将来**接进来的数据源守的 —— 那时候没人会记得回来补。
        if not (entity_domain or match.get("domain")):
            risk_flags.append("无法确定域名归属需人工核对")
        if not bool(match.get("is_purchasable", False)):
            risk_flags.append("库存不可采购")

        can_approve = not risk_flags and bool(match.get("is_purchasable", False))
        confidence = float(match.get("match_confidence") or 0)
        if risk_flags:
            confidence = min(confidence, 0.55)

        candidates.append({
            "candidate_key": _candidate_key(entity, match),
            "entity_key": entity.get("entity_key"),
            "industry_key": entity.get("industry_key") or entity.get("industry") or "general",
            "inventory": {
                "media_source": match.get("media_source") or "mhz_media",
                "inventory_id": int(match.get("inventory_id") or 0),
                "media_name": _display_name(match),
                "domain": match.get("domain") or "",
                "url": match.get("url") or match.get("entrance_link") or "",
                "price_yuan": match.get("price_yuan") or 0,
                "price_points": match.get("price_points") or 0,
                "is_active": match.get("is_active", True) is not False,
            },
            "match_method": match.get("match_method") or "",
            "match_confidence": round(confidence, 4),
            "can_approve": can_approve,
            "risk_flags": risk_flags,
            "evidence": {
                "name_evidence": name_evidence,
                "shared_platform_domain": is_shared_platform_domain(entity_domain or match.get("domain") or ""),
                "raw_match_method": match.get("match_method") or "",
            },
        })

    return sorted(
        candidates,
        key=lambda item: (bool(item.get("can_approve")), float(item.get("match_confidence") or 0)),
        reverse=True,
    )[: max(1, int(limit or 20))]


#: [P0-1 2026-08-15] 实体 / 库存缺失也是「实时判据」的一部分,常量化是为了让选集侧与审核侧
#: 引用同一份措辞,而不是各写各的字符串。
BLOCK_ENTITY_MISSING = "媒体实体不存在或缺少影子评分"
BLOCK_INVENTORY_MISSING = "库存资源不存在"
BLOCK_INVENTORY_MISMATCH = "库存记录与候选不一致"
BLOCK_NO_MATCH = "库存没有通过媒体实体匹配"
BLOCK_FALLBACK = "候选未通过校验"


def evaluate_candidate_live(
    entity: dict[str, Any] | None,
    reloaded_inventory: dict[str, Any] | None,
    candidate_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """🔴 [P0-1 2026-08-15] 一条绑定候选的**唯一实时判据入口**。

    背景(生产 bug):选集读 `geo_media_binding_candidates.can_approve` 这个写入时快照,
    审核走实时重算 —— 供应商把媒体下架后两者永久打架:候选恒在「建议通过」计数里,
    点一次失败一次,永远消不掉(生产实证 2026-08-15:候选 20564/20565/20693/20694,
    对应库存 287108/286119/118846/118848 的 `is_active` 全 = f;同批成功的
    20560~20563 对应库存 `is_active` 全 = t)。

    修法**不是**「再写一份 SQL 把 is_active join 进选集」—— 那是把同一判据写第二遍,
    正是本 bug 的成因。修法是让选集与审核调同一个函数,即本函数:

      · 审核侧 `verify_candidate_for_approval` = 本函数 + 「拦不住就抛」的薄包装;
      · 选集侧 `db.media_entity_flywheel_db.revalidate_binding_candidate_rows` 直接调本函数,
        按 block_reason 过滤。

    本函数不抛异常:调用方自行决定是拦(审核 → 409)还是过滤(选集 → 不进「建议通过」)。
    返回 `{"candidate": dict | None, "block_reason": str}`;`block_reason` 为空串即通过。
    """
    candidate_payload = candidate_payload or {}
    if not entity:
        return {"candidate": None, "block_reason": BLOCK_ENTITY_MISSING}
    if not reloaded_inventory:
        return {"candidate": None, "block_reason": BLOCK_INVENTORY_MISSING}

    claimed_inventory = candidate_payload.get("inventory") or {}
    claimed_id = _inventory_id(claimed_inventory)
    trusted_row = normalize_inventory_row(reloaded_inventory)
    if claimed_id and claimed_id != int(trusted_row.get("inventory_id") or 0):
        return {"candidate": None, "block_reason": BLOCK_INVENTORY_MISMATCH}

    candidates = build_binding_candidates(entity, [trusted_row], limit=1)
    if not candidates:
        return {"candidate": None, "block_reason": BLOCK_NO_MATCH}
    candidate = candidates[0]
    if not candidate.get("can_approve"):
        reason = "、".join(candidate.get("risk_flags") or []) or BLOCK_FALLBACK
        # 🔴 拦下来也照样把 candidate 交回去:选集侧要拿它的实时 risk_flags / match_confidence
        #    覆盖陈旧快照,让 UI 上「建议通过 / 证据不足」的标签同样跟着实时判据走。
        return {"candidate": candidate, "block_reason": reason}
    return {"candidate": candidate, "block_reason": ""}


def verify_candidate_for_approval(
    entity: dict[str, Any],
    reloaded_inventory: dict[str, Any],
    candidate_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Re-check a binding candidate from trusted DB inventory before approval.

    🔴 本函数**必须**保持为 `evaluate_candidate_live` 的薄包装 —— 判据只许有一份。
    谁把判定逻辑抄回这里,选集侧就会重新与审核侧漂移(成因见 evaluate_candidate_live 注释)。
    """
    verdict = evaluate_candidate_live(entity, reloaded_inventory, candidate_payload)
    if verdict["block_reason"]:
        raise ValueError(verdict["block_reason"])
    return verdict["candidate"]
