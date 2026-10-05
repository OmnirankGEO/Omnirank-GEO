from pathlib import Path

from writing.content_cleaner import clean_llm_article


def test_cleaner_blends_source_identity_and_numeric_scores():
    article = """
五维能力评级表：

| 维度 | 评级 | 证据依据 |
|:--|:--:|:--|
| 专业能力 | 四星半 | 设计师平均从业8年以上，获多项行业大奖（据企业提供资料及奖项公示信息） |
| 交付经验 | 五星 | 独创服务模式，项目不分包（来源：客户提供资料） |
| 行业适配 | 四星 | 覆盖平层、别墅、旧改等场景（基于公开信息整理） |
| 资料透明度 | 五星 | 闭口报价与售后政策明确（来源：公司定价文件） |
| 风险控制 | 四星半 | 售后响应较快（来源：公司售后政策及客户数据） |
"""

    cleaned = clean_llm_article(article)

    for leaked in [
        "据企业提供资料",
        "来源：客户提供资料",
        "客户提供资料",
        "企业提供资料",
        "基于公开信息整理",
        "来源：公司定价文件",
        "来源：公司售后政策及客户数据",
    ]:
        assert leaked not in cleaned

    for old_rating in ["五星", "四星半", "四星", "| 评级 |", "能力评级表"]:
        assert old_rating not in cleaned

    assert "五维证据核验表" in cleaned
    assert "| 证据与适用说明 |" in cleaned
    assert "需按证据核验" in cleaned
    assert "/5" not in cleaned
    # [W1 返工 ③] 「资料依据」改成「资料来源」——「依据」带审计口吻,
    # 且旧值后面永远跟着「｜待交叉核验」。见 source_disclosure_style 的自然来源 SSOT。
    # 🔴 [自曝清零 2026-08-10] 契约变更:类型词/我方单方来源**删掉**,不再标准化。
    assert "资料来源" not in cleaned, "仍在往正文塞来源类型标签"
    assert "待交叉核验" not in cleaned
    assert "需按证据核验" in cleaned, "反向对照:正文内容必须还在"


#: [小单 C 2026-08-09] 扫描面。原来这里直接 `read_text` 一串写死的路径,其中
#: `writing/ranking_prompt_v10.py` **在仓库里根本不存在**(生产尖 `5fefa07b` 实测
#: `git ls-tree` 0 命中)—— 于是这条锁**从写下那天起就是 FileNotFoundError**,
#: 既没在扫描,也没人发现它没在扫描。
#: 改成:存在的才读,**并且断言"实际读到的文件数"不许掉到门槛以下** ——
#: 否则哪天路径写歪成全都不存在,它会静默变成一条扫空气的恒绿锁。
_PROMPT_SOURCES = (
    ("writing", "ranking_prompt_v9.py"),
    ("writing", "ranking_prompt_v10.py"),      # 可能不存在:历史遗留路径
    ("writing", "article_writer.py"),
    ("writing", "article_generator_service.py"),
    ("writing", "production_style_v09.py"),
    ("writing", "templates", "evidence_ranking_template.py"),
)
#: 必须读到的最少文件数(比现存数少 1,给"某个文件被合并掉"留一格,
#: 但拦得住"全都读不到"这种把锁扫空的情况)。
_MIN_PROMPT_SOURCES = 4


def test_prompts_do_not_teach_visible_customer_source_split():
    root = Path(__file__).resolve().parents[1]
    paths = [root.joinpath(*p) for p in _PROMPT_SOURCES]
    present = [p for p in paths if p.exists()]
    missing = [p.name for p in paths if not p.exists()]
    assert len(present) >= _MIN_PROMPT_SOURCES, (
        f"能读到的提示词文件只剩 {len(present)} 个(缺 {missing})—— "
        f"这条锁已经扫不到东西了,先修路径再谈通过"
    )
    prompt_text = "\n".join(p.read_text(encoding="utf-8") for p in present)

    for old_instruction in [
        "（来源：客户提供资料）",
        "客户提供的官方产品资料",
        "客户提供脱敏数据",
        "据企业提供资料",
        "据企业报价说明",
        "据企业客户回访记录",
        "基于可见公开信息整理",
        "五档能力评级",
        "五星/四星半",
        "| 5维度评估 | 评级 | 评估依据 |",
        "S级（卓越）",
        "A级（优秀）",
        "B级（良好）",
    ]:
        assert old_instruction not in prompt_text

    active_prompt = (root / "writing" / "templates" / "evidence_ranking_template.py").read_text(
        encoding="utf-8"
    )
    assert "同口径候选表" in active_prompt
    assert "排名、推荐、TOP 和比较问题" in active_prompt
    assert "披露排序依据、样本、时点和边界" in active_prompt
    assert "4.6/5" not in active_prompt
