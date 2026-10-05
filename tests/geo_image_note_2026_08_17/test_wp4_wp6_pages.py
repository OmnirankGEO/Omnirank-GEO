"""WP4/WP6 · 合同链三页本体的结构判据(规格 01 §2 / §3 / §4 / §5 / §10)。

## 这一份与 Playwright 那一套的分工

**能在浏览器里打的,一律去浏览器打**(`frontend/tests/image-note/`,21+ 条):
H1 文案、四区渲染、320px 主 CTA 可达、Tab 顺序、未知态没有重试按钮、
一篇只能选一个账号 —— 这些都是"渲染之后才存在的事实",源码扫描证明不了。

本文件只留**浏览器打不到的结构性事实**:
  · 三页职责互斥(第二页不许出现账号市场;发布页不许复用短视频广播面板);
  · 前端不算钱(资金红线,扫全文件而不是扫某一次渲染);
  · 状态词表覆盖(机器态漏一个 → 用户看到裸英文);
  · lane 隔离(图文不复用短视频面板)。

🔴 规格 01 §10 明写:「只有 TSX 字符串锁,没有真实浏览器行为测试」= 不可签收。
   所以本文件**不是**判据的主体,它只是浏览器打不到的那一小块补充。
"""
from __future__ import annotations

import io
import json
import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]

# [WO_271 · 2026-09-23] 再改锚:合同链那几页都已不在 —— 制作台 ImageNoteStudio.tsx 与 imageNoteStudioApi.ts
#   随 #204 a2 退役(df9c142c5),校样页 ImageNoteProof.tsx / ImageNoteProofRoute.tsx 随 #203 并回三栏详情页
#   (cd4aa3bf1)。现役对象:图文创作流(pages/Writing 下 ImageNote* / imageNote*,机械枚举)+ 作品详情页
#   DouyinPostDetail.tsx(「点进去那一屏」,即原来的第二页)。
WRITING = REPO / "frontend/src/pages/Writing"
TOPIC_PANEL = WRITING / "ImageNoteTopicPanel.tsx"   # 选题 / 开始制作(总价在这里读)
ROUTE = WRITING / "ImageNoteDetailRoute.tsx"        # 图文工作台路由
DETAIL = WRITING / "DouyinPostDetail.tsx"           # 第二页:作品详情
PW_SPEC = REPO / "frontend/tests/image-note/image-note-pages.spec.ts"
PW_CONFIG = REPO / "frontend/playwright.image-note.config.ts"
APP = REPO / "frontend/src/App.tsx"
WORKSPACE = REPO / "frontend/src/pages/Writing/WritingWorkspace.tsx"
PUBLISH_CENTER = REPO / "frontend/src/pages/Publishing/PublishCenter.tsx"

ALL_NEW = tuple(sorted(p for p in WRITING.iterdir()
                       if p.is_file() and p.suffix in (".ts", ".tsx")
                       and p.name.startswith(("ImageNote", "imageNote")))) + (DETAIL,)


def _text(path: pathlib.Path) -> str:
    return io.open(path, encoding="utf-8", errors="ignore").read()


def _strip_ts_comments(text: str) -> str:
    """剥掉 `//` 与 `/* */` 再判。

    🔴 本仓已五次栽在「判据命中自己写的注释」上。扫描类判据默认先剥注释,
       把"下次小心"变成默认行为。
    """
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return "\n".join(line.split("//", 1)[0] for line in text.splitlines())


# ── 已退役（Review #161）· 规格 01 §10/§3.1/§5 该条款由 Owner #150 作废 ──────────
# test_all_new_pages_exist：分母守卫的对象已删


def test_image_note_page_denominator_is_real():
    """[WO_271] 分母守卫(接上面退役那一格的班):ALL_NEW 改成机械枚举后,枚举塌了(改名 / 挪目录)
    下面按 ALL_NEW 参数化的扫描会整组消失,「一处都没有」就成了空话。"""
    names = {p.name for p in ALL_NEW}
    assert {TOPIC_PANEL.name, ROUTE.name, DETAIL.name} <= names and len(names) >= 6, \
        f"图文页只枚举到 {sorted(names)} —— 分母塌了"
    assert all(p.is_file() for p in ALL_NEW)



# ---------------------------------------------------------------------------
# 前端不算钱(资金红线 · 01 §10 / 08_billing §3.3)
# ---------------------------------------------------------------------------

_RMB_PATTERN = re.compile(r"/\s*130\b|¥|人民币|yuanToPoints|pointsToYuan")


@pytest.mark.parametrize("path", ALL_NEW, ids=lambda p: p.name)
def test_no_money_math_in_image_note_pages(path):
    hits = sorted({m.group(0) for m in _RMB_PATTERN.finditer(_strip_ts_comments(_text(path)))})
    assert not hits, f"{path.name} 在前端算钱:{hits}"


def test_money_scan_actually_fires():
    """反向对照:扫描对真实违规**代码**必须命中,否则"没找到"只是正则抓不到。"""
    for sample in ("const p = points / 130;", "显示 ¥199", "yuanToPoints(x)", "人民币价格"):
        assert _RMB_PATTERN.search(_strip_ts_comments(sample)), sample


def test_total_points_comes_from_server_field_only():
    """主按钮上的总价只能来自服务端字段,不能是前端 reduce/sum 出来的。"""
    # [WO_271] 改锚:制作台的 api 模块已删;现在总价读在选题面板(production-quote 回包的 total_points)
    for path in (TOPIC_PANEL,):
        code = _strip_ts_comments(_text(path))
        assert "total_points" in code, f"{path.name} 没读服务端总价字段"
        # 前端把逐项价加起来 = 自己算了一遍总价,服务端改口径时会静默不一致
        assert not re.search(r"total_points[^\n]*reduce", code), \
            f"{path.name} 在前端对逐项价求和"


# ---------------------------------------------------------------------------
# 三页职责互斥(01 §2 表格「明确不能有」)
# ---------------------------------------------------------------------------

# ── 已退役（Review #161）· 规格 01 §10/§3.1/§5 该条款由 Owner #150 作废 ──────────
# test_page_one_has_no_account_market_or_platform_preview：「页一」（DouyinImagePost）已删



def test_page_two_has_no_client_or_quote_selector():
    """第二页明确不能有:再选客户、再定篇数、整批选题。"""
    # [WO_271] 第二页 = 三栏详情页(校样页已并回,cd4aa3bf1)
    code = _strip_ts_comments(_text(DETAIL))
    for banned in ("选择客户", "换客户", "选择报价", "media_id"):
        assert banned not in code, f"第二页出现了不该有的东西:{banned}"


# ── 已退役（Review #161）· 规格 01 §10/§3.1/§5 该条款由 Owner #150 作废 ──────────
# test_publish_panel_does_not_reuse_short_video_broadcast：§10 条款作废：发布面已撤



# ── 已退役（Review #161）· 规格 01 §10/§3.1/§5 该条款由 Owner #150 作废 ──────────
# test_image_note_tab_is_first_class_not_under_short_video：§10 条款作废：发布中心一级类型不再是验收项



# ---------------------------------------------------------------------------
# 状态词表覆盖:漏一个机器态,用户就会看到裸英文
# ---------------------------------------------------------------------------

# ── 已退役（Review #161）· 规格 01 §10/§3.1/§5 该条款由 Owner #150 作废 ──────────
# test_batch_item_states_are_all_translated：BATCH_ITEM_COPY 导出随 imageNoteApi.ts 删除



# ── 已退役（Review #161）· 规格 01 §10/§3.1/§5 该条款由 Owner #150 作废 ──────────
# test_unknown_artifact_state_has_no_retry_action：ARTIFACT_COPY 导出已删；新页长出状态表时需重新钉「未知态不给重试」



def test_no_crypto_random_uuid_below_browser_baseline():
    """浏览器底线(Edge 90 / Chrome 90 / Safari iOS 16)没有 `crypto.randomUUID`。

    在那些机型上它是 undefined → 点提交抛 TypeError → **只有旧机型的用户没反应**。
    ESLint `compat/compat` 会红,这里再锁一道:判据不该只活在 lint 配置里。
    """
    for path in ALL_NEW:
        assert "randomUUID" not in _strip_ts_comments(_text(path)), path.name


# ---------------------------------------------------------------------------
# 真浏览器判据必须存在且覆盖 7 档视口(01 §8 / §10)
# ---------------------------------------------------------------------------

def test_playwright_suite_covers_seven_viewports():
    code = _text(PW_SPEC)
    for width in (320, 390, 768, 1024, 1366, 1440, 1920):
        assert f"w: {width}" in code, f"缺 {width}px 视口"


def test_playwright_suite_is_wired_to_a_real_config():
    cfg = _text(PW_CONFIG)
    assert "testDir: './tests/image-note'" in cfg
    assert "webServer" in cfg, "没有 webServer = 跑的不是真页面"


def test_page_two_is_reachable_by_a_real_route():
    """没有路由的页面在浏览器里到不了,判据只能退化成字符串扫描。"""
    code = _strip_ts_comments(_text(APP))
    assert "writing/image-note/:postId" in code
    # [WO_271] 校样页路由已并回:带作品 id 的这条路由现在挂图文工作台路由(按 id 挂三栏详情)
    assert re.search(r'path="writing/image-note/:postId" element=\{<ProtectedRoute[^>]*><ImageNoteDetailRoute />', code), \
        "带作品 id 的路由没挂到图文工作台"


# ── 已退役（Review #161）· 规格 01 §10/§3.1/§5 该条款由 Owner #150 作废 ──────────
# test_contract_lane_and_ad_hoc_lane_are_both_reachable：§3.1 双 lane 条款由 Owner #150 作废（Review 点名）



# ── 已退役（Review #161）· 规格 01 §10/§3.1/§5 该条款由 Owner #150 作废 ──────────
# test_quote_picker_exists_where_the_page_demands_one：壳级 onPickQuote 出口随 #150 §4 拆除；选报价已内置于 Studio（confirmed_keyword_id）



# ── 已退役（Review #161）· 规格 01 §10/§3.1/§5 该条款由 Owner #150 作废 ──────────
# test_panel_has_its_own_client_selector：全局客户作用域 = Owner 2026-08-03 裁定（左上角唯一客户同步点），§5.2 随合同产线作废



# ---------------------------------------------------------------------------
# 错误合同:前端不自造文案
# ---------------------------------------------------------------------------

# ── 已退役（Review #161）· 规格 01 §10/§3.1/§5 该条款由 Owner #150 作废 ──────────
# test_error_contract_fields_are_passed_through_not_invented：富错误合同（reason/impact/repair_hint）只在 /posts/{id}/regenerate 与 /quotes/{id}/delivery-plan 上；新页只打 /posts 与 /production-quote，不产出该合同



# ── 已退役（Review #161）· 规格 01 §10/§3.1/§5 该条款由 Owner #150 作废 ──────────
# test_stage_labels_match_backend_vocabulary：新页无 stage 面（0 命中）



# ── 已退役（Review #161）· 规格 01 §10/§3.1/§5 该条款由 Owner #150 作废 ──────────
# test_ready_posts_are_quote_scoped_at_the_source：ImageNotePanel 已删；新页不取 ready posts



def test_playwright_spec_has_paired_negative_controls():
    """本套判据自己也要有"反向对照存在性"的检查:
    只写正向断言的浏览器测试与源码扫描一样容易恒真。"""
    code = _text(PW_SPEC)
    assert code.count("toHaveCount(0)") >= 4, "浏览器判据里的反向对照太少"
    assert "反向对照" in code
