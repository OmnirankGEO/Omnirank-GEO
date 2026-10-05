"""
quote_keyword_sync — 代理一键激活路径下的关键词同步 helper

用途:
    api/selection_api.py:mark_paid 走 token 路径(客户在 /s/:token 提交付款)有完整 200 行同步逻辑
    原一键激活端点走 quote_id 路径(代理后台一键激活)需要相同的同步效果([开源 E3 · WO_323 G3a · 2026-10-02] 该端点已删)

为避免动 selection_api.mark_paid(代理日常在用 · 风险高),本模块独立实现
轻量同步逻辑,仅复用 db.save_confirmed_keywords + save_keyword_cluster +
classify_keyword_layers_batch。

幂等:
    - 已有 confirmed_keywords WHERE quote_id → 跳过(返回现有数量)
    - 没有 session by quote_id → 返回 0(不阻断激活)

只处理 cluster mode 主路径 + flat mode fallback,与 selection_api.mark_paid 一致。

CTO-15.18 A.6 修复(2026-04-28 · Deploy-CTO 实证 quote 274):
    - BUG:flat mode 下 pricing_data.keywords 空时返 (0, 0) → confirmed_keywords 表空白
      但 quotes.total_keywords 写了数字 → 数据严重不一致
    - 修法:加 markdown fallback · pricing 空时从 quote.markdown 提关键词名 + 默认 layer
      至少落库 keyword 名(price=0 layer=NULL · 写作流可后续补价)
"""

import json
import logging
import re
from typing import Tuple, List

# [P1 容量合同 2026-08-08] 篇数唯一取数出口(本文件是 selection_api.mark_paid 的平行冻结路径,
#   两条路径必须同口径,否则同一张单走哪条激活就冻出不同容量)
from tools.pricing_bands import (
    LEGACY_MISSING_CAPACITY_DEFAULT as _CAPACITY_MISSING_DEFAULT,
    normalize_article_capacity as _normalize_article_capacity,
)

logger = logging.getLogger("GEO-QuoteKeywordSync")

# CTO-15.18 A.6 · markdown 关键词正则
# 支持 3 种 markdown 格式:
#   1. list 行 "- 关键词" / "- **关键词** · ¥1,038/月" / "- 关键词 (元/月)"
#   2. bold "**关键词**"
#   3. **表格行** | 关键词 | 热度 | 竞争 | 意图 | 价值 | 入门 | 标准 | 旗舰 |  (老板 2026-04-28 拍板真实格式)
# 排除标题(#)、引擎名(豆包/Kimi/DeepSeek)、价格行(¥开头)、纯数字
_MD_KW_LINE = re.compile(r"^\s*[-*]\s+\*?\*?([^\s\-*•·:|()\[\]【】#`*\d]{2,30}?)\*?\*?(?:\s*[·•|]\s*¥|\s*¥|\s*\(|\s*$)", re.MULTILINE)
_MD_KW_BOLD = re.compile(r"\*\*([^\s\-*\d¥]{2,30}?)\*\*")
# 表格行第一列(关键词字段)· 跳过表头/分隔行
_MD_TABLE_ROW = re.compile(r"^\s*\|\s*([^\s|][^|]{0,29}?)\s*\|", re.MULTILINE)
_BANNED_KW = {"豆包", "Kimi", "DeepSeek", "qwen3-max", "qwen3.7-max", "qwen3.6-plus", "GEO 总分", "AI 实测证据",
              "起步级", "勉强级", "良好级", "完善级", "严重不足",
              "类目抢占", "场景决策", "本地获客", "高转化场景",
              "基础版", "标准版", "旗舰版", "进阶版",
              "关键词", "热度", "竞争", "意图", "价值", "入门", "标准", "旗舰",
              "类型", "层级", "客户", "品牌", "价格", "月费"}


def _extract_keywords_from_markdown(md_text: str) -> List[str]:
    """从 quote.markdown 兜底提取关键词名(去重 · 限长 30 · 排除技术词)

    支持 3 种 markdown 格式:list / bold / 表格(老板 2026-04-28 实证 quote 274 是表格)

    返回:候选关键词列表(去重 · 长度 2-30 · 不含禁词)
    """
    if not md_text:
        return []
    candidates: List[str] = []
    seen: set = set()

    def _try_add(kw: str) -> None:
        kw = (kw or "").strip()
        # 表格分隔行 "---" / 表头列名 / 空格符号过滤
        if not kw or kw.startswith("-") or set(kw) <= set("- :"):
            return
        if kw in seen or kw in _BANNED_KW:
            return
        if len(kw) < 2 or len(kw) > 30:
            return
        if not re.search(r"[一-龥a-zA-Z]", kw):
            return
        seen.add(kw)
        candidates.append(kw)

    # 1. 表格行优先(quote.markdown 主格式 · 实证 quote 274)
    for m in _MD_TABLE_ROW.finditer(md_text):
        _try_add(m.group(1))
    # 2. list 行
    for m in _MD_KW_LINE.finditer(md_text):
        _try_add(m.group(1))
    # 3. bold(catch "**深圳家装设计师**")
    for m in _MD_KW_BOLD.finditer(md_text):
        _try_add(m.group(1))
    return candidates


def _safe_json(val, default=None):
    if default is None:
        default = {}
    if val is None:
        return default
    if isinstance(val, (dict, list)):
        return val
    if isinstance(val, str):
        try:
            return json.loads(val)
        except Exception:
            return default
    return default


def _build_brand_ctx(quote_id: int) -> dict:
    """从 quote + brand 反查 brand context 给 6 层分类用"""
    from db.diagnosis_db import get_quote, get_connection

    ctx = {"brand_name": "", "industry": "", "city": "", "competitors": []}
    try:
        q = get_quote(quote_id)
        if not q:
            return ctx
        ctx["brand_name"] = (q.get("brand_name") or "").strip()
        ctx["industry"] = (q.get("industry") or "").strip()
        ctx["city"] = (q.get("city") or "").strip()
        bid = q.get("brand_id")
        if not bid:
            return ctx
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT competitors_jsonb FROM brands WHERE id = %s", (bid,))
            row = cur.fetchone()
            if row:
                raw = row.get("competitors_jsonb") if isinstance(row, dict) else row[0]
                if isinstance(raw, str):
                    try:
                        raw = json.loads(raw)
                    except (ValueError, TypeError):
                        raw = []
                if isinstance(raw, list):
                    for c in raw:
                        if isinstance(c, dict) and c.get("name"):
                            ctx["competitors"].append(str(c["name"]).strip())
                        elif isinstance(c, str) and c.strip():
                            ctx["competitors"].append(c.strip())
        finally:
            try:
                conn.close()
            except Exception:
                pass
    except Exception as e:
        logger.warning(f"[quote_keyword_sync] brand_ctx 反查失败 quote={quote_id}: {e}")
    return ctx


def _attach_layer(kw_list: list, brand_ctx: dict) -> list:
    """对 kw_list 内每个 dict 注入 layer / layer_reason / layer_confidence"""
    if not kw_list:
        return kw_list
    try:
        from services.keyword_layer_classifier import classify_keyword_layers_batch
    except Exception as e:
        logger.warning(f"[quote_keyword_sync] keyword_layer_classifier import 失败: {e}")
        return kw_list

    names = [kw.get("keyword", "") for kw in kw_list if kw.get("keyword")]
    if not names:
        return kw_list
    try:
        layer_map = classify_keyword_layers_batch(
            names,
            brand_name=brand_ctx["brand_name"],
            city=brand_ctx["city"],
            industry=brand_ctx["industry"],
            competitors=brand_ctx["competitors"],
        )
        for kw in kw_list:
            name = kw.get("keyword", "")
            res = layer_map.get(name)
            if res:
                kw.setdefault("layer", res["layer"])
                kw.setdefault("layer_reason", res["reason"])
                kw.setdefault("layer_confidence", res["confidence"])
    except Exception as e:
        logger.warning(f"[quote_keyword_sync] layer 分类失败: {e}")
    return kw_list


def sync_quote_keywords_to_confirmed(quote_id: int) -> Tuple[int, int]:
    """
    确保 quote 的关键词同步到 confirmed_keywords 表。

    流程:
        1. COUNT confirmed_keywords WHERE quote_id → 有则幂等返回
        2. 查 keyword_selection_sessions 里最新 session by quote_id
        3. 没 session → 返回 (0, 0)
        4. 解析 clusters_data / pricing_data → save_confirmed_keywords + save_keyword_cluster
        5. 返回 (kw_count, cluster_count)

    Args:
        quote_id: 报价单 ID

    Returns:
        (kw_count, cluster_count) — 同步后的关键词数 + 主题包数
    """
    from db.diagnosis_db import (
        get_connection,
        save_confirmed_keywords,
        save_keyword_cluster,
    )

    conn = get_connection()
    try:
        cur = conn.cursor()
        # 1. 幂等
        cur.execute(
            "SELECT COUNT(*) AS cnt FROM confirmed_keywords WHERE quote_id = %s",
            (quote_id,),
        )
        row = cur.fetchone()
        existing = row["cnt"] if isinstance(row, dict) else (row[0] if row else 0)
        if existing and existing > 0:
            # 同步过 · 返回现有数量(cluster_count 不重复算)
            cur.execute(
                "SELECT COUNT(DISTINCT cluster_id) AS ccnt FROM confirmed_keywords "
                "WHERE quote_id = %s AND cluster_id IS NOT NULL",
                (quote_id,),
            )
            row2 = cur.fetchone()
            cc = row2["ccnt"] if isinstance(row2, dict) else (row2[0] if row2 else 0)
            return existing, cc

        # 2. 查最新 session
        cur.execute(
            """
            SELECT id, token, status, brand_id, selected_tier,
                   clusters_data, pricing_data, final_keyword_ids, gift_keywords
            FROM keyword_selection_sessions
            WHERE quote_id = %s
            ORDER BY id DESC LIMIT 1
            """,
            (quote_id,),
        )
        session = cur.fetchone()
        if not session:
            logger.info(f"[quote_keyword_sync] quote_id={quote_id} 无关联 session · 跳过同步")
            return 0, 0
    finally:
        try:
            conn.close()
        except Exception:
            pass

    # 3. brand context
    brand_ctx = _build_brand_ctx(quote_id)

    # 4. 解析 + 同步
    tier = session.get("selected_tier") if isinstance(session, dict) else session[4] or "standard"
    if isinstance(session, dict):
        clusters_data = _safe_json(session.get("clusters_data"))
        pricing_data = _safe_json(session.get("pricing_data"), {})
        final_ids = set(_safe_json(session.get("final_keyword_ids"), []))
        gift_keywords = _safe_json(session.get("gift_keywords"), [])
        token = session.get("token") or ""
    else:
        # tuple fallback (psycopg2 默认 cursor)
        clusters_data = _safe_json(session[5])
        pricing_data = _safe_json(session[6], {})
        final_ids = set(_safe_json(session[7], []))
        gift_keywords = _safe_json(session[8], [])
        token = session[1] or ""

    kw_count = 0
    cluster_count = 0

    try:
        if clusters_data and clusters_data.get("clusters"):
            # ── Cluster mode ──
            for cl in clusters_data["clusters"]:
                if not cl.get("is_selected"):
                    continue
                cluster_id = save_keyword_cluster(quote_id, token, cl)
                cluster_count += 1

                kw_list = []
                for kw in cl.get("core_keywords", []):
                    if not kw.get("is_selected"):
                        continue
                    tier_data = kw.get(tier, {})
                    kw_list.append({
                        "keyword": kw["keyword"],
                        "category": cl.get("business_tag", "通用词"),
                        "tier": tier,
                        "base_price": tier_data.get("price", 0),
                        "city_premium": 1.0,
                        "final_price": tier_data.get("price", 0),
                        "competitor_count": 0,
                        # [P1 容量合同 2026-08-08] 容量上限口径 · 走 SSOT 规整(显式 0 保留)
                        "required_articles": _normalize_article_capacity(
                            tier_data.get("articles"), when_missing=_CAPACITY_MISSING_DEFAULT),
                        "intent": kw.get("intent", "informational"),
                        "funnel_stage": kw.get("funnel_stage", "awareness"),
                        "cluster_id": cluster_id,
                        "is_core": True,
                        # [audit P2 2026-06-10] 超红海标透传(对照 mark_paid selection_api:2889 同字段)
                        # 旧版丢标 → 一键激活路径的超红海词混入达标计数(与服务锚口径"超红海不计达标"冲突)
                        "super_red_ocean": bool(kw.get("super_red_ocean", False)),
                    })

                for kw in cl.get("covered_keywords", []):
                    if kw.get("source") == "generated_variant":
                        continue
                    kw_list.append({
                        "keyword": kw["keyword"],
                        "category": cl.get("business_tag", "通用词"),
                        "tier": tier,
                        "base_price": 0,
                        "final_price": 0,
                        "competitor_count": 0,
                        "required_articles": 0,
                        "intent": "informational",
                        "funnel_stage": "awareness",
                        "cluster_id": cluster_id,
                        "is_core": False,
                        "super_red_ocean": bool(kw.get("super_red_ocean", False)),  # [audit P2 2026-06-10] 同 core
                    })

                if kw_list:
                    save_confirmed_keywords(quote_id, _attach_layer(kw_list, brand_ctx))
                    kw_count += len(kw_list)

            for uk in clusters_data.get("unclustered_keywords", []):
                uk_tier_data = uk.get(tier, {})
                if uk_tier_data.get("price", 0) > 0:
                    save_confirmed_keywords(quote_id, _attach_layer([{
                        "keyword": uk["keyword"],
                        "category": "自定义",
                        "tier": tier,
                        "base_price": uk_tier_data.get("price", 0),
                        "final_price": uk_tier_data.get("price", 0),
                        "competitor_count": 0,
                        # [P1 容量合同 2026-08-08] 同上
                        "required_articles": _normalize_article_capacity(
                            uk_tier_data.get("articles"), when_missing=_CAPACITY_MISSING_DEFAULT),
                        "intent": uk.get("intent", "informational"),
                        "funnel_stage": uk.get("funnel_stage", "awareness"),
                        "is_core": True,
                        "super_red_ocean": bool(uk.get("super_red_ocean", False)),  # [audit #5 返修] ⑥ 漏 unclustered sink · 对齐 mark_paid 核心词路径
                    }], brand_ctx))
                    kw_count += 1
        else:
            # ── Flat mode (backward compat) ──
            kw_list = []
            for pk in pricing_data.get("keywords", []):
                if pk.get("id") in final_ids:
                    tier_data = pk.get(tier, {})
                    kw_list.append({
                        "keyword": pk["keyword"],
                        "category": pk.get("category_label", "通用词"),
                        "tier": tier,
                        "base_price": tier_data.get("price", 0),
                        "city_premium": 1.0,
                        "final_price": tier_data.get("price", 0),
                        "competitor_count": 0,
                        # [P1 容量合同 2026-08-08] 容量上限口径 · 走 SSOT 规整(显式 0 保留)
                        "required_articles": _normalize_article_capacity(
                            tier_data.get("articles"), when_missing=_CAPACITY_MISSING_DEFAULT),
                        "intent": pk.get("intent", "informational"),
                        "funnel_stage": pk.get("funnel_stage", "awareness"),
                        "super_red_ocean": bool(pk.get("super_red_ocean", False)),  # [audit P2 2026-06-10] flat 同步
                    })

            # [P0 2026-06-05 老板定稿] gift_keywords 不再写入 confirmed_keywords(根治免费核心词后门)
            #   赠送词仅作服务备注(session 级)· 不进核心词/监控/写作链 · 想监控须作为已选核心词单独计价。

            if kw_list:
                save_confirmed_keywords(quote_id, _attach_layer(kw_list, brand_ctx))
                kw_count = len(kw_list)

        # CTO-15.18 A.6 (2026-04-28 · Deploy-CTO 实证)markdown 兜底
        # 老板红线:pricing 空时**不能返 0** · 必须从 quote.markdown 提词回填
        # 至少落库 keyword 名 · price=0 layer=NULL · 写作流可后续补价
        if kw_count == 0:
            try:
                from db.diagnosis_db import get_quote
                q = get_quote(quote_id)
                md_text = (q or {}).get("markdown") or ""
                md_kws = _extract_keywords_from_markdown(md_text)
                if md_kws:
                    md_kw_list = [{
                        "keyword": kw,
                        "category": "通用词",
                        "tier": tier,
                        "base_price": 0,
                        "city_premium": 1.0,
                        "final_price": 0,
                        "competitor_count": 0,
                        "required_articles": 0,
                        "intent": "informational",
                        "funnel_stage": "awareness",
                        "is_core": False,
                    } for kw in md_kws]
                    save_confirmed_keywords(quote_id, _attach_layer(md_kw_list, brand_ctx))
                    kw_count = len(md_kw_list)
                    logger.warning(
                        f"[quote_keyword_sync] CTO-15.18 A.6 markdown 兜底 quote_id={quote_id} "
                        f"提词 {kw_count} 个 · price=0 待写作流补价"
                    )
            except Exception as _me:
                logger.error(f"[quote_keyword_sync] markdown 兜底失败 quote_id={quote_id}: {_me}")
    except Exception as e:
        logger.error(f"[quote_keyword_sync] 同步失败 quote_id={quote_id}: {e}")
        return kw_count, cluster_count

    logger.info(
        f"[quote_keyword_sync] quote_id={quote_id} 同步 {kw_count} 关键词"
        + (f" / {cluster_count} 主题包" if cluster_count else "")
    )
    return kw_count, cluster_count


# ============================================================
# B.7 + B.8 (CTO-15.18 · 2026-04-28 · PM 干预 类 B):
#   quote 数字 SSOT helper · 修三处数字打架(顶部 X 词 / 矩阵 Y 词 Z 层 / 实际显示)
#   月费占位填实 · 替换"具体月费见方案"为真实最低档月费
# ============================================================

def get_quote_keyword_count_ssot(quote_id: int) -> dict:
    """quote 关键词数 SSOT(单一真相源 · 三处一致)

    返:
      {
        "matrix_count": int,    # 6 层关键词矩阵真实数(confirmed_keywords COUNT WHERE quote_id)
        "displayed_count": int, # 前端 banner 显示数(=matrix_count)
        "confirmed_count": int, # quotes.total_keywords 字段(legacy · 用作 reconcile 检查)
        "consistent": bool,     # 三处是否一致(若 false → audit_log warning)
      }

    用法:前端 quote proposal 页 / banner / 矩阵都调这个 helper · 不再各自 SQL
    老板痛点(40 岁老销售实测):quote 顶部 5 词 / 矩阵 7-8 词 / 实际 5 词 三处打架
    """
    from db.connection import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        # 真相源 = confirmed_keywords COUNT(soft delete 排除)
        cur.execute(
            """
            SELECT COUNT(*) AS cnt FROM confirmed_keywords
            WHERE quote_id = %s
              AND (status IS NULL OR status NOT IN ('deleted', 'agent_removed'))
            """,
            (quote_id,),
        )
        r = cur.fetchone()
        matrix_count = int(r["cnt"]) if r else 0

        # quotes.total_keywords legacy 字段(可能不准 · 用作 reconcile 检查)
        cur.execute(
            "SELECT total_keywords FROM quotes WHERE id = %s",
            (quote_id,),
        )
        r2 = cur.fetchone()
        confirmed_count = int(r2.get("total_keywords") or 0) if r2 else 0
    finally:
        try:
            conn.close()
        except Exception:
            pass

    return {
        "matrix_count": matrix_count,
        "displayed_count": matrix_count,  # 前端用 matrix_count 显示 · 不用 quotes.total_keywords
        "confirmed_count": confirmed_count,
        "consistent": matrix_count == confirmed_count,
    }


def get_quote_monthly_price_ssot(quote_id: int) -> dict:
    """quote 月费 SSOT · 解 B.8 月费占位"具体月费见方案"

    返:
      {
        "monthly_price": float | None,  # quotes.monthly_price 字段
        "min_package_price": float | None,  # quote_packages 最低档月费(基础版)
        "package_count": int,           # quote_packages 数量
        "display_text": str,            # 给前端话术展示用的"¥X起"(2026-06-05 去「/月」报价口径 · 交付目标=累计达标30天)
      }

    老板痛点:话术模板里"具体月费见方案"占位词 → 替换为真实月费
    """
    from db.connection import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT monthly_price, paid_amount FROM quotes WHERE id = %s",
            (quote_id,),
        )
        q = cur.fetchone()
        monthly_price = float(q.get("monthly_price")) if q and q.get("monthly_price") else None
        if monthly_price is None:
            try:
                from services.quote_numeric_repair import repair_quote_numeric_fields
                repaired = repair_quote_numeric_fields(quote_id)
                if repaired.get("monthly_price"):
                    monthly_price = float(repaired["monthly_price"])
            except Exception:
                pass

        # quote_packages 最低档(若有)· 不一定每个 quote 都有 packages
        min_pkg_price: float | None = None
        pkg_count = 0
        # [WO_242] 同样查不存在的 `quote_packages`。
        # ⚠️ **订正工单一处**:工单写「也查 quote_packages 后继续用同一 cursor」——
        #    实际这个函数里 `SELECT` 之后**没有任何语句**,`finally` 紧接着 close 连接,
        #    被打废的事务随连接一起死掉。⇒ 这一处是**潜伏的,不是活的**。
        #    仍然改:代价一行,而「哪天有人在后面加一条语句」是这类缺陷唯一的发生方式。
        from db.txn_guard import swallow
        with swallow(cur, "sp_pkg_minprice",
                     where="services/quote_keyword_sync.py:get_quote_monthly_price_ssot"):
            cur.execute(
                "SELECT MIN(monthly_price) AS mn, COUNT(*) AS c FROM quote_packages "
                "WHERE quote_id = %s",
                (quote_id,),
            )
            r = cur.fetchone()
            if r:
                if r.get("mn") is not None:
                    min_pkg_price = float(r["mn"])
                pkg_count = int(r.get("c") or 0)
    finally:
        try:
            conn.close()
        except Exception:
            pass

    # 显示文本:优先用 monthly_price 主档 · 其次 packages 最低档 · 都没有时 fallback
    # [2026-06-05 边界补丁1] 去掉「/月」报价交付口径 · 本轮 GEO 交付目标 = 累计达标30天(非按月套餐)
    if monthly_price:
        display = f"¥{int(monthly_price):,}"
    elif min_pkg_price:
        display = f"¥{int(min_pkg_price):,}起"
    else:
        display = "请见方案详情"

    return {
        "monthly_price": monthly_price,
        "min_package_price": min_pkg_price,
        "package_count": pkg_count,
        "display_text": display,
    }


def fill_pitch_placeholders(pitch_text: str, quote_id: int) -> str:
    """替换话术模板里的占位 → B.8 月费填实

    模板可能含:
      - "具体月费见方案" / "{{monthly_price}}" / "[月费]" 等
    替换为真实 monthly_price 或 min_package_price 显示文本

    向后兼容:输入无占位则原样返回
    """
    if not pitch_text:
        return pitch_text
    monthly = get_quote_monthly_price_ssot(quote_id)
    display = monthly.get("display_text") or "请见方案详情"

    # 几种常见占位形式
    replacements = [
        ("{{monthly_price}}", display),
        ("{{月费}}", display),
        ("[月费]", display),
        ("具体月费见方案", display),
        ("具体月费请见方案", display),
        ("月费见方案", display),
    ]
    out = pitch_text
    for old, new in replacements:
        out = out.replace(old, new)
    return out
