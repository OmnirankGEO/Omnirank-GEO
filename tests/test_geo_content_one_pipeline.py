"""SSOT 锁 · GEO 内容生产是一条流水线(不是两条)

配套文档:`docs/AI-CONTEXT/SSOT_GEO_CONTENT_ONE_PIPELINE_2026-08-03.md`

🔴 存在的理由:2026-07-30 本项目有实证 —— 当天四类失误的规则**全都已在 SSOT 里、
   且当天读过**,照样踩。结论是「只写进文档的规则读过也会踩」,
   高频规则必须落成**执行时会自己报错的东西**。这个文件就是那个"东西"。

铁律一句话:**客户(brand)是唯一作用域;文章与图文只有"制作流程"不同,其余全部相通。**

🔴 本文件里的锁分两类,标注清楚,别混:
   - ✅ 已对齐 → 锁住,防回退
   - ⏳ 迁移期 → **xfail(strict)**,修完那一刻会因为"意外通过"而转红,
     提醒把 xfail 摘掉。这样既不让基线长期红(红着的锁没人看),
     又不会让"还没修"被读成"已经对了"。
"""
from __future__ import annotations

import ast
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
GEO_API = ROOT / "api" / "geo_douyin_api.py"
# [WO_271 · 2026-09-23] 旧入口页 DouyinImagePost.tsx 已删(6b491ab23,09-08 #150 §4)。
# 读它的格改指现役 /writing/image-note 的后继文件;随整页消失的格原位退役,
# 登记在 tests/RETIRED_TESTS.txt(接替者写在那里)。
WRITING = ROOT / "frontend" / "src" / "pages" / "Writing"
TOPIC_PANEL = WRITING / "ImageNoteTopicPanel.tsx"   # 选词 / 蒸馏 / 开始制作
PRODUCTION = WRITING / "imageNoteProduction.ts"     # 下单载荷
ROUTE = WRITING / "ImageNoteDetailRoute.tsx"        # 图文工作台路由(客户从这里交给面板)
DETAIL = ROOT / "frontend" / "src" / "pages" / "Writing" / "DouyinPostDetail.tsx"
WORKSPACE = ROOT / "frontend" / "src" / "pages" / "Writing" / "WritingWorkspace.tsx"
HALL = ROOT / "frontend" / "src" / "pages" / "Writing" / "WritingHall.tsx"
KB_CARD = (ROOT / "frontend" / "src" / "components" / "client"
           / "ClientKnowledgeCard.tsx")
PUB_CENTER = ROOT / "frontend" / "src" / "pages" / "Publishing" / "PublishCenter.tsx"
SV_PANEL = ROOT / "frontend" / "src" / "pages" / "Publishing" / "ShortVideoPanel.tsx"
SSOT_DOC = (ROOT / "docs" / "AI-CONTEXT"
            / "SSOT_GEO_CONTENT_ONE_PIPELINE_2026-08-03.md")


def _image_note_flow() -> list:
    """[WO_271] 图文创作流:pages/Writing 下 ImageNote* / imageNote* **机械枚举** + 详情页。

    不手挑 —— 手挑的清单正是旧入口页删掉后这些锁一起失去对象的同一种病。
    """
    files = sorted(p for p in WRITING.iterdir()
                   if p.is_file() and p.suffix in (".ts", ".tsx")
                   and p.name.startswith(("ImageNote", "imageNote")))
    # 分母自证:枚举塌了(改名 / 挪目录)⇒ 每条「不许出现」都会空过变绿
    assert TOPIC_PANEL in files and ROUTE in files and PRODUCTION in files, (
        f"图文创作流只枚举到 {[p.name for p in files]} —— 分母塌了,先修枚举")
    return files + [DETAIL]


def _tsx(p: pathlib.Path) -> str:
    """剥注释 —— 本项目同一个坑踩过五次以上:断言命中的是**解释它的注释**,
    把真代码删掉锁照样绿。"""
    s = p.read_text(encoding="utf-8")
    s = re.sub(r"/\*.*?\*/", "", s, flags=re.S)
    return re.sub(r"(?<!:)//[^\n]*", "", s)


def test_comment_stripper_has_power():
    """前提面:剥注释本身要有判别力,否则下面的源码锁全是恒真的。"""
    raw = DETAIL.read_text(encoding="utf-8")
    assert "禁两套文案" in raw, "样本前提变了"
    assert "禁两套文案" not in _tsx(DETAIL)


# ═════════════════════════════════════════════════════════════
# L1/L2 · 客户是唯一作用域
# ═════════════════════════════════════════════════════════════
def test_create_post_accepts_brand_id():
    """后端**能**接住 brand_id(这一半早就有了,锁住防回退)。"""
    tree = ast.parse(GEO_API.read_text(encoding="utf-8"))
    cls = next(n for n in ast.walk(tree)
               if isinstance(n, ast.ClassDef) and n.name == "CreatePostRequest")
    fields = {t.target.id for t in cls.body if isinstance(t, ast.AnnAssign)
              and isinstance(t.target, ast.Name)}
    assert "brand_id" in fields, "创建请求连 brand_id 字段都没有"


def test_brand_id_is_persisted_on_create():
    """落库时必须把 brand_id 带上 —— 不落 = 下游知识库/联系方式/黄点全空转。"""
    tree = ast.parse(GEO_API.read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef)
              and n.name == "api_create_and_produce")
    body = ast.unparse(fn)
    assert "brand_id=req.brand_id" in body, "建作品时没把客户带进去"


def test_entry_page_sends_brand_id():
    """🔴 L2:前端下单必须带 brand_id。

    这是整条知识库链的**根**。生产实测:geo_douyin_posts 的 brand_id 全 NULL,
    因为 produce() 只发 {keyword, city, card_count}。
    """
    # [WO_271] 下单载荷搬到 imageNoteProduction.productionBatch;整条链:
    #   全局 ClientContext → 工作台路由 → 选题面板 → 载荷,一环都不许断
    src = _tsx(PRODUCTION)
    assert "brand_id: brandId" in src, "下单请求体没把选中的客户带上"
    assert "productionBatch(brandId," in _tsx(TOPIC_PANEL), "面板下单时没把当前客户交给载荷"
    assert "brandId={currentBrandId}" in _tsx(ROUTE), "面板拿到的客户不是全局那个"


def test_entry_page_has_no_own_client_selector():
    """入口页**不许**再有自己的客户选择器 —— 客户在创作中心那一层就定了。

    🔴 这条锁 2026-08-03 **语义反转**(原来断言的是"入口页必须有选客户这一步")。
       Owner:「左侧栏左上角是唯一同步客户的地方…GEO 图文里面的用户选择删除掉,
       完全理解错了,这样操作逻辑才能统一,不然是割裂的」。
       同一件事有两个地方能选,就是割裂 —— 原来那条锁锁的正是割裂本身。
    """
    # [WO_271] 改指图文创作流全部文件(机械枚举):哪一个都不许自己搜客户 / 自己拉客户列表。
    #   工作台路由没选客户时那一屏的下拉,选的是**全局那份** clients,选完 switchClient 回写全局
    #   (同 L2c 创作中心那一屏)—— 不是第二个客户选择器。
    for f in _image_note_flow():
        src = _tsx(f)
        assert "client-search" not in src, f"{f.name} 又有了自己的客户搜索框(割裂)"
        assert "/api/my-clients" not in src, f"{f.name} 又自己拉了一份客户列表(割裂)"
    route = _tsx(ROUTE)
    assert "useClientContext" in route, "客户没有来自全局 ClientContext(唯一同步点)"
    assert "onChange={e => switchClient(Number(e.target.value))}" in route, \
        "没选客户时那一屏选完没有回写全局(那就变成了第二个客户选择器)"


def test_workspace_gates_on_client_and_writes_back_to_global():
    """没选客户 → 创作中心先让你选;选完必须**回写全局**,而不是自己存一份。

    🔴 `switchClient` 是判据的核心:少了它,这一屏就变成了"第二个客户选择器"
       (选完左上角还是空的),正是要消灭的那种割裂。
    """
    src = _tsx(WORKSPACE)
    assert "writing-client-picker" in src, "创作中心没有未选客户时的选择屏"
    # 🔴 变异 W4 教训:`switchClient` 在 useClientContext 的解构里也出现,
    #    只断言这个名字,把 onClick 换掉照样绿。判据要打在**那次点击**上。
    assert "onClick={() => switchClient(c.id)}" in src, \
        "点客户没有回写全局 ClientContext(那就变成了第二个客户选择器)"
    assert "isAllClientsMode" in src, "没有放行 admin 的『全部客户』模式"


# ═════════════════════════════════════════════════════════════
# L3/L4 · 不得另起媒体列表 / 下单扣费路径
# ═════════════════════════════════════════════════════════════
def test_detail_page_has_no_parallel_media_list():
    """✅ 已对齐(2026-08-03 删除):图文线不得自建账号列表。

    根因留痕:那个列表 limit=50 且无分页,而合格账号实际 2,561 个 ——
    等于在正确的计费链前面套了个够不着完整池的窄窗口。
    """
    src = _tsx(DETAIL)
    for banned in ("/api/geo-douyin/accounts", "/api/geo-douyin/frequency-check"):
        assert banned not in src, f"图文线又自建媒体列表了: {banned}"


def test_detail_page_has_no_parallel_ordering_path():
    """✅ 已对齐:下单只能走发布中心既有链。"""
    src = _tsx(DETAIL)
    for banned in ("/api/meijiehezi/short-video/publish", "media_ids"):
        assert banned not in src, f"图文线又自建下单路径了: {banned}"


def test_geo_douyin_module_has_no_billing_of_its_own():
    """图文线自己不许扣费 —— 钱只在既有链上。

    🔴 例外:production_task / settlement 是 B 类冻结的合法出口(制作费),
       它们与"发布下单"是两回事,不在本条约束内。

    🔴 判据必须剥 **docstring**,不只是 `#` 注释。实测:image_pipeline.py 与
       content_generator.py 的模块 docstring 里都写着"由调用方 release_freeze
       退积分",只剥 `#` 的版本会把这两个文件误报成扣费点。
       (同型坑本项目已踩六次:断言命中的是解释它的文字,不是代码。)
    """
    allowed = {"production_task.py", "settlement.py"}
    keys = ("freeze_points", "commit_freeze", "release_freeze")
    hits = []
    for f in (ROOT / "services" / "geo_douyin").glob("*.py"):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        # 把所有 docstring 节点摘掉,再 unparse 回源码
        for node in ast.walk(tree):
            body = getattr(node, "body", None)
            if not isinstance(body, list) or not body:
                continue
            first = body[0]
            if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                body.pop(0)
        code = ast.unparse(tree) if tree.body else ""
        if any(k in code for k in keys):
            hits.append(f.name)
    assert set(hits) <= allowed, f"图文线出现了计外的扣费点: {set(hits) - allowed}"


def test_billing_scanner_has_power():
    """必须不命中面:扫描器要真能抓到扣费调用,否则上面那条是恒真的。"""
    src = "async def x():\n    await commit_freeze(freeze_id=1)\n"
    tree = ast.parse(src)
    assert "commit_freeze" in ast.unparse(tree)
    # 反向:只在 docstring 里提到的不算
    src2 = 'def y():\n    """由调用方 commit_freeze 处理"""\n    return 1\n'
    t2 = ast.parse(src2)
    for node in ast.walk(t2):
        body = getattr(node, "body", None)
        if isinstance(body, list) and body:
            f0 = body[0]
            if (isinstance(f0, ast.Expr) and isinstance(f0.value, ast.Constant)
                    and isinstance(f0.value.value, str)):
                body.pop(0)
    assert "commit_freeze" not in ast.unparse(t2), "docstring 没被剥掉"


# ═════════════════════════════════════════════════════════════
# L6 · 知识库读取不得两套
# ═════════════════════════════════════════════════════════════
def test_client_knowledge_has_one_implementation():
    """🔴 L6:「这个客户有什么」只能有**一处**计算。

    入口页的客户卡与详情页右栏的资料卡此前各算一遍
    `load_material_summary` + `count_authorized_images` —— 同一件事两套口径,
    改一处另一处不跟着变,两边显示早晚打架。
    现在两处都调 `build_client_knowledge`。

    判据:那两个底层读取函数在 **api 层一次都不出现**(只能经由共享 builder)。
    """
    api = GEO_API.read_text(encoding="utf-8")
    tree = ast.parse(api)
    # 剥 docstring 再断言 —— 本项目同型坑已踩六次
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(body, list) and body:
            f0 = body[0]
            if (isinstance(f0, ast.Expr) and isinstance(f0.value, ast.Constant)
                    and isinstance(f0.value.value, str)):
                body.pop(0)
    code = ast.unparse(tree)
    for low_level in ("load_material_summary", "count_authorized_images",
                      "load_authorized_image_thumbs"):
        assert low_level not in code, (
            f"api 层直接调了 {low_level} —— 绕过了共享的 build_client_knowledge,"
            f"知识库口径又变成两套")
    assert "build_client_knowledge" in code, "没走共享的知识库读取"


def test_stock_and_sources_stay_separate():
    """必须不命中面:「库存」与「出处」**不许合并**。

    - build_client_knowledge = 这个客户**有什么**
    - describe_sources       = 这条内容**实际引用了什么**
    合并会拿库存冒充出处 —— 用户以为内容引用了实际没引用的东西,比不显示更糟。
    """
    kb = (ROOT / "services" / "client_knowledge.py").read_text(encoding="utf-8")
    tree = ast.parse(kb)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "build_client_knowledge")
    body = list(fn.body)
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)):
        body.pop(0)
    code = ast.unparse(ast.Module(body=body, type_ignores=[]))
    assert "describe_sources" not in code, "库存计算里混进了出处(两者必须分开)"
    assert "generation_meta" not in code, "库存计算读了某条内容的留痕(串味)"


# ═════════════════════════════════════════════════════════════
# L5 · 发布中心三 tab 对称
# ═════════════════════════════════════════════════════════════
def test_publish_center_has_three_tabs():
    """前提面:三个 tab 都在(样本非空,否则下面那条是恒真的)。"""
    src = _tsx(PUB_CENTER)
    for t in ("'article'", "'wemedia'", "'svideo'"):
        assert t in src, f"发布中心缺 tab {t}"


def test_article_tabs_can_pick_content():
    """✅ 参照面:软文/自媒体 tab 本来就能选文章。"""
    src = _tsx(PUB_CENTER)
    assert "请先选择文章" in src, "软文 tab 的选内容那一栏不见了(参照面失效)"


def test_svideo_tab_can_pick_existing_image_post():
    """🔴 L5:短视频 tab 必须能**在中心内**选已做好的图文,
    与软文 tab 选文章对称。

    只做单向入口(详情页跳过来)不算打通 —— 用户直接点短视频 tab
    仍然只能手动传图。
    """
    src = _tsx(SV_PANEL)
    assert "geo-post-picker" in src, "短视频 tab 没有「选已做好的图文」那一栏"
    assert "/api/geo-douyin/posts?" in src, "没去拉已做好的图文列表"
    # 客户是唯一作用域:列表必须按当前项目的客户过滤,否则会串客户
    assert "qs.set('brand_id'" in src, "图文列表没按客户过滤(跨客户串内容)"


def test_prefill_has_exactly_one_implementation():
    """必须不命中面:两条入口(URL 跳转 / 面板内选)共用**同一份**预填。

    🔴 各写一份 = 同一事实写两处,改一处另一处就不一样了。
       判据:prepare-publish-media 在整个面板里只被调用一次。
    """
    src = _tsx(SV_PANEL)
    assert src.count("/api/geo-douyin/prepare-publish-media") == 1, \
        "预填被写了不止一份"
    assert "applyGeoPost" in src, "没有抽出共用的预填函数"


def test_jump_prefill_entry_still_works():
    """✅ 已对齐的那一半:从详情页跳过来能预填。

    锁住它,免得补"中心内选内容"时把跳转那条顺手改坏。
    """
    assert "geoPostId" in _tsx(SV_PANEL), "跳转预填入口没了"
    assert "geo_post_id" in _tsx(PUB_CENTER), "发布中心不再读 geo_post_id"


# ═════════════════════════════════════════════════════════════
# 移动端:hover-only 交互在触屏上等于不存在
# ═════════════════════════════════════════════════════════════
def test_no_hover_only_content():
    """🔴 任何"鼠标移上去才出现"的**内容**在手机上等于不存在。

    实测过的一个真 bug:横幅同时挂 onMouseEnter(置 true)和 onClick(toggle),
    触屏会为一次点击合成 mouseenter → 顺序变成 置true → toggle成false,
    手机上点它"开一下又立刻关掉"。
    修法:用 pointerType 把鼠标与触屏分开,不是删掉悬浮。
    """
    for f in _image_note_flow():   # [WO_271] 旧入口页已删;改扫图文创作流全部文件(含详情页)
        src = _tsx(f)
        assert "onMouseEnter" not in src, (
            f"{f.name} 用了 onMouseEnter —— 触屏会合成它,"
            f"与 click-toggle 打架;改用 onPointerEnter + pointerType 判定")
        # hover 才显形的内容(opacity-0 → hover:opacity-100)在触屏上永远看不到
        assert "hover:opacity-100" not in src, f"{f.name} 有 hover 才显形的内容"


# ── [WO_271] 退役:test_hover_is_gated_on_real_mouse ──
# 反向面防的是「靠删掉悬浮展开来满足上一格」。那个悬浮展开是旧入口页的试点说明横幅
# (pilot-banner),随页一起删了(6b491ab23);现役图文流没有任何悬浮展开,反向面没有对象。
# 上一格 test_no_hover_only_content 改扫现役流程全部文件,照旧守「不许 hover 才出现」;
# 横幅不许回来由必跑集 tests/test_geo_douyin_detail_ui.py::test_retired_image_note_pages_stay_deleted 守。
# 登记:tests/RETIRED_TESTS.txt。


# ═════════════════════════════════════════════════════════════
# 文档与锁必须同步(防"文档改了锁没跟上")
# ═════════════════════════════════════════════════════════════
def test_ssot_doc_exists_and_lists_every_lock():
    """SSOT 文档必须在,且 §5 那张表里点名的锁必须真的存在于本文件。

    🔴 防的是"文档写了一条铁律、却没有对应的锁"——那正是
       「只写进文档的规则读过也会踩」的复发形态。
    """
    assert SSOT_DOC.exists(), "SSOT 文档不在了"
    doc = SSOT_DOC.read_text(encoding="utf-8")
    # 🔴 锁已经散在多个文件里(本文件 / v2 锁文件 / detail_ui 等),元锁扫**整个
    #    tests 目录**。只扫自己的话,搬到别的文件里的锁会被误报成"不存在" ——
    #    而元锁要防的是"文档点名了一把根本不存在的锁",不是"锁必须待在哪个文件"。
    mine: set = set()
    for f in sorted(pathlib.Path(__file__).parent.rglob("test_*.py")):
        try:
            mine |= {n.name for n in ast.walk(ast.parse(f.read_text(encoding="utf-8")))
                     if isinstance(n, ast.FunctionDef)}
        except SyntaxError:
            continue        # 坏文件由它自己的收集阶段报错,不在这里连坐
    named = set(re.findall(r"`(test_[a-z0-9_]+)`", doc))
    missing = {n for n in named if n not in mine}
    assert not missing, f"文档点名了不存在的锁: {missing}"


def test_ssot_doc_marks_unfixed_items_honestly():
    """迁移期状态必须诚实标注 —— 免得被读成"已经对齐了"。"""
    doc = SSOT_DOC.read_text(encoding="utf-8")
    assert "尚未修完" in doc or "未修" in doc, \
        "SSOT 没标注迁移期状态,会被读成全部已对齐"
