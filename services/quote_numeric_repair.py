"""Repair legacy quote numeric fields from existing quote artifacts.

Some production quotes were generated successfully but persisted
monthly_price/total_articles as zero. The markdown and package rows usually
still contain the real commercial numbers, so read paths can safely repair the
canonical quote row without re-running pricing or charging the user again.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from db.txn_guard import swallow          # [WO_242] 见该模块抬头

logger = logging.getLogger("GEO-QuoteNumericRepair")


def _row_to_dict(row: Any) -> dict:
    if not row:
        return {}
    if isinstance(row, dict):
        return dict(row)
    try:
        return dict(row)
    except Exception:
        return {}


def _to_int(value: Any) -> int:
    if value is None:
        return 0
    try:
        return int(float(str(value).replace(",", "").strip()))
    except Exception:
        return 0


def _extract_price(text: str) -> int:
    if not text:
        return 0
    # Prefer explicit money amounts and ignore percentages such as "65%".
    patterns = [
        r"(?:[¥￥]\s*)?([1-9]\d{0,2}(?:,\d{3})+|[1-9]\d{2,6})\s*(?:元|块)",
        r"[¥￥]\s*([1-9]\d{0,2}(?:,\d{3})+|[1-9]\d{2,6})",
        r"([1-9]\d{0,2}(?:,\d{3})+|[1-9]\d{2,6})\s*/\s*月",
    ]
    for pattern in patterns:
        matches = re.findall(pattern, text)
        if matches:
            return _to_int(matches[-1])

    fallback = re.findall(r"([1-9]\d{2,6})(?!\s*%)", text)
    if fallback:
        return _to_int(fallback[-1])
    return 0


def _tier_aliases(tier: str | None) -> list[str]:
    key = (tier or "standard").lower().strip()
    if key in {"entry", "basic", "starter", "lite", "基础版", "入门版"}:
        return ["入门版", "基础版", "体验版", "entry", "basic"]
    if key in {"flagship", "premium", "pro", "旗舰版", "高级版"}:
        return ["旗舰版", "高级版", "premium", "flagship"]
    return ["标准版", "推荐版", "standard"]


def _extract_markdown_price(markdown: str, tier: str | None) -> int:
    if not markdown:
        return 0
    aliases = _tier_aliases(tier)
    prices: list[int] = []
    for raw_line in markdown.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if any(alias in line for alias in aliases):
            price = _extract_price(line)
            if price > 0:
                return price
        if ("/月" in line or "元/月" in line or "每月" in line) and "¥" in line:
            price = _extract_price(line)
            if price > 0:
                prices.append(price)
    if not prices:
        return 0
    prices = sorted(set(prices))
    if len(prices) >= 3 and "标准" in "".join(aliases):
        return prices[len(prices) // 2]
    return prices[0]


def _extract_markdown_articles(markdown: str, tier: str | None) -> int:
    if not markdown:
        return 0
    aliases = _tier_aliases(tier)
    candidates: list[int] = []
    for raw_line in markdown.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if any(alias in line for alias in aliases):
            m = re.search(r"([1-9]\d{0,3})\s*(?:篇|篇文章|篇内容|篇稿件)", line)
            if m:
                return _to_int(m.group(1))
        if "篇" in line and any(token in line for token in ("总", "共", "文章", "内容", "稿件")):
            for m in re.finditer(r"([1-9]\d{0,3})\s*(?:篇|篇文章|篇内容|篇稿件)", line):
                value = _to_int(m.group(1))
                if value > 0:
                    candidates.append(value)
    if not candidates:
        return 0
    candidates = sorted(set(candidates))
    if len(candidates) >= 3 and "标准" in "".join(aliases):
        return candidates[len(candidates) // 2]
    return max(candidates)


def _package_numbers(cur: Any, quote_id: int, tier: str | None) -> tuple[int, int]:
    # 🔴 [WO_242] `quote_packages` 生产**不存在**(`to_regclass` = NULL,仓内也没有建表语句)。
    #    改前这里是裸 `try/except: return 0, 0` —— 看着是优雅降级,实际是
    #    **把调用方的事务打废了**:回到 `repair_quote_numeric_fields` 之后,
    #    会话探测和 `UPDATE quotes` 全部静默失败,修复路径三个月零写入。
    #    `swallow()` 之后连接仍然可用,这就是本单的全部区别。
    rows: list = []
    _ok = False
    with swallow(cur, "sp_quote_packages",
                 where="services/quote_numeric_repair.py:_package_numbers"):
        cur.execute("SELECT * FROM quote_packages WHERE quote_id = %s", (quote_id,))
        rows = [_row_to_dict(r) for r in cur.fetchall()]
        _ok = True
    if not _ok or not rows:
        return 0, 0

    aliases = _tier_aliases(tier)
    selected = None
    for row in rows:
        hay = " ".join(str(row.get(k) or "") for k in ("tier", "name", "package_name", "label"))
        if any(alias.lower() in hay.lower() for alias in aliases):
            selected = row
            break
    if selected is None:
        selected = rows[len(rows) // 2] if len(rows) >= 3 else rows[0]

    price = 0
    for key in ("monthly_price", "final_price", "total_price", "price"):
        price = _to_int(selected.get(key))
        if price > 0:
            break
    articles = 0
    for key in ("total_articles", "articles", "article_count", "required_articles"):
        articles = _to_int(selected.get(key))
        if articles > 0:
            break
    return price, articles


def repair_quote_numeric_fields(quote_id: int, quote: dict | None = None) -> dict:
    """Backfill monthly_price/total_articles when a legacy quote has zeros.

    Returns a small result object:
      {updated: bool, monthly_price: int, total_articles: int}
    """
    from db.diagnosis_db import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        if quote is None:
            cur.execute(
                "SELECT id, tier, monthly_price, total_articles, markdown FROM quotes WHERE id = %s",
                (quote_id,),
            )
            quote = _row_to_dict(cur.fetchone())
        else:
            quote = dict(quote)
        if not quote:
            return {"updated": False, "monthly_price": 0, "total_articles": 0}

        current_price = _to_int(quote.get("monthly_price"))
        current_articles = _to_int(quote.get("total_articles"))
        if current_price > 0 and current_articles > 0:
            return {
                "updated": False,
                "monthly_price": current_price,
                "total_articles": current_articles,
            }

        package_price, package_articles = _package_numbers(cur, quote_id, quote.get("tier"))
        markdown = quote.get("markdown") or ""
        repaired_price = current_price or package_price or _extract_markdown_price(markdown, quote.get("tier"))
        repaired_articles = current_articles or package_articles or _extract_markdown_articles(markdown, quote.get("tier"))

        updates: list[str] = []
        params: list[Any] = []
        if current_price <= 0 and repaired_price > 0:
            updates.append("monthly_price = %s")
            params.append(repaired_price)
        if current_articles <= 0 and repaired_articles > 0:
            updates.append("total_articles = %s")
            params.append(repaired_articles)

        # [audit P2 2026-06-10] B1 故意把候选池 quote 的 monthly_price 置 0(总价不该回显 headline)。
        # repair 在读路径用旧 markdown 回填【落库】→ 候选池总价又变回 headline(全候选池价格被污染)。
        # 修:仅对【无进行中选词会话】的 legacy/已定稿 quote 落库;有 pending session 只返回展示值不 UPDATE。
        # 🔴 [WO_242 · Review 裁定 2026-09-19] 探测失败一律按「**有**会话」处理 ⇒ 不落库。
        #
        #    改前是 `False` = 「没有进行中的会话 ⇒ **可以写**」,方向是反的:
        #    事务被前一条打废之后,它反而让代码**以为**可以写,然后写失败 ——
        #    两次吞异常叠在一起、方向相反,「什么都没发生」于是看起来像「一切正常」。
        #
        #    为什么不只是"把连接救回来、语义照旧":
        #    那条原语义写于「探测失败 ≈ 表缺失」的年代,失败几乎恒真,落 False 才合理。
        #    **有了 SAVEPOINT 之后,探测失败意味着真出了别的事**(权限、超时、列变更),
        #    这时候写进去就是把 B1 那条「候选池总价被回填成 headline」的污染带回来。
        #    ⇒ 拿不准就**不写**,只回显示值。写错价比不写贵。
        _has_pending_session = True          # fail-closed:探测没成功就当作有会话
        with swallow(cur, "sp_pending_session",
                     where="services/quote_numeric_repair.py:repair_quote_numeric_fields"):
            cur.execute(
                "SELECT 1 FROM keyword_selection_sessions WHERE quote_id = %s "
                "AND status IN ('selecting','keywords_submitted','pricing_pending_review','quoted','adding_keywords') "
                "LIMIT 1",
                (quote_id,),
            )
            _has_pending_session = cur.fetchone() is not None

        updated = False
        if updates and not _has_pending_session:
            params.append(quote_id)
            cur.execute(
                f"UPDATE quotes SET {', '.join(updates)} WHERE id = %s",
                tuple(params),
            )
            conn.commit()
            updated = True
            logger.info(
                "[quote-repair] quote_id=%s monthly=%s articles=%s",
                quote_id,
                repaired_price,
                repaired_articles,
            )
        elif updates and _has_pending_session:
            logger.info(
                "[quote-repair] quote_id=%s 有进行中选词会话 · 只返回展示值不落库(防 B1 候选池总价被回填污染)",
                quote_id,
            )

        # [audit #9 返修] 显示层残漏:有进行中选词会话 → 不只是不落库,返回值也【不回显】markdown
        #   回算的 headline 虚高价(对齐 B1"候选池总价不显 headline")。否则 M3 列表/话术仍读到
        #   repaired_price 虚高总价。候选池 monthly_price 被 B1 故意置 0 → 回 current(0)。
        if _has_pending_session:
            return {
                "updated": False,
                "monthly_price": current_price,
                "total_articles": current_articles,
            }

        return {
            "updated": updated,
            "monthly_price": repaired_price or current_price,
            "total_articles": repaired_articles or current_articles,
        }
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.warning("[quote-repair] quote_id=%s skipped: %s", quote_id, e)
        return {"updated": False, "monthly_price": 0, "total_articles": 0}
    finally:
        try:
            conn.close()
        except Exception:
            pass
