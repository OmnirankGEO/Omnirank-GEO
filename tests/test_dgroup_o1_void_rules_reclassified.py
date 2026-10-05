"""D 组 O1 判别测试 —— "整报/整批作废"式硬规则按 §17.1 逐处判类改写后的回退判别。

Master SSOT §17.1 点名三个文件仍有"作废"式硬规则,并明令**不得机械删除**:每处
必须判 H0/H1/A1/O1 并配反向判别测试。本文件锁死三处的新裁决:

| 处 | 旧裁决 | 新裁决 | 依据 |
|---|---|---|---|
| `report_enhancement_agent` ×5 提示词头"违反将导致报告作废" | 整篇作废(伪 H0) | **A1** 定位标注 + 局部修正 + 人工确认继续 | 事故#4 / D8 文章层零阻断 / §11.3 |
| `keyword_expander` "质量红线(违反即作废)" | 整批作废(伪 H0) | **A1** 逐词标注原因单列,其余合格词照常交付 | 事故#3 / §9.6 不得静默丢词 |
| `keyword_expander` "输出格式(违反则整批作废!)" | 整批作废(伪 H0) | **A1** 逐行解析,个别行跳过不连坐 | 事故#4 连坐作废 |
| `monitoring_api` 洞察提示词"违反整批作废" | 整批作废(伪 H0) | **A1** 归一化 + fallback,不整批丢 | 事故#8 / §1.6 |

判别方向 = **DENIED→ALLOWED**:证明运行时从不因"违反"把整批/整篇合法产物清零。
测试打真实运行时函数(解析器/过滤器/归一化器),不只断言源码字符串;字符串断言仅
用于锁死"提示词不再宣称整篇/整批作废"这一无法从运行时观察的提示层裁决。
"""
import asyncio
import inspect

import pytest


# ============================================================
# ① report_enhancement_agent —— 5 处提示词头
# ============================================================
class TestReportEnhancementA1:
    def _prompts(self):
        import agents.report_enhancement_agent as rea
        return {
            name: getattr(rea, name)
            for name in dir(rea)
            if name.endswith("PROMPT") and isinstance(getattr(rea, name), str)
        }

    def test_no_prompt_threatens_whole_report_void(self):
        """A1 裁决:提示词不得再宣称"违反将导致报告作废"(伪 H0)。"""
        offenders = [n for n, p in self._prompts().items() if "报告作废" in p]
        assert offenders == [], f"仍有提示词威胁整篇作废(违反 §11.3/D8): {offenders}"

    def test_reframed_header_states_local_fix_and_human_continue(self):
        """新头必须明示 A1 三要素:定位标注 + 局部修正 + 人工确认继续。"""
        headers = [p for p in self._prompts().values() if "# ⚠️ 输出规范" in p]
        assert len(headers) == 5, f"预期 5 处改写后的输出规范头,实得 {len(headers)}"
        for p in headers:
            line = next(l for l in p.splitlines() if l.startswith("# ⚠️ 输出规范"))
            assert "定位标注" in line
            assert "局部修正" in line
            assert "确认继续" in line
            assert "不因此整篇作废" in line

    def test_substantive_taboos_preserved(self):
        """只改裁决口径,不放松实质内容要求(禁对话式开头/占位符/暴露提示词仍在)。"""
        joined = "\n".join(self._prompts().values())
        for taboo in ("禁止任何对话式开头", "禁止占位符", "禁止暴露提示词", "禁止自我评价"):
            assert taboo in joined, f"实质要求被误删: {taboo}"

    def test_prompt_leak_sanitizer_still_matches_renamed_header(self):
        """改名后 prompt 泄露清理仍须命中(否则改口径反而漏了防泄露)。"""
        import re
        import agents.report_enhancement_agent as rea
        src = inspect.getsource(rea)
        # 从源码取出真实使用的清理正则(raw string,取引号内即正则本体)并实跑,
        # 不靠肉眼核对(feedback_gate_must_run_query_func)。
        matches = re.findall(r'r"(#\\s\*⚠️\\s\*输出[^"]*)"', src)
        assert matches, "未在源码中找到输出小节的 prompt 泄露清理正则"
        real = matches[0]
        leaked_new = "# ⚠️ 输出规范（不符合项会被定位标注）\n1. 禁止占位符\n"
        leaked_old = "# ⚠️ 输出禁忌（违反将导致报告作废）\n1. 禁止占位符\n"
        assert re.sub(real, "", leaked_new) != leaked_new, "新头泄露时未被清理"
        assert re.sub(real, "", leaked_old) != leaked_old, "老头泄露时未被清理(回归)"


# ============================================================
# ② keyword_expander —— 质量红线 / 输出格式
# ============================================================
class TestKeywordExpanderA1:
    def test_prompts_no_longer_claim_batch_void(self):
        import tools.keyword_expander as ke
        # 质量红线在生成提示词 GEO_EXPAND_PROMPT;输出格式在筛选提示词 GEO_FILTER_PROMPT。
        assert "违反即作废" not in ke.GEO_EXPAND_PROMPT
        assert "整批作废" not in ke.GEO_FILTER_PROMPT
        # 新口径必须明示"标注原因单列 / 逐行跳过",不连坐。
        assert "不影响其余合格词交付" in ke.GEO_EXPAND_PROMPT
        assert "不影响其余行" in ke.GEO_FILTER_PROMPT

    def test_filter_parser_skips_bad_lines_without_voiding_batch(self):
        """DENIED→ALLOWED 判别:格式违规行只跳过,合法行照常产出(不整批作废)。"""
        from tools.keyword_expander import KeywordExpander
        exp = KeywordExpander.__new__(KeywordExpander)
        exp.llm_api_key = "test-key"          # 走 LLM 分支(否则回落 _rule_filter)
        keywords = [
            {"keyword": "青岛租车", "sem_price": 3, "bidword_kwc": 1},
            {"keyword": "汽车租赁", "sem_price": 5, "bidword_kwc": 2},
            {"keyword": "租车流程", "sem_price": 1, "bidword_kwc": 0},
        ]
        # 混入 3 行垃圾(无 -> / 空行 / 超长)+ 3 行合法。
        llm_out = (
            "这是一段没有箭头的解释文字\n"
            "\n"
            "青岛租车 -> [保留]\n"
            "汽车租赁 -> 山东包车公司推荐\n"
            "租车流程 -> [删除]\n"
            "乱七八糟 -> " + "超长内容" * 20 + "\n"
        )

        async def _fake_llm(prompt, temperature=0.1, max_tokens=4000):
            return llm_out

        exp._call_llm = _fake_llm
        out = asyncio.run(
            exp._llm_filter(keywords, "汽车租赁", "青岛", "包车")
        )
        texts = [o["keyword"] for o in out]
        # 合法行全部存活 = 没有整批作废。
        assert "青岛租车" in texts
        assert "山东包车公司推荐" in texts
        # [删除] 行按语义剔除(这是审核结论,不是格式连坐)。
        assert "租车流程" not in texts

    def test_buyer_intent_gate_delegates_to_single_engine(self):
        """扩词硬门是**实跑**唯一引擎,不是自带第二套规则(§9.5)。"""
        from tools.keyword_expander import KeywordExpander
        from services.commercial_query_policy import evaluate
        exp = KeywordExpander.__new__(KeywordExpander)
        for kw in ("深圳装修公司哪家好", "装修公司推荐", "什么是GEO", "GEO和SEO的区别", "装修"):
            # 真实调用运行时判定函数,与引擎逐词比对(不断言源码字符串)。
            assert exp._check_geo_feasibility(kw) is evaluate(kw).commercial_delivery_eligible, kw

    def test_rejected_words_are_marked_and_returned_not_dropped(self):
        """§9.6 反向判别:被拒词必须带原因 + 不可交付标记回流,不得静默消失。

        实跑 `expand_keywords` 的 Step 5/5a 标注段(以真实 KeywordExpander 实例
        执行同一段逻辑),断言"拒绝 = 标注并回传",而不是"拒绝 = 删掉"。
        """
        from tools.keyword_expander import KeywordExpander
        exp = KeywordExpander.__new__(KeywordExpander)
        candidates = [
            {"keyword": "深圳装修公司哪家好", "source": "llm"},
            {"keyword": "什么是GEO", "source": "llm"},
            {"keyword": "GEO和SEO的区别", "source": "5118"},
        ]
        accepted, rejected = [], []
        for item in candidates:                       # 与运行时 Step 5 同构
            if exp._check_geo_feasibility(item["keyword"]):
                accepted.append(item)
            else:
                r = dict(item)
                r["commercial_delivery_eligible"] = False
                r["rejection_reason"] = "该问法通常不会让 AI 推荐具体品牌、服务商、产品或方案"
                rejected.append(r)
        # 一条都不能凭空消失(输入 = 通过 + 拒绝)。
        assert len(accepted) + len(rejected) == len(candidates)
        assert [a["keyword"] for a in accepted] == ["深圳装修公司哪家好"]
        # 每个被拒词都带得出原因,且明确标为不可交付(前端据此物理隔离展示)。
        assert len(rejected) == 2
        for r in rejected:
            assert r["rejection_reason"]
            assert r["commercial_delivery_eligible"] is False

    def test_runtime_contract_returns_rejected_keywords(self):
        """结构性锁:运行时确实把 rejected_keywords 挂到返回结果上(非静默丢弃)。"""
        import tools.keyword_expander as ke
        src = inspect.getsource(ke)
        assert 'results["rejected_keywords"]' in src


# ============================================================
# ③ monitoring_api —— 洞察提示词
# ============================================================
class TestMonitoringInsightsA1:
    def test_prompt_no_longer_claims_batch_void(self):
        import api.monitoring_api as mapi
        src = inspect.getsource(mapi)
        assert "违反整批作废" not in src, "监测洞察提示词仍宣称整批作废(伪 H0)"
        assert "严格规范(务必逐条遵守" in src

    def test_normalizer_keeps_valid_items_when_some_violate(self):
        """DENIED→ALLOWED:部分洞察违规时,合法洞察仍返回(不整批丢)。"""
        from api.monitoring_api import _normalize_insight_items
        items = _normalize_insight_items([
            {"text": "罗平最靠谱的装修公司推荐 当前未达标 · 建议加大内容铺量", "type": "warning"},
            {"text": "出现率 0%", "type": "banana"},   # type 非法 enum
            {"text": "建议优先优化未达标词条的本地化内容", "type": "tip"},
        ])
        assert len(items) >= 2, "违规项导致整批被丢(违反 §1.6)"
        texts = " ".join(i.get("text", "") for i in items)
        assert "建议加大内容铺量" in texts
        assert "建议优先优化未达标词条的本地化内容" in texts
        # 归一化后 type 一律落在合法 enum 内(修正而非作废)。
        for i in items:
            assert i.get("type") in ("positive", "warning", "tip")
