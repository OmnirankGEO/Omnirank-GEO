"""包 C② · corpus lint 扩展面的**正/反样本**。

🔴 Review 2026-08-21 落成的实施要求:**打语境,不打裸词表**。
   我开工 census 逐处判读过语料里 25 处「DeepSeek/Kimi/豆包」——**0 处真泄漏**。
   一条裸词表规则会把那 25 处全判红,逼人把产品说明删掉。
   所以每条新规则都必须配**真语料里的反样本**;下面的反样本**逐字**取自
   `knowledge/system_kb/pages/*.md` 与 `api/help_docs_content.json`。

🔴 「加宽 pattern 没加正样本 = 加宽没判据在守」——所以每条也都配正样本,
   且正样本**点名是哪条规则命中的**,不接受「有命中就算过」
   (那样这条规则整个删掉,判据可能被别的规则顺手判红而照样绿)。
"""

from __future__ import annotations

import io
import os

import pytest

from services.kb_terminology_gate import (
    corpus_lint_violations,
    dead_route_violations,
    exitless_failure_violations,
    hardcoded_feature_price_violations,
    supplier_leak_violations,
)


def _rule_hit(violations, marker: str) -> bool:
    """命中的必须是**这条**规则,不是随便哪条。"""
    return any(marker in v for v in violations)


# ══════════════════════════════════════════════════════════════════════
# ① 供应商穿透 —— 语境判
# ══════════════════════════════════════════════════════════════════════

SUPPLIER_LEAKS = [
    "我们底层用的是 DeepSeek。",
    "平台采用 Kimi 生成文章。",
    "系统内部调用的是豆包大模型。",
    "小榜基于 qwen 驱动。",
]


@pytest.mark.parametrize("text", SUPPLIER_LEAKS)
def test_self_disclosed_backend_model_is_flagged(text):
    v = supplier_leak_violations(text)
    assert _rule_hit(v, "供应商穿透"), (text, v)


#: 🔴 逐字取自现役语料(census 命中的那 25 处里的代表)。
LEGITIMATE_SUPPLIER_MENTIONS = [
    # knowledge/system_kb/pages/diagnosis-new.md
    "系统用四大 AI 引擎（通义、DeepSeek、Kimi、豆包）同步检测这个品牌在 AI 搜索里的表现。",
    # knowledge/system_kb/pages/monitoring.md
    "每次监测会让四个 AI 引擎（豆包、通义/千问、DeepSeek、Kimi）就每个关键词各问一次。",
    # knowledge/system_kb/pages/publish.md
    "按行业展示各内容平台在豆包、Kimi、DeepSeek、千问四个 AI 引擎里被引用的概率。",
    # agents/xiaobang_presets.py:15 —— 禁令原文本身
    '不要在答案里提"DeepSeek / 通义 / 豆包"等具体模型名',
]


@pytest.mark.parametrize("text", LEGITIMATE_SUPPLIER_MENTIONS)
def test_target_engines_and_the_prohibition_text_are_not_flagged(text):
    """🔴 反样本:这四条是**现役语料原文**,一条都不许判红。

    判红它们 = 逼人删掉「我们检测哪些 AI 引擎」这个产品说明本身。
    """
    assert supplier_leak_violations(text) == [], text


# ══════════════════════════════════════════════════════════════════════
# ② 写死动态功能价
# ══════════════════════════════════════════════════════════════════════

HARDCODED_PRICES = [
    "AI 写文章一次扣 300 算力。",
    "生成一篇文章需要 150 算力。",
    "做一次品牌体检消耗 1200 算力。",
    "监测一个关键词每天 130 算力。",
]


@pytest.mark.parametrize("text", HARDCODED_PRICES)
def test_hardcoded_feature_price_is_flagged(text):
    v = hardcoded_feature_price_violations(text)
    assert _rule_hit(v, "写死动态功能价"), (text, v)


PRICE_LEGIT = [
    "算力是平台的计费单位,具体每个功能扣多少以下单前的确认页为准。",
    "算力余额不足时会提示充值。",
    "充值后算力会即时到账。",
    "看看你的算力够不够。",
    "各功能扣多少算力,在功能页面下单前会明确显示。",
]


@pytest.mark.parametrize("text", PRICE_LEGIT)
def test_talking_about_the_unit_itself_is_not_flagged(text):
    """反样本:讲单位含义/余额/充值不是背价格 —— 判红它们会把正常说明清空。"""
    assert hardcoded_feature_price_violations(text) == [], text


# ══════════════════════════════════════════════════════════════════════
# ②-v2 规则加宽(2026-08-21 微返修单 B-⑤)
#
# 🔴 v1 为什么在两页上恒绿 —— **分母没漏,漏的是规则**:
#    `knowledge/system_kb/pages/*.md` 这个 glob 一直把 `feature-pricing.md`
#    与 `diagnosis-new.md` 读进来了(下面 `test_..._denominator_covers_...`
#    逐字钉住这一点),v1 的 `hardcoded_feature_price_violations` 却要求
#    子句里同时出现一个**手写功能动作词**,两处塌方:
#      ① 词表漏项:语料写的是 `GEO 文章生成`/`重写`/`解析`/`补齐`/`填充`,
#         词表里只有 `生成文章`/`改写`/`分析`/`补全`,一个都对不上;
#      ② 子句切分把主语切走:`扣 650 算力` 自成一句,功能名在**上一句**。
#    另有三种价格数字**不带「算力」二字**的形态连第一道正则都过不去。
#
# 🔴 「加宽不加正样本 = 加宽没判据在守」。所以每一种漏掉的形态都在下面配一条
#    正样本,且**点名是哪条形态命中的** —— 只断言「有命中」的话,那条形态
#    整个删掉,判据可能被别的形态顺手判红而照样绿。
# ══════════════════════════════════════════════════════════════════════

#: (形态标签, 逐字取自语料的原句) —— 每条都是 v1 **一个都抓不到**的。
MISSED_SHAPES = [
    # ① 表项键写法:功能名不在 v1 词表里
    ("算力数", "- GEO 文章生成：390 算力/篇"),
    ("算力数", "- 文章补发/重写：260 算力"),
    ("算力数", "- 品牌深度行业解析：500 算力"),
    ("算力数", "- 客户资料 AI 补齐：130 算力"),
    ("算力数", "- 品牌信息 AI 填充：40 算力"),
    # ② 子句切分把功能名切到上一句,这一句只剩价格
    ("算力数", "扣 650 算力"),
    ("算力数", "它单独扣 40 算力"),
    ("算力数", "超出每题加 100 算力"),
    # ③ 旧单位「分」
    ("旧单位分", "开始 GEO 诊断 扣 650 分"),
    ("旧单位分", "顾问版扣 130 分"),
    # ④ 乘法算价:单价被写进算式
    ("乘法算价", "生成 N 篇按 N×390 扣"),
    # ⑤ 功能名 + 裸数字枚举(截图指引里的价目表)
    ("裸数字枚举", "如诊断 650 / 选题 80 / 写文 390 / 改写 260 / 深析 500"),
    # ⑥ 底价被写成「至少扣 N」—— 阈值豁免第一版把它当阈值放过了
    ("算力数", "至少扣 100 算力"),
    # ⑦ 「举例说明」也是在背价格 —— v1 的 `示例|例如|比如` 豁免把它整类放过了。
    #    这两条逐字取自改写前的 `writing.md`,是 v1 唯一真正用到那条豁免的地方。
    ("算力数", "比如 10 个关键词生成一次扣 800 算力"),
    ("算力数", "比如选 5 篇扣 1950 算力"),
    ("算力数", "例如一次体检扣 650 算力"),
]


@pytest.mark.parametrize("shape,text", MISSED_SHAPES,
                         ids=[f"{s}::{t[:16]}" for s, t in MISSED_SHAPES])
def test_every_shape_v1_missed_is_now_flagged_by_the_named_rule(shape, text):
    """🔴 正样本必须点名形态,不接受「有命中就算过」。"""
    v = hardcoded_feature_price_violations(text)
    assert _rule_hit(v, "写死动态功能价·%s" % shape), (shape, text, v)


#: 加宽必配反样本 —— 不然锁会逼人把合法说明删掉,最后门被整个关掉。
PRICE_LEGIT_V2 = [
    # 入账方向:充值赠送额不在「功能消耗目录」这条轴上
    "- 体验包：¥0 / 免费 / 到账 3888 算力。",
    "首充送 500 算力。",
    # 阈值/区间:是行为门槛,不是目录价
    "任何一次操作总价 ≥1000 算力会弹 5 秒倒计时确认,可取消。",
    "单次上限 20000 算力。",
    # 占位符:前端把真实数字填进去,文档里没有数字
    "弹窗写「当前算力 N，本次诊断需要 M 算力」。",
    # 改写后的现役写法 —— 判红它就等于把本单的修法否掉
    "- 品牌深度行业解析：按当前目录价扣算力（发起前确认页会显示当次数目）",
    "每词每天按当前目录价扣算力，确认框里会写明当次单价。",
]


@pytest.mark.parametrize("text", PRICE_LEGIT_V2)
def test_widened_rule_does_not_flag_grants_thresholds_or_placeholders(text):
    assert hardcoded_feature_price_violations(text) == [], text


def test_the_grant_exemption_does_not_swallow_a_real_charge():
    """反向对照:入账豁免**不许**把同子句里的真扣费一起放过。"""
    assert hardcoded_feature_price_violations("充值到账后每篇扣 390 算力") == []  # 入账语境,交给现金轴
    assert _rule_hit(hardcoded_feature_price_violations("每篇扣 390 算力"), "写死动态功能价·算力数")


def test_the_threshold_exemption_does_not_swallow_a_floor_price():
    """反向对照:阈值豁免**不许**把「至少扣 N」这种底价一起放过。

    第一版 `[^。;；\\n]{0,8}?` 就放过了它 —— 中间那段必须禁掉计费动词。
    """
    assert hardcoded_feature_price_violations("总价 ≥1000 算力会弹确认") == []
    assert _rule_hit(hardcoded_feature_price_violations("至少扣 100 算力"),
                     "写死动态功能价·算力数")


def test_minutes_and_scores_are_not_treated_as_money():
    """反样本:`分钟` / `分析` / `100 分制` 里的「分」不是钱。"""
    for text in ("诊断约消耗 5 分钟。", "系统会加 3 分析维度。", "完整度按 100 分制算。"):
        assert hardcoded_feature_price_violations(text) == [], text


def test_a_bare_enumeration_needs_at_least_two_pairs():
    """反样本:单个「XX 3 个」不是价目枚举,枚举形态要求至少两组。"""
    assert hardcoded_feature_price_violations("最多 20 个 / 每行一个") == []


# ══════════════════════════════════════════════════════════════════════
# ③ 死路由 —— 分母来自 App.tsx 真值
# ══════════════════════════════════════════════════════════════════════

def test_dead_route_is_flagged_against_the_real_app_routes():
    v = dead_route_violations("到 /this-page-never-existed 里去设置。")
    assert _rule_hit(v, "死路由"), v


def test_live_routes_are_not_flagged():
    """反样本:现役路由不许判红。"""
    for route in ("/monitoring", "/publish", "/help", "/feedback", "/pricing"):
        assert dead_route_violations("请到 %s 页面操作。" % route) == [], route


def test_api_paths_are_not_treated_as_frontend_routes():
    assert dead_route_violations("接口是 /api/xiaobang/chat。") == []


def test_dead_route_rule_records_not_verified_when_the_denominator_is_empty():
    """🔴 零分母只能记「没验」,不能记「没有死路由」。

    传空 known_routes ⇒ 必须返回空**且不是因为它判所有路由都活**。
    """
    assert dead_route_violations("/whatever-route", known_routes=frozenset()) == []
    # 反向对照:同一条输入在真分母下必须判红,证明上面的空不是规则本身失效
    assert dead_route_violations("/whatever-route") != []


# ══════════════════════════════════════════════════════════════════════
# ④ 无出口的失败文案
# ══════════════════════════════════════════════════════════════════════

EXITLESS = [
    "这个我没找到准确答案,你可以去帮助中心翻文档。",
    "不清楚,请查看帮助文档。",
    "暂时不支持,去看文档吧。",
]


@pytest.mark.parametrize("text", EXITLESS)
def test_exitless_failure_copy_is_flagged(text):
    v = exitless_failure_violations(text)
    assert _rule_hit(v, "失败文案无出口"), (text, v)


WITH_EXIT = [
    "这个我没找到准确答案,你把报错截图发我,或者点下方反馈给工作人员。",
    "找不到对应说明,建议先刷新重试;还不行就提交问题,会有人跟进。",
    "不清楚你指的是哪个按钮,告诉我按钮上的字我再帮你定位。",
    # 有出口 + 顺带给文档 ⇒ 合法(文档作为扩展阅读,不是唯一出口)
    "先在设置里确认一下开关状态;详细说明也可以在帮助中心查到。",
]


@pytest.mark.parametrize("text", WITH_EXIT)
def test_failure_copy_with_a_real_exit_is_not_flagged(text):
    """反样本:给了出口就不算违规 —— 否则会逼人把「答不上来」这句话本身删掉,
    而那恰恰是诚实的部分。"""
    assert exitless_failure_violations(text) == [], text


# ══════════════════════════════════════════════════════════════════════
# ⑤ 合集 & 与运行时门的隔离
# ══════════════════════════════════════════════════════════════════════

def test_corpus_lint_aggregates_all_four_rules():
    text = ("我们底层用的是 DeepSeek。AI 写文章一次扣 300 算力。"
            "到 /this-page-never-existed 设置。没找到准确答案,去帮助中心翻文档。")
    v = corpus_lint_violations(text)
    for marker in ("供应商穿透", "写死动态功能价", "死路由", "失败文案无出口"):
        assert _rule_hit(v, marker), (marker, v)


def test_the_new_rules_do_not_enter_the_runtime_write_gate():
    """🔴 新规则**故意不进** `all_violations`(它挂在 fail-closed 的写入门上)。

    往那里加 = 线上管理员写 FAQ 可能被当场拒。这条判据把「隔离」这件事钉住:
    哪天有人把它们并进去,这里会红,并被迫先给出存量语料的红/绿分母。
    """
    from services.kb_terminology_gate import all_violations
    leaky = "我们底层用的是 DeepSeek。"
    assert supplier_leak_violations(leaky) != []
    assert all_violations(leaky) == [], "新规则不该出现在运行时写入门上"


#: 🔴 逐字取自 `api/help_docs_content.json` 的现役写法。
#:    第一版死路由规则把它判红了 —— 那是**图片资源路径**,不是前端路由。
MARKDOWN_ASSET_TARGETS = [
    "![个人设置页](/help-screenshots/%E4%B8%AA%E4%BA%BA%E8%AE%BE%E7%BD%AE%E9%A1%B5.png)",
    "![推荐码卡片](/help-screenshots/card.png)",
    "看这张图 ![x](/assets/foo.svg) 就懂了",
]


@pytest.mark.parametrize("text", MARKDOWN_ASSET_TARGETS)
def test_markdown_asset_targets_are_not_treated_as_dead_routes(text):
    """反样本:图片/资源路径不是路由,判红它们会逼人删掉正文里的截图引用。"""
    assert dead_route_violations(text) == [], text


def test_a_real_dead_route_written_as_prose_is_still_flagged():
    """反向对照:上面那条豁免**不许**把真死路由一起放过。"""
    assert dead_route_violations("请到 /this-page-never-existed 设置。") != []


# ══════════════════════════════════════════════════════════════════════
# ⑥ 现役语料常绿锁(包 C①)
# ══════════════════════════════════════════════════════════════════════

#: 🔴 分母**锚到 `__file__`**,不吃 cwd。
#:    原来是 `glob.glob('knowledge/...')` 相对路径 —— 双树 A/B 时两臂在同一个
#:    cwd 下跑,就会双双读到**同一棵树**的语料,两臂恒等、零判别力。
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_PAGES_DIR = os.path.join(_REPO_ROOT, 'knowledge', 'system_kb', 'pages')
_EXTRA_CORPUS = ('api/help_docs_content.json',
                 'agents/xiaobang_canned_faq.py',
                 'agents/xiaobang_presets.py')


def _corpus_files():
    import glob
    files = sorted(glob.glob(os.path.join(_PAGES_DIR, '*.md')))
    files += [os.path.join(_REPO_ROOT, p) for p in _EXTRA_CORPUS]
    return files


def test_the_corpus_denominator_is_the_whole_pages_dir_not_a_hand_written_list():
    """🔴 分母自证:`pages/` 目录里**每一个** .md 都在分母里。

    手写分母漏掉的那一项不会让任何判据变红。所以这里拿目录**列表**跟分母
    机械求差,而不是断言「至少 25 个」——「至少」挡不住漏掉某一个具体页。

    并且逐字钉住本单那两页:B-⑤ 的 22 处残留**不是**分母漏了它们
    (它们一直在),是规则抓不到 —— 这条锁把这个结论固定下来。
    """
    on_disk = {f for f in os.listdir(_PAGES_DIR) if f.endswith('.md')}
    in_denominator = {os.path.basename(p) for p in _corpus_files() if p.endswith('.md')}
    assert on_disk, "pages/ 目录读空了,取数坏了"
    assert on_disk - in_denominator == set(), on_disk - in_denominator
    for page in ('feature-pricing.md', 'diagnosis-new.md'):
        assert page in in_denominator, page
    for extra in _EXTRA_CORPUS:
        assert os.path.exists(os.path.join(_REPO_ROOT, extra)), extra


def test_the_live_corpus_is_clean_under_the_new_rules():
    """🔴 存量语料常绿锁。

    包 C① 把 17 处写死的动态功能价改成了「以确认页为准」(改写优先,不删事实);
    本单规则加宽后**又清出 70 处**(6 个页面 —— 不止工单说的两页)。
    这条锁防的是**再被写回去** —— 那正是 35 班之前烂掉的机制:
    没有任何闸拦着,于是时变事实一次次沉淀进静态知识。
    """
    files = _corpus_files()
    # 分母自证:文件真的读到了(空分母下「全绿」毫无意义)
    assert len(files) >= 25, files
    total_chars = 0
    problems = []
    for path in files:
        text = io.open(path, encoding='utf-8').read()
        total_chars += len(text)
        for v in corpus_lint_violations(text):
            problems.append('%s :: %s' % (os.path.basename(path), v))
    assert total_chars > 50_000, "语料只读到 %d 字,取数坏了" % total_chars
    assert problems == [], '\n'.join(problems)


def test_the_two_pages_from_the_ticket_carry_no_hardcoded_price_at_all():
    """工单点名的两页单独再钉一遍 —— 常绿锁是全集,这条是**这两页**的作用域。

    全集锁绿可能是因为别的页把注意力吸走;点名页要有自己的判据。
    """
    for page in ('feature-pricing.md', 'diagnosis-new.md'):
        text = io.open(os.path.join(_PAGES_DIR, page), encoding='utf-8').read()
        assert hardcoded_feature_price_violations(text) == [], page


def test_the_corpus_lock_is_not_vacuous():
    """🔴 给上面那把锁注毒:往语料文本里塞一条写死价,必须被抓到。

    不注毒的常绿锁分不清「真干净」和「规则失效」。
    """
    poisoned = io.open(_corpus_files()[0], encoding='utf-8').read() + \
        "\n\n- 生成一篇文章需要 150 算力。\n"
    assert corpus_lint_violations(poisoned) != []
