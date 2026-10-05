"""[R 批 · U3] 行业归并 Resolver（LLM 选择题版）—— 代理自助调研入口的第一道口径闸。

老板 2026-07-05 拍板:「填不满的硬编码分类 → 用 LLM 判断」。用户填的行业原文五花八门
(「电梯维保」/「做电梯的」/「电梯行业」都是同一行业),不可能穷举硬编码别名。
本 resolver 用 deepseek-v4-flash **选择题**(给现有平台行业清单让它选,不开放生成,防幻觉)
把用户原文归并到平台标准行业,命中后沉淀别名表,第二次起零 LLM。

三级(SPEC §3-R3.4):
  1. 别名缓存精确命中(resolve_alias) → 零成本返回 resolved_by='alias_cache'。
  2. LLM 归并选择题(未命中且 allow_llm)→ merge 到现有行业 / new 建新行业,写别名 resolved_by='llm'。
  3. fail-soft 兜底(LLM 不可用/解析失败/allow_llm=False)→ 不写别名,以原文建/取行业 resolved_by='fallback'。

🔴🔴 归一同源不变式(「花钱可见」硬约束):
  返回的 industry_key 必须 = normalize_industry_key(**resolved 规范行业名**),不是用户原文。
  自助轮之后所有环节——round 快照的行业名、queue.industry_key、验收断言查发布榜、前端完成后
  查榜——全部用这个 resolved 行业名 / industry_key,不用品牌原文。这样「写库归一 = 读榜归一 =
  验收归一」三者同源,付费跑完榜必点亮。用户原文只是 resolver 的**输入**;归并结果要在确认弹窗
  对用户明示(F1 做),之后一律用规范行业。(不变式落点见 `_result()` 的 industry_key 计算行。)

纪律:
  - 纯 additive · fail-soft 绝不抛断链 · 别名 resolved_by 只写 'llm'|'admin'(fallback 不写别名表)。
  - LLM 只做「归入哪个/是否新行业」的语义判断,零价格数字(成本在 cost_estimator 走 SQL/常量)。
  - 不碰 normalize_industry_key(飞轮 SSOT,只读)/ billing / connection / auth / 四张公共池表。
  - LLM 调用模式(client/model/prompt/tracker/错误处理)照 article_intent_classifier /
    query_intent_classifier 逐条抄(deepseek-v4-flash via DashScope compat-mode)。
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

import httpx

# LLM 归并的解析器复用 article intent 的 JSON 抽取(不另造)
from services.research_monitor.article_intent_classifier import _extract_json_object
from services.research_monitor.industry_registry import ensure_research_industry
# 别名缓存读写 + 同款标准化(与 resolve_alias/write_alias 内部同键)
from db.research_selfserve_db import resolve_alias, write_alias, _normalize_alias_text
# 🔴 飞轮 SSOT · 只读:自助轮全链 industry_key 由它算出,归一同源不变式的唯一权威
from services.media_entity_flywheel import normalize_industry_key
from tools.llm_call_tracker import llm_track, usage_from_response_payload

logger = logging.getLogger("GEO-ResearchMonitor.IndustryResolver")

# ---- LLM 调用常量(抄 article_intent_classifier:18 / query_intent_classifier:32-35)----
DASHSCOPE_COMPAT_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
DEFAULT_RESOLVER_MODEL = (
    os.getenv("RESEARCH_INDUSTRY_RESOLVER_MODEL", "deepseek-v4-flash").strip()
    or "deepseek-v4-flash"
)


def _dashscope_key() -> str:
    return os.getenv("DASHSCOPE_API_KEY", "").strip()


# ============================================================
# DB 只读小工具(单点、可 monkeypatch;真实清单/回名来自 geo_research_industries)
# ============================================================


def _list_active_industries() -> list[dict]:
    """现有 active 行业清单(给 LLM 做选择题的选项)。

    与 scheduler_setup / research_monitor_round_api 同款排序(sort_order, id)。
    """
    from db.connection import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, name
              FROM geo_research_industries
             WHERE active = TRUE
             ORDER BY sort_order, id
            """
        )
        rows = cur.fetchall() or []
        return [{"id": r["id"], "name": r["name"]} for r in rows]
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _industry_id_by_name(name: str) -> Optional[int]:
    """[R#8] 按规范行业名只读取 id(仅 active) —— 纯 SELECT,绝不新建。

    persist=False 的免费预览(/draft)用:LLM 判 merge 命中现有行业时,给弹窗回一个真实
    industry_id,但不触发 ensure_research_industry 的 INSERT(那会让未付费预览往共享表沉淀
    垃圾行业)。命中返 id,否则 None(new 行业 / 无匹配)。
    """
    n = (name or "").strip()
    if not n:
        return None
    from db.connection import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id FROM geo_research_industries WHERE name = %s AND active = TRUE "
            "ORDER BY sort_order, id LIMIT 1",
            (n,),
        )
        row = cur.fetchone()
        return int(row["id"]) if row else None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _industry_name_by_id(industry_id: int) -> Optional[str]:
    """按 industry_id 回取规范行业名(别名缓存命中后拿 name 算 industry_key 用)。

    只取 active 行业:别名指向的行业若已被软删,返 None → 上层降级到 LLM 重新归并
    (不把付费轮路由到死行业)。
    """
    from db.connection import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT name FROM geo_research_industries WHERE id = %s AND active = TRUE",
            (int(industry_id),),
        )
        row = cur.fetchone()
        return row["name"] if row else None
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ============================================================
# LLM 归并选择题(deepseek-v4-flash · 只选清单里的或判新 · 防幻觉)
# ============================================================


def build_resolver_prompt(*, user_industry_raw: str, options: list[str]) -> list[dict[str, str]]:
    """搭 LLM 归并选择题 prompt(结构照 article_intent_classifier.build_intent_prompt)。

    核心约束:merge 时 industry_name 必须与清单里某行业【逐字一致】;只能从清单选或判新,
    禁止生成清单外的旧行业名(防幻觉造出平台没有的「假存量行业」)。
    """
    numbered = "\n".join(f"{i + 1}. {name}" for i, name in enumerate(options)) or "(暂无现有行业)"
    system = (
        "你是中文行业归并分析师。给定用户填写的行业原文,以及平台现有行业清单,"
        "判断应把用户行业归入清单中的哪一个现有行业,还是确属清单之外的新行业。"
        "只能选清单里已经存在的行业名,或判定为新行业并给出规范的中文行业名。"
        "绝对不要生成清单里没有出现过的旧行业名(防幻觉)。只返回 JSON。"
    )
    user = f"""现有平台行业清单(merge 时只能从中选一个):
{numbered}

判断规则:
- 若用户行业与清单中某个现有行业本质是同一行业(同义 / 子类 / 口语化 / 简写),
  decision="merge",industry_name 必须与清单中该行业名【逐字一致】。
- 若用户行业确实不属于清单中任何一个,decision="new",industry_name 给一个简洁规范的
  中文行业名(不要照抄清单里的名字,也不要带公司名 / 品牌名)。
- confidence 是 0 到 1 的小数,表示你的把握程度;reason 用一句中文说明。

只返回 JSON,格式:
{{"decision":"merge","industry_name":"电梯行业","confidence":0.9,"reason":"用户写的电梯维保属于电梯行业"}}

用户行业原文: {user_industry_raw or ''}
"""
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _normalize_resolver_decision(payload: Any, option_names: list[str]) -> dict:
    """把 LLM 原始 JSON 归一成 {decision, industry_name, confidence, reason}。

    防幻觉纠偏(用清单做真值):
      - merge 但给的名字不在清单 → 当 new(不硬造一个平台没有的「旧行业」)。
      - new 但清单里其实已有同名 → 纠为 merge(幂等去重,别名指到已存在行业)。
      - merge 命中清单 → 用清单里的【逐字规范名】(消大小写/全半角/空白漂移)。
    normalized 匹配复用 _normalize_alias_text(与别名表同键,口径一致)。
    """
    decision = str(payload.get("decision") or "").strip().lower()
    name = str(payload.get("industry_name") or "").strip()
    try:
        confidence = float(payload.get("confidence", 0.0))
    except Exception:
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))
    if not name:
        raise ValueError("resolver_empty_industry_name")

    norm_to_canonical = {_normalize_alias_text(n): n for n in option_names}
    matched = norm_to_canonical.get(_normalize_alias_text(name))

    if decision == "merge":
        if matched:
            name = matched  # 用清单逐字规范名
        else:
            decision = "new"  # 说 merge 却给了清单外的名字 → 当新行业,不硬造旧名
    elif decision == "new":
        if matched:
            decision = "merge"  # 说 new 但清单已有同名 → 纠正为 merge(去重)
            name = matched
    else:
        # 未知 decision → 用是否命中清单推断
        decision = "merge" if matched else "new"
        if matched:
            name = matched

    return {
        "decision": decision,
        "industry_name": name,
        "confidence": confidence,
        "reason": str(payload.get("reason") or "").strip()[:300],
    }


async def _llm_resolve_industry(
    user_industry_raw: str, *, model: str = DEFAULT_RESOLVER_MODEL
) -> dict:
    """单点 LLM 归并调用(async httpx → DashScope compat-mode · llm_track 计费 · 失败上抛)。

    抄 article_intent_classifier.classify_article_intent:临时 AsyncClient(timeout=60)、
    temperature=0、response_format=json_object、llm_track 记 token、异常先 record(success=False) 再 raise。
    返回归一后的 {decision, industry_name, confidence, reason}。上层 orchestrator 负责 fail-soft 兜底。
    """
    api_key = _dashscope_key()
    if not api_key:
        raise RuntimeError("DASHSCOPE_API_KEY 未配置,无法进行行业归并")

    options = _list_active_industries()
    option_names = [o["name"] for o in options]

    payload = {
        "model": model,
        "messages": build_resolver_prompt(user_industry_raw=user_industry_raw, options=option_names),
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=60.0) as client:
        async with llm_track(
            "research_monitor",
            "dashscope",
            model=model,
            metadata={"task": "industry_resolve"},
        ) as tracker:
            try:
                resp = await client.post(DASHSCOPE_COMPAT_URL, headers=headers, json=payload)
                resp.raise_for_status()
                data = resp.json()
                input_tokens, output_tokens, cached_tokens = usage_from_response_payload(data)
                tracker.record(
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cached_tokens=cached_tokens,
                    success=True,
                )
            except Exception as exc:
                tracker.record(success=False, error_msg=str(exc)[:500])
                raise
    content_text = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
    return _normalize_resolver_decision(_extract_json_object(content_text), option_names)


# ============================================================
# 结果打包(归一同源不变式的唯一落点)
# ============================================================


def _result(
    industry_id: Optional[int],
    industry_name: str,
    resolved_by: str,
    *,
    is_new: bool,
    confidence: Optional[float],
    user_industry_raw: str,
) -> dict:
    """统一返回结构。

    🔴 归一同源不变式落点:industry_key = normalize_industry_key(**resolved 规范行业名**),
    不是用户原文。自助轮之后 round 快照 / queue.industry_key / 验收查榜全用这个 key,
    与写库、读榜三者同源。
    """
    name = (industry_name or "").strip()
    return {
        "industry_id": int(industry_id) if industry_id is not None else None,
        "industry_name": name,
        # ↓↓↓ 不变式:归一化的是 resolved 规范名,不是 user_industry_raw ↓↓↓
        "industry_key": normalize_industry_key(name),
        "resolved_by": resolved_by,
        "is_new": bool(is_new),
        "confidence": float(confidence) if confidence is not None else None,
        "user_industry_raw": user_industry_raw or "",
    }


def _taxonomy_result(front, raw: str, persist: bool) -> dict:
    """前段(`services.industry_routing.route_front`)命中 → 统一返回结构。

    字典命中(`resolved_by='taxonomy'`):industry_key 仍 = normalize(**调研行规范名**)(归一同源不变式);
    预览(persist=False)只读:行存在就回它的 id(停用也回 —— 付费即点亮这一行),没建 ⇒ None;
    persist=True 才按**字典 slug** 建行 / 翻 active。**不写别名**(Review 09-23:按上下文判出的结果
    写进按原文作键的别名表,会把同原文的真建筑公司一起带走;字典本身就是缓存)。
    """
    from services.industry_routing import BY_TAXONOMY, category_public

    if front.by != BY_TAXONOMY:
        return _result(front.industry_id, front.industry_name, "alias_cache",
                       is_new=False, confidence=None, user_industry_raw=raw)
    industry_id = front.industry_id
    if persist:
        from services.research_monitor.industry_registry import ensure_taxonomy_research_row
        industry_id, _created, _activated = ensure_taxonomy_research_row(front.slug, front.industry_name)
    out = _result(industry_id, front.industry_name, "taxonomy",
                  is_new=front.industry_id is None,
                  confidence=front.category.confidence if front.category else None,
                  user_industry_raw=raw)
    out["category"] = category_public(front.category)
    out["taxonomy_slug"] = front.slug
    out["taxonomy_row_active"] = bool(front.active)
    return out


async def resolve_or_create_industry(
    user_industry_raw: str, *, allow_llm: bool = True, persist: bool = True,
    taxonomy: bool = False, brand: Optional[dict] = None, category_key: Optional[str] = None,
) -> dict:
    """把用户行业原文归并到平台标准行业(三级 · fail-soft 绝不断链)。

    返回:{industry_id, industry_name(规范名), industry_key(=normalize_industry_key(规范名)),
           resolved_by('alias_cache'|'llm'|'fallback'), is_new, confidence, user_industry_raw}。

    [R#8] persist(默认 True):正式 /self-serve 走 True —— LLM 归并结果 ensure_research_industry
    落库沉淀 + write_alias 写别名(第二次起零 LLM 命中级 1)。免费 /draft 预览走 False ——
    【只读】:跳过 ensure_research_industry 与 write_alias(绝不往共享表 geo_research_industries /
    别名表写),new 决策返回 industry_id=None(仅给决策 + 规范名供弹窗明示),merge 命中现有行业
    仍以只读 SELECT 返回真实 id。别名精确命中(级 1)本就只读,persist 两值行为一致。
    """
    raw = (user_industry_raw or "").strip()

    # —— 级 0(WO_267 · **调用方显式开启**,只有自助调研端点开):行业路由前段 ——
    #    admin 人工别名 > 行业大类字典(吃品牌上下文)> LLM 别名,与读路径 industry_canonical
    #    共用 `route_front` 这**一份**顺序。不开的调用方(包 B 的 freeze_industry 只认 llm 归并)逐字不变。
    #    前段没中 ⇒ 别名缓存已经查过,跳过下面的级 1,直接 LLM。
    if taxonomy:
        from services.industry_routing import route_front
        front = route_front(raw, brand=brand, category_key=category_key)
        if front is not None:
            return _taxonomy_result(front, raw, persist)
        cached_id = None
    else:
        # —— 级 1:别名缓存精确命中(零成本 · 第二次起走这里) ——
        cached_id = resolve_alias(raw)  # 内部已 _normalize_alias_text
    if cached_id is not None:
        name = _industry_name_by_id(cached_id)
        if name:
            return _result(
                cached_id, name, "alias_cache",
                is_new=False, confidence=None, user_industry_raw=raw,
            )
        # 别名指向的行业已软删/取不到名 → 落穿到 LLM 重新归并(不路由死行业)
        logger.info("[resolver] 别名命中但行业名取不到(可能软删),重新归并 raw=%r id=%s", raw, cached_id)

    # —— 级 2:LLM 归并选择题(未命中 + allow_llm) ——
    if allow_llm and raw:
        try:
            decision = await _llm_resolve_industry(raw)
            resolved_name = (decision.get("industry_name") or "").strip()
            confidence = decision.get("confidence")
            is_new = decision.get("decision") == "new"
            if resolved_name:
                if persist:
                    industry_id = ensure_research_industry(resolved_name)  # 幂等 upsert 拿 id
                    if industry_id is not None:
                        # 沉淀别名(resolved_by 只允许 'llm'|'admin');第二次起命中级 1
                        # 🔴 2026-08-09:`write_alias` 已改成**仅首次写入 + 写后重读**,
                        #    返回库里生效的那条。冲突时(常见于 admin 已人工改判)
                        #    **必须采信生效值**,不能继续用 LLM 那一版 ——
                        #    否则本次调用返回的 industry_id 与库里的别名映射不一致,
                        #    下一次走级 1 命中又会变回去,同一个原文两次给两个行业。
                        effective = write_alias(raw, industry_id, confidence,
                                                resolved_by="llm")
                        eff_id, eff_by = industry_id, "llm"
                        if effective and effective.get("active"):
                            eff_id = int(effective["industry_id"])
                            eff_by = str(effective.get("resolved_by") or "llm")
                            if eff_id != industry_id:
                                # 复用本模块既有的那一个(只取 active 行业),不另造。
                                eff_name = _industry_name_by_id(eff_id)
                                logger.info(
                                    "[industry-resolver] 别名已被 %s 定为 %s(%s),"
                                    "本次 LLM 的 %s 不采用 raw=%r",
                                    eff_by, eff_id, eff_name, industry_id, raw)
                                if eff_name:
                                    resolved_name = eff_name
                        return _result(
                            eff_id, resolved_name, eff_by,
                            is_new=(is_new and eff_id == industry_id),
                            confidence=confidence, user_industry_raw=raw,
                        )
                else:
                    # [R#8] persist=False(免费预览):不写库(不 ensure / 不 write_alias)。
                    #   merge 命中现有行业 → 只读 SELECT 拿真实 id 给弹窗;new → id=None(不建行业)。
                    preview_id = None if is_new else _industry_id_by_name(resolved_name)
                    return _result(
                        preview_id, resolved_name, "llm",
                        is_new=is_new, confidence=confidence, user_industry_raw=raw,
                    )
            logger.warning("[resolver] LLM 归并结果不可用(name=%r id 取不到),降级 fallback raw=%r", resolved_name, raw)
        except Exception as exc:  # fail-soft:LLM 不可用/解析失败绝不抛断链
            logger.warning("[resolver] LLM 归并失败,降级 fallback raw=%r: %s", raw, str(exc)[:200])

    # —— 级 3:fail-soft 兜底(不写别名表 · 避免无置信映射污染 · 也不违反 CHECK) ——
    fallback_name = raw or (user_industry_raw or "")
    if not fallback_name:
        industry_id = None
    elif persist:
        industry_id = ensure_research_industry(fallback_name)
    else:
        # [R#8] persist=False(免费预览):不新建行业,只读 SELECT(命中现有给 id,否则 None)。
        industry_id = _industry_id_by_name(fallback_name)
    return _result(
        industry_id, fallback_name, "fallback",
        is_new=False, confidence=None, user_industry_raw=raw,
    )


async def preview_resolve(user_industry_raw: str, *, taxonomy: bool = False,
                          brand: Optional[dict] = None, category_key: Optional[str] = None) -> dict:
    """draft 免费预览用的轻量归并(与 resolve_or_create_industry 同口径,但【只读】)。

    [R#8] 免费 /draft 预览走 persist=False:LLM 归并仅做「归入哪个 / 是否新行业」的语义判断,
    绝不 ensure_research_industry(INSERT 行业)/ write_alias(INSERT 别名)—— 未付费端点不写
    共享表,也不让 auto-created 垃圾行业进 resolver 选项池。别名沉淀留给正式 /self-serve(那次
    persist=True)。弹窗仍能拿到决策 + 规范行业名做归并明示(F1);merge 命中现有行业还能拿真实 id。
    """
    return await resolve_or_create_industry(user_industry_raw, allow_llm=True, persist=False,
                                            taxonomy=taxonomy, brand=brand, category_key=category_key)
