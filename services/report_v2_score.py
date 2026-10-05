"""report_v2_score — 诊断报告评分 SSOT 解析器

CTO bugfix 2026-04-27 · fix/integration-r3-browser-p1 · P1-4

定位:
  · M3 内部视角(/api/diagnosis/{id}) 和公开视角(/api/public/report/{id})
    必须读取同一份 GEO 总分 + 等级
  · 解决浏览器实测发现的 "代理 5/100 起步 vs 公开 18/100 隐形级" SSOT 破裂

版本口径:
  · v2 只读漏斗 SSOT · report_v2_modules_jsonb.funnel.total_score，等级按漏斗表派生
  · v1 只读 SQL total_score，等级按旧 5 维评分表派生
  · 两个版本不得互相回退；越界、非整数或缺失分数一律 fail-closed

调用方:
  - server.py::get_diagnosis_detail (/api/diagnosis/{id})
  - api/share_api.py::get_public_report (/api/public/report/{id})
  - 任何后续需要展示评分的 endpoint 都应走本 helper

元指令 14 · 评分 SSOT 铁律:
  顶部结论 / 正文等级表 / 前端徽章 / 导出 PDF 必须来自同一份配置
  本 helper 是评分对外读取的唯一入口
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

logger = logging.getLogger("GEO-ReportV2Score")


def _parse_bounded_integer_score(value: Any) -> Optional[int]:
    """Parse a real 0..100 integer score without truncation or clamping."""
    if value is None or isinstance(value, bool):
        return None
    if not isinstance(value, (int, float, Decimal, str)):
        return None
    try:
        raw = str(value).strip()
        if not raw:
            return None
        parsed = Decimal(raw)
    except (InvalidOperation, ValueError):
        return None
    if not parsed.is_finite() or parsed != parsed.to_integral_value():
        return None
    score = int(parsed)
    return score if 0 <= score <= 100 else None


def _derive_level(score: int, *, is_v2: bool) -> str:
    """Derive the level from the matching version's scoring SSOT."""
    if is_v2:
        from tools.scoring.funnel_score import get_funnel_meta

        return str(get_funnel_meta(score)["level"])
    from tools.scoring.scoring_levels import get_level

    return str(get_level(score))


def _v2_level_rank(level: str) -> Optional[int]:
    """v2 漏斗等级的**高低序**(越大越高)。不认识的名字返回 None。

    序取自评分器自己的 `_LEVELS_SORTED`(按分数下界排),不另抄一份名单 ——
    本仓 feedback_one_predicate_one_place_or_half_goes_unverified。
    """
    try:
        from tools.scoring.funnel_score import _LEVELS_SORTED
    except Exception:
        return None
    order = [name for name, _lo, _hi in sorted(_LEVELS_SORTED, key=lambda x: x[1])]
    name = str(level or "").strip()
    return order.index(name) if name in order else None


def _reconcile_level(
    stored_level: Any,
    derived_level: str,
    *,
    is_v2: bool,
    source: str,
    level_capped: Any = None,
) -> tuple:
    """存储等级与「由分数反推」的等级不一致时,决定用哪个。返回 (level, level_source)。

    🔴 [WO_233-c1] 原来这里叫 `_warn_on_stored_level_mismatch`,名副其实:
       它**只 logger.warning 一行,然后照样返回重推值**。
       后果不是"日志里有点噪音",是**评分器的封顶在对客读取口被推翻**:
       「只测品牌 10/10」评分器给 100 · 成长级(`level_capped=True`,
       刻意不让单层满分冒充主导),读出来是 100 · **主导级**。
       本仓 `a-warning-that-blocks-nothing-is-not-a-fix` 的又一件 ——
       而且每一个被抬高的案例,生产日志里都躺着那一行 warning。

    修法**不是**「一律信存储值」。模块抬头那句
    「persisted level text is never trusted as an independent fact」是有来由的:
    存储文本可能来自老版本、可能被写坏,盲信等于把 SSOT 交给一个不受控的字段。
    所以这里只接受**不高于**反推值的存储等级:

      · 存储等级 ≤ 反推等级 ⇒ 这是一次**合法的下调**(封顶就是下调),采用存储值;
      · 存储等级 > 反推等级 ⇒ 有人想把等级**抬高**,拒绝,继续用反推值并告警;
      · 存储等级不在已知等级表里 ⇒ 不认,用反推值并告警。

    ⇒ 「不让存储值抬高等级」这条老约束一字未松,只是不再反过来把封顶抹掉。
    """
    raw = None if stored_level is None else str(stored_level).strip()
    if not raw or raw == derived_level:
        return derived_level, "derived"

    if not is_v2:
        # v1(5 维旧体系)没有封顶概念,维持老行为:不信存储文本。
        logger.warning(
            "[report_v2_score] stored level mismatch source=%s; using versioned SSOT", source)
        return derived_level, "derived"

    stored_rank = _v2_level_rank(raw)
    derived_rank = _v2_level_rank(derived_level)
    if stored_rank is None or derived_rank is None:
        logger.warning(
            "[report_v2_score] unknown level name source=%s stored=%r derived=%r;"
            " using versioned SSOT", source, raw, derived_level)
        return derived_level, "derived"

    if stored_rank <= derived_rank:
        logger.info(
            "[report_v2_score] adopting stored (lower or equal) level source=%s"
            " stored=%s derived=%s level_capped=%s",
            source, raw, derived_level, bool(level_capped))
        return raw, "stored_capped" if level_capped else "stored_lower"

    logger.warning(
        "[report_v2_score] stored level is HIGHER than derived source=%s"
        " stored=%s derived=%s; refusing to inflate, using versioned SSOT",
        source, raw, derived_level)
    return derived_level, "derived"


def _parse_modules_jsonb(value: Any) -> Optional[dict]:
    """容错解析 report_v2_modules_jsonb (JSONB 列在不同 driver 下可能是 dict 或 str)"""
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    if isinstance(value, (bytes, bytearray)):
        try:
            value = value.decode("utf-8")
        except Exception:
            return None
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            return json.loads(s)
        except Exception:
            return None
    return None


def resolve_canonical_score(row: dict) -> dict:
    """Resolve a score/level pair using the report version's scoring SSOT.

    V2 uses only the persisted funnel score.  A missing or invalid V2 funnel
    fails closed because the legacy SQL score was calculated by another model.
    V1 uses the legacy SQL score.  In both cases scores must be integer values
    in 0..100 and the level is derived from the matching SSOT; persisted level
    text is never trusted as an independent fact.
    """
    if not isinstance(row, dict):
        return {"total_score": None, "level": None, "source": "missing"}

    is_v2 = str(row.get("report_v2_version") or "").strip().lower() == "v2"
    if is_v2:
        modules = _parse_modules_jsonb(row.get("report_v2_modules_jsonb"))
        if isinstance(modules, dict):
            funnel = modules.get("funnel")
            if isinstance(funnel, dict):
                score = _parse_bounded_integer_score(funnel.get("total_score"))
                if score is not None:
                    derived = _derive_level(score, is_v2=True)
                    meta = funnel.get("level_meta")
                    meta = meta if isinstance(meta, dict) else {}

                    # [WO_233-c3] 覆盖太薄 ⇒ **不出全局总分与等级**(分范围交付)。
                    # 🔴 不是回 0 —— 回 0 会被读成"一次都没被提到",那是**另一个**错误陈述;
                    #    也不是把单层折算成整体。这里回 None,与"缺分"走同一条既有契约
                    #    (调用方本来就要处理 total_score=None,见下方 fail-closed 分支),
                    #    所以不需要任何调用方改动。
                    # 存量报告不静默重写:老报告的 level_meta 没有 scope_limited 键 ⇒
                    #    `.get()` 取到 None ⇒ 行为与改前逐字一致。
                    if meta.get("scope_limited"):
                        return {
                            "total_score": None,
                            "level": None,
                            "source": "scope_limited",
                            "level_source": "scope_limited",
                            "derived_level": derived,
                            "level_capped": bool(meta.get("level_capped")),
                            "partial_sample": bool(meta.get("partial_sample")),
                            "scope_limited": True,
                            "score_version": "v2",
                            # 分范围交付要说清"哪一层测了、哪一层没测",
                            # 否则客户只看到一个空白,不知道是没测还是测了为 0。
                            "measured_layers": [
                                str(l.get("key") or l.get("label") or "")
                                for l in (funnel.get("layers") or [])
                                if isinstance(l, dict) and (l.get("total") or 0) > 0
                            ],
                        }
                    level, level_source = _reconcile_level(
                        funnel.get("level"), derived, is_v2=True,
                        source="v2_funnel", level_capped=meta.get("level_capped"),
                    )
                    # [WO_233-c1] 只回一个整数再让各自推等级,正是本缺陷的成因。
                    # 这里**附带**把封顶/取样标志一并交出去(additive,老键一个没动),
                    # 调用方要判「这分能不能拿去说主导级」时不必再去猜。
                    return {
                        "total_score": score,
                        "level": level,
                        "source": "v2_funnel",
                        "level_source": level_source,
                        "derived_level": derived,
                        "level_capped": bool(meta.get("level_capped")),
                        "partial_sample": bool(meta.get("partial_sample")),
                        "score_version": "v2",
                    }
        logger.warning(
            "[report_v2_score] invalid or missing V2 funnel score; failing closed"
        )
        return {"total_score": None, "level": None, "source": "missing"}

    score = _parse_bounded_integer_score(row.get("total_score"))
    if score is not None:
        derived = _derive_level(score, is_v2=False)
        level, level_source = _reconcile_level(
            row.get("level"), derived, is_v2=False, source="v1_column")
        return {
            "total_score": score,
            "level": level,
            "source": "v1_column",
            "level_source": level_source,
            "derived_level": derived,
            "level_capped": False,   # v1(5 维旧体系)没有封顶概念
            "partial_sample": False,
            "score_version": "v1",
        }

    return {"total_score": None, "level": None, "source": "missing"}


def format_report_date_zh(value: Any) -> str:
    """Format a report timestamp for customer-facing HTML/PDF output."""
    parsed: date | None = None
    if isinstance(value, datetime):
        parsed = value.date()
    elif isinstance(value, date):
        parsed = value
    elif value:
        raw = str(value).strip()
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
        except (TypeError, ValueError):
            try:
                parsed = datetime.strptime(raw[:10], "%Y-%m-%d").date()
            except (TypeError, ValueError):
                parsed = None
    if parsed is None:
        return ""
    return f"{parsed.year}年{parsed.month}月{parsed.day}日"


def build_canonical_report_meta(row: dict, *, raw_data: Any = None) -> dict:
    """Build shared HTML/PDF metadata without creating another score system."""
    record = row if isinstance(row, dict) else {}
    canonical = resolve_canonical_score(record)
    parsed_raw = raw_data
    if parsed_raw is None:
        parsed_raw = record.get("data") or record.get("raw_data_json")
    if isinstance(parsed_raw, str):
        parsed_raw = _parse_modules_jsonb(parsed_raw)
    if not isinstance(parsed_raw, dict):
        parsed_raw = {}
    nested_data = parsed_raw.get("data") if isinstance(parsed_raw.get("data"), dict) else {}
    return {
        "brand_name": record.get("brand_name"),
        "total_score": canonical.get("total_score"),
        "level": canonical.get("level"),
        "score_source": canonical.get("source"),
        "industry": record.get("industry"),
        "city": parsed_raw.get("city") or nested_data.get("city"),
        "created_at": format_report_date_zh(record.get("created_at")),
    }


__all__ = [
    "build_canonical_report_meta",
    "format_report_date_zh",
    "resolve_canonical_score",
]
