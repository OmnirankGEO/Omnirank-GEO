"""tests/test_social_pricing_ssot_align.py

v1.7.6.3 P0 紧急 · 社媒定价 SSOT 对齐 防回归 test 锁
(2026-05-24 老板拍板 · migration_015_social_pricing_ssot_align.sql)

真因(防回归):
  .planning/phases/07-social-studio-subscription/PRICING_DECISIONS.md 锁 capability 9 档
  · SubscriptionPlansPage.tsx:685 公开承诺 "¥0.04-¥15"
  · 但 feature_pricing 表从未按 SSOT UPDATE · 实扣走 GEO 老表 04-11 align 旧值
  · script_gen 实扣 ¥5 vs 用户预期 ¥0.31 = 差 16 倍 · 消费者保护红线 · MCN beta hold

修法 SSOT 9 档(锁死):
  轻量 5: profile_polish / ai_coach / comment_gen / dm_gen / scenario_gen / corpus_text / corpus_upload / task_route
  专业 40: script_gen / hook_gen / brand_fill / personality_refresh / industry_brief_rerun
  超级/组合 80: topic_gen / hook_script_combo
  视频分钟 40/min: video_asr (配套 BILLING_FALLBACK_CODE_MAP['video_asr']='video_asr')
  联网 10: web_search(v1.7.6.3.1 P0 Codex 修)· find_trending=0 是独立免费"找热点"·不再承接 web_search 计费
  拆视频短 390: single_video / video_framework / learn_viral / content_review
  拆博主短 1950: author_breakdown / team_portrait
  保持: rewrite_gen=650 / social_diagnosis=650 / deep_analyze=500

防回归点:
  1. PRICING_DATA seed (db/wallet_db.py) 含正确价
  2. hardcoded fallback (middleware/subscription_billing.py) 含正确价
  3. FE 兜底 (frontend/src/pages/Pricing/PricingPage.tsx) 含正确价(grep source · 不跑 build)
  4. BILLING_FALLBACK_CODE_MAP['video_asr'] = 'video_asr'(暗雷 · 老板审核重点)
  5. agent text 文案改新价(LLM 看的 prompt + confirm card)
"""
from __future__ import annotations

from pathlib import Path


# ============================================================
# SSOT 9 档锁(中央表 · test 1-N 都对这个表)
# ============================================================

PRICING_SSOT_2026_05_24 = {
    # 轻量 5
    "profile_polish": 5,
    "ai_coach": 5,
    "comment_gen": 5,
    "dm_gen": 5,
    "scenario_gen": 5,
    "corpus_text": 5,
    "corpus_upload": 5,
    "task_route": 5,
    # 专业 40
    "script_gen": 40,
    "hook_gen": 40,
    "brand_fill": 40,
    "personality_refresh": 40,
    "industry_brief_rerun": 40,
    # 超级/组合 80
    "topic_gen": 80,
    "hook_script_combo": 80,
    # 视频分钟 40/分钟
    "video_asr": 40,
    # 联网 10(v1.7.6.3.1 P0 · Codex 审核加)
    "web_search": 10,
    # 拆视频短 390
    "single_video": 390,
    "video_framework": 390,
    "learn_viral": 390,
    "content_review": 390,
    # 拆博主短 1950
    "author_breakdown": 1950,
    "team_portrait": 1950,
    # 保持
    "rewrite_gen": 650,
    "social_diagnosis": 650,
    "deep_analyze": 500,
}


# ============================================================
# Test 1 · db/wallet_db.py PRICING_DATA seed 价对 SSOT
# ============================================================


def test_seed_pricing_data_aligns_with_ssot():
    """PRICING_DATA seed 内每个 SSOT feature_code 价必对"""
    import re

    src = Path("db/wallet_db.py").read_text(encoding="utf-8")
    pricing_start = src.find("PRICING_DATA = [")
    assert pricing_start > 0, "测试自检:必能找到 PRICING_DATA = [ 锚点"
    pricing_end = src.find("    ]", pricing_start)
    assert pricing_end > pricing_start
    pricing_block = src[pricing_start:pricing_end]

    # 抽出所有 ('feature_code', '...', NNN, ..., ...)
    # PRICING_DATA 用对齐空格 · regex 容忍多空格
    pattern = re.compile(
        r"\(\s*'([a-z_]+)'\s*,\s*'[^']*'\s*,\s*(\d+)\s*,",
        re.MULTILINE,
    )
    matches = dict(pattern.findall(pricing_block))

    fails = []
    for code, expected in PRICING_SSOT_2026_05_24.items():
        # video_asr 走 seed 可选 · 不在 seed 也 OK(走 upsert / migration)
        # 但既然 v1.7.6.3 已加入 seed 也要对
        if code in matches:
            actual = int(matches[code])
            if actual != expected:
                fails.append(f"  {code}: seed={actual} · SSOT={expected}")
        elif code in ("video_asr", "industry_brief_rerun", "content_review", "web_search"):
            # 这 4 个走 upsert(line 320/360+ 单独 INSERT)· seed 没也接受
            # v1.7.6.3.1 加 web_search · 既 seed 也 upsert 都接受
            continue
        else:
            fails.append(f"  {code}: 缺 seed · SSOT={expected}")

    assert not fails, "PRICING_DATA seed 跟 SSOT 不一致:\n" + "\n".join(fails)


def test_seed_upserts_align_with_ssot():
    """db/wallet_db.py 三个强 upsert(industry_brief_rerun / content_review) 价必对 SSOT"""
    src = Path("db/wallet_db.py").read_text(encoding="utf-8")
    # industry_brief_rerun upsert: 40
    assert "VALUES ('industry_brief_rerun', '知识库字段重跑（AI）', 40" in src, (
        "v1.7.6.3 SSOT · industry_brief_rerun upsert 必为 40"
    )
    # content_review upsert: 390
    assert "VALUES ('content_review', '链接内容复盘', 390" in src, (
        "v1.7.6.3 SSOT · content_review upsert 必为 390"
    )


# ============================================================
# Test 2 · middleware/subscription_billing.py
# ============================================================


def test_hardcoded_fallback_aligns_with_ssot():
    """hardcoded fallback dict 跟 SSOT 关键 feature_code 一致"""
    src = Path("middleware/subscription_billing.py").read_text(encoding="utf-8")
    hc_idx = src.find("hardcoded = {")
    assert hc_idx > 0
    hc_end = src.find("}", hc_idx)
    hc_block = src[hc_idx:hc_end + 1]

    # 关键 feature_code 必含正确价
    critical = {
        "profile_polish": 5,
        "script_gen": 40,
        "hook_gen": 40,
        "topic_gen": 80,
        "hook_script_combo": 80,
        "video_asr": 40,
        "web_search": 10,  # v1.7.6.3.1 P0 · Codex 审核加(SSOT 联网档)
        "single_video": 390,
        "video_framework": 390,
        "content_review": 390,
        "author_breakdown": 1950,
        "team_portrait": 1950,
        "rewrite_gen": 650,
        "social_diagnosis": 650,
    }
    fails = []
    for code, expected in critical.items():
        if f'"{code}": {expected}' not in hc_block:
            fails.append(f"  {code}: 期望 {expected} 不在 hardcoded fallback dict")
    assert not fails, "hardcoded fallback 跟 SSOT 不一致:\n" + "\n".join(fails)


def test_video_asr_billing_fallback_map_breaks_old_alias():
    """⚠ 老板审核重点(2026-05-24)· video_asr 暗雷

    旧 BILLING_FALLBACK_CODE_MAP['video_asr'] = 'single_video' 在 single_video=390 后
    会让 video_asr 也变 390/分钟(不是 40/分钟)· 必须断开映射 · 让 video_asr 自查表
    """
    src = Path("middleware/subscription_billing.py").read_text(encoding="utf-8")
    # 必有 video_asr → video_asr(自映射 · 不映射到 single_video)
    assert '"video_asr": "video_asr"' in src, (
        "v1.7.6.3 P0 SSOT · BILLING_FALLBACK_CODE_MAP['video_asr'] 必断 single_video 旧映射 · 改成 'video_asr' 自查表"
    )
    # 反向断言:不能再有 video_asr → single_video 旧映射
    assert '"video_asr": "single_video"' not in src, (
        "v1.7.6.3 P0 SSOT · 必删旧 video_asr → single_video 映射(否则 single_video=390 后 video_asr 也变 390/分钟)"
    )


def test_web_search_billing_fallback_map_breaks_find_trending_alias():
    """⚠ Codex 审核重点(v1.7.6.3.1 P0 · 2026-05-24)· web_search 暗雷

    旧 BILLING_FALLBACK_CODE_MAP['web_search'] = 'find_trending' 在 find_trending=0 时
    会让 web_search 超量 fallback 仍 0 扣 · 跟 SSOT '联网 10' 冲突
    必须断映射 · 让 web_search 自查表(seed/migration 已 upsert web_search=10)
    find_trending=0 保留(产品要的免费'找热点')· 不再承接 web_search 计费
    """
    src = Path("middleware/subscription_billing.py").read_text(encoding="utf-8")
    assert '"web_search": "web_search"' in src, (
        "v1.7.6.3.1 P0 · BILLING_FALLBACK_CODE_MAP['web_search'] 必断 find_trending 旧映射 · 改成 'web_search' 自查表"
    )
    assert '"web_search": "find_trending"' not in src, (
        "v1.7.6.3.1 P0 · 必删旧 web_search → find_trending 映射(否则联网超量 fallback 0 扣)"
    )


def test_content_api_video_asr_precheck_fallback_uses_40():
    """⚠ Codex 审核 P1(v1.7.6.3.1 · 2026-05-24)· content_api video_asr precheck fallback

    api/content_api.py:11280 chat_attachment video_asr 余额预检
    DB 失败时 fallback 之前是 390(single_video 价)· 必须改 40(SSOT 视频分钟档)
    防 DB/缺行 fallback 脱节(防 DB 挂时算价错)
    """
    src = Path("api/content_api.py").read_text(encoding="utf-8")
    # 找 video_asr precheck 段
    idx = src.find('if feature_code == "video_asr" and check_minutes > 1:')
    assert idx > 0, "测试自检:必能找到 video_asr precheck 段"
    block = src[idx : idx + 800]
    # except fallback 不能是 390
    assert "base = 390" not in block, (
        "v1.7.6.3.1 P1 · video_asr precheck except fallback 必去掉 390(应为 40)"
    )
    # 必有 base = 40 或基于 get_feature_pricing 拿 video_asr 价
    assert "base = 40" in block, (
        "v1.7.6.3.1 P1 · video_asr precheck except fallback 必为 40(SSOT 视频分钟档)"
    )


# [开源 E3 · B2 · 2026-09-28] agents/social_agent.py 随 E3 删除,守它 prompt / 确认卡价格副本的 3 格退役。


# ============================================================
# Test 3 · frontend/src/pages/Pricing/PricingPage.tsx 兜底
# ============================================================


def test_frontend_fallback_pricing_aligns_with_ssot():
    """FE PricingPage 兜底数据跟 SSOT 关键 feature_code 一致"""
    src = Path("frontend/src/pages/Pricing/PricingPage.tsx").read_text(encoding="utf-8")
    pricing_idx = src.find("const PRICING_DATA: PricingItem[] = [")
    assert pricing_idx > 0
    pricing_end = src.find("];", pricing_idx)
    block = src[pricing_idx:pricing_end]

    critical = {
        "topic_gen": 80,
        "script_gen": 40,
        "hook_script_combo": 80,
        "single_video": 390,
        "video_framework": 390,
        "content_review": 390,
        "video_asr": 40,
        "web_search": 10,           # v1.7.6.3.2 (Codex 审 P1):防 FE 兜底再漂
        "social_diagnosis": 650,    # v1.7.6.3.2 (Codex 审 P1):防 FE 兜底 930 漂回
        "author_breakdown": 1950,
        "team_portrait": 1950,
        "profile_polish": 5,
        "ai_coach": 5,
        "rewrite_gen": 650,
    }
    fails = []
    for code, expected in critical.items():
        # FE 写法:`feature_code: 'topic_gen',  ... cost_points: 80`
        # 找到 feature_code 行 · 提取 cost_points
        code_idx = block.find(f"feature_code: '{code}'")
        if code_idx < 0:
            fails.append(f"  {code}: FE 兜底缺")
            continue
        line_end = block.find("\n", code_idx)
        line = block[code_idx:line_end]
        marker = f"cost_points: {expected},"
        if marker not in line:
            fails.append(f"  {code}: FE 兜底 cost_points 不是 {expected}(line: {line[:120]!r})")

    assert not fails, "FE PRICING_DATA 兜底跟 SSOT 不一致:\n" + "\n".join(fails)


# ============================================================
# Test 4 · 三处(seed / hardcoded fallback / FE 兜底)价交叉一致
# ============================================================


def test_three_sources_agree_on_critical_prices():
    """三处兜底价(seed / hardcoded / FE)对关键 feature_code 必一致

    防回归:之前的 BUG 就是三处脱节(老板 commit 改了 PLAN 决策但 DB seed / FE 兜底没同步)
    """
    seed_src = Path("db/wallet_db.py").read_text(encoding="utf-8")
    bill_src = Path("middleware/subscription_billing.py").read_text(encoding="utf-8")
    fe_src = Path("frontend/src/pages/Pricing/PricingPage.tsx").read_text(encoding="utf-8")

    # 5 个最关键 feature_code · 三源同时含正确价
    critical = {
        "script_gen": 40,
        "topic_gen": 80,
        "single_video": 390,
        "author_breakdown": 1950,
        "profile_polish": 5,
    }
    fails = []
    for code, expected in critical.items():
        # seed 用 PRICING_DATA tuple
        seed_pat = f"'{code}',"
        if seed_pat not in seed_src:
            fails.append(f"  {code}: seed 缺")
            continue
        # hardcoded 用 dict 字面量
        if f'"{code}": {expected}' not in bill_src:
            fails.append(f"  {code}: hardcoded fallback 缺 {expected}")
        # FE 用 feature_code: 'X' ... cost_points: N
        fe_idx = fe_src.find(f"feature_code: '{code}'")
        if fe_idx < 0:
            fails.append(f"  {code}: FE 兜底缺")
            continue
        fe_line = fe_src[fe_idx:fe_src.find("\n", fe_idx)]
        if f"cost_points: {expected}," not in fe_line:
            fails.append(f"  {code}: FE 兜底不是 {expected}")

    assert not fails, "三处兜底价不一致(SSOT drift):\n" + "\n".join(fails)


# ============================================================
# Test 5 · AI 助手 confirm card + system prompt 文案(助手文件已随开源 E3 B2 删,三格退役见上)
# ============================================================


# ============================================================
# Test 6 · migration_015 SQL 存在 + 用 UPSERT 不是 UPDATE
# ============================================================


def test_migration_015_uses_upsert_not_update():
    """老板审核重点(2026-05-24):必须 UPSERT 不能 UPDATE

    UPDATE 在老库缺 video_asr / industry_brief_rerun 时静默 0 行(不报错)· 部署后继续旧价
    """
    p = Path("scripts/migration_015_social_pricing_ssot_align.sql")
    assert p.exists(), "v1.7.6.3 · migration_015 SQL 必存在"
    src = p.read_text(encoding="utf-8")
    # 必有 ON CONFLICT DO UPDATE(UPSERT)
    assert "ON CONFLICT (feature_code) DO UPDATE" in src, (
        "v1.7.6.3 P0 · migration_015 必用 UPSERT(ON CONFLICT DO UPDATE)"
    )
    # 反向:不能用裸 UPDATE feature_pricing(老库缺行会静默 0 行)
    assert "UPDATE feature_pricing SET cost_points" not in src, (
        "v1.7.6.3 P0 · 不能用裸 UPDATE · 必须 UPSERT(老库缺行 video_asr 会静默 0 行)"
    )
    # 关键 feature_code 在 migration SQL 内
    for code in ("script_gen", "topic_gen", "video_asr", "author_breakdown", "profile_polish"):
        assert f"'{code}'" in src, f"v1.7.6.3 · migration_015 必含 {code}"


def test_migration_015_upserts_keep_set_not_just_comment():
    """v1.7.6.3.3 (Codex 三审 2026-05-24)· "保持"三项必 UPSERT 不能只注释

    真因:旧 migration 只写"保持 rewrite_gen=650/social_diagnosis=650/deep_analyze=500"注释
    · prod DB 这 3 项若被人改飘过 · migration 不会修 · 跟"用 migration 消灭 DB 旧价"冲突
    · Deploy-CTO 跑验证 SQL 时 social_diagnosis 期望 650 · 若 prod 漂到 930 验证就 FAIL
    修法:即使值不变 · 也强 UPSERT 保 prod 真值 = SSOT
    """
    from pathlib import Path
    src = Path("scripts/migration_015_social_pricing_ssot_align.sql").read_text(encoding="utf-8")
    # 3 个"保持"项必出现在 INSERT VALUES 段(不是注释)
    # 简单判:必含 ('rewrite_gen', ... 650 / ('social_diagnosis', ... 650 / ('deep_analyze', ... 500
    keep_set = {
        "rewrite_gen": 650,
        "social_diagnosis": 650,
        "deep_analyze": 500,
    }
    fails = []
    for code, price in keep_set.items():
        # 必有 ('code', '...', NNN, 这种 INSERT 行(不是 SQL 注释 -- 开头)
        # 检查方式:每行扫一下 · 找到 ('code' 的非注释行
        found_in_insert = False
        for line in src.split("\n"):
            stripped = line.strip()
            if stripped.startswith("--"):
                continue  # SQL 注释行 · 不算
            if f"('{code}'" in line and str(price) in line:
                found_in_insert = True
                break
        if not found_in_insert:
            fails.append(f"  {code}={price}: 必出现在 INSERT VALUES 段(不能只注释)")

    assert not fails, (
        "v1.7.6.3.3 P1 · migration_015 '保持'三项必 UPSERT 不能只注释:\n"
        + "\n".join(fails)
    )

    # 验证 SQL 注释也必同步含 social_diagnosis / deep_analyze
    assert "social_diagnosis" in src and "deep_analyze" in src, (
        "v1.7.6.3.3 P1 · migration_015 末尾验证 SQL 注释必含 social_diagnosis / deep_analyze"
    )
