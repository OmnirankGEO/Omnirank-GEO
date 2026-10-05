"""WO_206 c1a/c1b 判据 · 仓级锁(第一层:分母不许漂)。

钉三件:
  ① 重新枚举出的**每一个**模型名字面量都在签入表里 —— 新增一个没分类的 ⇒ 红;
  ② 分母本身**只许变长** —— 缩了就是有人又把锚改窄了(c1a 正是栽在这上面);
  ③ **不是官方线**的那些名字**逐行冻结内容** —— 不只冻计数。

🔴 ② 和 ③ 都是 c1b 新加的,各自对应一次真实的失手:

   ② c1a 的枚举锚是**形状猜测**(只认 `model=` / `"model":` 等 6 种写法),
     漏了 126 处生产字面量,其中有真发出点(`services/placement_service.py:945`
     的官方线调用就在里面)。而当时那条「每个发出点都已分类」的锁**是绿的** ——
     因为锚没看见的东西,锁也看不见。所以现在要冻分母本身。

   ③ Review 的 Q3 实测:把 `employees/base_employee.py:49` 的百炼名改成
     `deepseek-flash`,红的是 in-sync 锁,而 `dashscope_side_count_is_frozen`
     **没红** —— 计数没变、内容变了。只冻计数的反向对照没有牙。

🔴 本包**不**钉「official×a 必须发常量」—— 那是 c1d 的事。
   现在就钉会让整批判据一上来全红,而"全红的判据"没人会一条条看,
   最后只会被整体跳过(本仓的存量红就是这么攒出来的)。
"""
import sys

import pytest

sys.path.insert(0, __file__.replace("\\", "/").rsplit("/", 1)[0])

import census        # noqa: E402
import classified    # noqa: E402
import render_table  # noqa: E402

#: 生产字面量总数的冻结值(2026-09-14 实测)。**只许变长**。
#: 变短 = 要么有人删了发出点(那要在表里说清),要么有人又把枚举锚改窄了 ——
#: 而后者正是 c1a 那次失手,它当时**没有任何一条判据会红**。
#: 🔴 [开源 E3 · B2 · 2026-09-28] **216 → 199**:社媒工具包整删,带走 8 个文件里的 18 条字面量
#:    (flywheel_engine_v2 3 · generation_modes 3 · deepseek_skill_curator 6 · dynamic_review_skill 2 ·
#:     content_judge / conversation_orchestrator / intent_classifier_v2 / method_curator 各 1);基准 9ae1c8ac7 实测 217,
#:    两树逐条比:少的 18 条全在已删文件里,新增 0 —— 是删了发出点,不是锚改窄了。
FROZEN_PRODUCTION_TOTAL = 199

#: 社媒 / IP 板块那批 `unread` 的冻结量(2026-09-14 实测)。
#: 🔴 它是**路径规则**自动给的判决,和 blanket 同病:新行不会红。
#:    Review 09-14 读到 39 条 / 16 文件;`services/llm/deepseek_key_pool.py`
#:    经人读改判(它是 GEO 官方 key 池,不是社媒)后退出这一批,减 5 条 / 1 文件。
#: 🔴 [WO_259 · 开源 E0 2026-09-22] **(34, 15) → (31, 14)**。
#:    不是"有人删了发出点",是 `advisor_llm.py` 跟着 `deepseek_key_pool.py`
#:    一起从 `tools/social_operator/`(已随开源 E3 B2 删) 上提到了 `services/llm/` —— 路径规则据此
#:    不再把它判成社媒那批(它本来就是 GEO 主链的地基,33 个在役文件引它)。
#:    减 3 条 / 1 文件,与上面那条 09-14 的改判同因不同批。
#: 🔴 [开源 E3 · B2 · 2026-09-28] **(31, 14) → (6, 3)**:社媒工具包 8 个文件 18 条随包删,advisor_api 4 / content_api 3 /
#:    社媒主路径 router 2 条随 B3b / B1b-2b 删端点消失(那几片没同步本表,9ae1c8ac7 上已红)。逐文件差分全落在 E3 动过的文件里。
FROZEN_UNREAD = (6, 3)

#: 允许出现的 kind。Review 09-14 定的五种 + 一个诚实的非分类。
#: 🔴 不许再长出第六种:kind 一多,读表的人就不再逐类去想「它到底发不发名字」。
ALLOWED_KINDS = ("emit", "registry_key", "compare", "label", "doc", "unread")


@pytest.fixture(scope="module")
def rows():
    out = census.census(".")
    assert out, "枚举一条都没出 —— 仪器坏了,不是仓里没有"
    return out


@pytest.fixture(scope="module")
def prod(rows):
    """分母 = 生产 .py(排除 tests/ 与 scripts/,理由见 classified.py 表头)。"""
    return [r for r in rows if r["scope"] != "not_production"]


def test_every_model_name_literal_is_classified(prod):
    """① 分母不许漂:枚举出的每一个模型名字面量都必须在表里。

    🔴 这条是整把锁的地基。它红的时候意思是「仓里出现了一个模型名,
       但没人说过它是干什么的」—— 而那正是本单要根治的状态:
       改名时不知道该改哪些、不该改哪些。
    🔴 注意分母是**字面量**不是「我认为的发出点」:c1a 版本把分母定成后者,
       于是锚看不见的 126 处就永远不会红。
    """
    missing = [(r["path"], r["line"], r["model"]) for r in prod
               if classified.verdict_for(r) is None]
    assert not missing, (
        "这些模型名字面量不在签入表里(新增了就要人读一句依据再加进 classified.py):\n"
        + "\n".join("  %s:%d  %s" % m for m in missing))


def test_the_denominator_only_grows(prod):
    """② 分母冻结:生产字面量总数**只许变长**。

    🔴 变短有两种可能,都必须有人解释:
       (a) 真删了发出点 —— 那要同步删表,并在这里把冻结值调小、写明原因;
       (b) **枚举锚又被改窄了** —— 这是 c1a 的原病,而它当时静默无声:
           锚看不见的东西,"每个发出点都已分类"那条锁也看不见。
       所以这条锁的方向是**单向**的:长可以不问,短必须问。
    """
    assert len(prod) >= FROZEN_PRODUCTION_TOTAL, (
        "生产字面量从 %d 掉到了 %d。要么删了发出点(同步改表 + 调这个数并写明原因),"
        "要么枚举锚被改窄了 —— 后者是 c1a 栽过的那一跤,当时一条判据都没红。"
        % (FROZEN_PRODUCTION_TOTAL, len(prod)))


def test_blanket_verdicts_cover_no_more_than_they_were_signed_for(prod):
    """🔴 [Review 09-14 · P5] 文件级 blanket 不许**自动**收编新冒出来的字面量。

    blanket 是个抽屉:分母一变大,新行会被它裹进去,于是
    `test_every_model_name_literal_is_classified` 照样绿 —— Review 实测就是这么过去的
    (在 `config/deepseek_models.py` 里追加一个新名,只有 in-sync 一条红,
     而那条的提示语引向"重新渲染",照做就把新名静默收进表里)。

    所以每个 blanket 记着它当初盖了几条;超出 ⇒ 那几条按**未分类**算,
    必须有人真去读一眼再把数字调上来。加行要人点头,这正是 blanket 缺的那一步。
    """
    actual = {}
    for r in prod:
        actual[r["path"]] = actual.get(r["path"], 0) + 1

    missing_budget = [p for p in classified.FILE_VERDICT
                      if p not in classified.FILE_LITERAL_COUNT]
    assert not missing_budget, (
        "这些 blanket 没有冻结字面量数(加 blanket 必须同时记下它盖了几条):%s"
        % missing_budget)

    over = [(p, classified.FILE_LITERAL_COUNT[p], actual.get(p, 0))
            for p in classified.FILE_VERDICT
            if actual.get(p, 0) > classified.FILE_LITERAL_COUNT[p]]
    assert not over, (
        "这些文件冒出了 blanket 没签过字的行(签过 n / 现有 m):%s。\n"
        "去读那几行,判清它是 emit 还是别的,再把 FILE_LITERAL_COUNT 调上来 ——"
        "直接调数字等于替自己点头。" % (over,))

    under = [(p, classified.FILE_LITERAL_COUNT[p], actual.get(p, 0))
             for p in classified.FILE_VERDICT
             if actual.get(p, 0) < classified.FILE_LITERAL_COUNT[p]]
    assert not under, (
        "这些文件的字面量比签过的少(签过 n / 现有 m):%s。\n"
        "本单把字面量换成常量引用**不会**让它变少(census 两样都数),"
        "所以变少要么是真删了发出点,要么是 census 又瞎了。" % (under,))


def test_auto_path_rules_cover_a_frozen_amount(prod):
    """路径机械规则(社媒 / 非生产)盖住的量也冻结。

    🔴 它和 blanket 是同一个病:靠规则自动给判决的地方,新行同样不会红。
       社媒那批是 `unread` —— 一句「我没读过」,它只配 keep,
       而且 c1c / c1d 的分母**不含**它(见表头)。量变了要有人看一眼。
    """
    unread = [r for r in prod
              if (classified.verdict_for(r) or
                  ("?",))[0] == "unread"]
    files = {r["path"] for r in unread}
    assert (len(unread), len(files)) == FROZEN_UNREAD, (
        "社媒 unread 从 %s 变成了 (%d 条, %d 文件) —— 多出来的行没人读过,"
        "别让路径规则替社媒窗口签字" % (FROZEN_UNREAD, len(unread), len(files)))


def test_the_table_has_no_dead_rows(rows):
    """反向:表里也不许有**枚举不到**的行。

    少了它,表会慢慢变成一份"曾经存在过的行"的墓地 ——
    而墓地里的行会让分母看起来更大、覆盖看起来更全。
    """
    seen_files = {r["path"] for r in rows}
    dead_files = [p for p in classified.FILE_VERDICT if p not in seen_files]
    # [0913e 集成] 按 (path, model, occ) 比,不按行号 —— 行号一漂,
    # 这条会把**还活着**的行报成"枚举不到",而那种红是仪器脆不是真缺口。
    seen_sites = {(r["path"], r["model"], r["occ"]) for r in rows}
    dead_sites = [k for k in classified.SITE_VERDICT if k not in seen_sites]
    assert not dead_files and not dead_sites, (
        "表里这些发出点已经枚举不到了(名字改了/代码删了就同步删表):" + chr(10) +
        "  文件级: %s" % (dead_files,) + chr(10) +
        "  发出点级: %s" % (dead_sites,))


#: ③ **不是官方线**的那些名字,逐行冻结 `(文件, 名字, 条数)`(2026-09-14 实测)。
#:
#: 🔴 为什么不带行号:同文件里加删几行就会让行号整体平移,而一条**为无关原因转红**
#:    的冻结表,下一个人只会把它放宽 —— 放宽之后它就再也不会为真原因转红了。
#:    `(文件, 名字, 条数)` 对「名字被改了」同样敏感(Review 的 Q3 那发毒:
#:    把 base_employee.py:49 的百炼名改掉,这张表当场对不上),却不会被平移带红。
FROZEN_OTHER_LINE = [
    # 🔴 c1b② 移除:`config/model_config.py` 的 deepseek-chat 原被 c1a 判成「百炼」
    #    (理由写的是"该条目 model_type = dashscope_chat")—— **判错了**。
    #    逐行复读:那条 `deepseek_v32` 的 model_type 是 `openai_chat`,
    #    base_url 取 DEEPSEEK_CONFIG = https://api.deepseek.com/v1,是**官方线**发出点。
    #    改判 official/switch 后它离开这张「非官方线」的冻结表,不是名字被动过。
    #: 🔴 [WO_221-c1'] `db/monitoring_db.py` 那条**离开了本表**,不是名字被动过:
    #:   它是 DeepSeek 监测平台的成本占位,07-27 起该平台走官方线,
    #:   继续写 dashscope 就是把成本记到百炼名下 ⇒ 改判 official/switch。
    #:   (本文件上方注释早写过这种情况:改判后离表 != 名字被动过。)
    #: 🔴 [WO_221-c1' ⑦] `services/engine_contract.py` 也**离开了本表** ——
    #:   它改判成官方线(账本计划模型),不是名字被动过。
    ('api/employee_api.py', 'deepseek-v4-flash', 1),
    ('api/research_monitor_config_api.py', 'deepseek-v4-flash', 1),
    ('config/model_config.py', 'deepseek/deepseek-r1', 1),
    ('config/settings_manager.py', 'deepseek-v4-flash', 1),
    ('db/diagnosis_db.py', 'deepseek-reasoner', 1),
    ('db/diagnosis_db.py', 'deepseek-v4-flash', 7),
    ('db/diagnosis_db.py', 'deepseek-v4-pro', 4),
    ('employees/base_employee.py', 'deepseek-v3.2', 1),
    ('employees/base_employee.py', 'deepseek-v4-flash', 3),
    ('employees/base_employee.py', 'deepseek-v4-pro', 2),
    ('employees/content/content_writer.py', 'deepseek-v4-pro', 1),
    ('employees/diagnosis/ai_tester.py', 'deepseek-v4-flash', 1),
    ('employees/diagnosis/competitor_analyst.py', 'deepseek-v4-flash', 1),
    ('employees/diagnosis/data_collector.py', 'deepseek-v4-flash', 1),
    ('employees/diagnosis/report_writer.py', 'deepseek-v4-pro', 1),
    ('employees/dynamic_employee.py', 'deepseek-v4-flash', 1),
    ('employees/employee_registry.py', 'deepseek-v4-flash', 1),
    ('employees/support/chief_editor.py', 'deepseek-reasoner', 1),
    ('employees/support/ppt_specialist.py', 'deepseek-v4-pro', 1),
    # [开源 E3 · B2] ('server.py', 'deepseek-v4-flash', 1) 离表:所在内联端点随 B3a 删(发出点没了,不是名字被动过)
    ('services/flywheel_judgment.py', 'deepseek-v4-flash', 1),
    ('services/geo_observation/promotion.py', 'deepseek-v4-flash', 1),
    ('services/geo_observation/source_hooks.py', 'deepseek-v4-flash', 1),
    ('services/knowledge_pipeline.py', 'deepseek-v4-flash', 3),
    ('services/multi_ai_voter.py', 'deepseek-v4-flash', 2),
    ('services/research_monitor/article_intent_classifier.py', 'deepseek-v4-flash', 2),
    ('services/research_monitor/industry_resolver.py', 'deepseek-v4-flash', 2),
    ('services/research_monitor/platforms.py', 'deepseek-v4-flash', 1),
    ('services/research_monitor/query_intent_classifier.py', 'deepseek-v4-flash', 2),
    ('tools/ai_visibility/ai_tester.py', 'deepseek-v4-flash', 4),
    ('tools/industry_knowledge_collector.py', 'deepseek-v4-pro', 2),
    ('tools/keyword/longtail_matrix.py', 'deepseek-v4-flash', 1),
    ('tools/keyword_expander.py', 'deepseek-v4-flash', 2),
    ('tools/multi_llm_caller.py', 'deepseek-v4-flash', 1),
    ('workflows/diagnosis_workflow.py', 'deepseek-v4-flash', 1),
    ('writing/llm_providers.py', 'deepseek-ai/DeepSeek-V3', 1),
    ('writing/llm_providers.py', 'deepseek-v4-flash', 2),
    ('writing/llm_providers.py', 'deepseek-v4-pro', 1),
    ('writing/style_registry.py', 'deepseek-v4-flash', 1),
    ('writing/style_registry.py', 'deepseek-v4-pro', 1),
]


def test_other_line_names_are_frozen_by_content_not_by_count():
    """③ 反向对照:**不是官方线**的那些名字,逐行冻结内容。

    🔴 本单只改官方线。百炼那条线只会在运行时报「模型不存在」,或者更糟,
       静默落到别的档 —— 静默落档不会有任何告警,只有账单会变。
    🔴 冻内容不冻计数,是因为 Review 实测过:把一处百炼名换掉,
       **计数一点没变**,而只冻计数的那条锁全程是绿的。
    """
    actual = render_table.other_line_names()
    assert actual == FROZEN_OTHER_LINE, _diff_msg(FROZEN_OTHER_LINE, actual)


def _diff_msg(want, got):
    w, g = set(want), set(got)
    return ("非官方线的名字被动过了(或有人新增/删除了一条):\n"
            "  冻结表里有、现在没有: %s\n"
            "  现在有、冻结表里没有: %s\n"
            "改了要先说清为什么 —— 这条线改错只会在运行时报「模型不存在」,"
            "或者静默落到别的档。" % (sorted(w - g), sorted(g - w)))


#: [L3 · Review 09-14] **官方线上标了 keep** 的那些名字,同样逐行冻结内容。
#: 🔴 原来的反向对照只冻「非官方线」,于是官方线上**故意不动**的那些行没人看着 ——
#:    尤其 `deepseek-v4-pro`(multi_ai_voter:72 / deepseek_models:31):把它改成 flash
#:    只有 in-sync 一条红,而 in-sync 的提示语引向「重新渲染」,照做就全绿。
#:    v4-pro 是官方今天**还在供应**的另一档,改它是降档 —— 贵/慢/质量三件事一起变。
FROZEN_OFFICIAL_KEEP = [
    # 🔴 c1d 多出 placement_service 那一行:它是 c1d 写的**注释**(提醒下一行那个
    #    带斜杠的 DeepSeek/v4-flash 是展示标签不是模型 ID),签成 doc/keep 后进了这张表。
    #    是我自己加的字,不是哪个名字被动过。
    #: 🔴 [WO_220-c1 / WO_221-c1'] 官方线上新签的 keep 行进表;
    #:   同时移出两条:platforms / ai_tester 的 deepseek-v4-flash occ1 ——
    #:   前者改判成百炼侧(occ 位移),后者的官方那一处已改发常量、进了 flash 族。
    #: 🔴 顺序也是冻结的一部分:本表按 `render_table` 的产出顺序写,
    #:   不是「集合相等就行」—— 顺序变了同样说明有东西动过。
    ('config/deepseek_models.py', 'deepseek-chat', 1),
    ('config/deepseek_models.py', 'deepseek-flash', 1),
    ('config/deepseek_models.py', 'deepseek-reasoner', 1),
    ('config/deepseek_models.py', 'deepseek-v4-flash', 1),
    ('config/deepseek_models.py', 'deepseek-v4-flash-vision-exp', 1),
    ('config/deepseek_models.py', 'deepseek-v4-pro', 1),
    ('config/model_config.py', 'deepseek-chat', 1),
    ('config/model_config.py', 'deepseek-reasoner', 1),
    ('config/vision_routing.py', 'deepseek-flash', 2),
    ('db/social_preferences_db.py', 'deepseek-flash', 1),
    ('employees/base_employee.py', 'deepseek-reasoner', 2),
    # 🔴 [WO_259 2026-09-22] 新进这张表的两行 —— **不是新写的代码**:
    #    `advisor_llm.py` 从 `tools/social_operator/`(已随开源 E3 B2 删) 上提到 `services/llm/`,
    #    路径一离开社媒目录就不再被路径规则判成 unread,于是它的名字进了这张表。
    #    两处都在 `advisor_flash` 的 flash 档路由上(`SOCIAL_FLASH_PROVIDER` 判断
    #    与 `SOCIAL_FLASH_MODEL` 默认值),读名字不发名字。
    #    ⚠️ 那个默认值仍是**旧名**(官方 09-13 已改名)——
    #       本单是纯搬家不改行为,**没动它**,待办另记(见 WO_259 交付单)。
    ('services/llm/advisor_llm.py', 'deepseek-v4', 1),
    ('services/llm/advisor_llm.py', 'deepseek-v4-flash', 2),
    ('services/llm/deepseek_key_pool.py', 'deepseek-flash', 2),
    ('services/llm/deepseek_key_pool.py', 'deepseek-v4-flash', 1),
    ('services/llm/deepseek_key_pool.py', 'deepseek-v4-pro', 2),
    ('services/multi_ai_voter.py', 'deepseek-chat', 2),
    ('services/multi_ai_voter.py', 'deepseek-v4-pro', 2),
    ('services/placement_service.py', 'DeepSeek/v4-flash', 1),
    ('services/research_monitor/platforms.py', 'deepseek-flash', 2),
    ('tools/ai_visibility/ai_tester.py', 'deepseek-chat', 4),
    ('tools/ai_visibility/ai_tester.py', 'deepseek-flash', 1),
    ('writing/llm_providers.py', 'deepseek-v3', 1),
    ('writing/llm_utils.py', 'deepseek-flash', 1),
    ('writing/llm_utils.py', 'deepseek-reasoner', 1),
    ('writing/llm_utils.py', 'deepseek-v4', 1),
]


def test_official_keep_rows_are_frozen_too():
    """官方线上「故意不动」的那些名字,也要逐行对得上。

    🔴 keep 不是「随便」。它是一句**判断**:这一行今天就该是这个名字。
       没有这条锁,唯一会红的是 in-sync —— 而它只说「表和源对不上,重渲染一下」,
       照做之后表跟着变,谁也不会再问一句「这个名字为什么变了」。
    """
    actual = render_table.official_keep_names()
    assert actual == FROZEN_OFFICIAL_KEEP, _diff_msg(FROZEN_OFFICIAL_KEEP, actual)


#: [c1d] 官方线 × scope=a × kind=emit 里**允许还没改**的那几处,逐条签字。
#: 🔴 这张表就是 c1d 那条总锁的全部松动量。空着才是完成态;
#:    每加一行都必须写清「为什么它今天还不能改」,而不是「先放着」。
OFFICIAL_A_EMIT_EXCEPTIONS = {
    # 🔴 **空的**。c1e 之前这里有一条:`services/article_ai_review.py` ——
    #    它把模型名焊进了幂等键,改名 = 存量文章全部重评。
    #    c1e 把「发出去的名字」与「reviewer 身份串」**拆成两个东西**之后,
    #    那条理由就不存在了:发出名跟着官方改,身份串逐字节冻住,两件事互不打架。
    #    ——「它有理由不改」在多数时候其实是「这里有两个角色挤在一个名字上」。
}


def test_every_official_geo_emit_site_uses_the_constant(prod):
    """🔴 [c1d 总锁] GEO 干活线上**官方线的每一个发出点**都必须发常量。

    这是本单的收尾判据,也是唯一一条"从正面"说话的:
    前面那些锁说的都是「别漂」「别悄悄加」「别改错线」,
    这一条说的是**「改完了」**——而"改完了"必须是可查的,不能是我说完就算。

    🔴 范围只有 scope=a。scope=b(被监测引擎)不在此列:换它等于换被测对象。
       但要记一笔:官方已经让 `deepseek-chat` 静默回显 `deepseek-flash`
       (Deploy 206-d2 实打),所以 b 那几处「以为在测 chat、其实在测 flash」——
       可比性是被**厂商**破坏的,不是被本单破坏的,该由谁来定另说。

    🔴 例外必须逐条签字(见 OFFICIAL_A_EMIT_EXCEPTIONS),而不是靠一个白名单前缀:
       前缀会顺手放过它旁边那些没人看过的行。
    """
    from config.deepseek_models import DEEPSEEK_OFFICIAL_EMITTABLE

    bad, excused = [], []
    for r in prod:
        v = classified.verdict_for(r)
        if not v or v[0] != "emit" or v[1] != "official" or v[2] != "a":
            continue
        if r["model"] in DEEPSEEK_OFFICIAL_EMITTABLE:
            continue
        key = (r["path"], r["model"])
        if key in OFFICIAL_A_EMIT_EXCEPTIONS:
            excused.append(key)
            continue
        bad.append((r["path"], r["line"], r["model"], v[4]))
    assert not bad, (
        "这些官方线 GEO 发出点还在发旧名:%s。\n"
        "旧名今天仍然调得通,所以**不会报错** —— 只有思考开关不认新名"
        "(默认开思考,慢约 17% 贵约 35%)、key 池掉兜底档、计价落通用价。" % (bad,))

    # 反向对照:例外表里的行必须**真的还在**。
    # 少了它,例外会变成一张只增不减的许可证 —— 那一行早就改好了,
    # 而表上仍写着"它有理由不改",下一个人照抄这条理由去豁免别的行。
    stale = [k for k in OFFICIAL_A_EMIT_EXCEPTIONS if k not in set(excused)]
    assert not stale, (
        "例外表里这些行已经不存在了(改好了或删了),同步删掉例外:%s" % (stale,))


def test_monitored_engine_is_out_of_scope(prod):
    """范围 b(被监测引擎)一条都不许标成 switch。

    换被监测引擎 = 换被测对象,监测数据前后不可比 —— 那是产品决定,
    要 Owner 划线,不是本单顺手做的事。

    🔴 c1b 新增的一条正是这种陷阱:`services/research_monitor/platforms.py:491`
       走的确实是 `DEEPSEEK_OFFICIAL_URL`,看着像"官方线漏改了一处",
       但同段写着「官方通道 fail-closed,绝不静默回落百炼 —— 那正是
       以为在测 DeepSeek 实际测阿里的成因」:它是被测对象。
    """
    bad = []
    for r in prod:
        v = classified.verdict_for(r)
        if v and v[2] == "b" and v[4] == "switch":
            bad.append((r["path"], r["line"]))
    assert not bad, "被监测引擎被标成了要改:%s" % (bad,)


def test_every_verdict_has_a_human_reason():
    """表里每一行都要有一句**人读的依据**,不许空着。

    🔴 没有依据的分类等于没有分类:下一个人无法判断它对不对,
       只能选择相信 —— 而"相信一张没有依据的表"正是本单开头那个错误
       (照工单的 11 条做)的同一形状。
    """
    for key, v in list(classified.FILE_VERDICT.items()) + \
            list(classified.SITE_VERDICT.items()):
        kind, provider, scope, reason, plan = v
        assert kind in ALLOWED_KINDS, "kind 不在五种(+unread)里: %s %s" % (key, kind)
        assert provider in ("official", "dashscope", "openrouter", "registry",
                            "mixed", "n/a"), key
        assert scope in ("a", "b", "c", "x"), key
        assert plan in ("switch", "keep", "later"), key
        assert reason and len(reason.strip()) >= 6, "分类依据太短或为空: %s" % (key,)


def test_unread_rows_are_never_switched(prod):
    """`unread` 是「我没读过这一行」,那它就**不许**被排进 switch。

    🔴 少了这条,`unread` 会变成一个方便的抽屉:先塞进去、再顺手改掉,
       而"顺手改掉一行没人读过的代码"正是本单开头那个错误的另一种形状。
    """
    bad = []
    for r in prod:
        v = classified.verdict_for(r)
        if v and v[0] == "unread" and v[4] != "keep":
            bad.append((r["path"], r["line"], v[4]))
    assert not bad, "没读过的行被排了动作:%s" % (bad,)


def test_normalizer_only_touches_official_aliases():
    """归一函数只认官方线的历史名,别的原样返回。

    🔴🔴 **2026-09-14 翻面**:`deepseek-reasoner` 现在**归一**。
       c1a 写的是「reasoner 不归一 —— 它是官方线上另一个模型(开思考那档),
       折进去会悄悄改掉调用方要的行为」。那条理由建立在一个已经不成立的事实上:
       Deploy 206-d2 当天实打,官方 `/models` 只剩 `deepseek-flash` 与 `deepseek-v4-pro`,
       `deepseek-reasoner` 仍返 200 但**回显 `deepseek-flash`** —— 那一档已经没有了,
       四处发出点正在**静默降级**。Owner 2026-09-14 拍板,原话:
       「全部改成 deepseek-flash」。归一因此不再是"悄悄改行为",而是把
       已经发生的降级**写明**,让它在代码里看得见。
    🔴 百炼的名字传进来也原样返回 —— 判线的责任在调用点(那里才知道 base_url)。
    """
    from config.deepseek_models import (DEEPSEEK_OFFICIAL_FLASH,
                                        normalize_deepseek_model)

    for alias in ("deepseek-chat", "deepseek-v4-flash",
                  "deepseek-v4-flash-vision-exp", "deepseek-reasoner"):
        assert normalize_deepseek_model(alias) == DEEPSEEK_OFFICIAL_FLASH

    # 🔴 反向对照照旧:`deepseek-v4-pro` 是官方线上**还活着**的另一档
    #    (Deploy 实打它仍在 /models 里),归一到 flash 等于降档 —— 绝不许。
    for untouched in ("deepseek-v4-pro", "qwen3-max", "deepseek-v3.2", "", "  "):
        assert normalize_deepseek_model(untouched) == untouched.strip() or \
            normalize_deepseek_model(untouched) == untouched
    assert normalize_deepseek_model(None) is None


def test_new_name_is_wired_into_the_three_compare_roles():
    """「发」之外的三个角色都认得新名 —— 这三处漏一个都**不报错**。

    · 思考开关:不认 ⇒ 默认开思考(慢 ~17% 贵 ~35%);
    · key 池档位:不认 ⇒ 掉 realtime 兜底档(换了一批 key);
    · 计价表:不认 ⇒ 落 DEFAULT_PRICING(约 2.5× 且丢缓存折扣)。
    """
    from config.deepseek_models import DEEPSEEK_OFFICIAL_FLASH
    from tools.llm_call_tracker import PRICING_TABLE
    from services.llm.deepseek_key_pool import deepseek_role_for_model
    from writing.llm_utils import get_thinking_disabled_params

    official = "https://api.deepseek.com/v1/chat/completions"
    assert get_thinking_disabled_params(official, DEEPSEEK_OFFICIAL_FLASH) == \
        {"thinking": {"type": "disabled"}}
    assert deepseek_role_for_model(DEEPSEEK_OFFICIAL_FLASH) == "normal"

    new_row = PRICING_TABLE.get(("deepseek", DEEPSEEK_OFFICIAL_FLASH))
    old_row = PRICING_TABLE.get(("deepseek", "deepseek-v4-flash"))
    assert new_row is not None, "计价表没有新名 ⇒ 落通用价并丢缓存档"
    assert new_row == old_row, (
        "新名与旧名不同价。官方说旧名现在就是由 V4.1-Flash 提供服务、按 Flash 价计费,"
        "所以换名不换价;要改价请先对账(见 llm_call_tracker 里那条待对账注释)")


def test_dashscope_thinking_path_is_untouched():
    """反向对照:百炼那条线的思考参数**没被我碰过**。

    少了它,「把思考开关改成对所有模型都发 deepseek 的字段」也能让上面那条绿,
    而那会把百炼的 `enable_thinking` 换成一个它不认识的键。
    """
    from writing.llm_utils import get_thinking_disabled_params

    dash = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    for m in ("deepseek-flash", "deepseek-v4-flash", "qwen3-max"):
        assert get_thinking_disabled_params(dash, m) == {"enable_thinking": False}


def test_the_checked_in_table_is_in_sync_with_its_source():
    """签入的表必须与 `classified.py` **一致**(重新渲染零 diff)。

    🔴 `docs/AI-CONTEXT/*` 整个目录在 .gitignore 里(那 658 个已跟踪文件是
       当年 force-add 进去的),所以这份表**必须 `git add -f`** 才进得了树 ——
       忘了加就是"我以为签了、其实没签"。
    🔴 更要紧的是新鲜度:它是**生成物**。签进去之后如果数据源改了而没重渲染,
       读表的人会按一份过期的分类去改代码 —— 那比没有这张表更糟,
       因为它看起来是权威的。所以这条锁比"表存在"重要。
    """
    import io
    import pathlib
    import subprocess

    doc = pathlib.Path("docs/AI-CONTEXT/DEEPSEEK_OFFICIAL_EMIT_SITES_2026-09-14.md")
    assert doc.exists(), "签入表不在:%s" % doc

    tracked = subprocess.run(["git", "ls-files", "--error-unmatch", str(doc)],
                             capture_output=True)
    assert tracked.returncode == 0, (
        "表在磁盘上但**没进树**(docs/AI-CONTEXT/* 被 .gitignore 排除,"
        "要 `git add -f`)—— 那就不是签入表")

    # 🔴 [Review 09-14 · P9] 渲染到**临时文件**再比对,不许覆盖签入文件。
    #    原来的写法是「先渲染(覆盖)、再和自己比」—— census 一旦坏掉(比如枚举
    #    不到任何行),它会先把表冲成空的、再和空的比对,然后**报绿**:
    #    仪器坏了不该报绿,更不该顺手把证据一起改掉。
    import tempfile

    before = io.open(doc, encoding="utf-8").read()
    with tempfile.TemporaryDirectory() as d:
        tmp = pathlib.Path(d) / "rendered.md"
        render_table.main(out_path=str(tmp))
        after = io.open(tmp, encoding="utf-8").read()
    assert before == after, (
        "签入的表与 classified.py 对不上 —— 改了分类却没重新渲染。"
        "跑 `python tests/deepseek_official_line_2026_09_14/render_table.py`")
    # 反向对照:渲染出来的东西得像张表,不能是空的。
    # 少了它,「census 坏了 ⇒ 两边都空 ⇒ 相等 ⇒ 绿」这条路还留着。
    assert after.count("|") > 200 and "本单要改" in after, (
        "渲染结果不像一张表(长度 %d)—— 多半是 census 瞎了,而不是表对了" % len(after))


#: 已经改完(手里已经是官方线常量)的 switch 行**条数**。**只许变大**。
#: 🔴 为什么不用路径前缀:同一个文件里会**同时**有改完的和没改的行
#:    (`config/model_config.py` 就是:437 已改、423 还没),路径前缀表达不了。
#:    用条数则不管批次怎么切都成立,而且"改完的变少了"会立刻红。
DONE_SWITCH_ROWS = 58

#: v3.2 不是官方线的历史别名,是一处**坏了的**发出名:Deploy 206-d2 实打它现在返 400。
#: 只给评审A 那一行开口子(LINE 级判决里写明了),不放宽成一条通则。
#: c1b② 已把它改掉,这里留着是为了让"它曾经是坏的"这件事在锁里可查。
KNOWN_BROKEN_ON_OFFICIAL = {"services/writing_style_reviewer.py": {"deepseek-v3.2"}}


def test_switch_rows_hold_the_right_thing_on_both_sides_of_the_batch(prod):
    """switch 行手里拿的东西,改前改后各有各的规矩 —— 且两边分母不许都空。

    · 已改的:必须是**官方线允许发的名字**(常量)。漏一处、或者改成另一个旧别名,
      表上照样写着 switch,而那些名字**今天仍然调得通** —— 于是没有任何东西会报错,
      只有账单和默认参数悄悄变(思考开关不认新名 ⇒ 默认开思考,慢 ~17% 贵 ~35%)。
    · 还没改的:必须是**官方线的历史名**,且 provider 必须是 official。
      🔴 光看名字不够:**百炼侧的 ID 和官方线的历史别名是同一个字符串**
         (`deepseek-v4-flash` 两边都有),所以"手里拿的是官方别名"对
         「把一条百炼行标成 switch」毫无区分力 —— 注毒 S1 实测它没红。

    🔴 已改条数**只许变大**:变小意味着有人把改好的写回去了,或者分类表漂了。
    """
    from config.deepseek_models import (DEEPSEEK_OFFICIAL_ALIASES,
                                        DEEPSEEK_OFFICIAL_FLASH,
                                        DEEPSEEK_OFFICIAL_PRO)

    done, todo, bad = [], [], []
    by_line = {}
    for r in prod:
        v = classified.verdict_for(r)
        if not v or v[4] != "switch":
            continue
        if v[1] != "official":
            bad.append(("非官方线却标了 switch(本单只归一官方线)",
                        r["path"], r["line"], "%s/%s" % (v[1], r["model"])))
            continue
        # 🔴 [L1 · Review 09-14] 「改完了」只认 **Flash 常量**,不认整个可发集合。
        #    用 `in EMITTABLE` 的话,把一行改成 `deepseek-v4-pro` 也算 done ——
        #    而那不是改名,是**升档**(贵 3 倍、还开思考)。Review 实测:
        #    把 geo_scoring 改成 v4-pro,旧版批次锁全程是绿的。
        if r["model"] == DEEPSEEK_OFFICIAL_FLASH:
            done.append(r)
            by_line.setdefault((r["path"], r["line"]), []).append(True)
            continue
        if r["model"] == DEEPSEEK_OFFICIAL_PRO:
            bad.append(("switch 行出现 v4-pro:那是**升档**不是改名,要 Owner 划线",
                        r["path"], r["line"], r["model"]))
            continue
        todo.append(r)
        by_line.setdefault((r["path"], r["line"]), []).append(False)
        ok = set(DEEPSEEK_OFFICIAL_ALIASES)
        ok |= KNOWN_BROKEN_ON_OFFICIAL.get(r["path"], set())
        if r["model"] not in ok:
            bad.append(("标着 switch 却不是官方线历史名",
                        r["path"], r["line"], r["model"]))

    # 🔴 [L2 · Review 09-14] 同一行上不许「一半改完、一半还是旧名」。
    #    攻击面:往一条已改完的行上**再加回**一个别名字面量 —— 它继承那一行的
    #    switch 判决,于是被当成「还没改的旧名」放行,而它其实是刚被塞回去的。
    for (path, line), flags in by_line.items():
        if any(flags) and not all(flags):
            bad.append(("同一行上既有改完的常量又有旧名字面量 —— 多半是被塞回去的",
                        path, line, "done=%d / todo=%d"
                        % (sum(flags), len(flags) - sum(flags))))

    assert done or todo, (
        "switch 行一条都没有 —— 分母空了。空表全绿和覆盖完整的绿长得一模一样")
    assert not bad, chr(10).join("  %s: %s:%s %s" % x for x in bad)
    assert len(done) >= DONE_SWITCH_ROWS, (
        "已改的 switch 行从 %d 掉到 %d —— 有人把改好的写回旧名,或者分类表漂了"
        % (DONE_SWITCH_ROWS, len(done)))


def test_the_official_line_emits_only_what_the_vendor_still_serves():
    """已改的那些行,发出去的名字必须在**官方今天真的还供应**的集合里。

    🔴 [Deploy 206-d2 实打 2026-09-14] 官方 `/models` 现在只剩
       `deepseek-flash` 与 `deepseek-v4-pro`。`deepseek-chat` / `deepseek-v4-flash` /
       `deepseek-reasoner` 仍返 **200**,但**回显 `deepseek-flash`** ——
       也就是说"只验 HTTP 200"**抓不到静默降级**:调用成功、名字对不上、账单照走。
       所以单元层先钉住"我们只发这两个名字";回显那一层由 Deploy 的上线探针复核。
    """
    from config.deepseek_models import (DEEPSEEK_OFFICIAL_EMITTABLE,
                                        DEEPSEEK_OFFICIAL_FLASH,
                                        DEEPSEEK_OFFICIAL_PRO)

    assert DEEPSEEK_OFFICIAL_EMITTABLE == frozenset(
        {DEEPSEEK_OFFICIAL_FLASH, DEEPSEEK_OFFICIAL_PRO}), (
        "可发集合变了 —— 它对应的是官方 /models 的现值,改它要有当天的实打证据")
    assert DEEPSEEK_OFFICIAL_FLASH == "deepseek-flash"
    assert DEEPSEEK_OFFICIAL_PRO == "deepseek-v4-pro"


def test_both_reviewers_are_alive(monkeypatch):
    """🔴 [c1b② · P1] 文风评审必须**两个都活着** —— 这是本批唯一一处行为修复。

    原来评审A 默认 `deepseek-v3.2`,而它在官方线上返 400(Deploy 206-d2 实打),
    于是 `_resolve_reviewers` 给出的两个评审里有一个**必然失败**,判定只会返
    「单评审 · 建议继续观察」—— `replace` 这条路从来没通过。
    这条钉两件:两个评审都解析得出来,且评审A 发的是官方线还在供应的名字。
    """
    from config.deepseek_models import DEEPSEEK_OFFICIAL_EMITTABLE
    import services.writing_style_reviewer as wsr

    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key-not-used")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key-not-used")
    monkeypatch.delenv("REVIEW_MODEL_A", raising=False)
    monkeypatch.delenv("REVIEW_MODEL_B", raising=False)

    got = wsr._resolve_reviewers()
    assert len(got) == 2, (
        "只解析出 %d 个评审 —— 少于 2 个时判定恒返 observe,replace 永远不会发生" % len(got))
    a = [c for c in got if c["provider"] == "deepseek"]
    assert a, "评审A(官方线)不见了"
    assert a[0]["model"] in DEEPSEEK_OFFICIAL_EMITTABLE, (
        "评审A 发的是 %s —— 不在官方今天还供应的集合里(旧默认 deepseek-v3.2 实测返 400)"
        % a[0]["model"])

    # ── 成功臂:把官方 200 打进**真实**调用路径,钉住上线路的那个名字 ──
    # 🔴 只断言"解析出两个"不够:名字错是**调用时**才失败的(v3.2 就是这么坏了
    #    一个多月还没人发现)。所以要走一遍 `_call_review_llm`,看它往 wire 上写了什么。
    import asyncio
    import contextlib

    sent = {}

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": "{}"}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1}}

    async def _fake_post(self, url, headers=None, json=None, **kw):
        sent["url"] = url
        sent["model"] = (json or {}).get("model")
        return _Resp()

    class _NoopTracker:
        def record(self, **kw):
            return None

    @contextlib.asynccontextmanager
    async def _noop_track(*a, **kw):
        yield _NoopTracker()

    import httpx
    monkeypatch.setattr(httpx.AsyncClient, "post", _fake_post)
    monkeypatch.setattr(wsr, "llm_track", _noop_track)

    reviewer = dict(a[0])
    reviewer.setdefault("key", "test-key")
    out = asyncio.run(wsr._call_review_llm([{"role": "user", "content": "x"}], reviewer))
    assert out == "{}", "评审A 的 200 没被正常解析出来"
    assert "api.deepseek.com" in sent["url"], "评审A 没走官方线:%s" % sent["url"]
    assert sent["model"] in DEEPSEEK_OFFICIAL_EMITTABLE, (
        "上 wire 的模型名是 %s —— 判据必须钉**实际发出去的那个**,"
        "而不是配置里看起来的那个" % sent["model"])

    # ── 反向对照:dual 路径确实存在,且单评审确实到不了 replace ──
    # 少了它,「两个评审都活着」可以为真而 replace 仍然永远不发生 ——
    # 而本批要修的后果正是"replace 从没通过"。
    two = [{"better_side": "candidate"}, {"better_side": "candidate"}]
    assert wsr._aggregate(two, 2)[0] == "replace", "两个评审一致却给不出 replace"
    one = [{"better_side": "candidate"}]
    assert wsr._aggregate(one, 1)[0] == "observe", (
        "单评审居然能 replace —— 那是另一个方向的病(自嗨)")


def test_no_control_chars_in_the_instrument():
    """仪器自己不许带控制字符。

    🔴 这条不是洁癖。本仓 #195 栽过:改写脚本把判据正则里的词边界转义
       吃成退格 0x08,于是否定断言**恒真恒绿**,四条老判据静默失效数月。
       而我写 c1b 这一批时,在一句**专门讲这件事**的注释里又中了一次
       (heredoc 把那两个字符变成了一个真的 0x08)。
       所以它得是一条会自己喊的判据,不能靠记性。
    """
    import glob
    import io

    ok = (chr(10), chr(13), chr(9))
    bad = []
    for f in glob.glob("tests/deepseek_official_line_2026_09_14/*.py") + \
            ["config/deepseek_models.py"]:
        t = io.open(f, encoding="utf-8").read()
        hits = sorted({hex(ord(c)) for c in t if ord(c) < 32 and c not in ok})
        if hits:
            bad.append((f, hits))
    assert not bad, "仪器里有控制字符(多半是转义被吃掉了):%s" % (bad,)


# ═══════════════════════════════════════════════════════════════
# c1e · 一个名字扮两个角色,拆开之后各自钉住
# ═══════════════════════════════════════════════════════════════
def test_the_reviewer_identity_string_is_frozen_byte_for_byte():
    """🔴 `AI_REVIEWER` 是**幂等键**的一部分,逐字节不许动。

    `services/article_ai_review.py` 用它查「这篇文章这版内容已经被谁评过」,
    写回时也按它留痕。动一个字符 = 宣布换了个 reviewer = **存量文章全部重评**。
    里面那个 `deepseek-chat` 是 2026-08-01 起落库的**历史标签**,不是模型名。

    🔴 这条与下一条是**一对**:上面冻身份,下面追新名。
       c1e 之前两者共用同一个字面量,于是「跟着官方改名」与「别动幂等键」直接打架 ——
       那条行因此被整个标成 later 绕过去了。**「它有理由不改」在多数时候
       其实是「这里有两个角色挤在一个名字上」。**
    """
    from services.article_ai_review import AI_PROMPT_VERSION, AI_REVIEWER

    assert AI_REVIEWER == "ai:deepseek-chat@" + AI_PROMPT_VERSION
    assert AI_REVIEWER == "ai:deepseek-chat@v1-2026-08-01", (
        "reviewer 身份串变了 —— 存量文章会被全部重评。这是一次**迁移**,"
        "要单独定口径,不是顺手改个常量")


def test_the_article_reviewer_emits_the_official_constant():
    """另一半:真正**发出去**的那个名字取常量,跟着官方走。"""
    from config.deepseek_models import DEEPSEEK_OFFICIAL_FLASH
    from services.article_ai_review import AI_MODEL, AI_REVIEWER

    assert AI_MODEL == DEEPSEEK_OFFICIAL_FLASH
    # 🔴 反向对照:两者**今天必须不同值**。相同就说明又被合回一个名字了,
    #    而合回去之后,下一次改名会静默地把幂等键一起改掉。
    assert AI_MODEL not in AI_REVIEWER, (
        "发出名又出现在身份串里了 —— 两个角色被合回了一个名字")


def test_the_exception_table_is_empty_now():
    """🔴 c1d 那张例外表已经**清空**。

    留着这条,是为了让"又往里加一行"这件事有人看见:
    例外表每多一行,总锁的松动量就大一格,而松动量是不会自己变回去的。
    """
    assert OFFICIAL_A_EMIT_EXCEPTIONS == {}, (
        "例外表又有行了:%s。加之前先问一句 ——"
        "这真的是「有理由不改」,还是「两个角色挤在一个名字上」?"
        % (sorted(OFFICIAL_A_EMIT_EXCEPTIONS),))
