"""详情页交互补全 · 验收清单九条的锁(逐条对应工单「编码验收清单」)

  ① 重抽弹窗空输入随机          → test_empty_hint_*
  ② 10 次上限后端拒绝并提示      → test_limit_*
  ③ 扣费提示与价目表实价一致      → test_price_*
  ④ 城市版本切换加载对应作品      → test_city_*
  ⑤ 联系方式开关与收尾卡真联动    → test_contact_*
  ⑥ 账号搜索 + 频控真值          → test_account_search_* / test_frequency_*
  ⑦ 单卡重抽不覆盖他卡           → test_replace_one_card_*
  ⑧ 预览封面起 + 张序 + 与编辑区同步 → test_preview_*
  ⑨ 黄点无数据即隐藏             → test_yellow_dot_* / test_consistency_*

🔴 本文件所有"源码扫描"型断言,一律**先剥注释/docstring 再断言**。
   同一个坑本项目一轮踩过 5 次:断言命中的是*解释这件事的注释*,
   把真代码删掉锁照样绿。见 _code_of() / _sql_of()。

🔴 每条"必须命中"都配一条"必须不命中" —— 只有正向断言的锁可能是恒真的。
"""
from __future__ import annotations

import ast
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
API = ROOT / "api" / "geo_douyin_api.py"
DB = ROOT / "db" / "geo_douyin_db.py"
REDRAW = ROOT / "services" / "geo_douyin" / "redraw.py"
KB_CTX = ROOT / "services" / "geo_douyin" / "knowledge_context.py"
KB_CONS = ROOT / "services" / "geo_douyin" / "kb_consistency.py"
CARD_TPL = ROOT / "services" / "geo_douyin" / "card_templates.py"
PUB_ADAPTER = ROOT / "services" / "geo_douyin" / "publish_adapter.py"
DETAIL = ROOT / "frontend" / "src" / "pages" / "Writing" / "DouyinPostDetail.tsx"
# 🔴 [WO_258 · 2026-09-23] 原创作页 `DouyinImagePost.tsx` 已被 6b491ab23(#150 §4 · 09-08)
#    整页删除 —— 那一笔的「判据退役 k = 0」只扫了 frontend/scripts/*.mjs,没扫本文件,
#    于是引用它的 6 格从 09-08 起一直 FileNotFoundError(本文件当时没有常驻运行者)。
#    图文现在只住 `/writing/image-note[/:postId]`(Owner「唯一一屏」,#204 a2):
WRITING = ROOT / "frontend" / "src" / "pages" / "Writing"
IMAGE_NOTE_ROUTE = WRITING / "ImageNoteDetailRoute.tsx"   # 左栏列表 + 按 id 挂详情
TOPIC_PANEL = WRITING / "ImageNoteTopicPanel.tsx"         # 第 1 步:选题 + 设置 + 提交制作
PRODUCTION = WRITING / "imageNoteProduction.ts"           # 批量建帖请求体(纯函数)
FLOW_STEPS = WRITING / "ImageNoteFlowSteps.tsx"           # 三步条
APP_SIDEBAR = ROOT / "frontend" / "src" / "components" / "layout" / "AppSidebar.tsx"
MANIFEST = ROOT / "db" / "migration_manifest.py"
MIG_020 = ROOT / "db" / "migration_020_geo_douyin_detail_ui_2026_08_02.sql"


# ─────────────────────────────────────────────────────────────
# 取源码的工具:剥掉注释/docstring,只留真代码
# ─────────────────────────────────────────────────────────────
def _fn_node(path: pathlib.Path, name: str):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"找不到函数 {name} —— 锚点失效")


def _code_of(path: pathlib.Path, name: str) -> str:
    """函数的**代码**(docstring 已剥离)。

    🔴 ast.unparse 会把 docstring 原样带出来 —— 一条 docstring 里提到
       `publish_allowed`,针对该标识符的断言就会命中说明文字而不是实现。
    """
    node = _fn_node(path, name)
    body = list(node.body)
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        body = body[1:]
    if not body:
        return ""
    stub = ast.Module(body=body, type_ignores=[])
    return ast.unparse(stub)


def _sql_of(path: pathlib.Path, name: str) -> str:
    """函数里的 SQL 文本(注释已剥)。用于 SQL 结构断言。"""
    src = _code_of(path, name)
    return "\n".join(
        (line if line.find("--") < 0 else line[:line.find("--")])
        for line in src.splitlines()
    )


def _tsx(path: pathlib.Path) -> str:
    """TSX 源码,剥掉 // 行注释与 /* */ 块注释。

    🔴 本文件顶部的大段注释里就写着「一键发布」「published_url」这些词,
       不剥的话针对它们的断言会命中注释。
    """
    src = path.read_text(encoding="utf-8")
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    src = re.sub(r"(?<!:)//[^\n]*", "", src)
    return src


def _image_note_flow_files() -> list[pathlib.Path]:
    """图文创作流的全部前端文件:`pages/Writing/` 下 `ImageNote*` / `imageNote*`,**机械枚举**。

    🔴 不手挑。手挑的清单正是 6b491ab23 漏掉本文件的同一种病:
       「我以为创作页是哪几个文件」≠「创作页现在由哪几个文件组成」。
    """
    files = sorted(p for p in WRITING.iterdir()
                   if p.is_file() and p.suffix in (".ts", ".tsx")
                   and p.name.startswith(("ImageNote", "imageNote")))
    # 分母自证:枚举塌了(改名 / 挪目录)⇒ 下面每条「不许出现」都会空过变绿
    assert TOPIC_PANEL in files and IMAGE_NOTE_ROUTE in files and FLOW_STEPS in files, (
        f"图文创作流只枚举到 {[p.name for p in files]} —— 分母塌了,先修枚举")
    return files


def _flow_sources() -> dict[str, str]:
    return {p.name: _tsx(p) for p in _image_note_flow_files()}


def test_comment_stripper_has_power():
    """必须不命中面:剥注释这件事本身要有判别力,否则上面所有锁都是恒真的。"""
    fake = "const a = 1; // 这里提到 published_url\n/* 也提到 article_type: 3 */\nconst b = 2;"
    out = re.sub(r"/\*.*?\*/", "", fake, flags=re.S)
    out = re.sub(r"(?<!:)//[^\n]*", "", out)
    assert "published_url" not in out and "article_type" not in out, "剥注释没生效 = 恒真锁"
    assert "const b = 2;" in out, "剥过头了,把真代码也删了"


# ═════════════════════════════════════════════════════════════
# ① 重抽:免费 + 空输入随机
# ═════════════════════════════════════════════════════════════
def test_redraw_is_free_no_billing_import():
    """🔴 免费就是**真免费**:重抽模块一行计费代码都不能有。

    "扣 0 分"也不行 —— 那会在账目里留下一条 0 分流水,且随时可能被人改成非 0。
    """
    tree = ast.parse(REDRAW.read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
        elif isinstance(node, ast.Import):
            imported.extend(a.name for a in node.names)
    banned = [m for m in imported if "billing" in m or "wallet" in m]
    assert not banned, f"重抽模块引入了计费/钱包模块: {banned}"


def test_redraw_api_freezes_and_settles_both_ways():
    """重抽收费(Owner 2026-08-03 拍板 100 算力)必须是**完整的 B 类形态**。

    🔴 这条锁 2026-08-03 **语义反转**:原来断言的是"重抽端点不许出现计费调用"
       (那时重抽免费)。Owner 拍板改收费后,原判据变成"锁住一个已经不成立的
       产品决定" —— 留着它只会逼下一个人把正确实现改回去。反转而不是删除:
       删掉等于这条路径从此无锁。

    要点是**三条腿都在**:冻结 + 成功提交 + 失败释放。
    只冻不释放 = 每次失败漏一笔冻结,用户余额被吃掉只能等 12h sweeper。
    """
    code = _code_of(API, "api_redraw_card")
    for fn in ("freeze_points", "commit_freeze", "release_freeze"):
        assert fn in code, f"重抽端点缺少 {fn} —— 收费链不完整"
    # 必须不命中面:失败回调不能是空壳(只写个 pass 就"有 release_freeze"了)
    assert "on_failed=" in code, "没有把失败回调接给 dispatch_redraw,失败时不会退款"


def test_redraw_module_itself_stays_billing_free():
    """反向面:**模块**仍然零计费,扣费只在 API 层。

    资金动作集中在一处才审得动 —— 散进 worker 模块之后,
    "这条链到底扣没扣、扣几次"就没人能一眼看清了。
    """
    import ast

    src = (ROOT / "services" / "geo_douyin" / "redraw.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert "billing" not in (node.module or ""), "redraw 模块 import 了计费模块"
            for alias in node.names:
                assert alias.name not in ("freeze_points", "commit_freeze",
                                          "release_freeze"), \
                    f"redraw 模块导入了计费函数 {alias.name}"
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert "billing" not in alias.name, "redraw 模块 import 了计费模块"


def test_empty_hint_produces_prompt_without_extra_clause():
    """① 空输入 = 原样再跑一次(随机重抽),prompt 里不该多出"额外要求"。"""
    from services.geo_douyin.redraw import build_redraw_prompt
    post = {
        "id": 1, "keyword": "全屋定制", "city": "深圳", "style_key": "design_text",
        "cards": [{"idx": 1, "kind": "cover", "headline": "封面", "prompt": "原始 prompt"}],
        "generation_meta": {"content": {"cover": {"title": "深圳全屋定制哪家好",
                                                  "subtitle": "对比"}}},
    }
    p = build_redraw_prompt(post, 0, hint="")
    assert p, "空 hint 也必须能产出 prompt(否则'随机重抽'点了没反应)"
    assert "额外要求" not in p, "空 hint 不该追加额外要求段"


def test_hint_is_appended_not_replacing():
    """填了"想怎么改"是**追加**,不是把原稿换掉(用户说的是在这张基础上改)。"""
    from services.geo_douyin.redraw import build_redraw_prompt
    post = {
        "id": 1, "keyword": "全屋定制", "city": "深圳", "style_key": "design_text",
        "cards": [{"idx": 1, "kind": "cover", "headline": "封面", "prompt": "原始 prompt"}],
        "generation_meta": {"content": {"cover": {"title": "深圳全屋定制哪家好",
                                                  "subtitle": "对比"}}},
    }
    base = build_redraw_prompt(post, 0, hint="")
    withhint = build_redraw_prompt(post, 0, hint="去掉价格，换成工期说明")
    assert "额外要求" in withhint and "去掉价格" in withhint
    assert base.split("【额外要求】")[0] in withhint, "追加时把原 prompt 改掉了"


# ═════════════════════════════════════════════════════════════
# ② 10 次上限:后端强制 + 并发安全
# ═════════════════════════════════════════════════════════════
def test_limit_is_enforced_in_the_same_update():
    """🔴 判额度与加计数必须在**同一条 UPDATE 的 WHERE 里**。

    "先 SELECT 够不够、再 UPDATE 加一"在并发下会超发(两个请求都读到 9)。
    重抽真调生图 API 花钱,超发 = 白烧钱。
    """
    sql = _sql_of(DB, "bump_redraw_count")
    assert "UPDATE geo_douyin_posts" in sql
    assert "redraw_count < %s" in sql, "额度判断不在 UPDATE 的 WHERE 里"
    assert "RETURNING redraw_count" in sql, "没有把占用结果返回,调用方无从判断是否成功"


def test_limit_check_is_not_read_then_write():
    """必须不命中面:同一函数里不得出现独立的 SELECT 预查(那就是 check-then-act)。"""
    sql = _sql_of(DB, "bump_redraw_count")
    assert "SELECT redraw_count" not in sql, "出现了先查后写,并发下会超发"


def test_limit_returns_429_with_message():
    """② 上限触顶要**后端拒绝并给人话**,不是前端灰掉按钮了事。"""
    code = _code_of(API, "api_redraw_card")
    assert "429" in code, "触顶没有返回 429"
    assert "limit_reached" in code, "没有区分'触顶'与'生图失败'"


def test_limit_constant_is_ten():
    from services.geo_douyin.redraw import REDRAW_LIMIT_PER_POST
    assert REDRAW_LIMIT_PER_POST == 10, "工单写死每条累计 10 次"


def test_failed_redraw_refunds_the_quota():
    """额度是"成功才算用掉":生图失败必须退回,否则用户白丢一次机会。"""
    code = _code_of(REDRAW, "redraw_one_card")
    assert "refund_redraw_count" in code, "生图失败没退回额度"
    assert code.count("refund_redraw_count") >= 2, (
        "只在一个失败分支退了额度 —— 生图失败与落库失败都要退")


# ═════════════════════════════════════════════════════════════
# ③ 价格:读价目表实价,前端零硬编码
# ═════════════════════════════════════════════════════════════
def test_price_endpoint_reads_pricing_table():
    code = _code_of(API, "api_pricing")
    assert "get_feature_pricing" in code, "价目没走 feature_pricing(价目表 SSOT)"


def test_price_endpoint_returns_null_instead_of_guessing():
    """🔴 读不到价目要返回 null,**不能兜一个默认数**。

    显示一个猜的价比不显示更糟:用户按看到的价决策,扣的却是另一个数。
    """
    code = _code_of(API, "api_pricing")
    assert "return None" in code, "价目读不到时没有返回 None"
    for guess in ("390", "260", "130"):
        assert guess not in code, f"价目端点里出现了硬编码数字 {guess}"


def test_frontend_has_no_hardcoded_price():
    """③ 前端不许把价钱写死 —— Owner 调价不该要改前端代码。"""
    src = _tsx(DETAIL)
    hits = re.findall(r"\b(390|260)\s*算力", src)
    assert not hits, f"前端硬编码了价格: {hits}"
    assert "pricing?.regenerate?.cost_points" in src, "前端没读后端实价"


def test_frontend_price_lock_has_power():
    """必须不命中面:检测口径不能恒真。"""
    fake = "再次创作 · 390 算力"
    assert re.findall(r"\b(390|260)\s*算力", fake), "价格检测正则抓不到硬编码 = 恒真锁"


# ═════════════════════════════════════════════════════════════
# ④ 城市版本切换
# ═════════════════════════════════════════════════════════════
def test_city_siblings_scoped_by_keyword_brand_and_owner():
    """同一条内容的其他城市版本 = 同关键词 + 同品牌 + 同创建人。

    🔴 少任何一维都会串到别人的作品(尤其 created_by:少了就跨用户可见)。
    """
    sql = _sql_of(DB, "list_city_siblings")
    assert "s.keyword IS NOT DISTINCT FROM cur.keyword" in sql
    assert "s.brand_id IS NOT DISTINCT FROM cur.brand_id" in sql
    assert "s.created_by = cur.created_by" in sql, "没按创建人隔离 = 跨用户可见"
    assert "s.deleted_at IS NULL" in sql, "没过滤软删除"


def test_city_chips_navigate_to_that_post():
    """④ 点城市 chip 是**切到那条真作品**,不是在当前作品上改个城市名。"""
    src = _tsx(DETAIL)
    assert "onNavigate?.(s.id)" in src, "城市 chip 没有切到对应作品"
    assert "siblings" in src


def test_wording_no_brother_version():
    """措辞订正:「切换兄弟版本」→「城市版本」。"""
    src = _tsx(DETAIL)
    assert "兄弟版本" not in src, "还在用「兄弟版本」这个工程词"
    assert "城市版本" in src


def test_wording_contact_configured():
    """措辞订正:「联系方式已选」→「联系方式已配置」。"""
    src = _tsx(DETAIL)
    assert "联系方式已选" not in src, "还在用「联系方式已选」"
    assert "联系方式已配置" in src


# ═════════════════════════════════════════════════════════════
# ⑤ 联系方式开关与收尾卡真联动
# ═════════════════════════════════════════════════════════════
def test_contact_toggle_marks_closing_stale():
    """🔴 拨开关不能改变一张【已渲染的 PNG】—— 必须置"待重抽"位,并如实告诉用户。"""
    sql = _sql_of(DB, "set_contact_enabled")
    assert "closing_stale = TRUE" in sql, "开关没置'收尾卡待重抽'位 = 假联动"
    assert "contact_enabled IS DISTINCT FROM" in sql, (
        "没做幂等:重复拨同一个值会把已经重抽好的收尾卡又标成待重抽")


def test_closing_prompt_actually_carries_contact():
    """开关的**真落点**是收尾卡 prompt,不是数据库里一个 boolean。"""
    from services.geo_douyin.card_templates import StyleTokens, build_closing_prompt
    with_contact = build_closing_prompt("选购总结", "小结", StyleTokens(),
                                        contact_line="岱林生物 0755-1234")
    without = build_closing_prompt("选购总结", "小结", StyleTokens())
    assert "0755-1234" in with_contact, "联系方式没进收尾卡 prompt"
    assert "0755-1234" not in without, "没开开关也带上了联系方式"


def test_frontend_tells_user_it_needs_a_redraw():
    """⑤ 界面必须如实说明"要重抽一次才生效",不能显示"已插入"就完事。"""
    src = _tsx(DETAIL)
    assert "closing_stale" in src, "前端没读待重抽位"
    assert "重抽这张" in src


def test_confirm_card_shows_pending_state():
    """发布前确认卡不能在待重抽时显示"已插入"(那是骗人)。"""
    src = _tsx(DETAIL)
    assert "待重抽收尾卡" in src, "确认卡没有区分'已插入'与'待重抽'"


def test_redraw_closing_clears_stale():
    """重抽的正好是收尾卡时,要把待重抽位清掉。"""
    code = _code_of(REDRAW, "redraw_one_card")
    assert "clear_closing_stale" in code
    assert "closing" in code, "没有判断这张是不是收尾卡"


# ═════════════════════════════════════════════════════════════
# ⑥ 账号搜索 + 频控真值
# ═════════════════════════════════════════════════════════════
def test_account_search_filters_in_sql():
    """搜索必须在 SQL 里过滤 —— 前端 filter 只能搜到"这一页里的"。"""
    sql = _sql_of(PUB_ADAPTER, "list_douyin_image_accounts")
    assert "media_name LIKE %s" in sql, "账号搜索没落到 SQL"
    assert "ESCAPE" in sql, "LIKE 没写 ESCAPE 子句,搜 100% 会变成匹配任意"


def test_account_search_escapes_like_metachars():
    """行为面:% 和 _ 必须被转义,否则用户搜 '100%' 会命中一切。

    🔴 断言打在**行为**上而不是源码字符串上 —— 源码断言会被 ast.unparse 的
       引号归一化搞脆(实测:双引号写法在 unparse 后变单引号,锁直接假红)。
    """
    from services.geo_douyin.publish_adapter import escape_like
    assert escape_like("100%") == "100\\%", "% 没转义"
    assert escape_like("a_b") == "a\\_b", "_ 没转义"
    # 反斜杠必须先转:后转会把补进去的 \% 里那个反斜杠再转一次
    assert escape_like("a\\b") == "a\\\\b", "反斜杠没转义或转义顺序错了"


def test_account_search_escape_is_not_identity():
    """必须不命中面:转义函数不能是恒等的(那就等于没转)。"""
    from services.geo_douyin.publish_adapter import escape_like
    assert escape_like("100%") != "100%", "escape_like 是恒等函数 = 没转义"


def test_like_clause_declares_escape_char():
    """只转义不声明 ESCAPE 等于没转 —— PG 的 LIKE 默认没有转义符。"""
    sql = _sql_of(PUB_ADAPTER, "list_douyin_image_accounts")
    assert "ESCAPE" in sql, "LIKE 没声明 ESCAPE 子句"


def test_frequency_check_gets_real_per_account_counts():
    """🔴 不传 per_account_today 时,"单账号日发上限"那条规则**恒为不超限** ——
    校验在、判据不在,等于没校验。"""
    code = _code_of(API, "api_frequency_check")
    assert "load_today_counts" in code, "频控没取真值"
    assert "per_account_today=per_account" in code, "真值没传给 check_frequency"


def test_daily_limit_rule_is_not_vacuous():
    """行为面反向对照:喂真值时那条规则**真的会拦**;不喂时**真的不拦**。

    只断言"代码里传了参数"不够 —— 得证明这个参数确实改变结果。
    """
    from services.geo_douyin.publish_adapter import check_frequency
    hit = check_frequency(media_ids=[7], per_account_today={7: 99}, cities=["深圳"])
    assert not hit.ok and "account_daily_limit" in hit.violations, "喂了超限真值却没拦"
    miss = check_frequency(media_ids=[7], per_account_today={7: 0}, cities=["深圳"])
    assert miss.ok, "没超限却拦了"


def test_today_counts_excludes_failed_orders():
    """已取消/撤稿/失败/被拒的不算占用当日额度 —— 用一次失败惩罚用户一整天是错的。"""
    sql = _sql_of(PUB_ADAPTER, "load_today_counts")
    for st in ("cancelled", "withdrawn", "failed", "rejected"):
        assert st in sql, f"没排除 {st} 态"
    assert "created_at >= CURRENT_DATE" in sql, (
        "没按今天过滤,或用了 NOW()(created_at 是 timestamp without time zone)")


# ═════════════════════════════════════════════════════════════
# ⑦ 单卡重抽不覆盖他卡
# ═════════════════════════════════════════════════════════════
def test_replace_one_card_uses_pointwise_jsonb_set():
    """🔴 定点替换,不整份重写 —— 整份重写时任何读旧值的竞态都会覆盖别的卡。"""
    sql = _sql_of(DB, "replace_one_card")
    assert "jsonb_set(oss_keys" in sql, "没用 jsonb_set 定点替换"
    assert "jsonb_array_length(oss_keys) > %s" in sql, "没做下标越界防护"


def test_replace_one_card_does_not_rewrite_whole_array():
    """必须不命中面:不得出现整份赋值 oss_keys = %s::jsonb。"""
    sql = _sql_of(DB, "replace_one_card")
    assert "oss_keys = %s::jsonb" not in sql, "整份重写了 oss_keys,会覆盖其他卡"


def test_cover_key_follows_first_card_only():
    """重抽第 1 张要同步换封面 key,重抽其他张不能动封面。"""
    sql = _sql_of(DB, "replace_one_card")
    assert "CASE WHEN %s = 0 THEN %s ELSE cover_oss_key END" in sql


# ═════════════════════════════════════════════════════════════
# ⑧ 预览真实性(两条 P1)
# ═════════════════════════════════════════════════════════════
def test_preview_starts_at_cover():
    """P1:默认从封面(第 1 张)开始。"""
    src = _tsx(DETAIL)
    assert "if (!keepIdx) setActiveIdx(0)" in src, "打开详情没有回到封面"


def test_preview_big_image_and_phone_share_one_index():
    """P1:大图/缩略条/手机预览共用同一个下标 —— 联动是**结构上的**,不是同步逻辑。"""
    src = _tsx(DETAIL)
    assert src.count("previews[activeIdx]") >= 2, (
        "大图与手机预览没有共用 activeIdx(可能各存了一份)")
    assert "onClick={() => setActiveIdx(i)}" in src, "点缩略图没有跳张"


def test_preview_shows_card_counter():
    """P1:要显示张序 `当前/总数`,大图与手机各一处。"""
    src = _tsx(DETAIL)
    assert src.count("{activeIdx + 1}/{total}") >= 2, "张序角标缺失"


def test_preview_copy_is_single_source():
    """🔴 P1「禁两套文案」:手机预览直接渲染编辑区 state,不另拉一份。"""
    src = _tsx(DETAIL)
    assert 'data-testid="phone-title"' in src and 'data-testid="phone-body"' in src
    m = re.search(r'data-testid="phone-title">\s*\{([^}]+)\}', src)
    assert m, "找不到手机预览标题 —— 锚点失效"
    # 🔴 必须 startswith,不能只判"含 title" —— `detail?.post.title` 也含 title,
    #    那样一改成读服务端那份文案,锁照样绿(实测该变异会存活)。
    assert m.group(1).strip().startswith("title"), (
        f"手机预览标题读的不是编辑区 state,而是 {m.group(1).strip()[:40]}")
    m2 = re.search(r'data-testid="phone-body">\s*\{([^}]+)\}', src)
    assert m2, "找不到手机预览正文 —— 锚点失效"
    assert m2.group(1).strip().startswith("bodyText"), (
        f"手机预览正文读的不是编辑区 state,而是 {m2.group(1).strip()[:40]}")


def _second_copy_violations(flow: dict[str, str]) -> list[str]:
    out = [f"{name} 保留了一份预览副本(preview_urls)"
           for name, src in flow.items() if "preview_urls" in src]
    route = flow.get(IMAGE_NOTE_ROUTE.name, "")
    if "useParams<{ postId: string }>()" not in route:
        out.append("看的是哪条不再由路由参数 :postId 决定")
    if not re.search(r"<DouyinPostDetail\s+key=\{id\}\s+postId=\{id\}", route):
        out.append("详情不是按路由 id 挂载 —— 列表侧可能在往里塞整份数据")
    return out


def test_list_page_does_not_keep_a_second_copy():
    """必须不命中面:列表页不得再存一份 post/preview 副本(存了就会和详情页打架)。

    🔴 [WO_258] 列表侧现在是 `/writing/image-note` 的左栏;「只记看的是哪条」
       原来是 `activePostId`,现在由路由参数 `:postId` 承担,详情按 id 自己取数
       (`DouyinPostDetail` 的 Props 只收 id)。
    """
    assert not _second_copy_violations(_flow_sources())


def test_list_page_second_copy_lock_has_power():
    """反臂:列表侧塞回一份 preview_urls / 详情不按路由 id 挂,上面那条都要红。"""
    flow = _flow_sources()
    route = flow[IMAGE_NOTE_ROUTE.name]
    anchor = "interface ExistingPost { id: number;"
    assert route.count(anchor) == 1, "构造反例失败 —— ExistingPost 的写法变了,先更新这条对照"
    copied = {**flow, IMAGE_NOTE_ROUTE.name: route.replace(
        anchor, anchor + " preview_urls?: string[];")}
    assert any("preview_urls" in v for v in _second_copy_violations(copied))
    anchor = "postId={id}"
    assert route.count(anchor) == 1, "构造反例失败 —— 详情的挂载写法变了,先更新这条对照"
    unkeyed = {**flow, IMAGE_NOTE_ROUTE.name: route.replace(anchor, "postId={posts[0]?.id ?? 0}")}
    assert any("按路由 id 挂载" in v for v in _second_copy_violations(unkeyed))


def test_interaction_counts_are_dashes_not_fake_numbers():
    """互动位恒显示 — :我们拿不到真实点赞数,编一个是假数据。

    🔴 判据打在 **PhoneActionRail 的函数体**上,不打在某一串 className 上。
       原来锁的是 `<span className="text-[9px] leading-tight">—</span>` 一整串,
       2026-08-03 纯改样式(一行行为都没动)就把它打红了。
       打在源码串上的锁两头都脆:换皮能绕过、重构会误伤,而且误伤那次
       会诱导人去改**本来是对的代码**来迁就锁。
    """
    src = _tsx(DETAIL)
    assert "function PhoneActionRail" in src, "互动栏组件不见了"
    body = src.split("function PhoneActionRail", 1)[1].split("\nfunction ", 1)[0]
    assert "—" in body, "互动位没有恒显示 —"
    # 反向:互动位不许出现写死的计数(1.2万 / 3,456 这类)
    assert not re.search(r">\s*\d[\d,.]*\s*(万|k|K)?\s*<", body), \
        "互动位出现了写死的数字 = 假数据"


def test_prev_next_navigation_exists():
    """顶部逐条核发导航(N/M)。"""
    src = _tsx(DETAIL)
    assert 'data-testid="post-prev"' in src and 'data-testid="post-next"' in src
    assert "{orderPos + 1}/{orderIds.length}" in src, "没有显示第几条/共几条"


def test_download_all_moved_out_of_top_bar():
    """「下载全部」从顶部挪到发布区下方的次要位。"""
    src = _tsx(DETAIL)
    i_download = src.index('data-testid="download-all"')
    # 2026-08-03:发布搬到发布中心,本页的主行动按钮变成「去发布投放」。
    # 锚点跟着实现走 —— 指着已删的 douyin-publish-btn 会永远 red/假绿。
    i_publish = src.index('data-testid="douyin-goto-publish"')
    assert i_download > i_publish, "下载全部还在发布按钮上方(说明没挪下来)"
    i_regen = src.index('data-testid="regen-open"')
    assert i_download > i_regen, "下载全部还在顶栏"


# ═════════════════════════════════════════════════════════════
# ⑨ 黄点:有真数据源才显示,没有就整体隐藏
# ═════════════════════════════════════════════════════════════
def test_consistency_unchecked_without_materials():
    """⑨ 没有比对基准 → checked=False → 前端一个黄点都不给。"""
    from services.geo_douyin.kb_consistency import (card_flag_map,
                                                    check_cards_against_materials)
    rep = check_cards_against_materials(
        [{"entity": "A", "metric": "9999 元", "caveat": "x", "points": []}], "")
    assert rep.checked is False, "没有资料时不该声称核对过了"
    assert card_flag_map(rep) == {}, "没有比对基准却给了黄点 = 假数据"


def test_flag_map_respects_checked_flag():
    """🔴 直接锁 card_flag_map 的守卫本身。

    只用"空资料"那条路径测不出来 —— 那条返回的 cards 本来就是空列表,
    把守卫删掉照样得到 {}(实测该变异存活)。所以这里**手工构造**一份
    checked=False 但带 mismatch 的报告,守卫没了立刻现形。
    """
    from services.geo_douyin.kb_consistency import (CardCheck, ConsistencyReport,
                                                    card_flag_map)
    bad = [CardCheck(card_index=0, ok=False, mismatches=["8888 元"])]
    assert card_flag_map(ConsistencyReport(checked=False, cards=bad)) == {}, (
        "checked=False 时仍然给了黄点 = 假数据")
    assert card_flag_map(ConsistencyReport(checked=True, cards=bad)) == {0: ["8888 元"]}, (
        "checked=True 时反而不给黄点")


def test_refund_quota_actually_decrements():
    """额度退回必须真的减一 —— 空操作的"退回"和不退没区别。"""
    sql = _sql_of(DB, "refund_redraw_count")
    assert "redraw_count = GREATEST(redraw_count - 1, 0)" in sql, (
        "退回额度没有真的减一")


def test_consistency_flags_number_not_in_materials():
    """卡面出现资料里没有的事实型数字 → 标黄。"""
    from services.geo_douyin.kb_consistency import check_cards_against_materials
    rep = check_cards_against_materials(
        [{"entity": "A牌", "one_liner": "", "metric": "8888 元", "caveat": "注意",
          "points": ["工期 30 天"]}],
        "【公司简介】我们成立于 2010 年，工期通常 30 天。")
    assert rep.checked is True
    assert rep.flagged_count == 1, "资料里没有 8888 却没标黄"
    assert any("8888" in m for m in rep.cards[0].mismatches)


def test_consistency_does_not_flag_structural_numbers():
    """必须不命中面:「5 步」「3 家」这类版式数字**不该**标黄。

    把它们也标上,黄点会淹在噪声里,用户就不看了 —— 那才是真的失效。
    """
    from services.geo_douyin.kb_consistency import check_cards_against_materials
    rep = check_cards_against_materials(
        [{"entity": "A牌", "one_liner": "5 步决策法", "metric": "", "caveat": "注意",
          "points": ["3 家对比", "TOP10 榜单"]}],
        "【公司简介】随便一段没有数字的资料。")
    assert rep.checked is True
    assert rep.flagged_count == 0, f"结构性数字被误标: {rep.cards[0].mismatches}"


def test_consistency_matches_across_units():
    """资料写「45 天」、卡面写「45 个工作日」不该标黄(同一个数,单位写法不同)。"""
    from services.geo_douyin.kb_consistency import check_cards_against_materials
    rep = check_cards_against_materials(
        [{"entity": "A", "one_liner": "", "metric": "45 工作日", "caveat": "注意",
          "points": []}],
        "【核心卖点】标准工期 45 天。")
    assert rep.flagged_count == 0, "跨单位同数值被误标"


def test_frontend_hides_dots_when_unchecked():
    """前端侧:checked 为假时 flagMap 必须为空。"""
    src = _tsx(DETAIL)
    assert "if (!consistency?.checked) return m;" in src, (
        "前端没有在 checked=false 时清空黄点")


def test_yellow_dot_has_goto_copy_anchor():
    """黄点弹层要带「去修改正文」锚点,跳到文案区。"""
    src = _tsx(DETAIL)
    assert "去修改正文" in src
    assert 'data-testid="goto-copy-editor"' in src
    assert "douyin-body-input" in src, "锚点没有落到正文输入框"


def test_yellow_dot_wording_matches_spec():
    src = _tsx(DETAIL)
    assert "这条参数与资料库不一致，建议改文字" in src, "黄点文案与工单不一致"


# ═════════════════════════════════════════════════════════════
# 四款风格
# ═════════════════════════════════════════════════════════════
def test_four_styles_exist():
    from services.geo_douyin.card_templates import DEFAULT_STYLE_KEY, STYLE_PRESETS
    assert set(STYLE_PRESETS) == {"design_text", "table_review", "memo", "photo_overlay"}
    assert DEFAULT_STYLE_KEY == "design_text", "默认款应是文字卡(§6b 实测占 65%)"


def test_photo_style_needs_authorized_photo():
    """🔴 没有已授权实拍图时「实拍叠字」必须降级,不能照做 ——
    照做 = 模型编一张假实景图冒充客户现场。"""
    from services.geo_douyin.card_templates import resolve_style
    got = resolve_style("photo_overlay", has_authorized_photo=False)
    assert got.key == "design_text", "缺授权图时没有降级"
    ok = resolve_style("photo_overlay", has_authorized_photo=True)
    assert ok.key == "photo_overlay", "有授权图时反而不给用"


def test_style_choices_carry_disabled_reason():
    """置灰要带原因,不能干灰着让用户猜。"""
    from services.geo_douyin.card_templates import style_choices
    rows = {r["key"]: r for r in style_choices(has_authorized_photo=False)}
    assert rows["photo_overlay"]["disabled"] is True
    assert rows["photo_overlay"]["disabled_reason"], "置灰没给原因"
    assert rows["design_text"]["disabled"] is False


def test_five_element_framework_in_every_prompt():
    """五要素框架:任务 / 主体 / 风格 / 技术参数 / 输出规格,五段都要在。"""
    from services.geo_douyin.card_templates import (STYLE_PRESETS, StyleTokens,
                                                    build_closing_prompt,
                                                    build_content_prompt,
                                                    build_cover_prompt)
    style = StyleTokens(primary_color="宝蓝", header_bar="选购指南", footer_bar="来源")
    for preset in STYLE_PRESETS.values():
        for p in (build_cover_prompt("标题", "副标题", style, preset),
                  build_content_prompt(1, "A牌", ["要点"], style, preset=preset),
                  build_closing_prompt("总结", "小结", style, preset)):
            for section in ("【任务】", "【主体】", "【风格】", "【技术参数】", "【输出规格】"):
                assert section in p, f"{preset.key} 缺少 {section}"


def test_output_spec_states_ratio_and_resolution():
    """框架核心原则之一:必须标注比例与分辨率。"""
    from services.geo_douyin.card_templates import OUTPUT_SPEC
    assert "3:4" in OUTPUT_SPEC and "1080" in OUTPUT_SPEC


SAMPLES_DIR = "frontend/src/assets/style-samples"
EXPECTED_SAMPLES = 4


def _git_text(*args: str, env: dict | None = None) -> str:
    import subprocess
    return subprocess.run(["git", *args], cwd=str(ROOT), env=env,
                          capture_output=True, text=True, encoding="utf-8",
                          check=True).stdout


def _git_bytes(*args: str) -> bytes:
    import subprocess
    return subprocess.run(["git", *args], cwd=str(ROOT),
                          capture_output=True, check=True).stdout


def _tree_samples(rev: str = "HEAD") -> list:
    """**树里**的样板图清单(不是磁盘上的)。

    🔴 磁盘上在不算数,树里在才算 —— `.gitignore` 的全局 `*.jpg` 曾把这四张吞掉,
       工作树有图所以本机 `npm run build` 照常绿,但从干净检出构建会因四条硬 import 直接炸。
       复审抓到的就是这个:我的"build 绿"是假绿。
    """
    out = _git_text("ls-tree", "-r", rev, "--name-only", "--", SAMPLES_DIR)
    return sorted(p.strip() for p in out.splitlines()
                  if p.strip().endswith(".jpg"))


def test_style_samples_are_committed_to_the_tree():
    """工单:「用我们自己生成的家装样板套图放进风格选择器」。

    判据是 **git 树**,不是文件系统。四张、且文件名与四款风格一一对应。
    """
    from services.geo_douyin.card_templates import STYLE_PRESETS
    tracked = _tree_samples()
    assert len(tracked) == EXPECTED_SAMPLES, (
        f"树里只有 {len(tracked)} 张样板图(期望 {EXPECTED_SAMPLES}):{tracked}\n"
        f"—— 磁盘上有不算数,被 .gitignore 吞掉的话干净检出构建会炸")
    names = {p.rsplit("/", 1)[-1][:-4] for p in tracked}
    assert names == set(STYLE_PRESETS), f"样板图与四款风格对不上:{names}"


def test_style_sample_blobs_are_valid_jpeg_in_tree():
    """校**树里那份 blob** 的字节,不是磁盘那份。

    只判"文件存在"是恒真判据;这里读 blob 校 magic number + 体积量级。
    用 `git show HEAD:<path>` 取字节 —— 如果文件只在磁盘上,这一步直接失败。

    🔴 先断言数量再进循环:树里一张都没有时,`for` 体根本不执行,
       这条会**空循环假过**(实测:提交前跑它是绿的)。
    """
    paths = _tree_samples()
    assert len(paths) == EXPECTED_SAMPLES, f"树里样板图数量不对:{paths}"
    for path in paths:
        blob = _git_bytes("show", f"HEAD:{path}")
        assert blob[:2] == b"\xff\xd8" and blob[-2:] == b"\xff\xd9", f"{path} 不是完整 JPEG"
        assert 2_000 < len(blob) < 120_000, f"{path} 体积异常 {len(blob)}"


def test_style_samples_distinct_in_tree():
    """必须不命中面:四张不能是同一张复制四份(那就演示不出风格差异)。"""
    import hashlib
    digests = {hashlib.sha256(_git_bytes("show", f"HEAD:{p}")).hexdigest()
               for p in _tree_samples()}
    assert len(digests) == EXPECTED_SAMPLES, "样板图有重复 —— 并排看不出风格差异"


def test_working_tree_matches_committed_samples():
    """磁盘与树必须一致 —— 防"改了图但没提交",那会让本机看到的和线上不是一份。

    🔴 同样先断言数量:否则树里空的时候这条也空循环假过。
    """
    import hashlib
    paths = _tree_samples()
    assert len(paths) == EXPECTED_SAMPLES, f"树里样板图数量不对:{paths}"
    for path in paths:
        on_disk = (ROOT / path).read_bytes()
        in_tree = _git_bytes("show", f"HEAD:{path}")
        assert hashlib.sha256(on_disk).digest() == hashlib.sha256(in_tree).digest(), (
            f"{path} 磁盘与树不一致(改了没提交?)")


def test_every_tsx_import_points_at_a_tracked_file():
    """🔴 TSX 里每一条 style-samples 的 import 都必须对应树里真有的文件。

    这条直接锁住复审抓到的失败形态:import 是硬的,文件不在树里 = 干净检出构建炸。

    🔴 2026-08-04 改成**全仓 tsx 扫**,不再只看 DouyinPostDetail:
       今天把选择器抽成了共用组件 `components/writing/CardStylePicker.tsx`,
       原来那条只盯一个文件的判据当场转红 —— 它锁的其实是"这个文件里有 import",
       而要锁的是"**任何**地方的 import 都指向树里真有的文件"。
       只钉一个路径的锁,一搬家就退化成"改锁让它变绿"。
    """
    imported: list = []
    for tsx in (ROOT / "frontend" / "src").rglob("*.tsx"):
        imported += re.findall(r"from '@/assets/style-samples/([^']+)'",
                               tsx.read_text(encoding="utf-8"))
    assert imported, "全仓 tsx 里没有样板图 import —— 锚点失效"
    tracked = {p.rsplit("/", 1)[-1] for p in _tree_samples()}
    missing = [f for f in imported if f not in tracked]
    assert not missing, f"这些 import 在树里没有对应文件,干净检出会构建失败:{missing}"


def test_tree_level_check_has_discriminating_power():
    """🔴 反向对照:**真的从索引里去掉一张**,同一套判据必须转红。

    做法是造一个【临时索引】(GIT_INDEX_FILE)再 write-tree,
    对真实索引与工作树零影响 —— 不能为了验一条锁去动仓库状态。
    """
    import os
    import tempfile

    victim = _tree_samples()[0]
    with tempfile.TemporaryDirectory() as td:
        env = {**os.environ, "GIT_INDEX_FILE": os.path.join(td, "idx")}
        _git_text("read-tree", "HEAD", env=env)
        _git_text("update-index", "--force-remove", "--", victim, env=env)
        broken_tree = _git_text("write-tree", env=env).strip()

    out = _git_text("ls-tree", "-r", broken_tree, "--name-only", "--", SAMPLES_DIR)
    remaining = sorted(p.strip() for p in out.splitlines()
                       if p.strip().endswith(".jpg"))
    assert len(remaining) == EXPECTED_SAMPLES - 1, (
        "从索引拿掉一张后数量没变 —— 这套判据读的不是树,是恒真锁")
    assert victim not in remaining
    # 真实 HEAD 必须不受影响(证明上面那段没有污染仓库状态)
    assert len(_tree_samples()) == EXPECTED_SAMPLES, "反向对照污染了真实索引/HEAD"


def test_gitignore_whitelists_the_samples():
    """样板图必须进仓:开源仓的 .gitignore 不许忽略它们。

    主仓那份 .gitignore 有全局 `*.jpg`,靠一条白名单放行;开源仓的 .gitignore 只忽略运行时生成物,
    没有全局 `*.jpg`,所以这里直接问 git:这些样板图一张都不许被忽略。
    """
    import subprocess

    samples = sorted(p.relative_to(ROOT).as_posix() for p in (ROOT / SAMPLES_DIR).glob("*.jpg"))
    assert samples, "样板图目录是空的"
    r = subprocess.run(["git", "check-ignore", "--no-index", "--", *samples], cwd=ROOT,
                       capture_output=True, text=True)
    assert r.stdout.strip() == "", f"这些样板图会被 .gitignore 忽略:{r.stdout}"


PICKER = ROOT / "frontend" / "src" / "components" / "writing" / "CardStylePicker.tsx"


def test_style_picker_renders_samples():
    """选择器渲染的是**样板图**,不是四个纯文字按钮。

    🔴 判据跟着代码搬到共用组件 —— 2026-08-04 抽组件后,详情页与创作页
       共用这一份。锁留在详情页只会锁到一个 import 语句。

    🔴 判据分两半,缺一不可(第一版只写了前半,变异 V10h 直接存活):
       ① `<img src={STYLE_SAMPLES[s.key]}` —— 图真的被渲染;
       ② `{STYLE_SAMPLES[s.key] && (` —— 渲染受**这一款有没有样板图**的守卫。
       只断言 ① 的话,把守卫改成 `{false && (` 仍然绿:
       `STYLE_SAMPLES[s.key]` 这个串在 src= 那一行照样在,四个按钮却全成了纯文字。
    """
    src = _tsx(PICKER)
    assert "src={STYLE_SAMPLES[s.key]}" in src, "风格选择器没渲染样板图"
    assert "{STYLE_SAMPLES[s.key] && (" in src, (
        "样板图的渲染守卫没了 —— 要么整块渲染不出来,要么缺图时会裂图")
    assert "@/assets/style-samples/design_text.jpg" in src, "样板图没被 import"


def test_style_picker_is_shared_by_both_pages():
    """🔴 两页必须用**同一个**组件,不是各自复制一份。

    复制出去的那份在加第五款风格时必然漏改一处 —— 本仓反复栽过的
    "两份各自演化"。反向面:任一页没引用 = 红。

    🔴 [WO_258] 创作侧现在是第 1 步的 `ImageNoteTopicPanel`。判「真渲染」
       (`<CardStylePicker`)而不是「提到这个名字」—— 只剩一句 import 也会命中名字。
    """
    assert not _picker_violations({p.name: _tsx(p) for p in (DETAIL, TOPIC_PANEL)})


def _picker_violations(pages: dict[str, str]) -> list[str]:
    out = []
    for name, src in pages.items():
        if "<CardStylePicker" not in src:
            out.append(f"{name} 没渲染共用风格选择器")
        # 必须不命中面:两个页面文件里都不该再有自己的一份样板图表
        if "const STYLE_SAMPLES" in src:
            out.append(f"{name} 里又出现了一份本地样板图表 —— 组件白抽了")
    return out


def test_style_picker_lock_has_power():
    """反臂:创作侧换成自己的选择器 / 抄回一份样板图表,上面那条都要红。"""
    pages = {p.name: _tsx(p) for p in (DETAIL, TOPIC_PANEL)}
    panel = pages[TOPIC_PANEL.name]
    assert panel.count("<CardStylePicker") == 1, "构造反例失败 —— 创作侧选择器的写法变了"
    forked = {**pages, TOPIC_PANEL.name: panel.replace("<CardStylePicker", "<LocalStylePicker")}
    assert any("没渲染共用" in v for v in _picker_violations(forked))
    copied = {**pages, TOPIC_PANEL.name: panel + "\nconst STYLE_SAMPLES = {};\n"}
    assert any("本地样板图表" in v for v in _picker_violations(copied))


def test_style_is_choosable_before_the_first_generation():
    """🔴 第一次做就能选风格,不是做完花钱重做才能换。

    Owner 2026-08-04:「风格选择页我们之前不是预设了一些模板吗？现在没有这个部分」。
    根因:四款预设/端点/样例图 08-02 全就绪,但 `CreatePostRequest` 不收 style_key,
    唯一入口是详情页那个**收费**的「再次创作」弹窗。
    这条锁的是**入口**:建帖请求收 style_key,且真的透传给了生产。

    🔴 [WO_258] ② 原来只在 `api_create_and_produce` 的函数体里找调用 ——
       baf70539c(#150 §3.3 · 09-08)把逻辑挪进 `_create_and_dispatch_one` 后
       它读到空集,而产品其实照常透传。改成**按行为认**:从路由出发、沿本模块
       调用链走到的那处 `dispatch_production(...)` 带不带 style_key;两条建帖路由
       (单条 `/posts`、批量 `/posts/batch` —— 创作页现在打的是后者)都要带。
       ③ 创作页的请求体现在由 `productionBatch`(imageNoteProduction.ts)生成。
    """
    tree = ast.parse(API.read_text(encoding="utf-8"))

    # ① 请求模型收 style_key
    fields = {
        t.target.id
        for n in tree.body
        if isinstance(n, ast.ClassDef) and n.name == "CreatePostRequest"
        for t in n.body
        if isinstance(t, ast.AnnAssign) and isinstance(t.target, ast.Name)
    }
    assert "style_key" in fields, (
        "CreatePostRequest 不收 style_key —— 创作页选了风格也传不进去")
    # ①b 批量请求的每一项就是建帖请求 —— 否则 ① 管不到批量那条路
    items = [ast.unparse(t.annotation)
             for n in tree.body
             if isinstance(n, ast.ClassDef) and n.name == "BatchCreateRequest"
             for t in n.body
             if isinstance(t, ast.AnnAssign) and getattr(t.target, "id", "") == "items"]
    assert items == ["List[CreatePostRequest]"], f"批量建帖的每一项不再是 CreatePostRequest:{items}"

    # ② 建帖路由沿调用链真的把它透传给 dispatch_production(不是收了不用)
    for path in ("/posts", "/posts/batch"):
        handler = _route_handler(tree, "post", path)
        assert "style_key" in _dispatch_kwargs_reached_from(tree, handler), (
            f"POST {path}({handler})收了 style_key 却没传给 dispatch_production"
            " —— 收了不用等于没收")

    # ③ 创作页把选中的风格塞进了它真正提交的批量请求体
    assert not _style_chain_violations(_tsx(TOPIC_PANEL), _tsx(PRODUCTION))


def _route_handler(tree: ast.Module, method: str, path: str) -> str:
    for n in tree.body:
        if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for d in n.decorator_list:
            if (isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
                    and d.func.attr == method and d.args
                    and isinstance(d.args[0], ast.Constant) and d.args[0].value == path):
                return n.name
    raise AssertionError(f"找不到 {method.upper()} {path} 的处理函数 —— 锚点失效")


def _dispatch_kwargs_reached_from(tree: ast.Module, root: str) -> set[str]:
    """从 `root` 出发,沿本模块顶层函数的直接调用走到的每处 `dispatch_production(...)`
    的关键字参数并集。

    🔴 只沿调用链走,**不扫全模块**:`api_regenerate_post` 里那处调用
       不许替建帖路由作证(反臂专门钉这一点)。
    """
    funcs = {n.name: n for n in tree.body
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    seen: set[str] = set()
    todo, kws = [root], set()
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        seen.add(name)
        for c in ast.walk(funcs[name]):
            if not isinstance(c, ast.Call):
                continue
            callee = (c.func.id if isinstance(c.func, ast.Name)
                      else c.func.attr if isinstance(c.func, ast.Attribute) else "")
            if callee == "dispatch_production":
                kws |= {k.arg for k in c.keywords if k.arg}
            elif isinstance(c.func, ast.Name) and callee in funcs:
                todo.append(callee)
    return kws


def _style_chain_violations(panel: str, production: str) -> list[str]:
    """用户选的风格 → settings → 批量请求体 → 批量建帖端点,四环缺一环就是摆设。"""
    out = []
    if "onChange={setStyleKey}" not in panel:
        out.append("风格选择器选了不落到 styleKey")
    memo = re.search(r"const settings = useMemo\(\s*\(\)\s*=>\s*(.*?),\s*\[([^\]]*)\]\s*\)",
                     panel, flags=re.S)
    if not memo or "styleKey" not in _free_identifiers(memo.group(1)):
        out.append("提交用的 settings 里没有 styleKey")
    if not re.search(r"'/api/geo-douyin/posts/batch'.{0,200}?JSON\.stringify\(productionBatch\(",
                     panel, flags=re.S):
        out.append("创作页提交批量建帖的请求体不是 productionBatch 生成的")
    if "style_key: settings.styleKey" not in production:
        out.append("批量请求体里没有用户选的风格")
    return out


def test_style_passthrough_lock_has_power():
    """反臂 ①:只拿掉建帖那处生产调用的 style_key(重做那处**不动**)⇒ 两条路由都要红
    —— 证明 ② 只沿调用链走,`api_regenerate_post` 替不了建帖作证;
    对照:重做那处自己仍带 style_key(否则"红"可能只是全模块都读不到)。
    反臂 ②:请求体改成只用选题自带的风格 ⇒ ③ 要红。
    """
    tree = ast.parse(API.read_text(encoding="utf-8"))
    one = next(n for n in tree.body
               if isinstance(n, ast.AsyncFunctionDef) and n.name == "_create_and_dispatch_one")
    calls = [c for c in ast.walk(one)
             if isinstance(c, ast.Call) and getattr(c.func, "id", "") == "dispatch_production"]
    assert len(calls) == 1, "构造反例失败 —— 建帖那处生产调用的形状变了,先更新这条对照"
    kept = [k for k in calls[0].keywords if k.arg != "style_key"]
    assert len(kept) == len(calls[0].keywords) - 1, "构造反例失败 —— 那处调用没带 style_key 可拿"
    calls[0].keywords = kept
    for path in ("/posts", "/posts/batch"):
        assert "style_key" not in _dispatch_kwargs_reached_from(
            tree, _route_handler(tree, "post", path)), (
            f"拿掉建帖那处的 style_key 后 POST {path} 仍然绿 —— 判据被别处的调用作了证")
    assert "style_key" in _dispatch_kwargs_reached_from(tree, "api_regenerate_post"), (
        "对照失效:重做那处也读不到 style_key —— 上面的红可能只是尺子全瞎")

    production = _tsx(PRODUCTION)
    anchor = "style_key: settings.styleKey || row.styleKey"
    assert production.count(anchor) == 1, "构造反例失败 —— 请求体里风格那一项的写法变了"
    assert any("用户选的风格" in v for v in _style_chain_violations(
        _tsx(TOPIC_PANEL), production.replace(anchor, "style_key: row.styleKey")))


def test_image_client_error_carries_exception_type():
    """🔴 fail-soft 可以,失声不行。

    不少异常的 str() 是空串(asyncio.TimeoutError 就是),只记 str(e) 会让
    日志和返回的 error 双双为空 —— 出了事零线索。必须带异常类型。
    (2026-08-02 实测踩过:连挂两次、日志全空、白花两次生图钱。)
    """
    client = ROOT / "services" / "marketing" / "image_client.py"
    code = _code_of(client, "generate_image")
    assert "type(e).__name__" in code, "异常类型没进诊断信息"
    assert 'return {"ok": False, "error": str(e)[:120]' not in code, (
        "还在只回 str(e) —— 空消息异常会变成空 error")


def test_error_detail_is_nonempty_for_empty_message_exception():
    """行为面:构造一个 str() 为空的异常,诊断串必须仍然非空。"""
    e = TimeoutError()
    assert str(e) == "", "前提变了:TimeoutError 的 str() 不再是空串"
    detail = f"{type(e).__name__}: {e}".rstrip(": ").strip()
    assert detail == "TimeoutError", f"空消息异常的诊断串是 {detail!r}"


def test_style_presets_do_not_reference_external_repo():
    """必须不命中面:只借框架写法,**不得**引用参考仓库的图片/链接。"""
    src = CARD_TPL.read_text(encoding="utf-8")
    for banned in ("github.com/xianyu110", "awesome-gptimage2/blob", "raw.githubusercontent"):
        assert banned not in src, f"引用了外部仓库资源 {banned}"


# ═════════════════════════════════════════════════════════════
# 右栏「知识库 / 写作资料」卡
# ═════════════════════════════════════════════════════════════
def test_material_fields_are_the_real_ten():
    """资料填充度分母 = client_materials 里真实的 10 个内容字段。

    生产实测这 10 列都在,单个品牌通常填到 8-10 项 —— 所以 "N/10" 是有意义的进度。
    随手写个 8 当分母就是编数字。
    """
    from services.geo_douyin.knowledge_context import MATERIAL_FIELDS
    keys = {k for k, _ in MATERIAL_FIELDS}
    assert len(MATERIAL_FIELDS) == 10
    # 这些列名已对生产 information_schema 核过,不是猜的
    assert keys == {
        "company_intro", "core_selling_points", "unique_value", "case_studies",
        "credentials", "testimonials", "methodology", "pricing_tiers",
        "service_area", "team_size"}
    assert all(label for _, label in MATERIAL_FIELDS), "每项都要有用户可见名"


def test_knowledge_card_uses_same_authorization_bits():
    """🔴 右栏计数的授权判据必须与**生成时**完全一致。

    走岔的后果很具体:右栏显示"图片 13 张",生成时一张都用不上,
    用户会觉得功能坏了 —— 而两边各自都"没错"。
    """
    thumbs_sql = _sql_of(KB_CTX, "load_authorized_image_thumbs")
    count_sql = _sql_of(KB_CTX, "count_authorized_images")
    gen_sql = _sql_of(KB_CTX, "load_authorized_images")
    for sql in (thumbs_sql, count_sql):
        assert "COALESCE(publish_allowed, 0) = 1" in sql
        assert "COALESCE(rights_confirmed, 0) = 1" in sql
    # 三处判据逐字一致(不是"都含有这两个词"那种弱判据)
    for bit in ("COALESCE(publish_allowed, 0) = 1",
                "COALESCE(rights_confirmed, 0) = 1"):
        assert bit in gen_sql, "生成侧判据变了,右栏会跟它对不上"


def test_thumb_url_follows_existing_convention():
    """🔴 行为锁:缩略图取法照抄既有约定(thumbnail_key → safe_size_key → public_url)。

    之前这条只断言"源码里有 thumbnail_key 这个词" —— 把整行换成 `tk = None`,
    那个词还在 SQL 的 SELECT 里,锁照样绿(实测该变异存活)。所以改成直接测行为。
    """
    from services.geo_douyin.knowledge_context import resolve_thumb
    assert resolve_thumb({"thumbnail_key": "t/a.jpg", "safe_size_key": "s/a.jpg",
                          "public_url": "/p/a.jpg"}) == "/t/a.jpg", "没优先 thumbnail_key"
    assert resolve_thumb({"safe_size_key": "s/a.jpg",
                          "public_url": "/p/a.jpg"}) == "/s/a.jpg", "没回落 safe_size_key"
    assert resolve_thumb({"public_url": "/p/a.jpg"}) == "/p/a.jpg", "没回落 public_url"
    assert resolve_thumb({}) == "", "无地址时该返回空串(调用方据此跳过)"
    # 必须不命中面:key 要补前导 /,补重了也不行
    assert resolve_thumb({"thumbnail_key": "/t/a.jpg"}) == "/t/a.jpg", "前导 / 补重了"


def test_sources_used_is_real_provenance_not_inventory():
    """🔴 行为锁:「这条内容用到了」取生产时的**真实留痕**,不是客户资料库存。

    同样的教训:只断言源码含 "kb_sources" 时,把 used 改成空列表那个词还在,锁恒绿。
    """
    from services.geo_douyin.knowledge_context import describe_sources
    got = describe_sources({"kb_sources": ["client_materials", "brand_image_assets"]})
    assert got == ["客户资料", "已授权图片"], f"留痕没翻成人话: {got}"
    assert describe_sources({}) == [], "没有留痕时不该编出来源"
    assert describe_sources(None) == []
    # 未知来源原样透出,不吞掉(吞掉会让新来源静默消失)
    assert describe_sources({"kb_sources": ["brand_new_source"]}) == ["brand_new_source"]


def test_knowledge_endpoint_reports_load_failure_separately():
    """取失败要单独回,不能混进"没有数据"。

    🔴 断言打在**赋值整句**上而不是裸标识符 `load_failed` ——
    后者在初始化和返回字典里都出现,把 except 分支里那句删掉照样绿(实测存活)。

    🔴 2026-08-03 锚点第二次跟随实现走:这段逻辑先从端点搬进
       `geo_douyin/knowledge_context.build_client_knowledge`,本次又搬进
       **跨板块共享**的 `services/client_knowledge.py`(写文章与做图文共用一份,
       原因是同一个客户在两个页面显示的分数不一样:8 分母 vs 10 分母)。
       锁跟着搬 —— 留在旧位置就是锁在一个已经不存在的形状上。

    🔴 判据要**与引号形态无关**:`ast.unparse` 会把双引号规范成单引号,
       照原样写 `"load_failed": True` 必然对不上(实测踩到)。用规范化后的形态。
    """
    kb = ROOT / "services" / "client_knowledge.py"
    code = _code_of(kb, "build_client_knowledge").replace('"', "'")
    assert "'load_failed': True" in code, "except 分支没有把取失败标出来"
    assert "logger.error" in code, "取失败只记了 warning"
    # 反向面:成功路径必须明确回 False,不能靠"没这个键"让前端自己猜
    assert "'load_failed': False" in code, "成功路径没有显式标 load_failed=False"


def test_frontend_shows_dash_not_zero_when_unknown():
    """必须不命中面:数据没拉到时显示「—」,不是「资料 0/0」「图片 0 张」。

    显示 0 会被读成"客户什么都没填",那是假信息。
    """
    src = _tsx(DETAIL)
    assert "'资料 —'" in src, "资料未知时没显示 —"
    assert "'图片 —'" in src, "图片未知时没显示 —"


def test_frontend_distinguishes_load_failure():
    src = _tsx(DETAIL)
    assert "knowledge?.load_failed" in src, "前端没区分取失败"
    assert "不是没填" in src, "取失败的文案没跟'没填'区分开"


def test_frontend_renders_material_checklist_and_thumbs():
    src = _tsx(DETAIL)
    assert 'data-testid="kb-materials"' in src and 'data-testid="kb-images"' in src
    assert "materials.items.map" in src, "资料清单没渲染(缺哪项看不到)"
    assert "images.thumbs.map" in src, "参考图片缩略图没渲染"


# ═════════════════════════════════════════════════════════════
# 🔴 回归锁:brand_image_assets 两列是 smallint,不是 boolean
# ═════════════════════════════════════════════════════════════
def test_authorized_image_sql_compares_smallint():
    """生产实测这两列是 smallint(0/1)。

    写成 `COALESCE(col, FALSE) = TRUE` 会让 PG 直接报
    `COALESCE types smallint and boolean cannot be matched` ——
    整条查询抛异常、被上层 except 吞掉、降级成"这个客户没有图片素材"。
    表现是**功能安静地不存在**,所以能一直没人发现。
    """
    sql = _sql_of(KB_CTX, "load_authorized_images")
    assert "COALESCE(publish_allowed, 0) = 1" in sql
    assert "COALESCE(rights_confirmed, 0) = 1" in sql


def test_authorized_image_sql_has_no_boolean_coalesce():
    """必须不命中面:不得再出现 boolean 写法。"""
    sql = _sql_of(KB_CTX, "load_authorized_images")
    assert "COALESCE(publish_allowed, FALSE)" not in sql
    assert "COALESCE(rights_confirmed, FALSE)" not in sql


def test_both_authorization_bits_are_required():
    """两个位的与不是冗余:生产 232 行里 publish_allowed=1 有 129 行,
    两位都为 1 只有 33 行 —— 少判一位多放行 96 行未确权的图。"""
    sql = _sql_of(KB_CTX, "load_authorized_images")
    assert "publish_allowed" in sql and "rights_confirmed" in sql


def test_material_load_failure_is_visible():
    """🔴 取素材失败必须**看得见**,不能表现成"客户没资料"。

    这正是 smallint 事故能潜伏一整个包的根因:异常被吞 → 降级 → 看起来一切正常。
    """
    from services.geo_douyin.knowledge_context import BrandContext
    ctx = BrandContext()
    assert hasattr(ctx, "load_errors"), "没有区分'取失败'与'没有数据'"
    code = _code_of(KB_CTX, "build_brand_context")
    assert code.count("load_errors.append") >= 2, "失败路径没记录到 load_errors"
    assert "logger.error" in code, "取素材失败只记了 warning,日志里挑不出来"


# ═════════════════════════════════════════════════════════════
# 迁移登记(prestart 只跑清单,不 glob 目录)
# ═════════════════════════════════════════════════════════════
def test_migration_020_registered():
    """🔴 迁移建了 ≠ 会跑。017/018 就是漏登记导致生产表 0 张。"""
    src = MANIFEST.read_text(encoding="utf-8")
    src_code = "\n".join(
        (l if l.find("#") < 0 else l[:l.find("#")]) for l in src.splitlines())
    assert "db/migration_020_geo_douyin_detail_ui_2026_08_02.sql" in src_code, (
        "020 没登记进 manifest = 上线后字段不存在")


def test_rollback_020_not_registered():
    """必须不命中面:回滚脚本绝不能登记(登记 = 上线即把字段删了)。"""
    src = MANIFEST.read_text(encoding="utf-8")
    src_code = "\n".join(
        (l if l.find("#") < 0 else l[:l.find("#")]) for l in src.splitlines())
    assert "rollback_020" not in src_code, "回滚脚本被登记了"


def test_migration_020_is_additive_and_idempotent():
    sql = MIG_020.read_text(encoding="utf-8")
    sql_code = "\n".join(
        (l if l.find("--") < 0 else l[:l.find("--")]) for l in sql.splitlines())
    assert sql_code.count("ADD COLUMN IF NOT EXISTS") == 4, "四个字段不是全部 IF NOT EXISTS"
    for destructive in ("DROP COLUMN", "DROP TABLE", "TRUNCATE", "RENAME"):
        assert destructive not in sql_code.upper(), f"迁移里有破坏性 DDL: {destructive}"


def test_migration_020_order_after_017():
    """020 ALTER 的正是 017 建的表,必须排在它后面。"""
    src = MANIFEST.read_text(encoding="utf-8")
    i_017 = src.index("migration_017_geo_douyin_posts")
    i_020 = src.index("migration_020_geo_douyin_detail_ui")
    assert i_017 < i_020, "020 排在了 017 前面 —— ALTER 会打到不存在的表"


# ═════════════════════════════════════════════════════════════
# 端点存在性(防前端打 404)
# ═════════════════════════════════════════════════════════════
@pytest.mark.parametrize("path", [
    "/pricing", "/styles", "/posts/{post_id}/consistency",
    "/posts/{post_id}", "/posts/{post_id}/contact",
    "/posts/{post_id}/cards/{card_index}/redraw",
    "/posts/{post_id}/regenerate", "/posts/{post_id}/export",
])
def test_route_exists(path):
    src = API.read_text(encoding="utf-8")
    assert f'"{path}"' in src, f"路由 {path} 不存在"


def test_frontend_only_calls_existing_routes():
    """前端打的每个本包端点都要真存在。"""
    src = _tsx(DETAIL)
    api_src = API.read_text(encoding="utf-8")
    used = set(re.findall(r"/api/geo-douyin/([a-z\-]+)", src))
    for seg in used:
        assert f'"/{seg}' in api_src or f'"/{seg}"' in api_src, f"/{seg} 在后端不存在"


# ═══════════════════════════════════════════════════════════════
# 2026-08-04 · Owner 看图提的四件事
# ═══════════════════════════════════════════════════════════════

def test_produce_deps_include_the_fields_it_sends():
    """🔴 建帖请求里带的每个 state,都必须在 `produce` 的依赖数组里。

    抓的是一个**真实存在过的哑巴 bug**:`produce` 原来是
    `useCallback(..., [loadPosts])`,而 `loadPosts` 是 `useCallback(..., [])`
    —— 永远稳定。于是 produce 只在首渲染建一次,闭包锁死了首渲染那一刻的
    `aspectRatio`(空串)和 `brandId`。
    后果:**画幅选择器是个摆设** —— 点「全屏 9:16」照样出 3:4,
    后端 normalize 把空值落回默认,全程不报错、无日志、无人发现。

    判据不是"字面写了 aspectRatio",是:请求体里出现的每个标识符
    都要在依赖数组里 —— 以后再往请求体加字段,忘了加依赖同样会红。

    🔴 [WO_258] `produce` 随旧创作页删了。现在批量建帖的请求体由
       `productionBatch(brandId, selectedPending, settings, quote, …)` 生成;
       提交函数 `doStart` 不做 memo(每次渲染都是新闭包),会锁死旧值的只剩
       喂给请求体的那几个 `useMemo` —— 画幅 / 风格 / 张数就住在 `settings` 里。
       旧判据只取冒号后的标识符,认不出 `{ styleKey, aspectRatio }` 这种简写,
       所以改成 `_free_identifiers`(剥属性访问、对象键、箭头参数)。
    """
    gaps = _body_closure_gaps(_tsx(TOPIC_PANEL))
    assert "settings" in gaps, "请求体没从 settings 取值 —— 判据锚点失效,先修锚点"
    missing = {k: v for k, v in gaps.items() if v}
    assert not missing, (
        f"这些值进了请求体却不在依赖数组里,闭包会锁死首渲染的旧值:{missing}\n"
        f"—— 用户改了也发不出去,而且不报错")


_JS_NOT_DEPS = frozenset({
    "true", "false", "null", "undefined", "typeof", "instanceof", "new", "void", "in", "of",
    "Math", "Number", "String", "Array", "Object", "JSON", "Boolean", "Set", "Map",
})


def _free_identifiers(expr: str) -> set[str]:
    """一段 JS 表达式里**从外层闭包取值**的标识符。

    剥掉:字符串字面量 · 属性访问(`.x` / `?.x`)· 对象键(`{ k: …` / `, k: …`)·
    箭头函数自己的参数 · 全局对象。对象简写 `{ styleKey }` 里的名字**算**取值。
    """
    s = re.sub(r"'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"|`(?:\\.|[^`\\])*`", "''", expr)
    params: set[str] = set()
    for pm in re.finditer(r"\(([^()]*)\)\s*=>|\b([A-Za-z_$][\w$]*)\s*=>", s):
        params |= set(re.findall(r"[A-Za-z_$][\w$]*", pm.group(1) or pm.group(2)))
    out: set[str] = set()
    for m in re.finditer(r"[A-Za-z_$][\w$]*", s):
        before, after = s[:m.start()].rstrip(), s[m.end():].lstrip()
        if before.endswith("."):
            continue
        if after.startswith(":") and before.endswith(("{", ",")):
            continue
        out.add(m.group(0))
    return out - params - _JS_NOT_DEPS


def _body_closure_gaps(panel: str) -> dict[str, set[str]]:
    """批量建帖请求体取值链上的每个闭包 →「体内用到却不在依赖里」的标识符。"""
    call = re.search(r"JSON\.stringify\(productionBatch\(([^()]*)\)\)", panel)
    assert call, "找不到批量建帖的请求体 productionBatch(…) —— 判据锚点失效,先修锚点"
    roots = [re.match(r"[A-Za-z_$][\w$]*", a.strip()).group(0)
             for a in call.group(1).split(",") if a.strip()]
    gaps: dict[str, set[str]] = {}
    # ① 提交函数本身不做 memo ⇒ 每次渲染都是新闭包。谁把它包进 useCallback,
    #    这里先红 —— 逼他把请求体取值全列进依赖,再回来改本判据。
    decl = list(re.finditer(r"\n    const (\w+) = ", panel[:call.start()]))
    assert decl, "找不到提交函数的声明 —— 判据锚点失效"
    if panel[decl[-1].end():].startswith(("useCallback(", "useMemo(")):
        gaps[decl[-1].group(1)] = {"<提交函数被 memo 了:请求体取值全要进依赖>"}
    # ② 请求体取值里凡是 useMemo 出来的,体内用到的都要在依赖里
    for r in dict.fromkeys(roots):
        m = re.search(rf"const {r} = useMemo\(\s*\(\)\s*=>\s*(.*?),\s*\[([^\]]*)\]\s*\)",
                      panel, flags=re.S)
        if m:
            deps = {d.strip().split(".")[0].rstrip("?") for d in m.group(2).split(",") if d.strip()}
            gaps[r] = _free_identifiers(m.group(1)) - deps
    return gaps


def test_produce_deps_lock_has_power():
    """必须不命中面:把 aspectRatio 从依赖里去掉,上面那条要转红。

    不做这一步的话,"依赖数组恰好写全了"和"判据恒真"长得一模一样。
    """
    src = _tsx(TOPIC_PANEL)
    # 🔴 这里的对照串**故意写死** —— 依赖数组一变就转红,逼人来看一眼是不是又漏了一个。
    #    2026-08-06 补 brandIndustry 时它照章转红了(A/B 分基线红抓到的唯一一条),
    #    这正是它该有的行为;更新对照串而不是把它改成"宽松匹配"。
    #    2026-08-06 二次:补 contentForm / rankingCount / rankingTemplate 时又转红一次
    #    —— 同样照章更新对照串。**这条锁每红一次都说明有人往请求体加了字段**,
    #    正好逼他确认依赖数组也跟着加了。
    #    [WO_258] 对照串随判据改锚到 `settings` 的依赖数组,照旧写死。
    frozen = "[cardCount, styleKey, aspectRatio, clientContext, brandId]);"
    assert src.count(frozen) == 1, "构造反例失败 —— settings 的依赖数组写法变了,先更新这条对照"
    gaps = _body_closure_gaps(src.replace(frozen, "[cardCount, styleKey, clientContext, brandId]);"))
    assert gaps.get("settings") == {"aspectRatio"}, (
        f"去掉 aspectRatio 后判据读数是 {gaps} —— 要么恒真,要么认错了闭包")
    # 第二发:提交函数包进 useCallback ⇒ 它不再是每次渲染的新闭包,也要红
    anchor = "const doStart = async () => {"
    assert src.count(anchor) == 1, "构造反例失败 —— 提交函数的写法变了,先更新这条对照"
    assert _body_closure_gaps(src.replace(anchor, "const doStart = useCallback(async () => {")).get(
        "doStart"), "提交函数被 memo 了判据仍然绿"


def test_phone_frame_uses_a_real_device_ratio():
    """🔴 手机预览的机身必须是**真机比例**,不是被里面那张图撑出来的。

    Owner 2026-08-04:「这里的比例不像手机」。量了确实:机身宽 286,
    里面 3:4 的图按原比例撑到 381 高,加状态栏/导航/tab 约 455 →
    机身 1:1.59,而真机是 1:2.16。
    修法是给机身固定 aspect,图在屏幕区里等比居中(上下留黑,
    这也正是 3:4 图发到抖音上的真实样子)。
    """
    src = _tsx(DETAIL)
    m = re.search(r'data-testid="phone-frame"', src)
    assert m, "手机机身没有可定位的锚点"
    frame = src[max(0, m.start() - 600):m.start()]
    ar = re.search(r"aspect-\[(\d+)/([\d.]+)\]", frame)
    assert ar, "机身没有固定比例 —— 高度仍由里面那张图决定"
    ratio = float(ar.group(2)) / float(ar.group(1))
    assert 1.9 <= ratio <= 2.4, (
        f"机身比例 1:{ratio:.2f} 不像手机(真机 1:2.0~1:2.2)")
    # 必须不命中面:图不能再用 `w-full` 撑满机身,否则 aspect 会被顶破
    assert "max-h-full max-w-full object-contain" in src, (
        "图没有约束在屏幕区内 —— 固定了 aspect 却让图撑破它,等于没修")


def test_client_block_is_no_longer_a_numbered_step():
    """🔴「给哪个客户做」不再顶着步骤编号。

    Owner:「我怎么感觉 1 区完全没必要存在？1 区和 2 区感觉重复了」。
    根因比"重复"更具体:客户在创作中心左上角就定了、本页改不了
    (旧注释自己写着「本页不再选」)—— 它是**状态展示**,不是步骤。
    现在第 1 步是「想让客户搜什么」,客户名并进右栏资料卡抬头。

    🔴 [WO_258] 旧创作页删了。步骤条现在是 `ImageNoteFlowSteps` 的三步
       (0913P §4 P1 签过:「选题 / 制作图文 / 检查与修改」);客户在左上角选,
       图文页没选客户时只给一道「先选一位客户」的闸,不编号。
       客户名没被删,只是换了位置:侧栏的全局客户切换器是唯一的展示与同步点。
    """
    assert not _client_step_violations(_tsx(FLOW_STEPS), _flow_sources(), _tsx(APP_SIDEBAR))


def _client_step_violations(steps_src: str, flow: dict[str, str], sidebar: str) -> list[str]:
    out = []
    m = re.search(r"const STEPS = \[([^\]]*)\]", steps_src)
    steps = re.findall(r"'([^']*)'", m.group(1)) if m else []
    if not steps:
        out.append("找不到步骤表 —— 锚点失效")
    elif steps[0] != "选题":
        out.append(f"第 1 步现在是「{steps[0]}」—— 期望是真正要动手的第一件事")
    out += [f"步骤表里有客户那一步:「{s}」" for s in steps if "客户" in s]
    out += [f"{name} 里「给哪个客户做」还在" for name, src in flow.items() if "给哪个客户做" in src]
    if "<ClientSwitcherSidebar" not in sidebar:
        out.append("侧栏的全局客户切换器不见了 —— 客户名整个不见了,过头了")
    return out


def test_client_block_lock_has_power():
    """反臂:把「给哪个客户做」插回第 1 步 / 侧栏拿掉客户切换器,上面那条都要红。"""
    steps_src, flow, sidebar = _tsx(FLOW_STEPS), _flow_sources(), _tsx(APP_SIDEBAR)
    anchor = "const STEPS = ['"
    assert steps_src.count(anchor) == 1, "构造反例失败 —— 步骤表的写法变了,先更新这条对照"
    numbered = steps_src.replace(anchor, anchor + "给哪个客户做', '")
    got = _client_step_violations(numbered, {**flow, FLOW_STEPS.name: numbered}, sidebar)
    assert any("第 1 步现在是「给哪个客户做」" in v for v in got), got
    assert any("客户那一步" in v for v in got), got
    assert sidebar.count("<ClientSwitcherSidebar") == 1, "构造反例失败 —— 侧栏挂切换器的写法变了"
    assert any("客户名整个不见了" in v for v in _client_step_violations(
        steps_src, flow, sidebar.replace("<ClientSwitcherSidebar", "<NothingHere")))


def test_empty_gate_reason_shows_nothing_not_an_empty_box():
    """🔴 没有理由可说时，整块提示不显示 —— 不留一个只有感叹号的空黄框。

    本机实测抓到:原判据只有 `!canPublish`,而 `gateReason` 可以为空
    (闸没给理由 / 状态还没拉回来),页面上就出现一个一个字都没有的警告框。
    空提示比不提示更糟:用户知道有事、不知道是什么事、也不知道该做什么
    (feedback_hint_must_help_or_hide)。
    """
    src = _tsx(DETAIL)
    assert "{!canPublish && gateReason && (" in src, (
        "闸提示没有和 gateReason 一起判 —— 理由为空时会渲染一个空框")


def test_create_flow_does_not_compute_its_own_quota():
    """🔴 创作页不自己算配额 —— 配额只有报价一个口径(饱和曲线)。

    [WO_258] 原名 `test_rail_plan_hides_when_nothing_is_missing`,守两件事:
      ① 一条都不缺时不显示「还差多少」那块 —— **撤于 WO_258,产品 0913P 删了这块**:
         顶栏「买了 / 已做 / 还差」一句话(读 `/clients/{id}/plan`)由 019ec0f55 删除,
         全前端不再读 `/plan`,这个命题已经没有对象;
      ② 这里不能出现自己算配额的痕迹 —— 仍成立,改锚到图文创作流**全部**文件。
    """
    assert not _self_quota_violations(_flow_sources())


def _self_quota_violations(flow: dict[str, str]) -> list[str]:
    return [f"{name} 出现了自算配额的痕迹(Math.ceil)—— 配额只有报价一个口径"
            for name, src in flow.items() if "Math.ceil" in src]


def test_self_quota_lock_has_power():
    """反臂:往第 1 步塞一句按数量向上取整的配额,上面那条要红。"""
    flow = _flow_sources()
    poisoned = {**flow, TOPIC_PANEL.name: flow[TOPIC_PANEL.name]
                + "\nconst quota = Math.ceil(target / perPost);\n"}
    assert any(TOPIC_PANEL.name in v for v in _self_quota_violations(poisoned))


# ════════════════════════════════════════════════════════
# [WO_271 · 2026-09-23] 肯定式退役锁:图文旧入口页与它独有的机制不许回来
# ════════════════════════════════════════════════════════
# tests/ 里 18 个文件指着 09-08 / 09-13 / 09-15 删掉的图文旧页。WO_271 逐格处置:能在现役流程
# (/writing/image-note)里找到后继的格改指重判;**随整页一起消失**的格退役 ——
# `tests/RETIRED_TESTS.txt` 里给它们登记的接替者就是下面这一格:它断言那几页和它们
# 独有的机制**已不在**,回来一件就红。要重新引入其中任何一件,先改这里并写明为什么。
#: 已删的旧页,键 = frontend/src 下的路径(值写明哪一笔删的;值里带「已删」—— scripts/gate_refs_exist.py
#: 按行认否定断言)。分母 = 08-15 以来删掉的图文创作页全部(git log --diff-filter=D 机械枚举),
#: 除了 09-21 以新形状重建的 lib/imageNoteApi.ts,和不属创作流、tests/ 也没人指的 09-13 发布面板
_RETIRED_IMAGE_NOTE_PAGES = {
    "pages/Writing/DouyinImagePost.tsx": "已删 · 6b491ab23(09-08 #150 §4 旧图文入口页)",
    "pages/Writing/ImageNoteBatch.tsx": "已删 · 6b491ab23",
    "pages/Publishing/ImageNotePanel.tsx": "已删 · 6b491ab23",
    "pages/Writing/imageNoteQuoteScope.ts": "已删 · 6b491ab23",
    "pages/Writing/ImageNoteProof.tsx": "已删 · cd4aa3bf1(09-13 #203 校样页并回三栏详情页)",
    "pages/Writing/ImageNoteProofRoute.tsx": "已删 · cd4aa3bf1",
    "pages/Writing/ImageNoteStudio.tsx": "已删 · df9c142c5(09-15 #204 a2 制作台退役)",
    "pages/Writing/imageNoteStudioApi.ts": "已删 · df9c142c5",
}
#: 旧页独有、随页消失的机制:前端源码(去注释)里一处都不许再有。键 = 标记,值 = 它是什么、退了哪几格
_RETIRED_IMAGE_NOTE_MECHANISMS = {
    "imageNoteQuotePick": "按客户戳报价的作用域(test_image_note_quote_scope 五格)—— 新流程一切按全局 brandId 取数",
    "pilot-banner": "试点说明横幅 + 悬浮展开(async_and_entry 横幅格 · content_one_pipeline 悬浮反臂)",
    "RANKING_FIXED_CARDS": "前端另算一遍排名槽位(geovid_context_r3 槽位格)—— 槽位只由后端算",
    "clampPerCity": "「每城做几条」自由输入(v5 两格)—— 被选题列表取代(verify-image-note-topics A5 同守)",
}


def _retired_image_note_violations(sources: dict[str, str], flow: dict[str, str]) -> list[str]:
    """`sources` = 前端全部 ts/tsx(去注释);`flow` = 图文创作流文件(去注释)。返回违规清单。"""
    out: list[str] = []
    stems = [pathlib.Path(name).stem for name in _RETIRED_IMAGE_NOTE_PAGES]
    for rel, code in sources.items():
        for stem in stems:
            if re.search(r"""from\s+['"][^'"]*/""" + re.escape(stem) + r"""['"]""", code):
                out.append(f"{rel} 又 import 了已删的 {stem}")
        for marker in _RETIRED_IMAGE_NOTE_MECHANISMS:
            if marker in code:
                out.append(f"{rel} 出现了旧页独有的 {marker}")
    for name, code in flow.items():
        # 旧页挂过知识库卡(v2 共用组件格);现役图文流不画任何知识库卡,知识库一致性由
        # test_knowledge_card_uses_same_authorization_bits 在数据层守
        if "KnowledgeCard" in code:
            out.append(f"{name} 又画了知识库卡")
        # 顶栏「买了 / 已做 / 还差」(读 /plan)由产品 0913P(019ec0f55)删掉;每个关键词行自带已做 / 应做
        if "/plan" in code:
            out.append(f"{name} 又读了 /plan")
    # 「按客户戳报价」五格守的是:换客户后,上一个客户选的报价带不过来。新流程没有报价选择这份
    # state,靠的是选题面板**按当前客户整块重挂** —— 这一句没了,上一个客户勾的行 / 报价就可能留下
    if _PANEL_REMOUNT_PER_CLIENT not in flow.get(IMAGE_NOTE_ROUTE.name, ""):
        out.append(f"{IMAGE_NOTE_ROUTE.name} 不再按当前客户整块重挂选题面板")
    return out


_PANEL_REMOUNT_PER_CLIENT = "<ImageNoteTopicPanel key={currentBrandId} brandId={currentBrandId}"


def _frontend_sources() -> dict[str, str]:
    src_root = ROOT / "frontend" / "src"
    return {str(p.relative_to(ROOT)).replace("\\", "/"): _tsx(p)
            for p in src_root.rglob("*") if p.is_file() and p.suffix in (".ts", ".tsx")}


def test_retired_image_note_pages_stay_deleted():
    """🔴 [WO_271] 图文旧入口页(09-08 删)、校样页(09-13 删)与制作台(09-15 删)不许回来,
    它们独有的机制也不许换个地方回来。"""
    back = [name for name in _RETIRED_IMAGE_NOTE_PAGES if (ROOT / "frontend" / "src" / name).exists()]
    assert not back, f"已删的图文旧页回来了:{back}"
    sources = _frontend_sources()
    # 分母自证:扫不到前端源码时「一处都没有」是空话
    assert len(sources) > 500 and any(k.endswith("ImageNoteTopicPanel.tsx") for k in sources), (
        f"前端源码只扫到 {len(sources)} 个 —— 分母塌了")
    violations = _retired_image_note_violations(sources, _flow_sources())
    assert not violations, "\n".join(violations)


def test_retired_image_note_lock_has_power():
    """反臂:往活文件里塞回旧页 import / 旧机制 / 知识库卡 / /plan,上面那条的判定每一种都要命中;
    干净输入一条都不报(对照)。"""
    sources = _frontend_sources()
    flow = _flow_sources()
    assert _retired_image_note_violations(sources, flow) == []
    key = next(k for k in sources if k.endswith("ImageNoteTopicPanel.tsx"))
    for poison in ("import { X } from './DouyinImagePost';",
                   "const [imageNoteQuotePick] = useState(null);",
                   "<div data-testid=\"pilot-banner\" />",
                   "const RANKING_FIXED_CARDS = 2;",
                   "function clampPerCity(raw: string) { return 1; }"):
        got = _retired_image_note_violations({**sources, key: sources[key] + "\n" + poison + "\n"}, flow)
        assert got, f"塞了 {poison!r} 却没报"
    for poison in ("<ClientKnowledgeCard brandId={brandId} />", "void authFetch(`/api/geo-douyin/clients/${brandId}/plan`);"):
        got = _retired_image_note_violations(sources, {**flow, TOPIC_PANEL.name: flow[TOPIC_PANEL.name] + "\n" + poison + "\n"})
        assert got, f"图文流里塞了 {poison!r} 却没报"
    # 「回来了」那一查的牙:每个键都得是**真删过的那个路径**(在删它那一笔的父提交里存在)。
    # 键写错目录 ⇒ exists() 永远查不到,锁对那一页没牙 —— ImageNotePanel 就曾被写成 Writing/ 下(实在 Publishing/)
    import subprocess
    for name, why in _RETIRED_IMAGE_NOTE_PAGES.items():
        sha = re.search(r"\b[0-9a-f]{9}\b", why).group(0)
        if subprocess.run(["git", "-C", str(ROOT), "cat-file", "-e", f"{sha}^{{commit}}"],
                          capture_output=True).returncode != 0:
            pytest.skip(f"本地没有 {sha} 这笔历史(浅克隆?)—— 键的真实性这一查没跑")
        rc = subprocess.run(["git", "-C", str(ROOT), "cat-file", "-e", f"{sha}^:frontend/src/{name}"],
                            capture_output=True).returncode
        assert rc == 0, f"{name} 在 {sha} 的父提交里不存在 —— 键写错了路径,「回来了」那一查对它没牙"
    route = flow[IMAGE_NOTE_ROUTE.name]
    assert route.count(_PANEL_REMOUNT_PER_CLIENT) == 1, "对照前提:工作台路由里恰好一处按客户重挂"
    unkeyed = route.replace(_PANEL_REMOUNT_PER_CLIENT, "<ImageNoteTopicPanel brandId={currentBrandId}")
    assert _retired_image_note_violations(sources, {**flow, IMAGE_NOTE_ROUTE.name: unkeyed}), \
        "去掉按客户重挂的 key 却没报"
