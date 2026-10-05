"""出题生成器的**品牌侧输入** —— 预览端与诊断实跑共用这一份。

## 为什么是共享的一份,而不是「把预览端的参数补齐」

`tools.keyword_generator.analyze_client_business` 有两个调用点:

  · `server.py` 的 `/api/diagnosis/suggest-questions`(页面加载时的**预览**)
  · `workflows/diagnosis_workflow.py` 的**实跑**

它们喂的东西一度**严格不同**:预览跑的是 `keywords=[]`、无地域、无飞轮素材的
「瞎猜」配置 —— 而工作流自己的注释就写着这几项的分量:

    「城市 + 业务范围必须传进去:选词的地域适配全靠这两个值,
      缺了就只能靠品牌名瞎猜(驰鲸案例的 0 命中根因之一)」

把预览端的参数补齐只修**今天这一次**。下次有人给工作流加第 6 个品牌侧输入,
两边照样分家,而且**不会有任何东西变红** —— 这正是「能消灭的轴就别去诊断它」。
⇒ 所以两侧从**同一处**取值,再配一条数据流锁把分家钉住。

## 边界:这三样**不在**这里

  · `additional_info` / 上传文件 / `business_scope` —— **表单期**才存在的东西,
    页面加载那一刻用户还没填。留空是**不可消除的差距**,不假装补上。
  · `question_framing` —— 🔴 两侧**必须不同**:预览按 mode 传
    `None` / `"brand_directed"` 才产得出增长/防守两侧,工作流不传取默认。
    那是两侧的**来源**,不是「没对齐」。谁来统一它,两侧就没了。
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger("GEO-GeneratorInputs")

#: 生成器的**品牌侧**实参键。数据流锁按这个集合比两个调用点 ——
#: 加了第 6 个品牌侧输入却没进这里,锁就该红。
BRAND_SIDE_KEYS = ("keywords", "client_location", "brand_cities", "flywheel_material")


def _resolve_keywords(brand_id, brand_name, industry):
    """走**提交路径同一个阶梯**,并报出来自哪一级。

    🔴 fail-soft 到 `([], "none")`:调用它的是**页面加载**路径,
       取不到词是「这次没建议」,不是「这个页面打不开」。
       (与 `/api/diagnosis/keyword-suggestion` 的空态处置同形。)
    """
    from db.connection import get_connection
    from services.diagnosis_keyword_source import (
        NoKeywordSource, resolve_keywords_with_source)

    conn = get_connection()
    try:
        cur = conn.cursor()
        try:
            return resolve_keywords_with_source(
                cur, given=None, brand_id=brand_id,
                brand_name=brand_name, industry=industry)
        except NoKeywordSource:
            return [], "none"
    except Exception as err:
        logger.warning("[生成器输入] 取词失败 brand=%s: %s", brand_id, err)
        return [], "none"
    finally:
        try:
            conn.close()
        except Exception:
            pass


async def _flywheel_material(brand_name, industry, city):
    """B4 飞轮沉淀素材。**只吃品牌侧输入**,所以两侧都拿得到。

    判断点关闸时**根本不调用**(连库都不查)· 任何异常只记日志、绝不阻断 ——
    与抽出来之前逐字节同形。
    """
    try:
        from services.flywheel_judgment import judgment_enabled
        from services.flywheel_diagnosis_reuse import POINT_KEY as _B4_POINT_KEY

        if not judgment_enabled(_B4_POINT_KEY):
            return None

        from services.flywheel_diagnosis_reuse import suggest_reusable_material

        material = await asyncio.to_thread(
            suggest_reusable_material,
            brand_name=brand_name,
            industry=industry,
            city=city or "",
        )
        print(
            f"   ♻️ 飞轮沉淀复用: 同行 {len(material.get('competitor_seeds') or [])} 个 · "
            f"问题种子 {len(material.get('question_seeds') or [])} 条 · "
            f"来源 {material.get('source')}"
        )
        return material
    except Exception as err:
        print(f"   ⚠️ 飞轮沉淀复用跳过（不影响诊断）: {err}")
        return None


async def brand_side_generator_inputs(
    *,
    brand_id,
    brand_name: str,
    industry: str,
    client_location: str,
    keywords=None,
    resolve_missing_keywords: bool = False,
):
    """返回 `(kwargs, meta)`。`kwargs` 的键**恰是** `BRAND_SIDE_KEYS`。

    `meta` 只给调用方观测(`keyword_source`),**不是**生成器实参 ——
    混进 `kwargs` 会让数据流锁比较的集合被观测字段污染。

    🔴 `resolve_missing_keywords` 默认 **False**,这是行为保持的命门:
       工作流拿到的 `keywords` 是**它的调用方**已经跑过阶梯的结果,
       哪怕是空列表也要**原样透传**。在这里替它重解析,会把
       「调用方给了空词」变成「按品牌名重新造词」甚至抛 `NoKeywordSource` ——
       那是行为改变,不是抽取。
       只有预览端(它上游没有任何人跑过阶梯)才置 True。
    """
    from services.diagnosis_business_scope import resolve_brand_cities

    resolved, source = keywords, "given"
    if resolve_missing_keywords and not resolved:
        resolved, source = _resolve_keywords(brand_id, brand_name, industry)

    kwargs = {
        "keywords": resolved,
        "client_location": client_location,
        "brand_cities": resolve_brand_cities(brand_id),
        "flywheel_material": await _flywheel_material(
            brand_name, industry, client_location),
    }
    return kwargs, {"keyword_source": source}
