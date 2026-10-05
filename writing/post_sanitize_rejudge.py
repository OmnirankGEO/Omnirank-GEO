"""🔴 [顺序修复 2026-08-10] 清洗之后重跑判定 —— 让系统看到的那一版 = AI 读到的那一版。

## 为什么必须有这一步

生产尖 `dfe65fad` 实测,判定与清洗的真实次序:

    _save_article(2973)     判定@3132 / @3165   →  清洗@3197
    rewrite_article(3437)   判定@3637 / @3659   →  清洗@3684
    generate_articles(808)  判定 1247/1252/1383/1388/1420/1425/1444/1449
                            —— 该段**零清洗调用**

**12 处判定全部跑在清洗之前,清洗之后没有任何判定重跑。**
唯一的重评 `evidence_first_policy` 里那个 `_re_trust` 只在 `trust.hard` 非空时
跟在**自动修复**之后,与清洗无关,不构成缓解。

而清洗器 `body_internal_marker_sanitizer._REVIEW_LINE` 是 ``^.*(短语).*$``
—— **整行删**,连同该行携带的来源标注一起带走。构造 5 个样本真跑判定:
**3 个在清洗前后结论翻转**,方向全是「清洗后新增违规」
(`missing_source_boundary` / `missing_verification_steps`)。

净效果:**系统判定合规的那一版,和客户 / AI 实际读到的那一版不是同一篇。**
可见正文里数字还在、来源没了 —— 那正是 AI 引擎判定推广稿的第一特征,
直接压低被引用与被推荐的概率。

## 本模块只记不拦

重跑结果**覆盖** `quality_warning` 的 `evidence` / `evidence_precision` 负载
(让下游看到的是清洗后的真相),并额外落一份 `post_sanitize_delta`。
**不 raise、不改正文、不拒存** —— D8 文章层零阻断,不给客户设门槛。
"""
from __future__ import annotations

from typing import Any

#: 落进 `quality_warning` 的键名。锁按这个常量断言,不许调用方自己拼字符串。
DELTA_KEY: str = "post_sanitize_delta"
ERROR_KEY: str = "post_sanitize_rejudge_error"

_CODE_BUCKETS: tuple[str, ...] = ("hard", "soft", "warnings", "findings")


def _codes(payload: Any) -> set[str]:
    """把一个 warning 负载压成可比较的 ``bucket:code`` 集合。"""
    out: set[str] = set()
    if not isinstance(payload, dict):
        return out
    for bucket in _CODE_BUCKETS:
        for item in payload.get(bucket) or []:
            code = item.get("code") if isinstance(item, dict) else None
            if code:
                out.add(f"{bucket}:{code}")
    return out


def rejudge_after_sanitize(
    title: str,
    sanitized_content: str,
    article: dict,
    topic: dict | None = None,
    *,
    where: str = "",
) -> dict:
    """在**清洗后的正文**上重跑两个判定,把结论写回 ``article['quality_warning']``。

    返回写回后的 ``quality_warning`` 字典(便于调用方与测试断言)。

    Args:
        title: 文章标题(与入库标题一致)。
        sanitized_content: **清洗后**的正文 —— 传清洗前的等于这个函数白写。
        article: 会被就地修改的文章字典。
        topic: 选题字典,用于取 ``evidence_mode`` 与 evidence pack 兜底。
        where: 调用点标识,落进 delta 便于分辨是哪条保存路径。
    """
    from writing.evidence_first_policy import evaluate_content_trust
    from writing.evidence_precision_policy import evaluate_evidence_precision

    qw = article.get("quality_warning")
    if not isinstance(qw, dict):
        qw = {}

    before = _codes(qw.get("evidence")) | _codes(qw.get("evidence_precision"))

    try:
        mode = str((topic or {}).get("evidence_mode") or "unknown")
        trust = evaluate_content_trust(
            str(title or ""), str(sanitized_content or ""), evidence_mode=mode,
        )
        precision = evaluate_evidence_precision(
            str(sanitized_content or ""),
            article.get("evidence_pack") or (topic or {}).get("_evidence_pack") or {},
            article.get("brand_fact_snapshot")
            or (topic or {}).get("brand_fact_snapshot")
            or {},
        )
    except Exception as err:  # 重跑自身故障绝不阻断保存(D8)
        qw[ERROR_KEY] = str(err)[:300]
        article["quality_warning"] = qw
        return qw

    qw["evidence"] = trust.warning_payload()
    qw["evidence_precision"] = precision.payload()
    after = _codes(qw["evidence"]) | _codes(qw["evidence_precision"])
    qw[DELTA_KEY] = {
        "where": where,
        "added": sorted(after - before),
        "removed": sorted(before - after),
        "diverged": bool(after != before),
    }
    article["quality_warning"] = qw
    return qw
