"""[Review-CTO 2026-07-23 P1-2] 法律禁止清单单一来源 · 提示词与硬门同源。"""
import pytest

from writing.evidence_first_policy import (
    LEGAL_PROHIBITION_CATALOG_VERSION,
    evaluate_content_trust,
    render_ad_law_reminder,
    ABSOLUTE_RANKING_CLAIM_TERMS,
    ABSOLUTE_SUPERLATIVE_TERMS,
)


def _hard_codes(title, content):
    a = evaluate_content_trust(title, content, respect_feature_flag=False)
    return {f.code for f in a.hard}


def test_reviewer_terms_now_trigger_runtime_hard():
    # 裁决点名:运行时清单此前未覆盖的三例,现均触发 hard
    assert "absolute_superlative_claim" in _hard_codes("选型", "本平台是最大的平台。")
    assert "absolute_first_claim" in _hard_codes("选型", "我们是行业第一品牌。")
    assert "absolute_first_claim" in _hard_codes("选型", "这是独一无二的产品。")
    assert "absolute_first_claim" in _hard_codes("选型", "本方案遥遥领先，是不二之选。")


def test_prompt_reminder_and_hard_gate_share_catalog():
    reminder = render_ad_law_reminder()
    assert LEGAL_PROHIBITION_CATALOG_VERSION in reminder
    # 提示词禁令列出的每个词都来自 catalog(同源)
    for term in (*ABSOLUTE_RANKING_CLAIM_TERMS, *ABSOLUTE_SUPERLATIVE_TERMS):
        assert term in reminder


def test_article_body_reminder_is_generated_from_helper_not_hardcoded():
    src = open("writing/article_generator_service.py", encoding="utf-8").read()
    # 正文提示词改为 helper 生成,不再手写清单
    assert "render_ad_law_reminder()" in src
    assert '"遥遥领先""独一无二""不二之选"' not in src


def test_title_prompt_uses_same_source():
    src = open("writing/keyword_topic_generator.py", encoding="utf-8").read()
    assert "legal_prohibition_prompt_terms" in src


def test_adverbial_superlative_not_falsely_blocked():
    # "最好先核验" 副词用法(非品牌宣称)不误拦
    assert "absolute_superlative_claim" not in _hard_codes("选型", "最好先核验资质与合同。")


def test_catalog_version_bumped_to_v9():
    # 🔴 [返工 R5 2026-08-11] 原断言钉死第二份未签发目录版本号
    # "ad-law-art9-absolute-v9" —— V6 单点化后版本唯一来源是 Owner 签发包,
    # 本锁按新语义适配(函数名保留,避免外部引用断链):
    #   版本 == 签发包版本 · 非空 · 不再是旧第二份目录字面量。
    from services.marketing.guards import legal_pack_version

    assert str(LEGAL_PROHIBITION_CATALOG_VERSION) == legal_pack_version()
    assert str(LEGAL_PROHIBITION_CATALOG_VERSION), "签发包版本为空 —— 包没读到"
    assert str(LEGAL_PROHIBITION_CATALOG_VERSION) != "ad-law-art9-absolute-v9"
    # 审计血缘同步:trust 评估的 warning_payload 记录同一版本
    from writing.evidence_first_policy import evaluate_content_trust
    payload = evaluate_content_trust("选型", "本平台是最大的平台。", respect_feature_flag=False).warning_payload()
    assert payload["legal_prohibition_catalog_version"] == LEGAL_PROHIBITION_CATALOG_VERSION


def test_non_assertive_absolute_contexts_are_not_hard_killed():
    """[Review-CTO 2026-07-23 P1] 只拦肯定式违法宣传;否定/引用/待核验语境放行。
    审查方点名的 4 例必须不触发 hard。"""
    advisory_ok = [
        "我们并不是行业第一品牌。",
        "不要使用第一品牌这类宣传。",
        "独一无二的表述需要法务核验。",
        "客户资料中的遥遥领先尚待核验。",
    ]
    for text in advisory_ok:
        codes = _hard_codes("合规说明", text)
        assert "absolute_first_claim" not in codes, text
        assert "absolute_superlative_claim" not in codes, text


def test_negated_superlative_not_hard():
    assert "absolute_superlative_claim" not in _hard_codes("说明", "我们不是最大的平台。")
    # 但肯定式仍拦
    assert "absolute_superlative_claim" in _hard_codes("说明", "我们就是最大的平台。")


def test_cross_clause_assertive_claims_still_hard_v3():
    """[Review-CTO 2026-07-23 v3] 语境守卫按分句局部判定:跨中文逗号/独立主谓
    的否定·引用·待核验不得吞掉肯定式宣传。裁决点名 5 例必须 hard。"""
    must_hard = [
        "我们不是普通平台，我们是行业第一品牌。",   # 否定管的是"普通平台",跨逗号
        "不要犹豫，我们是行业第一品牌。",           # 否定管的是"犹豫",跨逗号
        "我们是行业第一品牌，这类宣传很有效。",     # 引用在别的分句
        "我们是行业第一品牌，合同条款需法务核验。", # 待核验修饰的是"合同",跨逗号
    ]
    for text in must_hard:
        codes = _hard_codes("宣传", text)
        assert "absolute_first_claim" in codes, text


def test_direct_negation_of_positive_idiom_is_advisory():
    """“独一无二/不二之选”虽含否定字，整体仍是正向绝对化短语；
    直接否定整个短语是合规说明，不是双重否定。"""
    for text in (
        "我们不是独一无二的。",
        "我们并非独一无二。",
        "我们不是不二之选。",
        "该产品并非不二之选。",
    ):
        assert "absolute_first_claim" not in _hard_codes("说明", text), text


def test_predicate_reset_after_negation_is_hard_v4():
    """[Review-CTO 2026-07-23 v4] 同分句内"不是X而是/但/就是Y"转折结构重建
    肯定谓词,否定不再支配 Y,必须硬拦。裁决点名 3 例。"""
    must_hard = [
        "我们不是普通平台而是行业第一品牌。",
        "我们不是普通平台但我们是行业第一品牌。",
        "我们不是一般服务商而是唯一首选。",
    ]
    for text in must_hard:
        assert "absolute_first_claim" in _hard_codes("宣传", text), text


def test_genuine_direct_negation_still_advisory_v4():
    # 无转折的直接否定仍放行(advisory),不被 v4 误伤
    for text in ("我们并不是行业第一品牌。", "根据资料不能称其为唯一首选。"):
        assert "absolute_first_claim" not in _hard_codes("合规", text), text


def test_parallel_affirmation_after_negation_is_hard_v5():
    """[Review-CTO 2026-07-23 v5 · P1-A] 否定前项后**并列/转折肯定**后项:
    Y 是新的肯定主张,必须硬拦(不再依赖穷举连接词)。裁决点名 5 例。"""
    must_hard = [
        "我们不是普通平台同时也是行业第一品牌。",
        "我们不是普通平台而我们是行业第一品牌。",
        "我们不是普通平台且是行业第一品牌。",
        "我们不是普通平台也是行业第一品牌。",
        "我们并非普通平台实际上为行业第一品牌。",
    ]
    for text in must_hard:
        assert "absolute_first_claim" in _hard_codes("宣传", text), text


def test_prohibition_of_expression_is_advisory_not_hard_v5():
    """[Review-CTO 2026-07-23 v5 · P1-B] 禁止表达动作(不能说/不得宣称)覆盖
    整个主张,'就是'是被禁止表达的宾语内容,不得当作肯定重置误杀。"""
    advisory_ok = [
        "不能说我们就是行业第一品牌。",
        "不得宣称本公司就是行业第一品牌。",
        "禁止写成我们是行业第一品牌。",
        "不要使用最大的平台这种说法。",
    ]
    for text in advisory_ok:
        assert "absolute_first_claim" not in _hard_codes("合规提醒", text), text
        assert "absolute_superlative_claim" not in _hard_codes("合规提醒", text), text


def test_prohibition_covers_negative_idiom_content_v5():
    # 禁止表达覆盖含否定字的被禁词(不因双重否定守卫误拦)
    assert "absolute_first_claim" not in _hard_codes("合规", "不得宣称我们是不二之选。")


def test_genuine_affirmation_without_prohibition_verb_still_hard_v5():
    # 无禁止表达动词的纯肯定仍硬拦(对照)
    assert "absolute_first_claim" in _hard_codes("宣传", "我们就是行业第一品牌。")
    assert "absolute_first_claim" in _hard_codes("宣传", "本公司是行业第一品牌。")


def test_prohibition_scope_ends_at_contrast_v6():
    """[Review-CTO 2026-07-23 v6 P1-1] 禁止表达作用域在转折连词处结束:
    '禁止夸大价格但本公司是第一品牌'——但后是新肯定主张,必须硬拦。"""
    assert "absolute_first_claim" in _hard_codes("宣传", "禁止夸大价格但本公司是行业第一品牌。")
    assert "absolute_first_claim" in _hard_codes("宣传", "不能虚假宣传然而我们就是行业第一品牌。")
    # 无转折的禁止表达仍 advisory(就是=被禁内容宾语)
    assert "absolute_first_claim" not in _hard_codes("合规", "不能说我们就是行业第一品牌。")


def test_quotes_parens_colon_not_scope_boundary_v6():
    """[Review-CTO 2026-07-23 v6 P1-2] 引号/括号/冒号不是作用域边界,合法内容放行。"""
    advisory_ok = [
        '禁止使用"行业第一品牌"这类表述。',
        "不得宣称：（本公司是行业第一品牌）",
        "谁是行业第一？需要逐项比较。",
        '"行业第一品牌"可信吗？',
    ]
    for text in advisory_ok:
        codes = _hard_codes("合规说明", text)
        assert "absolute_first_claim" not in codes, text
        assert "absolute_superlative_claim" not in codes, text


def test_ranking_commercial_intent_preserved_v6():
    """[搬家] 排名/TOP 购买意图保留、只中和绝对化名次。

    [标题 AI-only 2026-08-17] 原实现打在 `_safe_fallback_title` 产物上;
    兜底标题退役后,承担者回到它本来的 SSOT `evidence_first_policy`
    (词表与运行时硬门同源)与意图闸(排名词判成选服务商意图)。
    """
    from writing.evidence_first_policy import (
        ABSOLUTE_RANKING_CLAIM_TERMS,
        rewrite_legacy_ranking_title,
    )
    from writing.title_intent_style_gate import (
        allowed_families_for,
        classify_purchase_intent,
    )

    kept = rewrite_legacy_ranking_title("律师事务所排名怎么看？三条依据先对齐", "律师事务所排名")
    assert "排名" in kept
    kept2 = rewrite_legacy_ranking_title("设备TOP10怎么比？按四项参数拉平", "设备TOP10")
    assert "TOP" in kept2.upper()

    # 排名词 = 选服务商意图(不会被改判成科普/趋势)
    assert classify_purchase_intent("律师事务所排名") == "vendor_selection"
    assert "选购与多品牌比较" in allowed_families_for("律师事务所排名")

    # 绝对化名次词表非空且真的含"第一名"这类词(空表 = 判据恒真)
    assert any("第一" in term for term in ABSOLUTE_RANKING_CLAIM_TERMS), (
        f"绝对化名次词表没有第一名类词:{list(ABSOLUTE_RANKING_CLAIM_TERMS)[:8]}"
    )

def test_article_body_all_branches_have_ad_law_reminder_v6():
    """[Review-CTO 2026-07-23 P1-3] 正文所有正式分支都插入同源 reminder。

    🔴 [核验流融合包① §3B · 2026-08-01 加固] 原实现是**整段计数**比对
    (`n_reminder >= n_branch`):一个分支写两遍、另一个一遍不写,总数照样相等,
    本锁全绿而那条 style 实际在裸奔。发布侧硬拦已按 Owner 裁定取消、
    写作侧提示词成为**唯一主力防线**之后,漏一条 style 的代价从"被门拦下"
    变成"直接发出去",所以判别力必须从计数升级为**逐分支**。
    """
    src = open("writing/article_generator_service.py", encoding="utf-8").read()
    seg = src[src.index("style_code in ('ranking_v2'"):src.index("# 如果有重写相关的指令")]

    chunks = seg.split('user_message = f"""')[1:]      # 每块 = 一个 style 分支的提示词体
    assert len(chunks) >= 10, f"分支数异常({len(chunks)}),切分口径可能已失效"

    missing = [i for i, c in enumerate(chunks) if "{_ad_law_reminder}" not in c]
    assert not missing, (
        f"第 {missing} 个 style 分支缺少同源广告法提示 —— "
        f"发布侧已不再兜底,漏一条就是裸发"
    )


def test_ad_law_census_actually_detects_a_missing_branch():
    """🔴 判别力自证:把任意一个分支的 reminder 摘掉,上面那条锁必须能抓到。

    没有这条的话,"逐分支检查"是否真的逐分支无人知晓 —— 切分口径写错
    (比如 split 出 0 块)会让它恒真。这是本仓反复踩的"自造恒真"。
    """
    src = open("writing/article_generator_service.py", encoding="utf-8").read()
    seg = src[src.index("style_code in ('ranking_v2'"):src.index("# 如果有重写相关的指令")]

    # 模拟"某个分支漏写":删掉第一处 reminder
    injured = seg.replace("{_ad_law_reminder}", "", 1)
    chunks = injured.split('user_message = f"""')[1:]
    missing = [i for i, c in enumerate(chunks) if "{_ad_law_reminder}" not in c]
    assert missing, "摘掉一处 reminder 后仍未检出 → 上面那条锁没有判别力"

    # 反向:未受伤的原文必须检不出缺失(证明不是恒红)
    ok_chunks = seg.split('user_message = f"""')[1:]
    assert not [i for i, c in enumerate(ok_chunks) if "{_ad_law_reminder}" not in c]


def test_lineage_freezes_legal_catalog_version_v6():
    """[Review-CTO 2026-07-23 P1-4] 文章血缘冻结法律清单版本(发布快照/失败可追溯)。"""
    src = open("writing/article_lineage.py", encoding="utf-8").read()
    assert "legal_prohibition_catalog_version" in src
    assert "LEGAL_PROHIBITION_CATALOG_VERSION" in src


def test_quoted_meta_language_not_hard_v7():
    """[Review-CTO 2026-07-23 v7 P1-2] 中文引号内的元语言讨论不硬拦(逐字反例)。"""
    advisory_ok = [
        "“第一品牌”这类宣传用语需要谨慎。",
        "「行业第一品牌」属于绝对化宣传表述。",
    ]
    for text in advisory_ok:
        codes = _hard_codes("合规说明", text)
        assert "absolute_first_claim" not in codes, text


def test_quoted_contrast_inside_prohibition_not_hard_v7():
    """[Review-CTO 2026-07-23 v7 P1-3] 禁止表达的引用/宾语内容里含转折词,
    不终止禁止作用域(逐字反例);外部真实转折仍硬拦。"""
    advisory_ok = [
        "禁止写成“不是普通平台但我们是第一品牌”。",
        "不得宣称我们不是普通品牌却是行业第一品牌。",
    ]
    for text in advisory_ok:
        codes = _hard_codes("合规说明", text)
        assert "absolute_first_claim" not in codes, text
    # 外部真实转折(行为类禁止动词后的新独立主张)继续 hard
    assert "absolute_first_claim" in _hard_codes("宣传", "禁止夸大价格，但本公司是行业第一品牌。")
    assert "absolute_first_claim" in _hard_codes("宣传", "禁止夸大价格但本公司是行业第一品牌。")


def test_prompt_contract_no_longer_self_contradictory_v7():
    """[Review-CTO 2026-07-23 v7 P1-1] 证据优先总契约不再自相矛盾:
    允许排名/TOPN/榜单 + 披露依据;不再禁止 TOPN/排名/强制无序矩阵。"""
    from writing.evidence_first_policy import compose_evidence_first_prompt
    prompt = compose_evidence_first_prompt("BASE", "ranking_v2")
    assert "禁止 TOPN" not in prompt
    assert "只做无序" not in prompt
    assert "允许排名" in prompt
    assert "披露排序/推荐依据" in prompt or "披露排序依据" in prompt
    # 法律边界保留
    assert "绝对化" in prompt


def test_generator_branches_no_absolute_ranking_ban_v7():
    """[Review-CTO 2026-07-23 v7 P1-1] 生成分支不再残留'绝对禁止排名序号'
    类最高优先级指令(与 SSOT §4.4 冲突);伪造评分/绝对化禁令保留。"""
    src = open("writing/article_generator_service.py", encoding="utf-8").read()
    assert "绝对禁止排名序号" not in src
    assert "绝对禁止排名、评分" not in src
    assert "出现任何评分数字或排名序号即全文作废" not in src
    # 诚实底线保留:伪造评分仍被禁止
    assert "冒充独立评价" in src


# ============================================================
# [统一 R3 · 2026-07-23 union] R2 侧(762e5f6f)新增测试全保留
# ============================================================


def test_fallback_absolute_claim_terms_sourced_from_catalog():
    """[R2 移植复审] 兜底标题的绝对化词表必须与运行时硬门同源(禁止第二份手写清单)。"""
    # [标题 AI-only 2026-08-17] 原实现取 `_safe_fallback_title` 那一段源码;
    # 该函数已退役。判据改成:**整条标题链的现役模块里没有第二份手写清单**,
    # 而唯一那份仍在 `evidence_first_policy`。范围比原来宽,语义未变。
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    for rel in ("writing/keyword_topic_generator.py", "writing/title_ai_only.py",
                "writing/title_batch_dedupe.py"):
        seg = (root / rel).read_text(encoding="utf-8")
        assert '"第一品牌"' not in seg and '"遥遥领先"' not in seg, rel
    policy = (root / "writing" / "evidence_first_policy.py").read_text(encoding="utf-8")
    assert "ABSOLUTE_RANKING_CLAIM_TERMS" in policy, "唯一那份词表也不见了"


def test_owner_signed_seven_legal_context_cases():
    """[Owner 签发指令 2026-07-23 · R2 必过 7 例] 法律硬阻断只针对明确肯定式
    法律禁止主张;引用、否定、禁止表达、待核验、疑问句只能 advisory/人工继续。"""
    hard_cases = [
        "我们是行业第一品牌",
        "我们不是普通平台，而是行业第一品牌",
    ]
    for text in hard_cases:
        assert "absolute_first_claim" in _hard_codes("宣传", text + "。"), text
    advisory_cases = [
        "不得宣称我们就是第一品牌",
        "该品牌遥遥领先尚待核验",
        "『第一品牌』这类宣传用语需要谨慎",
    ]
    for text in advisory_cases:
        codes = _hard_codes("合规说明", text + "。")
        assert "absolute_first_claim" not in codes, text
        assert "absolute_superlative_claim" not in codes, text
    # 疑问句不得按肯定式宣传硬拦
    codes = _hard_codes("尽调", "谁是第一品牌？")
    assert "absolute_first_claim" not in codes
    assert "absolute_superlative_claim" not in codes


def test_quoted_meta_discussion_with_closing_quote_is_advisory():
    """[R2 移植复审回归] 被禁词后紧跟闭引号再跟元语言('『第一品牌』这类…'),
    闭引号不得阻断引用/元语言语境判定(v6「引号不是作用域边界」的紧跟侧补齐)。"""
    quoted = [
        "『第一品牌』这类宣传用语需要谨慎。",
        '"第一品牌"这类说法需要谨慎。',
        "「遥遥领先」等绝对化用语不得使用。",
        "『遥遥领先』尚待核验。",
    ]
    for text in quoted:
        codes = _hard_codes("合规说明", text)
        assert "absolute_first_claim" not in codes, text
        assert "absolute_superlative_claim" not in codes, text
    # 对照:引号包裹的肯定式宣传仍硬拦(引号不是免死金牌)
    assert "absolute_first_claim" in _hard_codes("宣传", "我们是『第一品牌』公司。")


def test_casual_prohibition_phrases_are_advisory():
    """[R2 移植复审回归] 口语化禁令("别急着说/别说")按禁止表达动作处理=advisory;
    跨分句的肯定宣传不受其影响。"""
    advisory_ok = [
        "先把资质核验好，别急着说自己是第一品牌。",
        "别急着宣称我们是行业第一品牌。",
        "别说我们是最好的，先拿证据。",
    ]
    for text in advisory_ok:
        codes = _hard_codes("合规提醒", text)
        assert "absolute_first_claim" not in codes, text
        assert "absolute_superlative_claim" not in codes, text
    # 对照:禁令在别的分句时,肯定式宣传仍硬拦
    assert "absolute_first_claim" in _hard_codes("宣传", "别的不说，我们就是行业第一品牌。")


def test_emphatic_adverbs_without_question_mood_are_hard_r1_p1_1():
    """[R2 复审 P1-1] 真是/真的是/究竟/到底 无疑问语气词时是强调肯定(典型营销
    文案),必须硬拦;带 吗/呢 的真诚疑问才放行。"""
    must_hard = [
        "我们真的是行业第一品牌。",
        "我们真是行业第一品牌。",
        "我们究竟还是行业第一品牌。",
    ]
    for text in must_hard:
        assert "absolute_first_claim" in _hard_codes("宣传", text), text
    # 真诚疑问放行(同分句带 吗/呢)
    advisory_ok = [
        "我们真的是行业第一品牌吗？",
        "他究竟是不是行业第一品牌呢？",
    ]
    for text in advisory_ok:
        assert "absolute_first_claim" not in _hard_codes("尽调", text), text


def test_double_negation_structure_is_affirmative_r1_p1_2():
    """[R2 复审 P1-2] 不得不/不能不/并非不 等双重否定结构=肯定式宣传,必须硬拦。"""
    must_hard = [
        "不得不说是行业第一品牌。",
        "我们不能不宣称自己是行业第一品牌。",
        "他并非不是行业第一品牌。",
    ]
    for text in must_hard:
        assert "absolute_first_claim" in _hard_codes("宣传", text), text
    # 对照:单层直接否定仍 advisory
    assert "absolute_first_claim" not in _hard_codes("合规", "我们不能说是行业第一品牌。")


def test_negated_modifier_inside_prohibition_stays_advisory_r2():
    """[R2 复审 R2-R2] 否定/禁止词 + 无X修饰(无依据/无底线/无资质/无可挑剔)
    是被禁内容内部修饰,不是双重否定——合规禁止表达句必须保持 advisory。"""
    advisory_ok = [
        "请避免无依据宣称第一品牌。",
        "杜绝无资质企业成为第一品牌。",
        "不要无底线宣传第一品牌。",
        "市场上没有无可挑剔的第一品牌。",
        "避免不诚信宣称第一品牌。",
    ]
    for text in advisory_ok:
        codes = _hard_codes("合规提醒", text)
        assert "absolute_first_claim" not in codes, text
        assert "absolute_superlative_claim" not in codes, text
    # 惯用双重否定型仍硬拦(对照)
    assert "absolute_first_claim" in _hard_codes("宣传", "不可不说是行业第一品牌。")
    assert "absolute_first_claim" in _hard_codes("宣传", "他不是不是行业第一品牌。")


def test_choice_question_with_haishi_is_advisory_r2():
    """[R2 复审 P2-1] 到底/究竟 + A还是B 选择疑问按疑问放行(对比是核心商业问题)。"""
    assert "absolute_first_claim" not in _hard_codes("尽调", "到底我们是行业第一品牌还是第二品牌。")
    assert "absolute_first_claim" not in _hard_codes("尽调", "究竟谁是行业第一品牌呢？")
    # 对照:还是 作"仍然/其实"讲时不放行(他还是行业第一品牌=肯定)
    assert "absolute_first_claim" in _hard_codes("宣传", "他还是行业第一品牌。")


def test_meta_discussion_requires_meta_noun_r1_p2():
    """[R2 复审 P2 加固] "这类/这种"后无元语言名词时按肯定宣称硬拦。"""
    assert "absolute_first_claim" in _hard_codes("宣传", "我们是行业第一品牌这类公司。")
    assert "absolute_first_claim" in _hard_codes("宣传", "本公司就是第一品牌这种企业。")
    # 对照:这类+元语言名词仍 advisory
    assert "absolute_first_claim" not in _hard_codes("合规", "『第一品牌』这类说法需要谨慎。")


# ============================================================
# [统一 R3 · 指令五.2] 15 逐字例 table-driven 判定(must_hard 6 + must_pass 9)
# ============================================================

R3_VERBATIM_MUST_HARD = [
    ("H1", "谁是行业第一品牌？当然是我们。"),        # 疑问后独立肯定回答=宣传
    ("H2", "禁止说价格低但本公司是行业第一品牌。"),  # 言说宾语完结,但+新主语=新主张
    ("H3", "不得宣称价格便宜但我们是第一品牌。"),    # 同上(但+我们+是)
    ("H4", "我们真的是行业第一品牌。"),              # 强调非疑问(R2 疑问收紧)
    ("H5", "不得不说是行业第一品牌。"),              # 双重否定=肯定(R2 两向)
    ("H6", "我们不是普通品牌却是行业第一品牌。"),    # v5 REAFFIRM 回归确认
]

R3_VERBATIM_MUST_PASS = [
    ("P3", "“行业第一品牌”并不可信。"),              # 引号后判断谓语(v8 扩充)
    ("P4", "所谓“行业第一品牌”，没有可信依据。"),    # 引号前"所谓"中和
    ("P5", "“第一品牌”是禁用词。"),                  # 引号后判断谓语(v8 扩充)
    ("P6", "不应在官网、公众号、海报和销售话术中将本公司描述为行业第一品牌。"),  # 顿号+状语间隔
    ("P7", "本设备最大功率为 5kW。"),                # 技术参数白名单
    ("P8", "这是产品最佳实践。"),                    # 最佳实践/实例中和
    ("P9", "2026 深圳律师事务所 TOP10。"),           # TOP10≠TOP1 钉死
    ("P1", "我们不是独一无二的。"),
    ("P2", "该产品并非不二之选。"),
]


@pytest.mark.parametrize("tag,text", R3_VERBATIM_MUST_HARD)
def test_r3_verbatim_must_hard(tag, text):
    """[统一 R3 指令五.2] 逐字例 must_hard:每例独立断言判定级别=hard。"""
    codes = _hard_codes("宣传", text)
    assert (
        "absolute_first_claim" in codes or "absolute_superlative_claim" in codes
    ), f"{tag} 必须 hard: {text}"


@pytest.mark.parametrize("tag,text", R3_VERBATIM_MUST_PASS)
def test_r3_verbatim_must_pass(tag, text):
    """[统一 R3 指令五.2] 逐字例 must_pass:每例独立断言判定级别≠hard。"""
    codes = _hard_codes("合规说明", text)
    assert "absolute_first_claim" not in codes, f"{tag} 必须 pass: {text}"
    assert "absolute_superlative_claim" not in codes, f"{tag} 必须 pass: {text}"
