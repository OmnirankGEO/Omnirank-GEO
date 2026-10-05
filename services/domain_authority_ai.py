"""
SAP 域名权威等级 AI 判定器（只服务诊断报告 Source Authority Pack）

背景:原 `classify_sap_tier` 对硬编码清单外的域名一律打成 tier3_small_media_wemedia
（普通网站/自媒体）。已知权威域名只有 ~24 个、永远填不完 → 客户合法的行业站/正规媒体
被误判成"普通网站",权威背书虚低（截图里"权威媒体 0"就是这么来的）。

本模块:
- 已知域名走硬编码"快速通道"（免 LLM、准、稳）——这是老板拍板保留的种子清单。
- 清单外"未知"域名（原本全被打成 tier3 的那批）交 AI 判等级。
- 结果持久化到 `domain_authority_cache`（同一域名永远同一等级 · 报告间不闪烁 · 省 LLM 成本，
  借鉴价格锁全局稳定性思路）。
- AI / DB 任意失败 → 未知域名一律回落 tier3_small_media_wemedia（= 现行行为），绝不抛断报告。

🔴 边界（与 PRICING/MONITORING 物理隔离）:
- 只服务 SAP（被 `source_authority_analyzer.aggregate_source_authority` 调用）。
- **不碰** 算价(`pricing_auditor`)/ 监测(`round_runner`) 共用的
  `tools.keyword_value_scorer._classify_domain_authority` 与
  `services.research_monitor.domain_tiering.get_domain_tier`（只读复用其结果，不改其行为）。
- endorsement_score 仍是【独立解释层】,绝不进 5 维评分 SSOT 主分。
- 不向用户暴露模型/供应商名（仅内部日志）。
"""
from __future__ import annotations
from config.deepseek_models import DEEPSEEK_OFFICIAL_FLASH

import json
import logging
import os
from typing import Optional

logger = logging.getLogger("GEO-DomainAuthorityAI")

# ========== SAP 五档（与 source_authority_analyzer.TIER_LABELS 对齐）==========
_VALID_TIERS = frozenset({
    "tier1_national",
    "structured_encyclopedia",
    "tier2_portal_vertical",
    "tier3_small_media_wemedia",
    "risk_low_quality",
})
_DEFAULT_TIER = "tier3_small_media_wemedia"  # 未知/失败一律回落（= 现行行为，保守不拔高）

# 结构化百科源（与 source_authority_analyzer._ENCYCLOPEDIA_DOMAINS 一致 · 故意小副本以解耦避免循环 import）
_ENCYCLOPEDIA_DOMAINS = ("baike.baidu.com", "baike.so.com", "wikipedia.org")

# 单次最多送 AI 判定的未知域名数（控 prompt 体积/成本；超出的回落默认 tier，下次再判）
_MAX_AI_DOMAINS = 50
_AI_TIMEOUT_SECONDS = 20.0
# 老板默认模型(2026-06-04):DeepSeek 官方 Flash 档 · 不用 qwen-turbo。
# 🔴 原注释写「deepseek-chat 别名」—— 那句话 2026-09-14 官方改名 deepseek-flash(同一档);名字统一由 config/deepseek_models 出 之后不再成立:
#    Deploy 206-d2 实打 deepseek-chat 已从官方定价页消失、调用回显 deepseek-flash。
#    别再拿注释当事实,名字看常量。
_AI_MODEL = DEEPSEEK_OFFICIAL_FLASH
_DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"

# 复用现有低层分级（惰性导入，避免 import 期重依赖；只读其结果，不改其行为）
_classify_domain_authority = None
_get_domain_tier = None


def _load_classifiers():
    global _classify_domain_authority, _get_domain_tier
    if _classify_domain_authority is None:
        from tools.keyword_value_scorer import _classify_domain_authority as cda
        _classify_domain_authority = cda
    if _get_domain_tier is None:
        from services.research_monitor.domain_tiering import get_domain_tier as gdt
        _get_domain_tier = gdt
    return _classify_domain_authority, _get_domain_tier


def seed_tier(domain: str) -> Optional[str]:
    """
    硬编码"快速通道"。复用现有 S/A/B/C/social/D + 百科 + 黑名单判定:
    - 百科 → structured_encyclopedia
    - S → tier1_national / A·B → tier2_portal_vertical / C·social → tier3
    - D 且黑名单（短链/低质）→ risk_low_quality
    - **D 且非黑名单（真正不认识的域名）→ None**  ← 这批原本全被打成 tier3，现在交 AI 判
    返回 None 表示"清单外未知，需 AI 判"。
    """
    if not domain:
        return _DEFAULT_TIER
    nd = domain.lower()
    for enc in _ENCYCLOPEDIA_DOMAINS:
        if enc in nd:
            return "structured_encyclopedia"
    classify, domain_tier = _load_classifiers()
    cat = classify(domain)        # S/A/B/C/social/D
    block = domain_tier(domain)   # whitelist/gray/blacklist
    if cat == "S":
        return "tier1_national"
    if cat in ("A", "B"):
        return "tier2_portal_vertical"
    if cat in ("C", "social"):
        return "tier3_small_media_wemedia"
    # cat == 'D'（低层未识别）
    if block == "blacklist":
        return "risk_low_quality"
    return None  # D + gray → 未知 → AI


# ========== 持久缓存（domain_authority_cache）==========
def _get_conn():
    """惰性取 DB 连接(抽成函数便于单测 monkeypatch)。"""
    from db.distillation_db import get_connection
    return get_connection()


def _ensure_cache_table(cur) -> None:
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS domain_authority_cache (
            domain     TEXT PRIMARY KEY,
            tier       TEXT NOT NULL,
            reason     TEXT,
            source     TEXT DEFAULT 'ai',
            updated_at TIMESTAMP DEFAULT NOW()
        )
        """
    )


def _row_get(row, key, idx):
    if row is None:
        return None
    if isinstance(row, dict):
        return row.get(key)
    try:
        return row[idx]
    except (IndexError, KeyError, TypeError):
        return None


def _get_cached(cur, domains: list[str]) -> dict[str, str]:
    if not domains:
        return {}
    cur.execute(
        "SELECT domain, tier FROM domain_authority_cache WHERE domain = ANY(%s)",
        (list(domains),),
    )
    out: dict[str, str] = {}
    for row in cur.fetchall() or []:
        d = _row_get(row, "domain", 0)
        t = _row_get(row, "tier", 1)
        if d and t in _VALID_TIERS:
            out[d] = t
    return out


def _upsert_cached(cur, results: dict[str, str], source: str = "ai") -> None:
    for domain, tier in results.items():
        if not domain or tier not in _VALID_TIERS:
            continue
        cur.execute(
            """
            INSERT INTO domain_authority_cache (domain, tier, source, updated_at)
            VALUES (%s, %s, %s, NOW())
            ON CONFLICT (domain) DO UPDATE SET
                tier = EXCLUDED.tier, source = EXCLUDED.source, updated_at = NOW()
            """,
            (domain, tier, source),
        )


# ========== AI 判定 ==========
_AI_PROMPT = """你是中文互联网域名权威等级评估助手。给你一批网站域名,判断每个域名作为"AI 引用信源"的权威等级。

只能输出下面 5 个英文 key 之一:
- tier1_national: 国家级/中央媒体/政府/权威主流大媒体(如 人民网、新华网、央视、各部委 gov 站、知名全国性大报)
- structured_encyclopedia: 百科类结构化词条站
- tier2_portal_vertical: 头部门户,或在其领域广为人知、有编辑把关的正规行业/财经/科技媒体
- tier3_small_media_wemedia: 普通网站、企业官网、个人博客、自媒体平台账号、不太知名的小站
- risk_low_quality: 短链、采集站、明显低质或不可信来源

判断规则(重要):
1. 只有你确实认得、且确属权威/知名机构的域名,才给 tier1 或 tier2;拿不准一律给 tier3(宁可保守,绝不拔高)。
2. 完全不认识的陌生域名 → tier3。
3. 只看域名本身的机构性质,不臆测它的具体内容。

域名列表:
{domains}

只输出严格 JSON(不要 markdown 代码块),key 是域名,value 是上面 5 个等级 key 之一。例如:
{{"some-portal.com": "tier2_portal_vertical", "xxx-blog.cn": "tier3_small_media_wemedia"}}"""


def _parse_ai_json(content: str) -> dict[str, str]:
    """容错解析 LLM 返回的 JSON(去 markdown 围栏 → json → json_repair 兜底)。"""
    if not content:
        return {}
    s = content.strip()
    if s.startswith("```"):
        # 去 ```json ... ``` 围栏
        s = s.split("```", 2)[1] if s.count("```") >= 2 else s.strip("`")
        if s.lstrip().lower().startswith("json"):
            s = s.lstrip()[4:]
    obj = None
    try:
        obj = json.loads(s)
    except Exception:
        try:
            from json_repair import repair_json
            obj = repair_json(s, return_objects=True)
        except Exception:
            return {}
    if not isinstance(obj, dict):
        return {}
    out: dict[str, str] = {}
    for k, v in obj.items():
        if isinstance(k, str) and isinstance(v, str) and v in _VALID_TIERS:
            out[k.strip().lower()] = v
    return out


def _deepseek_key() -> str:
    """取 DeepSeek key(优先 key 池轮询 · 回落 env)· 复用 model_config._deepseek_api_key 同款逻辑。"""
    try:
        from services.llm.deepseek_key_pool import pick_deepseek_api_key
        k = pick_deepseek_api_key("realtime")
        if k:
            return k
    except Exception:
        pass
    return os.getenv("DEEPSEEK_API_KEY", "").strip()


def _ai_classify(domains: list[str]) -> dict[str, str]:
    """同步调 DeepSeek V4 Flash(deepseek-chat)判一批未知域名 → {domain: tier}。失败抛异常由上层降级。"""
    api_key = _deepseek_key()
    if not api_key:
        logger.warning("[domain_authority_ai] DEEPSEEK_API_KEY 未设置 · 跳过 AI 判定降级")
        return {}
    import httpx

    prompt = _AI_PROMPT.format(domains="\n".join(domains))
    payload = {
        "model": _AI_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1,  # 低温稳定(同价格锁稳定性思路 · 同域名跨报告不闪烁)
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    with httpx.Client(timeout=_AI_TIMEOUT_SECONDS) as client:
        resp = client.post(_DEEPSEEK_URL, headers=headers, json=payload)
        resp.raise_for_status()
        data = resp.json()
    content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
    parsed = _parse_ai_json(content)
    # 只保留本次确实问的域名(防 LLM 多吐)
    return {d: parsed[d] for d in domains if d in parsed}


# ========== 主入口 ==========
def classify_domains_ai(domains) -> dict[str, str]:
    """
    给一批域名返回 {domain: SAP_tier}。
    已知域名走硬编码快通;未知域名查缓存→AI 判→写缓存;任意环节失败回落默认 tier(不抛)。
    """
    # 归一化 + 去重(保序无所谓,字典)
    uniq: list[str] = []
    seen = set()
    for d in domains or []:
        if not d or not isinstance(d, str):
            continue
        nd = d.strip().lower()
        if nd and nd not in seen:
            seen.add(nd)
            uniq.append(nd)
    if not uniq:
        return {}

    out: dict[str, str] = {}
    unknown: list[str] = []
    for d in uniq:
        t = seed_tier(d)
        if t is not None:
            out[d] = t          # 硬编码快通命中
        else:
            unknown.append(d)   # 清单外未知 → 走缓存/AI
    if not unknown:
        return out

    # 缓存查询(失败不致命)
    conn = None
    cached: dict[str, str] = {}
    try:
        conn = _get_conn()
        cur = conn.cursor()
        _ensure_cache_table(cur)
        cached = _get_cached(cur, unknown)
        conn.commit()
    except Exception as e:
        logger.warning("[domain_authority_ai] 缓存读取失败,降级直判: %s", e)
    out.update(cached)

    need_ai = [d for d in unknown if d not in cached]
    if need_ai:
        send = need_ai[:_MAX_AI_DOMAINS]
        if len(need_ai) > _MAX_AI_DOMAINS:
            logger.info("[domain_authority_ai] 未知域名 %d 个 · 本次只判前 %d 个,余回落默认",
                        len(need_ai), _MAX_AI_DOMAINS)
        ai_results: dict[str, str] = {}
        try:
            ai_results = _ai_classify(send)
        except Exception as e:
            logger.warning("[domain_authority_ai] AI 判定失败,未知域名回落 %s: %s", _DEFAULT_TIER, e)
        # 写回(只缓存确判到的;失败/未判的不缓存,下次再试)
        if conn is not None and ai_results:
            try:
                cur = conn.cursor()
                _upsert_cached(cur, ai_results, source="ai")
                conn.commit()
            except Exception as e:
                logger.warning("[domain_authority_ai] 缓存写入失败(不影响本次结果): %s", e)
        for d in need_ai:
            out[d] = ai_results.get(d) or _DEFAULT_TIER  # 未判到 → 回落(= 现行行为)

    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
    return out
