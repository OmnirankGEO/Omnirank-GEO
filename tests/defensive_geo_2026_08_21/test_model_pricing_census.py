"""模型 census 增量:计价配对锁 + 幽灵登记锁(⑥/⑦/⑧/⑨)。

配对锁的分母怎么来 —— 与工单原话的差异必须说清
------------------------------------------------
工单要求「分母从近 20 天 llm_call_log 导出,不手抄」。**本窗禁止触碰生产**,
所以那个分母我取不到。这里用的是**代码 census 分母**:凡是能被代码当成
"要发出去的模型名"的地方(现役默认值 / 平台契约 / 请求体里钉死的字面量),
都必须在 ``PRICING_TABLE`` 里有精确行。

两者的关系要摆明:
  · 代码分母 ⊇ 真实调用分母 —— 代码里配得出来的模型才可能被调用,
    所以代码分母全绿 ⇒ 真实调用不会落 DEFAULT_PRICING(方向是对的);
  · 但代码分母**看不见**"通过 SystemSettings 在运行时被改成别的值"那一类,
    真实日志能看见。所以生产那一半我另交了一个只读脚本给 Deploy 侧跑
    (见交付单),不是用代码分母冒充它。

不把它写成"连生产库就跑、连不上就 SKIP":SKIP 的判据等于没有判据 ——
本仓「no SKIP 锁」正是为了不让判据用跳过来假装绿。
"""

from __future__ import annotations

import ast
import io
import re
import tokenize
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _pricing_table():
    from tools.llm_call_tracker import PRICING_TABLE

    return PRICING_TABLE


# ══════════════════════════════════════════════════════════════════════════
# ⑥b/⑧ 配对锁:代码配得出来的 (provider, model) 必须有精确计价行
# ══════════════════════════════════════════════════════════════════════════
def _live_pairs() -> set[tuple[str, str]]:
    """机械分母:从**现役配置对象**读,不手抄清单。

    手抄的清单不会跟着代码变 —— 新加一个平台默认模型时它不会自己长出来,
    判据照绿,而那个模型在生产每次调用都落 DEFAULT_PRICING(成本记错账)。
    """
    pairs: set[tuple[str, str]] = set()

    # ① 监测/诊断主链的平台契约(provider 与 model 成对写在一起)
    from services.monitoring_lineage import _PLATFORM_CONTRACT

    for spec in _PLATFORM_CONTRACT.values():
        pairs.add((str(spec["provider"]), str(spec["model"])))

    # ② 调研监测那条链的千问默认(⑥ 双管线里的另一条)
    from services.research_monitor.platforms import _load_qwen_model

    import inspect
    sig = inspect.signature(_load_qwen_model)
    pairs.add(("dashscope", str(sig.parameters["default"].default)))

    # ③ ai_surface 各 surface 的默认模型
    try:
        from services.ai_surface_monitoring import lineage as _sl

        for spec in getattr(_sl, "SURFACES", {}).values():
            provider = getattr(spec, "provider_key", None)
            model = getattr(spec, "default_model_key", None)
            if provider and model:
                pairs.add((str(provider), str(model)))
    except Exception:                                    # pragma: no cover
        pass

    # ④ 顾问 provider 目录的 default_model(⑦a 修的就是这里的 kimi 那一行)
    from advisors.base_advisor import BaseAdvisor

    for provider, cfg in BaseAdvisor.API_PROVIDERS.items():
        model = cfg.get("default_model")
        if model:
            pairs.add((str(provider), str(model)))

    return pairs


def test_live_pair_denominator_is_real():
    """零分母守卫 + 形状守卫。

    分母空了(某个模块改名/改结构)会让下面那条恒绿,而它是唯一在守
    「新模型没补计价行」的东西。
    """
    pairs = _live_pairs()
    assert len(pairs) >= 6, f"只扫到 {len(pairs)} 对 (provider, model),分母可疑:{sorted(pairs)}"
    for provider, model in pairs:
        assert provider and model and "/" not in provider, (provider, model)


def test_every_live_pair_has_an_exact_pricing_row():
    """🔴 每个代码配得出来的 (provider, model) 都必须有**精确**计价行。

    未命中会落 ``DEFAULT_PRICING``(0.001/0.005)—— 那不是"差不多",
    是把一个模型的成本记成另一个数字,而账目上看不出任何异常。
    qwen-plus-latest 就是这么漏了很久:它活跃在跑,表里一直没有它。
    """
    table = _pricing_table()
    missing = sorted(p for p in _live_pairs() if p not in table)
    assert not missing, (
        "这些 (provider, model) 在代码里配得出来,却没有精确计价行 → 会落 DEFAULT_PRICING:\n  "
        + "\n  ".join(f"{p}" for p in missing))


def test_pricing_lookup_really_misses_for_an_unknown_pair():
    """反向对照:证明"未命中"这件事真的可被观测。

    没有这一条,上面那条可能只是因为 PRICING_TABLE 是个万能容器
    (比如有人给它加了 __missing__),那时它什么都没守。
    """
    table = _pricing_table()
    assert ("no-such-provider", "no-such-model") not in table, \
        "价目表对任意键都命中 —— 配对锁没有判别力"


# ══════════════════════════════════════════════════════════════════════════
# ⑥c 幽灵登记锁:目录/价目表里不许有"全仓没人调"的模型
# ══════════════════════════════════════════════════════════════════════════
_GHOST_MODELS = ("qwen3.5-plus",)


def test_no_ghost_model_claims_to_be_the_live_engine():
    """🔴 幽灵登记比缺登记更贵:它让读代码的人以为自己知道线上在跑什么。

    ``qwen3.5-plus`` 曾经在 config/model_config.py 里自称「监测/诊断引擎实际用此」,
    在 PRICING_TABLE 里占一行,还被三处注释引用 —— 而**全仓零活调用**,
    真正在跑的是 qwen3-max。
    这条扫的是**代码行**(跳过注释):墓碑注释要留着讲清历史,但不许再有
    可执行的登记。
    """
    from tools.llm_call_tracker import PRICING_TABLE

    comment = re.compile(r"^\s*(#|//)")
    for ghost in _GHOST_MODELS:
        assert not any(m == ghost for _, m in PRICING_TABLE), \
            f"{ghost} 仍占着一行计价 —— 它没有任何活调用"
        # 目录侧:只看代码行,墓碑注释允许逐字引用它(讲清历史)
        for rel in ("config/model_config.py", "tools/llm_call_tracker.py"):
            live = [ln for ln in (ROOT / rel).read_text(encoding="utf-8").splitlines()
                    if ghost in ln and not comment.match(ln)]
            assert not live, f"{rel} 的代码行里仍有 {ghost}:{live[:2]}"


def test_ghost_scan_denominator_is_not_empty():
    """反向对照 + 零分母守卫:被扫的名单不能是空的。"""
    assert _GHOST_MODELS, "幽灵名单是空的,上一条什么都没扫"


# ══════════════════════════════════════════════════════════════════════════
# ⑥a 双管线对照注释锁:两处必须互指
# ══════════════════════════════════════════════════════════════════════════
def test_the_two_qwen_pipelines_point_at_each_other():
    """同一引擎双管线双模型是**事实**,本单不改它 —— 但两处代码必须互指。

    不互指的后果很具体:下一个人只看到一边,就会以为那是全仓口径,
    然后在另一条链上按错误前提做判断(比如"千问答得短"其实是另一个模型)。
    """
    tester = (ROOT / "tools/ai_visibility/ai_tester.py").read_text(encoding="utf-8")
    plat = (ROOT / "services/research_monitor/platforms.py").read_text(encoding="utf-8")

    assert "research_monitor/platforms.py" in tester and "qwen-plus-latest" in tester, \
        "ai_tester 没有指向另一条管线 —— 对照注释缺失"
    assert "ai_visibility/ai_tester.py" in plat and "qwen3-max" in plat, \
        "platforms.py 没有指回主链 —— 对照注释缺失"


# ══════════════════════════════════════════════════════════════════════════
# ⑦ 配置异常锁
# ══════════════════════════════════════════════════════════════════════════
def test_no_provider_default_model_crosses_vendors():
    """🔴 ⑦a:端点与模型名必须同厂。

    Moonshot 端点配 Qwen 模型名 → 发出去必 400。它当前读不到,靠的是两条
    构造路径各自兜底了 model_name —— 那是**调用方的好意**,不是结构保证。
    """
    from advisors.base_advisor import BaseAdvisor

    vendor_of_model = [
        ("moonshot.cn", ("kimi", "moonshot")),
        ("volces.com", ("doubao", "ep-")),
        ("openrouter.ai", ("/",)),
    ]
    bad = []
    for provider, cfg in BaseAdvisor.API_PROVIDERS.items():
        base = str(cfg.get("api_base") or "")
        model = str(cfg.get("default_model") or "")
        for host, prefixes in vendor_of_model:
            if host in base and model and not any(model.startswith(p) or p in model
                                                  for p in prefixes):
                bad.append(f"{provider}: {host} 端点配了 {model!r}")
    assert not bad, "端点与默认模型不同厂(发出去会 400):\n  " + "\n  ".join(bad)





#: 孤儿配置的消费点探测。**结构锚**:只认 ``<something>.monitoring_tasks``
#: 属性访问,不裸匹配 ``monitoring_tasks`` —— 那个词同时是**表名**
#: (``SELECT ... FROM monitoring_tasks``)与一堆**局部变量名**
#: (``api/monitoring_api.py`` 里 6 处),裸匹配会把它们全判成消费点,
#: 于是这条判据永远红、永远没人能删这份配置。
#
# 🔴 [工单 C-7 2026-08-25] 这条正则与下面那份 glob **各漏了一格**,而漏掉的
#    正好是真缺陷所在的那一行:``server.py:15601`` 的
#    ``monitoring_tasks=request.monitoring_tasks or current.monitoring_tasks``。
#      · 接收者白名单 ``(settings|SystemSettings|_settings|cfg)`` 里没有
#        ``current`` —— 而 ``current = load_settings()`` 正是本仓取设置的
#        惯用名;
#      · glob 里没有 ``server.py``(它在仓库根,不在 api/ services/ tools/
#        workflows/ agents/ 任何一个下面)。
#    两个洞叠在一起,让一个**每次保存设置必抛 AttributeError** 的调用点
#    在两条判据下全绿。本仓记过这一条:「手写分母漏掉的那一项不会让任何判据变红」。
#
#    修法是让两个分母都**按路径形状机械枚举**,而不是继续往手抄清单里补名字:
#      · 接收者:任何 ``<标识符>.monitoring_tasks`` 属性访问都算候选,
#        再用负向前瞻排掉已知的**非配置**用法(SQL 里的表别名 / 局部变量);
#      · 文件面:扫全部**生产** .py(排 tests / 归档 / 虚拟环境),
#        而不是列举几个目录。
_ORPHAN_CONSUMER = re.compile(
    # 前面不能紧跟别的属性/点(排掉 ``a.b.monitoring_tasks`` 这种更深的路径不是目的,
    # 而是排掉 SQL 字符串里的 ``t.monitoring_tasks`` 这类表限定 —— 见下面的反向自证)。
    r"(?<![\w.])(?!public\.)(?P<recv>[A-Za-z_]\w*)\s*\.\s*monitoring_tasks\b")

#: 生产代码面 = 仓库根下全部 .py,减去这几棵**不是生产**的树。
#: 分母从"排除什么"定义,而不是从"包含哪几个目录"定义 ——
#: 后者每加一个新目录就静默少扫一块。
_ORPHAN_EXCLUDED_TREES = (
    "tests", "docs", "node_modules", ".git", ".venv", "venv",
    "agent-test-artifacts", "qa", "__pycache__",
)

#: SQL 表别名一类的**非配置**接收者。它们后面跟 ``.monitoring_tasks`` 时
#: 指的是那张表,不是那份配置。写成显式清单是因为这一格必须能被读懂;
#: 加名字之前先问一句"它真的是表别名吗"。
_ORPHAN_TABLE_ALIASES = frozenset({"public", "t", "mt", "c", "m"})


def _is_production_py(path: Path) -> bool:
    rel = path.relative_to(ROOT).as_posix()
    return not any(part in _ORPHAN_EXCLUDED_TREES for part in rel.split("/"))


def _code_only(src: str) -> str:
    """剥掉注释与字符串,**只看代码**。

    🔴 [工单 C-7 实测] 不剥的话这条判据会命中一堆**病历**:
       ``config/settings_manager.py`` 与 ``server.py`` 里都逐字写着
       「``settings.monitoring_tasks`` 已删」的说明段落 —— 那是删除这份
       幽灵配置时留下的证据,不是消费点。裸串锚把它判红,等于这条判据
       变成了"不许写病历"。本仓记过:引用裁决原文会让裸串结构锚判红,
       白名单一个不加(加白名单会把真消费点也放进去)。

    剥离失败(文件语法不完整)时**返回空串**并由调用方按"扫不到"处理 ——
    但那会让分母静默变小,所以下面 :func:`_orphan_consumer_hits` 对
    剥离失败的文件回落到原文扫描:宁可误报一次,不要静默漏掉一块。
    """
    out: list[str] = []
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type in (tokenize.COMMENT, tokenize.STRING):
            continue
        out.append(tok.string)
    return " ".join(out)


def _orphan_consumer_hits() -> list[str]:
    hits = []
    for path in ROOT.rglob("*.py"):
        if not _is_production_py(path):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if "monitoring_tasks" not in text:
            continue                       # 快路径:整个词都没有就不必 tokenize
        try:
            scan = _code_only(text)
        except (tokenize.TokenError, IndentationError, SyntaxError):
            # 🔴 剥不动就按**原文**扫:宁可误报一次让人来看,
            #    也不要让"这个文件没法 tokenize"变成"这个文件没有违规"。
            scan = text
        for m in _ORPHAN_CONSUMER.finditer(scan):
            if m.group("recv") in _ORPHAN_TABLE_ALIASES:
                continue
            hits.append(str(path.relative_to(ROOT)))
            break
    return sorted(hits)


def test_orphan_monitoring_tasks_config_is_gone():
    """🔴 [包F ⑦c · 2026-08-24 Owner 已批] 孤儿配置**已删**,判据随之转向。

    一期这条判据叫 ``test_orphan_claim_is_still_true``,钉的是
    「配置还在,但零消费者」—— 它明确写了为什么当时不能删:
    ``config/settings_manager.py`` 是受保护文件,且「系统设置改动需审批」,
    「等 Owner 批了再连判据一起落(改动与判据必须同 commit)」。

    Owner 已批 ⇒ 配置删了,这条判据**换向**:现在钉的是"它真的不在了"。
    换向而不是删掉:删掉的话,哪天有人把这份幽灵配置加回来,
    不会有任何东西变红。
    """
    import io as _io

    text = _io.open(ROOT / "config/settings_manager.py",
                    encoding="utf-8", newline="").read()
    # 结构锚:类属性声明那一行,不是注释里提到这个词(本文件与
    # settings_manager 的说明段落里都写着它 —— 那是病历,不是配置)。
    assert not re.search(r"(?m)^\s*monitoring_tasks\s*:\s*dict\s*=", text), (
        "幽灵配置 settings.monitoring_tasks 被加回来了 —— "
        "它全仓零消费点,却会让读代码的人以为线上监测按它路由")


def test_orphan_claim_premise_still_holds():
    """删除的**前提**仍然成立:全仓零消费点。

    这不是重复上一条。上一条证明"配置不在了",本条证明"当初判它是孤儿
    这件事没错" —— 假如现在扫出消费点,说明有人在删除之后又写了读取代码,
    那是一个 AttributeError 等着上生产,必须当场红。
    """
    hits = _orphan_consumer_hits()
    assert not hits, (
        "有代码在读 settings.monitoring_tasks,而该配置已于包F ⑦c 删除 —— "
        f"这是一个等着上生产的 AttributeError:{hits}")


def test_orphan_consumer_probe_has_discriminating_power():
    """探针活性自证:它必须真的能命中一个消费点。

    没有这一条,正则写错(或扫描面写错路径)时上面两条同样全绿 ——
    "扫不到"与"没有"在断言里长得一样。本仓记过这一条。
    """
    assert _ORPHAN_CONSUMER.search("x = settings.monitoring_tasks['a']"), \
        "探针连教科书式的消费点都命中不了 —— 正则写错了"
    # 🔴 [工单 C-7] **这一行是这条判据当初漏掉的那个真缺陷的原样复刻**。
    #    `server.py` 里那句 `current.monitoring_tasks` 曾同时躲过两道:
    #    接收者不在手抄白名单里、文件不在手抄 glob 里。
    #    正样本点名它,以后正则再窄回去会当场红。
    assert _ORPHAN_CONSUMER.search(
        "monitoring_tasks=request.monitoring_tasks or current.monitoring_tasks,"), \
        "探针命中不了 `current.monitoring_tasks` —— 接收者白名单又窄回去了"
    # 反向:表名与局部变量名**不许**被认成消费点
    assert not _ORPHAN_CONSUMER.search("JOIN monitoring_tasks mt ON x"), \
        "探针把表名当成了配置消费点 —— 这会让这份配置永远删不掉"
    assert not _ORPHAN_CONSUMER.search("monitoring_tasks = []"), \
        "探针把局部变量当成了配置消费点"
    assert not _ORPHAN_CONSUMER.search("FROM public.monitoring_tasks WHERE id=%s"), \
        "探针把 SQL 的 public.monitoring_tasks 当成了配置消费点"
    # 🔴 [工单 C-7 实测] 病历不许被判成消费点:``config/settings_manager.py``
    #    与 ``server.py`` 的说明段落里都逐字写着这个词。剥离必须真的把
    #    注释与字符串拿掉,否则这条判据会变成"不许写病历"。
    assert "monitoring_tasks" not in _code_only(
        "# 说明:settings.monitoring_tasks 已删\n"
        "X = '这里也写了 settings.monitoring_tasks'\n"
        "y = 1\n"), "剥离没把注释/字符串里的病历拿掉"
    assert "current" in _code_only("current.monitoring_tasks\n"), \
        "剥离把真代码也拿掉了 —— 探针失去判别力"


def test_orphan_scan_surface_actually_covers_the_repo_root():
    """🔴 [工单 C-7] 扫描面自证:它必须真的扫到 `server.py`。

    这条判据存在的唯一理由是那次实测:旧版 glob
    ``("api/*.py","services/**/*.py","tools/**/*.py","workflows/*.py","agents/*.py")``
    **不含仓库根**,于是 ``server.py`` 从来没被扫过 —— 而真缺陷正好在那里。
    "分母漏了一块"与"分母里没有违规项"在断言里长得一模一样,
    所以必须单独证明那一块在分母里。
    """
    scanned = {p.relative_to(ROOT).as_posix()
               for p in ROOT.rglob("*.py") if _is_production_py(p)}
    assert "server.py" in scanned, "扫描面不含 server.py —— 旧版 glob 的洞又回来了"
    # 分母不能是空的,也不能小到不可能覆盖生产
    assert len(scanned) > 200, f"生产 .py 只扫到 {len(scanned)} 个 —— 分母塌了"
    # 反向:测试树必须被排掉(否则判据自己写的正样本会把自己判红 ——
    # 本仓记过「工具打在自己所在的文件上会自我命中」)。
    assert not any(p.startswith("tests/") for p in scanned), \
        "扫描面把 tests/ 也扫进来了 —— 本文件里的正样本会自我命中"


# ══════════════════════════════════════════════════════════════════════════
# ⑨ 过时入库值锁
# ══════════════════════════════════════════════════════════════════════════
def test_writing_config_ships_the_live_model():
    """⑨:入库的 writing_config.json 不许还是 v3.2。

    生产那份被 volume 覆盖成 v4-flash(已亲验),但**新环境/开发机**拾取的是
    入库这一份 —— 于是同一份代码在不同机器上跑不同模型,而谁都没改过配置。
    """
    import json

    cfg = json.loads((ROOT / "data/writing_config.json").read_text(encoding="utf-8"))
    model = json.dumps(cfg, ensure_ascii=False)
    assert "deepseek-v3.2" not in model, "入库 writing_config.json 仍是 deepseek-v3.2"


def test_display_name_and_model_id_tell_the_same_story():
    """⑨:前端展示名不许和 model_id 说两件事。"""
    src = (ROOT / "writing/llm_providers.py").read_text(encoding="utf-8")
    assert "Qwen3 Max (阿里云)" not in src, \
        "display_name 还是 'Qwen3 Max' 而 model_id 已是 qwen3.7-max —— 前端会显示旧代号"
