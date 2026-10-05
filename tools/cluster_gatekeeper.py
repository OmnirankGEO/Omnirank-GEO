"""
关键词聚类守门员（Layer 2 防御）

[CTO-15.23 2026-05-05 P0 fix] 词包跨城/跨子类合并保险

背景:
  keyword_cluster.py 用 LLM 做语义聚类,但 LLM 会把"代理不知道客户秒懂"的边界
  误合并(深圳+东莞、工业 PCB + 消费 PCB、10吨叉车 + 3吨叉车...)
  导致 covered_keywords 0 元 0 文章,客户付钱后交付不全 → 退款 + 信誉炸

设计:
  Layer 1: 城市硬桶(代码层,在 keyword_cluster._split_by_city)
  Layer 2: 守门员 LLM 复核(本模块) — 对每个候选合并包独立调用 deepseek-v4-flash 判定
           thinking=OFF + temperature=0.1 + JSON 输出
           失败/超时/解析错 → 默认 split(保守兜底,宁多不少)

模型选择:
  deepseek-v4-flash + thinking=OFF
  · 实测 7 边界场景 100% 通过(scripts/cluster_v4_flash_vs_pro.py)
  · 单次 ~0.3-0.5s · ¥0.001/次 · 一次报价 ~10 包总开销 < ¥0.01
"""
import json
from config.deepseek_models import DEEPSEEK_OFFICIAL_FLASH
import logging
import os
import re
from typing import Optional

import httpx

logger = logging.getLogger("ClusterGatekeeper")

# ============================================
# 守门员 prompt
# ============================================
GATEKEEPER_PROMPT = """你是一个关键词合并合理性审核员。判断以下关键词包是否「客户搜任意一个,同一篇文章都能完整满足需求」。

## 候选合并包
{keywords}

## 必须 SPLIT 的硬规则(任意一条命中即拆)
1. **不同城市** — 深圳 vs 东莞 vs 惠州 是不同市场,本地竞争对手不同
2. **不同产品规格/子品类** — 10吨叉车 vs 3吨叉车 / 工业 PCB vs 消费 PCB / 进口红酒 vs 国产红酒
3. **不同服务环节** — 安装 vs 维保 vs 加装(完全不同业务流)
4. **不同客户群体** — 企业 vs 个人 / B2B vs B2C
5. **不同教学/服务模式** — 1对1 vs 小班 vs 网课 / 自营 vs 加盟
6. **不同行业子类** — 婚庆摄影 vs 商业广告摄影 vs 证件照
7. **任何客户在签约前会问"你们是哪种"的差异**

## 可以 MERGE 的情况
- 同城市 + 同产品 + 不同问法("哪家好" / "哪家靠谱" / "推荐" / "排名" / "公司推荐" / "排行榜")
- 仅修饰词差异("高端" / "专业" / "靠谱的" 同 + 同一服务)

## 输出 JSON(严格格式,不要任何其他文字)
{{
  "verdict": "merge" 或 "split",
  "split_groups": [["词1","词3"], ["词2"]],
  "reason": "一句话理由(20字内)"
}}

- verdict=merge 时 split_groups 留空数组 []
- verdict=split 时 split_groups 必须包含拆分方案,所有原词都要分配到某个组
"""


async def _call_gatekeeper_llm(
    keywords: list[str],
    timeout: float = 30.0,
) -> Optional[dict]:
    """
    调用守门员 LLM (deepseek-v4-flash thinking=OFF) 判定合并合理性

    Returns:
        dict: {"verdict": "merge"|"split", "split_groups": [[...], ...], "reason": "..."}
        None: 调用失败/解析失败 → 调用方应默认 split 兜底
    """
    from services.llm.deepseek_key_pool import has_deepseek_key, adeepseek_post_with_failover
    if not has_deepseek_key():
        logger.warning("守门员: DEEPSEEK_API_KEY 未配置 → 跳过复核")
        return None

    kw_list = "\n".join(f"- {kw}" for kw in keywords)
    prompt = GATEKEEPER_PROMPT.format(keywords=kw_list)

    body = {
        "model": DEEPSEEK_OFFICIAL_FLASH,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1,
        "max_tokens": 500,
        "stream": False,
        "thinking": {"type": "disabled"},
    }

    try:
        # [failover 2026-06-11] 多 key 失败自动换下一个重试(单 key=直调·向后兼容)·内含 llm_track
        resp = await adeepseek_post_with_failover(
            body,
            timeout=timeout,
            track_name="keyword_cluster_gatekeeper",
            track_model=DEEPSEEK_OFFICIAL_FLASH,
        )
        content = resp.json()["choices"][0]["message"]["content"]
    except Exception as e:
        logger.warning(f"守门员: 调用异常 {e}")
        return None

    # 解析 JSON
    try:
        match = re.search(r'\{.*\}', content, re.DOTALL)
        if not match:
            logger.warning(f"守门员: 无 JSON · content={content[:200]}")
            return None
        result = json.loads(match.group())
        verdict = result.get("verdict")
        if verdict not in ("merge", "split"):
            logger.warning(f"守门员: 无效 verdict={verdict}")
            return None
        return {
            "verdict": verdict,
            "split_groups": result.get("split_groups") or [],
            "reason": result.get("reason", ""),
        }
    except Exception as e:
        logger.warning(f"守门员: JSON 解析失败 {e} · content={content[:200]}")
        return None


async def gatekeeper_check(
    keywords: list[str],
    timeout: float = 30.0,
) -> list[list[str]]:
    """
    守门员主入口: 判断关键词包是否该 merge 还是 split

    Args:
        keywords: 候选合并包内的关键词列表
        timeout: LLM 调用超时(秒)

    Returns:
        list[list[str]]: 拆分方案
        - 长度 1 = merge(全部保留在一个包)
        - 长度 >1 = split(按守门员建议拆分)

        Fail-safe(LLM 失败/解析错):
        - 词数 <= 2 → merge(单词或双词无需拆)
        - 词数 > 2 → 默认每词单独成组(split 到底,保守不少收钱)

        关键词数 <= 1 直接返回不调 LLM(无意义)
    """
    if len(keywords) <= 1:
        return [list(keywords)]

    if len(keywords) == 2:
        # 两词时仍调一次 LLM (轻量,~¥0.001) · 让守门员判
        # 但失败时 merge 兜底(否则强拆 2 词同义会误拆)
        result = await _call_gatekeeper_llm(keywords, timeout)
        if result is None:
            logger.info(f"守门员失败 (2词,merge 兜底): {keywords}")
            return [list(keywords)]
        if result["verdict"] == "split":
            groups = result.get("split_groups") or [[k] for k in keywords]
            return _validate_split(groups, keywords)
        return [list(keywords)]

    # 3+ 词
    result = await _call_gatekeeper_llm(keywords, timeout)
    if result is None:
        # Fail-safe: 全部单独成组(保守 split)
        logger.info(f"守门员失败 (3+词,单独成组兜底): {len(keywords)} 词")
        return [[k] for k in keywords]

    if result["verdict"] == "merge":
        logger.info(f"守门员 ✅ merge ({len(keywords)}词): {result.get('reason', '')}")
        return [list(keywords)]

    # split
    groups = result.get("split_groups") or [[k] for k in keywords]
    validated = _validate_split(groups, keywords)
    logger.info(f"守门员 🔀 split ({len(keywords)}词 → {len(validated)}组): {result.get('reason', '')}")
    return validated


def _validate_split(groups: list[list[str]], original: list[str]) -> list[list[str]]:
    """校验 split_groups 合法性: 所有原词都要被分配,无遗漏无重复"""
    seen = set()
    valid_groups = []
    for g in groups:
        kept = [kw for kw in g if kw in original and kw not in seen]
        if kept:
            valid_groups.append(kept)
            seen.update(kept)

    # 兜底: LLM 漏分配的词单独成组
    missing = [kw for kw in original if kw not in seen]
    if missing:
        logger.warning(f"守门员 split_groups 漏分配 {len(missing)} 词,单独成组")
        for kw in missing:
            valid_groups.append([kw])

    return valid_groups if valid_groups else [list(original)]
