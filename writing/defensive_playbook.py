"""#185 c3 · 防御型写法(playbook)——「蒸馏研究」的产物与取用。

Owner 2026-09-13 原话:「防御型公司词的标题生成也需要经过 AI,
  要蒸馏一下研究一下防御型的这些文章怎么写。」

产物 = 一份按品牌持久化的 JSON:研究「X 怎么样 / 靠谱吗 / 投诉 / 差异 /
资质 / 适合谁 / 价格 / 团队」这类**公司词问法**下,AI 引擎会引用什么样的
标题与正文。标题侧与正文侧**都读它**,注入点各只有一处。

═══════════════════════════════════════════════════════════════════════
🔴 `sources_used` 是这份文件里最要紧的一段,原因不是它好看:

   工单列了四类素材 (a) 客户事实 (b) 竞品与行业 (c) 已被引用样本 (d) LLM 自身分析,
   并写明「代码里打印 `sources_used`,**不许假装有**」。

   逐表核过。**权威读数 = Deploy 2026-09-14 的生产只读**
   (`geo_agentscope/public`;Deploy 自证:编造表列数 0 / 编造列 0 / 正样本 brands 33 列):
     · `geo_article_citation_attributions` —— **存在,27 列,296 行**(2026-09-14 时点),
       `brand_id integer` / `question text` / `question_family text` ⇒
       **能**按品牌 + 公司词问法取样。🔴 三列**契约上都可空**,当时那 296 行恰好全非空 ——
       「现在的数据长这样」不是「这一列不会是 NULL」,取样 SQL 因此显式写 IS NOT NULL。
     · `geo_answer_adoption_metrics` —— 19 列里**没有 brand_id、没有 question**,
       只有 `source_url` / `prompt_id` / `industry_key`。
     · `geo_recommendation_outcomes` —— 6 列,只有 `scope_key varchar` 与
       `outcome_payload jsonb`,**同样没有品牌与问题字段**。

   🔴 为什么不引我自己那份私库读数:私库是夹具用 `prod_schema_2026-09-05.sql`
      **恢复**出来的,且 conftest 用 `ON_ERROR_STOP=0`(部分对象恢复失败也算建成功)。
      它能证的是「这段 SQL 跑得通」,证不了「生产长这样」——
      本仓那条 `verified-correctly-against-the-wrong-referent`。两件事分开说。

   ⇒ 工单点名的三张表,**只有一张能回答「这家品牌的公司词问法被引用过什么」**。
     另两张不是"今天为 0",是**结构上答不了这个问题**。

🔴 所以 `sources_used` 把这两件事**分开记**:
     `{"cited_samples": {"count": N, "table": "..."},
       "cited_samples_unavailable": {"geo_answer_adoption_metrics": "无 brand_id/question 列", ...}}`
   「今天为 0」会随时间变,「这张表答不了」不会。混成一个 `0`,
   下一个人会一直等它长出来 —— 而它永远不会长出来。
═══════════════════════════════════════════════════════════════════════
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Final, List, Optional, Tuple

logger = logging.getLogger("GEO-DefensivePlaybook")

#: 换 prompt / 换产物结构**必须**换它 —— 否则"这条角度是哪一版蒸出来的"查不清。
PLAYBOOK_VERSION: Final[str] = "v1-2026-09-14"

#: TTL(天)。Owner 口径 7 天;知识库一变也失效(见 `kb_fingerprint`)。
PLAYBOOK_TTL_DAYS: Final[int] = 7

#: 🔴 结构上答不了「这家品牌的公司词问法被引用过什么」的表,连同**为什么**。
#:    列在这里而不是悄悄跳过:下一个人会照着工单去找这两张表,
#:    找不到列的时候他需要知道这不是他读错了。
CITED_SAMPLE_TABLES_UNAVAILABLE: Final[Dict[str, str]] = {
    "geo_answer_adoption_metrics":
        "19 列里没有 brand_id、没有 question(只有 source_url / prompt_id / industry_key)"
        " —— 按品牌 + 公司词问法取样在结构上不成立",
    "geo_recommendation_outcomes":
        "6 列:id / prediction_id / scope_key / outcome_payload / observed_at /"
        " measurement_window —— 同样没有品牌与问题字段",
}

#: 能取样的那张表。
CITED_SAMPLE_TABLE: Final[str] = "geo_article_citation_attributions"

#: 防御方向的 `user_choice` 取值。**这一个名字定在这里**:
#: 正文侧判"是不是防御篇"要用它,端点侧判也要用它 —— 两处各写一个字面串,
#: 改名那天只会改掉其中一处,而另一处静默不再命中。
DEFENSIVE_USER_CHOICE: Final[str] = "defensive_company"

#: 正文提示块的**唯一**标题行。判据按它断言"注入了/没注入",
#: 所以它必须是一个别处不会出现的串。
DEFENSIVE_BODY_HEADING: Final[str] = "【防御型(公司词)正文写法】"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def kb_fingerprint(facts: Dict[str, Any]) -> str:
    """品牌事实的指纹。事实一变 ⇒ 指纹变 ⇒ 旧 playbook 失效。

    🔴 用指纹不用 `updated_at` 比大小:`client_profiles` 的 updated_at
       会被**与内容无关**的写入推前(整行 UPDATE 常带上它),那样每次都失效,
       TTL 就形同虚设;反过来,只改一个字段而没碰 updated_at 的路径也存在。
       指纹只看**值**。
    """
    payload = json.dumps({k: facts.get(k) for k in sorted(facts or {})},
                         ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


# ─────────────────────────────────────────────────────────────
# 取素材
# ─────────────────────────────────────────────────────────────
def load_brand_facts(conn, brand_id: Optional[int]) -> Dict[str, Any]:
    """这家品牌的**事实列**(缺事实提示 + playbook 指纹共用的那一份)。

    🔴 单点:标题侧(缺事实提示 / 蒸 playbook)与正文侧(取 playbook)
       必须读**同一份**事实,否则两边各自算出的 `kb_fingerprint` 不一样 ——
       表现是"标题刚蒸好的 playbook,正文这边永远缓存不命中",
       而两边各自看都合理。
    🔴 永不抛:取不到就当全缺(提示多说一句比少说一句安全,反正不阻断)。
    """
    from writing.defensive_questions import QUESTION_FACT_FIELDS

    cols = sorted({f for fs in QUESTION_FACT_FIELDS.values() for f in fs})
    if not brand_id or not cols:
        return {}
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT %s FROM client_profiles "
            "WHERE brand_id = %%s AND COALESCE(is_deleted, 0) = 0 "
            "ORDER BY updated_at DESC NULLS LAST LIMIT 1" % ", ".join(cols),
            (int(brand_id),))
        return dict(cur.fetchone() or {})
    except Exception as exc:                       # noqa: BLE001
        logger.warning("[#185 playbook] 取品牌事实失败(按全缺处理): %r", exc)
        _rollback_quietly(conn)
        return {}


def render_defensive_body_prompt(brand_id: Optional[int], user_choice: object,
                                 conn=None) -> str:
    """[T6] 正文侧**唯一**读 playbook 的地方。非防御篇返空串。

    🔴 单点:正文只有这一处读它。多一处就会出现「标题按这一版的角度写、
       正文按另一处拼的规则写」,而两边各自都是绿的。
    🔴 **只读不蒸**:正文阶段不打蒸馏 LLM。标题阶段已经蒸过并落库;
       正文再蒸一次,同一篇文章的标题与正文会依据两个不同版本的写法,
       而 `version` 列只记得下后蒸的那一版 —— 事后查不出来。
       取不到就返空串:正文照写,只是少一份写法参考。
    🔴 永不抛。
    """
    if str(user_choice or "") != DEFENSIVE_USER_CHOICE:
        return ""
    own_conn = False
    try:
        if conn is None:
            from db.connection import get_connection
            conn = get_connection()
            own_conn = True
        facts = load_brand_facts(conn, brand_id)
        row = load_live_playbook(conn, brand_id, kb_fingerprint(facts))
    except Exception as exc:                       # noqa: BLE001
        logger.warning("[#185 playbook] 正文取 playbook 失败(正文照写): %r", exc)
        return ""
    finally:
        if own_conn and conn is not None:
            try:
                conn.close()
            except Exception:                      # noqa: BLE001
                pass
    if not row:
        return ""
    payload = row.get("payload") or {}
    body_points = [str(x) for x in (payload.get("body_points") or []) if str(x).strip()]
    evidence_rules = [str(x) for x in (payload.get("evidence_rules") or []) if str(x).strip()]
    forbidden = [str(x) for x in (payload.get("forbidden") or []) if str(x).strip()]
    if not (body_points or evidence_rules or forbidden):
        return ""
    parts = ["", "", DEFENSIVE_BODY_HEADING,
             "(本品牌防御型写法 %s · 只作写作参考,事实以素材为准)" % row.get("version")]
    if body_points:
        parts.append("· 正文要覆盖:" + " / ".join(body_points[:8]))
    if evidence_rules:
        parts.append("· 举证方式:" + " / ".join(evidence_rules[:6]))
    if forbidden:
        parts.append("· 禁止:" + " / ".join(forbidden[:6]) + " / 不点名同行、不排名")
    return "\n".join(parts)


def _rollback_quietly(conn) -> None:
    """吞掉查询异常之后**必须**把事务回滚掉。

    🔴 PostgreSQL 一条语句报错就把整个事务置为 aborted:之后同一条连接上的
       每一句都返 `InFailedSqlTransaction`。于是"fail-soft 只是少一类素材"
       会变成"这条连接上后面每一次取数都失败" —— 而每一处都各自 `warning`
       一行"不阻断",日志看起来全是良性降级,实际素材整批为空。
       (本仓 2026-09-14 实测:`geo_defensive_playbooks` 缺表一次,
        后面 `collect_cited_samples` 与 `save_playbook` 跟着全废。)
    """
    try:
        conn.rollback()
    except Exception:                              # noqa: BLE001
        pass


def collect_cited_samples(conn, brand_id: Optional[int], question_tokens: Tuple[str, ...],
                          limit: int = 40) -> Tuple[List[dict], Dict[str, Any]]:
    """(c) 已被引用样本。返回 `(样本, 计数与不可用说明)`。

    🔴 按 `question` **文本**匹配八问关键词,不按 `question_family` 取值猜:
       那一列的取值域由监测侧写(默认 `legacy_unknown`),我看不到生产上的实际分布。
       用一个我没核过的取值去过滤,会得到一个**看起来精确**的空集。
    🔴 **永不抛**:取不到样本只是 playbook 素材少一类,不该让标题生成整条断掉。
    """
    used: Dict[str, Any] = {
        "cited_samples": {"count": 0, "table": CITED_SAMPLE_TABLE, "matched_by": "question_text"},
        "cited_samples_unavailable": dict(CITED_SAMPLE_TABLES_UNAVAILABLE),
    }
    if not brand_id:
        used["cited_samples"]["skipped_reason"] = "没有 brand_id"
        return [], used
    try:
        cur = conn.cursor()
        # 🔴 三列在**契约上都可空**(Deploy 2026-09-14 生产只读:brand_id / question /
        #    question_family 均 is_nullable=YES;当时那 296 行恰好全非空 ——
        #    「现在的数据长这样」不是「这一列不会是 NULL」)。
        #    所以显式写 `question IS NOT NULL`,不靠当下的数据分布替我把关。
        cur.execute(
            "SELECT question, question_family, style_family, publish_domain, target_outcome "
            "  FROM %s "
            " WHERE brand_id = %%s AND question IS NOT NULL AND question <> '' "
            " ORDER BY tested_at DESC LIMIT %%s" % CITED_SAMPLE_TABLE,
            (int(brand_id), int(limit)))
        rows = [dict(r) for r in (cur.fetchall() or [])]
    except Exception as exc:                       # noqa: BLE001
        logger.warning("[#185 playbook] 取已引用样本失败(不阻断): %r", exc)
        used["cited_samples"]["error"] = str(exc)[:200]
        _rollback_quietly(conn)
        return [], used

    hit = [r for r in rows
           if any(tok and tok in str(r.get("question") or "") for tok in question_tokens)]
    used["cited_samples"]["count"] = len(hit)
    used["cited_samples"]["scanned"] = len(rows)
    # 🔴 顺手记下 `question_family` 的**取值分布**(只计数,不用来过滤)。
    #    今天不能按它过滤 —— 它由监测侧写、默认 `legacy_unknown`,取值域我核不到;
    #    但把分布记下来,将来它有实际取值时,才有依据从"按 question 文本匹配"切过去。
    #    **NULL 单独算一档**:它与「值是空串」「值是 legacy_unknown」是三件事,
    #    合并之后就再也分不出"没写"和"写了个占位"。
    dist: Dict[str, int] = {}
    for r in rows:
        fam = r.get("question_family")
        key = "__NULL__" if fam is None else str(fam)
        dist[key] = dist.get(key, 0) + 1
    used["cited_samples"]["question_family_distribution"] = dist
    return hit, used


def collect_sources(conn, brand_id: Optional[int], facts: Dict[str, Any],
                    competitors: List[str],
                    question_tokens: Tuple[str, ...]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """把四类素材收齐,连同**逐项计数**一起返回。

    🔴 计数与查询要**对得上**(判据 T8 钉这一条):`client_profile_facts` 记的是
       "非空的事实列有几个",不是"这张表有几列" —— 后者恒定,前者才说明
       这家客户到底给了多少料。
    """
    non_empty = {k: v for k, v in (facts or {}).items()
                 if v not in (None, "", [], {})}
    samples, used = collect_cited_samples(conn, brand_id, question_tokens)
    used["client_profile_facts"] = len(non_empty)
    used["client_profile_fields_queried"] = len(facts or {})
    used["competitors"] = len(competitors or [])
    used["llm_self_analysis"] = 1          # (d) 一定有:LLM 自己那一份
    material = {
        "facts": non_empty,
        "competitors": list(competitors or [])[:10],
        "cited_samples": samples[:20],
    }
    return material, used


# ─────────────────────────────────────────────────────────────
# 持久化 / 取用
# ─────────────────────────────────────────────────────────────
def load_live_playbook(conn, brand_id: Optional[int], fingerprint: str) -> Optional[dict]:
    """取这家品牌**还没过期且指纹没变**的最新一版;没有就 None。

    🔴 过期行**不删**(留痕可回看"上一版是什么、用什么素材蒸的"),只是不取用。
    """
    if not brand_id:
        return None
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, version, payload, sources_used, kb_fingerprint, created_at, expires_at "
            "  FROM geo_defensive_playbooks "
            " WHERE brand_id = %s AND expires_at > now() "
            " ORDER BY id DESC LIMIT 1", (int(brand_id),))
        row = cur.fetchone()
    except Exception as exc:                       # noqa: BLE001
        logger.warning("[#185 playbook] 读 playbook 失败(按没有处理): %r", exc)
        _rollback_quietly(conn)
        return None
    if not row:
        return None
    row = dict(row)
    if str(row.get("kb_fingerprint") or "") != str(fingerprint or ""):
        # 知识库变了 ⇒ 这一版作废(行留着)。
        return None
    if str(row.get("version") or "") != PLAYBOOK_VERSION:
        # prompt / 结构换代 ⇒ 旧版不取用。
        return None
    return row


def save_playbook(conn, brand_id: int, payload: dict, sources_used: dict,
                  fingerprint: str) -> Optional[int]:
    """写一版。失败只告警,不抛 —— 缓存写不进去不该让标题生成断掉。"""
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO geo_defensive_playbooks "
            "       (brand_id, version, payload, sources_used, kb_fingerprint, expires_at) "
            "VALUES (%s, %s, %s::jsonb, %s::jsonb, %s, now() + make_interval(days => %s)) "
            "RETURNING id",
            (int(brand_id), PLAYBOOK_VERSION,
             json.dumps(payload, ensure_ascii=False),
             json.dumps(sources_used, ensure_ascii=False),
             str(fingerprint or ""), PLAYBOOK_TTL_DAYS))
        row = cur.fetchone()
        conn.commit()
        return int(dict(row)["id"]) if row else None
    except Exception as exc:                       # noqa: BLE001
        logger.warning("[#185 playbook] 写 playbook 失败(不阻断): %r", exc)
        try:
            conn.rollback()
        except Exception:                          # noqa: BLE001
            pass
        return None


def empty_playbook_reason(reason: str) -> Dict[str, Any]:
    """没有 playbook 时回给前端的形状 —— **恒有 reason**,不许静默。

    工单原话:「缺 playbook 时 reason 写明,前端要显示,不许静默」。
    """
    return {"version": None, "reason": str(reason or "unknown")}


# ─────────────────────────────────────────────────────────────
# 取用入口(标题侧与正文侧都调它)
# ─────────────────────────────────────────────────────────────
def _distill_with_llm(brand_name: str, industry: str, material: Dict[str, Any]) -> dict:
    """调 LLM 蒸出「防御型写法」。**只被 `get_or_build_playbook` 调**。

    抽成独立函数是为了给判据一个**可替换的缝**:T1/T5/T7 都要在不打真 LLM 的
    情况下断言"打没打、打了几次、打挂了怎么办"。
    """
    import httpx

    from writing.llm_utils import get_llm_config

    api_url, api_key, model, _provider = get_llm_config("topic_generation", module="writing")
    prompt = (
        "你在研究一类**公司词问法**下 AI 搜索引擎会引用什么样的内容。\n"
        "问法是这八类:怎么样 / 靠谱吗·口碑 / 投诉·售后 / 与同类的差异(不点名不排名)/ "
        "资质·案例 / 适合谁·不适合谁 / 价格·收费 / 团队·流程。\n\n"
        "品牌:%s(行业:%s)\n素材:%s\n\n"
        "只返回 JSON,形如 "
        '{"title_angles": {"问名": ["角度", ...]}, "body_points": [...], '
        '"evidence_rules": [...], "forbidden": [...]}。\n'
        "硬要求:不点名同行、不排名、不编造没有素材支撑的事实。"
        % (brand_name, industry, json.dumps(material, ensure_ascii=False)[:4000])
    )
    # 🔴 [#185 c3'] 这一次调用**必须进 `llm_call_log`**。
    #    它是每品牌每 7 天一次的真实成本;不进账的话,成本表上这条线永远是 0,
    #    而 0 与"没人用这个功能"长得一模一样 —— 等到有人问"防御型贵不贵",
    #    能查到的只有一句"查不到"。
    #    用 `llm_track_sync`(本函数是同步的);它在**抛异常时也会写一行**
    #    (success=False),所以蒸馏失败同样留痕,不会表现成"这次没调过"。
    from tools.llm_call_tracker import llm_track_sync, usage_from_response_payload

    with llm_track_sync(caller="defensive_playbook_distill",
                        platform=_provider, model=model) as _t:
        resp = httpx.post(
            api_url,
            headers={"Authorization": "Bearer %s" % api_key,
                     "Content-Type": "application/json"},
            json={"model": model,
                  "messages": [{"role": "user", "content": prompt}],
                  "temperature": 0.3,
                  "response_format": {"type": "json_object"}},
            timeout=90.0)
        resp.raise_for_status()
        payload = resp.json()
        _in, _out, _cached = usage_from_response_payload(payload)
        # 🔴 回显的 model 覆盖请求的 model:供应商静默换模型时,
        #    账上记的必须是**真正跑的那个**,不是我们要的那个。
        _t.record(input_tokens=_in, output_tokens=_out, cached_tokens=_cached,
                  model=str(payload.get("model") or model))
        content = (payload.get("choices") or [{}])[0].get("message", {}).get("content", "")
        data = json.loads(content)
        if not isinstance(data, dict):
            raise ValueError("蒸馏产物不是对象")
        return data


def get_or_build_playbook(conn, brand_id: Optional[int], brand_name: str, industry: str,
                          facts: Dict[str, Any], competitors: List[str],
                          question_tokens: Tuple[str, ...],
                          distill_fn=None) -> Tuple[Optional[dict], str]:
    """取(或蒸)这家品牌的防御型写法。返回 `(playbook_row_or_None, reason)`。

    🔴 **fail-soft**(工单 T7):蒸不出来只是少一份角度参考,
       标题该出还得出(落模板题)。所以本函数**永不抛**,
       但**必须给出 reason** —— 静默 None 会让前端显示成"一切正常"。
    🔴 命中缓存时**不打 LLM**(T5):同品牌 7 天内第二次调用要走这条路。
    """
    fingerprint = kb_fingerprint(facts)
    live = load_live_playbook(conn, brand_id, fingerprint)
    if live:
        return live, "cache_hit"
    if not brand_id:
        return None, "no_brand_id"

    material, used = collect_sources(conn, brand_id, facts, competitors, question_tokens)
    fn = distill_fn or _distill_with_llm
    try:
        payload = fn(brand_name, industry, material)
    except Exception as exc:                       # noqa: BLE001
        logger.warning("[#185 playbook] 蒸馏失败(标题仍出,落模板): %r", exc)
        return None, "distill_failed: %s" % (str(exc)[:120],)

    new_id = save_playbook(conn, int(brand_id), payload, used, fingerprint)
    row = {"id": new_id, "version": PLAYBOOK_VERSION, "payload": payload,
           "sources_used": used, "kb_fingerprint": fingerprint}
    return row, "built" if new_id else "built_not_persisted"
