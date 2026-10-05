"""媒体域名人话目录 · 工单 WO_MEDIA_BOARD_UX_CLOSURE_2026-08-05 §1。

问题(Owner 两轮截图):发布投放媒体榜上 `licai.cofool.com` / `05wang.com` /
`chinapp.com` / `news.koolearn.com` 这类**裸域名直出**;点击拿裸域名去中文名索引的
媒体库里搜必然空结果 = 死点击。根因是两份写死映射表(后端 `_DOMAIN_DISPLAY_NAMES`
40 条 + 前端 `GENERAL_MEDIA_LABELS` 13 条)盖不住长尾。

本模块 = 域名 → 中文名 + 一句话简介 的**落库缓存 SSOT**(表 `media_domain_directory`),
LLM 蒸馏填充,出榜时 join。

🔴 AI 辅助四件套(`feedback_ai_assist_by_default_rule`):
  - **留痕**:每笔真实 LLM 调用经 `llm_track(caller='media_domain_directory')` 记账;
    落库行带 `source='llm'` + `model` + `attempts` + `last_attempt_at`。
  - **幂等**:`ON CONFLICT (domain) DO UPDATE`,同一域名跑多少次都只有一行;
    `source='admin'` 的人工订正行**蒸馏永不覆盖**。
  - **不阻断主链**:出榜路径只做一次只读 join;缺名的域名丢进**后台守护线程**蒸馏,
    本次请求照常返回(这一轮仍显示域名,下一次出榜就有中文名)。
  - **fail-soft**:表不存在 / 查询失败 / 无 key / LLM 挂 → 一律返回空映射,
    榜完全退化为当前行为(显示裸域名),绝不抛给调用方。

🔴 负缓存:蒸馏跑过但没能确定的域名落 `zh_name=''` + `attempts+1`。读侧只认非空
   `zh_name`;`attempts >= _MAX_ATTEMPTS` 的空行不再重排队 —— 否则每次出榜都对同一批
   查不出来的长尾域名重复烧 LLM。
"""

from __future__ import annotations
from config.deepseek_models import DEEPSEEK_OFFICIAL_FLASH

import json
import logging
import os
import re
import threading
from typing import Any, Final

logger = logging.getLogger("GEO-MediaDomainDirectory")

_TRUE: Final = frozenset({"1", "true", "yes", "on"})

#: 蒸馏总闸(默认开 · `feedback_ai_assist_by_default_rule`:AI 辅助默认化)。
#: 关掉只停"写"(不再调 LLM),**读侧 join 照常** —— 已落库的中文名不会因为关闸而消失。
def is_distill_enabled() -> bool:
    return os.getenv("MEDIA_DOMAIN_DISTILL_ENABLED", "true").strip().lower() in _TRUE


#: 官方 DeepSeek Flash 档直连(`feedback_default_ai_model_deepseek_v4_flash`:默认模型;
#: 禁代理版)。展示层文案,分币级成本。
#: 🔴 记忆里那条 feedback 的 slug 仍写着 v4_flash —— 2026-09-14 官方改名 deepseek-flash(同一档);名字统一由 config/deepseek_models 出,档位没变、名字变了。
DISTILL_MODEL: Final = DEEPSEEK_OFFICIAL_FLASH
DISTILL_TRACK_NAME: Final = "media_domain_directory"

#: 一次蒸馏最多喂多少个域名(prompt 体积可控 + 单次失败的爆炸半径可控)。
_MAX_BATCH: Final = 12
#: 同一域名最多蒸馏几次(空 zh_name 的负缓存到达此值即不再重试)。
_MAX_ATTEMPTS: Final = 3
#: 同时最多几个后台蒸馏任务(单 worker 进程,避免出榜高频触发时线程堆积)。
_MAX_INFLIGHT_JOBS: Final = 2

_lock = threading.Lock()
#: 正在蒸馏中的域名(进程内去重):防止连续几次出榜对同一批域名重复排队。
_inflight_domains: set[str] = set()
_inflight_jobs = 0


def normalize_directory_domain(value: Any) -> str:
    """目录键归一:去协议 / 去路径 / 去 www. / 小写。非域名形态 → ''。

    🔴 **不做逐级剥子域**(与 `media_effectiveness_board._match_domain_map` 的行为
    刻意不同):`news.koolearn.com` 不是 `koolearn.com` 的别名,拿父域名的中文名套
    子站就是编造。查不到就是查不到,退回显示域名。
    """
    text = str(value or "").strip().lower()
    if not text:
        return ""
    text = re.sub(r"^[a-z]+://", "", text)
    text = text.split("/")[0].split("?")[0].strip()
    if text.startswith("www."):
        text = text[4:]
    if not re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", text):
        return ""
    return text[:200]


# ============================================================
# 读侧(出榜路径唯一入口)
# ============================================================


def get_directory_entries(domains: Any) -> dict[str, dict[str, str]]:
    """批量取已确定中文名的目录条目。

    返回 `{归一域名: {"zh_name", "one_liner", "source"}}`;**只含非空 zh_name**
    (空串是负缓存,不是名字)。fail-soft:表不存在 / 查询异常 → `{}`。
    """
    keys = _normalized_unique(domains)
    if not keys:
        return {}
    try:
        from db.connection import get_connection
    except Exception as exc:  # pragma: no cover - import 期异常只可能在环境残缺时
        logger.warning("[media-dir] db 层不可用(退化为空目录): %s", str(exc)[:200])
        return {}

    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor()
        # [P1-3 2026-08-14] media_form 随条目带出(COALESCE 空 = 未分档,渲染层
        # fail-closed 不产出形态化署名)。列缺失(迁移未跑)走下方兜底重查。
        try:
            cur.execute(
                "SELECT domain, zh_name, one_liner, source, COALESCE(media_form, '') AS media_form "
                "  FROM media_domain_directory "
                " WHERE domain = ANY(%s) AND zh_name <> ''",
                (keys,),
            )
            rows = cur.fetchall() or []
        except Exception:
            # 迁移未跑(UndefinedColumn):事务已脏,重连按旧列集查 —— 出榜零影响。
            try:
                conn.close()
            except Exception:
                pass
            conn = get_connection()
            cur = conn.cursor()
            cur.execute(
                "SELECT domain, zh_name, one_liner, source "
                "  FROM media_domain_directory "
                " WHERE domain = ANY(%s) AND zh_name <> ''",
                (keys,),
            )
            rows = cur.fetchall() or []
        out: dict[str, dict[str, str]] = {}
        for row in rows:
            dom = str(row["domain"] or "")
            if not dom:
                continue
            out[dom] = {
                "zh_name": str(row["zh_name"] or "").strip(),
                "one_liner": str(row["one_liner"] or "").strip(),
                "source": str(row["source"] or "llm"),
                "media_form": str(row.get("media_form") or "").strip(),
            }
        return out
    except Exception as exc:
        # 表还没建(迁移未跑)也走这里 —— 榜照常出,只是没有中文名。
        logger.warning("[media-dir] 目录查询失败(退化为空目录): %s", str(exc)[:200])
        return {}
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def request_distillation(domains: Any) -> int:
    """把"还没有中文名"的域名丢进后台蒸馏 · **立即返回**,绝不阻断出榜。

    返回真正排进本次后台任务的域名个数(0 = 没有要蒸馏的 / 闸关 / 已有任务在跑)。
    """
    if not is_distill_enabled():
        return 0
    keys = _normalized_unique(domains)
    if not keys:
        return 0
    try:
        pending = _filter_needs_distill(keys)
    except Exception as exc:
        logger.warning("[media-dir] 待蒸馏筛选失败(本次跳过): %s", str(exc)[:200])
        return 0
    if not pending:
        return 0

    global _inflight_jobs
    with _lock:
        if _inflight_jobs >= _MAX_INFLIGHT_JOBS:
            return 0
        batch = [d for d in pending if d not in _inflight_domains][:_MAX_BATCH]
        if not batch:
            return 0
        _inflight_domains.update(batch)
        _inflight_jobs += 1

    def _run() -> None:
        global _inflight_jobs
        try:
            distill_domains_sync(batch)
        except Exception as exc:  # noqa: BLE001 后台任务绝不把异常带出线程
            logger.warning("[media-dir] 后台蒸馏异常(忽略): %s", str(exc)[:200])
        finally:
            with _lock:
                _inflight_domains.difference_update(batch)
                _inflight_jobs -= 1

    threading.Thread(target=_run, name="media-domain-distill", daemon=True).start()
    return len(batch)


def _normalized_unique(domains: Any) -> list[str]:
    if not isinstance(domains, (list, tuple, set, frozenset)):
        return []
    seen: list[str] = []
    dedupe: set[str] = set()
    for d in domains:
        key = normalize_directory_domain(d)
        if key and key not in dedupe:
            dedupe.add(key)
            seen.append(key)
    return seen


def _filter_needs_distill(keys: list[str]) -> list[str]:
    """筛出"库里没有中文名、且负缓存尝试次数没到上限"的域名。"""
    from db.connection import get_connection

    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT domain, zh_name, attempts FROM media_domain_directory WHERE domain = ANY(%s)",
            (keys,),
        )
        known: dict[str, tuple[str, int]] = {}
        for row in cur.fetchall() or []:
            known[str(row["domain"] or "")] = (
                str(row["zh_name"] or "").strip(), int(row["attempts"] or 0),
            )
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    pending: list[str] = []
    for key in keys:
        hit = known.get(key)
        if hit is None:
            pending.append(key)          # 从没蒸馏过
            continue
        zh_name, attempts = hit
        if zh_name:
            continue                     # 已有名字
        if attempts < _MAX_ATTEMPTS:
            pending.append(key)          # 负缓存但还没到重试上限
    return pending


# ============================================================
# 写侧(蒸馏)
# ============================================================


def upsert_entries(entries: list[dict[str, Any]], *, model: str = "") -> int:
    """幂等写入蒸馏结果。返回真正写入的行数;失败返回 0(不抛)。

    🔴 `source='admin'`(人工订正)的行**永不被覆盖** —— WHERE 子句里挡住。
    🔴 空 `zh_name` 写成负缓存行:`attempts+1` 累加,不清掉已有名字。
    """
    rows = [e for e in (entries or []) if isinstance(e, dict) and normalize_directory_domain(e.get("domain"))]
    if not rows:
        return 0
    try:
        from db.connection import get_connection
    except Exception:
        return 0

    conn = None
    written = 0
    try:
        conn = get_connection()
        cur = conn.cursor()
        for e in rows:
            domain = normalize_directory_domain(e.get("domain"))
            zh_name = str(e.get("zh_name") or "").strip()[:200]
            one_liner = str(e.get("one_liner") or "").strip()[:300]
            cur.execute(
                """
                INSERT INTO media_domain_directory
                       (domain, zh_name, one_liner, source, model, attempts, last_attempt_at, updated_at)
                VALUES (%s, %s, %s, 'llm', %s, 1, now(), now())
                ON CONFLICT (domain) DO UPDATE SET
                    zh_name   = CASE WHEN EXCLUDED.zh_name <> '' THEN EXCLUDED.zh_name
                                     ELSE media_domain_directory.zh_name END,
                    one_liner = CASE WHEN EXCLUDED.one_liner <> '' THEN EXCLUDED.one_liner
                                     ELSE media_domain_directory.one_liner END,
                    model     = EXCLUDED.model,
                    attempts  = media_domain_directory.attempts + 1,
                    last_attempt_at = now(),
                    updated_at = now()
                 WHERE media_domain_directory.source <> 'admin'
                """,
                (domain, zh_name, one_liner, str(model or DISTILL_MODEL)[:60]),
            )
            written += 1
        conn.commit()
        return written
    except Exception as exc:
        logger.warning("[media-dir] 目录写入失败(忽略 · 下次出榜会重试): %s", str(exc)[:200])
        try:
            if conn is not None:
                conn.rollback()
        except Exception:
            pass
        return 0
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


_DISTILL_PROMPT: Final = """你是中文媒体站点识别器。下面每行是一个网站域名,请判断它是哪家中文媒体/网站。

要求:
1. 只输出你**确实知道**的站点。不确定的,`zh_name` 必须留空字符串 —— 编一个名字比不给名字更糟。
2. `zh_name` 用该站点对外的正式中文名(例:`ithome.com` → `IT之家`)。不要加"网""官网"等自造后缀。
3. `one_liner` 用一句话(≤30 字)说这个站是做什么的,面向不懂技术的销售人员。不确定就留空字符串。
4. 严格输出 JSON,形如:
{{"items": [{{"domain": "原样回抄的域名", "zh_name": "中文名或空串", "one_liner": "一句话或空串"}}]}}
5. 输入几行就输出几项,domain 原样回抄。

域名列表:
{domains}"""


async def distill_domains(domains: list[str]) -> list[dict[str, str]]:
    """调 LLM 蒸馏域名 → 中文名 + 一句话。fail-soft:任何失败返回 []。

    留痕:经 `adeepseek_post_with_failover(track_name=...)` → `llm_track` 记账。
    """
    keys = _normalized_unique(domains)
    if not keys:
        return []
    body = {
        "model": DISTILL_MODEL,
        "messages": [{"role": "user", "content": _DISTILL_PROMPT.format(domains="\n".join(keys))}],
        "temperature": 0.1,
        "max_tokens": 1200,
        "response_format": {"type": "json_object"},
    }
    try:
        from services.llm.deepseek_key_pool import (
            adeepseek_post_with_failover,
            deepseek_role_for_model,
        )

        resp = await adeepseek_post_with_failover(
            body,
            timeout=60.0,
            role=deepseek_role_for_model(DISTILL_MODEL),
            track_name=DISTILL_TRACK_NAME,
            track_model=DISTILL_MODEL,
        )
        content = (resp.json()["choices"][0]["message"]["content"] or "").strip()
    except Exception as exc:
        logger.warning("[media-dir] 蒸馏调用失败(本批跳过): %s", str(exc)[:200])
        return []

    try:
        payload = json.loads(content)
    except Exception:
        try:
            from json_repair import repair_json

            payload = json.loads(repair_json(content))
        except Exception as exc:
            logger.warning("[media-dir] 蒸馏返回非 JSON(本批跳过): %s", str(exc)[:200])
            return []

    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return []

    allowed = set(keys)
    out: list[dict[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        dom = normalize_directory_domain(item.get("domain"))
        # 🔴 只收**我们问过的**域名:模型自行发挥补出来的域名一律丢弃,
        #    否则目录里会混进从没在榜上出现过的臆造条目。
        if not dom or dom not in allowed:
            continue
        out.append({
            "domain": dom,
            "zh_name": str(item.get("zh_name") or "").strip(),
            "one_liner": str(item.get("one_liner") or "").strip(),
        })
    # 模型漏答的域名补成负缓存行,让 attempts 照常累加(否则永远排队、永远漏答)。
    answered = {o["domain"] for o in out}
    for key in keys:
        if key not in answered:
            out.append({"domain": key, "zh_name": "", "one_liner": ""})
    return out


def distill_domains_sync(domains: list[str]) -> int:
    """后台线程入口:跑一次蒸馏并落库。返回写入行数。任何异常都被吞掉(fail-soft)。"""
    import asyncio

    try:
        entries = asyncio.run(distill_domains(domains))
    except Exception as exc:
        logger.warning("[media-dir] 蒸馏事件循环异常(本批跳过): %s", str(exc)[:200])
        return 0
    if not entries:
        return 0
    named = sum(1 for e in entries if e.get("zh_name"))
    written = upsert_entries(entries, model=DISTILL_MODEL)
    logger.info("[media-dir] 蒸馏完成:请求 %s 个域名 · 拿到中文名 %s 个 · 落库 %s 行",
                len(domains), named, written)
    return written
