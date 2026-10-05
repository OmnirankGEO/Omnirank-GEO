"""异步化 + 创建入口页补课 · 锁

工单两件(Owner 2026-08-02「异步化和入口页一起做」):

  A. 异步化 —— 生产 nginx 对 /api/geo-douyin/* 走默认 `location /api/`,
     proxy_read_timeout = **60s**;而一组卡实测 ≈137s → 同步**必然 504**。
     所以三个长端点(下单 / 重新创作 / 重抽)全部改成立刻返回 + 轮询。
  B. 创建入口页补课 —— 深色语言对齐 / 试点横幅压一行 / 列表卡片化+状态徽章 /
     失败人话原因+一键重试。

🔴 本文件优先写**行为锁**(纯函数直接喂输入断输出),源码扫描型断言只在
   "这件事只能从结构上看出来"时才用,且一律先剥注释/docstring。
   教训:只断言"源码含某个词"的锁,换个写法就假绿;而一轮踩过 5 次的是
   断言命中了**解释这件事的注释**,把真代码删掉锁照样绿。

🔴 每条"必须命中"都配一条"必须不命中"。只有正向断言的锁可能是恒真的。
"""
from __future__ import annotations

import ast
import asyncio
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
API = ROOT / "api" / "geo_douyin_api.py"
DB = ROOT / "db" / "geo_douyin_db.py"
PROD = ROOT / "services" / "geo_douyin" / "production_task.py"
REDRAW = ROOT / "services" / "geo_douyin" / "redraw.py"
PIPELINE = ROOT / "services" / "geo_douyin" / "image_pipeline.py"
# [WO_271 · 2026-09-23] 旧入口页 DouyinImagePost.tsx 已删(6b491ab23,09-08 #150 §4)。
# 读它的格改指现役 /writing/image-note 的后继文件、按后继内容重判;随整页消失的格原位退役,
# 登记在 tests/RETIRED_TESTS.txt(接替者写在那里)。
WRITING = ROOT / "frontend" / "src" / "pages" / "Writing"
TOPIC_PANEL = WRITING / "ImageNoteTopicPanel.tsx"   # 左栏:选题 / 状态 / 开始制作
TOPICS = WRITING / "imageNoteTopics.ts"             # 行的形状:状态徽章表 / 失败原因
PRODUCTION = WRITING / "imageNoteProduction.ts"     # 下单载荷
ROUTE = WRITING / "ImageNoteDetailRoute.tsx"        # 图文工作台路由
DETAIL = WRITING / "DouyinPostDetail.tsx"           # 作品详情:制作进度 / 再次创作
HOOK = ROOT / "frontend" / "src" / "hooks" / "useDouyinPostTask.ts"
NGINX = ROOT / "nginx.conf"


# ─────────────────────────────────────────────────────────────
# 工具:剥注释(先证明剥这件事本身有判别力)
# ─────────────────────────────────────────────────────────────
def _fn_node(path: pathlib.Path, name: str):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{path.name} 里找不到函数 {name}")


def _code_of(path: pathlib.Path, name: str) -> str:
    node = _fn_node(path, name)
    body = list(node.body)
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        body = body[1:]
    return ast.unparse(ast.Module(body=body, type_ignores=[])) if body else ""


def _sql_of(path: pathlib.Path, name: str) -> str:
    src = _code_of(path, name)
    return "\n".join(
        (ln if ln.find("--") < 0 else ln[:ln.find("--")]) for ln in src.splitlines())


def _tsx(path: pathlib.Path) -> str:
    src = path.read_text(encoding="utf-8")
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"(?<!:)//[^\n]*", "", src)


def test_comment_stripper_has_power():
    """必须不命中面:剥注释本身要有判别力,否则下面的源码锁全是恒真的。"""
    raw_prod = PROD.read_text(encoding="utf-8")
    # 这两个词只出现在注释/docstring 里,不在任何一个被断言的函数体中
    assert "必然 504" in raw_prod, "样本前提变了:注释里已没有这句"
    assert "必然 504" not in _code_of(PROD, "dispatch_production")
    # [WO_271] 前端样本改用详情页(旧入口页已删;详情页文件头注释里同样写着这句)
    raw_tsx = DETAIL.read_text(encoding="utf-8")
    assert "禁两套文案" in raw_tsx
    assert "禁两套文案" not in _tsx(DETAIL)


# ═════════════════════════════════════════════════════════════
# A1. 进度语义(纯函数 · 行为锁)
# ═════════════════════════════════════════════════════════════
def _prog(**kw):
    from services.geo_douyin.task_progress import describe_task_progress
    now = kw.pop("now", 1_000_000.0)
    post_status = kw.pop("post_status", "")
    task = kw or None
    return describe_task_progress(task, now=now, post_status=post_status)


def test_percent_advances_with_cards_done():
    """images 阶段的百分比必须跟着"第几张"走 —— 这是"真进度"的定义。"""
    from services.geo_douyin.task_progress import compute_percent
    p0 = compute_percent("images", 0, 6)
    p3 = compute_percent("images", 3, 6)
    p6 = compute_percent("images", 6, 6)
    assert p0 < p3 < p6, f"进度没有随张数推进: {p0}/{p3}/{p6}"


def test_percent_does_not_move_without_cards():
    """必须不命中面:非 images 阶段不拿 done/total 插值(那个数没意义)。"""
    from services.geo_douyin.task_progress import compute_percent
    assert compute_percent("copy", 0, 6) == compute_percent("copy", 5, 6)


def test_percent_never_exceeds_hundred():
    from services.geo_douyin.task_progress import compute_percent
    # 分母比分子小(截断后分母改小的竞态)也不该冒出 120%
    assert 0 <= compute_percent("images", 99, 3) <= 100


def test_running_task_reports_running_and_active():
    out = _prog(status="running", stage="images", progress_done=2,
                progress_total=6, started_at=999_900.0, updated_at=999_990.0)
    assert out["state"] == "running"
    assert out["active"] is True
    assert out["done"] == 2 and out["total"] == 6


def test_no_progress_for_long_enough_is_stalled():
    """进程重启会让内存里的后台任务消失,而任务行停在 running。
    没有这条判定,前端会**永远转圈**。"""
    from services.geo_douyin.config import TASK_STALE_SECONDS
    out = _prog(status="running", stage="images", progress_done=1, progress_total=6,
                started_at=1.0, updated_at=1.0, now=1.0 + TASK_STALE_SECONDS + 5)
    assert out["state"] == "stalled"
    assert out["stalled"] is True
    assert out["active"] is False, "卡住了还继续轮 = 永远转圈"


def test_recently_updated_task_is_not_stalled():
    """必须不命中面:刚更新过的任务绝不能被判成卡住(否则正常任务会被误杀)。"""
    from services.geo_douyin.config import TASK_STALE_SECONDS
    out = _prog(status="running", stage="images", progress_done=1, progress_total=6,
                started_at=1.0, updated_at=1.0, now=1.0 + TASK_STALE_SECONDS - 5)
    assert out["state"] == "running" and out["stalled"] is False


def test_finished_task_is_never_stalled_however_old():
    """必须不命中面:成功的任务放一个月也不是"卡住"。
    这条曾是最容易写错的地方 —— 只按"多久没动"判会把所有历史任务全判成卡住。"""
    out = _prog(status="succeeded", stage="done", progress_done=6, progress_total=6,
                started_at=1.0, updated_at=1.0, now=1.0 + 86400 * 30)
    assert out["state"] == "succeeded"
    assert out["stalled"] is False
    assert out["percent"] == 100
    assert out["active"] is False


def test_missing_task_row_is_queued_not_failed():
    """点完"做这条"到后台真建行之间有个窗口,那一瞬间不该显示成失败。"""
    out = _prog(post_status="generating")
    assert out["state"] == "queued" and out["active"] is True


def test_missing_task_row_but_post_ready_is_success():
    out = _prog(post_status="ready")
    assert out["state"] == "succeeded" and out["active"] is False


def test_eta_is_none_when_unknowable():
    """估不出来就返回 None —— 前端据此不显示倒计时,而不是瞎编一个数。"""
    from services.geo_douyin.task_progress import estimate_remaining_seconds
    assert estimate_remaining_seconds("images", 0, 0) is None


def test_eta_shrinks_as_cards_finish():
    from services.geo_douyin.task_progress import estimate_remaining_seconds
    early = estimate_remaining_seconds("images", 1, 6)
    late = estimate_remaining_seconds("images", 5, 6)
    assert early > late > 0


def test_stage_labels_are_plain_language():
    """元指令:工程术语全站翻人话。阶段名不许把 copy/images 直接吐给用户。"""
    from services.geo_douyin.task_progress import STAGE_LABELS, stage_label
    for raw, label in STAGE_LABELS.items():
        assert not re.search(r"[a-zA-Z]", label), f"阶段 {raw} 的文案里有英文: {label}"
    # 未知阶段也要有人话兜底,不能把原始 stage 串回显
    assert stage_label("some_internal_stage") == "正在制作"


# ═════════════════════════════════════════════════════════════
# A2. 冻结失败必须留下可查的记录(异步化引入的新失败面)
# ═════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_freeze_failure_leaves_queryable_failed_task(monkeypatch):
    """余额不足是最常见的失败。异步化之后如果它不落任务行,
    前端轮询什么也查不到 → 永远转圈。"""
    calls = {"task": [], "post": [], "created": 0}

    def _create_task(**kw):
        calls["created"] += 1
        return 777

    async def _bad_freeze(*a, **kw):
        raise RuntimeError("insufficient points")

    monkeypatch.setattr("db.geo_douyin_db.create_task", _create_task)
    monkeypatch.setattr("db.geo_douyin_db.update_task",
                        lambda tid, **kw: calls["task"].append((tid, kw)))
    monkeypatch.setattr("db.geo_douyin_db.set_post_status",
                        lambda pid, st: calls["post"].append((pid, st)))
    monkeypatch.setattr("middleware.billing.freeze_points", _bad_freeze)

    from services.geo_douyin.production_task import run_image_post_production
    outcome = await run_image_post_production(
        post_id=42, user_id=1, keyword="全屋定制", card_count=3)

    assert outcome.ok is False
    assert calls["created"] == 1, "任务行必须在冻结**之前**就建好"
    assert any(kw.get("status") == "failed" for _tid, kw in calls["task"]), \
        "冻结失败没落任务行 → 前端查不到 → 永远转圈"
    assert ("freeze" in " ".join(str(kw.get("stage")) for _t, kw in calls["task"])), \
        "失败阶段要标成 freeze,否则人话翻译对不上"
    assert (42, "failed") in calls["post"]


@pytest.mark.asyncio
async def test_freeze_failure_never_calls_release(monkeypatch):
    """必须不命中面:冻结**没成功**时绝不能去 release 一个不存在的冻结
    —— 那是资金面的错误调用。"""
    released = []

    async def _bad_freeze(*a, **kw):
        raise RuntimeError("insufficient points")

    async def _release(**kw):
        released.append(kw)

    monkeypatch.setattr("db.geo_douyin_db.create_task", lambda **kw: 778)
    monkeypatch.setattr("db.geo_douyin_db.update_task", lambda *a, **kw: None)
    monkeypatch.setattr("db.geo_douyin_db.set_post_status", lambda *a, **kw: None)
    monkeypatch.setattr("middleware.billing.freeze_points", _bad_freeze)
    monkeypatch.setattr("middleware.billing.release_freeze", _release)

    from services.geo_douyin.production_task import run_image_post_production
    await run_image_post_production(post_id=43, user_id=1, keyword="k", card_count=2)
    assert released == [], f"冻结失败却调了退款: {released}"


def test_freeze_failure_maps_to_plain_language():
    """人话翻译要真的覆盖到这条路径的 error_msg 前缀。"""
    from api.geo_douyin_api import humanize_failure
    said = humanize_failure("freeze", "freeze_failed: insufficient points")
    assert "算力" in said or "充值" in said
    # 必须不命中面:兜底不回显原始串(里面可能带供应商/内部实现线索)
    assert "insufficient" not in said


# ═════════════════════════════════════════════════════════════
# A3. 后台调度:强引用防 GC + 不阻塞请求
# ═════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_dispatch_returns_immediately_and_keeps_strong_ref(monkeypatch):
    """下单端点必须**立刻**返回。这正是 60s nginx 超时下唯一能跑通的形态。"""
    started = asyncio.Event()
    release = asyncio.Event()

    async def _slow(**kw):
        started.set()
        await release.wait()
        return None

    monkeypatch.setattr(
        "services.geo_douyin.production_task.run_image_post_production", _slow)
    from services.geo_douyin import production_task as pt

    task = pt.dispatch_production(post_id=1, user_id=1, keyword="k")
    await asyncio.wait_for(started.wait(), timeout=2)
    # 活还在跑的时候,调度器必须已经返回了,并且持有强引用
    assert not task.done()
    assert pt.running_production_count() >= 1, \
        "没有强引用 → 事件循环可能把后台任务 GC 掉(表现是偶尔跑一半没了)"
    release.set()
    await task
    assert pt.running_production_count() == 0, "跑完要从集合里摘掉,否则是泄漏"


@pytest.mark.asyncio
async def test_redraw_dispatch_also_keeps_strong_ref(monkeypatch):
    """重抽同样是 fire-and-forget,同一个 GC 坑对它一样成立。
    (这条曾经漏掉:只给生产加了强引用集合,重抽那条没人测。)"""
    started = asyncio.Event()
    release = asyncio.Event()

    class _Res:
        ok, error, card_index, oss_key, redraw_used, limit_reached = \
            True, "", 0, "k", 1, False

    async def _slow(post, idx, *, hint=""):
        started.set()
        await release.wait()
        return _Res()

    monkeypatch.setattr("db.geo_douyin_db.create_task", lambda **kw: 900)
    monkeypatch.setattr("db.geo_douyin_db.update_task", lambda *a, **kw: None)
    monkeypatch.setattr("services.geo_douyin.redraw.redraw_one_card", _slow)

    from services.geo_douyin import redraw as rd
    task_id, task = await rd.dispatch_redraw({"id": 5, "created_by": 1}, 0)
    await asyncio.wait_for(started.wait(), timeout=2)
    assert task_id == 900
    assert not task.done(), "重抽端点必须立刻返回(单张 49-73s > nginx 60s)"
    assert rd.running_redraw_count() >= 1, "没强引用 → 后台重抽可能被 GC 掉"
    release.set()
    await task
    assert rd.running_redraw_count() == 0


def test_dispatch_uses_module_level_strong_ref_set():
    """结构锁:强引用集合必须是**模块级**的。
    写成函数内的局部变量 → 函数一返回集合就没了,等于没有强引用。"""
    tree = ast.parse(PROD.read_text(encoding="utf-8"))
    names = set()
    for node in tree.body:                      # 只看模块顶层
        if isinstance(node, ast.Assign):
            names |= {t.id for t in node.targets if isinstance(t, ast.Name)}
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            # `_RUNNING_PRODUCTIONS: set = set()` 是 AnnAssign 不是 Assign ——
            # 只认 Assign 的判据会把带类型标注的写法误判成"不在模块级"
            names.add(node.target.id)
    assert "_RUNNING_PRODUCTIONS" in names, "强引用集合不在模块级"
    body = _code_of(PROD, "dispatch_production")
    assert "_RUNNING_PRODUCTIONS.add" in body
    assert "add_done_callback" in body, "不摘除就是内存泄漏"


# ═════════════════════════════════════════════════════════════
# A4. 逐张进度:回调 + 原子自增
# ═════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_render_group_ticks_once_per_card(monkeypatch):
    ticks = []

    class _R:
        def __init__(self, i):
            self.ok, self.oss_key, self.cost_usd, self.error = True, f"k{i}", 0.0085, ""

    seq = {"n": 0}

    async def _fake_one(post_id, idx, headline, sub="", **kw):
        seq["n"] += 1
        return _R(seq["n"])

    async def _fake_sign(keys):
        return ["https://example.invalid/x.jpg"]

    monkeypatch.setattr("services.geo_douyin.image_pipeline.render_one_card", _fake_one)
    monkeypatch.setattr("services.geo_douyin.image_pipeline.signed_card_urls", _fake_sign)

    from services.geo_douyin.image_pipeline import render_prompt_group
    specs = [{"kind": "cover", "prompt": "a"}, {"kind": "content", "prompt": "b"},
             {"kind": "closing", "prompt": "c"}]
    await render_prompt_group(1, specs, on_card_done=lambda: ticks.append(1))
    assert len(ticks) == 3, f"3 张卡只记了 {len(ticks)} 次进度"


@pytest.mark.asyncio
async def test_progress_callback_failure_never_kills_the_batch(monkeypatch):
    """进度是附属品,产物才是主线:回调抛异常绝不能让已出的图作废。"""
    class _R:
        ok, oss_key, cost_usd, error = True, "k", 0.0085, ""

    async def _fake_one(*a, **kw):
        return _R()

    async def _fake_sign(keys):
        return []

    monkeypatch.setattr("services.geo_douyin.image_pipeline.render_one_card", _fake_one)
    monkeypatch.setattr("services.geo_douyin.image_pipeline.signed_card_urls", _fake_sign)

    def _boom():
        raise RuntimeError("db down")

    from services.geo_douyin.image_pipeline import render_prompt_group
    batch = await render_prompt_group(
        1, [{"kind": "cover", "prompt": "a"}, {"kind": "content", "prompt": "b"}],
        on_card_done=_boom)
    assert batch.ok_count == 2


@pytest.mark.asyncio
async def test_async_progress_callback_is_awaited(monkeypatch):
    """回调里有 DB 往返,必须允许它是 async 的 ——
    否则实现方只能同步调 psycopg2,那是事件循环阻塞红线。"""
    awaited = []

    class _R:
        ok, oss_key, cost_usd, error = True, "k", 0.0085, ""

    async def _fake_one(*a, **kw):
        return _R()

    async def _cb():
        await asyncio.sleep(0)
        awaited.append(1)

    monkeypatch.setattr("services.geo_douyin.image_pipeline.render_one_card", _fake_one)
    from services.geo_douyin.image_pipeline import render_prompt_group
    await render_prompt_group(1, [{"kind": "content", "prompt": "a"}], on_card_done=_cb)
    assert awaited == [1], "返回协程没有被 await → async 回调形同虚设"


def test_progress_bump_is_atomic_in_sql():
    """🔴 并发是 8。"先读出来 +1 再写回去"会在两张卡同时完成时丢计数
    —— 而且不会报错,只会让进度条永远差几张然后突然跳到 100%。"""
    sql = _sql_of(DB, "bump_task_progress")
    assert re.search(r"progress_done\s*,?\s*0\s*\)\s*\+\s*1", sql.replace("\n", " ")) \
        or "progress_done, 0) + 1" in sql.replace("\n", " "), \
        f"自增不在 SQL 里: {sql}"
    # 必须不命中面:函数体里不许出现"先 SELECT 出来"的读改写形态
    assert "SELECT" not in sql.upper(), "读改写形态会丢计数"


def test_stale_detection_columns_are_actually_selected():
    """🔴 少选 created_at/updated_at → 卡住判定永远判不出来 → 永远转圈。
    这是"锁一直都在,只是没人跑"的同型坑:判定逻辑写了,但输入没给。"""
    sql = _sql_of(DB, "get_latest_tasks_for_posts")
    assert "updated_at" in sql and "created_at" in sql


# ═════════════════════════════════════════════════════════════
# A5. 三个长端点都不再同步等(漏一个就还是会 504)
# ═════════════════════════════════════════════════════════════
@pytest.mark.parametrize("fn", ["api_create_and_produce", "api_regenerate_post"])
def test_long_endpoints_do_not_await_production(fn):
    body = _code_of(API, fn)
    assert "dispatch_production" in body, f"{fn} 没走后台调度"
    assert "await run_image_post_production" not in body, \
        f"{fn} 还在同步等生产 —— 生产 nginx 60s 必然 504"


def test_redraw_endpoint_does_not_await_generation():
    body = _code_of(API, "api_redraw_card")
    assert "dispatch_redraw" in body
    assert "await redraw_one_card" not in body, \
        "单张实测 49-73s > nginx 60s,同步会出现「前端报失败但图已生成、额度已扣」"


def test_redraw_limit_is_still_rejected_synchronously():
    """额度用满是当场就能判的,不该丢到后台让用户等着轮询才知道。

    🔴 判据必须落在**那个比较**上,不能只断言函数体里出现过 "REDRAW_LIMIT"/"429"
       —— 变异实测:把条件改成 `if False:` 之后,这两个词仍在错误分支里,
       只断言词的锁照样绿(弱锁:锚点没打在锁真正断言的东西上)。
    """
    node = _fn_node(API, "api_redraw_card")
    compares = [n for n in ast.walk(node) if isinstance(n, ast.Compare)]
    hit = [c for c in compares
           if any(isinstance(cmp_, ast.GtE) for cmp_ in c.ops)
           and "REDRAW_LIMIT_PER_POST" in ast.unparse(c)]
    assert hit, "端点里没有「已用次数 >= 上限」这个比较 → 额度拦截被架空了"
    assert "429" in _code_of(API, "api_redraw_card")


def _server_block(conf: str, name_line: str) -> str:
    """[WO_293] 容器 nginx 有两个 server 块(应用兜底 `_` + 官网 omnirank.cn),判据只看点名那一块:
    从含 `name_line` 的块的 `server {` 起按大括号配平截到它的 `}`。找不到 / 不唯一 ⇒ 红。"""
    assert conf.count(name_line) == 1, f"nginx.conf 里 `{name_line}` 应恰 1 处,读到 {conf.count(name_line)}"
    at = conf.index(name_line)
    start = conf.rindex("server {", 0, at)
    depth = 0
    for i in range(start, len(conf)):
        if conf[i] == "{":
            depth += 1
        elif conf[i] == "}":
            depth -= 1
            if depth == 0:
                return conf[start:i + 1]
    raise AssertionError("server 块没配平")


def test_nginx_has_no_special_long_timeout_for_this_prefix():
    """本单的前提事实:/api/geo-douyin/* 没有专属 location,走默认 60s。
    🔴 这条锁的作用是——**哪天有人给它加了长超时**,就会红,
       提醒复审重新判断"异步化还需不需要"。反过来说,只要它是绿的,
       上面那些"必须异步"的断言就有真实依据。"""
    conf = NGINX.read_text(encoding="utf-8")
    assert "geo-douyin" not in conf, \
        "nginx 里出现了 geo-douyin 专属配置,本单关于 60s 的前提要重新核"
    # [WO_293] 官网块也有一条 `location /api/`(返回 404),默认 60s 说的是应用兜底块那条
    app = _server_block(conf, "server_name _;")
    m = re.search(r"location /api/ \{(.*?)\n    \}", app, flags=re.S)
    assert m, "找不到默认 /api/ location,前提事实要重新核"
    assert "proxy_read_timeout 60s" in m.group(1)


# ═════════════════════════════════════════════════════════════
# B. 创建入口页补课(四条)
# ═════════════════════════════════════════════════════════════
# ── [WO_271] 退役:test_entry_page_banner_collapses_to_one_line ──
# 锁的是旧入口页那条试点说明横幅(pilot-banner / bannerOpen / 悬浮展开),横幅随页删除
# (6b491ab23),现役图文工作台没有这块。接替者:必跑集
# tests/test_geo_douyin_detail_ui.py::test_retired_image_note_pages_stay_deleted(pilot-banner
# 在前端源码里再出现就红)。登记:tests/RETIRED_TESTS.txt。


def test_entry_page_has_status_badges_for_every_state():
    """③ 状态徽章:草稿 / 制作中 / 待发布 / 已发布 / 失败已退款。"""
    # [WO_271] 改指后继:状态的人话在 imageNoteTopics 的 STATUS_LABEL(选题行 / 关键词行共用),
    #   面板照它渲染。旧页那五态是「作品」的状态;后继左栏列的是选题,状态换成这一套 ——
    #   「失败已退款」的退款那半句由详情页在失败时说。
    topics = _tsx(TOPICS)
    for state, label in (("pending", "未制作"), ("making", "制作中"), ("done", "已完成"),
                         ("failed", "没做成"), ("archived", "已归档"), ("unknown", "状态未知")):
        assert f"{state}: '{label}'" in topics, f"缺状态徽章 {state}/{label}"
    # 🔴 分母取**生产方**:关键词行的状态由 keyword_worklist 写,它写得出的每一态前端都要有人话
    #    (拿徽章表自己核自己是循环论证 —— 生产方多写一态,这里照样全绿)
    worklist = (ROOT / "services" / "geo_douyin" / "keyword_worklist.py").read_text(encoding="utf-8")
    produced = set(re.findall(r'state = "(\w+)"', worklist))
    assert {"pending", "making", "done", "failed"} <= produced, f"生产方状态只读出 {produced} —— 分母塌了"
    for state in sorted(produced):
        assert (f"{state}: '" in topics or f"o.production_status === '{state}'" in topics), \
            f"生产方会写 {state},前端没有它的人话(会显示成「状态未知」)"
    assert 'data-testid="topic-status">· {r.statusLabel}' in _tsx(TOPIC_PANEL), "面板没把状态徽章画出来"
    assert "这次没做成，费用已自动退回" in _tsx(DETAIL), "失败时没告诉用户钱已经退了"


def test_status_badge_map_is_single_source():
    """必须不命中面:徽章文案只能有**一处**定义。
    同一事实写两处 → 改一处另一处还在,是本项目栽过的弱锁四型之一。"""
    # [WO_271] 改指后继,按后继重判:状态 → 文案的表只许一处(imageNoteTopics 的 STATUS_LABEL),
    #   行上的徽章查它来,面板不再自己写一张表。筛选 chip 的名字另写了一份 → 拆到下一格
    #   xfail(WO_283-F7);这一格守「表只有一张」这一半,不跟着一起 xfail。
    flow = {f.name: _tsx(f) for f in (TOPIC_PANEL, TOPICS, PRODUCTION, ROUTE, DETAIL)}
    homes = [name for name, src in flow.items() if "STATUS_LABEL: Record<string, string> = {" in src]
    assert homes == [TOPICS.name], f"状态徽章表不是只有一处:{homes}"
    assert "statusLabel: STATUS_LABEL[status]" in flow[TOPICS.name], "行上的徽章不是查那张表来的"


def _chip_label_redefinitions(topics: str, panel: str) -> list:
    """面板里又手写了徽章表里的文案 ⇒ 返回那几条;分母自证读不出表 ⇒ 抛。"""
    m = re.search(r"const STATUS_LABEL: Record<string, string> = \{(.*?)\};", topics, flags=re.S)
    assert m, "找不到徽章表"
    labels = re.findall(r"'([^']+)'", m.group(1))
    assert len(labels) >= 4, f"徽章表只读出 {labels} —— 分母塌了"
    return [lb for lb in labels if f"'{lb}'" in panel]


def test_status_chip_names_come_from_the_badge_map():
    """🔴 同一个状态的人话只许写一处(上一格原来的判据,WO_271 拆出)。

    后继面板的筛选 chip(CHIPS)又手写了一遍「制作中 / 已完成 / 没做成」,待做那一档还叫
    「未制作」,与徽章表的「待做」已经不一致 —— 改一处另一处还在,正是这把锁要防的漂移。
    [WO_283-F7 已修] 徽章表把 pending 统一叫「未制作」(默认关键词视图与 chip 本来就这么叫),
    chip 各档名字一律 `statusText(...)` 从表里取。
    """
    redefined = _chip_label_redefinitions(_tsx(TOPICS), _tsx(TOPIC_PANEL))
    assert not redefined, f"面板里又手写了徽章表里的文案:{redefined}"
    assert "label: statusText('pending')" in _tsx(TOPIC_PANEL), "chip 不是从徽章表取的名字"


def test_status_chip_lock_has_power():
    """反臂(WO_283):chip 改回手写 ⇒ 上一格必须报;现役源码不报(对照)。"""
    topics, panel = _tsx(TOPICS), _tsx(TOPIC_PANEL)
    assert _chip_label_redefinitions(topics, panel) == []
    assert _chip_label_redefinitions(topics, panel.replace("statusText('making')", "'制作中'"))


def test_entry_page_shows_plain_failure_and_retry():
    """④ 失败条目给人话原因 + 一键重试。"""
    # [WO_271] 改指后继:失败行把后端给的人话原因画出来(r.failure ← failure_reason),
    #   「查看详情并重试」进详情页,重试走既有的「再次创作」端点(regenerate),不新造计费路径。
    panel = _tsx(TOPIC_PANEL)
    # 🔴 断言仍落在**渲染点**上(不是类型声明)
    assert "{r.failure}" in panel and 'data-testid="topic-failure"' in panel, \
        "没有把后端下发的人话原因渲染出来"
    assert "failure: str(o.failure_reason)" in _tsx(TOPICS), "行上的失败原因不是后端下发的那句"
    assert "查看详情并重试" in panel, "没有重试入口"
    assert "/api/geo-douyin/posts/${postId}/regenerate" in _tsx(DETAIL), "重试没走既有的再次创作端点"


def test_entry_page_never_hardcodes_price():
    """价目表 SSOT:前端不许把 390/260 写死(Owner 调价不该要改前端代码)。"""
    # [WO_271] 改指后继:两处价都从服务端来(/pricing 与 production-quote),前端一个数都不写死
    assert "/api/geo-douyin/pricing" in _tsx(TOPIC_PANEL)
    for f in (TOPIC_PANEL, TOPICS, PRODUCTION, ROUTE):
        assert not re.search(r"\b(390|260)\b", _tsx(f)), f"{f.name} 写死了价格"


def test_entry_page_polls_list_not_per_post():
    """5 个城市同时做也应该只有一条轮询请求 —— 列表接口已带每条的 progress。"""
    # [WO_271] 改指后继:制作中的行由面板**一条**列表心跳统一刷新(4 秒 · 静默重读,
    #   列表接口已带每行状态),不给每一行各起一个单条轮询。
    src = _tsx(TOPIC_PANEL)
    assert "buckets.making.length" in src and "load(brandId, ac.signal, 'refresh')" in src
    assert "setInterval" in src
    # 必须不命中面:面板不该给每条内容各起一个单条轮询
    assert "useDouyinPostTask(" not in src


def test_entry_page_progress_comes_from_backend():
    """进度必须是后端下发的真数据,不是前端模拟的动画。"""
    # [WO_271] 改指详情页:制作进度在详情页画(左栏行上只报状态),取的是后端任务的
    #   stage_label / percent(useDouyinPostTask),不在前端模拟。
    src = _tsx(DETAIL)
    assert "taskProgress.percent" in src and "taskProgress.stage_label" in src
    # 必须不命中面:不许自己按时间推算百分比
    assert "Date.now()" not in src


def test_hook_stops_polling_on_backend_active_flag():
    """前端不自己判"算不算跑完",以后端 active 为准。"""
    src = _tsx(HOOK)
    assert "next.active" in src and "stop()" in src
    # 必须不命中面:不许自建第二套 SSE(元指令 15)
    assert "EventSource" not in src and "WebSocket" not in src


def test_eta_formatter_returns_empty_when_unknown():
    """估不出来就不显示,**不瞎编一个数**。"""
    src = _tsx(HOOK)
    m = re.search(r"export function formatEta[\s\S]*?\n\}", src)
    assert m, "找不到 formatEta"
    assert "return ''" in m.group(0)
