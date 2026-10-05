"""C-7(1) · 幽灵配置 ``monitoring_tasks`` 的**保存链**(Codex 终审 P1-15 前半句)。

═══════════════════════════════════════════════════════════════════════
🔴 先证伪 Codex 的措辞:不是"表已删",是**设置字段已删**
═══════════════════════════════════════════════════════════════════════
Codex 写「现役写作配置仍写 ``monitoring_tasks``,删除该表后会稳定 500/假保存」。
实核:

  · ``monitoring_tasks`` **表**好好地在(生产 schema dump 里有,
    ``api/defensive_monitoring_api`` / ``db/monitoring_db`` 天天读它);
  · 真正被删掉的是 ``config/settings_manager.SystemSettings.monitoring_tasks``
    这个**字段** —— 包F ⑦c、Owner 2026-08-24 批复的"幽灵配置清理";
  · 而 ``server.py`` 的设置保存链一直还在写
    ``monitoring_tasks=request.monitoring_tasks or current.monitoring_tasks``。

两个后果都真实(与 Codex 说的两个症状一一对应,只是成因不同):

  · 请求体不带 ``monitoring_tasks`` ⇒ 走到 ``current.monitoring_tasks``
    ⇒ pydantic v2 上是 **AttributeError** ⇒ handler 的 except 翻成 **500**;
  · 带了的走短路,值被 ``extra=ignore`` 静默丢弃 ⇒ **假保存**
    (管理员改完点保存不报错,重开还是默认值)。

Owner 2026-08-25 拍板:后端 + 前端一起摘。
"""

from __future__ import annotations

import ast
import io
import tokenize

import pytest

from tests.defgeo_woc_closure_2026_08_25.conftest import ROOT


# ══════════════════════════════════════════════════════════════════════════
# A. 缺陷成因仍然成立(前提自证)
# ══════════════════════════════════════════════════════════════════════════
def test_c7_00_the_settings_field_really_is_gone() -> None:
    """前提:``SystemSettings`` 上真的没有这个字段,读它真的抛。

    没有这一条,下面"保存不炸了"可能只是因为字段又被加回来了 ——
    那时幽灵配置复活,而判据全绿。
    """
    from config.settings_manager import SystemSettings

    assert "monitoring_tasks" not in SystemSettings.model_fields, (
        "幽灵配置被加回来了 —— 它全仓零消费点,却会让人以为线上监测按它路由")
    with pytest.raises(AttributeError):
        _ = SystemSettings().monitoring_tasks


def test_c7_01_the_monitoring_tasks_table_is_alive_and_unrelated() -> None:
    """反向证伪:那张**表**没被删,与本项无关。

    判据要把「表」与「配置字段」分开 —— 混起来会让下一个人去删表。
    """
    from tests.defgeo_woc_closure_2026_08_25.conftest import connect

    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT to_regclass('public.monitoring_tasks') AS r")
        assert cur.fetchone()["r"] is not None, "monitoring_tasks 表不在了 —— 结论要重写"
        conn.rollback()
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════════
# B. 保存链**真跑一遍**不炸、不假保存
# ══════════════════════════════════════════════════════════════════════════
def test_c7_02_settings_save_really_runs_and_does_not_write_the_ghost(probes) -> None:
    """🔴 **执行毒级**判据:真 import server 之后真调 ``update_settings``。

    静态锚只能证明"那行文本不在了";只有真跑一遍能证明"跑起来不炸"
    (本仓记过:存在性毒 ≠ 执行毒 —— 我亲手毒过调用点、判据红了,
    包上线仍每单必炸 NameError)。

    观测来自 ``_role_probe`` 那次子进程真导入(两臂共用一次,不额外付钱):
      · ``ok`` —— 保存成功,**不是** 500;
      · ``hasGhostKey`` —— 落盘的 settings.json 里没有那个键(不再假保存)。

    拆红:把 ``monitoring_tasks=request.monitoring_tasks or current.monitoring_tasks``
    加回 server.py ⇒ ``ok=False`` + error 里是 AttributeError ⇒ 本条红。
    """
    for role in ("web", "cron"):
        got = probes[role]["settingsSave"]
        assert got["ok"] is True, f"ROLE={role} 保存设置炸了:{got['error']}"
        assert got["hasGhostKey"] is False, f"ROLE={role} 落盘里还有幽灵键:{got}"
        assert got["savedKeyCount"] > 30, f"ROLE={role} 落盘内容不对:{got}"


# ══════════════════════════════════════════════════════════════════════════
# C. 结构面:请求体与前端都不再声明它
# ══════════════════════════════════════════════════════════════════════════
def _code_only(src: str) -> str:
    """剥掉注释与字符串,只留代码。

    🔴 本文件与 server.py 里都逐字写着这个词(那是**病历**,不是配置)。
       裸串锚会当场命中病历,于是这条判据变成"不许写病历"。
       本仓记过:引用裁决原文会让裸串结构锚判红。
    """
    out: list[str] = []
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type in (tokenize.COMMENT, tokenize.STRING):
            continue
        out.append(tok.string)
    return " ".join(out)


def test_c7_03_the_request_model_no_longer_declares_it() -> None:
    """``SettingsUpdateRequest`` 不再声明这个字段。

    留着它 = 前端照样能发、后端照样丢 —— 假保存的另一半。
    (pydantic 默认忽略 extra,所以摘掉对还在发旧请求体的客户端是兼容的。)
    """
    src = io.open(ROOT / "server.py", encoding="utf-8", newline="").read()
    tree = ast.parse(src)
    node = next(n for n in ast.walk(tree)
                if isinstance(n, ast.ClassDef) and n.name == "SettingsUpdateRequest")
    fields = {t.target.id for t in node.body if isinstance(t, ast.AnnAssign)
              and isinstance(t.target, ast.Name)}
    assert fields, "取不到 SettingsUpdateRequest 的字段 —— 锚点已过期"
    assert "monitoring_tasks" not in fields, sorted(fields)
    # 活性自证:同族字段必须在(否则上面那条因为 fields 是空的而恒真)
    assert {"diagnosis_tasks", "writing_tasks", "social_tasks",
            "employee_tasks"} <= fields, sorted(fields)


def test_c7_04_the_save_handler_no_longer_reads_it() -> None:
    """``update_settings`` 的**代码**里不再有 ``.monitoring_tasks``。"""
    src = io.open(ROOT / "server.py", encoding="utf-8", newline="").read()
    tree = ast.parse(src)
    node = next(n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == "update_settings")
    body = _code_only(ast.get_source_segment(src, node) or "")
    assert "monitoring_tasks" not in body, "保存链里又出现了 monitoring_tasks"
    assert "SystemSettings" in body, "剥离之后代码是空的 —— 探针失去判别力"


def test_c7_05_the_frontend_section_is_gone_too() -> None:
    """前端那个「监测中心 LLM」区块也摘了(Owner 2026-08-25 选"后端+前端一起")。

    一个填了就丢的输入框比没有这个输入框更贵:管理员会以为线上监测
    按它路由。
    """
    src = io.open(ROOT / "frontend" / "src" / "pages" / "Settings" / "SettingsPage.tsx",
                  encoding="utf-8", newline="").read()
    # 判**代码**不判病历:文件头那段说明里逐字写着这个词。
    code = "\n".join(ln for ln in src.splitlines()
                     if not ln.lstrip().startswith("//"))
    assert "MONITORING_TASKS" not in code, "前端常量清单还在"
    assert "monitoring_tasks" not in code, "前端还在往这个 section 写"
    # 活性自证:同族 section 必须还在
    assert "EMPLOYEE_TASKS" in code or "employee_tasks" in code, (
        "同族 section 也没了 —— 删过头了")


# ══════════════════════════════════════════════════════════════════════════
# C2. [外选 EXTC-12] 钉的是**性质**,不是那一个键
# ══════════════════════════════════════════════════════════════════════════
#
# 🔴 上面 C 组三条(c7_03/04/05)钉的都是 ``monitoring_tasks`` 这个**名字**。
#    同类缺陷平移一格就隐身:把相邻那一行写成
#    ``employee_tasks=current.employee_tasks if False else {}``,
#    管理员改完点保存不报错、重开归零 —— 与 C-7 修掉的"假保存"一模一样,
#    而 c7_02 的探针只盯幽灵键、``savedKeyCount`` 因为默认值照样序列化不变、
#    c7_03 只验字段声明还在、c7_04 只验幽灵词不在:**四条全绿**。
#
#    所以这里把轴换成性质:**每一个声明出来的请求字段都要有人消费**,
#    **每一个读到的设置字段都要真的存在**。两个方向各一条。
#
#: 冻结例外集:声明了但**刻意不消费**的请求字段。
#: ``content_ratios`` —— 保存链那一行逐字写着「Legacy eight-angle ratios are
#: frozen; six-family style_ratios is the only editable SSOT」,取 ``current``
#: 是有意的。例外集**钉死大小**:再多一个就必须有人来解释为什么。
UNCONSUMED_BY_DESIGN: frozenset[str] = frozenset({"content_ratios"})


def _settings_handler_reads(receiver: str) -> tuple[set[str], set[str]]:
    """返回 (``SettingsUpdateRequest`` 声明的字段, ``update_settings`` 里 ``<receiver>.X`` 的读)。

    两边都是**机械枚举**:一边从类体的 AnnAssign 数,一边从函数体的
    Attribute 数。手抄任何一边都会让"漏了一格"这件事静默通过
    (本仓记过:手写分母漏掉的那一项不会让任何判据变红)。
    """
    src = io.open(ROOT / "server.py", encoding="utf-8", newline="").read()
    tree = ast.parse(src)
    cls = next(n for n in ast.walk(tree)
               if isinstance(n, ast.ClassDef) and n.name == "SettingsUpdateRequest")
    declared = {t.target.id for t in cls.body
                if isinstance(t, ast.AnnAssign) and isinstance(t.target, ast.Name)}
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n.name == "update_settings")
    reads = {sub.attr for sub in ast.walk(fn)
             if isinstance(sub, ast.Attribute) and isinstance(sub.value, ast.Name)
             and sub.value.id == receiver}
    return declared, reads


def test_c7_07_every_declared_request_field_is_actually_consumed() -> None:
    """🔴 [外选 EXTC-12] 声明了的请求字段,保存链里必须真的读它。

    声明了却没人读 = **假保存**:前端照发、pydantic 照收、保存链一转手就丢,
    管理员改完点保存不报错,重开还是默认值。这正是 C-7 那半个缺陷的**类**,
    而不是它的那一个名字。

    拆红:把 ``update_settings`` 里任何一处 ``request.X`` 换掉
    (例如 ``employee_tasks=current.employee_tasks if False else {}``)⇒ 本条红。
    """
    declared, reads = _settings_handler_reads("request")

    # 活性自证:两边都不是空的(空集合会让下面那条恒真)
    assert len(declared) > 40, f"声明字段只数出 {len(declared)} 个 —— 抽取口径坏了"
    assert len(reads) > 40, f"保存链里只数出 {len(reads)} 处读 —— 抽取口径坏了"

    assert declared - reads == UNCONSUMED_BY_DESIGN, (
        f"这些字段声明了却没人消费:{sorted(declared - reads - UNCONSUMED_BY_DESIGN)};"
        f"而例外集里这些已经用不上了:{sorted(UNCONSUMED_BY_DESIGN - (declared - reads))}")
    assert len(UNCONSUMED_BY_DESIGN) == 1, (
        f"「刻意不消费」的例外多了一个:{sorted(UNCONSUMED_BY_DESIGN)} —— "
        "冻结例外集要钉死大小,否则它会变成一个静默放行的口袋")
    # 反向:读了一个**没声明**的请求字段 ⇒ pydantic v2 上是 AttributeError ⇒ 500
    assert not (reads - declared), (
        f"保存链读了没声明的请求字段:{sorted(reads - declared)}")


def test_c7_08_every_settings_field_the_handler_reads_really_exists() -> None:
    """🔴 幽灵配置的**另一半**:``current.X`` 读的每一格都必须在 ``SystemSettings`` 上。

    C-7 那个 500 就是这么来的 —— ``SystemSettings.monitoring_tasks`` 被删了,
    保存链还在读 ``current.monitoring_tasks``,pydantic v2 上属性访问直接抛,
    handler 的 ``except Exception`` 把它翻成 500。这条把那个形态钉成性质:
    以后再删任何一个设置字段而忘了摘保存链,当场红,而不是等生产 500。
    """
    from config.settings_manager import SystemSettings

    _declared, reads = _settings_handler_reads("current")
    assert len(reads) > 5, f"``current.X`` 只数出 {len(reads)} 处 —— 抽取口径坏了"
    known = set(SystemSettings.model_fields)
    assert len(known) > 40, f"SystemSettings 只数出 {len(known)} 个字段"
    assert not (reads - known), (
        f"保存链读的这些设置字段在 SystemSettings 上不存在:{sorted(reads - known)} —— "
        "每一次不带该键的保存请求都会 500")


# ══════════════════════════════════════════════════════════════════════════
# D. 普查分母 —— 这条判据当初漏掉真缺陷的那个洞
# ══════════════════════════════════════════════════════════════════════════
def test_c7_06_the_orphan_census_now_covers_the_repo_root() -> None:
    """🔴 既有那条孤儿普查**两个洞**都补上了。

    它当初同时躲过两道:
      · 接收者白名单 ``(settings|SystemSettings|_settings|cfg)`` 里没有
        ``current`` —— 而 ``current = load_settings()`` 是本仓惯用名;
      · glob 里没有仓库根 ⇒ ``server.py`` 从来没被扫过。
    「分母漏了一块」与「分母里没有违规项」在断言里长得一模一样,
    所以这一条直接调它的探针来验(不是重写一份)。
    """
    from tests.defensive_geo_2026_08_21.test_model_pricing_census import (
        _ORPHAN_CONSUMER, _is_production_py, _orphan_consumer_hits,
    )

    assert _ORPHAN_CONSUMER.search("x = current.monitoring_tasks"), (
        "接收者白名单又窄回去了 —— current.monitoring_tasks 命中不了")
    scanned = {p.relative_to(ROOT).as_posix()
               for p in ROOT.rglob("*.py") if _is_production_py(p)}
    assert "server.py" in scanned, "扫描面仍然不含 server.py"
    assert _orphan_consumer_hits() == [], (
        f"全仓还有人读 settings.monitoring_tasks:{_orphan_consumer_hits()}")
