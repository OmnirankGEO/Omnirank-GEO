# -*- coding: utf-8 -*-
"""诊断业务范围(regional / national)的**唯一裁决点**。

[R3 · 2026-08-03 返工单 REWORK-DIAG-QGATE]

单独成模块而不是塞进 ``workflows/diagnosis_workflow.py``,是因为后者 import 链上
``db/diagnosis_db.py`` 在模块级调 ``init_db()`` → **import 就要连库**,判别锁没法
在没有库的环境里跑。这里只在函数体内惰性 import ``db.connection``,模块本身 import-pure。

选词矫正闸(``services.diagnosis_question_quality``)保持纯函数、不碰库;
"到底算全国还是区域"这个**要查档案**的判断放在这一层。
"""
from __future__ import annotations

from services.diagnosis_question_quality import (
    SCOPE_NATIONAL,
    normalize_business_scope,
)


def resolve_effective_business_scope(brand_id, business_scope: str) -> str:
    """业务范围以 ``brands.city_scope`` 为准 —— **只升不降**。

    **病根**:brand 745「贵州禾泉酒业」``brands.city_scope='national'``(招商 / OEM /
    基酒批发确实是全国生意),但矫正闸实际按 ``regional`` 跑 → ``super_tier1`` 层
    也被要求带地域并被整条替换,违反超一级词自身定义(出题 prompt 规则 3「超一级词
    禁止包含品牌名和地区」),两层测同一批题 → 漏斗评分失真。

    **scope 的真实来源**(返工单 R3 第 1 步要求查清 · 生产实测结论):
    是**前端 ``NewDiagnosis.tsx`` 表单的「业务范围」下拉**,不是 LLM 的
    ``business_context.business_type``。该下拉的预填只读
    ``client_profiles.service_scope / city_scope``;而 brand 745 **根本没有
    client_profiles 行** → 预填拿到空串 → 静默落到 ``'regional'`` →
    ``payload.business_scope='regional'`` → ``server.py`` → ``run_diagnosis_workflow``
    → ``analyze_client_business`` → ``_enforce_question_quality`` 一路透传。
    后端全链**从不读 brands.city_scope**。落库快照
    ``question_quality.business_scope`` 在 515/517/519 三次诊断里都是 ``'regional'``,
    与此吻合 —— 不是"LLM 传错了",是这条链压根没接上品牌档案。

    **为什么"只升不降"而不是返工单字面的"有值即以它为准"**:
    ``brands.city_scope`` 的 DEFAULT 就是 ``'local'``(生产 322 条 local / 8 条
    national;``server.py`` 自己的注释写着"生产实证 brands.business_type/city_scope
    313/314 行是死默认 B2C/local")。若把 ``'local'`` 也当权威值,所有在表单里
    **正确勾了「全国生意」**的客户都会被静默打回 regional —— 那是新一轮"修 A 坏 B"。
    所以:

      · ``city_scope='national'``   → 覆盖成 national(修好 brand 745)
      · ``'local'`` / 空 / 读不到  → **不覆盖**,仍走 表单值 → LLM business_type
                                     → 默认 regional 的旧链(旧行为逐字节保住)

    fail-soft:查库异常一律保持传入值,绝不阻断诊断。
    """
    if not brand_id:
        return business_scope
    if normalize_business_scope(business_scope) == SCOPE_NATIONAL:
        return business_scope  # 已经是 national,无需再查库
    try:
        from db.connection import get_connection

        conn = get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT city_scope FROM brands WHERE id = %s", (int(brand_id),))
            row = cursor.fetchone()
        finally:
            conn.close()
        brand_scope = str((row or {}).get("city_scope") or "").strip()
        if brand_scope and normalize_business_scope(brand_scope) == SCOPE_NATIONAL:
            print(
                f"   🌐 业务范围以品牌档案为准: brands.city_scope='{brand_scope}' → national"
                f"(表单传的是 '{business_scope or '(空)'}')"
            )
            return SCOPE_NATIONAL
    except Exception as err:  # fail-soft:查不到档案绝不阻断诊断
        print(f"   ⚠️ 读取 brands.city_scope 失败(保持传入值 · 不影响诊断): {err}")
    return business_scope


def resolve_brand_cities(brand_id) -> str:
    """读 ``brands.cities`` 原始串(整串,不做任何解析);读不到返回空串。

    [R5 · 2026-08-04 返工单 REWORK-DIAG-QGATE R5]

    **为什么需要它**:诊断的「客户区域」是**前端表单值**(``client_location``),
    与品牌档案 ``brands.cities`` 是两条独立的值。表单的预填只在
    ``GET /api/diagnosis/prefill/{brand_id}`` 那一处读过 ``brands.cities``,
    而且**历史诊断的 ``input_params.client_location`` 还会把它覆盖掉**
    (``server.py`` 的 "历史诊断 client_location 优先于 brand.cities")。
    → 用户一旦手改过一次区域,之后每次诊断都继承那个手改值,档案再也进不来。

    生产实证(brand 737 深圳港融医疗美容):
      · 诊断 509(08-01) 表单值 = ``广东省深圳市龙岗区``(= 档案值)→ 0 repairs
      · 诊断 529(08-04) 表单值 = ``广东省，香港``            → 5 repairs、4 条双地名
    同一品牌、同一份档案、同一套规则,差别只在表单值 —— 所以档案必须进 token 池。

    **只取原始串、不在这里解析**:主地名怎么选、token 怎么并,全部是
    ``services.diagnosis_question_quality`` 里的纯函数(可单测、不连库)。
    这一层只负责"把档案取出来"。

    fail-soft:查库异常一律返回空串,绝不阻断诊断(空串 = 退回只吃表单值的旧行为)。
    """
    if not brand_id:
        return ""
    try:
        from db.connection import get_connection

        conn = get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT cities FROM brands WHERE id = %s", (int(brand_id),))
            row = cursor.fetchone()
        finally:
            conn.close()
        cities = str((row or {}).get("cities") or "").strip()
        if cities:
            print(f"   📍 品牌档案经营城市: brands.cities='{cities}'(并入地域 token 池)")
        return cities
    except Exception as err:  # fail-soft:查不到档案绝不阻断诊断
        print(f"   ⚠️ 读取 brands.cities 失败(退回只用表单区域 · 不影响诊断): {err}")
        return ""
