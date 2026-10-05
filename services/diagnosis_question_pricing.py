"""诊断的**按题加价**规则 —— legacy 与防御/混合线的**唯一**实现。

🔴 [P0 · 2026-09-02 · Owner 拍板] 为什么会有这个模块
--------------------------------------------------
防御/混合线原本**按格**计价:``planned_cells = 题数 × 平台数``,
``_price_for_plan`` 再 ``unit * (cells - 1)``。Owner 手机实测
hybrid 5 题 × 4 平台 = 20 格 ⇒ **13,000 算力**,是 legacy 一次诊断的 20 倍。
admin 账号显示「平台承担」看不出来,真服务商会被真扣。

Owner 定的口径:**防御/混合线与 legacy 同规则** —— 基价来自现役
``feature_pricing``,加价**按题不按平台**,hybrid 按**一次**诊断计。

🔴 规则本体只许有这一份
----------------------
legacy 的这段规则原本内联在 ``server.py`` 的诊断端点里。两条线各写一份的话,
**必有一处没人验**(本仓记过),而这一处漂了就是钱漂了 ——
方向还不确定:多收要退款,少收是收入漏。所以这里是**唯一实现**,
两条线都调它;接线锁 ``test_both_lines_call_the_shared_rule`` 钉住这一点。

🔴 数字是**动态系数**,不是合同
------------------------------
``FREE_CUSTOM_QUESTIONS`` / ``EXTRA_POINTS_PER_QUESTION`` 的**当前值**取自
legacy 现役实现(``server.py`` 原内联段),不是我定的。资金 SSOT
``docs/SYSTEM_TRUTH/08_billing.md`` 只记**规则**不记数字 —— 数字以生产配置为准。
改这两个数 = 改价,要走 Owner。
"""

from __future__ import annotations

from typing import Optional

#: 规则版本 —— 进 ``pricing_catalog_version`` 的 hash。
#:
#: 🔴 改**规则**(而不只是改数字)时必须动它:版本不变 ⇒ 在途 preview 不会被
#:    判成 SNAPSHOT_CHANGED ⇒ 它们仍按**旧规则**冻结,而 confirm 响应显示的
#:    还是旧价。本次(按格 → 按题)正是这么被发现的:价目表一个字没改,
#:    而旧版 hash 只吃表内容,于是规则变了、版本没变。
PRICING_RULE_VERSION = "per-question-v1"

#: 免费的自定义题数上限:超过这个数的部分才加价。
FREE_CUSTOM_QUESTIONS = 8

#: 每道加价题的算力。
EXTRA_POINTS_PER_QUESTION = 100


#: 单题字数上限。**口径只有这一份。**
#:
#: 🔴 [#157] 原来只活在 `server.py` 的 `validate_custom_questions` 里,
#:    而防守线冻结题单时**根本不查** —— 于是 96 字品牌名生成的系统题
#:    (`{品牌名}是做什么的?` = 102 字)能被冻进题单,到后台派发才炸,
#:    而那时已经没有 HTTP 出口把它翻成 422:它落进 `except Exception`,
#:    归还 marker、反复重试,直到 attempts 耗尽由 sweeper 退款。
#:    用户看到的是「一直没跑起来,最后退了钱」,没有一句话说是题太长。
MAX_QUESTION_CHARS = 100


def question_length_violation(q) -> Optional[int]:
    """超限返回它的**实际字数**,否则 None。

    只回答「超没超、超多少」;**怎么说**由各自的出口决定 ——
    提交端要的是 422 报文,冻结端要的是"哪道题、多少字、怎么办"。
    口径一份、措辞两处,不是两份实现。
    """
    if not isinstance(q, str):
        return None
    n = len(q.strip())
    return n if n > MAX_QUESTION_CHARS else None


def normalize_questions(questions) -> list:
    """题集归一化 —— **沿用 legacy 已有的那一份口径,不新写第二份**。

    🔴 与 `server.py` 的 `DiagnosisRequest.validate_custom_questions` 逐条同解:
    strip -> 去空 -> **精确去重(区分大小写)** -> **保序**。
    两处各写一份归一化,是「同一谓词写两处」的典型:哪天一处改了大小写口径,
    哈希就永远对不上,而对不上的表现是**用户被 409 挡住**,不是报错。
    """
    out, seen = [], set()
    for q in (questions or []):
        if not isinstance(q, str):
            continue
        q = q.strip()
        if not q or q in seen:
            continue
        out.append(q)
        seen.add(q)
    return out


DIAGNOSIS_FEATURE_BY_SCOPE = {
    "geo": "geo_diagnosis",
    "social": "social_diagnosis",
    "full": "full_diagnosis",
}


def feature_code_for_scope(scope) -> str:
    """scope -> 扣费 feature_code 的**唯一**映射。

    🔴 [订正二十二] 这份映射原先在**三处**各写一份:
      server.py:3854 实际扣费 / server.py:3614 守卫 / pricing_ssot_api:266 预览。
    三处任意两处不一致,后果分别是:
      预览 != 守卫 => 该 scope **恒 409,永远提交不了**(Review 毒 10 打的就是这格);
      守卫 != 扣费 => 闸放行了,却按另一个价扣钱 —— **看到的 != 扣的** 又回来。
    而这两种不一致**都不会有任何东西报错**。加一条「三处必须相等」的判据只能盯住
    今天这三处;合成一处则结构上无从不一致 —— 消灭这根轴,而不是去诊断它。
    """
    return DIAGNOSIS_FEATURE_BY_SCOPE.get((scope or "geo"), "geo_diagnosis")


def quote_diagnosis_price(questions, *, base_points: int, ai_optimized: bool,
                          feature_code: str) -> dict:
    """[#84 sec3 返修二] **一次派生**出 points 与 pricePreviewId。

    🔴 为什么要合成一个函数:上一版 POST 里 `extra` 与 `price_preview_id()`
    各自算一遍,于是可以**分家** —— 把 extra 那次的开关写死 False,
    显示 650、id 却绑 950,提交放行扣 950,**用户看到的是 650**。
    加一条断言只能盯住今天这一种写法;从同一份 payload 派生,
    则「显示价」与「被绑的价」结构上不可能不同 —— 消灭这根轴,而不是去诊断它。
    """
    import hashlib
    import json

    norm = normalize_questions(questions)
    n = len(norm)
    extra = int(extra_points_for_questions(n, ai_optimized=bool(ai_optimized)))
    payload = {
        "questions": norm,
        "base_points": int(base_points),
        "ai_optimized": bool(ai_optimized),
        "feature_code": str(feature_code),
        "free_questions": int(FREE_CUSTOM_QUESTIONS),
        "per_extra": int(EXTRA_POINTS_PER_QUESTION),
        "extra": extra,
    }
    payload["points"] = int(base_points) + extra
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {
        "questions": norm,
        "question_count": n,
        "extra": extra,
        "points": payload["points"],
        "preview_id": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
    }


def price_preview_matches(preview_id, questions, *, base_points: int,
                          ai_optimized: bool, feature_code: str) -> bool:
    """[#84 sec3 返修] 守卫本体 —— **纯函数**,判据可以直接驱动。

    🔴 上一版把比对写在端点里,于是判据只能拿 `price_preview_id()` 跟自己比:
    我那条叫 `..._round_trips_through_the_guard` 的判据**从未调用过守卫**,
    三发毒(输入换成 []、!= 换成 ==、POST 返空题集)**全部存活**。
    判据的名字声称了断言从没验的性质 —— 抽成纯函数才能真的驱动那一行。
    """
    if not preview_id:
        return False
    expected = price_preview_id(
        questions,
        base_points=base_points,
        ai_optimized=ai_optimized,
        feature_code=feature_code,
    )
    return str(preview_id).strip() == expected


def price_preview_id(questions, *, base_points: int, ai_optimized: bool,
                     feature_code: str) -> str:
    """薄壳:与 `quote_diagnosis_price` **同源**,不再自己拼 payload。

    🔴 两处各拼一份 payload 就是「同一谓词写两处」——
    哪天一处加了轴另一处没加,id 对不上而**没有任何东西报错**。
    """
    return quote_diagnosis_price(
        questions, base_points=base_points, ai_optimized=ai_optimized,
        feature_code=feature_code)["preview_id"]


def extra_points_for_questions(question_count, *, ai_optimized: bool) -> int:
    """自定义题带来的**加价**(不含基价)。

    与 legacy 逐值同解:
      · 一道自定义题都没有        ⇒ 0
      · 开了 AI 优化              ⇒ 每道都加价
      · 没开                      ⇒ 只有超过 ``FREE_CUSTOM_QUESTIONS`` 的部分加价

    🔴 **不看平台数**。同一份题问 4 个平台仍是一次诊断的题量 ——
       这正是 Owner 要修的那件事:按格乘平台把一次诊断卖成了 20 次。

    🔴 负数/None 一律按 0:计价函数不该因为上游给了脏值就把钱算成负的。
    """
    n = int(question_count or 0)
    if n <= 0:
        return 0
    if ai_optimized:
        return n * EXTRA_POINTS_PER_QUESTION
    return max(0, n - FREE_CUSTOM_QUESTIONS) * EXTRA_POINTS_PER_QUESTION
